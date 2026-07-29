#!/usr/bin/env python3
"""aurora_update.py — обновить движок Aurora в работающем проекте (подход A).

Перезаписывает В ПРОЕКТЕ только инженерные файлы из `engine_manifest.txt`.
Контент проекта (aurora.config.yaml, AuroraKnowledgeDB, Raw, Sources, Deliverables,
Artifacts, Workspaces, Templates/, Prompts/) не трогается.

По умолчанию — DRY-RUN: показывает, что изменится, с кратким диффом. Запись — с --apply.

  python3 <kit>/aurora.py update <project>            # dry-run
  python3 <kit>/aurora.py update <project> --apply     # записать

Правила манифеста:
  обычная строка          — перезаписать файл 1:1
  (sync) <kind>           — обновить тело каждого .opencode/skills/<kind>-<slug>/SKILL.md,
                            подставив имя/slug из конфига (slug в имени папки сохраняется)
  (agents) AGENTS.md      — регенерировать из шаблона с полями проекта из конфига
  (seed) <dir>            — вариант (1): не перезаписывать; новые/изменённые файлы
                            положить рядом как <файл>.new для ручного сравнения

Проект может отказаться от обновлений отдельных путей — `.opencode/update_ignore.txt`
(glob-шаблоны, по одному в строке). Полезно для шаблонов, локализованных под проект:
иначе update предлагает одни и те же .new на каждом запуске.
"""
from __future__ import annotations
import argparse, difflib, re, shutil, sys
from pathlib import Path
from datetime import date

def find_kit() -> Path:
    """Где лежит kit. Копия этого скрипта живёт и в проекте — она обновлять не умеет.

    Обновление берёт файлы из kit'а: манифест, схему папок, версию. Если скрипт запущен
    из `.opencode/scripts/` проекта, рядом лежит `kit_path.txt` — путь, записанный при
    установке. Тогда работаем от него, а не от папки проекта: иначе update решит, что
    проект и есть kit, и упадёт на отсутствующем манифесте.
    """
    here = Path(__file__).resolve().parents[1]
    if (here / "engine_manifest.txt").is_file():
        return here
    hint = here / "kit_path.txt"          # .opencode/kit_path.txt
    if hint.is_file():
        kit = Path(hint.read_text(encoding="utf-8").strip()).expanduser()
        if (kit / "engine_manifest.txt").is_file():
            return kit
    return here


KIT = find_kit()
MANIFEST = KIT / "engine_manifest.txt"
STRUCTURE = KIT / "structure_dirs.txt"
VERSION_FILE = KIT / "VERSION"


def missing_dirs(target: Path):
    """Стандартные папки схемы, которых нет в проекте (создаются идемпотентно)."""
    if not STRUCTURE.is_file():
        return []
    out = []
    for line in STRUCTURE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not (target / line).is_dir():
            out.append(line)
    return out


# ---------- чтение конфига проекта (нужно для (sync)/(agents)) ----------

def project_fields(target: Path) -> dict:
    cfg = target / "aurora.config.yaml"
    f = {"name": target.name, "slug": target.name, "jira": "PROJECT", "space": "SPACE"}
    if cfg.is_file():
        t = cfg.read_text(encoding="utf-8")
        def g(key, default):
            m = re.search(rf'^\s*{key}\s*:\s*"?([^"\n#]+?)"?\s*$', t, re.M)
            return m.group(1).strip() if m else default
        f["name"] = g("name", f["name"])
        f["slug"] = g("slug", f["slug"])
        f["jira"] = g("project_key", f["jira"])
        f["space"] = g("space", f["space"])
    return f


def fill(text: str, f: dict) -> str:
    return (text.replace("{{PROJECT_NAME}}", f["name"])
                .replace("{{PROJECT_SLUG}}", f["slug"])
                .replace("{{JIRA_KEY}}", f["jira"])
                .replace("{{CONFLUENCE_SPACE}}", f["space"])
                .replace("{{DATE}}", date.today().isoformat()))


# ---------- разбор манифеста ----------

def parse_manifest():
    rows = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=>" not in line:
            continue
        src, dst = (x.strip() for x in line.split("=>", 1))
        rows.append((src, dst))
    return rows


# ---------- планирование изменений ----------

class Change:
    def __init__(self, kind, path, new_text=None, note="", external=None):
        self.kind = kind      # write | seed-new | skip
        self.path = path      # относительный путь в проекте
        self.new_text = new_text
        self.note = note
        self.external = external  # реальный путь, если запись уходит за пределы проекта (симлинк)


def _external_target(target: Path, dst: Path):
    """Если dst (через симлинки) резолвится ВНЕ дерева проекта — вернуть реальный путь."""
    try:
        real = dst.resolve()
        target.resolve().relative_to  # noqa
        real.relative_to(target.resolve())
        return None
    except (ValueError, OSError):
        try:
            return dst.resolve()
        except OSError:
            return None


