"""modoverrides.lua editing (workshop entries) and local mod folders."""
import re
from pathlib import Path

ENTRY_RE = re.compile(r"""\[\s*["'](workshop-\d+)["']\s*\]\s*=\s*\{""")
ENABLED_RE = re.compile(r"\benabled\s*=\s*(true|false)")


def _block_end(text: str, open_idx: int) -> int:
    """Index just past the '}' matching the '{' at open_idx. Skips Lua strings and -- comments."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c in ('"', "'"):
            i += 1
            while i < n and text[i] != c:
                if text[i] == "\\":
                    i += 1
                i += 1
        elif text.startswith("--", i):
            while i < n and text[i] != "\n":
                i += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced braces in modoverrides.lua")


def entries(text: str) -> list[dict]:
    out = []
    for m in ENTRY_RE.finditer(text):
        end = _block_end(text, m.end() - 1)
        body = text[m.end():end]
        flags = ENABLED_RE.findall(body)
        out.append({
            "id": m.group(1).removeprefix("workshop-"),
            "enabled": not flags or flags[-1] == "true",
            "start": m.start(),
            "end": end,
        })
    return out


def parse_workshop_id(s: str) -> str:
    """'123456', 'workshop-123456', or a steamcommunity.com URL with ?id=123456."""
    m = re.search(r"[?&]id=(\d+)", s) or re.search(r"(\d{5,})", s)
    if not m:
        raise ValueError(f"no workshop id found in {s!r}")
    return m.group(1)


def add(text: str, wid: str) -> str:
    if any(e["id"] == wid for e in entries(text)):
        return text
    line = f'  ["workshop-{wid}"]={{ configuration_options={{  }}, enabled=true }},\n'
    stripped = text.rstrip()
    if not stripped:
        return "return {\n" + line + "}\n"
    if not stripped.endswith("}"):
        raise ValueError("modoverrides.lua does not end with '}'")
    close = text.rfind("}")
    head = text[:close].rstrip()
    if not head.endswith(("{", ",")):
        head += ","
    return head + "\n" + line + text[close:]


def remove(text: str, wid: str) -> str:
    for e in entries(text):
        if e["id"] != wid:
            continue
        start, end = e["start"], e["end"]
        end += re.match(r"\s*,?[ \t]*\n?", text[end:]).end()
        line_start = text.rfind("\n", 0, start) + 1
        if not text[line_start:start].strip():
            start = line_start
        return text[:start] + text[end:]
    return text


def local_mods(mods_dir: Path) -> list[dict]:
    if not mods_dir.exists():
        return []
    return [{"name": d.name, "valid": (d / "modinfo.lua").exists()} for d in sorted(mods_dir.iterdir()) if d.is_dir()]
