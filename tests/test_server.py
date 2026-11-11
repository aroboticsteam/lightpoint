import asyncio
import shutil
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from tracker.config import load_config
from tracker.server import ENGINE, HUB, make_app

CONFIG = Path(__file__).parent.parent / "config" / "fixtures.example.json"


@pytest.fixture
async def client(tmp_path, unused_udp_port):
    path = tmp_path / "fixtures.json"
    shutil.copy(CONFIG, path)
    cfg = load_config(path)
    cfg.listen_port = unused_udp_port  # keep tests off the real Art-Net port
    cfg.out_port = unused_udp_port + 1
    async with TestClient(TestServer(make_app(cfg))) as c:
        yield c


async def next_state(ws, until=lambda s: True, timeout=2.0):
    """Read websocket messages until a state satisfying `until` arrives."""
    async def go():
        while True:
            m = await ws.receive_json()
            if m["type"] == "state" and until(m):
                return m
    return await asyncio.wait_for(go(), timeout)


def owner(state, fid):
    return next(f["owner"] for f in state["fixtures"] if f["id"] == fid)


async def test_pages(client):
    assert (await client.get("/")).status == 200
    assert (await client.get("/op/p1")).status == 200
    assert (await client.get("/op/nobody")).status == 404


async def test_state_is_pushed_on_connect(client):
    async with client.ws_connect("/ws") as ws:
        s = await next_state(ws)
        assert [o["id"] for o in s["operators"]] == ["p1", "p2", "p3", "p4"]
        assert all(f["owner"] is None for f in s["fixtures"])


async def test_selection_shows_up_live_on_other_pages(client):
    async with client.ws_connect("/ws") as one, client.ws_connect("/ws") as two:
        await next_state(one), await next_state(two)
        await one.send_json({"type": "claim", "op": "p1", "fid": "R1", "on": True})
        s = await next_state(two, lambda s: owner(s, "R1") == "p1")  # person 2 sees it without asking
        assert next(f["mode"] for f in s["fixtures"] if f["id"] == "R1") == "pan_tilt"

        # person 2 cannot take it: error goes only to them
        await two.send_json({"type": "claim", "op": "p2", "fid": "R1", "on": True})
        err = await asyncio.wait_for(two.receive_json(), 2)
        while err["type"] == "state":
            err = await asyncio.wait_for(two.receive_json(), 2)
        assert err["type"] == "error" and "held by Person 1" in err["message"]

        await one.send_json({"type": "claim", "op": "p1", "fid": "R1", "on": False})
        await next_state(two, lambda s: owner(s, "R1") is None)


async def test_dragging_a_target_is_live(client):
    async with client.ws_connect("/ws") as one, client.ws_connect("/ws") as two:
        await next_state(one), await next_state(two)
        for x in (2.0, 3.0, 4.0):
            await one.send_json({"type": "target", "op": "p1", "x": x, "y": 10, "z": 0})
        s = await next_state(two, lambda s: s["operators"][0]["target"] == [4.0, 10.0, 0.0])
        assert s["operators"][0]["target"][0] == 4.0


async def test_bad_json_and_commands_do_not_close_the_socket(client):
    async with client.ws_connect("/ws") as ws:
        await next_state(ws)
        await ws.send_str("not json")
        m = await asyncio.wait_for(ws.receive_json(), 2)
        assert m["type"] == "error"
        await ws.send_json({"type": "nope"})
        while (m := await asyncio.wait_for(ws.receive_json(), 2))["type"] == "state":
            pass
        assert m["type"] == "error"
        await ws.send_json({"type": "claim", "op": "p1", "fid": "R2", "on": True})
        await next_state(ws, lambda s: owner(s, "R2") == "p1")


async def test_closing_a_page_releases_its_lights_after_the_grace_period(client):
    hub = client.server.app[HUB]
    hub.release_after = 0.3
    async with client.ws_connect("/ws") as watcher:
        await next_state(watcher)
        async with client.ws_connect("/ws?op=p1") as me:
            await next_state(me)
            await me.send_json({"type": "claim", "op": "p1", "fid": "R1"})
            await me.send_json({"type": "target", "op": "p1", "x": 5, "y": 9, "z": 0})
            await next_state(watcher, lambda s: owner(s, "R1") == "p1")
        # page closed: still held during the grace period...
        await asyncio.sleep(0.1)
        assert client.server.app[ENGINE].owner["R1"] == "p1"
        # ...then handed back to the board, target cleared, and everyone sees it
        s = await next_state(watcher, lambda s: owner(s, "R1") is None)
        assert s["operators"][0]["target"] is None
        assert next(f["mode"] for f in s["fixtures"] if f["id"] == "R1") == "off"


async def test_reconnecting_within_the_grace_period_keeps_the_lights(client):
    hub = client.server.app[HUB]
    hub.release_after = 0.4
    async with client.ws_connect("/ws?op=p1") as me:
        await next_state(me)
        await me.send_json({"type": "claim", "op": "p1", "fid": "R1"})
        await next_state(me, lambda s: owner(s, "R1") == "p1")
    await asyncio.sleep(0.1)
    async with client.ws_connect("/ws?op=p1") as again:  # page reload
        await next_state(again)
        await asyncio.sleep(0.6)
        assert client.server.app[ENGINE].owner["R1"] == "p1"


async def test_overview_page_closing_releases_nothing(client):
    hub = client.server.app[HUB]
    hub.release_after = 0.1
    await client.post("/api/command", json={"type": "claim", "op": "p2", "fid": "R3"})
    async with client.ws_connect("/ws") as overview:  # no ?op=
        await next_state(overview)
    await asyncio.sleep(0.3)
    assert client.server.app[ENGINE].owner["R3"] == "p2"


async def test_rest_command_mirrors_the_websocket(client):
    r = await client.post("/api/command", json={"type": "claim", "op": "p3", "fid": "S1"})
    assert r.status == 200
    s = await (await client.get("/api/state")).json()
    assert owner(s, "S1") == "p3"
    assert (await client.post("/api/command", json={"type": "claim", "op": "p4", "fid": "S1"})).status == 400


async def test_floorplan_image_is_served_and_the_page_uses_it(client):
    r = await client.get("/floorplan.png")
    assert r.status == 200
    assert r.headers["Content-Type"] == "image/png"
    assert (await r.read()).startswith(b"\x89PNG")
    page = await (await client.get("/")).text()
    assert "/floorplan.png" in page and "drawImage(plan" in page
