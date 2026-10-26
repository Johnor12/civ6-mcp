"""Every Lua read of a tile's resource must check that this player can see it.

Resources such as Iron or Niter stay hidden until their tech; the engine
still reports them through plot:GetResourceType(). get_cities, the builder
tasks and the wonder advisor printed hidden Iron that way on 2026-10-26.
"""

from pathlib import Path

import civ_mcp.lua

_CHECKS = ("IsResourceVisible", "resVisible", "PrereqTech")
_WINDOW = 8  # lines after the read in which the check must appear


def test_every_resource_read_checks_visibility():
    unchecked = []
    for path in sorted(Path(civ_mcp.lua.__file__).parent.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if "GetResourceType()" not in line or line.strip().startswith("--"):
                continue
            window = "\n".join(lines[i : i + _WINDOW])
            if not any(check in window for check in _CHECKS):
                unchecked.append(f"{path.name}:{i + 1}: {line.strip()}")
    assert not unchecked, "resource read without a visibility check:\n" + "\n".join(unchecked)
