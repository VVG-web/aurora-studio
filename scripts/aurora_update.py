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
  (sync) <kind>           — обновить тело каждого .claude/skills/<kind>-<slug>/SKILL.md,
                            подставив имя/slug из конфига (slug в имени папки сохраняется)
  (agents) AGENTS.md      — регенерировать из шаблона с полями проекта из конфига
  (seed) <dir>            — вариант (1): не перезаписывать; новые/изменённые файлы
                            положить рядом как <файл>.new для ручного сравнения

Проект может отказаться от обновлений отдельных путей — `aurora.update_ignore.txt`
(glob-шаблоны, по одному в строке). Полезно для шаблонов, локализованных под проект:
иначе update предлагает одни и те же .new на каждом запуске.

Режим `--engine-only` — подготовка движка на этой машине. Копирует только пути под `.aurora/` из
манифеста (скрипты, навыки, документы, коннекторы): папка локальная и вне git, поэтому клон
проекта приходит без неё. Файлов проекта и git это не касается; остальное обновление (AGENTS.md,
шаблоны, схема папок, правила .gitignore) остаётся за полным `kit:update`. Панель вызывает этот
режим сама при выборе проекта с неполным движком и по кнопке «Подготовить проект».

Панель: `kit:update`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations
import argparse, difflib, os, re, shutil, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aurora_common import connector_scripts, load_connector_manifest, replace_file, utc_today, write_text_atomic  # noqa: E402 — дата в UTC, одна на движок; запись целиком; манифест коннектора без исключений
try:
    from aurora_common import ENGINE_BEHIND, engine_status, kit_engine_api, version_key  # noqa: E402 — версии и статус движка: одно определение
except ImportError:
    # Долгоживущий процесс (панель, запущенная до `git pull` кита) держит в памяти прежний `aurora_common` без этих имён:
    # модуль импортируется, а то, что без них не обойтись, говорит понятно, а не `ImportError` на импорте.
    ENGINE_BEHIND = ("behind_minor", "behind_patch")

    def version_key(v) -> tuple:
        nums = [int(x) for x in re.findall(r"\d+", str(v).split("+")[0])[:3]]
        return tuple(nums + [0] * (3 - len(nums)))

    def kit_engine_api(kit) -> int:
        return 1

    def engine_status(*_a, **_k):
        raise RuntimeError("в памяти aurora_common прежней версии (кит обновлён под работающим процессом): перезапустите панель")
import folder_guides as FG  # noqa: E402 — описания папок проекта (README.md)
import engine_txn as ETX  # noqa: E402 — проверка до записи, снимок, метка и откат обновления движка (BL-LCY-07)

def find_kit() -> Path:
    """Где лежит kit. Копия этого скрипта живёт и в проекте — она обновлять не умеет.

    Обновление берёт файлы из kit'а: манифест, схему папок, версию. Если скрипт запущен
    из `.aurora/scripts/` проекта, рядом лежит `kit_path.txt` — путь, записанный при
    установке. Тогда работаем от него, а не от папки проекта: иначе update решит, что
    проект и есть kit, и упадёт на отсутствующем манифесте.
    """
    here = Path(__file__).resolve().parents[1]
    if (here / "engine_manifest.txt").is_file():
        return here
    hint = here / "kit_path.txt"          # .aurora/kit_path.txt
    if hint.is_file():
        kit = Path(hint.read_text(encoding="utf-8").strip()).expanduser()
        if (kit / "engine_manifest.txt").is_file():
            return kit
    return here


KIT = find_kit()
MANIFEST = KIT / "engine_manifest.txt"
STRUCTURE = KIT / "structure_dirs.txt"
VERSION_FILE = KIT / "VERSION"


def _config_mirror_paths(target: Path):
    """Пути зеркал подключённых модулей так, как их отдаёт реестр (из конфига — как написал владелец)."""
    sys.path.insert(0, str(KIT / "scripts"))
    try:
        import sources_registry as R
    except ImportError:
        return []
    # путь из манифеста реестр уже проверил (`load()`: «../outside» отброшен вместе с модулем);
    # путь из конфига проекта пишет сам владелец и отдаётся как есть
    return [i["path"] for i in R.instances(str(target)) if i["path"]]


def _outside_project(path: str) -> bool:
    """Путь выходит за проект: абсолютный (в том числе «\\x», «C:/x») или с сегментом «..».

    Проверка лексическая и нарочно мягче `ext_safe_relpath`: «Sources/Wiki (old)» владельца — внутри проекта.
    """
    p = path.replace("\\", "/")
    return p.startswith("/") or bool(re.match(r"[A-Za-z]:", p)) or ".." in p.split("/")


def mirror_dirs(target: Path):
    """Папки зеркал подключённых модулей, которые `update` вправе создать: их нет в схеме, их заявляет реестр.

    Путь зеркала вне проекта (`../Shared/Confluence`, абсолютный) сюда не входит: значение владельца реестр
    отдаёт как есть и не правит, но `update` не создаёт за него папок за пределами проекта (`outside_mirror_dirs`).
    """
    return [d for d in _config_mirror_paths(target) if not _outside_project(d)]


def outside_mirror_dirs(target: Path):
    """Пути зеркал из конфига, выходящие за проект: `update` папок для них не создаёт."""
    return [d for d in _config_mirror_paths(target) if _outside_project(d)]


def missing_dirs(target: Path):
    """Стандартные папки схемы и зеркал, которых нет в проекте (создаются идемпотентно)."""
    schema = []
    if STRUCTURE.is_file():
        schema = [l.strip() for l in STRUCTURE.read_text(encoding="utf-8").splitlines()
                  if l.strip() and not l.strip().startswith("#")]
    return [d for d in schema + mirror_dirs(target) if not (target / d).is_dir()]


# ---------- чтение конфига проекта (нужно для (sync)/(agents)) ----------

def project_fields(target: Path) -> dict:
    cfg = target / "aurora.config.yaml"
    f = {"name": target.name, "slug": target.name, "jira": "PROJECT", "space": "SPACE"}
    if cfg.is_file():
        t = cfg.read_text(encoding="utf-8", errors="replace")
        def g(key, default):
            m = re.search(rf'^\s*{key}\s*:\s*"?([^"\n#]+?)"?\s*$', t, re.M)
            v = m.group(1).strip() if m else default
            # Значение не в UTF-8 (правка в cp1251) читается «с заменой» и стало бы мусором в
            # AGENTS.md и прочих файлах, которые update переписывает; берём значение по умолчанию.
            return default if "\ufffd" in v else v
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
                .replace("{{DATE}}", utc_today())
                .replace("{{YEAR}}", utc_today()[:4]))


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


def retired_paths() -> list:
    """Файлы движка, выведенные из состава: строки `- <путь>` в манифесте.

    Слияние скриптов оставляло в проектах их прежние копии: команда уже исполняется другим
    файлом, а старый лежит рядом, работает по-своему и расходится с kit'ом. Список ведётся
    руками — угадывать «чужое это или наше» обновление не имеет права.
    """
    out = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line.startswith("- ") and "=>" not in line:
            out.append(line[2:].strip())
    return out


# ---------- переезд движка из `.opencode/` (1.158.0) ----------
#
# До 1.158.0 движок жил в `.opencode/` — папке харнесса OpenCode — и ездил по git проекта.
# Теперь он в своей `.aurora/` и вне git: харнессы у каждого свои, движок — копия кита.
# Переезд раскладывает прежнюю папку по смыслу, ничего чужого не удаляя:
#   данные движка (состояние, прогоны, вложения, кэш) → `.aurora/`;
#   журнал запусков → `AuroraKnowledgeDB/meta/run_log.md` (с 1.168.0 вне git, см. ниже);
#   update_ignore → `aurora.update_ignore.txt` в корне проекта;
#   навыки проекта → `.claude/skills/` (общие, в git); скрипты проекта → `Scripts/`;
#   копии кита убираются — их заново ставит обновление в `.aurora/`;
#   файлы самого OpenCode и всё незнакомое остаются на месте и называются в отчёте.

LEGACY = ".opencode"
ENGINE = ".aurora"
LEGACY_DATA = ("state", "runs", "cache", "context")
# Что движок клал в `.opencode/` сам — копии кита: их ставит обновление в `.aurora/`.
LEGACY_ENGINE_DIRS = ("connectors", "docs", "vendor", "reports", "__pycache__")
LEGACY_ENGINE_FILES = ("commands.txt", "structure_dirs.txt", "moc_groups.txt", "scenarios.txt",
                       "kit_path.txt", "aurora.env.local.example")
