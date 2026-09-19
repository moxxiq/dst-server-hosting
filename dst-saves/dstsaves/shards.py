"""Talk to running DST shards: console lines through /ctl/<Shard>.cmd FIFOs,
answers read back from <Shard>/server_log.txt."""
import errno
import os
import re
import time
from pathlib import Path

LOG_TAIL_BYTES = 65536


def fifo_paths(ctl_dir: Path) -> dict[str, Path]:
    return {p.stem: p for p in sorted(ctl_dir.glob("*.cmd"))}


def open_fifo_for_write(fifo: Path) -> int | None:
    """Non-blocking write open. None when the shard is not running (no reader) or no FIFO."""
    try:
        return os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.ENOENT):
            return None
        raise


def shard_running(fifo: Path) -> bool:
    fd = open_fifo_for_write(fifo)
    if fd is None:
        return False
    os.close(fd)
    return True


def send_line(fifo: Path, line: str) -> bool:
    fd = open_fifo_for_write(fifo)
    if fd is None:
        return False
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)
    return True


def tail_text(path: Path, nbytes: int = LOG_TAIL_BYTES) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        return f.read().decode("utf-8", "replace")


def is_master(cluster_dir: Path, shard: str) -> bool:
    ini = cluster_dir / shard / "server.ini"
    if not ini.exists():
        return False
    return re.search(r"^\s*is_master\s*=\s*true", ini.read_text(errors="replace"), re.I | re.M) is not None


def query_shard(cluster_dir: Path, shard: str, fifo: Path, timeout: float = 6.0) -> tuple[int, int] | None:
    """Ask one shard for (cycles, players). None = not running or no answer in time."""
    nonce = f"{time.time_ns():x}"
    lua = (
        f'print("DSTSAVES {nonce} cycles=" .. tostring(TheWorld and TheWorld.state.cycles or -1)'
        f' .. " players=" .. tostring(AllPlayers and #AllPlayers or 0))'
    )
    if not send_line(fifo, lua):
        return None
    pattern = re.compile(rf"DSTSAVES {nonce} cycles=(-?\d+) players=(\d+)")
    log_path = cluster_dir / shard / "server_log.txt"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.5)
        m = pattern.search(tail_text(log_path))
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def save_all(ctl_dir: Path) -> list[str]:
    """c_save() on every running shard. Returns the shard names that received it."""
    return [shard for shard, fifo in fifo_paths(ctl_dir).items() if send_line(fifo, "c_save()")]


def wait_quiet(cluster_dir: Path, quiet: float = 3.0, max_wait: float = 30.0) -> None:
    """Return once nothing under <Shard>/save/ changed for `quiet` seconds (or after max_wait)."""
    deadline = time.monotonic() + max_wait
    while True:
        newest = 0.0
        for p in cluster_dir.glob("*/save/**/*"):
            if p.is_file():
                newest = max(newest, p.stat().st_mtime)
        if time.time() - newest >= quiet or time.monotonic() >= deadline:
            return
        time.sleep(1)
