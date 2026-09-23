"""
Measure how well the SSH connect retry absorbs a host that refuses connections.

Reproduces the condition behind "Error reading SSH protocol banner[Errno 104]
Connection reset by peer": a server whose MaxStartups refuses all but one
unauthenticated connection at a time. Not a pass/fail test — it prints a table
so the retry ceiling can be chosen against evidence rather than guessed.

    podman build -t localhost/ix-sshtest-throttled \
        -f tests/smoke/Containerfile.sshtarget-throttled tests/smoke
    podman run -d --name ix-throttled -p 127.0.0.1:2223:22 \
        localhost/ix-sshtest-throttled
    uv run python tests/smoke/throttle_probe.py
    podman rm -f ix-throttled

Measured 2026-09-23 (3 attempts is the shipped default):

     concurrent   no retry   3 attempts   6 attempts
              2        1/2          2/2          2/2
              3        1/3          3/3          3/3
              4        1/4          3/4          4/4
              6        1/6          3/6          6/6

The app opens at most three connections to one host — interactive SFTP pool,
transfer pool, one per terminal — so 3 attempts covers the real case. Raising
it costs failure latency on a host that is simply down: 1.8s at 3 attempts,
6.0s at 5, 9.0s at 6.
"""
import sys, time, logging
from concurrent.futures import ThreadPoolExecutor
logging.getLogger("paramiko").setLevel(logging.CRITICAL)
sys.path.insert(0, "appcode")
from services import ssh as H

PROFILE = {"id": 1, "host": "127.0.0.1", "port": 2223, "username": "testuser",
           "password": "testpass", "private_key": "", "host_key": ""}

def attempt(attempts):
    try:
        c = H.connect(dict(PROFILE), attempts=attempts); c.close(); return True
    except Exception:
        return False

print("against MaxStartups 1:100:2 — refuses all but one at a time\n")
print(f"{'concurrent':>11} {'no retry':>10} {'3 attempts':>12} {'6 attempts':>12}")
for n in (2, 3, 4, 6):
    row = []
    for attempts in (1, 3, 6):
        with ThreadPoolExecutor(max_workers=n) as ex:
            ok = sum(ex.map(attempt, [attempts] * n))
        row.append(f"{ok}/{n}")
        time.sleep(0.5)
    print(f"{n:>11} {row[0]:>10} {row[1]:>12} {row[2]:>12}")
