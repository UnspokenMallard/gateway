"""Budget's library (`~/.budget`, stdlib only): the gate, the ledger and the limit window."""
import sys
from pathlib import Path

try:
    from budget import ledger, limits  # noqa: F401
except ImportError:
    sys.path.insert(0, str(Path.home() / ".budget"))
    from budget import ledger, limits  # noqa: F401
