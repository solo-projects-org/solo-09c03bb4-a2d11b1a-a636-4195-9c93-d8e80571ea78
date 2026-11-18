"""FastAPI web surface for the fault-code classification service."""

from __future__ import annotations

import csv
import io
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from . import domain
from .db import Store

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = os.environ.get("FAULT_DB", str(BASE_DIR.parent / "data" / "ops.db"))

# Rows fetched per SQLite read transaction while streaming a CSV export.  Each
# batch gets its own short-lived connection (see _fetch_export_batch), so a slow
# client never pins a WAL read snapshot for the duration of the download.
EXPORT_BATCH_SIZE = max(1, int(os.environ.get("FAULT_CSV_BATCH_SIZE", "500")))

# Upper bound for one chunk written to the client.  Rendering is buffered per
# row and flushed at this size, so the peak export buffer is one row plus this
# amount - not one batch's encoded text.  (The ``recipe`` column repeats the
# publish's whole revision snapshot on every line, so a single row can already
# be hundreds of kilobytes on a large day.)
EXPORT_CHUNK_BYTES = max(4096, int(os.environ.get("FAULT_CSV_CHUNK_BYTES", "65536")))

EXPORT_COLUMNS = [
    "unit",
    "utc_day",
    "event_no",
    "revision",
    "firmware_version",
    "fault_code",
    "category",
    "occurred_at",
    "mapping_revision",
    "publish_id",
    "recipe",
]

store = Store(DB_PATH)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    domain.purge_stale_current(store)
    yield


app = FastAPI(title="风电机组故障码类别发布台", lifespan=lifespan)


app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def render(request: Request, name: str, **ctx):
    ctx.setdefault("request", request)
    return templates.TemplateResponse(request, name, ctx)


def flash_redirect(url: str, message: str, ok: bool = True) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(
        f"{url}{sep}msg={quote(message)}&ok={'1' if ok else '0'}",
        status_code=303,
    )


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #

@app.get("/")
def dashboard(request: Request, msg: str | None = None, ok: str = "1"):
    with store.read() as conn:
        days = []
        for unit, day in domain.days_with_events(conn):
            days.append(domain.day_view(conn, unit, day))
        current = domain.available_days(conn)
        recent = domain.event_history(conn)[-8:][::-1]
    return render(
        request,
        "index.html",
        days=days,
        current=current,
        recent=recent,
        msg=msg,
        ok=ok == "1",
    )


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #

@app.get("/events")
def events_page(request: Request, msg: str | None = None, ok: str = "1"):
    with store.read() as conn:
        history = domain.event_history(conn)[::-1]
    return render(request, "events.html", history=history, msg=msg, ok=ok == "1")


@app.post("/events")
def events_create(
    request: Request,
    event_no: str = Form(...),
    revision: int = Form(...),
    unit: str = Form(...),
    firmware_version: str = Form(...),
    fault_code: str = Form(...),
    occurred_at: str = Form(...),
):
    try:
        ev = domain.normalise_event_input(
            event_no, revision, unit, firmware_version, fault_code, occurred_at
        )
        domain.add_event_revision(store, ev)
    except domain.DomainError as exc:
        with store.read() as conn:
            history = domain.event_history(conn)[::-1]
        return render(
            request,
            "events.html",
            history=history,
            msg=str(exc),
            ok=False,
            form=dict(
                event_no=event_no,
                revision="",
                unit=unit,
                firmware_version=firmware_version,
                fault_code=fault_code,
                occurred_at=occurred_at,
            ),
        )
    return flash_redirect(
        "/events",
        f"已登记事件 {ev.event_no} 修订 {ev.revision}；相关日档现行资格已撤销",
    )


# --------------------------------------------------------------------------- #
# Mappings
# --------------------------------------------------------------------------- #

@app.get("/mappings")
def mappings_page(request: Request, msg: str | None = None, ok: str = "1"):
    with store.read() as conn:
        history = domain.mapping_history(conn)
    return render(
        request,
        "mappings.html",
        history=history,
        msg=msg,
        ok=ok == "1",
    )


