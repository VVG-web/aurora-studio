#!/usr/bin/env python3
"""report_analyst.py — собрать дашборд эффективности аналитиков.

Цепочка: выгрузка из Jira и Confluence → метрики → HTML. Всё проектное
(имя, год, адреса, ростер, события) берётся из `aurora.config.yaml`; сами шаги
цепочки лежат в `.opencode/reports/analyst/`.

  python3 .opencode/scripts/report_analyst.py                # выгрузить и собрать
  python3 .opencode/scripts/report_analyst.py --skip-fetch   # по уже выгруженному
  python3 .opencode/scripts/report_analyst.py --serve        # и открыть дашборд

Токены — в `.env.aurora.local` (он в .gitignore): JIRA_PERSONAL_TOKEN,
CONFLUENCE_PAT. В сам конфиг они не пишутся.

Панель: `ops:report`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# Шаги цепочки лежат рядом: в ките — reports/analyst/, в проекте — .opencode/reports/analyst/
HERE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "reports", "analyst")
sys.path.insert(0, HERE)
import paths

# Выгрузка (нужна сеть и токен) — один раз на все годы: сырьё от отчётного года
# не зависит, и менять период не должно стоить нового похода в Jira.
FETCH = ["fetch_issues.py", "fetch_subtasks.py", "fetch_full.py",
         "fetch_confluence_metadata.py"]
# Счёт — по разу на каждый год.
PER_YEAR = ["process_confluence.py", "make_analyst_metrics.py",
            "update_analyst_metrics.py", "verify_weekly_by_person.py"]
# Сборка HTML — один раз: она складывает в один файл все посчитанные годы.
ASSEMBLE = ["make_extended.py"]

# Что должно лежать в кэше, чтобы сборка без выгрузки имела смысл. Без этой проверки
# `--skip-fetch` на пустом кэше падает трассировкой из середины чужого скрипта.
NEEDED = {"issues.json": "выгрузка задач Jira",
          "full_status.json": "история статусов Jira",
          "confluence_raw_metadata.json": "страницы Confluence"}


def env_missing() -> list:
    """Токены берём из окружения и из `.env.aurora.local` — как остальной движок."""
    env = dict(os.environ)
    local = os.path.join(paths.PROJECT_ROOT, ".env.aurora.local")
    if os.path.isfile(local):
        for line in open(local, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    os.environ.update(env)
    need = {"JIRA_PERSONAL_TOKEN": "Jira",
            "CONFLUENCE_PAT|CONFLUENCE_PERSONAL_TOKEN": "Confluence"}
    return [f"{names} (доступ к {what})" for names, what in need.items()
            if not any(env.get(n) for n in names.split("|"))]


def run(script: str, step: str, year: int | None = None) -> bool:
    print(f"\n{step} {script}" + (f" · {year}" if year else ""))
    env = dict(os.environ)
    if year:
        env["AURORA_REPORT_YEAR"] = str(year)
    else:
        env.pop("AURORA_REPORT_YEAR", None)
    rc = subprocess.run([sys.executable, os.path.join(HERE, script)],
                        cwd=paths.PROJECT_ROOT, env=env).returncode
    if rc != 0:
        print(f"❌ {script} завершился с кодом {rc}", file=sys.stderr)
        return False
    return True


def years_in_data() -> list:
    """Годы, в которых на проекте вообще что-то происходило.

    Проект живёт дольше календарного года, и какие именно годы у него рабочие,
    движок знать не может — но может посмотреть. Берём годы из истории статусов
    Jira и дат создания страниц Confluence; конфиг, если в нём объявлены `years:`,
    эту находку переопределяет.
    """
    import json
    seen = set()
    fs = paths.data("full_status.json")
    if os.path.isfile(fs):
        for issue in json.load(open(fs, encoding="utf-8")).values():
            for tr in issue.get("status_history", []):
                at = (tr.get("at") or "")[:4]
                if at.isdigit():
                    seen.add(int(at))
    cm = paths.data("confluence_raw_metadata.json")
    if os.path.isfile(cm):
        for page in json.load(open(cm, encoding="utf-8")).get("pages", []):
            for field in ("created", "updated"):
                at = (page.get(field) or "")[:4]
                if at.isdigit():
                    seen.add(int(at))
    return sorted(seen)


def main() -> int:
    ap = argparse.ArgumentParser(description="Дашборд эффективности аналитиков")
    ap.add_argument("--skip-fetch", action="store_true",
                    help="не ходить в Jira и Confluence, считать по выгруженному")
    ap.add_argument("--serve", action="store_true",
                    help="открыть собранный дашборд в браузере")
    a = ap.parse_args()

    if not os.path.isfile(paths.CONFIG_PATH):
        print("ops:report: нет aurora.config.yaml — запускать надо из корня проекта",
              file=sys.stderr)
        return 1

    if not os.path.isfile(paths.ROSTER_PATH):
        print(f"⚠️  нет ростера {os.path.relpath(paths.ROSTER_PATH, paths.PROJECT_ROOT)} — "
              "роли в отчёте будут пустыми")

    paths.ensure_dirs()

    if a.skip_fetch:
        missing = [f"{f} ({what})" for f, what in NEEDED.items()
                   if not os.path.isfile(paths.data(f))]
        if missing:
            print("ops:report: в кэше нет данных, по которым считать:", file=sys.stderr)
            for m in missing:
                print(f"   — {m}", file=sys.stderr)
            print("   Уберите --skip-fetch, чтобы выгрузить их.", file=sys.stderr)
            return 1
    else:
        missing = env_missing()
        if missing:
            print("ops:report: нет доступов для выгрузки:", file=sys.stderr)
            for m in missing:
                print(f"   — {m}", file=sys.stderr)
            print("   Положите их в .env.aurora.local либо соберите по уже выгруженному:"
                  "\n   python3 .opencode/scripts/report_analyst.py --skip-fetch", file=sys.stderr)
            return 1

    fetch = [] if a.skip_fetch else FETCH
    # Выгрузку делаем до определения годов: по чему их искать, знает только сырьё.
    for i, script in enumerate(fetch, 1):
        if not run(script, f"[выгрузка {i}/{len(fetch)}]"):
            return 1

    years = paths.configured_years() or years_in_data()
    if not years:
        print("ops:report: в выгрузках нет ни одной датированной записи — считать нечего",
              file=sys.stderr)
        return 1

    print(f"\nОтчёт по аналитикам · проект {paths.PROJECT_NAME} · "
          f"{'годы ' + ', '.join(map(str, years)) if len(years) > 1 else str(years[0]) + ' год'}")

    total = len(years) * len(PER_YEAR) + len(ASSEMBLE)
    n = 0
    for y in years:
        for script in PER_YEAR:
            n += 1
            if not run(script, f"[{n}/{total}]", year=y):
                return 1
    for script in ASSEMBLE:
        n += 1
        if not run(script, f"[{n}/{total}]"):
            return 1

    print(f"\n✅ Готово: {os.path.relpath(paths.OUTPUT_PATH, paths.PROJECT_ROOT)}")
    if a.serve:
        subprocess.run([sys.executable, os.path.join(HERE, "serve_dashboard.py"), "--html"],
                       cwd=paths.PROJECT_ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
