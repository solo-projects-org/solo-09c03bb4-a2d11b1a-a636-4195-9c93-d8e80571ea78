"""Domain-level tests: every rule is exercised through the same Store the web app uses."""

from __future__ import annotations

import sqlite3

import pytest

from app import domain
from app.db import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "ops.db"))
    s.init()
    return s


def ev(store, event_no, revision, unit, fw, code, at):
    domain.add_event_revision(
        store,
        domain.normalise_event_input(event_no, revision, unit, fw, code, at),
    )


def map_(store, fw, code, revision, category):
    domain.add_mapping_revision(store, fw, code, revision, category)


def publish(store, unit, day):
    with store.read() as conn:
        view = domain.day_view(conn, unit, day)
        token, recipe = view.revision_token, view.recipe
    return domain.publish_day(store, unit, day, token, recipe)


def test_revision_numbers_must_be_consecutive(store):
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    with pytest.raises(domain.Conflict):
        ev(store, "E1", 3, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    with pytest.raises(domain.Conflict):
        ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    ev(store, "E1", 2, "WTG-01", "FW-1", "B", "2026-01-01T02:00:00Z")
    with store.read() as conn:
        current = domain.current_event_revisions(conn)
    assert [(c["event_no"], c["revision"]) for c in current] == [("E1", 2)]


def test_only_latest_revision_counts_but_history_kept(store):
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    ev(store, "E1", 2, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    with store.read() as conn:
        assert len(domain.event_history(conn)) == 2
        assert len(domain.current_event_revisions(conn)) == 1


def test_rejects_non_utc_and_naive_timestamps(store):
    with pytest.raises(domain.DomainError):
        domain.parse_utc("2026-01-01T08:00:00")
    with pytest.raises(domain.DomainError):
        domain.parse_utc("2026-01-01T08:00:00+08:00")
    assert domain.parse_utc("2026-01-01T08:00:00Z").hour == 8


def test_missing_mapping_blocks_whole_day_and_lists_events(store):
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    ev(store, "E2", 1, "WTG-01", "FW-1", "B", "2026-01-01T02:00:00Z")
    map_(store, "FW-1", "A", 1, "变桨系统")

    with store.read() as conn:
        view = domain.day_view(conn, "WTG-01", "2026-01-01")
    assert [m["event_no"] for m in view.missing] == ["E2"]
    assert view.publish is None

    with pytest.raises(domain.DomainError, match="缺少类别映射"):
        publish(store, "WTG-01", "2026-01-01")

    with store.read() as conn:
        assert domain.available_days(conn) == []


def test_same_fault_code_differs_by_firmware(store):
    map_(store, "FW-1", "E-1", 1, "变桨系统")
    map_(store, "FW-2", "E-1", 1, "偏航系统")
    ev(store, "E1", 1, "WTG-01", "FW-1", "E-1", "2026-01-01T01:00:00Z")
    ev(store, "E2", 1, "WTG-01", "FW-2", "E-1", "2026-01-01T02:00:00Z")
    publish(store, "WTG-01", "2026-01-01")

    with store.read() as conn:
        view = domain.day_view(conn, "WTG-01", "2026-01-01")
        items = domain.publish_items(conn, view.publish["id"])
    assert [(i["event_no"], i["category"]) for i in items] == [
        ("E1", "变桨系统"),
        ("E2", "偏航系统"),
    ]


def test_event_correction_revokes_old_and_new_day(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T23:00:00Z")
    ev(store, "E2", 1, "WTG-01", "FW-1", "A", "2026-01-02T01:00:00Z")
    publish(store, "WTG-01", "2026-01-01")
    publish(store, "WTG-01", "2026-01-02")

    # Move E1 to the next UTC day.
    ev(store, "E1", 2, "WTG-01", "FW-1", "A", "2026-01-02T02:00:00Z")

    with store.read() as conn:
        d1 = domain.day_view(conn, "WTG-01", "2026-01-01")
        d2 = domain.day_view(conn, "WTG-01", "2026-01-02")
    assert d1.publish is None and d1.revoked
    assert d2.publish is None and d2.revoked
    assert "事件 E1 修订 2" in d1.revoked[0]["revoked_reason"]


def test_event_correction_of_unit_revokes_both_units(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    publish(store, "WTG-01", "2026-01-01")

    ev(store, "E1", 2, "WTG-02", "FW-1", "A", "2026-01-01T01:00:00Z")

    with store.read() as conn:
        old = domain.day_view(conn, "WTG-01", "2026-01-01")
        new = domain.day_view(conn, "WTG-02", "2026-01-01")
    assert old.publish is None
    assert new.publish is None
    assert new.events and new.events[0]["event_no"] == "E1"


def test_mapping_revision_revokes_only_affected_days(store):
    map_(store, "FW-1", "A", 1, "旧类")
    map_(store, "FW-1", "B", 1, "B类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    ev(store, "E2", 1, "WTG-01", "FW-1", "B", "2026-01-02T01:00:00Z")
    ev(store, "E3", 1, "WTG-02", "FW-1", "A", "2026-01-03T01:00:00Z")
    publish(store, "WTG-01", "2026-01-01")
    publish(store, "WTG-01", "2026-01-02")
    publish(store, "WTG-02", "2026-01-03")

    map_(store, "FW-1", "A", 2, "新类")

    with store.read() as conn:
        assert domain.day_view(conn, "WTG-01", "2026-01-01").publish is None
        assert domain.day_view(conn, "WTG-02", "2026-01-03").publish is None
        # Unrelated day still queryable.
        assert domain.day_view(conn, "WTG-01", "2026-01-02").publish is not None
    with store.read() as conn:
        avail = {(d["unit"], d["day"]) for d in domain.available_days(conn)}
    assert avail == {("WTG-01", "2026-01-02")}


def test_mapping_revision_must_increment(store):
    map_(store, "FW-1", "A", 1, "A类")
    with pytest.raises(domain.Conflict):
        map_(store, "FW-1", "A", 3, "A类")


def test_republish_uses_latest_mapping_revision(store):
    map_(store, "FW-1", "A", 1, "旧类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    publish(store, "WTG-01", "2026-01-01")
    map_(store, "FW-1", "A", 2, "新类")
    publish(store, "WTG-01", "2026-01-01")

    with store.read() as conn:
        view = domain.day_view(conn, "WTG-01", "2026-01-01")
        assert view.publish is not None
        items = domain.publish_items(conn, view.publish["id"])
        assert [(i["category"], i["map_revision"]) for i in items] == [("新类", 2)]
        assert len(view.revoked) == 1


def test_stale_mapping_recipe_refused_even_with_fresh_event_token(store):
    map_(store, "FW-1", "A", 1, "旧类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    publish(store, "WTG-01", "2026-01-01")
    with store.read() as conn:
        view = domain.day_view(conn, "WTG-01", "2026-01-01")
        token, stale_recipe = view.revision_token, view.recipe

    map_(store, "FW-1", "A", 2, "新类")

    # Event revisions are still current, but the mapping basis the operator saw is
    # stale: publishing must be refused, not silently produce a mixed file.
    with pytest.raises(domain.Conflict, match="映射"):
        domain.publish_day(store, "WTG-01", "2026-01-01", token, stale_recipe)

    with store.read() as conn:
        view = domain.day_view(conn, "WTG-01", "2026-01-01")
        assert view.publish is None
        assert len(view.revoked) == 1


def test_stale_revision_token_refused_and_old_stays_non_current(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    publish(store, "WTG-01", "2026-01-01")
    with store.read() as conn:
        stale = domain.day_view(conn, "WTG-01", "2026-01-01")

    ev(store, "E1", 2, "WTG-01", "FW-1", "A", "2026-01-01T05:00:00Z")

    with pytest.raises(domain.Conflict):
        domain.publish_day(
            store, "WTG-01", "2026-01-01", stale.revision_token, stale.recipe
        )

    with store.read() as conn:
        view = domain.day_view(conn, "WTG-01", "2026-01-01")
        assert view.publish is None
        assert len(view.revoked) == 1

    # The refused recompute must not have resurrected anything; the correct
    # token then publishes exactly one current file.
    publish(store, "WTG-01", "2026-01-01")
    with store.read() as conn:
        assert domain.day_view(conn, "WTG-01", "2026-01-01").publish is not None


def test_failed_publish_rolls_back_and_keeps_old_revoked(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    publish(store, "WTG-01", "2026-01-01")

    # Add an event whose mapping is missing: the day becomes unpublishable.
    ev(store, "E2", 1, "WTG-01", "FW-9", "Z", "2026-01-01T02:00:00Z")
    with pytest.raises(domain.DomainError):
        publish(store, "WTG-01", "2026-01-01")

    with store.read() as conn:
        view = domain.day_view(conn, "WTG-01", "2026-01-01")
        assert view.publish is None
        assert len(view.revoked) == 1
        # No half-written publish_items: the only publish row is the revoked one.
        publishes = domain.all_publishes(conn)
        assert len(publishes) == 1
        assert conn.execute("SELECT COUNT(*) FROM publish_items").fetchone()[0] == 1


def test_restart_demotes_but_never_promotes(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    publish(store, "WTG-01", "2026-01-01")

    # Simulate a crash that left a CURRENT row for an outdated revision set: this
    # can only happen via corruption/hand-edit, because normal writes are atomic.
    with store.write() as conn:
        conn.execute(
            "UPDATE publishes SET event_revisions = 'E1@0' WHERE status = 'CURRENT'"
        )

    restarted = Store(store.path)
    restarted.init()
    assert domain.purge_stale_current(restarted) == 1
    with restarted.read() as conn:
        assert domain.day_view(conn, "WTG-01", "2026-01-01").publish is None

    # A healthy CURRENT publish survives a restart untouched.
    publish(store, "WTG-01", "2026-01-01")
    assert domain.purge_stale_current(restarted) == 0
    with restarted.read() as conn:
        assert domain.day_view(conn, "WTG-01", "2026-01-01").publish is not None


def test_revoked_publish_content_is_retained_for_audit(store):
    map_(store, "FW-1", "A", 1, "旧类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    publish(store, "WTG-01", "2026-01-01")
    map_(store, "FW-1", "A", 2, "新类")
    publish(store, "WTG-01", "2026-01-01")

    with store.read() as conn:
        view = domain.day_view(conn, "WTG-01", "2026-01-01")
        old = view.revoked[0]
        old_items = domain.publish_items(conn, old["id"])
    assert [(i["category"], i["map_revision"]) for i in old_items] == [("旧类", 1)]


def test_unique_current_per_day_enforced_by_db(store):
    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    publish(store, "WTG-01", "2026-01-01")
    with pytest.raises(sqlite3.IntegrityError):
        with store.write() as conn:
            conn.execute(
                "INSERT INTO publishes (unit, day, event_revisions, recipe,"
                " event_count, status, created_at)"
                " VALUES ('WTG-01', '2026-01-01', 'x', 'y', 1, 'CURRENT', 'now')"
            )


def test_concurrent_publishes_leave_exactly_one_current(store):
    import threading

    map_(store, "FW-1", "A", 1, "A类")
    ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
    with store.read() as conn:
        view = domain.day_view(conn, "WTG-01", "2026-01-01")
        token, recipe = view.revision_token, view.recipe

    errors: list[BaseException] = []

    def worker():
        try:
            domain.publish_day(store, "WTG-01", "2026-01-01", token, recipe)
        except BaseException as exc:  # pragma: no cover - only on race bugs
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    with store.read() as conn:
        current = conn.execute(
            "SELECT COUNT(*) FROM publishes WHERE status = 'CURRENT'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM publishes").fetchone()[0]
    assert current == 1
    assert total == 4


def test_concurrent_event_revisions_do_not_duplicate(store):
    import threading

    results: list[str] = []

    def worker():
        try:
            ev(store, "E1", 1, "WTG-01", "FW-1", "A", "2026-01-01T01:00:00Z")
            results.append("ok")
        except domain.Conflict:
            results.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
