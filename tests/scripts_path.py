"""Import scripts/extract_bbg.py for tests (scripts/ is not a package)."""

import importlib.util
from pathlib import Path


def extract_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "extract_bbg.py"
    spec = importlib.util.spec_from_file_location("extract_bbg", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
