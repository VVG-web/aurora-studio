#!/usr/bin/env python3
"""aurora_doctor.py — check Aurora project onboarding readiness.

Run from project root:
  python3 .opencode/scripts/aurora_doctor.py
  python3 .opencode/scripts/aurora_doctor.py --structure   # подробно по структуре папок

Проверяет конфиг, скиллы, секреты в git, версию движка и **фиксированную структуру папок**
(схема движка `.opencode/structure_dirs.txt` одинакова во всех проектах Авроры: свои типы
артефактов и свои разделы базы не заводятся — для всего нестандартного есть `Workspaces/`).

Exit 0 if OK / warnings only; 1 if blocking errors.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import RETIRED_FIELDS
from pathlib import Path

ROOT = Path.cwd()
CONFIG = ROOT / "aurora.config.yaml"
ENV_LOCAL = ROOT / ".env.aurora.local"
ENV_EXAMPLE = ROOT / "aurora.env.local.example"
SKILL = ROOT / ".opencode" / "skills" / "aurora-vault" / "SKILL.md"
AGENTS = ROOT / "AGENTS.md"

SECRET_PATTERNS = [
    re.compile(r"(?i)ATLASSIAN_API_TOKEN\s*=\s*\S+"),
    re.compile(r"(?i)api[_-]?token\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)password\s*[:=]\s*\S+"),
]

TRACKED_SCAN_GLOBS = [
    "aurora.config.yaml",
    "AGENTS.md",
    ".cursor/rules/**/*.mdc",
    ".opencode/skills/**/*.md",
    ".opencode/skills/**/*.json",
]


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
    hits = []
    for pattern in ("aurora.config.yaml", "AGENTS.md"):
        p = ROOT / pattern
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for rx in SECRET_PATTERNS:
            if rx.search(text):
                hits.append(f"{pattern}: возможный секрет ({rx.pattern[:40]}…)")
    skills = ROOT / ".opencode" / "skills"
    if skills.is_dir():
        for p in skills.rglob("*"):
            if p.suffix.lower() not in {".md", ".json", ".mdc", ".yml", ".yaml"}:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for rx in SECRET_PATTERNS:
                if rx.search(text):
                    hits.append(f"{p.relative_to(ROOT)}: возможный секрет")
                    break
    return hits


STRUCTURE_FILE = ROOT / ".opencode" / "structure_dirs.txt"
# Папки самого движка и системы контроля версий — они часть инструмента, не схемы.
# Всё остальное вне схемы допустимо ТОЛЬКО если закрыто .gitignore (см. git_ignored).
ENGINE_DIRS_TOP = {".git", ".opencode", ".cursor", ".claude"}
# Внутри этих корней второй уровень задаётся движком (свои подпапки заводить нельзя).
MANAGED_ROOTS = ("Artifacts", "AuroraKnowledgeDB", "Raw", "Sources", "Deliverables")


def mirror_owners() -> dict:
    """{папка зеркала: модуль} — что в `Sources/` заявлено подключёнными модулями.

    Схема папок фиксирована движком, но зеркала в неё не входят: их набор зависит от
    того, чем команда пользуется. Поэтому легитимность папки в `Sources/` определяет
    реестр модулей, а не список в файле схемы.
    """
    sys.path.insert(0, str(ROOT / ".opencode" / "scripts"))
    try:
        import sources_registry as R
    except ImportError:
        return {}
    return R.mirror_paths(str(ROOT))


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
        out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, timeout=60)
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
    try:
        out = subprocess.run(["git", "check-ignore", "--stdin"]
                             + (["--no-index"] if rules_only else []),
                             input="\n".join(paths), capture_output=True, text=True, timeout=60)
    except Exception:
        return set()
    return {line.strip().rstrip("/") for line in out.stdout.splitlines() if line.strip()}


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
        warns.append("нет .opencode/structure_dirs.txt — движок старее 1.3.0, "
                     "обновите: python3 <kit>/aurora.py update <проект> --apply")
        return errors, warns, lines

    mirrors = mirror_owners()
    known = set(schema) | set(mirrors)
    known_tops = {p.split("/")[0] for p in known}
    missing = [d for d in schema if not (ROOT / d).is_dir()]
    for path, module in sorted(mirrors.items()):
        if not (ROOT / path).is_dir():
            warns.append(f"модуль {module} заявил зеркало {path}/, а папки нет "
                         "(создать: aurora.py update <проект> --structure-only --apply)")
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

    if extra_top:
        empty = [n for n in extra_top if not any((ROOT / n).rglob("*"))]
        note = f" (пустые: {', '.join(empty)})" if empty else ""
        errors.append(f"папки верхнего уровня вне схемы движка: {', '.join(extra_top)}{note} "
                      "→ перенесите содержимое в Workspaces/<задача>/, удалите пустое "
                      "либо закройте .gitignore, если это служебное")
    if stale:
        errors.append(f"закрыты .gitignore, но лежат в индексе git: {', '.join(stale)} "
                      "→ правило не действует задним числом, уберите из индекса: "
                      f"git rm -r --cached {' '.join(stale)}")
    if extra_sub:
        errors.append(f"структурные папки вне схемы движка: {', '.join(extra_sub)} "
                      "→ либо стандартный тип, либо Workspaces/<задача>/; новый тип — только релизом kit'а")
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
    return errors, warns, lines


def main() -> int:
    errors: list[str] = []
    warns: list[str] = []
    structure_verbose = "--structure" in sys.argv   # подробно о папках вне схемы

    if not CONFIG.exists():
        errors.append("нет aurora.config.yaml — скопируйте из Aurora kit templates/")
    else:
        data = load_yaml_lite(CONFIG)
        if data.get("_raw"):
            if not data.get("project"):
                warns.append("aurora.config.yaml: не найден блок project (проверьте YAML)")
            if not data.get("atlassian"):
                warns.append("aurora.config.yaml: не найден блок atlassian")
        else:
            proj = data.get("project") or {}
            atl = data.get("atlassian") or {}
            if not (isinstance(proj, dict) and proj.get("name") and proj.get("slug")):
                errors.append("aurora.config.yaml: project.name / project.slug обязательны")
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
        errors.append("нет .opencode/skills/aurora-vault/SKILL.md")
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
        errors.append(f"SECRET: {h}")

    vfile = ROOT / "AuroraKnowledgeDB" / "meta" / "aurora_version.txt"
    engine_ver = vfile.read_text(encoding="utf-8").strip() if vfile.exists() else None
    if not engine_ver:
        warns.append("версия движка не проставлена — обновите через `aurora.py update <project>`")

    seeds = retired_fields_in_seeds()
    if seeds:
        warns.append(f"выведенные из схемы поля в шаблонах ({len(seeds)}): "
                     + "; ".join(seeds[:3]) + (" …" if len(seeds) > 3 else "")
                     + " → уберите: kb_fix.py --retire --apply")

    s_err, s_warn, s_lines = check_structure(structure_verbose)
    errors += s_err
    warns += s_warn

    print("Aurora doctor —", ROOT)
    if engine_ver:
        print(f"движок: {engine_ver}  (сверка с kit: `aurora.py update .` — dry-run)")
    # режим приватности — свойство контура, человек должен видеть его при онбординге
    print(f"приватность: privacy.scrub = {privacy_mode()}"
          + {"off": " (контур закрытый, ПДн не ищем)",
             "mask": " (маскирование ожидается перед публикацией)"}.get(privacy_mode(), ""))
    for e in errors:
        print("ERROR:", e)
    for w in warns:
        print("WARN:", w)
    for line in s_lines:
        print(line)
    if not errors and not warns:
        print("OK: config and onboarding look ready")
    elif not errors:
        print("OK with warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
