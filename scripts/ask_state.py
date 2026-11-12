#!/usr/bin/env python3
"""ask_state.py - durable state for the «Спросить» pipeline.

The pipeline must survive a closed browser, a cancelled process and a retry after
an interrupted model call. State is therefore kept next to the question, not in
memory:

  .aurora/cache/ask/<question-id>/
    plan.md              human-readable plan
    plan.json            versioned plan (the UI reads it)
    manifest.json        integrity record for every step and role
    state.json           current status and counters
    events.jsonl         append-only work log
    control.json         pause/cancel requests from the UI
    steps/<item>.md      intermediate result of one plan item
    answer.md            final synthesized answer

The directory is local cache: the project journal in
`AuroraKnowledgeDB/meta/ask/` remains the team-visible conversation. This split
keeps transient prompts and provider details out of git while the final answer
is still written to the journal by the caller.
"""
from __future__ import annotations

import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from pipeline_state import (
    RunStore,
    atomic_write,
    plan_markdown as _plan_markdown,
    read_json,
    safe_id,
    sha256,
    tail_events,
    utc_now,
    write_json,
)

SCHEMA_VERSION = 1
ROOT = Path(".aurora") / "cache" / "ask"
DEFAULT_KEEP = 50
QUARANTINE = "quarantine"


def root_for(cwd: str | Path) -> Path:
    return Path(cwd) / ROOT


def _stamp_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def plan_markdown(plan: dict) -> str:
    return _plan_markdown(
        plan,
        note=("_Обновляется пайплайном «Спросить». Пункт с `x` завершён, `~` частичный, "
              "`!` ошибка, `>` выполняется._"),
    )


