#!/usr/bin/env python3
"""sync_audit.py — целостность зеркал Sources/ (фреймворк «Аврора»).

Синк пишет состояние (`Sources/Confluence/sync_state.md`, `Sources/JIRA/update_log.md`),
но никто не сверяет состояние с диском — и зеркало тихо расходится с реальностью.
Скрипт делает эту сверку машинно (на крупной базе ручной аудит показал 216 «пропавших» и 526
незарегистрированных файлов).

Что проверяется:
  MISSING     — страница/задача есть в состоянии синка, файла на диске нет
  ORPHAN      — файл на диске есть, в состоянии синка не зарегистрирован
  MOVED       — файл найден по page_id, но лежит не там, где записано состояние
  COLLISION   — один page_id зарегистрирован под разными путями (или путь — дважды)
  STALE       — состояние синка старше N дней (по умолчанию 14)

Запуск из корня проекта:
  python3 .opencode/scripts/sync_audit.py
  python3 .opencode/scripts/sync_audit.py --stale-days 7 --report Artifacts/reports/2026-07-26_sync_audit.md

Ничего не меняет. Выход: 0 — расхождений нет; 1 — есть (нужен досинк или чистка).
"""
from __future__ import annotations

import argparse
import os
import re
import unicodedata
import sys
from datetime import date, datetime

CONF_DIR = "Sources/Confluence"
JIRA_DIR = "Sources/JIRA"
TODAY = date.today()

# Служебные файлы синка — не страницы.
SERVICE_RE = re.compile(
    r"(sync_state|update_log|sync_paths|_prompt|_template|-rules|_rules|SYNC_|FINAL_SYNC|README)",
    re.I)
ROW_RE = re.compile(r"^\|\s*[^|]*\|\s*(\d{4,})\s*\|([^|]*)\|\s*([^|]+?)\s*\|\s*([A-Z_]+)?\s*\|")
JIRA_ROW_RE = re.compile(r"^\|\s*([A-Z][A-Z0-9]+-\d+)\s*\|([^|]*)\|\s*([^|]+?)\s*\|")
ID_IN_FILE_RE = re.compile(r"^\s*-\s*\*\*ID:\*\*\s*(\d{4,})", re.M)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def read_config_paths() -> tuple:
    conf, jira = CONF_DIR, JIRA_DIR
    cfg = "aurora.config.yaml"
    if os.path.isfile(cfg):
        text = open(cfg, encoding="utf-8", errors="ignore").read()
        m = re.search(r"^\s*sources_confluence:\s*(\S+)\s*$", text, re.M)
        if m:
            conf = m.group(1).strip('"\'')
        m = re.search(r"^\s*sources_jira:\s*(\S+)\s*$", text, re.M)
        if m:
            jira = m.group(1).strip('"\'')
    return conf, jira


def nfc(path: str) -> str:
    """Пути в единую нормализацию Unicode.

    macOS отдаёт имена файлов в NFD («и» + диакритика раздельно), а состояние синка
    писалось откуда придётся — та же самая страница выглядит и как MISSING, и как ORPHAN
    одновременно. Сравнивать пути без нормализации на macOS нельзя.
    """
    return unicodedata.normalize("NFC", path)


def disk_files(root: str) -> dict:
    """{относительный путь: полный путь} для всех .md, кроме служебных файлов синка."""
    out = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            if not f.endswith(".md") or SERVICE_RE.search(f):
                continue
            full = os.path.join(dirpath, f).replace("\\", "/")
            out[nfc(os.path.relpath(full, root).replace("\\", "/"))] = full
    return out


def parse_confluence_state(root: str):
    """→ (записи с полным путём, записи только с page_id (путь обрезан), дата, нечитаемые строки).

    Синк иногда пишет путь сокращённо («.../Имя.md») — такая запись всё ещё пригодна:
    страницу можно найти на диске по page_id из тела файла.
    """
    state = os.path.join(root, "sync_state.md")
    if not os.path.isfile(state):
        return [], [], None, 0
    rows, truncated, bad = [], [], 0
    text = open(state, encoding="utf-8", errors="ignore").read()
    m = DATE_RE.search(text[:400])
    state_date = m.group(1) if m else None
    for line in text.splitlines():
        if not line.startswith("|") or line.startswith("|---") or "Page ID" in line:
            continue
        m = ROW_RE.match(line)
        if not m:
            if re.match(r"^\|\s*\d+\s*\|", line):
                bad += 1
            continue
        page_id, _title, path, status = m.group(1), m.group(2), m.group(3).strip(), (m.group(4) or "")
        path = nfc(path.replace("\\", "/"))
        if not path.endswith(".md"):
            bad += 1
        elif path.startswith(".../") or "/.../" in path:
            truncated.append((page_id, path))
        else:
            rows.append((page_id, path, status))
    return rows, truncated, state_date, bad