@app.post("/mappings")
def mappings_create(
    request: Request,
    firmware_version: str = Form(...),
    fault_code: str = Form(...),
    revision: int = Form(...),
    category: str = Form(...),
):
    try:
        domain.add_mapping_revision(
            store, firmware_version, fault_code, revision, category
        )
    except domain.DomainError as exc:
        with store.read() as conn:
            history = domain.mapping_history(conn)
        return render(
            request,
            "mappings.html",
            history=history,
            msg=str(exc),
            ok=False,
            form=dict(
                firmware_version=firmware_version,
                fault_code=fault_code,
                revision="",
                category=category,
            ),
        )
    return flash_redirect(
        "/mappings",
        f"已登记映射 {firmware_version}/{fault_code} 修订 {revision} → {category}；"
        "受影响日档现行资格已撤销",
    )


# --------------------------------------------------------------------------- #
# Daily files
# --------------------------------------------------------------------------- #

@app.get("/days/{unit}/{day}")
def day_page(request: Request, unit: str, day: str, msg: str | None = None, ok: str = "1"):
    with store.read() as conn:
        view = domain.day_view(conn, unit, day)
        items = domain.publish_items(conn, view.publish["id"]) if view.publish else []
    return render(
        request,
        "day.html",
        view=view,
        items=items,
        revision_set=view.revision_token,
        recipe=view.recipe,
        msg=msg,
        ok=ok == "1",
    )


@app.post("/days/{unit}/{day}/publish")
def day_publish(
    request: Request,
    unit: str,
    day: str,
    revision_set: str = Form(...),
    recipe: str = Form(...),
):
    try:
        domain.publish_day(store, unit, day, revision_set, recipe)
    except domain.DomainError as exc:
        return flash_redirect(f"/days/{unit}/{day}", str(exc), ok=False)
    return flash_redirect(f"/days/{unit}/{day}", "已发布完整日档，现行资格生效")


@app.post("/days/{unit}/{day}/revoke")
def day_revoke(request: Request, unit: str, day: str):
    n = domain.revoke_day(store, unit, day, "操作员手动撤销")
    msg = "已撤销当前日档的现行资格" if n else "该日没有现行日档"
    return flash_redirect(f"/days/{unit}/{day}", msg, ok=bool(n))


# --------------------------------------------------------------------------- #
# Read API for the analysis group
# --------------------------------------------------------------------------- #

@app.get("/api/days")
def api_days():
    with store.read() as conn:
        return {"days": domain.available_days(conn)}


@app.get("/api/days/{unit}/{day}")
def api_day(unit: str, day: str):
    with store.read() as conn:
        view = domain.day_view(conn, unit, day)
        if view.publish is None:
            reasons = []
            if view.missing:
                reasons.append("缺少类别映射")
            if not view.events:
                reasons.append("该日无事件")
            if view.revoked and not reasons:
                reasons.append("现行资格已撤销，等待重算发布")
            return JSONResponse(
                status_code=409,
                content={
                    "unit": unit,
                    "day": day,
                    "available": False,
                    "reasons": reasons or ["该日尚未发布"],
                    "missing_mappings": [
                        {
                            "event_no": e["event_no"],
                            "revision": e["revision"],
                            "firmware_version": e["firmware_version"],
                            "fault_code": e["fault_code"],
                        }
                        for e in view.missing
                    ],
                },
            )
        items = [
            {
                "event_no": str(i["event_no"]),
                "revision": i["revision"],
                "unit": i["unit"],
                "fault_code": i["fault_code"],
                "category": i["category"],
                "occurred_at": i["occurred_at"],
                "mapping_revision": i["map_revision"],
            }
            for i in domain.publish_items(conn, view.publish["id"])
        ]
        return {
            "unit": unit,
            "day": day,
            "available": True,
            "publish_id": view.publish["id"],
            "published_at": view.publish["created_at"],
            "event_revisions": view.publish["event_revisions"],
            "recipe": view.publish["recipe"],
            "events": items,
        }


