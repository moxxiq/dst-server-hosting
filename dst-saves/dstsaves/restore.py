"""Extract a cluster zip over the active cluster directory."""
import shutil
import zipfile
from pathlib import Path

JUNK_DIRS = {"__MACOSX"}
JUNK_FILES = {".DS_Store", "Thumbs.db"}


def _is_junk(name: str) -> bool:
    parts = Path(name).parts
    return any(p in JUNK_DIRS for p in parts) or (len(parts) > 0 and parts[-1] in JUNK_FILES)


def cluster_prefix(z: zipfile.ZipFile) -> str:
    """'' when cluster.ini is at the archive root, '<folder>/' when one level down.
    ValueError for traversal, missing cluster.ini, or deeper nesting."""
    names = [n for n in z.namelist() if not _is_junk(n)]
    for n in names:
        if n.startswith("/") or ".." in Path(n).parts:
            raise ValueError(f"unsafe path in archive: {n}")
    inis = [n for n in names if Path(n).name == "cluster.ini"]
    if not inis:
        raise ValueError("archive contains no cluster.ini")
    ini = min(inis, key=lambda n: len(Path(n).parts))
    if len(Path(ini).parts) > 2:
        raise ValueError(f"cluster.ini is nested too deep: {ini}")
    return ini[: -len("cluster.ini")]


def extract_cluster(zip_path: Path, dest: Path) -> int:
    """Replace `dest` with the cluster stored in `zip_path`. Returns the file count."""
    tmp = dest.parent / f".extracting-{dest.name}"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    count = 0
    with zipfile.ZipFile(zip_path) as z:
        prefix = cluster_prefix(z)
        for info in z.infolist():
            n = info.filename
            if _is_junk(n) or not n.startswith(prefix) or n == prefix:
                continue
            target = tmp / n[len(prefix):]
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    if dest.exists():
        shutil.rmtree(dest)
    tmp.rename(dest)
    return count
