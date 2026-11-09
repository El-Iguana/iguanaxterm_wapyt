import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPCODE = ROOT / "appcode"
TESTS = ROOT / "tests"
for path in (APPCODE, ROOT, TESTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

