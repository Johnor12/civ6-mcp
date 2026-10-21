#!/usr/bin/env python3
"""Convert a hotseat save into a single-player save for the stand-in agent, and back.

LEGACY DETOUR. The MCP Bridge mod (scripts/install_bridge_mod.py) lets the
server play inside the live hotseat session — see docs/hotseat-standin.md.
This script remains for machines where the mod cannot be used.

FireTuner shuts down as soon as Civ 6 enters a hotseat session, so without
the bridge the MCP server cannot play a hotseat turn directly.  The
workaround is a detour through single-player mode:

    hotseat save  --to-single-->  single-player save (one human, rest AI)
                                       |
                              agent plays the turn via civ6-mcp but
                              never ends it, then saves (finish_turn)
                                       |
    hotseat save  <--to-hotseat--  single-player save

Only the uncompressed save header is touched.  Each player slot carries an
``ACTOR_AI_HUMAN`` integer (3 = human, 1 = AI); the game-state blob is left
byte-for-byte intact.  Markers come from the pydt/civ6-save-parser project.

Usage:
    uv run python scripts/hotseat_standin.py inspect "POUNDMAKER 63 150 AD"
    uv run python scripts/hotseat_standin.py to-single "POUNDMAKER 63 150 AD" --leader LEADER_POUNDMAKER
    uv run python scripts/hotseat_standin.py to-hotseat            # uses the marker
    uv run python scripts/hotseat_standin.py status | clear

Save names may be given with or without the .Civ6Save extension.  Bare names
are looked up in the game's Saves/Hotseat and Saves/Single folders; paths are
used as-is.  Originals are never modified.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, "src")
from civ_mcp.game_launcher import HOTSEAT_SAVE_DIR, SINGLE_SAVE_DIR  # noqa: E402
from civ_mcp.standin import (  # noqa: E402
    DEFAULT_OUT_SAVE,
    MODE_SINGLE,
    STANDIN_PATH,
    StandinConfig,
    load_config,
    save_config,
)

# 4-byte record markers in the uncompressed header (pydt/civ6-save-parser).
START_ACTOR = bytes([0x58, 0xBA, 0x7F, 0x4C])
ACTOR_NAME = bytes([0x2F, 0x5C, 0x5E, 0x9D])
LEADER_NAME = bytes([0x5F, 0x5E, 0xCD, 0xE8])
ACTOR_TYPE = bytes([0xBE, 0xAB, 0x55, 0xCA])
ACTOR_AI_HUMAN = bytes([0x95, 0xB9, 0x42, 0xCE])
PLAYER_ALIVE = bytes([0xA6, 0xDF, 0xA7, 0x62])
IS_CURRENT_TURN = bytes([0xCB, 0x21, 0xB0, 0x7A])
GAME_TURN = bytes([0x9D, 0x2C, 0xE6, 0xBD])

AI = 1
HUMAN = 3
FULL_CIV = "CIVILIZATION_LEVEL_FULL_CIV"

EXT = ".Civ6Save"


@dataclass
class Slot:
    offset: int  # START_ACTOR offset
    civ: str
    leader: str
    ai_human_pos: int  # offset of the uint32 value
    ai_human: int
    alive: bool | None
    current_turn: bool | None
    current_turn_pos: int | None = None  # offset of the bool byte

    @property
    def is_human(self) -> bool:
        return self.ai_human == HUMAN


# ── header record readers ─────────────────────────────────────────────────


def _find_all(data: bytes, marker: bytes, start: int, end: int) -> list[int]:
    out: list[int] = []
    pos = start
    while True:
        i = data.find(marker, pos, end)
        if i < 0:
            return out
        out.append(i)
        pos = i + 1


def _read_str(data: bytes, rec: int) -> str:
    """String record: marker(4) type(4) len(3) flags(5) bytes[len] (NUL-terminated)."""
    body = rec + 8
    ln = data[body] | (data[body + 1] << 8) | (data[body + 2] << 16)
    raw = data[body + 8 : body + 8 + ln]
    return raw.split(b"\x00", 1)[0].decode("ascii", "replace")


def _read_int_pos(rec: int) -> int:
    """Int record: marker(4) type(4) zeros(8) uint32 — value lives at +16."""
    return rec + 16


def _read_bool(data: bytes, rec: int) -> bool:
    """Bool record: marker(4) type(4) zeros(8) byte(1) pad(4)."""
    return bool(data[rec + 16])


def header_end(data: bytes) -> int:
    mod_index = data.rfind(b"MOD_TITLE")
    if mod_index < 0:
        raise ValueError("not a Civ 6 save (MOD_TITLE marker missing)")
    return data.index(b"\x78\x9c", mod_index)


def game_turn(data: bytes) -> int | None:
    i = data.find(GAME_TURN, 0, header_end(data))
    if i < 0:
        return None
    return struct.unpack_from("<I", data, _read_int_pos(i))[0]


def read_slots(data: bytes) -> list[Slot]:
    """Return the full-civilization player slots in header order."""
    end = header_end(data)
    starts = _find_all(data, START_ACTOR, 0, end)
    slots: list[Slot] = []
    for k, st in enumerate(starts):
        en = starts[k + 1] if k + 1 < len(starts) else end
        types = _find_all(data, ACTOR_TYPE, st, en)
        aih = _find_all(data, ACTOR_AI_HUMAN, st, en)
        if not types or not aih:
            continue
        if _read_str(data, types[0]) != FULL_CIV:
            continue
        names = _find_all(data, ACTOR_NAME, st, en)
        leaders = _find_all(data, LEADER_NAME, st, en)
        alive = _find_all(data, PLAYER_ALIVE, st, en)
        cur = _find_all(data, IS_CURRENT_TURN, st, en)
        vpos = _read_int_pos(aih[0])
        slots.append(
            Slot(
                offset=st,
                civ=_read_str(data, names[0]) if names else "?",
                leader=_read_str(data, leaders[0]) if leaders else "?",
                ai_human_pos=vpos,
                ai_human=struct.unpack_from("<I", data, vpos)[0],
                alive=_read_bool(data, alive[0]) if alive else None,
                current_turn=_read_bool(data, cur[0]) if cur else None,
                current_turn_pos=(cur[0] + 16) if cur else None,
            )
        )
    return slots


# ── conversions ───────────────────────────────────────────────────────────


def to_single(data: bytes, leader: str) -> tuple[bytes, list[str]]:
    """Keep ``leader`` human; turn every other human full civ into an AI."""
    buf = bytearray(data)
    slots = read_slots(data)
    if not any(s.leader == leader for s in slots):
        raise SystemExit(
            f"leader {leader!r} not in this save. Slots: "
            + ", ".join(s.leader for s in slots)
        )
    changed: list[str] = []
    for s in slots:
        if s.leader == leader:
            if s.ai_human != HUMAN:
                struct.pack_into("<I", buf, s.ai_human_pos, HUMAN)
                changed.append(f"{s.leader}: {s.ai_human}->{HUMAN}")
        elif s.ai_human == HUMAN:
            struct.pack_into("<I", buf, s.ai_human_pos, AI)
            changed.append(f"{s.leader}: {HUMAN}->{AI}")
    return bytes(buf), changed


def to_hotseat(data: bytes, original: bytes) -> tuple[bytes, list[str]]:
    """Restore each slot's human/AI flag — and its "current turn" flag — from ``original``.

    A single-player save clears the per-slot IS_CURRENT_TURN header flag that
    hotseat saves carry for the player whose turn it is, so it is copied back
    from the hotseat save the turn started from.
    """
    buf = bytearray(data)
    want = {s.leader: s for s in read_slots(original)}
    changed: list[str] = []
    for s in read_slots(data):
        orig = want.get(s.leader)
        if orig is None:
            raise SystemExit(f"slot {s.leader} missing from the original hotseat save")
        if orig.ai_human != s.ai_human:
            struct.pack_into("<I", buf, s.ai_human_pos, orig.ai_human)
            changed.append(f"{s.leader}: {s.ai_human}->{orig.ai_human}")
        if (
            s.current_turn_pos is not None
            and orig.current_turn is not None
            and orig.current_turn != s.current_turn
        ):
            buf[s.current_turn_pos] = 1 if orig.current_turn else 0
            changed.append(
                f"{s.leader}: current_turn {s.current_turn}->{orig.current_turn}"
            )
    return bytes(buf), changed


# ── CLI ───────────────────────────────────────────────────────────────────


def _resolve(name: str, *dirs: str) -> Path:
    p = Path(name)
    if p.suffix.lower() != EXT.lower():
        p = p.with_name(p.name + EXT)
    if p.exists():
        return p
    for d in dirs:
        cand = Path(d) / p.name
        if cand.exists():
            return cand
    raise SystemExit(f"save not found: {name} (looked in {', '.join(dirs) or 'cwd'})")


def _out_path(name: str, directory: str) -> Path:
    p = Path(name)
    if p.suffix.lower() != EXT.lower():
        p = p.with_name(p.name + EXT)
    if p.parent == Path("."):
        p = Path(directory) / p.name
    return p


def cmd_inspect(args: argparse.Namespace) -> None:
    path = _resolve(args.save, HOTSEAT_SAVE_DIR, SINGLE_SAVE_DIR)
    data = path.read_bytes()
    print(f"{path}\nturn {game_turn(data)}")
    for s in read_slots(data):
        kind = {HUMAN: "human", AI: "ai"}.get(s.ai_human, str(s.ai_human))
        flags = []
        if s.alive is False:
            flags.append("dead")
        if s.current_turn:
            flags.append("CURRENT TURN")
        print(f"  {s.leader:24} {s.civ:28} {kind:6} {' '.join(flags)}")


def cmd_to_single(args: argparse.Namespace) -> None:
    src = _resolve(args.save, HOTSEAT_SAVE_DIR, SINGLE_SAVE_DIR)
    out = _out_path(args.out or "MCP_STANDIN_IN", SINGLE_SAVE_DIR)
    if out.exists() and not args.force:
        raise SystemExit(f"{out} exists (use --force to overwrite)")
    data, changed = to_single(src.read_bytes(), args.leader)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"wrote {out}")
    for c in changed:
        print("  " + c)
    cfg = StandinConfig(
        active=True,
        mode=MODE_SINGLE,
        leader=args.leader,
        hotseat_save=src.stem,
        single_save=out.stem,
        out_save=args.out_save or DEFAULT_OUT_SAVE,
        note=args.note or "",
    )
    save_config(cfg)
    print(f"stand-in marker written: {STANDIN_PATH}")
    print(
        "Next: start the game, load this save as a single-player game "
        f"(load_game_save('{out.stem}') or Single Player > Load Game), then let the "
        "agent play. The MCP server reads the marker: end_turn saves the finished "
        f"turn as '{cfg.out_save}' instead of ending it."
    )


def cmd_to_hotseat(args: argparse.Namespace) -> None:
    cfg = load_config()
    save_name = args.save or (cfg.out_save if cfg else None)
    original = args.original or (cfg.hotseat_save if cfg else None)
    if not save_name or not original:
        raise SystemExit(
            "no stand-in marker found: pass the finished save and --from <hotseat save>"
        )
    src = _resolve(save_name, SINGLE_SAVE_DIR, HOTSEAT_SAVE_DIR)
    orig = _resolve(original, HOTSEAT_SAVE_DIR, SINGLE_SAVE_DIR)
    data = src.read_bytes()
    turn = game_turn(data)
    leader = (cfg.leader if cfg else "") or next(
        (s.leader for s in read_slots(data) if s.is_human), "PLAYER"
    )
    default_out = f"{leader.replace('LEADER_', '').replace('_', ' ')} {turn} MCP"
    out = _out_path(args.out or default_out, HOTSEAT_SAVE_DIR)
    if out.exists() and not args.force:
        raise SystemExit(f"{out} exists (use --force to overwrite)")
    data, changed = to_hotseat(data, orig.read_bytes())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"wrote {out}")
    for c in changed:
        print("  " + c)
    if cfg is not None and not args.keep_marker:
        cfg.active = False
        save_config(cfg)
        print("stand-in marker deactivated")
    print(
        "Load it in Civ 6 via Multiplayer > Hotseat > Load Game, check the player "
        "list in the staging room, press Start Game, then End Turn for this player."
    )


def cmd_status(args: argparse.Namespace) -> None:
    cfg = load_config()
    if cfg is None:
        print(f"no stand-in marker ({STANDIN_PATH})")
        return
    for k, v in vars(cfg).items():
        print(f"  {k}: {v}")


def cmd_clear(args: argparse.Namespace) -> None:
    if STANDIN_PATH.exists():
        STANDIN_PATH.unlink()
        print(f"removed {STANDIN_PATH}")
    else:
        print("no stand-in marker to remove")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("inspect", help="list player slots and their human/AI flags")
    p.add_argument("save")
    p.set_defaults(fn=cmd_inspect)

    p = sub.add_parser("to-single", help="hotseat save -> single-player save")
    p.add_argument("save", help="hotseat save name or path")
    p.add_argument(
        "--leader", required=True, help="e.g. LEADER_POUNDMAKER (stays human)"
    )
    p.add_argument(
        "--out", help="output name (default MCP_STANDIN_IN, in Saves/Single)"
    )
    p.add_argument(
        "--out-save",
        help=f"name end_turn saves the finished turn under (default {DEFAULT_OUT_SAVE})",
    )
    p.add_argument(
        "--note", help="what the absent player asked for (shown to the agent)"
    )
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_to_single)

    p = sub.add_parser("to-hotseat", help="single-player save -> hotseat save")
    p.add_argument(
        "save",
        nargs="?",
        help="single-player save written by end_turn (default: marker)",
    )
    p.add_argument(
        "--from",
        dest="original",
        help="the hotseat save the turn started from (default: marker)",
    )
    p.add_argument(
        "--out", help="output name (default '<LEADER> <turn> MCP', in Saves/Hotseat)"
    )
    p.add_argument("--keep-marker", action="store_true", help="leave the marker active")
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_to_hotseat)

    p = sub.add_parser("status", help="show the stand-in marker")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("clear", help="remove the stand-in marker")
    p.set_defaults(fn=cmd_clear)

    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