# Скрипты, выведенные из кита раньше, чем появился список `- <путь>` в манифесте.
KIT_LEFTOVERS = ("kb_verify.py",)
JUNK = (".DS_Store", "Thumbs.db", "desktop.ini")
# Где у проекта бывают ссылки на пути движка: шаблоны, промпты, навыки, свои скрипты.
REWRITE_DIRS = ("Templates", "Prompts", "TemplatesCommon", "Scripts", ".claude/skills")
REWRITE_EXT = (".md", ".txt", ".py", ".sh", ".json", ".yaml", ".yml")


def legacy_paths(text: str) -> str:
    """Пути прежней раскладки (до 1.158.0) → новые: навыки кита и движок — `.aurora/`,
    навыки проекта — `.claude/skills/`, журнал — `AuroraKnowledgeDB/meta/`."""
    if LEGACY + "/" not in text:
        return text
    text = re.sub(r"\.opencode/skills/(?!aurora-)", ".claude/skills/", text)
    text = text.replace(".opencode/run_log.md", "AuroraKnowledgeDB/meta/run_log.md")
    text = text.replace(".opencode/update_ignore.txt", UPDATE_IGNORE)
    return text.replace(".opencode/", ".aurora/")


def relayout_rewrite(target: Path) -> list:
    """Поправить пути движка в файлах проекта (после переезда). → поправленные пути."""
    done = []
    for base in REWRITE_DIRS:
        root = target / base
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in REWRITE_EXT or p.name.endswith(".new"):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            fixed = legacy_paths(text)
            if fixed != text:
                write_text_atomic(p, fixed)
                done.append(p.relative_to(target).as_posix())
    return done


def _kit_script_names() -> set:
    names = {p.name for p in (KIT / "scripts").rglob("*") if p.is_file()}
    names |= {Path(dst).name for _src, dst in parse_manifest()}
    return names | set(KIT_LEFTOVERS)


def _kit_skill_names() -> set:
    return {p.name for p in (KIT / "skills").iterdir() if p.is_dir()}


def relayout_plan(target: Path) -> dict:
    """Что переезд сделает в проекте: {moves: [(откуда, куда)], drop: [пути], keep: [пути],
    clash: [(откуда, куда)]}. Пути — от корня проекта. Нет `.opencode/` — пусто."""
    old = target / LEGACY
    out = {"moves": [], "drop": [], "keep": [], "clash": []}
    if not old.is_dir():
        return out

    def move(src: str, dst: str):
        (out["clash"] if (target / dst).exists() else out["moves"]).append((src, dst))

    for name in sorted(p.name for p in old.iterdir()):
        rel = f"{LEGACY}/{name}"
        if name in JUNK:
            out["drop"].append(rel)
        elif name in LEGACY_DATA:
            for child in sorted((old / name).iterdir()):
                move(f"{rel}/{child.name}", f"{ENGINE}/{name}/{child.name}")
        elif name == "run_log.md":
            move(rel, "AuroraKnowledgeDB/meta/run_log.md")
        elif name == "update_ignore.txt":
            move(rel, UPDATE_IGNORE)
        elif name == "ping-state.json":
            move(rel, f"{ENGINE}/{name}")
        elif name == "skills":
            kit = _kit_skill_names()
            for sk in sorted(p for p in (old / name).iterdir()):
                if sk.name in kit or sk.name in JUNK:
                    out["drop"].append(f"{rel}/{sk.name}")
                elif sk.is_dir() and (sk / "SKILL.md").is_file():
                    move(f"{rel}/{sk.name}", f".claude/skills/{sk.name}")
                else:
                    out["keep"].append(f"{rel}/{sk.name}")
        elif name == "scripts":
            kit = _kit_script_names()
            for sc in sorted(p for p in (old / name).iterdir()):
                if sc.name in kit or sc.is_dir() or sc.name in JUNK or sc.name == ".gitkeep":
                    out["drop"].append(f"{rel}/{sc.name}")
                else:
                    move(f"{rel}/{sc.name}", f"Scripts/{sc.name}")
        elif name in LEGACY_ENGINE_DIRS or name in LEGACY_ENGINE_FILES:
            out["drop"].append(rel)
        else:
            out["keep"].append(rel)           # OpenCode (package.json, node_modules…) и чужое
    return out


def relayout_moves(target: Path, rp: dict) -> list:
    """Переложить данные, журнал, навыки и скрипты проекта. → что не вышло."""
    import shutil
    failed = []
    for src, dst in rp["moves"]:
        s, d = target / src, target / dst
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(s), str(d))
        except OSError as e:
            failed.append(f"{src} → {dst}: {e}")
    ui = target / UPDATE_IGNORE
    if ui.is_file():
        # Пути в списке — к навыкам на прежнем месте: правило должно указывать на новое.
        try:
            text = ui.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = None          # не UTF-8 — не наш текст: молча не переписываем
        fixed = re.sub(r"\.(?:opencode|aurora)/skills/", ".claude/skills/", text or "")
        if text is not None and fixed != text:
            write_text_atomic(ui, fixed)
    return failed


def relayout_cleanup(target: Path, rp: dict) -> list:
    """Убрать из `.opencode/` копии кита — после того как движок встал в `.aurora/`.
    Пустую папку — тоже. → что не вышло."""
    import shutil
    failed = []
    for rel in rp["drop"]:
        p = target / rel
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            elif p.exists() or p.is_symlink():
                p.unlink()
        except OSError as e:
            failed.append(f"{rel}: {e}")
    old = target / LEGACY
    for d in sorted((x for x in old.rglob("*") if x.is_dir()), key=lambda x: -len(x.parts)) \
            if old.is_dir() else []:
        try:
            d.rmdir()                          # только пустые: непустую rmdir не тронет
        except OSError:
            pass
    try:
        old.rmdir()
    except OSError:
        pass
    return failed


def agent_running(target: Path) -> str:
    """Идёт ли в проекте пишущий прогон агента (замок на новом или прежнем месте)."""
    from aurora_common import lock_holder
    for base in (ENGINE, LEGACY):
        held = lock_holder(target / base / "state" / "agent.lock")
        if held:
            return f"{held.get('task')} (pid {held.get('pid')})"
    return ""


def print_guides(guides: list) -> None:
    if not guides:
        return
    new = [g for g in guides if g[2] == "create"]
    print(f"\nОписания папок (README.md): создать {len(new)}, обновить {len(guides) - len(new)}")
    for rel, _text, kind in guides[:60]:
        print(f"  {'+' if kind == 'create' else '~'} {rel}")


def print_relayout(rp: dict) -> None:
    if not (rp["moves"] or rp["drop"] or rp["clash"]):
        return
    print("Переезд движка из .opencode/ в .aurora/ (1.158.0):")
    data = {}
    for src, dst in rp["moves"]:
        parts = src.split("/")
        if len(parts) > 2 and parts[1] in LEGACY_DATA:
            data.setdefault(parts[1], 0)
            data[parts[1]] += 1
            continue
        print(f"  → {src}  ⇒  {dst}")
    for name, n in sorted(data.items()):
        print(f"  → {LEGACY}/{name}/ ({n})  ⇒  {ENGINE}/{name}/")
    for src, dst in rp["clash"]:
        print(f"  ! {src}: на месте уже есть {dst} — остаётся как было, сравните сами")
    if rp["drop"]:
        print(f"  − копии кита в .opencode/ уберутся после установки движка: {len(rp['drop'])}")
    for rel in rp["keep"]:
        print(f"  · {rel} — не движок (OpenCode или ваше), остаётся")
    print()


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


UPDATE_IGNORE = "aurora.update_ignore.txt"       # с 1.158.0 — в корне проекта, в git


def load_ignore(target: Path) -> list:
    """Пути, которые проект сознательно ведёт по-своему (`aurora.update_ignore.txt`).

    Без этого списка `update` бесконечно предлагает одни и те же `.new` для шаблонов,
    локализованных под проект: их отвергают, а на следующем обновлении они возвращаются.
    Формат: по одному glob-шаблону в строке, `#` — комментарий. До 1.158.0 файл лежал в
    папке движка — читаем и оттуда, пока перенос его не переложил.
    """
    path = target / UPDATE_IGNORE
    if not path.is_file():
        path = target / LEGACY / "update_ignore.txt"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


# Манифесты коннекторов кита, которые последний `plan()` пропустил: причины печатает `run()`.
SKIPPED_CONNECTORS: list = []


