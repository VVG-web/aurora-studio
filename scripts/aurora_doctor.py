#!/usr/bin/env python3
"""aurora_doctor.py — check Aurora project onboarding readiness.

Run from project root:
  python3 .aurora/scripts/aurora_doctor.py
  python3 .aurora/scripts/aurora_doctor.py --structure   # подробно по структуре папок

Проверяет конфиг, скиллы, секреты в git, версию движка и **фиксированную структуру папок**
(схема движка `.aurora/structure_dirs.txt` одинакова во всех проектах Авроры: свои типы
артефактов и свои разделы базы не заводятся — для всего нестандартного есть `Workspaces/`).

Exit 0 if OK / warnings only; 1 if blocking errors.

Панель: `kit:doctor`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import RETIRED_FIELDS, config_list, python_status, root_link_escapes, scan_secret_files
from pathlib import Path

ROOT = Path.cwd()
CONFIG = ROOT / "aurora.config.yaml"
ENV_LOCAL = ROOT / ".env.aurora.local"
ENV_EXAMPLE = ROOT / "aurora.env.local.example"
SKILL = ROOT / ".aurora" / "skills" / "aurora-vault" / "SKILL.md"
AGENTS = ROOT / "AGENTS.md"

def load_yaml_lite(path: Path) -> dict:
    """Minimal YAML subset reader (no PyYAML dependency): enough for doctor checks."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    # Разбор без PyYAML: конфиг у нас фиксированной формы, и вся кодовая база
    # читает его одинаково — второй путь разбора расходился бы молча.
    out: dict = {"_raw": True}
    if re.search(r"(?m)^project:\s*$", text):
        out["project"] = True
    if re.search(r"(?m)^atlassian:\s*$", text):
        out["atlassian"] = True
    if re.search(r"(?m)^skills:\s*$", text):
        out["skills"] = True
    m = re.search(r"(?m)^\s+space:\s*[\"']?(\S+?)[\"']?\s*$", text)
    if m:
        out["_space"] = m.group(1)
    m = re.search(r"(?m)^\s+project_key:\s*[\"']?(\S+?)[\"']?\s*$", text)
    if m:
        out["_jira"] = m.group(1)
    return out


def scan_secrets() -> list[str]:
    """Секреты в том, что уедет в git: конфиг, AGENTS.md, навыки, mcp.json, боты.

    Правила и список мест — общие с линтером базы и хуками (`aurora_common.scan_secret_files`):
    раньше здесь было три своих шаблона и папка `.aurora/skills`, которой в git нет.
    """
    hits = []   # через .append: реестр переводов kit_i18n собирает сообщения доктора именно по вызовам append
    for rel, rule, _line in scan_secret_files(str(ROOT)):
        hits.append(f"{rel}: возможный секрет ({rule})")
    return hits


STRUCTURE_FILE = ROOT / ".aurora" / "structure_dirs.txt"
# Папки самого движка и системы контроля версий — они часть инструмента, не схемы.
# Всё остальное вне схемы допустимо ТОЛЬКО если закрыто .gitignore (см. git_ignored).
# Харнессы — папки агентов и редакторов (у каждого свои, вне git); `.opencode` — ещё и
# прежнее место движка до 1.158.0.
ENGINE_DIRS_TOP = {".git", ".aurora", ".claude", ".cursor", ".opencode", ".codex", ".gemini",
                   ".windsurf", ".agents"}
# Внутри этих корней второй уровень задаётся движком (свои подпапки заводить нельзя).
MANAGED_ROOTS = ("Artifacts", "AuroraKnowledgeDB", "Raw", "Sources", "Deliverables")


def artifact_dirs() -> dict:
    """{папка: вид документа} — куда проект складывает свои артефакты.

    Схему папок задаёт движок, но виды документов объявляет проект: у одного заказчика
    проектное решение, у другого регламент, и папка результата у каждого своя. Реестр
    в `aurora.config.yaml` — такое же основание для папки, как реестр модулей для
    зеркала. Иначе выходит ловушка: человек объявил папку в панели, движок её создал,
    а doctor тут же назвал её нарушением схемы.
    """
    sys.path.insert(0, str(ROOT / ".aurora" / "scripts"))
    try:
        import make_kinds as MK
    except ImportError:
        return {}
    out = {}
    for kind, rec in (MK.read_kinds(str(ROOT)) or {}).items():
        path = (rec.get("out") or "").strip().strip('"').rstrip("/")
        if path and not os.path.isabs(path):
            out[path] = kind
    return out


def project_dirs() -> dict:
    """{папка: "проект"} — структурные папки, которые проект объявил своими.

    Схема движка одна на все проекты и меняется выпуском кита. Но у проекта бывает своя
    папка, которой нет у других: наследие прежней базы, вложения, особый раздел. Добавить её
    в схему кита значит завести пустую такую же во всех проектах; переложить в `Workspaces/`
    — сломать ссылки на её содержимое. Объявление в `aurora.config.yaml`
    (`extra_structure_dirs: [...]`) — такое же основание для папки, как реестр артефактов:
    законна в этом проекте и только в нём. Абсолютные пути и выход за пределы проекта не
    принимаются: это объявление папки, а не лазейка в проверке.
    """
    out = {}
    for raw in config_list("extra_structure_dirs"):
        path = raw.replace("\\", "/").strip()
        if not path or path.startswith("/") or os.path.isabs(path):
            continue
        path = path.strip("/")
        if not path or ".." in path.split("/"):
            continue
        out[path] = "проект"
    return out


def mirror_owners() -> dict:
    """{папка зеркала: модуль} — что в `Sources/` заявлено подключёнными модулями.

    Схема папок фиксирована движком, но зеркала в неё не входят: их набор зависит от
    того, чем команда пользуется. Поэтому легитимность папки в `Sources/` определяет
    реестр модулей, а не список в файле схемы.
    """
    sys.path.insert(0, str(ROOT / ".aurora" / "scripts"))
    try:
        import sources_registry as R
    except ImportError:
        return {}
    return R.mirror_paths(str(ROOT))


