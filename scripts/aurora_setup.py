#!/usr/bin/env python3
"""aurora_setup.py — интерактивная настройка проекта Aurora (перезапускаемая).

Читает и перезаписывает `aurora.config.yaml` в целевом проекте. Запускается:
  • автоматически из `aurora.py new <target>` при первичной установке;
  • вручную из корня проекта в любой момент, чтобы изменить/дополнить настройки:
        python3 .opencode/scripts/aurora_setup.py

Если `aurora.config.yaml` уже есть — режим редактирования: текущие значения показываются
в [квадратных скобках], пустой ввод оставляет как есть.

Без зависимостей (только стандартная библиотека). Схема конфига фиксирована — файл
перезаписывается целиком из собранных ответов.

Форма вместо диалога: `--json файл` (или `--json -` для stdin) принимает те же ответы
структурой — так настройку запускает панель, не дублируя логику записи конфига.

Неинтерактивно (CI/тесты): `--non-interactive` берёт значения по умолчанию/из --set,
либо ответы подаются в stdin построчно.
"""
from __future__ import annotations
import argparse, os, re, sys
from pathlib import Path


# ---------- лёгкий разбор нашего фиксированного YAML (без PyYAML) ----------

def read_config(path: Path) -> dict:
    """Достаёт известные скалярные ключи и sync_roots из aurora.config.yaml."""
    cfg = {
        "name": "", "slug": "",
        "conf_url": "", "conf_space": "", "sync_roots": [],
        "jira_url": "", "jira_key": "", "jira_jql": "",
        "threshold": "20",
    }
    if not path.is_file():
        return cfg
    text = path.read_text(encoding="utf-8")

    def scalar(key, default=""):
        m = re.search(rf'^\s*{re.escape(key)}\s*:\s*"?([^"\n#]+?)"?\s*$', text, re.M)
        return m.group(1).strip() if m else default

    cfg["name"] = scalar("name")
    cfg["slug"] = scalar("slug")
    # confluence блок
    cm = re.search(r"confluence:(.*?)(?:\n  jira:|\Z)", text, re.S)
    cblock = cm.group(1) if cm else ""
    cfg["conf_url"] = (re.search(r'base_url:\s*"?([^"\n]+)', cblock) or [None, ""])[1].strip() if cblock else ""
    cfg["conf_space"] = (re.search(r'space:\s*"?([^"\n]+)', cblock) or [None, ""])[1].strip() if cblock else ""
    # sync_roots: пары page_id / title
    for m in re.finditer(r'page_id:\s*"?([^"\n]+?)"?\s*\n\s*title:\s*"?([^"\n]+?)"?\s*(?:\n|$)', cblock):
        cfg["sync_roots"].append((m.group(1).strip(), m.group(2).strip()))
    # jira блок
    jm = re.search(r"jira:(.*?)(?:\n  auth:|\Z)", text, re.S)
    jblock = jm.group(1) if jm else ""
    cfg["jira_url"] = (re.search(r'base_url:\s*"?([^"\n]+)', jblock) or [None, ""])[1].strip() if jblock else ""
    cfg["jira_key"] = (re.search(r'project_key:\s*"?([^"\n]+)', jblock) or [None, ""])[1].strip() if jblock else ""
    cfg["jira_jql"] = (re.search(r'default_jql:\s*"?([^"\n]+?)"?\s*$', jblock, re.M) or [None, ""])[1].strip() if jblock else ""
    cfg["threshold"] = scalar("verified_threshold_pct", "20")
    cfg["scrub"] = scalar("scrub", "report")
    return cfg


def source_sections(target: Path, slug: str) -> tuple:
    """(секция sources:, строки sync-скиллов для skills.recommended).

    Читаем реестр, а не список из головы: сколько модулей подключено, столько строк
    и появится. При первой настройке реестр отдаёт исторические два зеркала.
    """
    try:
        import sources_registry as R
    except ImportError:                       # конфиг настраивают из проекта без реестра
        return "", ""
    items = R.instances(str(target))
    if not items:
        return "", ""
    src = ["\n# Подключённые модули источников. Манифесты — в .opencode/connectors/,",
           "# что установлено и что подключено — `python3 .opencode/scripts/sources_registry.py`.",
           "sources:"]
    skills = []
    for i in items:
        src.append(f'  - id: {i["id"]}\n    module: {i["module"]}\n    path: {i["path"]}')
        skill = ((i.get("manifest") or {}).get("run") or {}).get("skill", "")
        if skill:
            skills.append(f'    - name: "{skill}-{slug}"\n      via: repo')
    return "\n".join(src) + "\n", "\n".join(skills)


