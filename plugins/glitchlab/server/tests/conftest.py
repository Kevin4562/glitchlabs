from __future__ import annotations

import os
from pathlib import Path
import tempfile


TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="glitchlab-release-tests-"))
os.environ["GLITCHLAB_DATA"] = str(TEST_DATA_DIR)
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