def plan(target: Path, f: dict):
    changes = []
    SKIPPED_CONNECTORS.clear()
    ignore = load_ignore(target)

    def diff_or_new(rel_dst: str, new_text: str, seed=False):
        from fnmatch import fnmatch
        probe = rel_dst[:-4] if rel_dst.endswith(".new") else rel_dst
        if any(fnmatch(probe, pat) or fnmatch(rel_dst, pat) for pat in ignore):
            return
        dst = target / rel_dst
        ext = _external_target(target, dst) if dst.exists() or dst.parent.is_symlink() else None
        if dst.is_file():
            # Файл, оборванный посреди записи (убитый процесс), может быть не UTF-8: он просто
            # «отличается» и перепишется, а не роняет каждое следующее обновление.
            old = dst.read_text(encoding="utf-8", errors="replace")
            if old == new_text:
                return  # идентично — нечего делать
            if legacy_paths(new_text) == old:
                # Шаблон кита ещё помнит прежний путь (например, в ссылке на историю git), а
                # копия в проекте уже переписана на новый. Это одно и то же: не плодить `.new`
                # на свежем проекте при каждом обновлении.
                return
            if legacy_paths(old) == new_text:
                # Разница только в путях движка (`.opencode/` → `.aurora/`, 1.158.0): это
                # не правка кита, а переезд — поправить на месте, без `.new` на разбор.
                changes.append(Change("write", rel_dst, new_text, "пути движка → .aurora/", ext))
                return
            if seed:
                changes.append(Change("seed-new", rel_dst + ".new", new_text,
                                      "изменён в kit — рядом положен .new (вариант 1)", ext))
            else:
                changes.append(Change("write", rel_dst, new_text, _short_diff(old, new_text), ext))
        else:
            changes.append(Change("seed-new" if seed else "write", rel_dst, new_text, "новый файл", ext))

    for src, dst in parse_manifest():
        sm = re.match(r"\((\w+)\)\s*(.*)", dst)   # у правила может не быть аргумента
        if not sm:  # обычная перезапись
            diff_or_new(dst, (KIT / src).read_text(encoding="utf-8"))
            continue
        rule, arg = sm.group(1), sm.group(2).strip()
        if rule == "connectors":
            for man in sorted((KIT / src).glob("*/connector.json")):
                # Кривой манифест (нет id, не JSON, корень не объект, api_version из будущего) —
                # строка в отчёт и пропуск одного модуля: update всех проектов не падает.
                m, why = load_connector_manifest(man)
                if m is None:
                    SKIPPED_CONNECTORS.extend(why)
                    continue
                diff_or_new(f".aurora/connectors/{m['id']}.json",
                            man.read_text(encoding="utf-8"))
                for script in connector_scripts(m):
                    # скрипт модуля (запуска или операций) может лежать рядом с манифестом
                    # (доустановленный модуль, операции встроенного) или в scripts/ kit'а
                    # (встроенный — его же импортируют панель и publish_doc)
                    body = man.parent / script
                    body = body if body.is_file() else KIT / "scripts" / script
                    if body.is_file():
                        diff_or_new(f".aurora/scripts/{script}",
                                    body.read_text(encoding="utf-8"))
                skill, tpl = m.get("run", {}).get("skill", ""), man.parent / "SKILL.md"
                # Тело sync-скилла проект часто дорабатывает под свой контур (правила
                # синка, батчи, ссылки на файлы правил). Поэтому НЕ перезаписываем: если
                # файл есть и отличается — кладём kit-версию рядом как .new.
                if skill and tpl.is_file():
                    for folder in sorted((target / ".claude/skills").glob(f"{skill}-*")):
                        if folder.is_dir():
                            diff_or_new(f".claude/skills/{folder.name}/SKILL.md",
                                        fill(tpl.read_text(encoding="utf-8"), f), seed=True)
        elif rule == "launcher":
            # путь к kit'у подставляем при раскладке: у каждой машины он свой
            body = (KIT / src).read_text(encoding="utf-8").replace("{{KIT_PATH}}", str(KIT))
            diff_or_new(arg, body)
        elif rule == "agents":
            diff_or_new(arg, fill((KIT / src).read_text(encoding="utf-8"), f))
        elif rule == "seed":
            src_path = KIT / src
            if src_path.is_dir():
                for p in src_path.rglob("*"):
                    if p.is_file():
                        diff_or_new(f"{arg}/{p.relative_to(src_path).as_posix()}",
                                    p.read_text(encoding="utf-8"), seed=True)
            elif src_path.is_file():
                # Одиночный файл-заготовка (пустой ростер, пустой список событий).
                # Правило умело только папки, и такая строка манифеста молча не делала
                # ничего: заготовка не появлялась, а отчёт собирался без ролей.
                diff_or_new(arg, src_path.read_text(encoding="utf-8"), seed=True)
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
    write_text_atomic(vf, ver + "\n")


# ---------- движок проекта на этой машине ----------
#
# `.aurora/` — копия движка на ЭТОМ компьютере, вне git: клон проекта приходит без неё. Панель
# в таком проекте молча брала скрипты из кита, а полусобранная копия (нет `scripts/agents/`)
# давала странные сбои — «мимо Pydantic AI» при зелёной установке. Здесь копия сверяется с
# манифестом и достраивается; файлов проекта и git это не касается.

KIT_POINTER = f"{ENGINE}/kit_path.txt"
ENGINE_MARK = f"{ENGINE}/engine_version.txt"      # версия кита, с которой скопирован движок на этой машине


def stamp_engine(target: Path, ver: str) -> None:
    """Метка копии движка: с какой версией кита совпадает `.aurora/` на этой машине.

    Штамп проекта (`aurora_version.txt`) ездит через git и говорит о версии у владельца, метка —
    локальная: по ней видно, что копия на этом компьютере отстала. Полное обновление ставит и то и другое."""
    if (target / ENGINE).is_dir() and _linkless(target, ENGINE_MARK) and not _ignored(ENGINE_MARK, load_ignore(target)):
        write_text_atomic(target / ENGINE_MARK, ver + "\n")


def engine_targets() -> list:
    """Пути под `.aurora/`, которые манифест кладёт в проект. Проекта не читает.

    Тот же разбор строк, что у `plan()` (обычные пути и модули источников); тест сверяет оба."""
    out = []
    for src, dst in parse_manifest():
        sm = re.match(r"\((\w+)\)\s*(.*)", dst)
        if not sm:
            if dst.startswith(ENGINE + "/"):
                out.append(dst)
        elif sm.group(1) == "connectors":
            for man in sorted((KIT / src).glob("*/connector.json")):
                m, _why = load_connector_manifest(man)
                if m is None:
                    continue
                out.append(f"{ENGINE}/connectors/{m['id']}.json")
                for script in connector_scripts(m):
                    if (man.parent / script).is_file() or (KIT / "scripts" / script).is_file():
                        out.append(f"{ENGINE}/scripts/{script}")
    return list(dict.fromkeys(out))


def _ignored(rel: str, ignore: list) -> bool:
    from fnmatch import fnmatch
    return any(fnmatch(rel, pat) for pat in ignore)


def _read_version(path: Path) -> str:
    try:
        v = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return v if re.match(r"\d", v) else ""


def mark_version(target: Path) -> str:
    """Версия кита, с которой скопирован движок на этой машине (метка копии); нет метки — пусто."""
    return _read_version(target / ENGINE_MARK)


def copy_version(target: Path) -> str:
    """Версия движка в `.aurora/` для показа: метка копии, а нет её — штамп проекта (`aurora_version.txt`)."""
    return mark_version(target) or _read_version(target / "AuroraKnowledgeDB/meta/aurora_version.txt")


def _linkless(target: Path, rel: str) -> bool:
    """Путь от корня проекта без ссылок на каждом уровне: через ссылку подготовка не пишет и не удаляет."""
    p = target
    for part in Path(rel).parts:
        p = p / part
        if p.is_symlink():
            return False
    return True


