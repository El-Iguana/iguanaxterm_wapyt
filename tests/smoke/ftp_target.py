"""
Throwaway FTPS target for ftp_smoke.py: 127.0.0.1:2121, TLS *required* on
both channels (as the NAS is), user ``testuser``/``testpass``.

    uv run --with pyftpdlib --with pyopenssl python tests/smoke/ftp_target.py /tmp/ixftp
"""
import sys
import tempfile
from pathlib import Path

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import TLS_FTPHandler
from pyftpdlib.servers import FTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_ftp import _self_signed  # noqa: E402

root = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
(root / "share" / "docs").mkdir(parents=True, exist_ok=True)
(root / "share" / "hello.txt").write_text("hello over ftps\n")

authorizer = DummyAuthorizer()
authorizer.add_user("testuser", "testpass", str(root), perm="elradfmwMT")


class Handler(TLS_FTPHandler):
    certfile = str(_self_signed(Path(tempfile.mkdtemp())))
    tls_control_required = True
    tls_data_required = True
    passive_ports = range(30000, 30050)


Handler.authorizer = authorizer
print(f"FTPS target on 127.0.0.1:2121, root {root}", flush=True)
FTPServer(("127.0.0.1", 2121), Handler).serve_forever()