class AskState:
    """State of one question. All writes are atomic."""

    def __init__(self, cwd: str | Path, question_id: str = "", question: str = "",
                 keep: int = DEFAULT_KEEP):
        self.cwd = Path(cwd)
        self.root = root_for(self.cwd)
        self.id = safe_id(question_id or f"{_stamp_id()}-{question}")
        self.dir = self.root / self.id
        self.keep = max(1, int(keep or DEFAULT_KEEP))
        self._lock = threading.RLock()
        self.store = RunStore(self.root, self.id)

    # ------------------------------------------------------------ paths
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
    def answer_path(self) -> Path:
        return self.dir / "answer.md"

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    @property
    def control_path(self) -> Path:
        return self.dir / "control.json"

    def step_path(self, item_id: str) -> Path:
        return self.dir / "steps" / (safe_id(item_id) + ".md")

    def check_path(self, item_id: str, role: str) -> Path:
        return self.dir / "checks" / f"{safe_id(item_id)}-{safe_id(role)}.md"

    # ------------------------------------------------------------ lifecycle
    def create(self, question: str, plan: dict, *, mode: str = "ask",
               version: int = 1) -> dict:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.root.mkdir(parents=True, exist_ok=True)
            plan = dict(plan or {})
            plan["version"] = int(plan.get("version") or 1)
            plan["id"] = self.id
            plan["question"] = question
            plan.setdefault("created", utc_now())
            for i, step in enumerate(plan.get("steps") or [], 1):
                step.setdefault("id", f"step-{i}")
                step.setdefault("title", f"Шаг {i}")
                step["status"] = "pending"
            write_json(self.plan_json, plan)
            atomic_write(self.plan_path, plan_markdown(plan))
            manifest = {
                "schema": SCHEMA_VERSION,
                "question_id": self.id,
                "question": question,
                "created": utc_now(),
                "plan_version": int(plan.get("version") or 1),
                "mode": mode,
                "steps": [],
                "answer": {"status": "pending", "sha256": "", "file": ""},
            }
            for step in plan.get("steps") or []:
                manifest["steps"].append({
                    "plan_item_id": step["id"], "title": step.get("title", ""),
                    "status": "pending", "role": "", "model": "", "provider": "",
                    "started": "", "finished": "", "seconds": 0,
                    "sha256": "", "file": "", "attempts": 0, "error": "",
                    "checks": [],
                })
            write_json(self.manifest_path, manifest)
            self.update_state(status="planned", current="", total=len(plan.get("steps") or []),
                              question=question, keep=self.keep)
            self.event("plan", "План создан до первого ответа модели",
                       steps=len(plan.get("steps") or []))
            self.prune()
            return plan

    def state(self) -> dict:
        return self.store.state()

    def update_state(self, **fields) -> dict:
        return self.store.update_state(**fields)

    def plan(self) -> dict:
        return read_json(self.plan_json, {}) or {}

    def set_plan(self, plan: dict, *, mode: str = "", question: str = "") -> dict:
        """Replace a preliminary plan with the planner's plan, versioning it.

        The preliminary plan is written before the first answer call so the file
        always exists. A planner result replaces it as version+1; completed steps
        with the same ids keep their hashes and statuses.
        """
        with self._lock:
            old = self.plan()
            manifest = self.manifest()
            plan = dict(plan or {})
            plan["version"] = int(old.get("version") or 0) + 1
            plan["id"] = self.id
            if question:
                plan["question"] = question
            plan.setdefault("created", old.get("created") or utc_now())
            for i, step in enumerate(plan.get("steps") or [], 1):
                step.setdefault("id", f"step-{i}")
                step.setdefault("title", f"Шаг {i}")
                step.setdefault("status", "pending")
            old_steps = {s.get("plan_item_id"): s for s in manifest.get("steps") or []}
            manifest["steps"] = []
            manifest["plan_version"] = plan["version"]
            if question:
                manifest["question"] = question
            if mode:
                manifest["mode"] = mode
            for step in plan.get("steps") or []:
                prior = old_steps.get(step["id"]) or {}
                manifest["steps"].append({
                    "plan_item_id": step["id"], "title": step.get("title", ""),
                    "status": prior.get("status") or "pending", "role": prior.get("role", ""),
                    "model": prior.get("model", ""), "provider": prior.get("provider", ""),
                    "started": prior.get("started", ""), "finished": prior.get("finished", ""),
                    "seconds": prior.get("seconds", 0), "sha256": prior.get("sha256", ""),
                    "file": prior.get("file", ""), "attempts": prior.get("attempts", 0),
                    "error": prior.get("error", ""), "checks": prior.get("checks") or [],
                })
            write_json(self.plan_json, plan)
            atomic_write(self.plan_path, plan_markdown(plan))
            write_json(self.manifest_path, manifest)
            return plan

    def manifest(self) -> dict:
        return read_json(self.manifest_path, {}) or {}

    def event(self, kind: str, message: str, **fields) -> None:
        self.store.event(kind, message, **fields)

    # ------------------------------------------------------------ steps
    def write_step(self, item_id: str, text: str, *, role: str = "worker",
                   model: str = "", provider: str = "", status: str = "done",
                   seconds: float = 0, error: str = "", attempts: int = 0,
                   check: bool = False) -> dict:
        """Write one step result and update its manifest record atomically."""
        with self._lock:
            path = self.check_path(item_id, role) if check else self.step_path(item_id)
            atomic_write(path, str(text or ""))
            digest = sha256(str(text or ""))
            manifest = self.manifest()
            found = False
            for step in manifest.get("steps") or []:
                if step.get("plan_item_id") != item_id:
                    continue
                found = True
                record = {
                    "role": role, "model": model, "provider": provider,
                    "status": status, "seconds": round(float(seconds or 0), 2),
                    "sha256": digest, "file": str(path.relative_to(self.dir)),
                    "finished": utc_now(), "error": str(error or "")[:1000],
                    "attempts": int(attempts or 0),
                }
                if check:
                    step.setdefault("checks", []).append(record)
                else:
                    step.update(record)
                    step["plan_item_id"] = item_id
            if not found:
                manifest.setdefault("steps", []).append({
                    "plan_item_id": item_id, "title": item_id, "status": status,
                    "role": role, "model": model, "provider": provider,
                    "finished": utc_now(), "seconds": round(float(seconds or 0), 2),
                    "sha256": digest, "file": str(path.relative_to(self.dir)),
                    "attempts": int(attempts or 0), "error": str(error or ""),
                    "checks": [{"role": role, "model": model, "provider": provider,
                                 "sha256": digest}] if check else [],
                })
            write_json(self.manifest_path, manifest)
            if not check:
                self.mark_plan_item(item_id, status)
            self.event("step", f"{item_id}: {role} {status}",
                       step=item_id, role=role, model=model, provider=provider,
                       sha256=digest, seconds=round(float(seconds or 0), 2))
            return {"sha256": digest, "file": str(path), "status": status}

    def mark_step(self, item_id: str, **fields) -> None:
        with self._lock:
            manifest = self.manifest()
            for step in manifest.get("steps") or []:
                if step.get("plan_item_id") == item_id:
                    step.update(fields)
            write_json(self.manifest_path, manifest)

    def mark_plan_item(self, item_id: str, status: str) -> None:
        """Keep plan.json and plan.md in step with the manifest."""
        with self._lock:
            plan = self.plan()
            changed = False
            for step in plan.get("steps") or []:
                if str(step.get("id")) == str(item_id):
                    step["status"] = str(status)
                    changed = True
            if changed:
                write_json(self.plan_json, plan)
                atomic_write(self.plan_path, plan_markdown(plan))

    def write_answer(self, text: str, *, status: str = "done") -> dict:
        with self._lock:
            atomic_write(self.answer_path, str(text or ""))
            digest = sha256(str(text or ""))
            manifest = self.manifest()
            manifest["answer"] = {"status": status, "sha256": digest,
                                  "file": str(self.answer_path.relative_to(self.dir)),
                                  "finished": utc_now()}
            write_json(self.manifest_path, manifest)
            return {"sha256": digest, "file": str(self.answer_path)}

    # ------------------------------------------------------------ resume
    def validate_resume(self) -> dict:
        """Check the plan against files and hashes, quarantine orphans.

        Returns {"ok", "steps": {id: {"status", "reason"}}, "orphans": [...]}.
        A corrupted or missing step is reset to pending; an extra file whose
        plan item is gone is moved into `quarantine/`, never deleted silently.
        """
        plan = self.plan()
        manifest = self.manifest()
        wanted = {str(s.get("id")) for s in plan.get("steps") or []}
        result = {"ok": True, "steps": {}, "orphans": []}
        for step in manifest.get("steps") or []:
            item = str(step.get("plan_item_id") or "")
            path = self.dir / str(step.get("file") or "")
            expected = str(step.get("sha256") or "")
            reason = ""
            if item not in wanted:
                reason = "пункта нет в плане"
            elif step.get("status") not in ("done", "partial"):
                reason = "шаг не завершён"
            elif not path.is_file():
                reason = "файла шага нет"
            elif expected and sha256(path.read_text(encoding="utf-8", errors="replace")) != expected:
                reason = "хеш не совпал"
            if reason:
                step["status"] = "pending"
                step["error"] = f"восстановление: {reason}"
                result["steps"][item] = {"status": "pending", "reason": reason}
                result["ok"] = False
        steps_dir = self.dir / "steps"
        for path in sorted(steps_dir.glob("*.md")) if steps_dir.is_dir() else []:
            item = path.stem
            if item in wanted:
                continue
            qdir = self.dir / QUARANTINE
            qdir.mkdir(parents=True, exist_ok=True)
            target = qdir / path.name
            try:
                shutil.move(str(path), str(target))
                result["orphans"].append(str(target.relative_to(self.dir)))
            except OSError as exc:
                result["orphans"].append(f"{path.name}: {exc}")
        write_json(self.manifest_path, manifest)
        return result

    # ------------------------------------------------------------ control
    def control(self) -> dict:
        return self.store.control()

    def set_control(self, *, pause: bool | None = None, cancel: bool | None = None) -> dict:
        return self.store.set_control(pause=pause, cancel=cancel)

    # ------------------------------------------------------------ retention
    def prune(self) -> list[str]:
        """Keep at most `keep` question directories, newest first.

        Only pipeline cache is pruned here. The team journal lives in
        `AuroraKnowledgeDB/meta/ask/` and is not touched by this helper: deleting
        knowledge is a separate, explicit decision.
        """
        if not self.root.is_dir():
            return []
        rows = sorted((p for p in self.root.iterdir() if p.is_dir() and p.name != QUARANTINE),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        removed = []
        for path in rows[self.keep:]:
            if path == self.dir:
                continue
            try:
                shutil.rmtree(path)
                removed.append(path.name)
            except OSError:
                pass
        removed += prune_journals(self.cwd, self.keep, protect=self.id)
        return removed


def prune_journals(cwd: str | Path, keep: int = DEFAULT_KEEP,
                   protect: str = "") -> list[str]:
    """Keep only the newest question journals in `AuroraKnowledgeDB/meta/ask`.

    The cache and the team journal are two stores of the same question. The
    pipeline prunes both so a long-lived project does not grow two independent
    histories. Only `.md` files are touched; a user's unrelated file in meta is
    left alone.
    """
    root = Path(cwd) / "AuroraKnowledgeDB" / "meta" / "ask"
    if not root.is_dir():
        return []
    keep = max(1, int(keep or DEFAULT_KEEP))
    rows = sorted((p for p in root.glob("*.md") if p.is_file()
                   and (not protect or p.stem != protect)),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for path in rows[keep:]:
        try:
            path.unlink()
            removed.append("journal:" + path.name)
        except OSError:
            pass
    return removed


def read_state(cwd: str | Path, question_id: str) -> dict:
    """Read one question state for the panel, without creating a directory."""
    state = AskState(cwd, question_id)
    if not state.dir.is_dir():
        return {"error": "вопроса нет", "id": state.id}
    plan = state.plan()
    # `pack` is the model's working context, not UI data: returning it would
    # duplicate the knowledge base in every status poll.
    plan = dict(plan)
    plan["steps"] = [{k: v for k, v in step.items() if k != "pack"}
                     for step in plan.get("steps") or []]
    return {
        "id": state.id,
        "question": state.state().get("question", ""),
        "state": state.state(),
        "plan": plan,
        "manifest": state.manifest(),
        "answer": state.answer_path.read_text(encoding="utf-8", errors="replace")
                  if state.answer_path.is_file() else "",
        "events": _tail_events(state.events_path),
    }


def _tail_events(path: Path, limit: int = 80) -> list:
    return tail_events(path, limit)


def list_questions(cwd: str | Path, limit: int = 20) -> list:
    root = root_for(cwd)
    if not root.is_dir():
        return []
    rows = []
    for path in sorted((p for p in root.iterdir() if p.is_dir() and p.name != QUARANTINE),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:max(1, int(limit))]:
        state = AskState(cwd, path.name)
        rows.append({"id": path.name, "state": state.state(),
                     "plan": state.plan().get("title", ""),
                     "answer": state.answer_path.is_file()})
    return rows
