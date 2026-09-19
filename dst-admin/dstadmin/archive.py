"""Zip validation for cluster uploads and extraction for local mod uploads."""
import re
import shutil
import zipfile
from pathlib import Path

JUNK_DIRS = {"__MACOSX"}
JUNK_FILES = {".DS_Store", "Thumbs.db"}


def _members(z: zipfile.ZipFile) -> list[str]:
    names = []
    for n in z.namelist():
        parts = Path(n).parts
        if any(p in JUNK_DIRS for p in parts) or (parts and parts[-1] in JUNK_FILES):
            continue
        if n.startswith("/") or ".." in parts:
            raise ValueError(f"unsafe path in archive: {n}")
        names.append(n)
    return names


def validate_cluster_zip(path: Path) -> dict:
    """ValueError unless `path` is a zip with cluster.ini at depth 0 or 1. Returns {"prefix", "shards"}."""
    if not zipfile.is_zipfile(path):
        raise ValueError("not a zip file")
    with zipfile.ZipFile(path) as z:
        names = _members(z)
        inis = [n for n in names if Path(n).name == "cluster.ini"]
        if not inis:
            raise ValueError("no cluster.ini in archive")
        ini = min(inis, key=lambda n: len(Path(n).parts))
        if len(Path(ini).parts) > 2:
            raise ValueError(f"cluster.ini nested too deep: {ini}")
        prefix = ini[: -len("cluster.ini")]
        shards = sorted({
            Path(n[len(prefix):]).parts[0]
            for n in names
            if n.startswith(prefix) and Path(n).name == "server.ini" and len(Path(n[len(prefix):]).parts) == 2
        })
        return {"prefix": prefix, "shards": shards}


def extract_mod_zip(path: Path, mods_dir: Path) -> str:
    """Extract a mod zip into mods_dir/<name>/ with modinfo.lua at the top. Returns the folder name."""
    if not zipfile.is_zipfile(path):
        raise ValueError("not a zip file")
    with zipfile.ZipFile(path) as z:
        names = _members(z)
        infos = [n for n in names if Path(n).name == "modinfo.lua"]
        if not infos:
            raise ValueError("no modinfo.lua in archive")
        top = min(infos, key=lambda n: len(Path(n).parts))
        parts = Path(top).parts
        if len(parts) > 2:
            raise ValueError(f"modinfo.lua nested too deep: {top}")
        prefix = top[: -len("modinfo.lua")]
        raw_name = parts[0] if len(parts) == 2 else path.stem
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_name)
        dest = mods_dir / name
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for info in z.infolist():
            n = info.filename
            if n not in names or not n.startswith(prefix) or n == prefix:
                continue
            target = dest / n[len(prefix):]
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
    return name