def load_ignore(target: Path) -> list:
    """Пути, которые проект сознательно ведёт по-своему (`.opencode/update_ignore.txt`).

    Без этого списка `update` бесконечно предлагает одни и те же `.new` для шаблонов,
    локализованных под проект: их отвергают, а на следующем обновлении они возвращаются.
    Формат: по одному glob-шаблону в строке, `#` — комментарий.
    """
    path = target / ".opencode" / "update_ignore.txt"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def plan(target: Path, f: dict):
    changes = []
    ignore = load_ignore(target)

    def diff_or_new(rel_dst: str, new_text: str, seed=False):
        from fnmatch import fnmatch
        probe = rel_dst[:-4] if rel_dst.endswith(".new") else rel_dst
        if any(fnmatch(probe, pat) or fnmatch(rel_dst, pat) for pat in ignore):
            return
        dst = target / rel_dst
        ext = _external_target(target, dst) if dst.exists() or dst.parent.is_symlink() else None
        if dst.is_file():
            old = dst.read_text(encoding="utf-8")
            if old == new_text:
                return  # идентично — нечего делать
            if seed:
                changes.append(Change("seed-new", rel_dst + ".new", new_text,
                                      "изменён в kit — рядом положен .new (вариант 1)", ext))
            else:
                changes.append(Change("write", rel_dst, new_text, _short_diff(old, new_text), ext))
        else:
            changes.append(Change("seed-new" if seed else "write", rel_dst, new_text, "новый файл", ext))

    for src, dst in parse_manifest():
        sm = re.match(r"\((\w+)\)\s+(.*)", dst)
        if not sm:  # обычная перезапись
            diff_or_new(dst, (KIT / src).read_text(encoding="utf-8"))
            continue
        rule, arg = sm.group(1), sm.group(2).strip()
        if rule == "sync":
            # Тело sync-скилла проект часто дорабатывает под свой контур (правила синка,
            # батчи, ссылки на файлы правил). Поэтому НЕ перезаписываем: если файл уже
            # есть и отличается — кладём kit-версию рядом как .new (как для Templates).
            body = (KIT / src).read_text(encoding="utf-8")
            for folder in sorted((target / ".opencode/skills").glob(f"{arg}-*")):
                if folder.is_dir():
                    diff_or_new(f".opencode/skills/{folder.name}/SKILL.md", fill(body, f), seed=True)
        elif rule == "launcher":
            # путь к kit'у подставляем при раскладке: у каждой машины он свой
            body = (KIT / src).read_text(encoding="utf-8").replace("{{KIT_PATH}}", str(KIT))
            diff_or_new(arg, body)
        elif rule == "agents":
            diff_or_new(arg, fill((KIT / src).read_text(encoding="utf-8"), f))
        elif rule == "seed":
            src_dir = KIT / src
            if src_dir.is_dir():
                for p in src_dir.rglob("*"):
                    if p.is_file():
                        diff_or_new(f"{arg}/{p.relative_to(src_dir).as_posix()}",
                                    p.read_text(encoding="utf-8"), seed=True)
    return changes


def _short_diff(old: str, new: str, ctx=1) -> str:
    d = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                  lineterm="", n=ctx))
    body = [l for l in d[2:] if l and l[0] in "+-"]
    plus = sum(1 for l in body if l.startswith("+"))
    minus = sum(1 for l in body if l.startswith("-"))
    return f"~{plus}+/{minus}-"


# ---------- версии ----------

def kit_version() -> str:
    return VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.is_file() else "0.0.0"


def project_version(target: Path) -> str:
    vf = target / "AuroraKnowledgeDB/meta/aurora_version.txt"
    return vf.read_text(encoding="utf-8").strip() if vf.is_file() else "(нет штампа, до 1.0.0)"


def stamp_version(target: Path, ver: str):
    vf = target / "AuroraKnowledgeDB/meta/aurora_version.txt"
    vf.parent.mkdir(parents=True, exist_ok=True)
    vf.write_text(ver + "\n", encoding="utf-8")


# ---------- main ----------

