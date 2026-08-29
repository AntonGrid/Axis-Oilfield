import sys
from pathlib import Path

# Make `oilfield.*` importable regardless of the CWD pytest is launched from.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
