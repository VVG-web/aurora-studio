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

Панель: `kit:update`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations
import argparse, difflib, json, re, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aurora_common import utc_today  # noqa: E402 — дата в UTC, одна на движок
import folder_guides as FG  # noqa: E402 — описания папок проекта (README.md)

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


def mirror_dirs(target: Path):
    """Папки зеркал подключённых модулей: их нет в схеме, их заявляет реестр."""
    sys.path.insert(0, str(KIT / "scripts"))
    try:
        import sources_registry as R
    except ImportError:
        return []
    return [i["path"] for i in R.instances(str(target)) if i["path"]]


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
                p.write_text(fixed, encoding="utf-8")
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
        text = ui.read_text(encoding="utf-8")
        fixed = re.sub(r"\.(?:opencode|aurora)/skills/", ".claude/skills/", text)
        if fixed != text:
            ui.write_text(fixed, encoding="utf-8")
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
    from aurora_common import pid_alive
    for base in (ENGINE, LEGACY):
        lock = target / base / "state" / "agent.lock"
        try:
            held = json.loads(lock.read_text(encoding="utf-8"))
            if pid_alive(int(held.get("pid") or 0)):
                return f"{held.get('task')} (pid {held.get('pid')})"
        except (OSError, ValueError, TypeError):
            continue
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
                m = json.loads(man.read_text(encoding="utf-8"))
                diff_or_new(f".aurora/connectors/{m['id']}.json",
                            man.read_text(encoding="utf-8"))
                script = m.get("run", {}).get("script", "")
                if script:
                    # скрипт модуля может лежать рядом с манифестом (доустановленный
                    # модуль) или в scripts/ kit'а (встроенный — его же импортируют
                    # панель и publish_doc)
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
    vf.write_text(ver + "\n", encoding="utf-8")


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
    except OSError:
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
    except OSError:
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
    text = cfg.read_text(encoding="utf-8")
    new, done = fill_trust_defaults(text)
    return (new, done) if new != text else ("", [])


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
    writes = [c for c in changes if c.kind == "write"]
    seeds = [c for c in changes if c.kind == "seed-new"]
    retired = [r for r in retired_paths() if (target / r).is_file()]
    cfg_text, cfg_done = config_defaults(target, ignored)
    rp = relayout_plan(target)
    # Файлы OpenCode и чужое остаются в `.opencode/` навсегда — это не переезд.
    moving = bool(rp["moves"] or rp["drop"] or rp["clash"])
    guides = FG.plan(target, KIT)

    if not changes and not new_dirs and not retired and not cfg_done and not moving and not guides:
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

    if not apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply, чтобы применить.")
        return 0

    busy = agent_running(target) if moving else ""
    if busy:
        # Переезд перекладывает состояние прогона из-под работающего агента.
        print(f"\n⛔ В проекте идёт прогон агента: {busy}. Переезд движка — после него.",
              file=sys.stderr)
        return 3
    failed = relayout_moves(target, rp) if moving else []
    rewritten = relayout_rewrite(target)
    for d in new_dirs:
        p = target / d
        p.mkdir(parents=True, exist_ok=True)
        (p / ".gitkeep").touch()
    for c in writes + seeds:
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
        (target / r).unlink()
    if cfg_done:
        (target / "aurora.config.yaml").write_text(cfg_text, encoding="utf-8")
    (target / ".aurora").mkdir(parents=True, exist_ok=True)
    (target / ".aurora/kit_path.txt").write_text(str(KIT) + "\n", encoding="utf-8")
    if moving:
        failed += relayout_cleanup(target, rp)
    guides = FG.plan(target, KIT)        # после папок схемы и переезда: им тоже описания
    FG.apply(target, guides)
    refreshed = refresh_hooks(target)
    ignored = refresh_gitignore(target)
    untracked = untrack_runlog(target)
    kb_date = backfill_kb_updated(target)
    attrs = refresh_gitattributes(target)
    stamp_version(target, kv)
    print(f"\n✅ Применено: {len(new_dirs)} папок, {len(writes)} перезаписей, "
          f"{len(seeds)} .new-файлов, {len(retired)} удалено"
          + (f", хук обновлён ({refreshed})" if refreshed else "")
          + (f", в .gitignore дописано правил: {len(ignored)}" if ignored else "")
          + (", журнал запусков снят с учёта git (файл на месте)" if untracked else "")
          + (f", дата обновления базы из истории прогонов: {kb_date}" if kb_date else "")
          + (f", в .gitattributes дописано правил: {len(attrs)}" if attrs else "")
          + (f", конфиг: {', '.join(cfg_done)}" if cfg_done else "")
          + (f", описаний папок: {len(guides)}" if guides else "")
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
    ap.add_argument("--structure-only", action="store_true",
                    help="Только досоздать недостающие папки схемы + штамп версии; движок не трогать")
    a = ap.parse_args()
    if not kit_is_reachable():
        return 2
    return run(Path(a.target).expanduser().resolve(), a.apply, a.structure_only)


if __name__ == "__main__":
    sys.exit(main())
