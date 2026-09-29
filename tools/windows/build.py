#!/usr/bin/env python3
"""
Build IguanaXterm's native Windows install.

    python tools/windows/build.py                     # the bundle only
    python tools/windows/build.py --installer         # ...and setup.exe (Windows)

Needs ``uv`` on PATH and the wapyt checkout next to this repo (or
``--wapyt``). The bundle builds on any OS: CPython is the official
*embeddable* package, and the dependencies are installed from ``uv.lock`` as
prebuilt Windows wheels (``uv pip --python-platform``, which, unlike pip,
evaluates markers for the target, not the build machine). Only the last step,
Inno Setup's ISCC, needs Windows.

Layout (``build/windows/bundle``, installed to
``%LOCALAPPDATA%\\Programs\\IguanaXterm``)::

    python\\                 embeddable CPython + Lib\\site-packages
    app\\                    service.py, manage.py, appcode\\, the launcher
    Reset IguanaXterm password.cmd

Why embeddable Python and not PyInstaller: pytincture loads the BFF modules by
file path at runtime and serves appcode's source to the browser, so the app
has to exist as files anyway; a frozen executable adds a layer that can only
hide those files.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
OUT = ROOT / "build" / "windows"
DIST = ROOT / "dist"

# The official embeddable package. Bump both together; the hash is the one in
# python.org's .spdx.json for this file.
PYTHON_VERSION = "3.13.15"
PYTHON_SHA256 = "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_TAG = "".join(PYTHON_VERSION.split(".")[:2])  # "313"
TARGET = ["--python-platform", "x86_64-pc-windows-msvc", "--python-version", "3.13"]

# The browser installs wapyt at this version from appcode (see dev_wheel.sh).
BROWSER_WAPYT_VERSION = "99.99.99"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(str(part) for part in cmd), flush=True)
    return subprocess.run([str(part) for part in cmd], check=True, **kwargs)


def app_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version = "([^"]+)"', text, re.M).group(1)


def pinned_wapyt_ref() -> str:
    text = (ROOT / "Containerfile").read_text(encoding="utf-8")
    return re.search(r"^ARG WAPYT_REF=(\S+)", text, re.M).group(1)


# ── steps ───────────────────────────────────────────────────────────────────

def fetch_python(bundle: Path) -> None:
    print("==> embeddable CPython", PYTHON_VERSION)
    cache = OUT / "cache" / Path(PYTHON_URL).name
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(PYTHON_URL, timeout=120) as response:
            cache.write_bytes(response.read())
    digest = hashlib.sha256(cache.read_bytes()).hexdigest()
    if digest != PYTHON_SHA256:
        cache.unlink()
        sys.exit(f"{cache.name}: sha256 {digest}, expected {PYTHON_SHA256}")
    target = bundle / "python"
    with zipfile.ZipFile(cache) as archive:
        archive.extractall(target)
    # The ._pth file *replaces* sys.path in the embeddable build: list the
    # stdlib zip, site-packages and the app, and nothing from the machine.
    (target / f"python{PYTHON_TAG}._pth").write_text(
        f"python{PYTHON_TAG}.zip\n.\nLib\\site-packages\n..\\app\nimport site\n",
        encoding="utf-8",
    )


def locked_requirements(work: Path) -> Path:
    """uv.lock as requirements, minus the two packages built from source."""
    exported = run(
        ["uv", "export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-project",
         "--no-header", "--no-annotate"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    keep, pytincture = [], None
    for line in exported.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("pytincture @"):
            pytincture = line
        elif line.startswith(("../", "./", "-e ")) or line.startswith("wapyt"):
            continue
        else:
            keep.append(line)
    if pytincture is None:
        sys.exit("uv export did not list pytincture")
    requirements = work / "requirements.txt"
    requirements.write_text("\n".join(keep) + "\n", encoding="utf-8")
    (work / "pytincture.txt").write_text(pytincture + "\n", encoding="utf-8")
    return requirements


def install_packages(bundle: Path, wapyt: Path, work: Path) -> None:
    print("==> Windows wheels from uv.lock")
    site = bundle / "python" / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    requirements = locked_requirements(work)
    base = ["uv", "pip", "install", "--quiet", "--target", site, *TARGET]
    run([*base, "--only-binary", ":all:", "-r", requirements,
         "-r", HERE / "requirements-launcher.txt"])
    # Pure Python, built from source: pytincture at the locked commit, and
    # wapyt non-editable (widgetset discovery reads the installed metadata).
    run([*base, "--no-deps", "-r", work / "pytincture.txt"])
    wheels = work / "server-wheels"
    run(["uv", "build", "--wheel", "--quiet", "--out-dir", wheels, wapyt])
    run([*base, "--no-deps", *sorted(wheels.glob("wapyt-*.whl"))])
    # Tests and build leftovers some wheels ship at the top level.
    for junk in ("tests", "build", "bin", "scripts"):
        shutil.rmtree(site / junk, ignore_errors=True)


def browser_wheel(wapyt: Path, work: Path) -> Path:
    """What wapyt's scripts/dev_wheel.sh builds, without bash."""
    print("==> wapyt", BROWSER_WAPYT_VERSION, "for the browser")
    source = work / "wapyt-src"
    shutil.copytree(wapyt, source, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", "build", "dist", "*.whl"))
    pyproject = source / "pyproject.toml"
    pyproject.write_text(re.sub(r'^version = ".*"$', f'version = "{BROWSER_WAPYT_VERSION}"',
                                pyproject.read_text(encoding="utf-8"), flags=re.M), encoding="utf-8")
    manifest = source / "MANIFEST.in"
    text = manifest.read_text(encoding="utf-8")
    if "pytincture-assets.json" not in text:
        manifest.write_text("include wapyt/pytincture-assets.json\n" + text, encoding="utf-8")
    run([sys.executable, wapyt / "scripts" / "generate_assets_manifest.py", source])
    out = work / "browser-wheels"
    run(["uv", "build", "--wheel", "--quiet", "--out-dir", out, source])
    return out / f"wapyt-{BROWSER_WAPYT_VERSION}-py3-none-any.whl"


def copy_app(bundle: Path, wheel: Path) -> None:
    print("==> the app")
    app = bundle / "app"
    app.mkdir(parents=True)
    for name in ("service.py", "pytincture_compat.py", "manage.py", "LICENSE"):
        shutil.copy2(ROOT / name, app / name)
    shutil.copytree(ROOT / "appcode", app / "appcode", ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", "wapyt-*.whl"))
    shutil.copy2(wheel, app / "appcode" / wheel.name)
    shutil.copy2(HERE / "launcher.py", app / "iguanaxterm_launcher.py")
    shutil.copy2(HERE / "iguanaxterm.ico", app / "iguanaxterm.ico")
    (bundle / "Reset IguanaXterm password.cmd").write_text(
        "@echo off\r\n"
        '"%~dp0python\\python.exe" "%~dp0app\\iguanaxterm_launcher.py" --reset-password\r\n',
        encoding="utf-8",
    )


def build_installer(bundle: Path) -> Path:
    print("==> Inno Setup")
    iscc = shutil.which("iscc") or shutil.which("ISCC")
    if iscc is None:
        for candidate in (r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
                          r"C:\Program Files\Inno Setup 6\ISCC.exe"):
            if Path(candidate).exists():
                iscc = candidate
                break
    if iscc is None:
        sys.exit("ISCC.exe not found: install Inno Setup 6 (choco install innosetup)")
    DIST.mkdir(exist_ok=True)
    version = app_version()
    run([iscc, "/Qp", f"/DAppVersion={version}", f"/DBundleDir={bundle}",
         f"/DOutputDir={DIST}", f"/DIconFile={HERE / 'iguanaxterm.ico'}", HERE / "iguanaxterm.iss"])
    return DIST / f"IguanaXterm-{version}-setup.exe"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--wapyt", type=Path, default=ROOT.parent / "wa_pytincture_widgetset",
                        help="wapyt checkout (default: the sibling directory)")
    parser.add_argument("--installer", action="store_true", help="also run Inno Setup (Windows)")
    args = parser.parse_args()

    wapyt = args.wapyt.resolve()
    if not (wapyt / "pyproject.toml").exists():
        sys.exit(f"no wapyt checkout at {wapyt}")
    head = subprocess.run(["git", "-C", str(wapyt), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    if head and head != pinned_wapyt_ref():
        print(f"  note: wapyt is at {head[:12]}, the Containerfile pins {pinned_wapyt_ref()[:12]}")

    bundle = OUT / "bundle"
    shutil.rmtree(bundle, ignore_errors=True)
    bundle.mkdir(parents=True)
    with tempfile.TemporaryDirectory() as scratch:
        work = Path(scratch)
        fetch_python(bundle)
        install_packages(bundle, wapyt, work)
        copy_app(bundle, browser_wheel(wapyt, work))
    size = sum(f.stat().st_size for f in bundle.rglob("*") if f.is_file())
    print(f"==> bundle: {bundle} ({size / 2**20:.0f} MB)")
    if args.installer:
        print(f"==> installer: {build_installer(bundle)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