def parse_jira_log(root: str):
    log = os.path.join(root, "update_log.md")
    if not os.path.isfile(log):
        return [], None
    rows, latest = [], None
    for line in open(log, encoding="utf-8", errors="ignore"):
        m = JIRA_ROW_RE.match(line)
        if not m:
            continue
        key = m.group(1)
        # дату ищем во всей строке: у прежнего LLM-лога и у нового состояния разные колонки
        d = DATE_RE.search(line)
        if d:
            latest = max(latest or d.group(1), d.group(1))
        rows.append((key, d.group(1) if d else "—"))
    return rows, latest


def days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (TODAY - datetime.strptime(iso, "%Y-%m-%d").date()).days
    except ValueError:
        return None


def audit_confluence(root: str, stale_days: int, out: list) -> int:
    if not os.path.isdir(root):
        out.append(f"- зеркала Confluence нет ({root}/) — пропущено\n")
        return 0
    rows, truncated, state_date, bad = parse_confluence_state(root)
    files = disk_files(root)
    out.append(f"## Confluence ({root})\n")
    if not rows and not truncated:
        if not files:
            out.append("- зеркало пустое, синк ещё не запускался — проверять нечего\n")
            return 0
        out.append(f"- **нет sync_state.md** или он не разбирается: файлов на диске {len(files)}, "
                   "состояние синка неизвестно → аудит невозможен, зафиксируйте состояние синком.\n")
        return 1

    by_path, by_id = {}, {}
    collisions = []
    for page_id, path, _status in rows:
        if path in by_path and by_path[path] != page_id:
            collisions.append(f"путь `{path}` зарегистрирован за page_id {by_path[path]} и {page_id}")
        by_path[path] = page_id
        by_id.setdefault(page_id, set()).add(path)
    for pid, paths in by_id.items():
        if len(paths) > 1:
            collisions.append(f"page_id {pid} → " + ", ".join(f"`{p}`" for p in sorted(paths)))

    ids_on_disk = {}
    for rel, full in files.items():
        try:
            m = ID_IN_FILE_RE.search(open(full, encoding="utf-8", errors="ignore").read(4000))
        except Exception:
            m = None
        if m:
            ids_on_disk.setdefault(m.group(1), []).append(rel)

    missing, moved, claimed = [], [], set()
    for path, pid in sorted(by_path.items()):
        if path in files:
            claimed.add(path)
            continue
        elsewhere = ids_on_disk.get(pid)
        if elsewhere:
            moved.append((pid, path, elsewhere[0]))
            claimed.update(elsewhere)
        else:
            missing.append((pid, path))
    for pid, path in truncated:
        elsewhere = ids_on_disk.get(pid)
        if elsewhere:
            claimed.update(elsewhere)
        else:
            missing.append((pid, path + "  (путь в состоянии обрезан)"))
    orphans = sorted(rel for rel in files if rel not in claimed)

    age = days_since(state_date)
    out.append(f"- зарегистрировано записей: {len(rows) + len(truncated)} "
               f"(полных путей {len(by_path)}, обрезанных — резолв по page_id: {len(truncated)}), "
               f"файлов на диске: {len(files)}")
    out.append(f"- дата состояния: {state_date or '—'}" + (f" ({age} дн. назад)" if age is not None else ""))
    if bad:
        out.append(f"- строк состояния, которые не разобрались: **{bad}**")
    if truncated:
        out.append(f"- ⚠️ синк пишет обрезанные пути ({len(truncated)} строк) — состояние теряет "
                   "проверяемость; синк-скилл должен писать полный путь от корня зеркала")
    out.append(f"- MISSING: **{len(missing)}** · MOVED: **{len(moved)}** · ORPHAN: **{len(orphans)}** "
               f"· COLLISION: **{len(collisions)}**\n")

    problems = len(missing) + len(orphans) + len(collisions) + len(moved) + bad
    if age is not None and age > stale_days:
        out.append(f"⚠️ STALE: состояние синка старше {stale_days} дней — запустите `sync:confluence`.\n")
        problems += 1
    if missing:
        out.append(f"### MISSING — в состоянии есть, на диске нет ({len(missing)})\n")
        for pid, path in missing[:40]:
            out.append(f"- {pid} · `{path}`")
        if len(missing) > 40:
            out.append(f"- … ещё {len(missing) - 40}")
        out.append("")
    if moved:
        out.append(f"### MOVED — файл найден по page_id в другом месте ({len(moved)})\n")
        for pid, was, now in moved[:40]:
            out.append(f"- {pid}: состояние `{was}` → диск `{now}`")
        if len(moved) > 40:
            out.append(f"- … ещё {len(moved) - 40}")
        out.append("")
    if orphans:
        out.append(f"### ORPHAN — на диске есть, в состоянии нет ({len(orphans)})\n")
        for rel in orphans[:40]:
            out.append(f"- `{rel}`")
        if len(orphans) > 40:
            out.append(f"- … ещё {len(orphans) - 40}")
        out.append("")
    if collisions:
        out.append(f"### COLLISION ({len(collisions)})\n")
        for c in collisions[:40]:
            out.append(f"- {c}")
        out.append("")
    return problems


