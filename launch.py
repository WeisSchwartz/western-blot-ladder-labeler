"""Start the local web app and open it in the browser.

Run via run.sh after the one-time setup.sh. Works without installing the
package because it adds src/ to the path.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from blot_ladder.web.app import serve  # noqa: E402

if __name__ == "__main__":
    serve()
