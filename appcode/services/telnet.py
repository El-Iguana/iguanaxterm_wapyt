"""
Minimal Telnet option negotiation (RFC 854 / 1073 / 1091).

Ported from the original app's parser with two corrections:

* it returns the **unconsumed tail**. The original dropped a sequence that
  straddled two TCP reads, and then misparsed everything after it — rare on a
  fast link, reliable on a slow one;
* NAWS is negotiated, so a browser window resize reaches the remote. The
  original could not resize a Telnet session at all.
"""
from __future__ import annotations

import struct

IAC = 0xFF   # Interpret As Command
SE = 0xF0    # Subnegotiation End
SB = 0xFA    # Subnegotiation Begin
WILL = 0xFB
WONT = 0xFC
DO = 0xFD
DONT = 0xFE

OPT_ECHO = 0x01
OPT_SGA = 0x03    # Suppress Go Ahead
OPT_TTYPE = 0x18  # Terminal Type
OPT_NAWS = 0x1F   # Negotiate About Window Size

TERMINAL_TYPE = b"xterm-256color"

# Options this client is willing to perform when the server asks (IAC DO x).
_WILL_DO = {OPT_SGA, OPT_NAWS, OPT_TTYPE}
# Options this client is happy for the server to perform (IAC WILL x).
_ACCEPT_FROM_SERVER = {OPT_ECHO, OPT_SGA}


def initial_negotiation() -> bytes:
    """The opening offer, sent immediately after the socket connects."""
    return bytes(
        [
            IAC, WILL, OPT_SGA,
            IAC, DO, OPT_SGA,
            IAC, DO, OPT_ECHO,
            IAC, WILL, OPT_NAWS,
        ]
    )


def naws(cols: int, rows: int) -> bytes:
    """A window-size subnegotiation."""
    cols = max(1, min(int(cols), 0xFFFF))
    rows = max(1, min(int(rows), 0xFFFF))
    payload = struct.pack(">HH", cols, rows)
    # A 0xFF inside the payload has to be doubled or it reads as a command.
    payload = payload.replace(b"\xff", b"\xff\xff")
    return bytes([IAC, SB, OPT_NAWS]) + payload + bytes([IAC, SE])


def _terminal_type_reply() -> bytes:
    return bytes([IAC, SB, OPT_TTYPE, 0x00]) + TERMINAL_TYPE + bytes([IAC, SE])


def process(data: bytes) -> tuple[bytes, bytes, bytes]:
    """
    Split a raw read into display bytes, our reply, and an unconsumed tail.

    Feed the tail back in front of the next read::

        display, reply, buffer = process(buffer + chunk)

    Returns:
        ``(display, reply, remainder)``. ``remainder`` is non-empty only when
        the buffer ends part-way through a command, and is never display data.
    """
    display = bytearray()
    reply = bytearray()
    index = 0
    length = len(data)

    while index < length:
        byte = data[index]

        if byte != IAC:
            display.append(byte)
            index += 1
            continue

        if index + 1 >= length:
            break  # bare IAC at the end — wait for more

        command = data[index + 1]

        if command == IAC:  # escaped literal 0xFF
            display.append(IAC)
            index += 2
            continue

        if command in (WILL, WONT, DO, DONT):
            if index + 2 >= length:
                break  # option byte has not arrived yet
            option = data[index + 2]

            if command == WILL:
                # Server offers to do something.
                reply += bytes(
                    [IAC, DO if option in _ACCEPT_FROM_SERVER else DONT, option]
                )
            elif command == DO:
                # Server asks us to do something.
                if option in _WILL_DO:
                    reply += bytes([IAC, WILL, option])
                    if option == OPT_NAWS:
                        # The size is corrected the moment the client reports
                        # its real geometry; this keeps the handshake complete.
                        reply += naws(80, 24)
                else:
                    reply += bytes([IAC, WONT, option])
            elif command == WONT:
                reply += bytes([IAC, DONT, option])
            else:  # DONT
                reply += bytes([IAC, WONT, option])

            index += 3
            continue

        if command == SB:
            end = data.find(bytes([IAC, SE]), index + 2)
            if end == -1:
                break  # subnegotiation still arriving
            if (
                end > index + 2
                and data[index + 2] == OPT_TTYPE
                and end > index + 3
                and data[index + 3] == 0x01  # SEND
            ):
                reply += _terminal_type_reply()
            index = end + 2
            continue

        # Two-byte command we do not implement (NOP, DM, BRK, ...).
        index += 2

    return bytes(display), bytes(reply), data[index:]
