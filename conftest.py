"""Pytest bootstrap.

Pytest collects ``tests/*.py`` whose imports are rooted at the repo (``from app
import ...`` / ``from tools import ...``). The mere presence of this file at the
repo root makes pytest treat that root as the rootdir; the explicit ``sys.path``
insert below guarantees ``app`` and ``tools`` are importable under both
``pytest`` (console-script entry point) and ``python -m pytest``.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
