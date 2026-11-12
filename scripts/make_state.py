#!/usr/bin/env python3
"""make_state.py - durable state, attempts, drafts and reports for `agent:make`.

The artifact itself remains in the project's declared output folder. This module
keeps the operational history next to the session:

  Workspaces/<session>/run/
    plan.md, plan.json       plan before the first writer call
    manifest.json            hashes of pack, drafts, checks and artifact
    state.json               status, counters, current stage
    events.jsonl             append-only progress log
    control.json             pause/cancel/continue requests
    drafts/vN.md             every writer version, never overwritten
    checks/vN-role.json      critic and Momus verdicts per version
    report.json              machine-readable run report

Reports for a manager are written to `Artifacts/reports/make/` and are the file
opened by the "Посмотреть отчёт" button in the panel.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from pipeline_state import (
    RunStore,
    atomic_write,
    number,
    plan_markdown,
    read_json,
    safe_id,
    section_scalars,
    sha256,
    tail_events,
    utc_now,
    write_json,
)

SCHEMA_VERSION = 1
RUN_DIR = "run"
REPORT_DIR = Path("Artifacts") / "reports" / "make"
DEFAULT_TOTAL = 3600.0
HARD_TOTAL_CAP = 3600.0
LONG_SECONDS = 600
DEFAULT_ATTEMPTS = 3
DEFAULT_SAME_ERROR = 2
DEFAULT_NO_PROGRESS = 2
DEFAULT_QUESTION_ROUNDS = 3
DEFAULT_REPAIRS = 2
DEFAULT_PARALLEL_RUNS = 3
SID_RE = re.compile(r"[\w][\w.-]*\Z")


def _warn_seconds(raw) -> list[int]:
    if isinstance(raw, (list, tuple)):
        values = raw
    else:
        values = re.split(r"[,;\s]+", str(raw or ""))
    out = []
    for value in values:
        try:
            n = int(float(value))
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.append(n)
    return sorted(set(out)) or [600, 1200]


def pipeline_config(cfg: dict | None = None, cwd: str | Path = ".") -> dict:
    """Limits are anti-loop limits, not per-stage work budgets."""
    raw = dict((cfg or {}).get("make_pipeline") or {})
    if not raw:
        raw = section_scalars(cwd, "make_pipeline")
    total = number(raw.get("hard_timeout_seconds")
                  or raw.get("total_seconds"), DEFAULT_TOTAL)
    total = max(1.0, min(HARD_TOTAL_CAP, total))
    attempts = int(number(raw.get("max_attempts_per_stage")
                          or raw.get("max_attempts"), DEFAULT_ATTEMPTS))
    same = int(number(raw.get("max_same_error"), DEFAULT_SAME_ERROR))
    no_progress = int(number(raw.get("max_no_progress"), DEFAULT_NO_PROGRESS))
    rounds = int(number(raw.get("max_question_rounds"), DEFAULT_QUESTION_ROUNDS))
    repairs = int(number(raw.get("max_repairs"), DEFAULT_REPAIRS))
    parallel = int(number(raw.get("max_parallel_runs"), DEFAULT_PARALLEL_RUNS))
    return {
        "hard_timeout_seconds": total,
        "max_attempts_per_stage": max(1, min(10, attempts)),
        "max_same_error": max(1, min(5, same)),
        "max_no_progress": max(1, min(5, no_progress)),
        "max_question_rounds": max(1, min(10, rounds)),
        "max_repairs": max(0, min(2, repairs)),
        "max_parallel_runs": max(1, min(16, parallel)),
        "warn_seconds": _warn_seconds(raw.get("warn_seconds")),
        "long_seconds": LONG_SECONDS,
        "max_calls": 0,
        "max_tokens": 0,
        "report_dir": str(raw.get("report_dir") or REPORT_DIR).replace("\\", "/"),
    }


def reports_dir(cwd: str | Path, settings: dict | None = None) -> Path:
    rel = str((settings or {}).get("report_dir") or REPORT_DIR).replace("\\", "/")
    return Path(cwd) / rel


def _sid_ok(sid: str) -> bool:
    return bool(SID_RE.match(str(sid or ""))) and ".." not in str(sid or "")


def make_run(cwd: str | Path, sid: str, *, kind: str = "", title: str = "",
             settings: dict | None = None) -> "MakeRun":
    return MakeRun(cwd, sid, kind=kind, title=title, settings=settings)


class UsageCollector:
    """One record per actual model call. Thread-safe for future multi-run use."""

    def __init__(self):
        self._lock = threading.RLock()
        self.rows: list[dict] = []

    def add(self, stage: str, role: str, result: dict, *, attempt: int = 1) -> dict:
        result = result if isinstance(result, dict) else {}
        log = " ".join(str(x) for x in result.get("log") or [])
        backend = result.get("backend")
        row = {
            "stage": str(stage or ""),
            "role": str(role or ""),
            "attempt": int(attempt or 1),
            "ok": bool(result.get("ok")),
            "model": str(result.get("model") or ""),
            "provider": str(result.get("provider") or backend or ""),
            "seconds": round(float(result.get("seconds") or 0), 2),
            "tokens_in": int(result.get("tokens_in") or 0),
            "tokens_out": int(result.get("tokens_out") or 0),
            "fallback": bool(backend not in (None, 1) or "запас" in log.lower()
                             or "фоллбэк" in log.lower()),
            "error": str((result.get("error") or {}).get("message")
                         or (result.get("error") or {}).get("reason")
                         or result.get("why") or "")[:500] if not result.get("ok") else "",
        }
        with self._lock:
            self.rows.append(row)
        return row

    def summary(self) -> dict:
        with self._lock:
            rows = list(self.rows)
        stages: dict[str, dict] = {}
        for row in rows:
            item = stages.setdefault(row["stage"] or "other", {
                "calls": 0, "seconds": 0.0, "tokens_in": 0, "tokens_out": 0,
                "errors": 0, "retries": 0,
            })
            item["calls"] += 1
            item["seconds"] = round(item["seconds"] + float(row["seconds"] or 0), 2)
            item["tokens_in"] += int(row["tokens_in"] or 0)
            item["tokens_out"] += int(row["tokens_out"] or 0)
            item["errors"] += 0 if row["ok"] else 1
            item["retries"] += max(0, int(row["attempt"] or 1) - 1)
        return {
            "calls": len(rows),
            "seconds": round(sum(float(r["seconds"] or 0) for r in rows), 2),
            "tokens_in": sum(int(r["tokens_in"] or 0) for r in rows),
            "tokens_out": sum(int(r["tokens_out"] or 0) for r in rows),
            "models": sorted({r["model"] for r in rows if r["model"]}),
            "providers": sorted({r["provider"] for r in rows if r["provider"]}),
            "fallbacks": sum(1 for r in rows if r["fallback"]),
            "errors": sum(1 for r in rows if not r["ok"]),
            "retries": sum(max(0, int(r["attempt"] or 1) - 1) for r in rows),
            "stages": stages,
            "rows": rows,
            "tokens_estimated": not any(r["tokens_in"] or r["tokens_out"] for r in rows),
        }


class MakeRun:
    """One artifact production run. All writes are atomic."""

    def __init__(self, cwd: str | Path, sid: str, *, kind: str = "", title: str = "",
                 settings: dict | None = None):
        if not _sid_ok(sid):
            raise ValueError(f"недопустимый идентификатор сессии: {sid!r}")
        self.cwd = Path(cwd)
        self.sid = str(sid)
        self.kind = str(kind or "")
        self.title = str(title or "")
        self.settings = dict(settings or {})
        self.dir = self.cwd / "Workspaces" / self.sid / RUN_DIR
        self._lock = threading.RLock()
        self.store = RunStore(self.dir.parent, RUN_DIR)
        self.drafts_dir = self.dir / "drafts"
        self.checks_dir = self.dir / "checks"

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
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    @property
    def control_path(self) -> Path:
        return self.dir / "control.json"

    @property
    def report_json(self) -> Path:
        return self.dir / "report.json"

    def draft_path(self, version: int) -> Path:
        return self.drafts_dir / f"v{int(version)}.md"

    def check_path(self, version: int, role: str) -> Path:
        return self.checks_dir / f"v{int(version)}-{safe_id(role, 24)}.json"

    # ------------------------------------------------------------ lifecycle
    def ensure(self) -> "MakeRun":
        self.dir.mkdir(parents=True, exist_ok=True)
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        self.checks_dir.mkdir(parents=True, exist_ok=True)
        self.store.ensure()
        return self

    def state(self) -> dict:
        return self.store.state()

    def update_state(self, **fields) -> dict:
        fields.setdefault("id", self.sid)
        return self.store.update_state(**fields)

    def event(self, kind: str, message: str, **fields) -> None:
        self.store.event(kind, message, sid=self.sid, **fields)

    def control(self) -> dict:
        return self.store.control()

    def set_control(self, *, pause: bool | None = None, cancel: bool | None = None,
                    continue_run: bool | None = None) -> dict:
        return self.store.set_control(pause=pause, cancel=cancel,
                                      continue_run=continue_run)

    def create(self, *, meta: dict | None = None) -> dict:
        with self._lock:
            self.ensure()
            if not self.state():
                self.update_state(status="planned", current="", started=utc_now(),
                                  **{k: v for k, v in (meta or {}).items()
                                     if k in ("kind", "title", "idea")})
            if not self.manifest_path.is_file():
                write_json(self.manifest_path, {
                    "schema": SCHEMA_VERSION,
                    "sid": self.sid,
                    "kind": self.kind,
                    "title": self.title,
                    "created": utc_now(),
                    "plan_version": 0,
                    "plan_sha256": "",
                    "pack": {"chars": 0, "sha256": ""},
                    "stages": {},
                    "drafts": [],
                    "best": {},
                    "checks": [],
                    "attempts": [],
                    "artifact": {"path": "", "sha256": "", "status": "pending"},
                    "report": {"json": "", "md": "", "status": ""},
                })
            return self.state()

    def plan(self) -> dict:
        return read_json(self.plan_json, {}) or {}

    def set_plan(self, plan: dict, *, source: str = "", question: str = "",
                 note: str = "") -> dict:
        with self._lock:
            old = self.plan()
            manifest = self.manifest()
            data = dict(plan or {})
            data["version"] = int(old.get("version") or 0) + 1
            data["id"] = self.sid
            if question:
                data["question"] = question
            data.setdefault("created", utc_now())
            if source:
                data["source"] = source
            prior = {str(s.get("id")): s for s in old.get("steps") or []}
            for i, step in enumerate(data.get("steps") or [], 1):
                step.setdefault("id", f"step-{i}")
                step.setdefault("title", f"Шаг {i}")
                previous = prior.get(str(step["id"])) or {}
                step.setdefault("status", previous.get("status") or "pending")
            data["updated"] = utc_now()
            self.ensure()
            write_json(self.plan_json, data)
            atomic_write(self.plan_path, plan_markdown(
                data, note=note or ("_Обновляется производством артефакта. "
                                    "`x` проверено, `~` частично, `!` ошибка._")))
            manifest["plan_version"] = data["version"]
            manifest["plan_sha256"] = sha256(str(data))
            manifest["plan_source"] = data.get("source", "")
            manifest["stages"] = manifest.get("stages") or {}
            write_json(self.manifest_path, manifest)
            return data

    def mark_plan(self, statuses: dict, notes: dict | None = None) -> dict:
        """Update plan item statuses from a critic verdict."""
        with self._lock:
            plan = self.plan()
            notes = notes or {}
            for step in plan.get("steps") or []:
                key = str(step.get("id"))
                if key in statuses:
                    value = str(statuses[key] or "").strip()
                    if value in ("done", "partial", "failed", "not_checked", "blocked"):
                        step["status"] = value
                if key in notes:
                    step["check_note"] = str(notes[key])[:400]
            if plan:
                plan["updated"] = utc_now()
                write_json(self.plan_json, plan)
                atomic_write(self.plan_path, plan_markdown(
                    plan, note=("_Обновляется производством артефакта. `x` проверено, "
                                "`~` частично, `!` ошибка._")))
            return plan

    # ------------------------------------------------------------ manifest
    def manifest(self) -> dict:
        return read_json(self.manifest_path, {}) or {}

    def save_manifest(self, manifest: dict) -> dict:
        with self._lock:
            write_json(self.manifest_path, manifest)
            return manifest

    def update_manifest(self, **fields) -> dict:
        with self._lock:
            manifest = self.manifest()
            manifest.update(fields)
            write_json(self.manifest_path, manifest)
            return manifest

    def set_pack(self, pack: str) -> dict:
        return self.update_manifest(pack={"chars": len(pack or ""),
                                          "sha256": sha256(pack or "")})

    def set_stage(self, stage: str, when: str | None = None) -> dict:
        with self._lock:
            manifest = self.manifest()
            stages = manifest.setdefault("stages", {})
            stages[str(stage)] = when or utc_now()
            write_json(self.manifest_path, manifest)
            return stages

    def write_draft(self, version: int, text: str, *, role: str = "worker",
                    model: str = "", provider: str = "", seconds: float = 0,
                    status: str = "candidate", parent: int = 0,
                    reason: str = "") -> dict:
        with self._lock:
            self.ensure()
            path = self.draft_path(version)
            atomic_write(path, str(text or ""))
            record = {
                "version": int(version),
                "file": str(path.relative_to(self.dir)),
                "sha256": sha256(text or ""),
                "chars": len(str(text or "")),
                "role": role,
                "model": str(model or ""),
                "provider": str(provider or ""),
                "seconds": round(float(seconds or 0), 2),
                "status": status,
                "parent": int(parent or 0),
                "reason": str(reason or "")[:500],
                "created": utc_now(),
            }
            manifest = self.manifest()
            drafts = [d for d in manifest.get("drafts") or []
                      if int(d.get("version") or 0) != int(version)]
            drafts.append(record)
            manifest["drafts"] = sorted(drafts, key=lambda d: int(d.get("version") or 0))
            manifest["latest"] = record
            write_json(self.manifest_path, manifest)
            self.event("draft", f"версия {version}: {record['chars']} знаков",
                       version=int(version), sha256=record["sha256"],
                       model=record["model"], seconds=record["seconds"])
            return record

    def read_draft(self, version: int) -> str:
        try:
            return self.draft_path(version).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def latest_draft(self) -> dict:
        return self.manifest().get("latest") or {}

    def set_best(self, version: int, *, reason: str = "") -> dict:
        with self._lock:
            manifest = self.manifest()
            found = next((d for d in manifest.get("drafts") or []
                          if int(d.get("version") or 0) == int(version)), {})
            if found:
                manifest["best"] = dict(found, reason=str(reason or ""))
                for item in manifest.get("drafts") or []:
                    if int(item.get("version") or 0) == int(version):
                        item["status"] = "best"
                write_json(self.manifest_path, manifest)
            return manifest.get("best") or {}

    def best_draft(self) -> dict:
        return self.manifest().get("best") or {}

    def set_artifact(self, path: str, *, status: str = "") -> dict:
        with self._lock:
            manifest = self.manifest()
            full = self.cwd / path if path else None
            digest = ""
            if full and full.is_file():
                digest = sha256(full.read_text(encoding="utf-8", errors="replace"))
            manifest["artifact"] = {"path": str(path or ""), "sha256": digest,
                                    "status": str(status or ""), "updated": utc_now()}
            write_json(self.manifest_path, manifest)
            return manifest["artifact"]

    def write_check(self, version: int, role: str, text: str, *, data: dict | None = None,
                    model: str = "", provider: str = "", seconds: float = 0,
                    status: str = "done", error: str = "") -> dict:
        with self._lock:
            self.ensure()
            path = self.check_path(version, role)
            payload = {
                "version": int(version),
                "role": str(role),
                "status": str(status),
                "model": str(model or ""),
                "provider": str(provider or ""),
                "seconds": round(float(seconds or 0), 2),
                "error": str(error or "")[:1000],
                "text": str(text or ""),
                "data": data if isinstance(data, dict) else {},
                "sha256": sha256(text or ""),
                "created": utc_now(),
            }
            write_json(path, payload)
            manifest = self.manifest()
            checks = [c for c in manifest.get("checks") or []
                      if not (int(c.get("version") or 0) == int(version)
                              and str(c.get("role")) == str(role))]
            checks.append({k: v for k, v in payload.items() if k != "text"})
            manifest["checks"] = sorted(
                checks, key=lambda c: (int(c.get("version") or 0), str(c.get("role"))))
            write_json(self.manifest_path, manifest)
            self.event("check", f"v{version}: {role} {status}", version=int(version),
                       role=str(role), seconds=round(float(seconds or 0), 2),
                       error=str(error or "")[:200])
            return payload

    def add_attempt(self, record: dict) -> dict:
        with self._lock:
            manifest = self.manifest()
            attempts = list(manifest.get("attempts") or [])
            row = {
                "stage": str(record.get("stage") or ""),
                "role": str(record.get("role") or ""),
                "model": str(record.get("model") or ""),
                "provider": str(record.get("provider") or ""),
                "seconds": round(float(record.get("seconds") or 0), 2),
                "tokens_in": int(record.get("tokens_in") or 0),
                "tokens_out": int(record.get("tokens_out") or 0),
                "ok": bool(record.get("ok")),
                "error": str(record.get("error") or "")[:500],
                "at": utc_now(),
            }
            attempts.append(row)
            manifest["attempts"] = attempts[-200:]
            write_json(self.manifest_path, manifest)
            return row

    # ------------------------------------------------------------ reports
    def manager_report_path(self, artifact_path: str = "") -> Path:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        kind = safe_id(self.kind or "artifact", 24)
        title = safe_id(self.title or Path(str(artifact_path or "")).stem or self.sid, 40)
        return reports_dir(self.cwd, self.settings) / f"{date}_{kind}_{title}_{safe_id(self.sid, 20)}.md"

    def write_report(self, data: dict, *, manager_md: str = "",
                     artifact_path: str = "") -> dict:
        with self._lock:
            self.ensure()
            write_json(self.report_json, data)
            manifest = self.manifest()
            report = {"json": str(self.report_json.relative_to(self.dir)),
                      "md": "", "status": str(data.get("status") or "")}
            if manager_md.strip():
                path = self.manager_report_path(artifact_path)
                atomic_write(path, manager_md.strip() + "\n")
                report["md"] = str(path.relative_to(self.cwd)).replace("\\", "/")
            manifest["report"] = report
            write_json(self.manifest_path, manifest)
            self.event("report", "отчёт сохранён", md=report["md"],
                       status=report["status"])
            return report

    # ------------------------------------------------------------ resume
    def validate_resume(self) -> dict:
        """Check draft files against the manifest. An artifact is never deleted."""
        with self._lock:
            manifest = self.manifest()
            result = {"ok": True, "drafts": {}, "artifact_changed": False}
            for draft in manifest.get("drafts") or []:
                path = self.dir / str(draft.get("file") or "")
                version = int(draft.get("version") or 0)
                expected = str(draft.get("sha256") or "")
                if not path.is_file():
                    result["ok"] = False
                    result["drafts"][version] = "файла нет"
                elif expected and sha256(path.read_text(encoding="utf-8", errors="replace")) != expected:
                    result["ok"] = False
                    result["drafts"][version] = "хеш не совпал"
                if result["drafts"].get(version):
                    draft["status"] = "corrupt"
            best = manifest.get("best") or {}
            if best and result["drafts"].get(int(best.get("version") or 0)):
                manifest["best"] = {}
                result["ok"] = False
            artifact = manifest.get("artifact") or {}
            path = self.cwd / str(artifact.get("path") or "")
            expected = str(artifact.get("sha256") or "")
            if artifact.get("path") and path.is_file() and expected:
                got = sha256(path.read_text(encoding="utf-8", errors="replace"))
                if got != expected:
                    result["artifact_changed"] = True
            write_json(self.manifest_path, manifest)
            return result


def read_run(cwd: str | Path, sid: str, *, limit: int = 80) -> dict:
    """Read one run for the panel without creating files."""
    if not _sid_ok(sid):
        return {"error": "недопустимый идентификатор сессии", "sid": sid}
    run = MakeRun(cwd, sid)
    if not run.state_path.is_file():
        return {"error": "запуска нет", "sid": sid}
    plan = run.plan()
    plan = dict(plan)
    plan["steps"] = [{k: v for k, v in step.items() if k != "pack"}
                     for step in plan.get("steps") or []]
    manifest = run.manifest()
    return {
        "sid": sid,
        "state": run.state(),
        "plan": plan,
        "manifest": manifest,
        "report": read_json(run.report_json, {}) or {},
        "events": tail_events(run.events_path, limit),
        "control": run.control(),
        "artifact": manifest.get("artifact") or {},
        "report_md": (manifest.get("report") or {}).get("md", ""),
    }


def list_runs(cwd: str | Path, *, limit: int = 50) -> list[dict]:
    root = Path(cwd) / "Workspaces"
    if not root.is_dir():
        return []
    rows = []
    for run_dir in root.glob(f"*/{RUN_DIR}"):
        if not run_dir.is_dir():
            continue
        sid = run_dir.parent.name
        if not _sid_ok(sid):
            continue
        try:
            state = read_json(run_dir / "state.json", {}) or {}
            manifest = read_json(run_dir / "manifest.json", {}) or {}
            plan = read_json(run_dir / "plan.json", {}) or {}
            if not state and not manifest:
                continue
            rows.append({
                "sid": sid,
                "kind": state.get("kind") or manifest.get("kind") or "",
                "title": state.get("title") or manifest.get("title") or plan.get("title") or sid,
                "status": state.get("status") or "unknown",
                "stage": state.get("current") or "",
                "started": state.get("started") or manifest.get("created") or "",
                "updated": state.get("updated") or "",
                "artifact": (manifest.get("artifact") or {}).get("path", ""),
                "report": (manifest.get("report") or {}).get("md", ""),
                "plan_steps": len(plan.get("steps") or []),
                "attempts": len(manifest.get("attempts") or []),
            })
        except (OSError, ValueError):
            continue
    rows.sort(key=lambda r: str(r.get("updated") or r.get("started") or ""), reverse=True)
    return rows[:max(1, int(limit or 50))]
