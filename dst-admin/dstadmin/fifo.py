"""fifo.py — write console lines into a shard FIFO without blocking."""
import errno
import os
from pathlib import Path


def send_line(fifo: Path, line: str) -> bool:
    """False when the shard is not running (no reader) or the FIFO does not exist."""
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno in (errno.ENXIO, errno.ENOENT):
            return False
        raise
    try:
        os.write(fd, (line + "\n").encode())
    finally:
        os.close(fd)
    return True
