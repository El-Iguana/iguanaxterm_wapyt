"""
The Guacamole protocol, as far as the RDP relay needs it (#57). Pure, so it is
unit-tested without a guacd.

An instruction is a list of elements, written ``LENGTH.VALUE`` and separated
by commas, ending in a semicolon::

    4.size,4.1024,3.768;

LENGTH counts **characters** (Unicode code points), not bytes, which is what
guacd and guacamole-common-js both do. So the relay decodes the guacd byte
stream to text before it splits it, and a multibyte character never confuses a
length.
"""
from __future__ import annotations

import codecs
from typing import Iterable, Optional

# The protocol version the relay speaks in "connect". guacd 1.6 offers
# VERSION_1_5_0 in "args" and accepts it back.
PROTOCOL_VERSION = "VERSION_1_5_0"


class ProtocolError(ValueError):
    """The stream isn't valid Guacamole protocol."""


def encode(*elements: object) -> str:
    """One instruction from its opcode and arguments."""
    parts = []
    for element in elements:
        text = "" if element is None else str(element)
        parts.append(f"{len(text)}.{text}")
    return ",".join(parts) + ";"


def tunnel_uuid(uuid: str) -> str:
    """The internal instruction Guacamole.WebSocketTunnel expects first."""
    return encode("", uuid)


class Splitter:
    """
    Splits a stream of text into whole instructions.

    :meth:`feed` takes any chunk (half an instruction, three and a half) and
    returns the instructions it completed, each as ``(raw_text, elements)``.
    The raw text is passed on unchanged, so nothing is re-encoded on the way
    through.
    """

    # A single instruction larger than this is refused rather than buffered
    # forever. guacd's own limit is 8192 characters per instruction.
    MAX_INSTRUCTION = 1 << 20

    def __init__(self) -> None:
        self._buffer = ""
        self._start = 0       # where the current instruction began
        self._pos = 0         # how far it has been parsed
        self._elements: list[str] = []

    def feed(self, text: str) -> list[tuple[str, list[str]]]:
        self._buffer += text
        done: list[tuple[str, list[str]]] = []
        buf = self._buffer
        while True:
            dot = buf.find(".", self._pos)
            if dot == -1:
                if len(buf) - self._pos > 12:   # far more digits than any length
                    raise ProtocolError("element length too long")
                break
            digits = buf[self._pos:dot]
            if not digits.isdigit():
                raise ProtocolError(f"bad element length {digits[:12]!r}")
            length = int(digits)
            if length > self.MAX_INSTRUCTION:
                raise ProtocolError("element too long")
            end = dot + 1 + length
            if end >= len(buf):               # value or terminator not here yet
                break
            self._elements.append(buf[dot + 1:end])
            terminator = buf[end]
            self._pos = end + 1
            if terminator == ",":
                continue
            if terminator != ";":
                raise ProtocolError(f"bad terminator {terminator!r}")
            done.append((buf[self._start:self._pos], self._elements))
            self._elements = []
            self._start = self._pos
        if self._start:
            buf = buf[self._start:]
            self._pos -= self._start
            self._start = 0
        if self._pos > self.MAX_INSTRUCTION:
            raise ProtocolError("instruction too long")
        self._buffer = buf
        return done


class ByteSplitter:
    """A :class:`Splitter` for a byte stream (guacd's TCP side), UTF-8."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._splitter = Splitter()

    def feed(self, data: bytes) -> list[tuple[str, list[str]]]:
        try:
            text = self._decoder.decode(data)
        except UnicodeDecodeError as exc:
            raise ProtocolError("not UTF-8") from exc
        return self._splitter.feed(text)


# Opcodes the browser may never send once the connection is made: they would
# let it pick another protocol, host or login (#57).
CLIENT_FORBIDDEN = frozenset({"select", "connect"})


def connect_values(args: Iterable[str], params: dict) -> list[str]:
    """
    The values for "connect", in the order guacd asked for them in "args".

    The first "args" element is guacd's protocol version; it is answered with
    ours. Parameters this relay doesn't set are sent empty, which guacd reads
    as "use the default".
    """
    names = list(args)
    values = []
    for index, name in enumerate(names):
        if index == 0 and name.startswith("VERSION_"):
            values.append(PROTOCOL_VERSION)
        else:
            value = params.get(name)
            values.append("" if value is None else str(value))
    return values


# guacd status codes (Guacamole.Status.Code), as a person would want them said.
_STATUS_TEXT = {
    0x0200: "The remote desktop isn't responding.",
    0x0201: "The remote desktop is busy (too many connections).",
    0x0202: "The remote desktop took too long to answer.",
    0x0203: "The remote desktop ran into an error.",
    0x0204: "The connection was refused: check the host and port.",
    0x0205: "Someone else took over this desktop session.",
    0x0206: "The connection was closed by the remote desktop.",
    0x0207: "The remote desktop couldn't be reached.",
    0x0208: "The remote desktop isn't available right now.",
    0x0209: "The session was closed because another one replaced it.",
    0x020D: "The session ended.",
    0x0301: "The login was refused. Check the username, password and domain, "
            "and the Security setting (NLA, TLS or RDP).",
    0x0303: "That account isn't allowed to sign in remotely.",
    0x0308: "The remote desktop closed the session for being idle.",
    0x031D: "Too many connections from here.",
}


def status_message(code: Optional[str], text: str = "") -> str:
    """A readable message for a guacd "error" instruction."""
    try:
        number = int(code or "0")
    except ValueError:
        number = 0
    known = _STATUS_TEXT.get(number)
    if known and text and text.lower() not in known.lower():
        return f"{known} ({text})"
    return known or text or f"The remote desktop reported an error ({code})."
