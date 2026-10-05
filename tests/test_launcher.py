"""tools/windows/launcher.py — the native install's launcher."""
from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "tools" / "windows" / "launcher.py"


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    monkeypatch.setenv("GANXTERM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GANXTERM_HOST", raising=False)
    spec = importlib.util.spec_from_file_location("iguanaxterm_launcher_under_test", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_data_dir_follows_the_environment(launcher, tmp_path):
    assert launcher.DATA == tmp_path
    assert launcher.STATE == tmp_path / "launcher.json"


def test_default_data_dir_is_per_user(launcher, monkeypatch, tmp_path):
    monkeypatch.delenv("GANXTERM_DATA_DIR")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    expected = tmp_path / "local" / "IguanaXterm" if sys.platform == "win32" else tmp_path / "xdg" / "iguanaxterm"
    assert launcher.data_dir() == expected


def test_the_url_is_the_loopback_ip_never_localhost(launcher):
    assert launcher.url(8765) == "http://127.0.0.3:8765/iguanaxterm"


def test_its_loopback_address_is_its_own(launcher):
    # Cookies are per host, not per port: on 127.0.0.1 (or Monguana's
    # 127.0.0.2) the shared pytincture session cookie would sign one app out
    # whenever you sign in to the other.
    assert launcher.HOST == "127.0.0.3"


def test_free_port_skips_a_taken_one(launcher, monkeypatch):
    with socket.socket() as taken:
        taken.bind((launcher.HOST, 0))
        port = taken.getsockname()[1]
        monkeypatch.setattr(launcher, "FIRST_PORT", port)
        monkeypatch.setattr(launcher, "LAST_PORT", port + 20)
        chosen = launcher.free_port()
    assert chosen is not None and port < chosen <= port + 20


def test_a_stale_state_file_is_not_a_running_instance(launcher):
    launcher.STATE.write_text('{"port": 1, "server_pid": 1, "launcher_pid": 1}', encoding="utf-8")
    assert launcher.read_state() is None
    assert launcher.main(["--status"]) == 1


def test_downloads_go_to_this_computers_downloads_folder(launcher, monkeypatch, tmp_path):
    monkeypatch.delenv("GANXTERM_DOWNLOAD_DIR", raising=False)
    monkeypatch.setattr(launcher.Path, "home", lambda: tmp_path)
    if sys.platform != "win32":
        assert launcher.downloads_dir() == tmp_path / "Downloads" / "IguanaXterm"
    monkeypatch.setenv("GANXTERM_DOWNLOAD_DIR", str(tmp_path / "elsewhere"))
    assert launcher.downloads_dir() == tmp_path / "elsewhere"
