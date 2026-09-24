import sys
from pathlib import Path

# Repository root on the path, so `core` and `soulstruct_patches` import as in the app.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import soulstruct_patches  # noqa: E402,F401  (patches soulstruct at import time)
