from tracker.artnet import (
    OP_POLL_REPLY,
    POLL_REPLY_LEN,
    _Receiver,
    decode_artdmx,
    encode_artdmx,
    encode_artpollreply,
    is_artpoll,
)


def test_roundtrip():
    data = bytes(range(200))
    pkt = encode_artdmx(0x0123, data, sequence=7)
    assert pkt[:8] == b"Art-Net\x00"
    port, seq, out = decode_artdmx(pkt)
    assert (port, seq, out) == (0x0123, 7, data)


def test_odd_length_is_padded():
    _, _, out = decode_artdmx(encode_artdmx(0, b"\x01\x02\x03"))
    assert out == b"\x01\x02\x03\x00"


def test_rejects_other_packets():
    assert decode_artdmx(b"not artnet at all, definitely") is None
    assert decode_artdmx(b"Art-Net\x00\x00\x20" + bytes(20)) is None  # ArtPoll opcode


ARTPOLL = b"Art-Net\x00" + b"\x00\x20" + bytes([0, 14, 0, 0])


def test_artpoll_detected():
    assert is_artpoll(ARTPOLL)
    assert not is_artpoll(encode_artdmx(0, b"\x00\x00"))
    assert not is_artpoll(b"short")


def test_artpollreply_layout():
    r = encode_artpollreply("172.16.7.251", 0x0123, mac=bytes([1, 2, 3, 4, 5, 6]))
    assert len(r) == POLL_REPLY_LEN == 239
    assert r[:8] == b"Art-Net\x00" and int.from_bytes(r[8:10], "little") == OP_POLL_REPLY
    assert tuple(r[10:14]) == (172, 16, 7, 251) and int.from_bytes(r[14:16], "little") == 6454
    assert (r[18], r[19]) == (0x01, 0x02)  # net 1, sub-net 2
    assert r[173] == 1 and r[174] == 0x80  # one port, DMX512 output
    assert r[190] == 0x03  # SwOut: universe 3 within sub-net 2
    assert r[26:44].rstrip(b"\x00") == b"light-tracker"
    assert tuple(r[201:207]) == (1, 2, 3, 4, 5, 6)


async def test_receiver_answers_poll_and_still_passes_frames():
    frames = []
    sent = []

    class _FakeTransport:
        def sendto(self, data, addr):
            sent.append((data, addr))

    rx = _Receiver(5, frames.append, advertise_ip="172.16.7.251")
    rx.connection_made(_FakeTransport())
    rx.datagram_received(ARTPOLL, ("172.16.7.40", 6454))
    assert len(sent) == 2 and sent[0][1] == ("172.16.7.40", 6454)
    assert sent[0][0] == sent[1][0] and len(sent[0][0]) == 239
    rx.datagram_received(encode_artdmx(5, b"\x01\x02"), ("172.16.7.40", 6454))
    rx.datagram_received(encode_artdmx(6, b"\x09\x09"), ("172.16.7.40", 6454))  # other universe: ignored
    assert frames == [b"\x01\x02"]
