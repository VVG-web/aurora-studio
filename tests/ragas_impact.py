#!/usr/bin/env python3
"""ragas_impact.py - когда изменения требуют расширенного RAGAS-прогона.

Правило: изменения получения знаний для «Спросить» и «Продуктивности» проверяются
RAGAS на живом проекте. Чистый дизайн (скины, CSS, HTML, i18n) не требует прогона.
"""
from __future__ import annotations

import argparse
import subprocess

# Код, который меняет поиск, сборку пака, ответ или запись артефакта.
TRIGGERS = (
    "scripts/answer_core.py",
    "scripts/production_core.py",
    "scripts/agent_runner.py",
    "scripts/reliable_core.py",
    "scripts/ctx_pack.py",
    "scripts/quality_eval.py",
    "scripts/quality_golden.json",
    "scripts/quality_baseline.json",
    "scripts/kb_",
    "scripts/context_",
    "scripts/sources_",
    "cockpit/aurora_cockpit.py",
)
# Смешанные файлы: в них бывает и дизайн, и загрузка данных. Консервативно требуем RAGAS.
MANUAL = (
    "cockpit/modules/ask/view.js",
    "cockpit/modules/work/view.js",
)
DESIGN = (
    "cockpit/skins/",
    "cockpit/modules/ask/i18n/",
    "cockpit/modules/work/i18n/",
    "cockpit/modules/ask/view.html",
    "cockpit/modules/work/view.html",
)


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("./")


def classify(paths) -> dict:
    """{required, manual, reasons, design_only} по списку изменённых путей."""
    paths = [_norm(p) for p in paths if str(p).strip()]
    reasons = [p for p in paths if any(p == t or p.startswith(t) for t in TRIGGERS)]
    manual = [p for p in paths if any(p == t or p.startswith(t) for t in MANUAL)]
    if reasons:
        return {"required": True, "manual": manual, "reasons": reasons, "design_only": False}
    if manual:
        return {"required": True, "manual": manual, "reasons": manual, "design_only": False}
    design = [p for p in paths if any(p == t or p.startswith(t) for t in DESIGN)]
    return {"required": False, "manual": [], "reasons": [],
            "design_only": bool(paths) and len(design) == len(paths)}


def changed_paths(base: str = "origin/master") -> list[str]:
    """Пути рабочего дерева и коммитов от base; git недоступен - пусто."""
    out: list[str] = []
    for args in (["diff", "--name-only", base + "...HEAD"],
                 ["diff", "--name-only"],
                 ["diff", "--cached", "--name-only"]):
        try:
            cp = subprocess.run(["git", *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        if cp.returncode == 0:
            out.extend(cp.stdout.splitlines())
    return list(dict.fromkeys(_norm(p) for p in out if p.strip()))


def main() -> int:
    ap = argparse.ArgumentParser(description="Нужен ли расширенный RAGAS-прогон")
    ap.add_argument("--base", default="origin/master")
    ap.add_argument("--paths", nargs="*", default=None)
    a = ap.parse_args()
    paths = list(a.paths) if a.paths is not None else changed_paths(a.base)
    result = classify(paths)
    print(("RAGAS обязателен" if result["required"] else "RAGAS не требуется")
          + (": " + ", ".join(result["reasons"]) if result["reasons"] else ""))
    if result["manual"] and result["required"]:
        print("Проверьте diff: если в файле нет изменений загрузки данных, это дизайн.")
    return 1 if result["required"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
