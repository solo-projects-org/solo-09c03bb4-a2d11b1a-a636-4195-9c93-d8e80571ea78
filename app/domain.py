"""Domain rules.

The domain is deliberately split from HTTP so it can be tested directly and so
that every rule is checked under the same transaction that mutates state.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from .db import Store, log, utc_now_iso

EVENT_NO_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._: -]{1,64}$")
CODE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,32}$")
CATEGORY_RE = re.compile(r"^[^\x00-\x1f]{1,64}$")


class DomainError(Exception):
    """A rule violation that should be shown to the operator as-is."""


class Conflict(DomainError):
    """Stale/duplicate revision or concurrent publish attempt."""


@dataclass(frozen=True)
class EventRevision:
    event_no: str
    revision: int
    unit: str
    firmware_version: str
    fault_code: str
    occurred_at: datetime


def parse_utc(value: str) -> datetime:
    """Accept ISO-8601 with an explicit UTC offset and normalise to UTC.

    A naive timestamp is rejected: the operator was asked for a UTC instant and
    guessing would silently shift events across day boundaries.
    """
    raw = (value or "").strip()
    if not raw:
        raise DomainError("UTC 时刻不能为空")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise DomainError(f"无法解析 UTC 时刻: {raw!r}") from None
    if dt.tzinfo is None:
        raise DomainError("时刻必须带 UTC 时区标记（例如 2026-01-01T08:00:00Z）")
    if dt.utcoffset() is not None and dt.utcoffset().total_seconds() != 0:
        raise DomainError("时刻必须是 UTC（偏移为 +00:00）")
    return dt.astimezone(timezone.utc)


def canonical_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require(value: str, label: str, pattern: re.Pattern[str]) -> str:
    cleaned = (value or "").strip()
    if not pattern.match(cleaned):
        raise DomainError(f"{label} 格式不合法: {value!r}")
    return cleaned


def normalise_event_input(
    event_no: str,
    revision: int,
    unit: str,
    firmware_version: str,
    fault_code: str,
    occurred_at: str,
) -> EventRevision:
    return EventRevision(
        event_no=_require(event_no, "事件号", EVENT_NO_RE),
        revision=int(revision),
        unit=_require(unit, "机组", TOKEN_RE),
        firmware_version=_require(firmware_version, "固件版本", TOKEN_RE),
        fault_code=_require(fault_code, "故障码", CODE_RE),
        occurred_at=parse_utc(occurred_at),
    )


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #

def add_event_revision(store: Store, ev: EventRevision) -> None:
    """Record an event revision.  Revision numbers must increase by exactly 1.

    Every affected (unit, day) - the ones the event used to occupy *and* the ones
    it now occupies - loses its current publish in the same transaction.  That is
    the "原日档与新日档都立即失去现行资格" rule.
    """
    if ev.revision < 1:
        raise DomainError("修订号必须从 1 开始递增")
    occurred = canonical_utc(ev.occurred_at)
    with store.write() as conn:
        prior = conn.execute(
            "SELECT id, revision, unit, occurred_at FROM events"
            " WHERE event_no = ? ORDER BY revision DESC LIMIT 1",
            (ev.event_no,),
        ).fetchone()
        expected = 1 if prior is None else prior["revision"] + 1
        if ev.revision != expected:
            raise Conflict(
                f"事件 {ev.event_no} 的修订号必须为 {expected}，收到 {ev.revision}"
                "（不允许跳号或重复提交）"
            )

        now = utc_now_iso()
        conn.execute(
            "INSERT INTO events (event_no, revision, unit, firmware_version,"
            " fault_code, occurred_at, revision_created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev.event_no,
                ev.revision,
                ev.unit,
                ev.firmware_version,
                ev.fault_code,
                occurred,
                now,
            ),
        )

        affected: set[tuple[str, str]] = {(ev.unit, occurred[:10])}
        if prior is not None:
            affected.add((prior["unit"], prior["occurred_at"][:10]))
            # A correction may change any field of the event; the old publish for
            # the old day was built from the old revision, so it must go too even
            # if unit/day happen to be unchanged.
        for unit, day in sorted(affected):
            revoke_day_locked(
                conn,
                unit,
                day,
                f"事件 {ev.event_no} 修订 {ev.revision} 更正",
            )
        log(
            conn,
            "event.revision",
            f"{ev.event_no} r{ev.revision} unit={ev.unit} day={occurred[:10]}",
        )


def current_event_revisions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Latest revision of every event number."""
    return conn.execute(
        "SELECT e.* FROM events e"
        " JOIN (SELECT event_no, MAX(revision) AS r FROM events GROUP BY event_no) m"
        "   ON m.event_no = e.event_no AND m.r = e.revision"
        " ORDER BY e.occurred_at, e.event_no"
    ).fetchall()


