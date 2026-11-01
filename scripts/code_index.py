#!/usr/bin/env python3
"""code_index.py — индекс кода проекта: инвентарь репозиториев в `Code/`.

Команда читает только git-объекты клона и складывает инвентарь в
`.aurora/cache/code/<имя>/index.sqlite`. Это кэш, а не знание: его можно удалить и построить
заново. Модели и сеть не нужны; содержимое файлов не хранится, секреты маскируются.

  python3 .aurora/scripts/code_index.py --index                    # план: что будет проиндексировано
  python3 .aurora/scripts/code_index.py --index --apply            # построить или обновить индекс
  python3 .aurora/scripts/code_index.py --index --apply --rebuild  # удалить и построить заново
  python3 .aurora/scripts/code_index.py --status [--json]          # ревизия, отставание, состав
  python3 .aurora/scripts/code_index.py --secrets [--json]         # находки секретов без значений

Без `--apply` команда ничего не пишет. Коды возврата: 0 — всё построено, 1 — часть
репозиториев не проиндексирована, 2 — отказ или сбой (нет git, нет клона, предел файлов).

Панель: `code:index`, `code:status`, `code:secrets`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import code_repos as CREPO  # noqa: E402
import codeidx.model as model  # noqa: E402
import codeidx.scan as scan  # noqa: E402
import codeidx.store as store_mod  # noqa: E402
from aurora_common import engine_dir, lock_release, lock_take  # noqa: E402

EXIT_OK, EXIT_PART, EXIT_FAIL = 0, 1, 2


def index_dir(project, name: str) -> Path:
    return Path(project) / engine_dir(project) / "cache" / "code" / name


def index_path(project, name: str) -> Path:
    return index_dir(project, name) / "index.sqlite"


def _remove_index(path: Path) -> None:
    """Удалить индекс и его спутники WAL: иначе старая база возвращается из журнала."""
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink()
        except OSError:
            pass


def lock_path(project) -> Path:
    return Path(project) / engine_dir(project) / "state" / "code-index.lock"


def _say(*a) -> None:
    print(*a, flush=True)


def _fmt_int(n) -> str:
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(n)


def selected(project, only: str) -> tuple:
    """(чистый реестр, имена, замечания, неизвестное имя)."""
    clean, probs = CREPO.check(CREPO.read_repos(project))
    names = list(clean)
    if only:
        hit = CREPO.find(clean, only)
        if not hit:
            return clean, [], probs, only
        names = [hit]
    return clean, names, probs, ""


def plan_repo(project, name: str, rec: dict) -> dict:
    """План без записи: ревизия, состав, наличие индекса."""
    clone = CREPO.clone_path(project, name)
    out = {"name": name, "clone": str(clone), "ok": False, "rev": "", "files": 0, "index": ""}
    if not clone.is_dir():
        out["problem"] = "клона нет: выполните code:sync"
        return out
    try:
        repo = scan.GitRepo(clone)
        out["rev"] = repo.head()
        entries = repo.ls_tree(out["rev"])
    except scan.GitError as e:
        out["problem"] = str(e)
        return out
    out["files"] = len(entries)
    out["index"] = "есть" if index_path(project, name).is_file() else "нет"
    out["ok"] = True
    return out


def run_plan(project, clean: dict, names: list, as_json: bool) -> int:
    rows = [plan_repo(project, n, clean[n]) for n in names]
    if as_json:
        _say(json.dumps({"repos": rows}, ensure_ascii=False))
        return EXIT_OK if all(r["ok"] for r in rows) else EXIT_PART
    _say(f"code:index — план, репозиториев {len(rows)}:")
    for r in rows:
        if not r["ok"]:
            _say(f"✗ {r['name']}: {r.get('problem', 'не проиндексирован')}")
        else:
            _say(f"· {r['name']}: ревизия {r['rev'][:8]}, файлов {_fmt_int(r['files'])}, индекс {r['index']}")
    _say("Ничего не записано: это план. Построить индекс — с --apply.")
    return EXIT_OK if all(r["ok"] for r in rows) else EXIT_PART


def _summary_line(name: str, stats: model.ScanStats) -> str:
    speed = stats.parsed / stats.duration_s if stats.duration_s > 0 else 0
    parts = [f"✓ {name}: ревизия {stats.rev[:8]}",
             f"файлов {_fmt_int(stats.files)}",
             f"в индексе {_fmt_int(stats.indexed)}",
             f"прочитано {_fmt_int(stats.parsed)}",
             f"из кэша {_fmt_int(stats.reused)}",
             f"{stats.duration_s:.2f} с",
             f"{speed:.0f} файлов/с"]
    if stats.secrets:
        parts.append(f"секретов {_fmt_int(stats.secrets)}")
    if stats.skipped:
        parts.append("пропущено: " + ", ".join(f"{k} {v}" for k, v in sorted(stats.skipped.items())))
    return ", ".join(parts)


def index_one(project, name: str, rec: dict, rebuild: bool) -> dict:
    clone = CREPO.clone_path(project, name)
    if not clone.is_dir():
        return {"name": name, "ok": False, "problem": "клона нет: выполните code:sync"}
    path = index_path(project, name)
    if rebuild and path.exists():
        _remove_index(path)
    try:
        with store_mod.Store(path) as st:
            stats = scan.scan_repo(clone, st, include=rec.get("include") or "",
                                   exclude=rec.get("exclude") or "")
            out = {"name": name, "ok": True, "rev": stats.rev, "files": stats.files,
                   "indexed": stats.indexed, "parsed": stats.parsed, "reused": stats.reused,
                   "secrets": stats.secrets, "duration_s": round(stats.duration_s, 3),
                   "skipped": stats.skipped, "languages": stats.languages,
                   "index": str(path), "line": _summary_line(name, stats)}
            return out
    except (scan.GitError, scan.ScanRefused) as e:
        return {"name": name, "ok": False, "problem": str(e)}
    except Exception as e:  # noqa: BLE001 — один репозиторий не должен ронять остальные
        return {"name": name, "ok": False, "problem": f"{type(e).__name__}: {e}"}


def run_index(project, clean: dict, names: list, apply: bool, rebuild: bool, as_json: bool) -> int:
    if not apply:
        return run_plan(project, clean, names, as_json)
    if not names:
        if as_json:
            _say(json.dumps({"repos": []}, ensure_ascii=False))
        else:
            _say("Репозиториев кода в реестре нет — индексировать нечего (подключаются в разделе «Код»).")
        return EXIT_OK
    lock = lock_path(project)
    lock.parent.mkdir(parents=True, exist_ok=True)
    taken, holder = lock_take(str(lock), {"task": "code:index"})
    if not taken:
        _say(f"✗ индексация кода уже идёт (процесс {holder.get('pid', '?')}) — дождитесь её конца")
        return EXIT_FAIL
    try:
        rows = [index_one(project, n, clean[n], rebuild) for n in names]
    finally:
        lock_release(str(lock))
    if as_json:
        _say(json.dumps({"repos": rows}, ensure_ascii=False))
    else:
        for row in rows:
            if row["ok"]:
                _say(row["line"])
            else:
                _say(f"✗ {row['name']}: {row.get('problem', 'не проиндексирован')}")
    return EXIT_OK if all(r["ok"] for r in rows) else EXIT_PART


def status_repo(project, name: str, rec: dict) -> dict:
    clone = CREPO.clone_path(project, name)
    path = index_path(project, name)
    out = {"name": name, "index": "нет", "clone": "нет"}
    if not path.is_file():
        out["problem"] = "индекс не построен: code:index --apply"
        return out
    try:
        with store_mod.Store(path) as st:
            out.update(st.status())
    except Exception as e:  # noqa: BLE001
        out["problem"] = f"индекс не читается: {type(e).__name__}: {e}"
        return out
    out["index"] = "есть"
    if not clone.is_dir():
        out["problem"] = "клона нет: индекс есть, но обновлять нечего"
        return out
    try:
        repo = scan.GitRepo(clone)
        out["clone"] = "есть"
        head = repo.head()
        out["head_rev"] = head
        index_rev = out.get("index_rev") or out.get("repo_rev") or ""
        if index_rev and index_rev != head:
            if repo.is_ancestor(index_rev, head):
                out["behind"] = repo.count_between(index_rev, head)
            else:
                out["behind"] = "история переписана"
        elif index_rev:
            out["behind"] = 0
    except scan.GitError as e:
        out["problem"] = str(e)
    return out


def run_status(project, clean: dict, names: list, as_json: bool) -> int:
    rows = [status_repo(project, n, clean[n]) for n in names]
    if as_json:
        _say(json.dumps({"repos": rows}, ensure_ascii=False))
        return EXIT_OK if all(not r.get("problem") for r in rows) else EXIT_PART
    if not rows:
        _say("Репозиториев кода в реестре нет.")
        return EXIT_OK
    bad = False
    for r in rows:
        if r.get("problem") and r.get("index") != "есть":
            _say(f"✗ {r['name']}: {r['problem']}")
            bad = True
            continue
        behind = r.get("behind", 0)
        tail = "актуален" if behind == 0 else (f"отстал на {behind}" if isinstance(behind, int) else str(behind))
        langs = r.get("languages") or {}
        top = ", ".join(f"{k} {v.get('files', 0)}" for k, v in list(langs.items())[:5])
        _say(f"· {r['name']}: {r.get('index_rev', '')[:8] or '—'}, {tail}, "
             f"файлов {_fmt_int(r.get('files', 0))}, в индексе {_fmt_int(r.get('indexed', 0))}"
             + (f", секретов {_fmt_int(r.get('secrets', 0))}" if r.get("secrets") else "")
             + (f", языки: {top}" if top else ""))
        if r.get("problem"):
            _say(f"    {r['problem']}")
            bad = True
    return EXIT_PART if bad else EXIT_OK


def run_secrets(project, clean: dict, names: list, as_json: bool) -> int:
    rows = []
    for name in names:
        path = index_path(project, name)
        if not path.is_file():
            rows.append({"name": name, "problem": "индекс не построен", "secrets": []})
            continue
        try:
            with store_mod.Store(path) as st:
                rows.append({"name": name, "secrets": st.secrets()})
        except Exception as e:  # noqa: BLE001
            rows.append({"name": name, "problem": f"{type(e).__name__}: {e}", "secrets": []})
    if as_json:
        _say(json.dumps({"repos": rows}, ensure_ascii=False))
    else:
        total = 0
        for row in rows:
            if row.get("problem"):
                _say(f"✗ {row['name']}: {row['problem']}")
                continue
            items = row.get("secrets") or []
            total += len(items)
            if not items:
                _say(f"✓ {row['name']}: находок нет")
                continue
            _say(f"! {row['name']}: находок {len(items)} (значения не показываются)")
            for hit in items:
                _say(f"    {hit['path']}:{hit['line']}  {hit['rule']}  {hit['mask']}")
        if total:
            _say("Секрет уже в истории репозитория: отзовите или замените его в источнике. "
                 "Аврора историю чужого репозитория не меняет.")
    return EXIT_OK


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Индекс кода проекта: инвентарь репозиториев Code/")
    p.add_argument("--project", default=".", help="корень проекта (по умолчанию текущая папка)")
    p.add_argument("--repo", default="", help="только один репозиторий по имени")
    p.add_argument("--index", action="store_true", help="построить или обновить индекс")
    p.add_argument("--status", action="store_true", help="состояние индекса без записи")
    p.add_argument("--secrets", action="store_true", help="находки секретов без значений")
    p.add_argument("--rebuild", action="store_true", help="удалить индекс и построить заново")
    p.add_argument("--apply", action="store_true", help="записать индекс (без него — план)")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    args = p.parse_args(argv)
    project = Path(args.project).resolve()
    clean, names, probs, unknown = selected(project, args.repo)
    if unknown:
        _say(f"✗ репозитория «{unknown}» нет в реестре кода — подключите его в разделе «Код»")
        return EXIT_FAIL
    bad_names = {x["name"] for x in probs}
    for x in probs:
        line = (f"✗ {x['name']}: запись реестра не годится — поле {x['field']} ({x['code']}); "
                "поправьте в разделе «Код»")
        print(line, file=sys.stderr if args.json else sys.stdout, flush=True)
    names = [n for n in names if n not in bad_names]
    try:
        if args.status:
            return run_status(project, clean, names, args.json)
        if args.secrets:
            return run_secrets(project, clean, names, args.json)
        return run_index(project, clean, names, args.apply, args.rebuild, args.json)
    except KeyboardInterrupt:
        _say("остановлено")
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
