"""Build src/civ_mcp/data/bbg_leaders.json from a saved BBG leaders page.

    curl -o leaders.html https://civ6bbg.github.io/en_US/leaders_7.5.html
    python scripts/extract_bbg.py leaders.html src/civ_mcp/data/bbg_leaders.json

Only the leaders in this group's games are kept (LEADERS below, named as in
the page's LOC_LEADER_<NAME>_NAME comments). Update the list and re-run when
the players or the BBG version change.
"""

from __future__ import annotations

import html
import json
import re
import sys

LEADERS = {
    "CLEOPATRA_ORIGINAL", "CLEOPATRA_ALT", "NZINGA_MBANDE", "VICTORIA_ALT", "SUK_AL_HASAN",
    "JFD_STANISLAW", "JULIUS_CAESAR", "GITARJA", "GENGHIS_KHAN", "AMANITORE", "LIME_THULE_DAVE",
}

_MARK = re.compile(r"<!--LOC_CIVILIZATION_\w+?_NAME LOC_LEADER_(\w+?)_NAME-->")
_ICON = re.compile(r"<img[^>]*ICON_(\w+?)\.webp[^>]*/?>\s*(?P<next>(?=[A-Za-z]))?")


def _icon(m: re.Match[str]) -> str:
    # "+2 <icon Gold/> Gold" already names the icon; a bare icon gets its name.
    return "" if m.group("next") is not None else " " + m.group(1).replace("_", " ").title()


def clean(fragment: str) -> str:
    fragment = _ICON.sub(_icon, fragment)
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = fragment.replace("<span>&#8226;</span>", "-")
    fragment = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    fragment = re.sub(r"[ \t]{2,}", " ", fragment)
    return "\n".join(line.strip() for line in fragment.splitlines() if line.strip())


def extract(page: str) -> dict[str, dict]:
    marks = list(_MARK.finditer(page))
    result: dict[str, dict] = {}
    for i, m in enumerate(marks):
        leader = m.group(1)
        if leader not in LEADERS:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(page)
        seg = page[m.start():end]
        title = re.search(r'<h2 class="civ-name">\s*([^<]+?)\s*<', seg)
        abilities = []
        for h in re.finditer(
            r'<h3 class="civ-ability-name"[^>]*>\s*([^<]+?)\s*(?:<img[^>]*>\s*)?</h3>', seg
        ):
            p = re.search(r'<p class="civ-ability-desc actual-text"[^>]*>(.*?)</p>', seg[h.end():], re.S)
            if p:
                abilities.append({"name": html.unescape(h.group(1)).strip(), "text": clean(p.group(1))})
        result["LEADER_" + leader.removesuffix("_ORIGINAL")] = {
            "title": title.group(1) if title else leader,
            "abilities": abilities,
        }
    return result


if __name__ == "__main__":
    src, out = sys.argv[1:3]
    with open(src, encoding="utf-8") as f:
        data = extract(f.read())
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    print({k: len(v["abilities"]) for k, v in data.items()})