def registry_skipped() -> list:
    """Строки о манифестах модулей источников, которые реестр пропустил (кривой `connector.json`).

    Реестр не падает на таком файле, а копит причину; без этой строки модуль просто молча
    исчез бы из проекта. Справка не имеет права ронять доктора.
    """
    try:
        import sources_registry as R
        return list(getattr(R, "registry_report", lambda: [])())
    except Exception:  # noqa: BLE001
        return []


def read_structure() -> list[str]:
    if not STRUCTURE_FILE.is_file():
        return []
    out = []
    for line in STRUCTURE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line.rstrip("/"))
    return out


def check_case_drift(schema: list) -> list:
    """Папки схемы, чьё написание в индексе git расходится с диском.

    macOS/Windows нечувствительны к регистру и прячут расхождение: на диске `Raw/`,
    в индексе `raw/`. На Linux/CI это станет двумя разными папками, а скрипты движка
    (они ходят по путям схемы) перестанут находить контент. См. conventions.md, правило 1.
    """
    import subprocess
    tops = sorted({p.split("/")[0] for p in schema})
    try:
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if out.returncode != 0:
            return []
    except Exception:
        return []
    indexed = {line.strip('"').split("/")[0] for line in out.stdout.splitlines() if "/" in line}
    drift = []
    for top in tops:
        if not (ROOT / top).is_dir() or top in indexed:
            continue
        same_ci = [i for i in indexed if i.lower() == top.lower()]
        if same_ci:
            drift.append(f"{top}/ на диске ↔ {same_ci[0]}/ в git")
    return drift


def git_ignored(paths: list, rules_only: bool = False) -> set:
    """Какие из путей закрыты `.gitignore` (спрашиваем сам git — он знает все слои).

    Правило Авроры: **что в .gitignore, то вне схемы допустимо**. Такие папки не едут
    в репозиторий, значит не разъезжаются между проектами: кэши линтеров, состояние
    редакторов и инструментов, локальные песочницы. Схему стережём только там, где
    содержимое попадает в git и в контекст модели.

    `rules_only=True` — спросить только правила, не глядя в индекс: git по умолчанию
    отвечает «не игнорируется» про уже отслеживаемые пути, и папка с давно закоммиченным
    мусором выглядит как нарушение схемы, хотя правило для неё есть.
    """
    if not paths:
        return set()
    import subprocess
    # Пути идут через NUL (`-z`) и байтами. Без этого — две беды на ровном месте: git
    # печатает не-ASCII пути в кавычках с восьмеричными кодами («\320\241…»), и папка по-русски,
    # закрытая правилом, не узнавалась; а текстовый режим на Windows превращает `\n` в
    # `\r\n`, и каждый путь приходил в git с хвостом `\r`.
    try:
        out = subprocess.run(["git", "-c", "core.quotepath=off", "check-ignore", "--stdin", "-z"]
                             + (["--no-index"] if rules_only else []),
                             input=("\0".join(paths) + "\0").encode("utf-8"),
                             capture_output=True, timeout=60)
    except Exception:
        return set()
    return {p.rstrip("/") for p in out.stdout.decode("utf-8", "replace").split("\0") if p}


def retired_fields_in_seeds() -> list:
    """Выведенные из схемы поля в шаблонах и промптах.

    Карточки чистит `kb:retire`, но шаблон — источник новых карточек: пока поле стоит
    там, оно вернётся в базу с первой же созданной карточкой.
    """
    hits = []
    for root in ("Templates", "Prompts"):
        base = ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            found = [f for f in RETIRED_FIELDS if re.search(rf"^{f}\s*:", text, re.M)]
            if re.search(r"^status\s*:\s*\"?canonical", text, re.M):
                found.append("status: canonical")
            if found:
                hits.append(f"{path.relative_to(ROOT)} — {', '.join(found)}")
    return hits


def privacy_mode() -> str:
    """`privacy.scrub` из конфига: off / report / mask (по умолчанию report)."""
    if not CONFIG.exists():
        return "report"
    m = re.search(r"^\s*scrub\s*:\s*\"?(off|report|mask)\"?\s*$",
                  CONFIG.read_text(encoding="utf-8", errors="ignore"), re.M)
    return m.group(1) if m else "report"


FILE_LIKE_RE = re.compile(r"\.[A-Za-z][A-Za-z0-9]{1,4}$")


def sort_extra(paths: list) -> tuple:
    """Разобрать папки вне схемы по случаям. → (путь файла, пустые, прочие).

    Одно сообщение на все случаи вводило в заблуждение. Каталог `Artifacts/Реестр.md`
    назывался «структурной папкой» с советом узаконить его, хотя это имя файла, принятое
    за папку. Пустая папка, которой нет даже в git, числилась блокером. Разные случаи
    лечатся по-разному — и называются по-разному.
    """
    file_like, empty, other = [], [], []
    for rel in paths:
        path = ROOT / rel
        if FILE_LIKE_RE.search(path.name):
            file_like.append(rel)
        elif not any(f.is_file() and not f.name.startswith(".") for f in path.rglob("*")):
            empty.append(rel)
        else:
            other.append(rel)
    return file_like, empty, other


def nearest_standard(rel: str, schema) -> str:
    """Стандартная папка с тем же или близким именем: `Deliverables/drafts` → `Artifacts/drafts`."""
    import difflib
    by_name: dict = {}
    for d in schema:
        by_name.setdefault(d.rstrip("/").split("/")[-1].lower(), d)
    name = rel.rstrip("/").split("/")[-1].lower()
    if name in by_name and by_name[name] != rel:
        return by_name[name]
    close = difflib.get_close_matches(name, list(by_name), n=1, cutoff=0.8)
    return by_name[close[0]] if close else ""


