#!/usr/bin/env python3
"""Install (or refresh) the MCP Bridge mod into the game's Mods folder.

    uv run python scripts/install_bridge_mod.py            # install/refresh
    uv run python scripts/install_bridge_mod.py --status   # where things are

The mod is pure UI (it changes no rules and no saved games). After the first
install, start Civ 6: it discovers and enables new mods on launch; check
Additional Content > Mods if "MCP Bridge" is not enabled. Changes to the
.modinfo need a game restart; changes to the Lua files take effect on the
next load of a game (in-game context) or return to the main menu (front end).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, "src")
from civ_mcp import bridge  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--status", action="store_true", help="print paths only")
    parser.add_argument("--mods-dir", help="override the game's Mods folder")
    args = parser.parse_args()
    mods_dir = Path(args.mods_dir) if args.mods_dir else None
    target = bridge.installed_mod_dir(mods_dir)
    print(f"bundled mod:   {bridge.bundled_mod_dir()}")
    print(f"game Mods dir: {target.parent}")
    print(f"Lua.log:       {bridge.default_lua_log()}")
    if args.status:
        print(f"installed:     {bridge.is_mod_installed(mods_dir)} ({target})")
        return
    out = bridge.install_mod(mods_dir)
    print(f"installed to:  {out}")
    print(
        "Next: (re)start Civ 6. The game picks the mod up on launch and enables "
        "it; verify under Additional Content > Mods. With CIV_MCP_TRANSPORT unset "
        "(auto) the server talks to the game through the mod whenever it is "
        "installed, including inside hotseat sessions."
    )


if __name__ == "__main__":
    main()
