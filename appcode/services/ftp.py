"""
Plain FTP, dressed as the slice of ``paramiko.SFTPClient`` this app uses.

The file browser, the connection pool and the transfer routes were all written
against paramiko. Rather than teach each of them a second protocol, an FTP
connection answers the same calls — ``normalize``, ``listdir_attr``, ``stat``,
``open``, ``mkdir``, ``rename``, ``remove``, ``rmdir`` — and the pool's
liveness probe (``get_transport().is_active()``). Everything above this module
stays protocol-blind.

The differences that cannot be hidden, and how they are handled:

- **One control connection does one thing at a time.** While a RETR or STOR is
  streaming, any other command on the same connection corrupts the exchange.
  ``_busy`` is taken by every command and held by an open file handle until it
  closes, so a listing waits for a transfer instead of interleaving with it.
- **Servers drop idle control connections** (a NAS typically after a few
  minutes). A command that finds the connection gone reconnects once and
  retries, so a pane left open over lunch still browses.
- **TLS when the server offers it, and pinned.** Explicit FTPS (``AUTH TLS``)
  is used whenever the server accepts it — some NAS firmware refuses a login
  without it. Those certificates are self-signed, so there is no CA to check
  against; the certificate is pinned on first use in ``host_key``, exactly as
  an SSH host key is, and a server that later changes certificate *or stops
  offering TLS* is refused. The pin is checked before the password is sent.
- **Errors are reply strings.** ``error_perm`` is translated into the
  ``IOError`` family the callers already catch — ``_mkdir_p`` and
  ``_remove_recursive`` decide what exists by catching exactly that.
"""
from __future__ import annotations

import ftplib
import hashlib
import socket
import ssl
import stat as stat_module
import threading
import time
from calendar import timegm
from typing import Callable, Optional

import paramiko

from services.ssh import HostKeyChanged

_TIMEOUT_SECONDS = 20

# How long a command waits for a transfer on the same connection to finish.
# Long enough to outlast an ordinary file; short enough that an abandoned
# handle (a download the browser never started reading) cannot wedge the
# connection for good.
_BUSY_WAIT_SECONDS = 120

_MONTHS = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}


class FTPUnavailable(Exception):
    """The server could not be reached or refused the login."""


# What a dropped control connection looks like. Deliberately not bare OSError:
# FileNotFoundError and PermissionError are OSErrors too, and they are answers,
# not a lost connection.
_LOST = (EOFError, ConnectionError, TimeoutError, ftplib.error_reply)


# ── Listing parsers (pure, unit-tested) ───────────────────────────────────────


def attrs_from_facts(name: str, facts: dict) -> Optional[paramiko.SFTPAttributes]:
    """
    One MLSD entry as ``SFTPAttributes``, or ``None`` for ``.`` and ``..``.

    Fact names are case-insensitive by RFC 3659; servers disagree on case
    (this NAS sends ``UNIX.mode``), so they are lowered first.
    """
    facts = {key.lower(): value for key, value in facts.items()}
    kind = facts.get("type", "file").lower()
    if kind in ("cdir", "pdir") or name in (".", ".."):
        return None

    attr = paramiko.SFTPAttributes()
    attr.filename = name
    perms = _octal(facts.get("unix.mode"))
    if kind == "dir":
        attr.st_mode = stat_module.S_IFDIR | perms
    elif "symlink" in kind:
        # OS.unix=symlink — RFC 3659's own example. Shown as a link, and
        # followed like one when opened.
        attr.st_mode = stat_module.S_IFLNK | perms
    else:
        attr.st_mode = stat_module.S_IFREG | perms
    attr.st_size = int(facts.get("size") or 0)
    attr.st_mtime = _mlsd_time(facts.get("modify"))
    return attr


