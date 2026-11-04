from tracker.artnet import decode_artdmx, encode_artdmx


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
