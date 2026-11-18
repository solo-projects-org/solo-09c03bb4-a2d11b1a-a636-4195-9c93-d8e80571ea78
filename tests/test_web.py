from __future__ import annotations

import contextlib
import csv
import io
import os
import tempfile
from urllib.parse import unquote

import pytest

# The web module binds its Store at import time, so point it at a throwaway
# database before anything imports app.main.
_TMP = tempfile.mkdtemp(prefix="fault-ops-tests-")
os.environ["FAULT_DB"] = os.path.join(_TMP, "web.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


@pytest.fixture()
def client():
    main.store.init()
    with TestClient(main.app) as c:
        yield c
    with main.store.write() as conn:
        for table in ("publish_items", "publishes", "mappings", "events", "operation_log"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence")


def revision_form(client, unit, day, **overrides):
    page = client.get(f"/days/{unit}/{day}").text
    data = {
        "revision_set": _hidden(page, "revision_set"),
        "recipe": _hidden(page, "recipe"),
    }
    data.update(overrides)
    return data


def _hidden(page, name):
    import re

    m = re.search(rf'name="{name}" value="([^"]*)"', page)
    return m.group(1) if m else ""


def post_event(client, **kw):
    data = dict(
        event_no="EVT-1",
        revision=1,
        unit="WTG-01",
        firmware_version="FW-1.4",
        fault_code="E-1042",
        occurred_at="2026-03-01T10:00:00Z",
    )
    data.update(kw)
    return client.post("/events", data=data, follow_redirects=False)


def test_pages_render(client):
    for path in ("/", "/events", "/mappings", "/api/days", "/healthz"):
        r = client.get(path)
        assert r.status_code == 200, path


def test_event_create_via_form_and_validation_error_shown(client):
    r = post_event(client, revision=2)
    assert r.status_code == 200
    assert "修订号必须为 1" in r.text

    r = post_event(client)
    assert r.status_code == 303
    assert "已登记事件" in unquote(r.headers["location"])

    r = post_event(client, revision=3)
    assert r.status_code == 200
    assert "修订号必须为 2" in r.text

    page = client.get("/events").text
    assert "EVT-1" in page


def test_publish_flow_over_http(client):
    post_event(client)
    r = client.post(
        "/mappings",
        data={
            "firmware_version": "FW-1.4",
            "fault_code": "E-1042",
            "revision": 1,
            "category": "变桨系统",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    page = client.get("/days/WTG-01/2026-03-01").text
    assert "按现行修订重算并发布" in page

    form = revision_form(client, "WTG-01", "2026-03-01")
    assert form["revision_set"] == "EVT-1@1"
    r = client.post(
        "/days/WTG-01/2026-03-01/publish", data=form, follow_redirects=False
    )
    assert r.status_code == 303
    assert "ok=1" in r.headers["location"]

    api = client.get("/api/days/WTG-01/2026-03-01")
    assert api.status_code == 200
    body = api.json()
    assert body["available"] is True
    assert body["events"][0]["category"] == "变桨系统"

    # Correct the event: the day immediately becomes unavailable.
    post_event(client, revision=2, occurred_at="2026-03-01T12:00:00Z")
    api = client.get("/api/days/WTG-01/2026-03-01")
    assert api.status_code == 409
    assert api.json()["available"] is False

    # Stale token from before the correction is rejected; the page offers fresh one.
    r = client.post(
        "/days/WTG-01/2026-03-01/publish",
        data={"revision_set": "EVT-1@1", "recipe": form["recipe"]},
        follow_redirects=False,
    )
    assert "ok=0" in r.headers["location"]
    assert client.get("/api/days/WTG-01/2026-03-01").status_code == 409

    r = client.post(
        "/days/WTG-01/2026-03-01/publish",
        data=revision_form(client, "WTG-01", "2026-03-01"),
        follow_redirects=False,
    )
    assert "ok=1" in r.headers["location"]
    assert client.get("/api/days/WTG-01/2026-03-01").json()["event_revisions"] == "EVT-1@2"


def test_missing_mapping_blocks_publish_and_is_listed(client):
    post_event(client)
    r = client.post(
        "/days/WTG-01/2026-03-01/publish",
        data=revision_form(client, "WTG-01", "2026-03-01"),
        follow_redirects=False,
    )
    assert "ok=0" in r.headers["location"]
    assert "缺少类别映射" in unquote(r.headers["location"])

    page = client.get("/days/WTG-01/2026-03-01").text
    assert "缺少映射的事件" in page
    assert "EVT-1" in page
    assert client.get("/api/days").json() == {"days": []}


def test_api_unavailable_reasons(client):
    post_event(client)
    body = client.get("/api/days/WTG-01/2026-03-01").json()
    assert body["available"] is False
    assert "缺少类别映射" in body["reasons"]
    assert body["missing_mappings"][0]["fault_code"] == "E-1042"


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #

def _publish_day(client, unit, day):
    r = client.post(
        f"/days/{unit}/{day}/publish",
        data=revision_form(client, unit, day),
        follow_redirects=False,
    )
    assert r.status_code == 303 and "ok=1" in r.headers["location"]


def _add_mapping(client, fw="FW-1.4", code="E-1042", revision=1, category="变桨系统"):
    r = client.post(
        "/mappings",
        data={
            "firmware_version": fw,
            "fault_code": code,
            "revision": revision,
            "category": category,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303


def _csv_rows(response):
    return list(csv.reader(io.StringIO(response.text)))


def test_chunks_are_bounded_even_when_one_row_is_huge(client, monkeypatch):
    """Encoding is flushed per row: peak buffer is one row + one chunk, not a batch."""
    monkeypatch.setattr(main, "EXPORT_BATCH_SIZE", 500)
    monkeypatch.setattr(main, "EXPORT_CHUNK_BYTES", 4096)
    _add_mapping(client)
    for n in range(1, 6):
        post_event(client, event_no=f"EVT-{n}", occurred_at=f"2026-03-01T{n:02d}:00:00Z")
    _publish_day(client, "WTG-01", "2026-03-01")
    huge_recipe = "R" * 100_000

    import asyncio

    async def collect():
        sizes = []
        async for chunk in main._export_csv_chunks(
            "WTG-01", "2026-03-01", 1, huge_recipe
        ):
            sizes.append(len(chunk))
        return sizes

    sizes = asyncio.run(collect())
    assert len(sizes) >= 5
    # Header chunk is small; every row is emitted on its own because one row
    # already exceeds the chunk size.
    assert max(sizes) < 101_000
    assert sum(sizes) > 5 * 100_000


def test_csv_export_columns_and_publish_id(client):
    post_event(client)
    _add_mapping(client)
    _publish_day(client, "WTG-01", "2026-03-01")

    r = client.get("/api/days/WTG-01/2026-03-01/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = _csv_rows(r)
    assert rows[0] == [
        "unit", "utc_day", "event_no", "revision", "firmware_version",
        "fault_code", "category", "occurred_at", "mapping_revision",
        "publish_id", "recipe",
    ]
    assert rows[1][:9] == [
        "WTG-01", "2026-03-01", "EVT-1", "1", "FW-1.4",
        "E-1042", "变桨系统", "2026-03-01T10:00:00.000000Z", "1",
    ]
    assert rows[1][9] == "1"  # publish_id of the first publish
    assert rows[1][10].startswith("E[EVT-1@1]|M[")


def test_csv_export_unavailable_matches_json_reasons(client):
    post_event(client)
    r = client.get("/api/days/WTG-01/2026-03-01/export.csv")
    assert r.status_code == 409
    assert r.json()["available"] is False
    assert "缺少类别映射" in r.json()["reasons"]

    assert client.get("/api/days/WTG-99/2026-03-01/export.csv").json()["reasons"] == [
        "该日没有现行日档"
    ]

    _add_mapping(client)
    _publish_day(client, "WTG-01", "2026-03-01")
    post_event(client, revision=2)
    r = client.get("/api/days/WTG-01/2026-03-01/export.csv")
    assert r.status_code == 409
    assert "现行资格已撤销，等待重算发布" in r.json()["reasons"]


def test_day_page_has_download_link_only_when_current(client):
    post_event(client)
    _add_mapping(client)
    assert "/export.csv" not in client.get("/days/WTG-01/2026-03-01").text
    _publish_day(client, "WTG-01", "2026-03-01")
    page = client.get("/days/WTG-01/2026-03-01").text
    assert "/api/days/WTG-01/2026-03-01/export.csv" in page
    assert "下载 CSV" in page


def test_csv_export_is_identical_to_json_and_spans_many_batches(client, monkeypatch):
    # Shrink the batch size so a handful of rows already crosses several reads.
    monkeypatch.setattr(main, "EXPORT_BATCH_SIZE", 2)
    fw, code = "FW-1.4", "E-1042"
    _add_mapping(client, fw, code)
    for n in range(1, 8):
        post_event(
            client,
            event_no=f"EVT-{n}",
            occurred_at=f"2026-03-01T{n:02d}:00:00Z",
        )
    _publish_day(client, "WTG-01", "2026-03-01")

    api = client.get("/api/days/WTG-01/2026-03-01").json()
    rows = _csv_rows(client.get("/api/days/WTG-01/2026-03-01/export.csv"))
    header, body = rows[0], rows[1:]
    assert header[:2] == ["unit", "utc_day"]
    assert len(body) == 7 == len(api["events"])
    assert [r[2] for r in body] == [e["event_no"] for e in api["events"]]
    assert [r[7] for r in body] == [e["occurred_at"] for e in api["events"]]
    assert {r[9] for r in body} == {str(api["publish_id"])}
    assert len({r[10] for r in body}) == 1


def test_csv_export_streams_one_batch_at_a_time(client, monkeypatch):
    """The generator must not pre-materialise all items of the publish."""
    _add_mapping(client)
    for n in range(1, 6):
        post_event(client, event_no=f"EVT-{n}", occurred_at=f"2026-03-01T{n:02d}:00:00Z")
    _publish_day(client, "WTG-01", "2026-03-01")
    publish_id = client.get("/api/days/WTG-01/2026-03-01").json()["publish_id"]

    calls: list[int] = []
    real = main._fetch_export_batch

    def counting_batch(pid, after, limit):
        rows = real(pid, after, limit)
        calls.append(len(rows))
        return rows

    monkeypatch.setattr(main, "EXPORT_BATCH_SIZE", 2)
    monkeypatch.setattr(main, "_fetch_export_batch", counting_batch)
    r = client.get("/api/days/WTG-01/2026-03-01/export.csv")
    assert r.status_code == 200
    assert calls == [2, 2, 1]          # paginated, never one 5-row fetch
    assert len(_csv_rows(r)) == 6      # header + 5


def test_csv_export_uses_keyset_pagination_beyond_one_batch(client, monkeypatch):
    """Every row appears exactly once even when batches split equal timestamps."""
    monkeypatch.setattr(main, "EXPORT_BATCH_SIZE", 1)
    _add_mapping(client)
    for n in range(1, 5):
        post_event(client, event_no=f"EVT-{n}", occurred_at="2026-03-01T10:00:00Z")
    _publish_day(client, "WTG-01", "2026-03-01")
    body = _csv_rows(client.get("/api/days/WTG-01/2026-03-01/export.csv"))[1:]
    assert [r[2] for r in body] == ["EVT-1", "EVT-2", "EVT-3", "EVT-4"]


def test_batches_hold_no_connection_between_reads(client):
    """Each batch's SQLite connection is closed before the next one is opened."""
    _add_mapping(client)
    post_event(client)
    _publish_day(client, "WTG-01", "2026-03-01")

    live: list[int] = []
    real_read = main.store.read

    @contextlib.contextmanager
    def tracking_read():
        cm = real_read()
        inner = cm.__enter__()
        live.append(1)
        try:
            yield inner
        finally:
            live.pop()
            cm.__exit__(None, None, None)

    main.store.read = tracking_read
    try:
        r = client.get("/api/days/WTG-01/2026-03-01/export.csv")
    finally:
        main.store.read = real_read
    assert r.status_code == 200 and len(_csv_rows(r)) == 2
    assert live == []  # nothing left pinned after the response completes


def test_in_flight_download_keeps_its_publish_across_changes(client, monkeypatch):
    """A download may be split by corrections/mapping revisions/re-publishes.

    The client still receives the complete, self-consistent file of the publish
    it started on, while a request made after the change resolves fresh.
    """
    monkeypatch.setattr(main, "EXPORT_BATCH_SIZE", 1)
    _add_mapping(client, "FW-1.4", "E-1042", 1, "变桨系统")
    for n in range(1, 7):
        post_event(
            client,
            event_no=f"EVT-{n}",
            occurred_at=f"2026-03-01T{n:02d}:00:00Z",
        )
    _publish_day(client, "WTG-01", "2026-03-01")
    first = client.get("/api/days/WTG-01/2026-03-01").json()
    first_publish = first["publish_id"]

    # Walk the export generator by hand and mutate state between batches,
    # exactly as a slow client's download would interleave with operator writes.
    import asyncio

    async def drive():
        gen = main._export_csv_chunks(
            "WTG-01", "2026-03-01", first_publish, first["recipe"]
        )
        chunks = [await gen.__anext__()]
        # Between batch 1 and 2: correct an event, revise the mapping, re-publish.
        post_event(
            client, revision=2, occurred_at="2026-03-01T03:30:00Z",
            fault_code="E-1042",
        )
        _add_mapping(client, "FW-1.4", "E-1042", 2, "新类别")
        assert client.get("/api/days/WTG-01/2026-03-01").status_code == 409
        assert (
            client.get("/api/days/WTG-01/2026-03-01/export.csv").status_code == 409
        )
        _publish_day(client, "WTG-01", "2026-03-01")
        async for chunk in gen:
            chunks.append(chunk)
        return b"".join(chunks)

    payload = asyncio.run(drive())
    rows = list(csv.reader(io.StringIO(payload.decode("utf-8"))))
    body = rows[1:]
    # The interrupted-but-not-broken download is the *old* publish in full.
    assert [r[2] for r in body] == [f"EVT-{n}" for n in range(1, 7)]
    assert {r[9] for r in body} == {str(first_publish)}
    assert {r[6] for r in body} == {"变桨系统"}
    assert {r[10] for r in body} == {first["recipe"]}

    # A request after the changes resolves to the new publish (and its file).
    fresh = client.get("/api/days/WTG-01/2026-03-01/export.csv")
    fresh_body = _csv_rows(fresh)[1:]
    assert {r[9] for r in fresh_body} != {str(first_publish)}
    assert {r[6] for r in fresh_body} == {"新类别"}
    # EVT-1's correction moved it within the day, so it is still present once.
    assert sorted(r[2] for r in fresh_body) == [f"EVT-{n}" for n in range(1, 7)]
    assert [r[3] for r in fresh_body if r[2] == "EVT-1"] == ["2"]


def test_disconnect_mid_download_releases_batches(client, monkeypatch):
    """Abandoning the stream (client gone) stops reads and closes the connection."""
    monkeypatch.setattr(main, "EXPORT_BATCH_SIZE", 1)
    monkeypatch.setattr(main, "EXPORT_CHUNK_BYTES", 1)
    _add_mapping(client)
    for n in range(1, 9):
        post_event(client, event_no=f"EVT-{n}", occurred_at=f"2026-03-01T{n:02d}:00:00Z")
    _publish_day(client, "WTG-01", "2026-03-01")
    publish_id = client.get("/api/days/WTG-01/2026-03-01").json()["publish_id"]

    fetched: list[int] = []
    real = main._fetch_export_batch
    monkeypatch.setattr(
        main,
        "_fetch_export_batch",
        lambda pid, after, limit: fetched.append(1) or real(pid, after, limit),
    )

    import asyncio

    async def abandon():
        gen = main._export_csv_chunks("WTG-01", "2026-03-01", publish_id, "r")
        await gen.__anext__()
        await gen.aclose()   # Starlette does this when the client disconnects

    asyncio.run(abandon())
    assert fetched == [1]  # only the batch already yielded was read

    # The abandoned reader left no lock behind: writers still proceed promptly.
    post_event(client, revision=2, event_no="EVT-1")
    assert client.get("/api/days/WTG-01/2026-03-01").status_code == 409