def write_config(path: Path, c: dict):
    roots = "[]"
    if c["sync_roots"]:
        lines = []
        for pid, title in c["sync_roots"]:
            # ссылку собираем только для настоящего номера: иначе в конфиг попадал
            # адрес вида …?pageId=https://…/display/… — бессмысленный и вводящий в заблуждение
            url = (f"{c['conf_url'].rstrip('/')}/pages/viewpage.action?pageId={pid}"
                   if c["conf_url"] and str(pid).isdigit() else "")
            lines.append(f'      - page_id: "{pid}"\n        title: "{title}"'
                         + (f'\n        url: "{url}"' if url else ""))
        roots = "\n" + "\n".join(lines)
    sources, sync_skills = source_sections(path.parent, c["slug"])
    text = f"""# Aurora project configuration (committed). Schema version 1.
# Отредактировать в любой момент: python3 .opencode/scripts/aurora_setup.py
# Секреты (токены) сюда НЕ класть — они в .env.aurora.local (gitignored).
# Агенты и sync-скиллы читают константы проекта ТОЛЬКО отсюда.

aurora:
  version: 1

project:
  name: "{c['name']}"
  slug: "{c['slug']}"

skills:
  required:
    - name: aurora-vault
      via: repo
  recommended:
    - name: mcp-atlassian
      via: cursor-mcp
      note: "Authenticate in Cursor MCP with YOUR Atlassian account (never commit tokens)."
{sync_skills}
{sources}
atlassian:
  confluence:
    base_url: "{c['conf_url']}"
    space: "{c['conf_space']}"
    sync_roots: {roots}
  jira:
    base_url: "{c['jira_url']}"
    project_key: "{c['jira_key']}"
    default_jql: "{c['jira_jql']}"
  auth:
    mode: mcp_user

paths:
  knowledge_db: AuroraKnowledgeDB
  sources_confluence: Sources/Confluence
  sources_jira: Sources/JIRA

privacy:
  # Режим kb:scrub — свойство контура, а не вкуса.
  #   off    — репозиторий в закрытом git, те же тексты открыты команде: искать нечего
  #   report — искать и показывать, маскирование только руками
  #   mask   — маскирование ожидается: находки становятся ошибкой
  scrub: {c['scrub']}
  mask_contacts: false   # почта в Reporter:/Assignee: — атрибуция, не ПДн
  include_raw: false     # Raw/ и released/ — доказательства, правка ломает неизменяемость

bootstrap:
  verified_threshold_pct: {c['threshold']}
"""
    path.write_text(text, encoding="utf-8")


# ---------- интерактив ----------