def attrs_from_list_line(line: str, now: Optional[float] = None) -> Optional[paramiko.SFTPAttributes]:
    """
    One line of a Unix-style ``LIST``, for servers without MLSD.

    ``drwxr-xr-x 2 owner group 4096 Jan 31 12:00 name`` — nine
    whitespace-separated fields, the last of which is the name and may itself
    contain spaces. Anything else (a ``total 12`` header, a DOS-style listing)
    returns ``None`` and is skipped rather than guessed at.
    """
    parts = line.split(None, 8)
    if len(parts) < 9 or len(parts[0]) < 10 or parts[0][0] not in "-dl":
        return None
    mode_text, _links, _owner, _group, size, month, day, clock, name = parts
    if name in (".", ".."):
        return None

    attr = paramiko.SFTPAttributes()
    kind = mode_text[0]
    if kind == "l":
        name = name.split(" -> ", 1)[0]
        attr.st_mode = stat_module.S_IFLNK
    elif kind == "d":
        attr.st_mode = stat_module.S_IFDIR
    else:
        attr.st_mode = stat_module.S_IFREG
    attr.st_mode |= _perm_bits(mode_text[1:10])
    attr.filename = name
    attr.st_size = int(size) if size.isdigit() else 0
    attr.st_mtime = _list_time(month, day, clock, now)
    return attr


def _octal(text: Optional[str]) -> int:
    try:
        return int(text or "0", 8) & 0o7777
    except ValueError:
        return 0


def _perm_bits(text: str) -> int:
    bits = 0
    for index, char in enumerate(text[:9]):
        if char not in "-":
            bits |= 1 << (8 - index)
    return bits


def _mlsd_time(text: Optional[str]) -> int:
    """``YYYYMMDDHHMMSS[.sss]``, always UTC."""
    if not text or len(text) < 14 or not text[:14].isdigit():
        return 0
    return timegm((int(text[0:4]), int(text[4:6]), int(text[6:8]),
                   int(text[8:10]), int(text[10:12]), int(text[12:14]), 0, 0, 0))


def _list_time(month: str, day: str, clock: str, now: Optional[float]) -> int:
    """
    ``Jan 31 12:00`` (within the last six months, year implied) or
    ``Jan 31  2024``. LIST gives no zone; UTC is as good a guess as any.
    """
    month_number = _MONTHS.get(month[:3].lower())
    if month_number is None or not day.isdigit():
        return 0
    if ":" in clock:
        hour, _, minute = clock.partition(":")
        current = time.gmtime(now if now is not None else time.time())
        year = current.tm_year
        # A date "in the future" is from last year: ls drops the year only for
        # the past six months, which can straddle January.
        if (month_number, int(day)) > (current.tm_mon, current.tm_mday + 1):
            year -= 1
        try:
            return timegm((year, month_number, int(day), int(hour), int(minute), 0, 0, 0, 0))
        except ValueError:
            return 0
    if clock.isdigit():
        return timegm((int(clock), month_number, int(day), 0, 0, 0, 0, 0, 0))
    return 0


def _as_ioerror(exc: ftplib.error_perm, path: str) -> IOError:
    """A 5xx reply, as the exception a paramiko caller would have caught."""
    text = str(exc)
    if text.startswith("550") and "denied" not in text.lower():
        return FileNotFoundError(2, text, path)
    if "denied" in text.lower():
        return PermissionError(13, text, path)
    return IOError(text)


# ── The client ────────────────────────────────────────────────────────────────


def certificate_pin(der: bytes) -> str:
    """How a TLS certificate is recorded in ``sessions.host_key``."""
    return f"tls-sha256 {hashlib.sha256(der).hexdigest()}"


class _FTPS(ftplib.FTP_TLS):
    """
    ``FTP_TLS`` whose data connections resume the control connection's TLS
    session.

    Many servers (vsftpd's ``require_ssl_reuse``, and FileZilla Server) refuse
    a data connection that negotiates a fresh session, since that is what a
    hijacked data port would do. ftplib never offers the session; this does.
    """

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session
            )
        return conn, size


def _tls_context() -> ssl.SSLContext:
    # No CA verification: trust comes from the pin, as with SSH host keys.
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