def event_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events ORDER BY event_no, revision"
    ).fetchall()


# --------------------------------------------------------------------------- #
# Mapping
# --------------------------------------------------------------------------- #

def add_mapping_revision(
    store: Store,
    firmware_version: str,
    fault_code: str,
    revision: int,
    category: str,
) -> None:
    fw = _require(firmware_version, "固件版本", TOKEN_RE)
    code = _require(fault_code, "故障码", CODE_RE)
    cat = _require(category, "类别", CATEGORY_RE)
    if revision < 1:
        raise DomainError("映射修订号必须从 1 开始递增")

    with store.write() as conn:
        prior = conn.execute(
            "SELECT revision FROM mappings WHERE firmware_version = ? AND fault_code = ?"
            " ORDER BY revision DESC LIMIT 1",
            (fw, code),
        ).fetchone()
        expected = 1 if prior is None else prior["revision"] + 1
        if revision != expected:
            raise Conflict(
                f"映射 {fw}/{code} 的修订号必须为 {expected}，收到 {revision}"
            )
        conn.execute(
            "INSERT INTO mappings (firmware_version, fault_code, revision, category,"
            " created_at) VALUES (?, ?, ?, ?, ?)",
            (fw, code, revision, cat, utc_now_iso()),
        )
        affected = _days_using_mapping_locked(conn, fw, code)
        for unit, day in sorted(affected):
            revoke_day_locked(
                conn,
                unit,
                day,
                f"映射 {fw}/{code} 新增修订 {revision}",
            )
        log(
            conn,
            "mapping.revision",
            f"{fw}/{code} r{revision} -> {cat}; revoked {len(affected)} day(s)",
        )


def mapping_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM mappings ORDER BY firmware_version, fault_code, revision"
    ).fetchall()


def effective_mapping(
    conn: sqlite3.Connection, firmware_version: str, fault_code: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM mappings WHERE firmware_version = ? AND fault_code = ?"
        " ORDER BY revision DESC LIMIT 1",
        (firmware_version, fault_code),
    ).fetchone()


def _days_using_mapping_locked(
    conn: sqlite3.Connection, fw: str, code: str
) -> set[tuple[str, str]]:
    """(unit, day) pairs whose data is affected by a (firmware, fault code) mapping.

    A mapping revision changes the answer for every day whose events include that
    pair, regardless of which mapping revision the old publish consumed, so we
    look at both live publishes and the current event set.
    """
    days: set[tuple[str, str]] = set()
    rows = conn.execute(
        "SELECT DISTINCT p.unit, p.day FROM publishes p"
        " WHERE p.status = 'CURRENT' AND p.id IN ("
        "   SELECT publish_id FROM publish_items"
        "   WHERE firmware_version = ? AND fault_code = ?"
        " )"
        " UNION"
        " SELECT DISTINCT e.unit, substr(e.occurred_at, 1, 10) FROM events e"
        " JOIN (SELECT event_no, MAX(revision) AS r FROM events GROUP BY event_no) m"
        "   ON m.event_no = e.event_no AND m.r = e.revision"
        " WHERE e.firmware_version = ? AND e.fault_code = ?",
        (fw, code, fw, code),
    ).fetchall()
    for row in rows:
        days.add((row[0], row[1]))
    return days


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #

@dataclass
class DayView:
    unit: str
    day: str
    events: list[sqlite3.Row]
    missing: list[sqlite3.Row]           # events without a mapping
    publish: sqlite3.Row | None          # CURRENT publish or None
    revoked: list[sqlite3.Row]           # historical revoked publishes
    categories: dict[str, str] = field(default_factory=dict)
    map_revisions: dict[tuple[str, str], int] = field(default_factory=dict)

    @property
    def missing_ids(self) -> set[str]:
        return {ev["event_no"] for ev in self.missing}

    @property
    def revision_token(self) -> str:
        """Canonical, unambiguous snapshot of the event revisions on this day.

        ``event_no@revision`` pairs are used because bare revision numbers collide
        across event numbers (every event starts at 1).
        """
        return revision_token(self.events)

    @property
    def recipe(self) -> str:
        """Everything the publish depends on: event revisions *and* mapping revisions."""
        pairs = sorted(
            f"{fw}/{code}@{rev}" for (fw, code), rev in self.map_revisions.items()
        )
        return f"E[{revision_token(self.events)}]|M[{','.join(pairs)}]"


