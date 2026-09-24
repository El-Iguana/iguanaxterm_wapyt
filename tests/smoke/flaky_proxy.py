"""
A TCP proxy that cuts downloads mid-body, for resume_smoke.py.

    python3 tests/smoke/flaky_proxy.py LISTEN_PORT TARGET_PORT CONTROL_FILE

Relays everything between the browser and the app untouched, except: a
connection carrying a GET for a download whose path contains ``resumeme`` is
reset (RST, like a dropped network) once it has passed ``CUT_AFTER`` response
bytes -- as long as the number in CONTROL_FILE is above zero, which each cut
decrements. The test writes that number to choose how many drops it gets.
"""
from __future__ import annotations

import asyncio
import socket
import struct
import sys
from pathlib import Path

# Small, so every attempt can be cut however far the file already is: at 3 MB
# a resume near the end never reached the threshold, and a test asking for
# five drops got four.
CUT_AFTER = 512 * 1024


def _take_cut(control: Path) -> bool:
    try:
        left = int(control.read_text().strip() or 0)
    except (OSError, ValueError):
        return False
    if left <= 0:
        return False
    control.write_text(str(left - 1))
    return True


async def _handle(client_r, client_w, target_port: int, control: Path) -> None:
    try:
        server_r, server_w = await asyncio.open_connection("127.0.0.1", target_port)
    except OSError:
        client_w.close()
        return
    state = {"download": False, "sent": 0}

    async def upstream() -> None:
        while data := await client_r.read(65536):
            for line in data.split(b"\r\n"):
                if line.startswith(b"GET ") and b"/download?" in line:
                    state["download"] = b"resumeme" in line
                    state["sent"] = 0
            server_w.write(data)
            await server_w.drain()
        server_w.close()

    async def downstream() -> None:
        while data := await server_r.read(65536):
            if state["download"]:
                state["sent"] += len(data)
                if state["sent"] > CUT_AFTER and _take_cut(control):
                    # SO_LINGER 0: close sends RST, which the browser reports
                    # as a network error mid-body -- a real drop, not an EOF.
                    sock = client_w.get_extra_info("socket")
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    client_w.transport.abort()
                    server_w.close()
                    return
            client_w.write(data)
            await client_w.drain()
        client_w.close()

    await asyncio.gather(upstream(), downstream(), return_exceptions=True)


async def main(listen_port: int, target_port: int, control: Path) -> None:
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, target_port, control), "127.0.0.1", listen_port
    )
    print(f"flaky proxy 127.0.0.1:{listen_port} -> :{target_port}, cuts from {control}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])))