class _Transport:
    """What the pool's liveness probe asks for. A local flag, no round trip."""

    def __init__(self, client: "FTPFiles") -> None:
        self._client = client

    def is_active(self) -> bool:
        return not self._client.closed


class FTPFiles:
    """One logged-in FTP control connection, reconnecting when it drops."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        pin: str = "",
        on_learn_pin: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._host = host
        self._port = int(port or 21)
        self._username = username or "anonymous"
        self._password = password or ""
        self._pin = pin or ""
        self._on_learn_pin = on_learn_pin
        self.secure = False
        self._busy = threading.Lock()
        self._ftp: Optional[ftplib.FTP] = None
        self._home = "/"
        self._mlsd = True
        self.closed = False
        self._dial()

    # -- connection ---------------------------------------------------------

    def _dial(self) -> None:
        ftp = _FTPS(timeout=_TIMEOUT_SECONDS, encoding="utf-8", context=_tls_context())
        try:
            ftp.connect(self._host, self._port)
        except (OSError, EOFError) as exc:
            raise FTPUnavailable(
                f"Could not reach {self._host}:{self._port} over FTP ({exc})"
            ) from exc
        try:
            self._secure(ftp)
            # secure=False: _secure already negotiated TLS if there is any to
            # have, and FTP_TLS.login would otherwise insist on it.
            ftp.login(self._username, self._password, secure=False)
            if self.secure:
                ftp.prot_p()  # encrypt the data connections too
        except ftplib.error_perm as exc:
            ftp.close()
            raise FTPUnavailable(f"FTP login refused: {exc}") from exc
        except BaseException:
            ftp.close()
            raise
        try:
            # UTF8 is advertised by most servers but switched on by few until
            # asked; non-ASCII names come back mangled otherwise.
            ftp.sendcmd("OPTS UTF8 ON")
        except ftplib.all_errors:
            pass
        ftp.voidcmd("TYPE I")
        self._home = ftp.pwd() or "/"
        self._ftp = ftp
        self.closed = False

    def _secure(self, ftp: _FTPS) -> None:
        """Upgrade to TLS if the server will, and hold it to the pin."""
        try:
            ftp.auth()
        except ftplib.error_perm:
            # 500/502/504: the server does not do TLS. Fine the first time;
            # after a certificate has been pinned it is a downgrade.
            if self._pin:
                raise HostKeyChanged(self._host, self._pin, "no TLS offered")
            self.secure = False
            return

        received = certificate_pin(ftp.sock.getpeercert(binary_form=True))
        if self._pin and self._pin != received:
            raise HostKeyChanged(self._host, self._pin, received)
        if not self._pin:
            self._pin = received
            if self._on_learn_pin is not None:
                self._on_learn_pin(received)
        self.secure = True

    def _call(self, path: str, work: Callable[[ftplib.FTP], object]):
        """
        Run one command sequence with the connection to ourselves.

        A dropped control connection shows up as EOFError, a reset, or a 421;
        any of those gets one fresh login and one retry. A 5xx is the server
        answering, not the connection failing, and is never retried.
        """
        if not self._busy.acquire(timeout=_BUSY_WAIT_SECONDS):
            raise IOError("The FTP connection is busy with another transfer")
        try:
            for attempt in (1, 2):
                if self._ftp is None:
                    self._dial()
                try:
                    return work(self._ftp)
                except ftplib.error_perm as exc:
                    raise _as_ioerror(exc, path) from exc
                except ftplib.error_temp as exc:
                    if not str(exc).startswith("421"):  # 421 = closing the connection
                        raise IOError(str(exc)) from exc
                    self._drop()
                    if attempt == 2:
                        raise IOError(f"FTP connection lost: {exc}") from exc
                except _LOST as exc:
                    self._drop()
                    if attempt == 2:
                        raise IOError(f"FTP connection lost: {exc}") from exc
        finally:
            self._busy.release()

    def _drop(self) -> None:
        ftp, self._ftp = self._ftp, None
        if ftp is not None:
            try:
                ftp.close()
            except Exception:
                pass

    # -- the pool's view ----------------------------------------------------

    def get_transport(self) -> _Transport:
        return _Transport(self)

    def open_sftp(self) -> "FTPFiles":
        return self

    def close(self) -> None:
        # No QUIT: the pool calls this under its global lock, and a polite
        # goodbye to a server that has gone quiet would hold that lock for the
        # full timeout. Servers treat a dropped control connection as a logout.
        self.closed = True
        self._drop()

    # -- the SFTPClient surface ----------------------------------------------

    def normalize(self, path: str) -> str:
        if path in ("", ".", "~"):
            return self._home
        return path

    def listdir_attr(self, path: str) -> list:
        """
        Entries in a directory; ``IOError`` if ``path`` is not one.

        Changing into the directory first is what makes "not a directory" an
        error on every server: a LIST of a *file* succeeds on many of them and
        lists the file itself, which would send ``_remove_recursive`` looking
        for children of a file.
        """

        def _work(ftp: ftplib.FTP) -> list:
            ftp.cwd(path)
            try:
                return self._list_here(ftp)
            finally:
                ftp.cwd(self._home)

        return self._call(path, _work)

    def _list_here(self, ftp: ftplib.FTP) -> list:
        if self._mlsd:
            try:
                entries = [attrs_from_facts(name, facts) for name, facts in ftp.mlsd()]
                return [entry for entry in entries if entry is not None]
            except ftplib.error_perm as exc:
                if not str(exc).startswith(("500", "502")):
                    raise
                self._mlsd = False  # the server does not know MLSD; stop asking
        lines: list[str] = []
        ftp.retrlines("LIST", lines.append)
        now = time.time()
        entries = [attrs_from_list_line(line, now) for line in lines]
        return [entry for entry in entries if entry is not None]

    def stat(self, path: str) -> paramiko.SFTPAttributes:
        def _work(ftp: ftplib.FTP) -> paramiko.SFTPAttributes:
            try:
                reply = ftp.sendcmd(f"MLST {path}")
            except ftplib.error_perm as exc:
                if not str(exc).startswith(("500", "502")):
                    raise
                return self._stat_without_mlst(ftp, path)
            # 250-Listing path / " type=file;size=12; /path" / 250 End
            for line in reply.splitlines()[1:]:
                if line.startswith(" "):
                    facts_text, _, _name = line.strip().partition(" ")
                    facts = dict(
                        fact.split("=", 1) for fact in facts_text.split(";") if "=" in fact
                    )
                    attr = attrs_from_facts(path.rsplit("/", 1)[-1] or "/", facts)
                    if attr is None:  # MLST of a directory reports type=cdir
                        attr = attrs_from_facts(path, {**facts, "type": "dir"})
                    return attr
            raise FileNotFoundError(2, "No such file", path)

        return self._call(path, _work)

    def _stat_without_mlst(self, ftp: ftplib.FTP, path: str) -> paramiko.SFTPAttributes:
        attr = paramiko.SFTPAttributes()
        attr.filename = path.rsplit("/", 1)[-1]
        try:
            ftp.cwd(path)
            ftp.cwd(self._home)
            attr.st_mode = stat_module.S_IFDIR
            attr.st_size = 0
            return attr
        except ftplib.error_perm:
            pass
        attr.st_mode = stat_module.S_IFREG
        attr.st_size = ftp.size(path) or 0  # raises 550 if it does not exist
        return attr

    def mkdir(self, path: str) -> None:
        self._call(path, lambda ftp: ftp.mkd(path))

    def rmdir(self, path: str) -> None:
        self._call(path, lambda ftp: ftp.rmd(path))

    def remove(self, path: str) -> None:
        self._call(path, lambda ftp: ftp.delete(path))

    def rename(self, old_path: str, new_path: str) -> None:
        self._call(old_path, lambda ftp: ftp.rename(old_path, new_path))

    def open(self, path: str, mode: str = "rb") -> "_TransferHandle":
        """
        A streaming handle for RETR (``rb``) or STOR (``wb``).

        Holds ``_busy`` until closed: the control connection is committed to
        this transfer until the server's closing reply has been read.
        """
        if mode not in ("rb", "wb", "r", "w"):
            raise ValueError(f"FTP handles are read or write only, not {mode!r}")
        command = f"{'RETR' if 'r' in mode else 'STOR'} {path}"

        if not self._busy.acquire(timeout=_BUSY_WAIT_SECONDS):
            raise IOError("The FTP connection is busy with another transfer")
        try:
            for attempt in (1, 2):
                if self._ftp is None:
                    self._dial()
                try:
                    # Binary, every time. ftplib's mlsd() and retrlines() leave
                    # the connection in TYPE A, and a RETR or STOR after a
                    # listing would otherwise let the server rewrite line
                    # endings inside a JPEG. retrbinary() does the same.
                    self._ftp.voidcmd("TYPE I")
                    data = self._ftp.transfercmd(command)
                    break
                except ftplib.error_perm as exc:
                    raise _as_ioerror(exc, path) from exc
                except ftplib.error_temp as exc:
                    if not str(exc).startswith("421"):
                        raise IOError(str(exc)) from exc
                    self._drop()
                    if attempt == 2:
                        raise IOError(f"FTP connection lost: {exc}") from exc
                except _LOST as exc:
                    self._drop()
                    if attempt == 2:
                        raise IOError(f"FTP connection lost: {exc}") from exc
        except BaseException:
            self._busy.release()
            raise
        return _TransferHandle(self, data)

    def _finish_transfer(self, data: socket.socket, failed: bool) -> None:
        try:
            if isinstance(data, ssl.SSLSocket) and not failed:
                # Send close_notify, as ftplib does after STOR/RETR; without it
                # some servers log the upload as truncated.
                try:
                    data = data.unwrap()
                except (OSError, ValueError):
                    pass
            try:
                data.close()
            except OSError:
                pass
            if self._ftp is not None:
                try:
                    self._ftp.voidresp()
                except ftplib.all_errors:
                    # The server's closing reply never came or was an error.
                    # The control connection's state is unknown now, so start
                    # the next command on a fresh one.
                    self._drop()
                    if not failed:
                        raise
        finally:
            self._busy.release()


class _TransferHandle:
    """The file-like object ``transfer.py`` reads from or writes to."""

    def __init__(self, owner: FTPFiles, data: socket.socket) -> None:
        self._owner = owner
        self._data = data
        self._closed = False
        self._failed = False

    def read(self, size: int = -1) -> bytes:
        try:
            if size is None or size < 0:
                chunks = []
                while chunk := self._data.recv(256 * 1024):
                    chunks.append(chunk)
                return b"".join(chunks)
            return self._data.recv(size)
        except OSError:
            self._failed = True
            raise

    def write(self, data: bytes) -> int:
        try:
            self._data.sendall(data)
        except OSError:
            self._failed = True
            raise
        return len(data)

    def prefetch(self, *_args) -> None:
        """paramiko read-ahead; a socket stream is already sequential."""

    def set_pipelined(self, *_args) -> None:
        """paramiko write pipelining; likewise nothing to do."""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._owner._finish_transfer(self._data, self._failed)

    def __del__(self) -> None:
        # A download whose response body was never iterated never reaches the
        # generator's finally; without this the connection stays busy.
        try:
            self.close()
        except Exception:
            pass


def connect(session: dict, on_learn_host_key: Optional[Callable[[str], None]] = None) -> FTPFiles:
    return FTPFiles(
        host=session.get("host", ""),
        port=int(session.get("port") or 21),
        username=session.get("username") or "",
        password=session.get("password") or "",
        pin=session.get("host_key") or "",
        on_learn_pin=on_learn_host_key,
    )