def revoke_day_locked(
    conn: sqlite3.Connection, unit: str, day: str, reason: str
) -> int:
    cur = conn.execute(
        "UPDATE publishes SET status = 'REVOKED', revoked_at = ?, revoked_reason = ?"
        " WHERE unit = ? AND day = ? AND status = 'CURRENT'",
        (utc_now_iso(), reason, unit, day),
    )
    return cur.rowcount


def revoke_day(store: Store, unit: str, day: str, reason: str) -> int:
    with store.write() as conn:
        n = revoke_day_locked(conn, unit, day, reason)
        if n:
            log(conn, "publish.revoke", f"{unit} {day}: {reason}")
        return n


def day_view(conn: sqlite3.Connection, unit: str, day: str) -> DayView:
    events = conn.execute(
        "SELECT e.* FROM events e"
        " JOIN (SELECT event_no, MAX(revision) AS r FROM events GROUP BY event_no) m"
        "   ON m.event_no = e.event_no AND m.r = e.revision"
        " WHERE e.unit = ? AND substr(e.occurred_at, 1, 10) = ?"
        " ORDER BY e.occurred_at, e.event_no",
        (unit, day),
    ).fetchall()
    missing = [
        ev for ev in events
        if effective_mapping(conn, ev["firmware_version"], ev["fault_code"]) is None
    ]
    categories: dict[str, str] = {}
    map_revisions: dict[tuple[str, str], int] = {}
    for ev in events:
        key = (ev["firmware_version"], ev["fault_code"])
        mp = effective_mapping(conn, *key)
        if mp is not None:
            categories[ev["event_no"]] = mp["category"]
            map_revisions[key] = mp["revision"]
    publish = conn.execute(
        "SELECT * FROM publishes WHERE unit = ? AND day = ? AND status = 'CURRENT'",
        (unit, day),
    ).fetchone()
    revoked = conn.execute(
        "SELECT * FROM publishes WHERE unit = ? AND day = ? AND status = 'REVOKED'"
        " ORDER BY created_at DESC",
        (unit, day),
    ).fetchall()
    return DayView(
        unit, day, events, missing, publish, revoked, categories, map_revisions
    )


def revision_token(events: Iterable[sqlite3.Row]) -> str:
    """Stable signature of a set of event revisions (order-independent)."""
    return ",".join(
        sorted(f"{ev['event_no']}@{ev['revision']}" for ev in events)
    )


