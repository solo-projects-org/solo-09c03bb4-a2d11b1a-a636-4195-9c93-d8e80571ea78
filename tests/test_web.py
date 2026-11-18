from __future__ import annotations

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