def _witness_differs(target: Path, ignore: list) -> bool:
    """Свидетель устаревшей копии без метки: реестр команд в `.aurora/` расходится с китовым.

    Копии, сделанные до метки, версии не помнят, а штамп проекта едет через git и может быть новее
    копии на этой машине. Реестр команд меняется почти с каждым выпуском, читается за миллисекунду."""
    if _ignored(f"{ENGINE}/commands.txt", ignore):
        return False
    try:
        return (target / ENGINE / "commands.txt").read_text(encoding="utf-8") != \
            (KIT / "commands.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _pointer_ok(target: Path) -> bool:
    """`kit_path.txt` ведёт в кит: копии скриптов проекта берут от него манифест, версию и настройку."""
    try:
        kit = Path((target / KIT_POINTER).read_text(encoding="utf-8").strip()).expanduser()
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return (kit / "engine_manifest.txt").is_file()


def engine_state(target: Path) -> dict:
    """Состояние движка проекта на этой машине: сверка `.aurora/` с манифестом и версией кита.

    `state`: `ok` — всё на месте; `missing` — движка нет вовсе (клон без `.aurora/`); `incomplete` —
    не хватает файлов манифеста (они в `missing`); `stale` — версия копии ниже кита (или движок
    ещё в прежней `.opencode/`: `legacy`); `unknown` — кита с манифестом не видно. Читает только
    пути: ни сети, ни содержимого файлов."""
    kit = kit_version()
    out = {"state": "unknown", "missing": [], "total": 0, "copy": "", "kit": kit, "legacy": False,
           "status": "unknown", "status_reason": "", "stamp_ahead": False}
    if not MANIFEST.is_file():
        return out
    out["copy"] = copy_version(target)
    if (target / LEGACY / "scripts").is_dir() and not (target / ENGINE / "scripts").is_dir():
        return {**out, "state": "stale", "legacy": True}       # переезд в .aurora/ — только полным kit:update
    ignore = load_ignore(target)
    files = [p for p in engine_targets() if not _ignored(p, ignore)]
    missing = [p for p in files if not (target / p).is_file()]
    wanted = list(files)
    if not _ignored(KIT_POINTER, ignore):
        wanted.append(KIT_POINTER)
        if not _pointer_ok(target):
            missing.append(KIT_POINTER)
    out.update(missing=missing, total=len(wanted))
    # «Отстала ли версия» решает единый статус (`aurora_common.engine_status`), а не своё сравнение строк: он же даёт
    # панели, гейту маршрутов, «Обновить все» и доктору один ответ. Оборванное обновление тоже требует обновления.
    es = engine_status(target, kit, kit_engine_api(KIT))
    out.update(status=es["status"], status_reason=es["reason"], stamp_ahead=es["stamp_ahead"],
               stamp_reason=es["stamp_reason"])
    behind = es["status"] in ENGINE_BEHIND or es["status"] == "partial"
    if files and len([m for m in missing if m != KIT_POINTER]) >= len(files):
        out["state"] = "missing"                  # ни одного файла манифеста: движка на машине нет
    elif missing:
        out["state"] = "incomplete"
    elif mark_version(target):
        out["state"] = "stale" if behind else "ok"
    else:
        # метки нет (копия старше метки): версию подскажет штамп проекта — нижняя оценка — и свидетель
        out["state"] = "stale" if behind or _witness_differs(target, ignore) else "ok"
    return out


def _inside(target: Path, path: Path) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(target))
        return True
    except ValueError:
        return False


