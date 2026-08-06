#!/usr/bin/env python3
"""sync_audit.py — зеркала Sources/: целостность и дрейф (фреймворк «Аврора»).

Синк пишет состояние (`sync_state.md` у wiki, `update_log.md` у доски), но никто не
сверяет состояние с диском — и зеркало тихо расходится с реальностью. Скрипт делает
эту сверку машинно (на крупной базе ручной аудит показал 216 «пропавших» и 526
незарегистрированных файлов).

Что проверять, скрипт не знает заранее: список зеркал даёт реестр подключённых модулей
(`sources_registry.py`), а правила сверки — вид хранилища, объявленный модулем:
wiki (дерево страниц с номерами) или board (плоский список задач с ключами).

Что проверяется:
  MISSING     — страница/задача есть в состоянии синка, файла на диске нет
  ORPHAN      — файл на диске есть, в состоянии синка не зарегистрирован
  MOVED       — файл найден по page_id, но лежит не там, где записано состояние
  COLLISION   — один page_id зарегистрирован под разными путями (или путь — дважды)
  STALE       — состояние синка старше N дней (по умолчанию 14)

Запуск из корня проекта:
  python3 .opencode/scripts/sync_audit.py
  python3 .opencode/scripts/sync_audit.py --source Confluence
  python3 .opencode/scripts/sync_audit.py --stale-days 7 --report Artifacts/reports/2026-07-26_sync_audit.md
  python3 .opencode/scripts/sync_audit.py --json      # для панели

Ничего не меняет. Выход: 0 — расхождений нет; 1 — есть (нужен досинк или чистка).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime

import sources_registry as R
from aurora_common import (KB_ROOT, TRUSTED, frontmatter, git_guard, set_field,
                           split_frontmatter, walk_md)
from sources_core import ASSET_DIR_RE, SERVICE_RE, cited_by_cards, nfc

TODAY = date.today()

ROW_RE = re.compile(r"^\|\s*[^|]*\|\s*(\d{4,})\s*\|([^|]*)\|\s*([^|]+?)\s*\|\s*([A-Z_]+)?\s*\|")
JIRA_ROW_RE = re.compile(r"^\|\s*([A-Z][A-Z0-9]+-\d+)\s*\|([^|]*)\|\s*([^|]+?)\s*\|")
# page_id пишется в шапке зеркала (`page_id: 12345`); `- **ID:** 12345` — формат
# прежнего синк-скилла, он ещё встречается в старых проектах
ID_IN_FILE_RE = re.compile(r"^\s*(?:page_id:\s*|-\s*\*\*ID:\*\*\s*)(\d{4,})", re.M)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


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


def foreign_files(root: str) -> list:
    """Файлы зеркала, которые не страницы и не служебное: `.md_COLLISION`, `.bak`, копии.

    Зеркало — машинная выгрузка, и всё, что в нём не выгружено синком, — след прежних
    инструментов. Пока аудит смотрел только на `.md`, такой файл был невидим, а папка с
    ним читалась человеком как дубль каталога, переживший `--force --prune`.
    """
    out = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.startswith(".") or f.endswith(".md") or SERVICE_RE.search(f):
                continue
            rel = nfc(os.path.relpath(os.path.join(dirpath, f), root).replace("\\", "/"))
            if ASSET_DIR_RE.search(rel):
                continue      # схемы страницы — содержимое зеркала, а не чужой файл
            out.append(rel)
    return sorted(out)


def parse_wiki_state(root: str, state_name: str):
    """→ (записи с полным путём, записи только с page_id (путь обрезан), дата, нечитаемые строки).

    Синк иногда пишет путь сокращённо («.../Имя.md») — такая запись всё ещё пригодна:
    страницу можно найти на диске по page_id из тела файла.
    """
    state = os.path.join(root, state_name)
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


def parse_board_state(root: str, state_name: str):
    log = os.path.join(root, state_name)
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


def audit_wiki(src: dict, stale_days: int, out: list, stats: dict) -> int:
    """Дерево страниц: путь и номер страницы должны сходиться с состоянием синка."""
    root, state_name = src["path"], src["state"]
    if not os.path.isdir(root):
        out.append(f"- зеркала {src['id']} нет ({root}/) — пропущено\n")
        return 0
    rows, truncated, state_date, bad = parse_wiki_state(root, state_name)
    files = disk_files(root)
    out.append(f"## {src['id']} ({root})\n")
    if not rows and not truncated:
        if not files:
            out.append("- зеркало пустое, синк ещё не запускался — проверять нечего\n")
            return 0
        out.append(f"- **нет {state_name}** или он не разбирается: файлов на диске {len(files)}, "
                   "состояние синка неизвестно → аудит невозможен, зафиксируйте состояние синком.\n")
        stats[src["id"]] = {"kind": "wiki", "path": root, "no_state": True,
                            "files": len(files)}
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

    # Пути, различающиеся только регистром, — это не потеря страницы, а переименование
    # в источнике, которое не доехало до диска: файловая система macOS и Windows к
    # регистру нечувствительна и оставляет папку под старым именем. Показывать это
    # как MISSING и ORPHAN одновременно — врать про потерю.
    by_ci = {rel.casefold(): rel for rel in orphans}
    recase = []
    for pid, path in list(missing):
        hit = by_ci.get(path.casefold())
        if hit:
            recase.append((pid, hit, path))
            missing.remove((pid, path))
            orphans.remove(hit)
            del by_ci[path.casefold()]
    # то же самое, но найденное по page_id: MOVED сказал бы «перепишите состояние»,
    # хотя переписывать нужно не состояние, а регистр папки на диске
    for pid, path, on_disk in list(moved):
        if path.casefold() == on_disk.casefold():
            recase.append((pid, on_disk, path))
            moved.remove((pid, path, on_disk))

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
    foreign = foreign_files(root)
    out.append(f"- MISSING: **{len(missing)}** · MOVED: **{len(moved)}** · ORPHAN: **{len(orphans)}** "
               f"· CASE: **{len(recase)}** · COLLISION: **{len(collisions)}** "
               f"· ПОСТОРОННИЕ: **{len(foreign)}**\n")
    stats[src["id"]] = {"kind": "wiki", "path": root, "missing": len(missing),
                        "orphan": len(orphans), "moved": len(moved), "case": len(recase),
                        "collision": len(collisions), "foreign": len(foreign),
                        "state_date": state_date, "age_days": days_since(state_date)}

    problems = (len(missing) + len(orphans) + len(collisions) + len(moved) + len(recase)
                + len(foreign) + bad)
    if foreign:
        out.append(f"### ПОСТОРОННИЕ — файлы, которых синк не выгружал ({len(foreign)})\n")
        out.append("Следы прежних инструментов: `.md_COLLISION`, `.bak`, копии. Зеркало — "
                   "машинная выгрузка, такому в нём не место; папка с ними читается как "
                   "дубль каталога. Убрать: `sync:confluence --prune`.\n")
        for f in foreign[:20]:
            out.append(f"- `{f}`")
        if len(foreign) > 20:
            out.append(f"- … ещё {len(foreign) - 20}")
        out.append("")
    if recase:
        out.append(f"### CASE — путь отличается только регистром ({len(recase)})\n")
        out.append("Страницу переименовали в источнике, а файловая система оставила папку "
                   "под старым именем. Страница на месте, потери нет.\n")
        for pid, on_disk, in_state in recase[:20]:
            out.append(f"- {pid} · на диске `{on_disk}` · в состоянии `{in_state}`")
        if len(recase) > 20:
            out.append(f"- … ещё {len(recase) - 20}")
        out.append("")
    if age is not None and age > stale_days:
        out.append(f"⚠️ STALE: состояние синка старше {stale_days} дней — "
                   f"запустите `{src['command'] or 'синк'}`.\n")
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


def audit_board(src: dict, stale_days: int, out: list, stats: dict) -> int:
    """Плоская доска: ключ задачи — имя файла, состояние — список ключей."""
    root, state_name = src["path"], src["state"]
    if not os.path.isdir(root):
        out.append(f"- зеркала {src['id']} нет ({root}/) — пропущено\n")
        return 0
    rows, latest = parse_board_state(root, state_name)
    files = disk_files(root)
    keys_on_disk = {os.path.splitext(os.path.basename(rel))[0].upper(): rel for rel in files}
    out.append(f"## {src['id']} ({root})\n")
    if not rows:
        if not files:
            out.append("- зеркало пустое, экспорт ещё не запускался — проверять нечего\n")
            return 0
        out.append(f"- **нет {state_name}** или он пуст: файлов на диске {len(files)} — "
                   "состояние синка не ведётся.\n")
        stats[src["id"]] = {"kind": "board", "path": root, "no_state": True,
                            "files": len(files)}
        return 1

    logged = {k.upper(): d for k, d in rows}
    missing = sorted(k for k in logged if k not in keys_on_disk)
    orphans = sorted(k for k in keys_on_disk if k not in logged)
    age = days_since(latest)
    out.append(f"- в логе задач: {len(logged)} · файлов на диске: {len(files)}")
    out.append(f"- последний синк: {latest or '—'}" + (f" ({age} дн. назад)" if age is not None else ""))
    out.append(f"- MISSING: **{len(missing)}** · ORPHAN: **{len(orphans)}**\n")
    stats[src["id"]] = {"kind": "board", "path": root, "missing": len(missing),
                        "orphan": len(orphans), "state_date": latest,
                        "age_days": age}
    # Сирота, на которую ссылается карточка, и сирота, о которой все забыли, — разные
    # случаи: первую нельзя просто удалить, и отчёт обязан это различать, иначе человек
    # гадает, почему `--prune` отработал, а расхождение осталось.
    cited = cited_by_cards(root, [keys_on_disk[k] for k in orphans])
    if cited:
        out.append(f"- из них на **{len(cited)}** ссылаются карточки (`source:`) — поэтому "
                   f"`{src['command'] or 'синк'} --prune` их и не удалил: это оборвало бы провенанс.")
        out.append("  Сначала перенацелить ссылки: `kb:remap-sources --mirror "
                   f"{root}`, потом повторить prune.\n")
    problems = len(missing) + len(orphans)
    if age is not None and age > stale_days:
        out.append(f"⚠️ STALE: лог синка старше {stale_days} дней — "
                   f"запустите `{src['command'] or 'синк'}`.\n")
        problems += 1
    if missing:
        out.append(f"### MISSING ({len(missing)})\n" + ", ".join(missing[:60]) + "\n")
    if orphans:
        out.append(f"### ORPHAN ({len(orphans)})\n" + ", ".join(orphans[:60]) + "\n")
    return problems


AUDITORS = {"wiki": audit_wiki, "board": audit_board}


def file_hash(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def drift_collect(only_trusted: bool) -> tuple:
    """→ (дрейф, без хеша, битые источники, всего проверено)."""
    drift, unstamped, broken, total = [], [], [], 0
    for path in walk_md(KB_ROOT, skip_service=True, skip_archive=True):
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        fm = frontmatter(text)
        src = (fm.get("source") or "").strip()
        status = (fm.get("status") or "").strip()
        if not src or src.startswith("http") or "/" not in src:
            continue
        if only_trusted and status not in TRUSTED:
            continue
        total += 1
        if not os.path.isfile(src):
            broken.append((path, src, status))
            continue
        actual = file_hash(src)
        recorded = (fm.get("source_hash") or "").strip()
        if not recorded:
            unstamped.append((path, src, actual, status))
        elif recorded != actual:
            drift.append((path, src, status, fm.get("owner", "—"), fm.get("verified", "—")))
    return drift, unstamped, broken, total


def stamp(unstamped: list, apply: bool) -> int:
    done = 0
    for path, _src, actual, _status in unstamped:
        text = open(path, encoding="utf-8").read()
        head, rest = split_frontmatter(text)
        if head is None:
            continue
        new = "---" + set_field(set_field(head, "source_hash", actual),
                                "source_synced", TODAY.isoformat()) + rest
        done += 1
        if apply:
            open(path, "w", encoding="utf-8").write(new)
    return done




def drift_report(a) -> tuple:
    """Дрейф: источник изменился после того, как знание проверили.

    Инвариант 3: синк не перезаписывает проверенное — значит после каждого синка
    остаётся вопрос, какие `verified` карточки построены на страницах, которые с тех
    пор изменились. Сравнение хеша — механика; решение «перепроверить, заменить,
    признать несущественным» — человека.
    """
    drift, unstamped, broken, total = drift_collect(not a.all)
    scope = "все карточки" if a.all else "только verified"
    L = [f"# Дрейф источников — {TODAY.isoformat()}", "",
         f"Проверено карточек с источником: {total} ({scope})", "",
         f"- **дрейф** (источник изменился после сверки): **{len(drift)}**",
         f"- без `source_hash` (сравнивать не с чем): {len(unstamped)}",
         f"- битый `source` (файла нет): {len(broken)}", ""]

    if drift:
        L += ["## Дрейф — перепроверить\n",
              "Источник изменился, карточка осталась прежней. Решает владелец: перепроверить"
              " и обновить `verified`, заменить знание через `kb:supersede` или признать"
              " изменение несущественным (тогда `--stamp`).", "",
              "| Карточка | Статус | Владелец | Проверено | Источник |", "|---|---|---|---|---|"]
        for path, src, status, owner, ver in sorted(drift, key=lambda x: x[3]):
            L.append(f"| {os.path.basename(path)[:-3]} | {status} | {owner} | {ver} | {src[:70]} |")
        L.append("")
    if broken:
        L += [f"## Битые источники ({len(broken)})\n",
              "Страницы больше нет в зеркале: удалена, переименована или вне синкаемых корней.",
              "Перенацелить — `kit:remap-sources`; если исчезла совсем — деприкейтнуть карточку.", ""]
        for path, src, status in broken[:30]:
            L.append(f"- {os.path.basename(path)[:-3]} ({status}) → `{src}`")
        if len(broken) > 30:
            L.append(f"- … ещё {len(broken) - 30}")
        L.append("")
    if unstamped and not a.stamp:
        L += [f"## Без `source_hash` ({len(unstamped)})\n",
              "Дрейф у них не обнаружить. Зафиксировать текущее состояние: "
              "`sync:diff --stamp --apply` — но только после того, как карточки проверены.", ""]

    return "\n".join(L), drift, unstamped


def main() -> int:
    ap = argparse.ArgumentParser(description="Аудит целостности зеркал Sources/")
    ap.add_argument("--stale-days", type=int, default=14, help="через сколько дней синк считается протухшим")
    ap.add_argument("--report", metavar="PATH", help="сохранить отчёт в файл")
    ap.add_argument("--source", metavar="ID", action="append",
                    help="проверять только это зеркало (id из aurora.config.yaml → sources)")
    ap.add_argument("--json", action="store_true", help="машиночитаемый итог (для панели)")
    ap.add_argument("--drift", action="store_true",
                    help="дрейф: источник изменился после того, как знание проверили")
    ap.add_argument("--all", action="store_true",
                    help="дрейф по всем карточкам, а не только verified")
    ap.add_argument("--stamp", action="store_true",
                    help="проставить source_hash там, где его нет (с --apply)")
    ap.add_argument("--apply", action="store_true", help="записать (для --stamp)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="писать по незакоммиченному дереву (откат станет невозможным)")
    ap.add_argument("--confluence-only", action="store_true",
                    help="то же, что --source Confluence (оставлено для совместимости)")
    ap.add_argument("--jira-only", action="store_true",
                    help="то же, что --source JIRA (оставлено для совместимости)")
    a = ap.parse_args()

    if a.drift or a.stamp:
        if not os.path.isdir(KB_ROOT):
            print(f"sync_audit: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
            return 1
        text, drift, unstamped = drift_report(a)
        print(text)
        if a.report:
            os.makedirs(os.path.dirname(a.report) or ".", exist_ok=True)
            open(a.report, "w", encoding="utf-8").write(text + "\n")
            print(f"\nОтчёт: {a.report}")
        if a.stamp:
            if a.apply and not git_guard(KB_ROOT, a.allow_dirty, "простановка source_hash"):
                return 2
            n = stamp(unstamped, a.apply)
            print(f"\n{'✅ Проставлено' if a.apply else '(dry-run) К простановке'}: {n} карточек")
            if not a.apply:
                print("Повторите с --apply.")
        return 1 if drift else 0

    if not os.path.isdir("Sources"):
        print("sync_audit: нет папки Sources/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    only = list(a.source or [])
    only += ["Confluence"] if a.confluence_only else []
    only += ["JIRA"] if a.jira_only else []
    sources = [s for s in R.instances() if not only or s["id"] in only]

    out = [f"# Аудит зеркал Sources/ — {TODAY.isoformat()}", ""]
    problems, stats = 0, {}
    if not sources:
        out.append("- подключённых зеркал нет: секция `sources:` в aurora.config.yaml пуста "
                   "или модули не установлены (`sources_registry.py`)\n")
    for src in sources:
        auditor = AUDITORS.get(src["kind"])
        if not auditor:
            out.append(f"## {src['id']} ({src['path']})\n")
            out.append(f"- модуль `{src['module']}` не установлен — правила проверки неизвестны, "
                       "зеркало пропущено\n")
            problems += 1
            continue
        problems += auditor(src, a.stale_days, out, stats)

    if a.json:
        print(json.dumps({"problems": problems, "mirrors": stats}, ensure_ascii=False))
        return 1 if problems else 0

    out += ["## Что делать", "",
            "- MISSING → досинхронизировать страницы/задачи (синк по page_id/ключу);",
            "- ORPHAN → либо страница переименована/удалена в источнике (тогда удалить зеркало",
            "  и проверить карточки, чьи `source` на неё ссылались), либо синк не записал состояние;",
            "- MOVED → перезаписать состояние синка (файл на месте, запись устарела);",
            "- CASE → повторить синк: с 1.17.2 он сам выправляет регистр папок;",
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
