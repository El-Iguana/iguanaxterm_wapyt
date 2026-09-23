"""
Unit tests for the pure-logic modules: path helpers and the Telnet parser.

Both run identically in CPython and Pyodide, which is the point — in the
original app this logic lived in browser JavaScript and could not be tested
without a browser.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "appcode"))

from services import telnet  # noqa: E402
from services.paths import (  # noqa: E402
    breadcrumbs,
    file_icon,
    format_mode,
    format_size,
    glob_filter,
    is_safe_name,
    join_path,
    parent_path,
)


# ── Paths ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "base,name,expected",
    [
        ("/var/log", "syslog", "/var/log/syslog"),
        ("/var/log/", "syslog", "/var/log/syslog"),
        ("/", "etc", "/etc"),
        ("", "etc", "/etc"),
    ],
)
def test_join_path(base, name, expected):
    assert join_path(base, name) == expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/var/log/nginx", "/var/log"),
        ("/var/log/nginx/", "/var/log"),
        ("/var", "/"),
        ("/", "/"),
        ("", "/"),
    ],
)
def test_parent_path(path, expected):
    assert parent_path(path) == expected


def test_breadcrumbs():
    assert [crumb["label"] for crumb in breadcrumbs("/var/log/nginx")] == [
        "/", "var", "log", "nginx",
    ]
    assert [crumb["path"] for crumb in breadcrumbs("/var/log")] == [
        "/", "/var", "/var/log",
    ]
    assert breadcrumbs("/") == [{"label": "/", "path": "/"}]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("file.txt", True),
        ("", False),
        (".", False),
        ("..", False),
        ("a/b", False),
        ("a\0b", False),
        (".hidden", True),
    ],
)
def test_is_safe_name(name, expected):
    assert is_safe_name(name) is expected


def test_format_size():
    assert format_size(0) == "0 B"
    assert format_size(512) == "512 B"
    assert format_size(1024) == "1.0 KB"
    assert format_size(1536) == "1.5 KB"
    assert format_size(1024 ** 2) == "1.0 MB"
    assert format_size(1024 ** 3) == "1.0 GB"
    assert format_size(None) == "0 B"


def test_format_mode():
    assert format_mode(0o100644) == "0644"
    assert format_mode(0o40755) == "0755"
    assert format_mode(0) == ""


def test_file_icon():
    assert file_icon("x", is_dir=True) == "mdi-folder"
    assert file_icon("x", is_link=True) == "mdi-link-variant"
    assert file_icon("main.py") == "mdi-language-python"
    assert file_icon("archive.tar.gz") == "mdi-zip-box"
    assert file_icon("id_ed25519.pem") == "mdi-key"
    assert file_icon("NOTES") == "mdi-file-outline"
    assert file_icon("README.MD") == "mdi-file-document-outline"


def test_glob_filter():
    entries = [{"name": n} for n in ("app.log", "app.py", "error.log", "README")]
    assert [e["name"] for e in glob_filter(entries, "*.log")] == ["app.log", "error.log"]
    # A bare substring is wrapped, so typing in a filter box works as expected.
    assert [e["name"] for e in glob_filter(entries, "app")] == ["app.log", "app.py"]
    assert len(glob_filter(entries, "")) == 4
    assert [e["name"] for e in glob_filter(entries, "*.LOG")] == ["app.log", "error.log"]


# ── Telnet ────────────────────────────────────────────────────────────────────

IAC, SB, SE = telnet.IAC, telnet.SB, telnet.SE
WILL, WONT, DO, DONT = telnet.WILL, telnet.WONT, telnet.DO, telnet.DONT


def test_plain_data_passes_through():
    display, reply, rest = telnet.process(b"hello world")
    assert display == b"hello world"
    assert reply == b""
    assert rest == b""


def test_escaped_iac_becomes_literal():
    display, _reply, rest = telnet.process(bytes([IAC, IAC]) + b"x")
    assert display == bytes([IAC]) + b"x"
    assert rest == b""


def test_will_echo_is_accepted():
    _display, reply, _rest = telnet.process(bytes([IAC, WILL, telnet.OPT_ECHO]))
    assert reply == bytes([IAC, DO, telnet.OPT_ECHO])


def test_will_unknown_option_is_refused():
    _display, reply, _rest = telnet.process(bytes([IAC, WILL, 0x42]))
    assert reply == bytes([IAC, DONT, 0x42])


def test_do_naws_is_accepted_with_a_size():
    _display, reply, _rest = telnet.process(bytes([IAC, DO, telnet.OPT_NAWS]))
    assert reply.startswith(bytes([IAC, WILL, telnet.OPT_NAWS]))
    assert bytes([IAC, SB, telnet.OPT_NAWS]) in reply
    assert reply.endswith(bytes([IAC, SE]))


def test_do_unknown_option_is_refused():
    _display, reply, _rest = telnet.process(bytes([IAC, DO, 0x42]))
    assert reply == bytes([IAC, WONT, 0x42])


def test_subnegotiation_is_stripped():
    stream = b"a" + bytes([IAC, SB, telnet.OPT_NAWS, 0, 80, 0, 24, IAC, SE]) + b"b"
    display, _reply, rest = telnet.process(stream)
    assert display == b"ab"
    assert rest == b""


def test_terminal_type_request_is_answered():
    stream = bytes([IAC, SB, telnet.OPT_TTYPE, 0x01, IAC, SE])
    _display, reply, _rest = telnet.process(stream)
    assert telnet.TERMINAL_TYPE in reply


def test_split_command_is_returned_as_remainder():
    """
    The original parser dropped a sequence straddling two reads and then
    misparsed everything after it. The tail has to come back intact.
    """
    full = b"ab" + bytes([IAC, WILL, telnet.OPT_SGA]) + b"cd"
    for split in range(1, len(full)):
        first, second = full[:split], full[split:]

        display1, reply1, rest = telnet.process(first)
        display2, reply2, rest2 = telnet.process(rest + second)

        assert rest2 == b""
        assert display1 + display2 == b"abcd"
        assert reply1 + reply2 == bytes([IAC, DO, telnet.OPT_SGA])


def test_split_subnegotiation_is_returned_as_remainder():
    full = bytes([IAC, SB, telnet.OPT_NAWS, 0, 80, 0, 24, IAC, SE]) + b"ok"
    for split in range(1, len(full)):
        # A split can land after the subnegotiation, so the first call may
        # already emit display bytes; both halves have to be accumulated.
        display1, _reply1, rest = telnet.process(full[:split])
        display2, _reply2, rest2 = telnet.process(rest + full[split:])
        assert rest2 == b""
        assert display1 + display2 == b"ok"


def test_naws_escapes_literal_ff():
    # 0xFFFF would otherwise read as an embedded IAC and corrupt the stream.
    payload = telnet.naws(0xFFFF, 24)
    assert payload.count(bytes([IAC, IAC])) == 2
    assert payload.endswith(bytes([IAC, SE]))


def test_naws_clamps_out_of_range():
    assert telnet.naws(0, 0) == telnet.naws(1, 1)
    assert telnet.naws(10 ** 9, 10 ** 9) == telnet.naws(0xFFFF, 0xFFFF)


def test_initial_negotiation_offers_sga_and_naws():
    offer = telnet.initial_negotiation()
    assert bytes([IAC, WILL, telnet.OPT_SGA]) in offer
    assert bytes([IAC, WILL, telnet.OPT_NAWS]) in offer
    assert bytes([IAC, DO, telnet.OPT_ECHO]) in offer