@app.get("/api/days/{unit}/{day}/export.csv")
async def api_day_csv(unit: str, day: str):
    """Same content as the JSON endpoint, as a CSV download for downstream jobs.

    The file is streamed in small row batches rather than assembled in memory:
    a day with tens of thousands of events must not cost tens of thousands of
    rows' worth of RAM on the server.  The CURRENT publish is resolved once, on
    its own short-lived connection; the item batches are then read by keyset
    pagination, each through its own short-lived connection.  So no read
    transaction stays open longer than one batch: a slow client cannot pin a WAL
    read snapshot (or a temporary spool file) for the length of the download,
    and a disconnected client simply stops advancing the batches.

    The ``publishes`` / ``publish_items`` rows of a publish are append-only, so
    once the CURRENT publish has been resolved for this request its file is
    immutable.  Corrections, mapping revisions and re-publishes that land while
    the download is in flight revoke or supersede it for *new* requests but
    cannot truncate or alter what this client receives.
    """
    resolved = await run_in_threadpool(_resolve_export, unit, day)
    if resolved[0] == "unavailable":
        return JSONResponse(
            status_code=409,
            content={
                "unit": unit,
                "day": day,
                "available": False,
                "reasons": resolved[1],
            },
        )

    _, publish_id, recipe = resolved
    filename = f"{_safe_filename_part(unit)}_{_safe_filename_part(day)}.csv"
    return StreamingResponse(
        _export_csv_chunks(unit, day, publish_id, recipe),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _safe_filename_part(value: str) -> str:
    """Keep the download filename header single-line and quote-free."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value or "")
    return cleaned or "_"


def _resolve_export(unit: str, day: str):
    """Resolve the CURRENT publish for this request and release the connection.

    Returns ``("ok", publish_id, recipe)`` or ``("unavailable", reasons)``.  It
    deliberately does not call ``day_view``: that loads every event of the day
    (and its mappings), which is exactly the unbounded work the streaming export
    must avoid.  Reasons are computed with existence checks instead.
    """
    with store.read() as conn:
        row = domain.current_publish(conn, unit, day)
        if row is None:
            return ("unavailable", domain.unavailable_reasons(conn, unit, day))
        return ("ok", row["id"], row["recipe"])


async def _export_csv_chunks(unit: str, day: str, publish_id: int, recipe: str):
    """Yield the CSV one bounded buffer at a time.

    At most one batch of rows is held at once, and encoded text is flushed per
    row whenever it reaches ``EXPORT_CHUNK_BYTES``: the live export buffer is
    bounded by one row plus one chunk, never by the size of the day.  (The
    ``recipe`` column repeats the publish's whole revision snapshot, so one row
    can itself be large on a big day.)  If the client disconnects, Starlette
    cancels this generator; no batch connection is left open because
    ``_fetch_export_batch`` closes it in ``finally`` before returning, and
    nothing is spooled to disk.
    """
    after: tuple[str, str, int] | None = None
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_COLUMNS)

    def take(force: bool) -> bytes | None:
        if not force and buf.tell() < EXPORT_CHUNK_BYTES:
            return None
        text = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return text.encode("utf-8")

    while True:
        rows = await run_in_threadpool(
            _fetch_export_batch, publish_id, after, EXPORT_BATCH_SIZE
        )
        if not rows:
            break
        for item in rows:
            _write_csv_row(writer, unit, day, item, publish_id, recipe)
            chunk = take(False)
            if chunk is not None:
                yield chunk
        if len(rows) < EXPORT_BATCH_SIZE:
            break
        last = rows[-1]
        after = (last["occurred_at"], last["event_no"], last["id"])
    chunk = take(True)
    if chunk is not None:
        yield chunk


def _write_csv_row(writer, unit: str, day: str, item, publish_id: int,
                   recipe: str) -> None:
    writer.writerow(
        [unit, day, item["event_no"], item["revision"], item["firmware_version"],
         item["fault_code"], item["category"], item["occurred_at"],
         item["map_revision"], publish_id, recipe]
    )


def _fetch_export_batch(publish_id: int, after, limit: int):
    """Read one batch of items on a fresh connection; close it before returning."""
    with store.read() as conn:
        return domain.publish_item_batch(conn, publish_id, after, limit)


@app.get("/healthz")
def healthz():
    with store.read() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/status")
def status():
    """Operator-facing sanity: what is current, and what is waiting for recompute."""
    with store.read() as conn:
        current = domain.available_days(conn)
        waiting = []
        for unit, day in domain.days_with_events(conn):
            view = domain.day_view(conn, unit, day)
            if view.publish is None:
                waiting.append(
                    {
                        "unit": unit,
                        "day": day,
                        "missing_mappings": len(view.missing),
                        "revoked_publishes": len(view.revoked),
                    }
                )
        recent_log = [
            dict(r)
            for r in conn.execute(
                "SELECT at, action, detail FROM operation_log ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]
    return {
        "current": [(d["unit"], d["day"], d["event_count"]) for d in current],
        "waiting_for_recompute": waiting,
        "recent_operations": recent_log,
    }
