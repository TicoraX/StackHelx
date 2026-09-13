import collections
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import guardrails, registry

MAX_LIMIT = 50
MAX_ENTRIES = 500
RETAIN_ENTRIES = 250
_history_lock = threading.Lock()


def _history_file(pid: str) -> Path:
    clean_pid = guardrails.validate_identifier(pid, "pid")
    return (registry.HOME / "history" / f"{clean_pid}.jsonl").resolve()


def _read_tail_lines(path: Path, limit: int) -> list[str]:
    """Lee las últimas `limit` líneas de un archivo sin cortes en líneas largas."""
    if limit <= 0:
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return [line.rstrip("\r\n") for line in collections.deque(f, maxlen=limit) if line.strip()]
    except OSError:
        return []


def _rotate_if_needed(path: Path) -> None:
    """Si el archivo supera el umbral de tamaño, lo trunca reteniendo solo los últimos RETAIN_ENTRIES."""
    try:
        if not path.is_file() or path.stat().st_size < 100_000:
            return
        last_lines = _read_tail_lines(path, RETAIN_ENTRIES)
        if last_lines:
            tmp_path = path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                for line_str in last_lines:
                    f.write(line_str.strip() + "\n")
            tmp_path.replace(path)
    except (OSError, ValueError):
        pass


def append(pid: str, data: dict[str, Any]) -> None:
    if "timestamp" not in data:
        data["timestamp"] = datetime.now(timezone.utc).isoformat()

    try:
        path = _history_file(pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _history_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(data) + "\n")
            _rotate_if_needed(path)
    except (OSError, ValueError):
        pass


def read(pid: str, limit: int = 5) -> list[dict[str, Any]]:
    limit = max(1, min(limit, MAX_LIMIT))
    try:
        path = _history_file(pid)
    except ValueError:
        return []

    if not path.is_file():
        return []

    records = []
    with _history_lock:
        raw_lines = _read_tail_lines(path, limit)

    for line in raw_lines:
        if line.strip():
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    records.append(record)
            except json.JSONDecodeError:
                continue

    return records
