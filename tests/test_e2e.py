"""Real UDP: a fake board sends ArtDmx in, a fake node receives the merged frame out."""
import asyncio
import shutil
import socket
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from tracker.artnet import decode_artdmx, encode_artdmx
from tracker.config import load_config
from tracker.server import make_app

CONFIG = Path(__file__).parent.parent / "config" / "fixtures.example.json"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def latest_frame(node: socket.socket, timeout=1.0):
    loop, end, last = asyncio.get_running_loop(), asyncio.get_running_loop().time() + timeout, None
    while loop.time() < end:
        try:
            pkt = await asyncio.wait_for(loop.sock_recv(node, 2048), 0.2)
        except asyncio.TimeoutError:
            continue
        last = decode_artdmx(pkt)
    return last


async def test_board_frame_is_merged_and_forwarded(tmp_path):
    path = tmp_path / "fixtures.json"
    shutil.copy(CONFIG, path)
    cfg = load_config(path)
    cfg.listen_port, cfg.out_port = free_port(), free_port()
    node = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    node.bind(("127.0.0.1", cfg.out_port))
    node.setblocking(False)
    board = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    async with TestClient(TestServer(make_app(cfg))) as c:
        frame = bytearray(512)
        # R1 is patched at 312, so profile channel N is frame index 311 + N - 1
        frame[311 + 5 - 1] = 99  # speed (ch5), board value
        frame[311 + 6 - 1] = 200  # dimmer (ch6)
        board.sendto(encode_artdmx(cfg.in_universe, bytes(frame)), ("127.0.0.1", cfg.listen_port))
        await asyncio.sleep(0.2)

        # untouched head: output equals board input
        port, _, out = await latest_frame(node)
        assert port == cfg.out_universe and bytes(out) == bytes(frame)

        await c.post("/api/command", json={"type": "target", "op": "p1", "x": 8, "y": 14, "z": 0})
        await c.post("/api/command", json={"type": "claim", "op": "p1", "fid": "R1"})
        await asyncio.sleep(1.0)
        _, _, out = await latest_frame(node)
        assert out[311] != 0 or out[312] != 0  # R1 pan moved off the board's 0
        assert out[311 + 5 - 1] == 0  # speed forced fast
        assert out[311 + 6 - 1] == 200  # dimmer still the board's
        assert out[328:] == bytes(frame)[328:]  # R2 and beyond untouched
    node.close()
    board.close()
