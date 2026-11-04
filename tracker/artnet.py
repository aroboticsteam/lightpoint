"""Minimal Art-Net: ArtDmx encode/decode plus asyncio receiver and sender."""
from __future__ import annotations

import asyncio
import socket
import struct
from typing import Callable

ARTNET_PORT = 6454
_ID = b"Art-Net\x00"
OP_DMX = 0x5000


def encode_artdmx(port_address: int, data: bytes, sequence: int = 0) -> bytes:
    """Build an ArtDmx packet. `port_address` is the 15-bit net/subnet/universe."""
    if len(data) % 2:
        data += b"\x00"  # spec: length must be even
    head = bytes([0, 14, sequence & 0xFF, 0, port_address & 0xFF, (port_address >> 8) & 0x7F])
    return _ID + struct.pack("<H", OP_DMX) + head + struct.pack(">H", len(data)) + data


def decode_artdmx(packet: bytes) -> tuple[int, int, bytes] | None:
    """Return (port_address, sequence, data) or None if not an ArtDmx packet."""
    if len(packet) < 18 or packet[:8] != _ID:
        return None
    if struct.unpack("<H", packet[8:10])[0] != OP_DMX:
        return None
    port_address = packet[14] | ((packet[15] & 0x7F) << 8)
    length = struct.unpack(">H", packet[16:18])[0]
    return port_address, packet[12], packet[18 : 18 + length]


class _Receiver(asyncio.DatagramProtocol):
    def __init__(self, port_address: int, on_frame: Callable[[bytes], None]):
        self.port_address = port_address
        self.on_frame = on_frame

    def datagram_received(self, data: bytes, addr) -> None:
        pkt = decode_artdmx(data)
        if pkt and pkt[0] == self.port_address:
            self.on_frame(pkt[2])


async def start_receiver(host: str, port: int, port_address: int, on_frame):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _Receiver(port_address, on_frame),
        local_addr=(host, port),
        reuse_port=True,
        allow_broadcast=True,
    )
    return transport


class Sender:
    def __init__(self, transport, host: str, port: int, port_address: int):
        self.transport, self.host, self.port = transport, host, port
        self.port_address = port_address
        self.sequence = 0

    @classmethod
    async def create(cls, host: str, port: int, port_address: int) -> "Sender":
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol, allow_broadcast=True, family=socket.AF_INET
        )
        return cls(transport, host, port, port_address)

    def send(self, frame: bytes) -> None:
        self.sequence = (self.sequence % 255) + 1  # 0 means "sequencing disabled"
        pkt = encode_artdmx(self.port_address, frame, self.sequence)
        self.transport.sendto(pkt, (self.host, self.port))
