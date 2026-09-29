import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPCODE = ROOT / "appcode"
for path in (APPCODE, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

# What service.py applies before building the app (pytincture on Windows).
import pytincture_compat  # noqa: E402

pytincture_compat.apply()