def check_structure(verbose: bool = False):
    """Сверить фактические папки с фиксированной схемой движка.

    → (errors, warns, lines). Лишние структурные папки — ошибка схемы: их содержимое
    должно жить в `Workspaces/<задача>/`, иначе проекты Авроры разъезжаются.
    Исключение — пути, закрытые `.gitignore`: они не попадают в репозиторий и схему
    не ломают (см. `git_ignored`).
    """
    errors: list[str] = []
    warns: list[str] = []
    lines: list[str] = []
    schema = read_structure()
    if not schema:
        warns.append("нет .aurora/structure_dirs.txt — движок старее 1.3.0, "
                     "обновите: python3 <kit>/aurora.py update <проект> --apply")
        return errors, warns, lines

    mirrors = mirror_owners()
    skipped = registry_skipped()
    if skipped:
        warns.append(f"манифест модуля источников не принят ({len(skipped)}): "
                     f"{'; '.join(skipped[:2])}{' …' if len(skipped) > 2 else ''} → исправьте или "
                     "удалите этот connector.json в .aurora/connectors/: остальные модули работают")
    artifacts = artifact_dirs()
    declared = project_dirs()
    known = set(schema) | set(mirrors) | set(artifacts) | set(declared)
    known_tops = {p.split("/")[0] for p in known}
    missing = [d for d in schema if not (ROOT / d).is_dir()]
    for path, module in sorted(mirrors.items()):
        if not (ROOT / path).is_dir():
            warns.append(f"модуль {module} заявил зеркало {path}/, а папки нет "
                         "(создать: aurora.py update <проект> --structure-only --apply)")
    for path in sorted(declared):
        if not (ROOT / path).is_dir():
            warns.append(f"в aurora.config.yaml объявлена своя папка {path}/ "
                         "(extra_structure_dirs), а на диске её нет — уберите из списка или создайте")
    if missing:
        warns.append(f"не хватает {len(missing)} стандартных папок "
                     f"(создать: aurora.py update <проект> --structure-only --apply)")
        lines += [f"    отсутствует: {d}/" for d in missing]

    # Кандидаты верхнего уровня: всё, кроме папок самого движка. Скрытые папки больше
    # не пропускаем скопом — их отсеет .gitignore, если они действительно служебные.
    candidates = sorted(p.name for p in ROOT.iterdir()
                        if p.is_dir() and p.name not in ENGINE_DIRS_TOP
                        and p.name not in known_tops)
    probes = [f"{n}/" for n in candidates] + [n for n in candidates]
    ignored = git_ignored(probes)
    # правило есть, но путь всё ещё в индексе: .gitignore не действует задним числом
    stale = sorted(set(git_ignored(probes, rules_only=True)) - set(ignored))
    extra_top = [n for n in candidates if n not in ignored and n not in stale]
    allowed_by_ignore = [n for n in candidates if n in ignored]
    extra_sub, unclaimed = [], []
    for root_name in MANAGED_ROOTS:
        root_dir = ROOT / root_name
        if not root_dir.is_dir():
            continue
        for p in sorted(root_dir.iterdir()):
            if not p.is_dir() or p.name.startswith("."):
                continue
            rel = f"{root_name}/{p.name}"
            if rel in known:
                continue
            # Папку в Sources/ заводит модуль источника, а не схема. Ничья папка —
            # замечание, а не блокер: так выглядит и ручная выгрузка, и зеркало,
            # модуль которого ещё не подключили. Сборку это не ломает.
            if root_name == "Sources":
                unclaimed.append(rel)
                continue
            extra_sub.append(rel)

    drift = check_case_drift(schema)
    if drift:
        errors.append("регистр папок схемы расходится с индексом git: " + "; ".join(drift) +
                      " → на Linux/CI это станет разными папками; почините: "
                      "git mv <имя> <имя>_tmp && git mv <имя>_tmp <Имя>")

    top_file, top_empty, top_other = sort_extra(extra_top)
    sub_file, sub_empty, sub_other = sort_extra(extra_sub)
    if top_file or sub_file:
        errors.append(f"путь файла принят за папку: {', '.join(top_file + sub_file)} → такой "
                      "каталог появляется, когда в «папке результата» вида документа стоит путь "
                      "к файлу; перенесите содержимое в стандартную папку и удалите каталог")
    if top_empty or sub_empty:
        warns.append(f"пустые папки вне схемы движка: {', '.join(top_empty + sub_empty)} "
                     "→ удалите: пустая папка в git не попадает, а схему путает")
    if top_other:
        errors.append(f"папки верхнего уровня вне схемы движка: {', '.join(top_other)} "
                      "→ перенесите содержимое в Workspaces/<задача>/, "
                      "закройте .gitignore, если это служебное, либо объявите своей папкой "
                      "проекта: paths → extra_structure_dirs в aurora.config.yaml")
    if stale:
        errors.append(f"закрыты .gitignore, но лежат в индексе git: {', '.join(stale)} "
                      "→ правило не действует задним числом, уберите из индекса: "
                      f"git rm -r --cached {' '.join(stale)}")
    if sub_other:
        named = []
        for rel in sub_other:
            near = nearest_standard(rel, schema)
            named.append(f"{rel} (ближе всего стандартная {near})" if near else rel)
        # Сначала — стандартная папка и Workspaces; объявить свою — последний выход, а не
        # первый: подсказка «объявите» на мусорной папке узаконивает мусор.
        errors.append(f"структурные папки вне схемы движка: {', '.join(named)} "
                      "→ перенесите содержимое в стандартную папку или в Workspaces/<задача>/; "
                      "если папка нужна именно этому проекту — объявите её в aurora.config.yaml "
                      "(paths → extra_structure_dirs); новый тип для всех проектов — только "
                      "релизом kit'а")
    if unclaimed:
        warns.append(f"зеркала без модуля: {', '.join(unclaimed)} → подключите модуль "
                     "в aurora.config.yaml (sources:) или перенесите выгрузку в Raw/; "
                     "что установлено — sources_registry.py")
    if verbose:
        lines.append(f"    схема: {len(schema)} папок, факт: не хватает {len(missing)}, "
                     f"лишних {len(extra_top) + len(extra_sub)}")
        if allowed_by_ignore:
            lines.append(f"    вне схемы, но закрыты .gitignore (допустимо): "
                         f"{', '.join(allowed_by_ignore)}")
        if declared:
            lines.append(f"    вне схемы, но объявлены проектом (extra_structure_dirs): "
                         f"{', '.join(sorted(declared))}")
    return errors, warns, lines


