"""Persisted poll/backup state (data/backups/state.json) and atomic JSON writes."""
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class State:
    last_cycles: int = -1
    last_players: int = -1
    last_r2_cycles: int = -1
    last_backup: dict | None = None
    last_upload: dict | None = None


def save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def load_state(path: Path) -> State:
    if not path.exists():
        return State()
    data = json.loads(path.read_text())
    defaults = asdict(State())
    return State(**{k: data.get(k, v) for k, v in defaults.items()})


def save_state(path: Path, state: State) -> None:
    save_json(path, asdict(state))