def run(target: Path, apply: bool, structure_only: bool = False):
    if not (target / "AuroraKnowledgeDB").is_dir():
        print(f"⚠️  {target} не похоже на проект Aurora (нет AuroraKnowledgeDB/).", file=sys.stderr)
        return 1
    f = project_fields(target)
    kv, pv = kit_version(), project_version(target)
    ignored = load_ignore(target)
    print(f"Aurora update{' (только структура)' if structure_only else ''} — {target}")
    if ignored:
        print(f"  update_ignore: {len(ignored)} правил — эти пути проект ведёт сам")
    print(f"  проект: {f['name']} (slug {f['slug']}) · версия {pv} → kit {kv}\n")

    new_dirs = missing_dirs(target)
    if new_dirs:
        print(f"Недостающие папки схемы к созданию: {len(new_dirs)}")
        for d in new_dirs:
            print(f"  + {d}/")
        print()

    # режим «только структура»: папки + штамп версии, движок не трогаем
    if structure_only:
        if not new_dirs and pv == kv:
            print("✅ Структура актуальна, версия совпадает — изменений нет.")
            return 0
        if not apply:
            print("(dry-run) Ничего не создано. Повторите с --apply.")
            return 0
        for d in new_dirs:
            p = target / d
            p.mkdir(parents=True, exist_ok=True)
            (p / ".gitkeep").touch()
        stamp_version(target, kv)
        print(f"✅ Создано папок: {len(new_dirs)}. Версия → {kv}. Движок не тронут.")
        return 0

    changes = plan(target, f)
    writes = [c for c in changes if c.kind == "write"]
    seeds = [c for c in changes if c.kind == "seed-new"]

    if not changes and not new_dirs:
        print("✅ Движок и структура уже актуальны — изменений нет.")
        if apply and pv != kv:
            stamp_version(target, kv)
            print(f"   Проставлен штамп версии: {kv}")
        return 0

    ext_writes = [c for c in changes if c.external]
    if ext_writes:
        print("⚠️  ВНИМАНИЕ: часть путей — симлинки в ОБЩИЕ локации вне проекта.")
        print("   Запись затронет не только этот проект, а всё, что использует эти файлы:")
        for c in ext_writes:
            print(f"     {c.path} → {c.external}")
        print("   Это нормально при модели «общий движок через симлинк», но по одному")
        print("   проекту вы обновляете общий движок. Если не этого хотели — прервите.\n")

    print(f"Инженерные файлы к перезаписи: {len(writes)}")
    for c in writes:
        tag = "  ⚠️shared" if c.external else ""
        print(f"  ~ {c.path}   [{c.note}]{tag}")
    if seeds:
        print(f"\nШаблоны/промпты (вариант 1 — рядом как .new, руками сравнить): {len(seeds)}")
        for c in seeds:
            print(f"  + {c.path}   [{c.note}]")

    if not apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply, чтобы применить.")
        return 0

    for d in new_dirs:
        p = target / d
        p.mkdir(parents=True, exist_ok=True)
        (p / ".gitkeep").touch()
    for c in writes + seeds:
        dst = target / c.path
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(c.new_text, encoding="utf-8")
        if dst.suffix in (".command", ".sh"):
            dst.chmod(0o755)     # без бита исполнения двойной щелчок не сработает
    (target / ".opencode").mkdir(parents=True, exist_ok=True)
    (target / ".opencode/kit_path.txt").write_text(str(KIT) + "\n", encoding="utf-8")
    stamp_version(target, kv)
    print(f"\n✅ Применено: {len(new_dirs)} папок, {len(writes)} перезаписей, {len(seeds)} .new-файлов. Версия → {kv}")
    print("   Проверьте: python3 .opencode/scripts/aurora_doctor.py && git diff")
    if seeds:
        print("   Не забудьте сравнить *.new с вашими Templates/Prompts и удалить .new.")
    return 0


def kit_is_reachable() -> bool:
    """Понятная ошибка вместо трассировки: человек не должен читать стек, чтобы понять,
    что запустил копию скрипта из проекта, а kit лежит в другом месте."""
    if MANIFEST.is_file():
        return True
    print("Обновление берёт файлы из kit'а, а он не найден.\n", file=sys.stderr)
    print(f"  искал: {MANIFEST}", file=sys.stderr)
    hint = Path(__file__).resolve().parents[1] / "kit_path.txt"
    print(f"  подсказка о пути: {hint} — {'есть, но путь не ведёт в kit' if hint.is_file() else 'нет'}",
          file=sys.stderr)
    print("\nЧто делать:", file=sys.stderr)
    print("  1) запустите из самого kit'а:  python3 <kit>/aurora.py update <проект>", file=sys.stderr)
    print("  2) либо запишите путь к kit'у: echo /путь/к/aurora-studio > "
          "<проект>/.opencode/kit_path.txt", file=sys.stderr)
    return False


def main():
    ap = argparse.ArgumentParser(description="Обновить движок Aurora в проекте по манифесту")
    ap.add_argument("target", nargs="?", default=".", help="Корень проекта")
    ap.add_argument("--apply", action="store_true", help="Записать изменения (иначе dry-run)")
    ap.add_argument("--structure-only", action="store_true",
                    help="Только досоздать недостающие папки схемы + штамп версии; движок не трогать")
    a = ap.parse_args()
    if not kit_is_reachable():
        return 2
    return run(Path(a.target).expanduser().resolve(), a.apply, a.structure_only)


if __name__ == "__main__":
    sys.exit(main())
