"""The Guacamole protocol framing the RDP relay relies on (#57)."""
from __future__ import annotations

import pytest

from services import guac


def test_encode_counts_characters_not_bytes():
    assert guac.encode("size", 1024, 768) == "4.size,4.1024,3.768;"
    assert guac.encode("key", "é✓") == "3.key,2.é✓;"          # 2 characters, 5 bytes
    assert guac.encode("", "abc") == "0.,3.abc;"
    assert guac.encode("x", None) == "1.x,0.;"


def test_the_tunnel_uuid_instruction():
    uuid = "12345678-1234-1234-1234-123456789012"
    assert guac.tunnel_uuid(uuid) == f"0.,36.{uuid};"


def test_split_whole_instructions():
    splitter = guac.Splitter()
    got = splitter.feed("4.sync,3.123;5.mouse,1.1,1.2,1.0;")
    assert [elements for _raw, elements in got] == [["sync", "123"], ["mouse", "1", "2", "0"]]
    assert [raw for raw, _e in got] == ["4.sync,3.123;", "5.mouse,1.1,1.2,1.0;"]


@pytest.mark.parametrize("cut", range(1, 30))
def test_split_across_any_boundary(cut):
    text = "4.sync,3.123;3.key,2.é✓,1.1;"
    splitter = guac.Splitter()
    got = splitter.feed(text[:cut]) + splitter.feed(text[cut:])
    assert [elements for _raw, elements in got] == [["sync", "123"], ["key", "é✓", "1"]]
    assert "".join(raw for raw, _e in got) == text


def test_a_value_may_contain_the_separators():
    got = guac.Splitter().feed("4.name,5.a,b;c;")
    assert got[0][1] == ["name", "a,b;c"]


def test_bytes_split_mid_character():
    text = guac.encode("clipboard", "naïve ✓ 日本") + guac.encode("sync", "1")
    data = text.encode()
    splitter = guac.ByteSplitter()
    got = []
    for i in range(len(data)):                 # one byte at a time
        got += splitter.feed(data[i:i + 1])
    assert [e for _r, e in got] == [["clipboard", "naïve ✓ 日本"], ["sync", "1"]]


@pytest.mark.parametrize("bad", ["x.sync;", "4.sync|", "4.syncX", "-1.a;"])
def test_garbage_is_refused(bad):
    with pytest.raises(guac.ProtocolError):
        guac.Splitter().feed(bad + "4.sync,1.1;" * 2)


def test_a_huge_length_is_refused():
    with pytest.raises(guac.ProtocolError):
        guac.Splitter().feed("99999999.a")


def test_bad_utf8_is_refused():
    with pytest.raises(guac.ProtocolError):
        guac.ByteSplitter().feed(b"4.sync,1.\xff;")


def test_connect_values_follow_the_args_order():
    args = ["VERSION_1_5_0", "hostname", "port", "username", "password", "security", "unknown"]
    params = {"hostname": "h", "port": "3389", "username": "u", "password": "p", "security": "nla"}
    assert guac.connect_values(args, params) == [
        guac.PROTOCOL_VERSION, "h", "3389", "u", "p", "nla", "",
    ]


def test_status_messages():
    assert "login was refused" in guac.status_message("769", "Security negotiation failed")
    assert "Security negotiation failed" in guac.status_message("769", "Security negotiation failed")
    assert guac.status_message("9999", "odd thing") == "odd thing"
    assert "error" in guac.status_message(None, "")
