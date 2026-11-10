"""Minimal Art-Net: ArtDmx encode/decode plus asyncio receiver and sender."""
from __future__ import annotations

import asyncio
import socket
import struct
import uuid
from typing import Callable

ARTNET_PORT = 6454
_ID = b"Art-Net\x00"
OP_DMX = 0x5000
OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100
POLL_REPLY_LEN = 239


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


def is_artpoll(packet: bytes) -> bool:
    return len(packet) >= 12 and packet[:8] == _ID and struct.unpack("<H", packet[8:10])[0] == OP_POLL


def _fixed(text: str, size: int) -> bytes:
    return text.encode("ascii", "replace")[: size - 1].ljust(size, b"\x00")


def encode_artpollreply(
    ip: str,
    port_address: int,
    name: str = "light-tracker",
    long_name: str = "VEX light tracker (Art-Net proxy)",
    mac: bytes | None = None,
) -> bytes:
    """ArtPollReply (239 bytes) announcing one DMX *output* port (a port that
    receives ArtDmx from the network) on `port_address`. A console in unicast
    mode only sends to nodes that advertise a universe this way."""
    if mac is None:
        mac = uuid.getnode().to_bytes(6, "big")
    ip_bytes = bytes(int(o) for o in ip.split("."))
    net, sub, uni = (port_address >> 8) & 0x7F, (port_address >> 4) & 0x0F, port_address & 0x0F
    pkt = bytearray()
    pkt += _ID + struct.pack("<H", OP_POLL_REPLY)
    pkt += ip_bytes + struct.pack("<H", ARTNET_PORT)
    pkt += bytes([0, 1])  # firmware version
    pkt += bytes([net, sub])
    pkt += bytes([0x00, 0xFF])  # OEM (unregistered)
    pkt += bytes([0])  # UBEA
    pkt += bytes([0xD0])  # status 1: indicators normal
    pkt += bytes([0xF0, 0x7F])  # ESTA manufacturer 0x7FF0 (prototyping)
    pkt += _fixed(name, 18) + _fixed(long_name, 64) + _fixed("#0001 [0000] OK", 64)
    pkt += bytes([0, 1])  # one port
    pkt += bytes([0x80, 0, 0, 0])  # port types: DMX512 output
    pkt += bytes(4)  # good input
    pkt += bytes([0x80, 0, 0, 0])  # good output A: data being output
    pkt += bytes(4)  # SwIn
    pkt += bytes([uni, 0, 0, 0])  # SwOut
    pkt += bytes([0, 0, 0])  # AcnPriority, SwMacro, SwRemote
    pkt += bytes(3)  # spare
    pkt += bytes([0])  # style: node
    pkt += mac + ip_bytes + bytes([1])  # MAC, bind IP, bind index
    pkt += bytes([0x08])  # status 2: 15-bit port-address
    pkt += bytes(4)  # good output B
    pkt += bytes([0])  # status 3
    pkt += bytes(6 + 2 + 2 + 1 + 10)  # default responder UUID, user, refresh rate, queue policy, filler
    assert len(pkt) == POLL_REPLY_LEN
    return bytes(pkt)


def local_ip_towards(peer: str) -> str:
    """The local address the OS would use to reach `peer` (for ArtPollReply)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((peer, ARTNET_PORT))
            return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"


class _Receiver(asyncio.DatagramProtocol):
    def __init__(self, port_address: int, on_frame: Callable[[bytes], None], advertise_ip: str | None = None):
        self.port_address = port_address
        self.on_frame = on_frame
        self.advertise_ip = advertise_ip
        self.transport = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if is_artpoll(data):
            self._reply_to_poll(addr)
            return
        pkt = decode_artdmx(data)
        if pkt and pkt[0] == self.port_address:
            self.on_frame(pkt[2])

    def _reply_to_poll(self, addr) -> None:
        if self.transport is None:
            return
        ip = self.advertise_ip or local_ip_towards(addr[0])
        reply = encode_artpollreply(ip, self.port_address)
        # Spec says replies are broadcast; also answer the poller directly so
        # a console in unicast mode learns the address even if broadcast is filtered.
        self.transport.sendto(reply, (addr[0], ARTNET_PORT))
        try:
            self.transport.sendto(reply, ("255.255.255.255", ARTNET_PORT))
        except OSError:
            pass


async def start_receiver(host: str, port: int, port_address: int, on_frame, advertise_ip: str | None = None):
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _Receiver(port_address, on_frame, advertise_ip),
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