def _git_ignored(target: Path, rel: str):
    """Закрыт ли путь правилом git: True/False; None — это не репозиторий или git недоступен."""
    try:
        cp = subprocess.run(["git", "-C", str(target), "check-ignore", "-q", "--", rel],
                            capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return True if cp.returncode == 0 else False if cp.returncode == 1 else None


def _write_engine_file(dst: Path, text: str) -> None:
    """Файл движка целиком или никак: временный файл рядом и подмена. Ссылка заменяется сама, а не её цель."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.aurora-tmp")
    try:
        tmp.write_bytes(text.encode("utf-8"))        # LF, как у полного обновления
        try:
            shutil.copymode(dst, tmp)                # права прежнего файла переходят к новому
        except OSError:
            pass
        replace_file(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def check_engine_set(target: Path, items, extra=()) -> list:
    """Проверка набора файлов движка до записи (`engine_txn.verify_overlay`) → список проблем. `extra` — файлы, которые
    обновление запишет следом через ссылки (общий движок): они входят в состав после обновления, хотя этим шагом не пишутся."""
    staged = {rel[len(ENGINE) + 1:]: text for rel, text in list(items) + list(extra)}
    return ETX.verify_overlay(staged, target / ENGINE / "scripts", {p.stem for p in (KIT / "scripts").glob("*.py")})


def apply_engine(target: Path, items: list, gone: list, ver: str, from_ver: str, effects=(), extra=()) -> dict:
    """Положить файлы движка проекта как одну операцию: проверка → снимок → метка → запись → сверка → (при сбое) откат.

    `items` — [(путь от корня проекта, новый текст)] под `.aurora/`, `gone` — выведенные файлы к удалению. Скрипты
    проверяются ДО первой записи (`check_engine_set`): сломанный набор не ляжет в проект и не оставит смешанный движок.
    Перед записью снимается копия заменяемого и удаляемого (вне проекта, три последних), создаётся метка
    `update.pending` — её снимает только конец обновления. Повторный запуск после обрыва продолжает ту же операцию:
    новый снимок не снимается, прежний дополняется, и откат ведёт к состоянию до первой попытки. После записи хеш каждого
    файла сверяется с проверенным текстом, а скрипты компилируются; расхождение откатывает файлы из снимка.
    → {ok, stage (`verify` / `snapshot` — ничего не тронуто; `write` — откат), problems, backup, restored}."""
    res = {"ok": False, "stage": "", "problems": [], "backup": "", "restored": False}
    bad = check_engine_set(target, items, extra)
    if bad:
        return {**res, "stage": "verify", "problems": bad}
    rels = [rel for rel, _t in items] + list(gone) + [ENGINE_MARK, KIT_POINTER, ETX.LOCK_FILE]
    stamp = target / "AuroraKnowledgeDB/meta/aurora_version.txt"
    meta = {"from": from_ver, "to": ver, "effects": list(effects),
            "project_stamp": stamp.read_text(encoding="utf-8").strip() if stamp.is_file() else None}
    prev = ETX.read_pending(target)
    snap = ETX.own_snapshot(target, prev.get("backup")) if prev.get("backup") else None
    try:
        if snap is not None:
            ETX.extend_snapshot(target, snap, rels)
            meta["from"] = ETX.clean(prev.get("from") or from_ver)
        else:
            snap = ETX.snapshot(target, rels, meta)
        res["backup"] = str(snap)
        ETX.mark_pending(target, {"from": meta["from"], "to": ver, "backup": str(snap)})
    except OSError as e:
        # Снимки лежат в `~/.aurora/engine-backups`: домашней папки нет, она только для чтения или диск полон —
        # обновление не начинается (ни один файл движка не тронут), а не падает трассировкой.
        return {**res, "stage": "snapshot", "problems": [f"снимок движка не снят ({e}); обновление не начато"]}
    failed = []
    for rel, text in items:
        try:
            _write_engine_file(target / rel, text)
        except OSError as e:
            failed.append(f"{rel}: {e}")
    for rel in gone:
        try:
            (target / rel).unlink(missing_ok=True)
        except OSError as e:
            failed.append(f"{rel}: {e}")
    if not failed:
        failed = ETX.check_installed(target, {rel: ETX.sha256_text(text) for rel, text in items})
    if failed:
        back = ETX.restore(target, snap)
        res.update(stage="write", problems=failed + [f"откат: {b}" for b in back], restored=not back)
        if not back:
            ETX.clear_pending(target)       # движок вернулся целиком; метка остаётся только у неоконченного отката
        return res
    return {**res, "ok": True}


def seal_engine(target: Path, ver: str) -> None:
    """Конец обновления движка: `engine.lock.json` с хешами нынешних файлов и снятая метка `update.pending`."""
    if (target / ENGINE).is_dir() and _linkless(target, ETX.LOCK_FILE) and not _ignored(ETX.LOCK_FILE, load_ignore(target)):
        ETX.write_lock(target, KIT, ver, [p for p in engine_targets() if (target / p).is_file()])
    ETX.clear_pending(target)


def engine_prepare(target: Path, apply: bool = True) -> dict:
    """Подготовить движок проекта на этой машине: пути под `.aurora/` из манифеста, и только они.

    Идемпотентно: файл, совпавший с китом, не пишется. Расходящийся с китом файл движка
    заменяется — копия должна быть целой, одной версии: недостающий файл рядом со старым
    `aurora_common.py` падал бы там, где из кита работал. Исключения: пути из
    `aurora.update_ignore.txt` проект ведёт сам; движок НОВЕЕ кита (по метке копии) только
    достраивается, не откатывается; через ссылки и за пределы проекта не пишется и не удаляется
    (общий движок обновляет полный `kit:update` с предупреждением). Движок в прежней `.opencode/`
    не переезжает — переезд раскладывает данные проекта и идёт полным обновлением.
    → {ok, reason, before, after, created, refreshed, removed, shared, failed, ahead, ignored}.
    """
    res = {"ok": False, "reason": "", "applied": False, "before": "", "after": "", "created": [],
           "refreshed": [], "removed": [], "shared": [], "failed": [], "ahead": False, "ignored": None}
    if not (target / "AuroraKnowledgeDB").is_dir():
        return {**res, "reason": "not_project"}
    st = engine_state(target)
    res.update(before=st["state"], after=st["state"])
    if st["state"] == "unknown":
        return {**res, "reason": "no_kit"}
    if st["legacy"]:
        return {**res, "reason": "legacy"}
    # «новее кита» знает только метка копии: штамп проекта едет через git и может быть новее и кита этой машины
    res["ahead"] = bool(mark_version(target)) and version_key(mark_version(target)) > version_key(st["kit"])
    ignore = load_ignore(target)
    todo, seen = [], set()
    for c in plan(target, project_fields(target)):
        # путь бывает в плане дважды (скрипт модуля лежит и в манифесте, и в его описании)
        if c.kind != "write" or not c.path.startswith(ENGINE + "/") or c.path in seen:
            continue
        seen.add(c.path)
        dst = target / c.path
        if c.external or ".." in Path(c.path).parts or not _linkless(target, c.path) or not _inside(target, dst.parent):
            res["shared"].append(c.path)
            continue
        if res["ahead"] and dst.exists():
            continue
        todo.append((c, not dst.exists()))
    gone = [r for r in retired_paths() if r.startswith(ENGINE + "/") and _linkless(target, r)
            and (target / r).is_file() and not _ignored(r, ignore)]
    res["ok"] = True
    if not apply:
        res.update(created=[c.path for c, new in todo if new], refreshed=[c.path for c, new in todo if not new],
                   removed=gone)
        return res
    lock = ETX.update_lock(target)
    try:
        locked = bool(todo or gone) and lock.acquire(timeout=0.5)
    except OSError as e:
        return {**res, "ok": False, "failed": [f"замок обновления не открыт: {e}"]}
    if (todo or gone) and not locked:
        return {**res, "ok": False, "reason": "update_running", "failed": ["обновление этого проекта уже идёт"]}
    try:
        held = agent_running(target) if (todo or gone) else ""
        if held:
            return {**res, "ok": False, "reason": "agent_running", "held": held,
                    "failed": [f"в проекте идёт прогон агента ({held}): движок подготовится после него"]}
        if todo or gone:
            done = apply_engine(target, [(c.path, c.new_text) for c, _new in todo], gone, kit_version(),
                                copy_version(target) or "?")
            if done["ok"]:
                for c, new in todo:
                    res["created" if new else "refreshed"].append(c.path)
                res["removed"].extend(gone)
            else:
                res["failed"].extend(done["problems"])
        # указатель на кит и метка — тем же правилом: не через ссылку, не в путь, который ведёт проект сам
        for rel, text in ((KIT_POINTER, str(KIT)), (ENGINE_MARK, kit_version())):
            if res["failed"] or _ignored(rel, ignore):
                continue
            if rel == ENGINE_MARK and (res["ahead"] or res["shared"]):
                continue                                  # копия не совпала с китом целиком: версию не объявляем
            if not _linkless(target, rel):
                res["shared"].append(rel)
                continue
            try:
                (target / ENGINE).mkdir(parents=True, exist_ok=True)
                write_text_atomic(target / rel, text + "\n")
            except OSError as e:
                res["failed"].append(f"{rel}: {e}")
        if not res["failed"]:
            if not res["ahead"] and not res["shared"]:
                seal_engine(target, kit_version())        # engine.lock.json и снятая метка
            else:
                ETX.clear_pending(target)                 # движок положен; замок и метку версии у чужой копии не объявляем
    finally:
        if locked:
            lock.release()
    res["applied"] = True
    res["ok"] = not res["failed"]
    res["ignored"] = _git_ignored(target, KIT_POINTER)
    res["after"] = engine_state(target)["state"]
    return res


def engine_report(res: dict, apply: bool) -> list:
    """Что сделала (или сделает) подготовка движка — строки для терминала и для «Консоли» панели."""
    lines = []
    for label, mark, rows in (("Создать", "+", res["created"]), ("Заменить (расходится с китом)", "~", res["refreshed"]),
                              ("Убрать (выведены из движка)", "−", res["removed"])):
        if rows:
            lines.append(f"{label}: {len(rows)}")
            lines += [f"  {mark} {rel}" for rel in rows[:40]]
            if len(rows) > 40:
                lines.append(f"  … ещё {len(rows) - 40}")
    lines += [f"  ⚠️ {rel}: ссылка или путь вне проекта — не тронут (общий движок обновляет полный kit:update)"
              for rel in res["shared"]]
    if res["ahead"]:
        lines.append("  ⚠️ движок проекта новее кита — только достроен, существующие файлы не заменены")
    if not apply:
        done = res["created"] or res["refreshed"] or res["removed"]
        lines.append("\n(dry-run) Ничего не записано. Повторите с --apply." if done
                     else "✅ Движок проекта уже актуален — делать нечего.")
        return lines
    lines += [f"   ⚠️ не вышло: {line}" for line in res["failed"][:40]]
    if len(res["failed"]) > 40:
        lines.append(f"   … ещё {len(res['failed']) - 40}")
    lines.append(f"\n{'✅' if res['ok'] else '⚠️'} Движок проекта подготовлен на этом компьютере: создано "
                 f"{len(res['created'])}, заменено {len(res['refreshed'])}, убрано {len(res['removed'])} "
                 f"файлов в .aurora/. Файлы проекта и git не тронуты. Версия движка → {kit_version()}")
    if res["ignored"] is False:
        lines.append("   ⚠️ .aurora/ не закрыта правилом .gitignore — git увидит её как новую папку; "
                     "правило добавит «Версия» → «Применить»")
    return lines


def run_engine_only(target: Path, apply: bool) -> int:
    """`--engine-only`: достроить `.aurora/` и сказать, что сделано. Файлов проекта и git не касается."""
    if not (target / "AuroraKnowledgeDB").is_dir():
        print(f"⚠️  {target} не похоже на проект Aurora (нет AuroraKnowledgeDB/).", file=sys.stderr)
        return 1
    print(f"Aurora update (только движок) — {target}")
    st = engine_state(target)
    print(f"  движок проекта: {st['state']}" + (f" ({st['copy']})" if st["copy"] else "")
          + f" · kit {st['kit']}\n")
    res = engine_prepare(target, apply)
    if res["reason"] == "legacy":
        print("⚠️  Движок проекта ещё в прежней папке .opencode/: его переезд в .aurora/ делает полное "
              "обновление — раздел «Версия» → «Предпросмотр обновления» → «Применить».", file=sys.stderr)
        return 1
    if not res["ok"] and not res["applied"]:
        print(f"⚠️  Движок не подготовлен: {res['reason']}.", file=sys.stderr)
        return 1
    print("\n".join(engine_report(res, apply)))
    return 0 if res["ok"] else 1


def run_rollback(target: Path, apply: bool) -> int:
    """`--rollback`: вернуть файлы движка проекта к состоянию до последнего обновления (из снимка, BL-LCY-07).

    Откатывается только `.aurora/` (копия движка вне git): файлы самого проекта, которые обновление тоже правило
    (`AGENTS.md`, шаблоны, `.gitignore`, конфиг), живут в git — их возвращает git, а здесь они только названы.
    Без `--apply` — просмотр: что вернётся и что уберётся."""
    if not (target / "AuroraKnowledgeDB").is_dir():
        print(f"⚠️  {target} не похоже на проект Aurora (нет AuroraKnowledgeDB/).", file=sys.stderr)
        return 1
    print(f"Aurora update --rollback — {target}")
    shots = ETX.list_snapshots(target)
    if not shots:
        print("⚠️  Снимков движка этого проекта нет: обновление, которое можно откатить, ещё не делалось "
              f"(снимки лежат в {ETX.backups_root()}).", file=sys.stderr)
        return 1
    snap, man = shots[0]
    files = man.get("files") or {}
    restore_n = sum(1 for rec in files.values() if rec.get("existed"))
    print(f"  снимок {snap.name}: версия до обновления {ETX.clean(man.get('from'))}, после — {ETX.clean(man.get('to'))}")
    print(f"  вернётся файлов движка: {restore_n}; уберётся созданных обновлением: {len(files) - restore_n}")
    effects = [ETX.clean(e, 200) for e in (man.get("effects") if isinstance(man.get("effects"), list) else [])]
    if effects:
        print(f"  Файлы проекта, которые обновление тоже правило ({len(effects)}), в git — откат через git, не отсюда:")
        print("".join(f"     {e}\n" for e in effects[:10]).rstrip("\n"))
        if len(effects) > 10:
            print(f"     … ещё {len(effects) - 10}")
    if not apply:
        print("\n(dry-run) Ничего не возвращено. Повторите с --apply.")
        return 0
    busy = agent_running(target)
    if busy:
        print(f"\n⛔ В проекте идёт прогон агента: {busy}. Откат движка — после него.", file=sys.stderr)
        return 3
    lock = ETX.update_lock(target)
    try:
        taken = lock.acquire(timeout=0.5)
    except OSError as e:
        print(f"\n⛔ Замок обновления проекта не открыт: {e}", file=sys.stderr)
        return 5
    if not taken:
        print("\n⛔ Обновление этого проекта уже идёт. Откат — после него.", file=sys.stderr)
        return 3
    try:
        failed = ETX.restore(target, snap)
        if not failed:
            if ETX.version_text(man.get("project_stamp")):
                stamp_version(target, man["project_stamp"].strip())      # штамп проекта говорит ту версию, что и вернувшийся движок
            ETX.clear_pending(target)
    finally:
        lock.release()
    for line in failed:
        print(f"   ⚠️ не вернулось: {line}", file=sys.stderr)
    if failed:
        return 1
    print(f"\n✅ Движок проекта возвращён к версии {ETX.clean(man.get('from'))}. Проверьте: в панели `kit:doctor`.")
    return 0


# ---------- main ----------

def refresh_hooks(target: Path) -> str:
    """Переставить git-хук, если он наш. → режим, которым переставлен (пусто — не трогали).

    Хук — это установленная КОПИЯ в `.git/hooks/`, а не файл движка. Обновление движка
    везло новый `aurora_hooks.py` и оставляло в проекте хук, поставленный при заведении.
    На живом проекте так и вышло: храповик, переделанный в ките месяцы назад (абсолютный
    счёт → плотность, отказ → предупреждение), туда не доехал ни разу, и человек воевал
    с поведением, которого в ките уже нет.

    Чужой хук не трогаем: его ставил не движок, и перезаписать его молча — потерять
    чужую работу. Режим сохраняем прежний: его выбирал человек.
    """
    hook = target / ".git" / "hooks" / "pre-commit"
    if not hook.is_file():
        return ""
    try:
        text = hook.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    if "линтер базы знаний Авроры" not in text:
        return ""            # чужой хук — не наше дело
    m = re.search(r"режим:\s*([a-z]+)", text)
    mode = m.group(1) if m else "ratchet"
    cp = subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"),
                         "--install", "--mode", mode, "--force"],
                        cwd=str(target), capture_output=True, text=True, encoding="utf-8", errors="replace")
    return mode if cp.returncode == 0 else ""


def tracked_but_ignored(target: Path) -> list:
    """Файлы в git, которые закрывают правила .gitignore ОТ КИТА. Нет git — пусто.

    Только правила кита: свои правила человек пишет сам и знает, что под ними лежит.
    Подсказка «снять с учёта» для его рабочих папок (у одного проекта — сотня файлов
    `Workspaces/…`) звала бы убрать из истории его же работу.
    """
    try:
        p = subprocess.run(["git", "-C", str(target), "-c", "core.quotepath=off", "ls-files", "-ci",
                            "--exclude-standard"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        paths = [l for l in p.stdout.splitlines() if l.strip()] if p.returncode == 0 else []
        if not paths:
            return []
        # Байтами: текстовый режим на Windows превращает `\n` в `\r\n`, и git получал
        # каждый путь с хвостом `\r`.
        v = subprocess.run(["git", "-C", str(target), "-c", "core.quotepath=off", "check-ignore",
                            "-v", "--no-index", "--stdin"],
                           input=("\n".join(paths) + "\n").encode("utf-8"),
                           capture_output=True, timeout=30)
        v_out = v.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return []
    sys.path.insert(0, str(KIT / "scripts"))
    try:
        from install_aurora import GITIGNORE_BLOCK
    except Exception:
        return []
    kit_rules = {l.strip() for l in GITIGNORE_BLOCK.splitlines()
                 if l.strip() and not l.strip().startswith("#")}
    out = []
    for line in v_out.splitlines():
        where, _, path = line.partition("\t")
        pattern = where.split(":", 2)[-1] if where.count(":") >= 2 else ""
        if pattern.strip() in kit_rules and path.strip():
            out.append(path.strip())
    return out


def refresh_gitignore(target: Path) -> list:
    """Дописать в .gitignore правила кита, появившиеся после заведения проекта.

    Тот же класс, что и с хуком: правило живёт в ките, а в проекте лежит копия, снятая
    при установке. Обновление движка возило новый установщик и не трогало файл — и
    `.aurora/state/` остался вне игнора на проектах, заведённых до этого правила.
    Замок агента попал под контроль версий и после каждого прогона оставлял дерево
    грязным; чекпойнт агента делает `git add -A` и утащил бы его в историю как работу.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    try:
        from install_aurora import merge_gitignore
    except Exception:
        return []
    try:
        return merge_gitignore(target / ".gitignore")
    except (OSError, UnicodeDecodeError):
        return []


RUNLOG_REL = "AuroraKnowledgeDB/meta/run_log.md"


def untrack_runlog(target: Path) -> bool:
    """Снять журнал запусков с учёта git, оставив файл на диске (1.168.0).

    Правило .gitignore не снимает с учёта то, что уже в истории: журнал продолжал бы
    меняться в каждом коммите. Здесь кит сам решил вывести его из git — и снимает сам,
    только этот файл. Удаление из индекса уйдёт со следующим коммитом проекта."""
    try:
        tracked = subprocess.run(["git", "-C", str(target), "ls-files", "--error-unmatch", "--",
                                  RUNLOG_REL], capture_output=True, timeout=30).returncode == 0
        if not tracked:
            return False
        return subprocess.run(["git", "-C", str(target), "rm", "--cached", "-q", "--", RUNLOG_REL],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def backfill_kb_updated(target: Path) -> str:
    """Дата обновления базы для проекта, где её ещё нет: из истории прогонов этой машины
    (`.aurora/runs`). → дата полного обновления или пусто."""
    sys.path.insert(0, str(KIT / "scripts"))
    try:
        import kb_updated as KU
        rec = KU.backfill(target, str(target / ".aurora" / "runs"))
    except Exception:  # noqa: BLE001 — дата не повод ронять обновление движка
        return ""
    return (rec or {}).get("updated", "")


def refresh_gitattributes(target: Path) -> list:
    """Дописать в .gitattributes правила кита — сейчас одно: пусковой .bat без перевода строк.

    Обновление кладёт start-aurora.bat с CRLF, а git с core.autocrlf=input сохранял его в
    истории проекта с LF: на Mac этого не видно, а на Windows клон проекта получал
    пусковой файл, в котором cmd.exe не находит меток goto. У самого кита правило есть
    давно; проекты его не получали.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    try:
        from install_aurora import merge_gitignore, GITATTRIBUTES_BLOCK
    except Exception:
        return []
    try:
        return merge_gitignore(target / ".gitattributes", GITATTRIBUTES_BLOCK)
    except (OSError, UnicodeDecodeError):
        return []


def config_defaults(target: Path, ignore: list) -> tuple:
    """(новый текст, что записать) — значения доверия по умолчанию для конфига проекта.

    Настройка доверия по умолчанию — обычная настройка проекта, и видно её должно быть в
    `aurora.config.yaml`, а не угадывать по поведению движка. Пишется только туда, где
    список пуст или ключа нет; заданное проектом не трогается.
    """
    from fnmatch import fnmatch
    cfg = target / "aurora.config.yaml"
    if not cfg.is_file() or any(fnmatch("aurora.config.yaml", p) for p in ignore):
        return "", []
    sys.path.insert(0, str(KIT / "scripts"))
    try:
        from aurora_setup import fill_trust_defaults
    except Exception:                                    # noqa: BLE001
        return "", []
    try:
        text = cfg.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return "", []        # не UTF-8: не угадываем, что в нём, и обратно не пишем
    new, done = fill_trust_defaults(text)
    return (new, done) if new != text else ("", [])


CATALOG_REL = "AuroraKnowledgeDB/meta/card_types.md"      # каталог типов карточек: генерируется (card_types.write_catalog)


def catalog_ignored(ignore: list) -> bool:
    from fnmatch import fnmatch
    return any(fnmatch(CATALOG_REL, p) for p in ignore)


def catalog_pending(target: Path, ignore: list, seeds: list) -> bool:
    """Нужно ли пересобрать каталог типов карточек: файла нет, он отстал от шаблонов или от блока `cards:` конфига,
    либо обновление доставит шаблон, которого реестр проекта ещё не видит (новый тип или версия новее). Каталог — не
    чужой файл, а сборка движка: пересобирает его обновление (вместе с `kb:cards --write --apply`), если проект не
    оставил его себе в `aurora.update_ignore.txt`. Шаблон, который реестр и так видит (запасной путь — шаблоны кита),
    каталога не меняет."""
    if catalog_ignored(ignore):
        return False
    sys.path.insert(0, str(KIT / "scripts"))
    try:
        import card_types as CT
        reg = CT.safe_registry(str(target), kit_root=str(KIT))
        if CT.catalog_state(str(target), reg, kit_root=str(KIT)) in ("missing", "stale"):
            return True
        for c in seeds:
            m = CT.TEMPLATE_NAME_RE.match(Path(c.path).name)
            if not (m and c.path.startswith(("TemplatesCommon/", "Templates/"))):
                continue
            seen = reg.types.get(m.group("type"))
            if seen is None or CT.version_key(m.group("version")) > CT.version_key(seen.version):
                return (target / "AuroraKnowledgeDB").is_dir()
        return False
    except Exception:  # noqa: BLE001 — каталог не повод ронять обновление движка
        return False


def rebuild_catalog(target: Path, ignore: list) -> bool:
    """Собрать каталог типов карточек после того, как шаблоны и конфиг на месте. → записан ли."""
    if catalog_ignored(ignore):
        return False
    sys.path.insert(0, str(KIT / "scripts"))
    try:
        import card_types as CT
        return CT.write_catalog(str(target), kit_root=str(KIT))
    except Exception as e:  # noqa: BLE001 — каталог не повод ронять обновление движка
        print(f"   ⚠️ каталог типов карточек не собран: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def run(target: Path, apply: bool, structure_only: bool = False, allow_downgrade: bool = False):
    """Обновление проекта. Запись (`--apply`) идёт под замком проекта: кнопка, Cron и вторая панель не обновляют один
    проект одновременно (BL-LCY-07). Просмотр (dry-run) замка не берёт."""
    if apply and not allow_downgrade and (target / "AuroraKnowledgeDB").is_dir():
        # Старый кит поверх нового проекта понизил бы штамп (он едет через git к коллегам) и поставил старый движок на
        # данные новее его: обновление не пишет ничего (даже замка), пока человек не скажет, что откат осознанный.
        st = engine_status(target, kit_version(), kit_engine_api(KIT))
        if st["status"] == "ahead" or st["stamp_ahead"]:
            print(f"\n⛔ {st['reason'] if st['status'] == 'ahead' else st['stamp_reason']}.\n   Ничего не записано. Обновите кит "
                  "(git pull в папке кита); осознанный откат кита и движка — `--allow-downgrade`.", file=sys.stderr)
            return 4
    if not apply or structure_only or not (target / "AuroraKnowledgeDB").is_dir():
        return _run(target, apply, structure_only, allow_downgrade)
    lock = ETX.update_lock(target)
    try:
        taken = lock.acquire(timeout=0.5)
    except OSError as e:
        print(f"\n⛔ Замок обновления проекта не открыт: {e}", file=sys.stderr)
        return 5
    if not taken:
        print("\n⛔ Обновление этого проекта уже идёт (другая панель, Cron или терминал). Повторите после него.",
              file=sys.stderr)
        return 3
    try:
        return _run(target, apply, structure_only, allow_downgrade)
    finally:
        lock.release()


def _run(target: Path, apply: bool, structure_only: bool = False, allow_downgrade: bool = False):
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
    st = engine_status(target, kv, kit_engine_api(KIT))
    if st["status"] == "ahead" or st["stamp_ahead"]:    # запись без `--allow-downgrade` отклонена раньше, в `run`
        why = st["reason"] if st["status"] == "ahead" else st["stamp_reason"]
        if not apply:
            print(f"⚠️  {why}. `--apply` откажет без `--allow-downgrade`.\n")
        else:
            print(f"⚠️  {why}. Понижение разрешено флагом --allow-downgrade.\n")

    new_dirs = missing_dirs(target)
    if new_dirs:
        print(f"Недостающие папки схемы к созданию: {len(new_dirs)}")
        for d in new_dirs:
            print(f"  + {d}/")
        print()
    outside = outside_mirror_dirs(target)
    for d in outside:
        print(f"⚠️  Путь зеркала вне проекта — папка не создана: {d}")
    if outside:
        print()

    # режим «только структура»: папки + штамп версии, движок не трогаем
    if structure_only:
        guides = FG.plan(target, KIT)
        print_guides(guides)
        if not new_dirs and pv == kv and not guides:
            print("✅ Структура актуальна, версия совпадает — изменений нет.")
            return 0
        if not apply:
            print("(dry-run) Ничего не создано. Повторите с --apply.")
            return 0
        for d in new_dirs:
            p = target / d
            p.mkdir(parents=True, exist_ok=True)
            (p / ".gitkeep").touch()
        guides = FG.plan(target, KIT)
        FG.apply(target, guides)
        stamp_version(target, kv)
        print(f"✅ Создано папок: {len(new_dirs)}, описаний папок: {len(guides)}. Версия → {kv}. "
              "Движок не тронут.")
        return 0

    changes = plan(target, f)
    for line in SKIPPED_CONNECTORS:
        print(f"⚠️  Модуль источников пропущен: {line}\n")
    writes = [c for c in changes if c.kind == "write"]
    seeds = [c for c in changes if c.kind == "seed-new"]
    retired = [r for r in retired_paths() if (target / r).is_file()]
    cfg_text, cfg_done = config_defaults(target, ignored)
    rp = relayout_plan(target)
    # Файлы OpenCode и чужое остаются в `.opencode/` навсегда — это не переезд.
    moving = bool(rp["moves"] or rp["drop"] or rp["clash"])
    guides = FG.plan(target, KIT)
    catalog = catalog_pending(target, ignored, seeds)

    if not changes and not new_dirs and not retired and not cfg_done and not moving and not guides and not catalog:
        print("✅ Движок и структура уже актуальны — изменений нет.")
        if apply:
            # Правила .gitignore/.gitattributes кит дописывает в конце прогона «по возможности»
            # (отказ записи обновление не останавливает). Оборванный прогон успел поставить
            # движок, а правил — нет: повтор видел «всё актуально» и штамповал версию, и правила
            # не появлялись, пока кит не изменит что-то ещё. Слияние идемпотентно: недорого
            # проверить и здесь.
            #
            # Прогон, не дошедший до штампа (`pv != kv`), не сделал и остальных хвостовых шагов:
            # хук, снятие журнала с учёта git, дата обновления базы. Все они идемпотентны.
            late = refresh_gitignore(target) + refresh_gitattributes(target)
            if late:
                print(f"   Дописано правил .gitignore/.gitattributes: {len(late)}")
            if pv != kv:
                hook = refresh_hooks(target)
                if hook:
                    print(f"   Хук обновлён ({hook})")
                if untrack_runlog(target):
                    print("   Журнал запусков снят с учёта git (файл на месте)")
                kb_date = backfill_kb_updated(target)
                if kb_date:
                    print(f"   Дата обновления базы из истории прогонов: {kb_date}")
        if apply:
            stamp_engine(target, kv)       # движок совпал с китом: метка копии не отстаёт от штампа
        if apply:
            seal_engine(target, kv)        # замок движка с хешами; метка оборванного обновления, если была, снимается
        if apply and pv != kv:
            stamp_version(target, kv)      # штамп проекта — последним: он говорит «обновление дошло до конца»
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

    print_relayout(rp)
    print(f"Инженерные файлы к перезаписи: {len(writes)}")
    for c in writes:
        tag = "  ⚠️shared" if c.external else ""
        print(f"  ~ {c.path}   [{c.note}]{tag}")
    if seeds:
        print(f"\nШаблоны/промпты (вариант 1 — рядом как .new, руками сравнить): {len(seeds)}")
        for c in seeds:
            print(f"  + {c.path}   [{c.note}]")
    if retired:
        print(f"\nВыведены из движка — будут удалены: {len(retired)}")
        for r in retired:
            print(f"  − {r}")

    if cfg_done:
        print(f"\nКонфиг проекта — значения доверия по умолчанию: {', '.join(cfg_done)}")
    print_guides(guides)
    if catalog:
        print(f"\nКарточки базы — каталог типов будет пересобран: {CATALOG_REL}")

    # Файлы движка (`.aurora/`) кладутся одной операцией (`apply_engine`): проверка до записи, снимок, метка, сверка.
    # Движок в прежней `.opencode/` (переезд) и пути через ссылки — прежним порядком, без снимка (но в проверке набора
    # их тексты участвуют: общий файл будет перезаписан следом).
    engine_items, gone, linked_items = [], [], []
    if not moving:
        for c in writes:
            if not c.path.startswith(ENGINE + "/"):
                continue
            if not c.external and ".." not in Path(c.path).parts and _linkless(target, c.path):
                engine_items.append((c.path, c.new_text))
            else:
                linked_items.append((c.path, c.new_text))
        gone = [r_ for r_ in retired if r_.startswith(ENGINE + "/") and _linkless(target, r_)]
    if not apply:
        bad = check_engine_set(target, engine_items, linked_items) if engine_items else []
        if bad:
            print("\n⚠️  Новый набор файлов движка не пройдёт проверку до записи — `--apply` откажет (код 5):")
            print("\n".join(f"   - {line}" for line in bad[:10]))
        print("\n(dry-run) Ничего не записано. Повторите с --apply, чтобы применить.")
        return 0
    busy = agent_running(target) if (moving or engine_items or gone) else ""
    if busy:
        # Переезд и замена скриптов перекладывают движок из-под работающего агента: один прогон двумя версиями — худшее.
        print(f"\n⛔ В проекте идёт прогон агента: {busy}. Обновление движка — после него.",
              file=sys.stderr)
        return 3
    prev = ETX.read_pending(target)         # замок обновления держим мы сами: метка — след прежнего, оборванного запуска
    if prev:
        print(f"\n⚠️  Прошлое обновление движка ({ETX.clean(prev.get('from'))} → {ETX.clean(prev.get('to'))}) оборвалось на середине: "
              "достраиваю. Откатить к состоянию до него — `update --rollback`.")
    if engine_items or gone:
        effects = [c.path for c in writes + seeds if not c.path.startswith(ENGINE + "/")] \
            + [r_ for r_ in retired if not r_.startswith(ENGINE + "/")]
        effects += ["aurora.config.yaml, .gitignore, .gitattributes, хук git, описания папок, каталог типов карточек "
                    "(те из них, что обновление изменило)"]
        done = apply_engine(target, engine_items, gone, kv, pv, effects, linked_items)
        if not done["ok"]:
            untouched = done["stage"] in ("verify", "snapshot")
            print(f"\n⛔ Движок проекта не обновлён ({'проверка до записи' if done['stage'] == 'verify' else 'снимок' if done['stage'] == 'snapshot' else 'запись'}): "
                  + ("проект не тронут." if untouched else
                     ("файлы возвращены из снимка." if done["restored"] else
                      "откат неполный — `update --rollback`.")), file=sys.stderr)
            for line in done["problems"][:20]:
                print(f"   - {line}", file=sys.stderr)
            if done["stage"] == "verify" and _ignored(ENGINE + "/scripts/aurora_common.py", ignored):
                print("   Ядро (aurora_common.py) закреплено в aurora.update_ignore.txt и не обновляется, а новые скрипты "
                      "ждут его новых имён: уберите строку из update_ignore или обновите своё ядро сами.", file=sys.stderr)
            return 5 if untouched else 6
    engine_done = {rel for rel, _t in engine_items}
    failed = relayout_moves(target, rp) if moving else []
    rewritten = relayout_rewrite(target)
    for d in new_dirs:
        p = target / d
        p.mkdir(parents=True, exist_ok=True)
        (p / ".gitkeep").touch()
    for c in writes + seeds:
        if c.path in engine_done:
            continue                 # уже лежит: записано и сверено `apply_engine`
        dst = target / c.path
        dst.parent.mkdir(parents=True, exist_ok=True)
        # .bat — с CRLF при любой системе (с одними LF cmd.exe промахивается мимо меток goto).
        text = c.new_text
        if dst.suffix == ".bat":
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        dst.write_bytes(text.encode("utf-8"))
        if dst.suffix in (".command", ".sh"):
            dst.chmod(0o755)     # без бита исполнения двойной щелчок не сработает
    for r in retired:
        if r in gone:
            continue
        # Второе обновление (кнопка и Cron разом) могло убрать файл раньше: это не ошибка.
        (target / r).unlink(missing_ok=True)
    if cfg_done:
        write_text_atomic(target / "aurora.config.yaml", cfg_text)
    (target / ".aurora").mkdir(parents=True, exist_ok=True)
    write_text_atomic(target / ".aurora/kit_path.txt", str(KIT) + "\n")
    if moving:
        failed += relayout_cleanup(target, rp)
    guides = FG.plan(target, KIT)        # после папок схемы и переезда: им тоже описания
    FG.apply(target, guides)
    catalog_done = rebuild_catalog(target, ignored)        # после шаблонов и конфига: каталог собирается по ним
    refreshed = refresh_hooks(target)
    ignored = refresh_gitignore(target)
    untracked = untrack_runlog(target)
    kb_date = backfill_kb_updated(target)
    attrs = refresh_gitattributes(target)
    stamp_engine(target, kv)
    seal_engine(target, kv)                # замок движка с хешами файлов; метка update.pending снимается
    stamp_version(target, kv)              # штамп проекта — последним: по нему видно, что обновление дошло до конца
    print(f"\n✅ Применено: {len(new_dirs)} папок, {len(writes)} перезаписей, "
          f"{len(seeds)} .new-файлов, {len(retired)} удалено"
          + (f", хук обновлён ({refreshed})" if refreshed else "")
          + (f", в .gitignore дописано правил: {len(ignored)}" if ignored else "")
          + (", журнал запусков снят с учёта git (файл на месте)" if untracked else "")
          + (f", дата обновления базы из истории прогонов: {kb_date}" if kb_date else "")
          + (f", в .gitattributes дописано правил: {len(attrs)}" if attrs else "")
          + (f", конфиг: {', '.join(cfg_done)}" if cfg_done else "")
          + (f", описаний папок: {len(guides)}" if guides else "")
          + (", каталог типов карточек пересобран" if catalog_done else "")
          + (f", пути движка поправлены в {len(rewritten)} файлах проекта" if rewritten else "")
          + (f", переезд в .aurora/: перенесено {len(rp['moves'])}, "
             f"убрано копий кита {len(rp['drop'])}" if moving else "")
          + f". Версия → {kv}")
    for line in failed:
        print(f"   ⚠️ не вышло: {line}")
    print("   Проверьте: в панели `kit:doctor`, затем git diff")
    stuck = tracked_but_ignored(target)
    if stuck:
        # Правило .gitignore не снимает с учёта то, что уже попало в историю: такие файлы
        # продолжают меняться в каждом коммите. Снять — решение о git проекта, не движка.
        def top(p: str) -> str:
            parts = p.split("/")
            # `.claude/skills/` остаётся в git — снимаем только соседей, а не всю `.claude`.
            n = 2 if parts[0] == ".claude" else 1 if parts[0].startswith(".") else 3
            return "/".join(parts[:n])
        tops = sorted({top(p) for p in stuck})
        print(f"   В git уже лежат файлы, которые теперь закрыты .gitignore: {len(stuck)}. "
              "Снять с учёта, не трогая диск:\n"
              + "\n".join(f"     git rm -r --cached -q \"{t}\"" for t in tops[:5]))
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
          "<проект>/.aurora/kit_path.txt", file=sys.stderr)
    return False


def main():
    ap = argparse.ArgumentParser(description="Обновить движок Aurora в проекте по манифесту")
    ap.add_argument("target", nargs="?", default=".", help="Корень проекта")
    ap.add_argument("--apply", action="store_true", help="Записать изменения (иначе dry-run)")
    only = ap.add_mutually_exclusive_group()
    only.add_argument("--structure-only", action="store_true",
                      help="Только досоздать недостающие папки схемы + штамп версии; движок не трогать")
    only.add_argument("--engine-only", action="store_true",
                      help="Только движок в .aurora/ этой машины; файлы проекта и git не трогаются")
    ap.add_argument("--allow-downgrade", action="store_true",
                    help="Разрешить обновление, когда движок или штамп проекта НОВЕЕ кита (осознанный откат кита)")
    only.add_argument("--rollback", action="store_true",
                      help="Вернуть движок проекта (.aurora/) к состоянию до последнего обновления из снимка; "
                           "без --apply — только просмотр")
    a = ap.parse_args()
    target = Path(a.target).expanduser().resolve()
    if a.rollback:                                  # откату нужны снимки в ~/.aurora, а не кит
        return run_rollback(target, a.apply)
    if not kit_is_reachable():
        return 2
    if a.engine_only:
        return run_engine_only(target, a.apply)
    return run(target, a.apply, a.structure_only, a.allow_downgrade)


if __name__ == "__main__":
    sys.exit(main())