def slugify(name: str) -> str:
    parts = [p for p in re.split(r"[\s_\-]+", name) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Project"


def skill_prefixes(target: Path) -> list:
    """Префиксы имён sync-скиллов — из манифестов модулей, а не из списка в коде."""
    try:
        import sources_registry as R
    except ImportError:
        return []
    return sorted({(m.get("run") or {}).get("skill", "")
                   for m in R.installed(str(target)).values()} - {""})


def reconcile_sync_skills(target: Path, slug: str):
    """Привести имена папок sync-скиллов к текущему slug (переименовать при расхождении).

    install мог создать их с угаданным slug; если пользователь сменил slug в setup —
    переименовываем, чтобы конфиг и папки совпадали. Работает без доступа к kit.
    """
    skdir = target / ".opencode/skills"
    if not skdir.is_dir():
        return
    for kind in skill_prefixes(target):
        want = skdir / f"{kind}-{slug}"
        existing = [d for d in skdir.glob(f"{kind}-*") if d.is_dir() and d != want]
        if want.exists():
            continue
        if existing:  # переименовать первый найденный
            old = existing[0]
            old.rename(want)
            # поправить имя внутри SKILL.md / skill.json
            for f in want.rglob("*"):
                if f.is_file() and f.suffix in (".md", ".json"):
                    t = f.read_text(encoding="utf-8")
                    t = re.sub(rf"{kind}-\S+", f"{kind}-{slug}", t)
                    f.write_text(t, encoding="utf-8")
            print(f"  переименован скилл: {old.name} → {want.name}")


def run_answers(target: Path, answers: dict) -> int:
    """Настройка по готовым ответам — тем же путём, что и диалог.

    Форму задаёт панель, а записывает всё этот же скрипт: иначе у настройки появилось бы
    два разных способа собрать конфиг, и они разошлись бы на второй же правке.
    Ключи те же, что поля диалога; `sync_roots` — список пар page_id и title.
    """
    cfg_path = target / "aurora.config.yaml"
    c = read_config(cfg_path)
    for key in ("name", "slug", "conf_url", "conf_space", "jira_url", "jira_key",
                "jira_jql", "scrub", "threshold"):
        if key in answers and str(answers[key]).strip():
            c[key] = str(answers[key]).strip()
    if "sync_roots" in answers:
        roots, unresolved = [], []
        for item in answers["sync_roots"] or []:
            pid = str(item.get("page_id", "")).strip()
            if not pid:
                continue
            m = re.search(r"pageId=(\d+)", pid)     # вставили ссылку целиком — берём номер
            if m:
                pid = m.group(1)
            if not pid.isdigit():
                unresolved.append(pid)
            roots.append((pid, str(item.get("title") or f"page {pid}").strip()))
        c["sync_roots"] = roots
        for raw in unresolved:
            print(f"⚠️  не номер страницы: {raw}\n"
                  "    синк такой корень использовать не сможет. Confluence показывает номер "
                  "в адресе вида …/pages/viewpage.action?pageId=NNN — либо откройте страницу "
                  "через «…» → Page Information, либо дайте панели разрешить ссылку.",
                  file=sys.stderr)
    if c["scrub"] not in ("off", "report", "mask"):
        c["scrub"] = "report"
    reconcile_sync_skills(target, c["slug"])
    write_config(cfg_path, c)
    print(f"✅ Записано: {cfg_path}")
    print(f"   Confluence {c['conf_url']} · пространство {c['conf_space']} · "
          f"корней синка {len(c['sync_roots'])}")
    print(f"   Jira {c['jira_url']} · проект {c['jira_key']}")
    print(f"   Приватность: {c['scrub']} · порог bootstrap: {c['threshold']}%")
    return 0


def run(target: Path, interactive: bool):
    cfg_path = target / "aurora.config.yaml"
    c = read_config(cfg_path)
    editing = cfg_path.is_file()

    print(f"\n{'Редактирование' if editing else 'Настройка'} проекта Aurora — {target}")
    print("Пустой ввод оставляет текущее значение в [скобках].\n" if interactive else "(неинтерактивно)\n")

    def ask(prompt, cur, allow_empty=True):
        if not interactive:
            return cur
        raw = input(f"  {prompt} [{cur}]: ").strip()
        if not raw:
            return cur
        return raw

    # 1. проект
    c["name"] = ask("Название проекта", c["name"] or "My Project")
    default_slug = c["slug"] or slugify(c["name"])
    c["slug"] = ask("Slug (латиница, для имён скиллов)", default_slug)

    # 2. Confluence
    print("\nConfluence:")
    c["conf_url"] = ask("base URL", c["conf_url"] or "https://confluence.example.com")
    c["conf_space"] = ask("space key", c["conf_space"] or c["slug"])
    if interactive:
        # Корней синка почти всегда несколько (разделы пространства). Раньше добавление
        # пряталось за вопросом «действие [оставить/clear/add]», и на Enter не добавлялось
        # ничего — человек уходил с одним корнем или без корней вовсе.
        if c["sync_roots"]:
            print(f"\n  Корневые страницы для синка — сейчас {len(c['sync_roots'])}:")
            for pid, title in c["sync_roots"]:
                print(f"    · {pid} — {title}")
            act = ask("  Оставить как есть / добавить ещё / очистить [оставить/add/clear]",
                      "оставить").lower()
            if act.startswith("c"):
                c["sync_roots"] = []
            add_more = act.startswith(("a", "д", "c"))
        else:
            print("\n  Корневые страницы для синка. Тянется страница и всё её поддерево,"
                  "\n  поэтому обычно указывают несколько разделов пространства.")
            add_more = True
        while add_more:
            n = len(c["sync_roots"]) + 1
            pid = input(f"    {n}) page_id (Enter — закончить): ").strip()
            if not pid:
                break
            if not pid.isdigit():
                # человек вставил ссылку целиком — вытащим номер сами
                import re as _re
                m = _re.search(r"pageId=(\d+)", pid)
                if m:
                    pid = m.group(1)
                else:
                    print("       нужен номер страницы из URL (…viewpage.action?pageId=NNN)")
                    continue
            title = input("       название (Enter — по номеру): ").strip() or f"page {pid}"
            c["sync_roots"].append((pid, title))
        if c["sync_roots"]:
            print(f"  → корней синка: {len(c['sync_roots'])}")

    # 3. Jira
    print("\nJira:")
    c["jira_url"] = ask("base URL", c["jira_url"] or "https://jira.example.com")
    c["jira_key"] = ask("project key", c["jira_key"] or c["slug"].upper())
    # если JQL пуст или это авто-шаблон для другого ключа — пересобрать под текущий ключ
    auto = re.fullmatch(r"project = \S+ ORDER BY updated DESC", c["jira_jql"] or "")
    default_jql = (f"project = {c['jira_key']} ORDER BY updated DESC"
                   if not c["jira_jql"] or auto else c["jira_jql"])
    c["jira_jql"] = ask("default JQL", default_jql)

    # 4. приватность
    print("\nПриватность (kb:scrub): off — закрытый контур, ПДн не ищем; "
          "report — показывать находки; mask — маскирование обязательно")
    while True:
        mode = ask("Режим off/report/mask", c["scrub"] or "report").strip().lower()
        if mode in ("off", "report", "mask"):
            c["scrub"] = mode
            break
        print("    Допустимо: off, report, mask")

    # 5. bootstrap
    c["threshold"] = ask("\nBootstrap: порог % verified для строгого ретрива", c["threshold"] or "20")

    reconcile_sync_skills(target, c["slug"])
    write_config(cfg_path, c)
    print(f"\n✅ Записано: {cfg_path}")
    print(f"   Confluence space {c['conf_space']} · {len(c['sync_roots'])} корневых страниц · Jira {c['jira_key']}")
    print("\nДальше:")
    print("  • проверка:  python3 .opencode/scripts/aurora_doctor.py")
    print(f"  • синк:      /confluence-sync-{c['slug']} · /jira-export-{c['slug']}")
    print("  • сборка:    /aurora-vault build")
    print("  • изменить настройки позже — просто запустите этот скрипт снова.")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Интерактивная настройка проекта Aurora")
    ap.add_argument("--target", default=".", help="Корень проекта (по умолчанию — текущая папка)")
    ap.add_argument("--non-interactive", action="store_true", help="Без вопросов (значения по умолчанию/текущие)")
    ap.add_argument("--json", metavar="FILE",
                    help="ответы из JSON-файла или '-' для stdin (режим формы: панель)")
    a = ap.parse_args()
    target = Path(a.target).expanduser().resolve()
    if not (target / "AuroraKnowledgeDB").is_dir() and not (target / "aurora.config.yaml").is_file():
        print(f"⚠️  {target} не похоже на проект Aurora (нет AuroraKnowledgeDB/ и aurora.config.yaml).", file=sys.stderr)
        print("   Разверните сначала: python3 aurora.py new <target>", file=sys.stderr)
        return 1
    if a.json:
        import json as _json
        raw = sys.stdin.read() if a.json == "-" else Path(a.json).read_text(encoding="utf-8")
        try:
            answers = _json.loads(raw or "{}")
        except Exception as e:
            print(f"aurora_setup: не разобран JSON с ответами: {e}", file=sys.stderr)
            return 2
        return run_answers(target, answers)
    return run(target, interactive=not a.non_interactive)


if __name__ == "__main__":
    sys.exit(main())
