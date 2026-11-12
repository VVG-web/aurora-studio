#!/usr/bin/env python3
"""pipeline_state.py - durable primitives shared by long-running pipelines.

The module intentionally knows nothing about questions or artifacts. It owns the
parts that must survive a closed panel and a killed process: atomic writes,
JSON state, SHA-256, a human-readable plan, an append-only event log and the
cooperative pause/cancel file.

`ask_state` and `make_state` build their own step/attempt models on top of it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

QUARANTINE = "quarantine"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8", "replace")).hexdigest()


def safe_id(text: str, limit: int = 48) -> str:
    value = re.sub(r"[^\w.-]+", "-", str(text or "").strip().lower(), flags=re.UNICODE)
    value = value.strip("-._")[:limit]
    return value or "run"


def number(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def section_scalars(cwd: str | Path, name: str) -> dict:
    """Read scalar keys of an optional project YAML section without a YAML dependency."""
    path = Path(cwd) / "aurora.config.yaml"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    out, inside = {}, False
    for line in lines:
        if re.match(rf"^{re.escape(name)}\s*:\s*$", line):
            inside = True
            continue
        if inside:
            if line.strip() and not line.startswith((" ", "\t")):
                break
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            m = re.match(r"^\s+([\w-]+)\s*:\s*(.*?)\s*$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def atomic_write(path: str | Path, text: str, *, mode: int | None = None) -> Path:
    """Write text completely or leave the previous file untouched.

    The UI and the resume path may read a file while a model call is still
    finishing. A half-written manifest is worse than an older one because it
    silently loses completed work.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(str(text))
        f.flush()
        os.fsync(f.fileno())
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, path)
    return path


def read_json(path: str | Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: str | Path, data, *, mode: int | None = None) -> Path:
    return atomic_write(
        path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", mode=mode
    )


def plan_markdown(plan: dict, *, note: str = "") -> str:
    """Render a plan with checkboxes. Works for answer steps and artifact sections."""
    title = str((plan or {}).get("title") or "План")
    lines = [f"# План: {title}", ""]
    steps = list((plan or {}).get("steps") or [])
    for i, step in enumerate(steps, 1):
        status = str(step.get("status") or "pending")
        mark = {
            "done": "x",
            "partial": "~",
            "failed": "!",
            "running": ">",
            "blocked": "=",
            "skipped": "-",
        }.get(status, " ")
        lines.append(f"- [{mark}] {i}. {step.get('title') or step.get('id') or 'Шаг'}")
        if step.get("brief"):
            lines.append(f"  {str(step['brief']).strip()}")
    lines.append("")
    if note:
        lines.extend([note, ""])
    return "\n".join(lines) + "\n"


def tail_events(path: str | Path, limit: int = 80) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError):
        return []
    return rows[-max(1, int(limit or 80)):]


class RunStore:
    """Filesystem layout and atomic lifecycle of one durable run."""

    def __init__(self, root: str | Path, run_id: str, *, keep: int = 0):
        self.root = Path(root)
        self.id = safe_id(run_id)
        self.dir = self.root / self.id
        self.keep = max(0, int(keep or 0))
        self._lock = threading.RLock()

    @property
    def plan_path(self) -> Path:
        return self.dir / "plan.md"

    @property
    def plan_json(self) -> Path:
        return self.dir / "plan.json"

    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    @property
    def control_path(self) -> Path:
        return self.dir / "control.json"

    def ensure(self) -> "RunStore":
        self.dir.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        return self

    def state(self) -> dict:
        return read_json(self.state_path, {}) or {}

    def update_state(self, **fields) -> dict:
        with self._lock:
            state = self.state()
            state.update(fields)
            state.setdefault("id", self.id)
            state["updated"] = utc_now()
            write_json(self.state_path, state)
            return state

    def event(self, kind: str, message: str, **fields) -> None:
        row = {"at": utc_now(), "kind": str(kind), "message": str(message)[:1000], **fields}
        self.ensure()
        with self._lock:
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def control(self) -> dict:
        got = read_json(self.control_path, {}) or {}
        return {
            "pause": bool(got.get("pause")),
            "cancel": bool(got.get("cancel")),
            "continue": bool(got.get("continue")),
            "updated": got.get("updated", ""),
        }

    def set_control(
        self,
        *,
        pause: bool | None = None,
        cancel: bool | None = None,
        continue_run: bool | None = None,
    ) -> dict:
        with self._lock:
            got = self.control()
            if pause is not None:
                got["pause"] = bool(pause)
            if cancel is not None:
                got["cancel"] = bool(cancel)
            if continue_run is not None:
                got["continue"] = bool(continue_run)
            got["updated"] = utc_now()
            write_json(self.control_path, got)
            return got