def parse_revision_token(token: str) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    for part in (token or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "@" not in part:
            raise DomainError(f"事件修订快照格式不合法: {part!r}")
        event_no, _, rev = part.rpartition("@")
        try:
            pairs.append((event_no.strip(), int(rev)))
        except ValueError:
            raise DomainError(f"事件修订快照格式不合法: {part!r}") from None
    return sorted(pairs)


def days_with_events(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT DISTINCT unit, substr(occurred_at, 1, 10) AS day FROM events"
        " ORDER BY day DESC, unit"
    ).fetchall()
    return [(r["unit"], r["day"]) for r in rows]


def publish_day(
    store: Store,
    unit: str,
    day: str,
    revision_token_in: str,
    recipe_in: str,
) -> int:
    """Publish a complete daily file for (unit, day) under one coherent revision set.

    ``revision_token_in`` / ``recipe_in`` are the snapshots the operator reviewed on
    the page (event revisions, and event+mapping revisions).  Both are mandatory:
    if either no longer matches live data the publish is refused rather than
    silently publishing something else.  A failure anywhere rolls the whole
    transaction back, leaving the prior revocation - and therefore the prior
    publish's non-current status - intact.

    The revocation of whatever was current happens before insertion, so a
    re-publish never needs to overwrite a CURRENT row.  The partial unique index is
    a second guard.
    """
    with store.write() as conn:
        view = day_view(conn, unit, day)
        if not view.events:
            raise DomainError(f"{unit} {day} 没有任何事件，无法发布日档")

        live_token = revision_token(view.events)
        requested = parse_revision_token(revision_token_in)
        live_pairs = sorted((ev["event_no"], ev["revision"]) for ev in view.events)
        if requested != live_pairs:
            raise Conflict(
                "重算所依据的事件修订已过期：页面提交的事件修订集合与当前数据不一致，"
                "请重新核对后再发布"
            )
        if recipe_in != view.recipe:
            raise Conflict(
                "重算所依据的修订已过期：期间事件或类别映射有新的修订，"
                "请重新核对后再发布"
            )

        if view.missing:
            missing_desc = ", ".join(
                f"{ev['event_no']}({ev['firmware_version']}/{ev['fault_code']})"
                for ev in view.missing
            )
            raise DomainError(
                f"{unit} {day} 缺少类别映射，整日不能供数：{missing_desc}"
            )

        # Sanity: no revision may be ambiguous between two units/days.
        for ev in view.events:
            if ev["unit"] != unit or ev["occurred_at"][:10] != day:
                raise Conflict("事件集合在事务内发生变化，请重试")

        revoke_day_locked(conn, unit, day, "重新发布取代旧日档")
        rows = []
        for ev in view.events:
            mp = effective_mapping(conn, ev["firmware_version"], ev["fault_code"])
            assert mp is not None
            rows.append(
                {
                    "event_no": ev["event_no"],
                    "revision": ev["revision"],
                    "unit": ev["unit"],
                    "firmware_version": ev["firmware_version"],
                    "fault_code": ev["fault_code"],
                    "category": mp["category"],
                    "occurred_at": ev["occurred_at"],
                    "map_revision": mp["revision"],
                }
            )
        cur = conn.execute(
            "INSERT INTO publishes (unit, day, event_revisions, recipe, event_count,"
            " status, created_at) VALUES (?, ?, ?, ?, ?, 'CURRENT', ?)",
            (
                unit,
                day,
                live_token,
                view.recipe,
                len(rows),
                utc_now_iso(),
            ),
        )
        publish_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO publish_items (publish_id, event_no, revision, unit,"
            " firmware_version, fault_code, category, occurred_at, map_revision)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    publish_id,
                    r["event_no"],
                    r["revision"],
                    r["unit"],
                    r["firmware_version"],
                    r["fault_code"],
                    r["category"],
                    r["occurred_at"],
                    r["map_revision"],
                )
                for r in rows
            ],
        )
        log(conn, "publish.create", f"{unit} {day} id={publish_id} n={len(rows)}")
        return publish_id


def publish_items(conn: sqlite3.Connection, publish_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM publish_items WHERE publish_id = ? ORDER BY occurred_at, event_no",
        (publish_id,),
    ).fetchall()


def all_publishes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM publishes ORDER BY created_at DESC, id DESC"
    ).fetchall()


def purge_stale_current(store: Store) -> int:
    """Boot-time guard.

    Nothing in normal operation can leave a CURRENT publish that contradicts the
    data, but a crash between a write and its revocation (or a hand-edited DB)
    could.  On startup we revoke any CURRENT publish that no longer matches the
    live revision set or has lost a mapping, so a restart can never *promote* a
    stale file - it can only demote one.
    """
    with store.write() as conn:
        rows = conn.execute(
            "SELECT id, unit, day, event_revisions, recipe FROM publishes"
            " WHERE status = 'CURRENT'"
        ).fetchall()
        revoked = 0
        for row in rows:
            view = day_view(conn, row["unit"], row["day"])
            if (
                view.missing
                or not view.events
                or row["event_revisions"] != revision_token(view.events)
                or row["recipe"] != view.recipe
            ):
                revoke_day_locked(
                    conn,
                    row["unit"],
                    row["day"],
                    "启动校验：现行日档与当前数据不一致",
                )
                revoked += 1
        if revoked:
            log(conn, "startup.revoke", f"{revoked} stale current publish(es) revoked")
        return revoked


def available_days(conn: sqlite3.Connection) -> list[dict]:
    """Daily files that are currently available for downstream consumption."""
    rows = conn.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM publish_items i WHERE i.publish_id = p.id)"
        " AS items FROM publishes p WHERE p.status = 'CURRENT'"
        " ORDER BY p.day DESC, p.unit"
    ).fetchall()
    return [dict(r) for r in rows]