def audit_jira(root: str, stale_days: int, out: list) -> int:
    if not os.path.isdir(root):
        out.append(f"- зеркала Jira нет ({root}/) — пропущено\n")
        return 0
    rows, latest = parse_jira_log(root)
    files = disk_files(root)
    keys_on_disk = {os.path.splitext(os.path.basename(rel))[0].upper(): rel for rel in files}
    out.append(f"## Jira ({root})\n")
    if not rows:
        if not files:
            out.append("- зеркало пустое, экспорт ещё не запускался — проверять нечего\n")
            return 0
        out.append(f"- **нет update_log.md** или он пуст: файлов на диске {len(files)} — "
                   "состояние синка не ведётся.\n")
        return 1

    logged = {k.upper(): d for k, d in rows}
    missing = sorted(k for k in logged if k not in keys_on_disk)
    orphans = sorted(k for k in keys_on_disk if k not in logged)
    age = days_since(latest)
    out.append(f"- в логе задач: {len(logged)} · файлов на диске: {len(files)}")
    out.append(f"- последний синк: {latest or '—'}" + (f" ({age} дн. назад)" if age is not None else ""))
    out.append(f"- MISSING: **{len(missing)}** · ORPHAN: **{len(orphans)}**\n")
    problems = len(missing) + len(orphans)
    if age is not None and age > stale_days:
        out.append(f"⚠️ STALE: лог синка старше {stale_days} дней — запустите `sync:jira`.\n")
        problems += 1
    if missing:
        out.append(f"### MISSING ({len(missing)})\n" + ", ".join(missing[:60]) + "\n")
    if orphans:
        out.append(f"### ORPHAN ({len(orphans)})\n" + ", ".join(orphans[:60]) + "\n")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Аудит целостности зеркал Sources/")
    ap.add_argument("--stale-days", type=int, default=14, help="через сколько дней синк считается протухшим")
    ap.add_argument("--report", metavar="PATH", help="сохранить отчёт в файл")
    ap.add_argument("--confluence-only", action="store_true",
                    help="проверять только зеркало Confluence")
    ap.add_argument("--jira-only", action="store_true",
                    help="проверять только зеркало Jira")
    a = ap.parse_args()

    conf_dir, jira_dir = read_config_paths()
    if not os.path.isdir("Sources"):
        print("sync_audit: нет папки Sources/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    out = [f"# Аудит зеркал Sources/ — {TODAY.isoformat()}", ""]
    problems = 0
    if not a.jira_only:
        problems += audit_confluence(conf_dir, a.stale_days, out)
    if not a.confluence_only:
        problems += audit_jira(jira_dir, a.stale_days, out)

    out += ["## Что делать", "",
            "- MISSING → досинхронизировать страницы/задачи (синк по page_id/ключу);",
            "- ORPHAN → либо страница переименована/удалена в источнике (тогда удалить зеркало",
            "  и проверить карточки, чьи `source` на неё ссылались), либо синк не записал состояние;",
            "- MOVED → перезаписать состояние синка (файл на месте, запись устарела);",
            "- COLLISION → страницы с именами, различающимися только регистром/гомоглифами:",
            "  переименовать зеркало и прогнать `kb_fix.py --links`.", ""]
    report = "\n".join(out)
    print(report)
    if a.report:
        os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
        with open(a.report, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"Отчёт: {a.report}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