def check_portable_names() -> list:
    """Имена, которые не пройдут на Windows, macOS или Linux. → предупреждения.

    Проект — git-репозиторий, и его выгружают на любой из трёх систем: имя, созданное на
    одной, на другой ломает выгрузку ВСЕГО репозитория. Смотрим то, что лежит в git: оно и
    уезжает. Для каждой группы сказано, чем чинить — руками это сотни переименований.
    """
    import subprocess
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from aurora_common import case_clashes, path_problems
        out = subprocess.run(["git", "-C", str(ROOT), "-c", "core.quotepath=false",
                              "ls-files", "-z"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=60).stdout
    except Exception:  # noqa: BLE001 — нет git или старый движок: проверять нечего
        return []
    rels = [r for r in out.split("\0") if r]
    groups: dict = {}
    for rel in rels:
        why = path_problems(rel)
        if not why:
            continue
        where = ("зеркало Confluence" if rel.startswith("Sources/Confluence/") else
                 "карточки" if rel.startswith("AuroraKnowledgeDB/") else "прочее")
        groups.setdefault(where, []).append((rel, why[0]))
    fix = {"зеркало Confluence": "`kb_names.py --apply` переложит страницы без сети по правилу "
                                 "синка и переведёт на них базу",
           "карточки": "`kb_names.py --apply` переименует и поправит ссылки",
           "прочее": "переименуйте руками по правилам из AGENTS.md"}
    warns = []
    for where, items in groups.items():
        sample = "; ".join(f"{r[:70]}{'…' if len(r) > 70 else ''} — {w}" for r, w in items[:2])
        warns.append(f"имена: {where} — {len(items)} путей не пройдут на Windows, macOS или "
                     f"Linux ({sample}). Чинить: {fix[where]}")
    clash = case_clashes(rels)
    if clash:
        a, b = clash[0]
        warns.append(f"имена: {len(clash)} пар путей отличаются только регистром или формой "
                     f"букв — на macOS и Windows это один файл ({a} ↔ {b}). Лишнюю запись "
                     f"снимет из индекса git `kb_names.py --apply`")
    return warns


# Почему находка — блокер. Строка находки говорит «что» и «как исправить» (через «→»), но
# не «почему это мешает»: человек видел «блокеры: 2», не понимал, что сломано и зачем
# чинить, и не знал, куда нажать. Панель показывает все три части в отчёте «Блокеры»
# раздела «Здоровье» (1.161.0); строку `WHY:` печатаем сразу за `ERROR:`.
BLOCKER_WHY = {
    "регистр папок схемы расходится": "на Linux и в CI папки, разные только регистром, — "
        "две разные папки: карточки разъедутся по двум адресам, ссылки и оглавления сломаются",
    "путь файла принят за папку": "документы этого вида ложатся мимо стандартной папки: их не "
        "находят ни поиск, ни отчёты, а движок считает каталог чужим",
    "папки верхнего уровня вне схемы движка": "движок не знает, что в этих папках: не "
        "разбирает их и не следит за ними, а обновление кита и проверки считают их лишними",
    "закрыты .gitignore, но лежат в индексе git": "правило .gitignore не действует задним "
        "числом: файлы продолжают уезжать в общий репозиторий, хотя закрыты",
    "структурные папки вне схемы движка": "содержимое нестандартной папки движок не видит: "
        "оно выпадает из базы, поиска и отчётов",
    "нет aurora.config.yaml": "без настройки движок не знает проекта — имени, источников, "
        "папок, — и ни одна команда не работает правильно",
    "aurora.config.yaml: project.name": "по имени и slug проект узнают панель, поиск для "
        "агентов (MCP) и отчёты; без них проект безымянный",
    "нет .aurora/skills/aurora-vault/SKILL.md": "движок проекта установлен не полностью: без "
        "навыка ассистенты не знают правил базы и пишут в неё как попало",
    "SECRET:": "ключ или токен в файле под git уедет в общий репозиторий и останется в "
        "истории навсегда",
    "проект не под git": "без git нет отката: агент перед записью ставит точку возврата и "
        "без неё писать в базу отказывается",
}


def blocker_why(text: str) -> str:
    """Почему находка мешает работе — по началу её текста; не знаем — пусто."""
    for head, why in BLOCKER_WHY.items():
        if text.startswith(head):
            return why
    return ""


def check_kit_hooks() -> list:
    """Предупреждения о git-хуках самого кита на этой машине: свой pre-push старее скрипта проверки.

    Хук ставится копией текста, обновление кита её не меняло, и защита от публикации веток кухни
    и внутренних названий, исправленная в ките, действовала только после ручной переустановки.
    Нет хука или он чужой — не наше дело (кит из архива, клон без права пуша)."""
    kit = Path(__file__).resolve().parent.parent
    if not ((kit / "engine_manifest.txt").is_file() and (kit / ".git").exists()):
        return []
    try:
        import subprocess
        import aurora_hooks as _h
        out = subprocess.run(["git", "-C", str(kit), "rev-parse", "--git-dir"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=20).stdout.strip()
        if _h.push_hook_state((kit / out).resolve()) != "outdated":
            return []
    except Exception:  # noqa: BLE001 — справка не имеет права ронять доктора
        return []
    out = []   # через .append: реестр переводов kit_i18n собирает сообщения доктора именно по вызовам append
    out.append("git-хук pre-push кита старее скрипта проверки — защита публикации неполная "
               "(ветки кухни по HEAD, названия в содержимом); переставьте: `kit:hooks` с флагом --install "
               "(кнопка «Обновить кит» в панели переставляет его сама, но только когда вышла новая версия)")
    return out


def check_mirror_heads() -> list:
    """Предупреждение о зеркалах `Sources/` с подозрительной шапкой (BL-SEC-13).

    Зеркала, записанные до закрытия подделки полей шапки через заголовок страницы, лежат на
    диске и дальше (неизменную страницу синк держит из кэша). Читатели доверия таким шапкам
    уже не верят, но человек должен знать, какие файлы перезаписать. Данные проекта не
    трогаем: только называем и говорим чем лечить."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import sync_audit as SA
        found = []
        for path in sorted(mirror_owners()):
            if os.path.isdir(path):
                found += [f"{path}/{rel}" for rel, _codes in SA.odd_head_files(path)]
    except Exception:  # noqa: BLE001 — справка не имеет права ронять доктора
        return []
    out = []   # через .append: реестр переводов kit_i18n собирает сообщения доктора именно по вызовам append
    if found:
        shown = ", ".join(found[:3]) + (" …" if len(found) > 3 else "")
        out.append(f"в зеркалах Sources/ шапок с повтором поля, подложным trusted, незакрытой или оборванной шапкой: {len(found)} "
                   f"({shown}) — доверию таких файлов не верим; подробно: `sync:audit`, лечится "
                   "перезаписью файла: `--force` команды синка зеркала")
    return out


def check_linked_roots() -> list:
    """Предупреждение о каталогах базы и источников, которые сами — ссылка наружу проекта.

    `AuroraKnowledgeDB`, `Sources/<зеркало>`, `Raw/<папка>`, закоммиченные как symlink на чужую
    папку (хранилище Obsidian, общее зеркало), движок не читает: граница чтения отказывает
    каждому файлу. Одна строка вместо ошибки на каждую карточку; данные не трогаем."""
    found = []
    for top in ("AuroraKnowledgeDB", "Sources", "Raw"):
        if not os.path.isdir(top):
            continue
        if root_link_escapes(top):
            found.append(top)
            continue
        if top != "AuroraKnowledgeDB":
            found += [f"{top}/{d}" for d in sorted(os.listdir(top))
                      if os.path.isdir(os.path.join(top, d)) and root_link_escapes(os.path.join(top, d))]
    out = []   # через .append: реестр переводов kit_i18n собирает сообщения доктора именно по вызовам append
    if found:
        shown = ", ".join(found[:3]) + (" …" if len(found) > 3 else "")
        out.append(f"каталог базы или источников — ссылка наружу проекта: движок его не читает ({shown}) "
                   "— положите папку в проект или уберите ссылку")
    return out


def check_card_templates(root: Path | None = None) -> list:
    """Сведения о шаблонах карточек: сколько типов кита доставлено в проект, есть ли проблемы реестра, сколько
    карточек без `template:`. → строки уровня «сведение».

    Отдельного уровня «сведение» (`info`) у доктора нет, и заводить механизм ради трёх строк незачем: сведения —
    обычные строки без `ERROR:` и `WARN:`, так печатаются «агент:» и «приватность:». Страница «Здоровье» их не
    собирает, итог «OK with warnings» они не меняют: карточка без `template:` «до-шаблонная» и ошибкой не считается,
    а нарушения у карточек со штампом показывает `kb:lint` ошибками. Что делать с нарушениями — `kb:cards --check`."""
    root = root or ROOT
    out = []   # через .append: реестр переводов kit_i18n собирает сообщения доктора именно по вызовам append
    try:
        from card_types import catalog_pinned, catalog_state, delivered_kit_types, registry, template_stamps, type_counts
        reg = registry(str(root))
        got, total = delivered_kit_types(reg)
        has_base = (root / "AuroraKnowledgeDB").is_dir()
        cards, stamped = template_stamps(str(root)) if has_base else (None, 0)
        catalog = catalog_state(str(root), reg)
        pinned = catalog != "ok" and catalog_pinned(str(root))
        off = sorted(t for t, rec in reg.overrides.items() if rec.get("enabled") is False)
        counts = type_counts(str(root)) if off and has_base else {}
    except Exception:  # noqa: BLE001 — справка не имеет права ронять доктора
        return out
    if got == total:
        out.append(f"шаблоны карточек: {got} из {total} доставлены")
    else:
        out.append(f"шаблоны карточек: {got} из {total} доставлены — доставьте остальные обновлением проекта: `kit:update`")
    # замечания к блоку `cards:` конфига — предупреждения (`check_cards_block`); здесь только проблемы шаблонов и реестра
    problems = [p for p in reg.problems if p not in reg.config_problems]
    if problems:
        # паспорт шаблона пишет человек: в одну строку, чтобы чужой текст не стал строкой `WARN:` или `ERROR:`
        shown = "; ".join(" ".join(p.split())[:140] for p in problems[:2]) + (" …" if len(problems) > 2 else "")
        out.append(f"типы: проблемы реестра — {len(problems)}: {shown}")
    else:
        out.append("типы: проблемы реестра — нет")
    if cards is not None:
        out.append(f"карточек без шаблона: {cards - stamped}")
    if catalog == "ok":
        out.append("каталог типов карточек: актуален")
    elif catalog == "missing" and pinned:
        out.append("каталог типов карточек: нет, а проект оставил его себе (aurora.update_ignore.txt) — обновление его не соберёт, "
                   "соберите командой `kb:cards --write --apply`")
    elif catalog == "stale" and pinned:
        out.append("каталог типов карточек: отстал, а проект оставил его себе (aurora.update_ignore.txt) — обновление его не пересоберёт, "
                   "пересоберите командой `kb:cards --write --apply`")
    elif catalog == "missing":
        out.append("каталог типов карточек: нет — соберите его обновлением проекта: `kit:update`")
    elif catalog == "stale":
        out.append("каталог типов карточек: отстал от шаблонов или настроек — пересоберите его обновлением проекта: `kit:update`")
    left = ", ".join(f"{t} — {counts[t]}" for t in off if counts.get(t))
    if left:
        out.append(f"типы карточек: выключены в блоке cards:, но карточки в базе остались — {left}")
    return out


def check_cards_block(root: Path | None = None) -> list:
    """Блок `cards:` конфига проекта: что реестр типов не принял (тип вне реестра, поле шаблона, непонятное значение).
    → предупреждения; пусто — блока нет или он в порядке. Строка блока при этом пропущена, а не применена."""
    root = root or ROOT
    out = []   # через .append: реестр переводов kit_i18n собирает сообщения доктора именно по вызовам append
    try:
        from card_types import registry
        problems = list(registry(str(root)).config_problems)
    except Exception:  # noqa: BLE001 — справка не имеет права ронять доктора
        return out
    for problem in problems[:5]:
        # строку блока пишет человек: в одну строку, чтобы чужой текст не стал строкой `WARN:` или `ERROR:`
        out.append(f"aurora.config.yaml: {' '.join(problem.split())[:200]} → поправьте блок cards: или уберите строку")
    if len(problems) > 5:
        out.append(f"aurora.config.yaml: в блоке cards: ещё замечаний: {len(problems) - 5}")
    return out


def check_web_token_binding(root: Path | None = None, environ: dict | None = None) -> list:
    """Токен обхода сайтов есть, а привязки к хостам нет — предупреждение (пусто — всё в
    порядке). Привязка — строка `AURORA_WEB_TOKEN_HOSTS` в файле машины или запомненные хосты
    первого прогона (`.aurora/state/trusted-hosts.json`); без неё токен уходит на каждый хост
    общего списка страниц, и страница, добавленная туда чужим PR, его получила бы."""
    root = root or ROOT
    out = []   # через .append: реестр переводов kit_i18n собирает сообщения доктора именно по вызовам append
    try:
        from aurora_common import ENV_FILE, load_env
        from sources_core import trusted_hosts
        env = dict(os.environ if environ is None else environ)
        env.update(load_env(root / ENV_FILE))
        if not (env.get("WEB_PERSONAL_TOKEN") or env.get("WEB_PAT")) \
                or str(env.get("AURORA_WEB_TOKEN_HOSTS", "")).strip() \
                or trusted_hosts("WEB", str(root)):
            return out
    except Exception:  # noqa: BLE001 — справка не имеет права ронять доктора
        return out
    out.append("токен обхода сайтов есть, привязки к хостам нет: он уходит на каждый хост общего "
               "списка страниц → впишите в файл машины AURORA_WEB_TOKEN_HOSTS=wiki.example.com,… "
               "или запустите выгрузку сайтов: движок запомнит хосты, которые ответили")
    return out


def check_web_private_sources(root: Path | None = None) -> list:
    """Источники блока `web:`, которые ходят во внутреннюю сеть: хост из `allow_private` или
    страница с приватным адресом-литералом (BL-SEC-17). Предупреждение, не ошибка: внутренняя
    вики — законный источник, но человек должен видеть, что обход выходит за публичную сеть.
    Имена не резолвятся: доктор работает без сети. → предупреждения; пусто — таких нет."""
    root = root or ROOT
    out = []   # через .append: реестр переводов kit_i18n собирает сообщения доктора именно по вызовам append
    try:
        import socket
        from urllib.parse import urlsplit
        from aurora_common import _allow_private_entries, _non_global, canon_host, is_loopback_host
        from web_export import pages_from_config, settings_from_config
        cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
        entries = _allow_private_entries(settings_from_config(cfg)["allow_private"])
        allowed, refused, used = [], [], set()
        for url, _trusted, _feed in pages_from_config(cfg):
            try:                                  # один битый адрес (порт 99999) не гасит остальные
                p = urlsplit(url)
                # то же написание имени, что у политики экспорта: `169.254.169.254.`, мягкий перенос, `¹²³`
                host, port = canon_host(p.hostname or ""), p.port or {"http": 80, "https": 443}.get(p.scheme, 0)
            except ValueError:
                continue
            # без логина и пароля из адреса: вывод доктора попадает в консоль панели и журналы
            shown = f"{p.scheme}://{'[' + host + ']' if ':' in host else host}" \
                    f"{':' + str(p.port) if p.port else ''}{p.path}"
            hit = [(h, pt) for h, pt in entries if h == host and pt in (0, port)]
            if hit:
                used.update(hit)
                allowed.append(shown)
                continue
            why = _non_global(host)
            if why == "не адрес" and not is_loopback_host(host):
                try:                              # `2130706433`, `0x7f.1`: числовой литерал, как в экспорте
                    why = _non_global(socket.inet_ntoa(socket.inet_aton(host)))
                except (OSError, ValueError):
                    pass                          # имя — не адрес, без сети не проверить
            if is_loopback_host(host) or why not in ("", "не адрес"):
                refused.append(shown)
        loose = [f"{h}:{pt}" if pt else h for h, pt in entries if (h, pt) not in used]
    except Exception:  # noqa: BLE001 — справка не имеет права ронять доктора
        return out
    if allowed:
        out.append(f"aurora.config.yaml: web.allow_private разрешает обходу внутренние адреса, страниц: "
                   f"{len(allowed)} ({', '.join(allowed[:3])}"
                   f"{' …' if len(allowed) > 3 else ''}) → убедитесь, что это ваши внутренние источники; "
                   "привязка токена от списка не расширяется")
    if loose:
        out.append(f"aurora.config.yaml: web.allow_private — записи без страницы в списке: {', '.join(loose[:3])}"
                   f"{' …' if len(loose) > 3 else ''} → разрешение действует и на редиректы публичных страниц; "
                   "уберите запись, если внутренний источник вам не нужен")
    if refused:
        out.append(f"aurora.config.yaml: web — страниц с приватным адресом без allow_private: {len(refused)} "
                   f"({', '.join(refused[:3])}{' …' if len(refused) > 3 else ''}) "
                   "→ выгрузка их откажет; если это внутренняя вики, внесите хост в web.allow_private")
    return out


def check_engine(warns: list) -> list:
    """Сверка `.aurora/` с манифестом кита и его версией. → строки подробного перечня недостающего.

    Находка идёт в `warns` с первыми именами недостающих файлов, полный список — отдельными строками
    под находками. Кита с манифестом не видно (копия движка без указателя на кит) — сверять не с чем:
    молча, как и при старой копии `aurora_update.py` без этой проверки."""
    try:
        import aurora_update as AU
        st = AU.engine_state(ROOT)
    except Exception:  # noqa: BLE001 — справка не роняет доктора
        return []
    miss = st["missing"]
    names = ", ".join(miss[:6]) + (" …" if len(miss) > 6 else "")
    if st["legacy"]:
        warns.append(f"движок проекта в прежней папке .opencode/ (версия {st['copy'] or '—'}) → «Версия» → "
                     "«Предпросмотр обновления» → «Применить»: движок переедет в .aurora/")
    elif st["state"] == "missing":
        warns.append(f"движка проекта нет на этом компьютере: в .aurora/ ни одного из {st['total']} файлов по "
                     "манифесту → откройте проект в панели (движок подготовится сам) или «Версия» → "
                     "«Подготовить проект»")
    elif st["state"] == "incomplete":
        warns.append(f"движок проекта неполный: нет {len(miss)} из {st['total']} файлов в .aurora/ ({names}) → "
                     "«Версия» → «Подготовить проект»")
    elif st["state"] == "stale":
        warns.append(f"движок проекта {st['copy']} старше кита {st['kit']} → «Версия» → «Обновить»")
    if not miss or st["legacy"]:
        return []
    return [f"недостающие файлы движка ({len(miss)}):"] + [f"  {m}" for m in miss]


def check_python(errors: list, warns: list, vi=None, platform=None) -> None:
    """Версия Python, на котором идёт doctor: 3.11+ — OK, 3.10 — предупреждение (снят с
    поддержки), ниже — ошибка с подсказкой по системе. `vi` подменяют тесты."""
    level, text = python_status(vi, platform)
    if level == "ok":
        print("OK:", text)
    elif level == "warn":
        warns.append(text)
    else:
        errors.append(text)


def main() -> int:
    ap = argparse.ArgumentParser(description="Готовность проекта Aurora к работе")
    ap.add_argument("--structure", action="store_true",
                    help="подробно по структуре папок: что вне схемы движка")
    structure_verbose = ap.parse_args().structure

    errors: list[str] = []
    warns: list[str] = []

    check_python(errors, warns)

    if not CONFIG.exists():
        errors.append("нет aurora.config.yaml → скопируйте из Aurora kit templates/")
    else:
        data = load_yaml_lite(CONFIG)
        # Корень синка, записанный дважды: синк обходит его один раз (с 1.147.0), но
        # настройка врёт о числе корней, и форма показывает два одинаковых.
        from aurora_common import sync_roots
        ids = [r["page_id"] for r in sync_roots(CONFIG.read_text(encoding="utf-8"))]
        twice = sorted({i for i in ids if ids.count(i) > 1})
        if twice:
            warns.append("aurora.config.yaml: корень синка записан больше одного раза — "
                         + ", ".join(twice) + "; уберите повтор в «Настройки проекта» → "
                         "«Источники»")
        if data.get("_raw"):
            if not data.get("project"):
                warns.append("aurora.config.yaml: не найден блок project (проверьте YAML)")
            if not data.get("atlassian"):
                warns.append("aurora.config.yaml: не найден блок atlassian")
        else:
            proj = data.get("project") or {}
            atl = data.get("atlassian") or {}
            if not (isinstance(proj, dict) and proj.get("name") and proj.get("slug")):
                errors.append("aurora.config.yaml: project.name / project.slug обязательны → "
                              "заполните их в «Настройки проекта»")
            conf = (atl.get("confluence") or {}) if isinstance(atl, dict) else {}
            jira = (atl.get("jira") or {}) if isinstance(atl, dict) else {}
            if not conf.get("space"):
                warns.append("atlassian.confluence.space пуст")
            if not jira.get("project_key"):
                warns.append("atlassian.jira.project_key пуст")
            auth = atl.get("auth") if isinstance(atl, dict) else None
            if isinstance(auth, dict) and auth.get("mode") not in (None, "mcp_user"):
                warns.append(f"atlassian.auth.mode={auth.get('mode')!r} — токены не должны быть в yaml")

    if not SKILL.exists():
        errors.append("нет .aurora/skills/aurora-vault/SKILL.md → обновите движок проекта: "
                      "раздел «Версия»")
    if not AGENTS.exists():
        warns.append("нет AGENTS.md")

    if ENV_LOCAL.exists():
        print("OK: .env.aurora.local присутствует (локально)")
    else:
        warns.append(
            "нет .env.aurora.local — для CLI скопируйте aurora.env.local.example "
            "(MCP-авторизация всё равно в Cursor)"
        )
    if not ENV_EXAMPLE.exists():
        warns.append("нет aurora.env.local.example (добавьте из kit)")

    for h in scan_secrets():
        errors.append(f"SECRET: {h} → уберите значение из файла (секреты — только в "
                      ".env.aurora.local) и смените ключ, если он уже попал в коммит")

    # Проект без git — это проект без отката. Агент такого не переживает: перед прогоном
    # он делает чекпойнт, не может — отказывается писать вовсе. В живом проекте это
    # выглядело как «agent:build отработал за ноль секунд и вернул 1», и понять, почему
    # база не растёт, было нельзя. Всё остальное doctor при этом честно говорил «OK».
    import subprocess                      # как в остальных проверках: git нужен не всегда
    try:
        rc = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20).returncode
    except Exception:
        rc = 1
    if rc != 0:
        errors.append("проект не под git → заведите: `git init`, затем первый коммит "
                      "всей базы (раздел «Git»)")

    vfile = ROOT / "AuroraKnowledgeDB" / "meta" / "aurora_version.txt"
    engine_ver = vfile.read_text(encoding="utf-8").strip() if vfile.exists() else None
    if not engine_ver:
        warns.append("версия движка не проставлена — обновите через `aurora.py update <project>`")

    # Копия движка в `.aurora/` — на этой машине, вне git: клон проекта приходит без неё, а
    # полусобранная давала странные сбои («мимо Pydantic AI» при зелёной установке). Точный
    # перечень недостающего — ниже находок; лечит панель: «Версия» → «Подготовить проект».
    engine_lines = check_engine(warns)
    seeds = retired_fields_in_seeds()
    if seeds:
        warns.append(f"выведенные из схемы поля в шаблонах ({len(seeds)}): "
                     + "; ".join(seeds[:3]) + (" …" if len(seeds) > 3 else "")
                     + " → уберите: kb_fix.py --retire --apply")

    warns += check_portable_names()
    warns += check_kit_hooks()
    warns += check_mirror_heads()
    warns += check_linked_roots()
    warns += check_web_token_binding()
    warns += check_web_private_sources()
    warns += check_cards_block()

    s_err, s_warn, s_lines = check_structure(structure_verbose)
    errors += s_err
    warns += s_warn

    print("Aurora doctor —", ROOT)
    if engine_ver:
        print(f"движок: {engine_ver}  (сверка с kit: `aurora.py update .` — dry-run)")
    # Встроенный агент — справка, не проверка: без него всё работает, но человек должен
    # видеть при онбординге, настроен ли контур LLM и чем он исполняется.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import agent_core as _ag
        _cfg = _ag.config()
        if _cfg["backends"]:
            _ok, _v = _ag.venv_status()
            _chat = [b for b in _cfg["backends"] if b.get("chat", True)]
            _vec = _ag.embed_ring(_cfg)
            # venv стоит, а файла адаптера нет — движок проекта неполный: «venv не установлен» тут было бы ложью
            _gap = getattr(_ag, "adapter_gap", lambda: "")() if _ok else ""
            print(f"агент: провайдеров чата {len(_chat)} · векторных {len(_vec)}"
                  f" · адаптер {_cfg['adapter']}"
                  + ((f" — {_gap}; вызовы идут мимо Pydantic AI" if _gap else f" ({_v})") if _ok
                     else " — venv не установлен, stdlib-фолбэк")
                  + " · настройка одна на кит · проверить: agent:ping")
        else:
            print("агент: не настроен (это не ошибка) — панель: раздел «Модели»")
    except Exception:  # noqa: BLE001 — справка не имеет права ронять доктора
        pass
    # Связь — справка из сохранённого состояния: доктор сети не трогает. Выключенная проверка
    # (по умолчанию) молчит, чтобы не шуметь на каждом запуске.
    try:
        import connectivity as _cn
        _svc = _cn.AvailabilityService()
        if _svc.enabled:
            _snap = _svc.snapshot()
            _ages = [r["age_s"] for r in [_snap["internet"]] + _snap["profiles"] if r.get("age_s") is not None]
            print(f"связь: {_snap['state']} — {_cn.mode_line(_snap)}"
                  + (f" · проверено {min(_ages)} с назад" if _ages else "") + " · подробнее: net:status")
    except Exception:  # noqa: BLE001 — справка не имеет права ронять доктора
        pass
    # С 1.153.0 модели настраиваются одним файлом кита. Переменные моделей в `.env`
    # проекта больше не действуют: сказать об этом, чтобы человек не правил их впустую.
    # Файл не трогаем — он человека; называем только имена, не значения.
    try:
        from aurora_common import ENV_FILE, load_env
        _left = sorted(k for k in load_env(ROOT / ENV_FILE)
                       if k.startswith(("AURORA_AGENT_", "AURORA_EMBED_", "AURORA_OCR_")))
        if _left:
            warns.append(f"{ENV_FILE}: переменные моделей больше не действуют — настройка "
                         f"моделей одна на кит (раздел «Модели»); уберите их: "
                         + ", ".join(_left[:6]) + (" …" if len(_left) > 6 else ""))
    except Exception:  # noqa: BLE001
        pass
    # режим приватности — свойство контура, человек должен видеть его при онбординге
    print(f"приватность: privacy.scrub = {privacy_mode()}"
          + {"off": " (контур закрытый, ПДн не ищем)",
             "mask": " (маскирование ожидается перед публикацией)"}.get(privacy_mode(), ""))
    for e in errors:
        print("ERROR:", e)
        why = blocker_why(e)
        if why:
            print("WHY:", why)
    for w in warns:
        print("WARN:", w)
    for line in s_lines:
        print(line)
    for line in engine_lines:
        print(line)
    for line in check_card_templates():           # сведения, не находки: итог ниже они не меняют
        print(line)
    if not errors and not warns:
        print("OK: config and onboarding look ready")
    elif not errors:
        print("OK with warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
