"""aiohttp app: web UI, websocket, Art-Net in/out and the 40 Hz tick loop.

The pages talk only over /ws: they send commands (see commands.py) and receive the full
state whenever it changes (checked 20 times a second, and straight after every command).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from . import artnet
from .commands import CommandError, apply
from .config import Config
from .engine import Engine

log = logging.getLogger("tracker")
STATIC = Path(__file__).parent / "static"
ENGINE, HUB, RX, TASKS = (web.AppKey(n) for n in ("engine", "hub", "rx", "tasks"))


class Hub:
    """Connected pages. `push` sends the current state to everyone, but only if it changed."""

    def __init__(self, engine: Engine, release_after: float):
        self.engine = engine
        self.release_after = release_after
        self.clients: set[web.WebSocketResponse] = set()
        self.pages: dict[str, int] = {}  # open pages per operator
        self.pending: dict[str, asyncio.Task] = {}  # operators about to be released
        self._last = ""

    def page_opened(self, op: str) -> None:
        self.pages[op] = self.pages.get(op, 0) + 1
        if task := self.pending.pop(op, None):
            task.cancel()  # they came back (reload, wifi blip) in time

    def page_closed(self, op: str) -> None:
        self.pages[op] -= 1
        if self.pages[op] == 0 and self.release_after >= 0:
            self.pending[op] = asyncio.create_task(self._release_later(op))

    async def _release_later(self, op: str) -> None:
        """Nobody has this person's page open any more: hand their lights back to the board."""
        await asyncio.sleep(self.release_after)
        self.pending.pop(op, None)
        for fid in self.engine.operator_fixtures(op):
            self.engine.unclaim(op, fid)
        self.engine.set_target(op, None)
        log.info("%s left: lights released to the board", self.engine.operators[op]["name"])
        await self.push()

    def _state_json(self) -> str:
        return json.dumps({"type": "state", **self.engine.snapshot()}, sort_keys=True)

    async def push(self, force: bool = False) -> None:
        text = self._state_json()
        if text == self._last and not force:
            return
        self._last = text
        await asyncio.gather(*(self._send(ws, text) for ws in list(self.clients)))

    @staticmethod
    async def _send(ws: web.WebSocketResponse, text: str) -> None:
        try:
            await ws.send_str(text)
        except ConnectionError:
            pass


async def _tick_loop(engine: Engine, sender: artnet.Sender, hz: float) -> None:
    period, last = 1.0 / hz, time.monotonic()
    while True:
        await asyncio.sleep(period)
        now = time.monotonic()
        sender.send(engine.tick(now - last))
        last = now


async def _push_loop(hub: Hub) -> None:
    while True:
        await asyncio.sleep(0.05)
        await hub.push()


def make_app(cfg: Config) -> web.Application:
    engine = Engine(cfg)
    hub = Hub(engine, cfg.disconnect_release_s)
    app = web.Application()
    app[ENGINE], app[HUB] = engine, hub

    async def on_startup(app):
        app[RX] = await artnet.start_receiver(cfg.listen_host, cfg.listen_port, cfg.in_universe, engine.on_board_frame)
        sender = await artnet.Sender.create(cfg.out_host, cfg.out_port, cfg.out_universe)
        app[TASKS] = [
            asyncio.create_task(_tick_loop(engine, sender, cfg.tick_hz)),
            asyncio.create_task(_push_loop(hub)),
        ]
        log.info(
            "listening on %s:%d universe %d, sending universe %d to %s:%d",
            cfg.listen_host, cfg.listen_port, cfg.in_universe, cfg.out_universe, cfg.out_host, cfg.out_port,
        )

    async def on_cleanup(app):
        for t in [*app[TASKS], *hub.pending.values()]:
            t.cancel()
        app[RX].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    async def ws_handler(req):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(req)
        hub.clients.add(ws)
        op = req.query.get("op")  # operator pages say who they are; the overview doesn't
        op = op if op in engine.operators else None
        if op:
            hub.page_opened(op)
        await ws.send_str(hub._state_json())
        try:
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue
                try:
                    reply = apply(engine, json.loads(m.data))
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "invalid JSON"})
                    continue
                except CommandError as e:
                    await ws.send_json({"type": "error", "message": str(e)})
                    continue
                if reply:
                    await ws.send_json(reply)
                await hub.push()
        finally:
            hub.clients.discard(ws)
            if op:
                hub.page_closed(op)
        return ws

    async def command(req):
        """Same commands as the websocket, for scripts and tests."""
        try:
            reply = apply(engine, await req.json())
        except (json.JSONDecodeError, CommandError) as e:
            raise web.HTTPBadRequest(text=str(e) or "invalid JSON")
        await hub.push()
        return web.json_response(reply or {"ok": True})

    async def state(_):
        return web.json_response(engine.snapshot())

    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    async def op_page(req):
        if req.match_info["op"] not in engine.operators:
            raise web.HTTPNotFound(text="unknown operator")
        return web.FileResponse(STATIC / "index.html")

    app.router.add_get("/", index)
    app.router.add_get("/op/{op}", op_page)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/command", command)
    return app
