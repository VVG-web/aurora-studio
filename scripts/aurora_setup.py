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

# Скаляр читается в трёх формах: 'одинарные кавычки' (внутри '' — это кавычка),
# "двойные" и без кавычек. Одинарные — единственный способ хранить значение с `"`
# внутри: JQL с датой вида created >= "2026-11-01" в двойных кавычках ломал строку,
# и ни один читатель конфига её не разбирал — поле после сохранения выглядело пустым.
def yaml_scalar(text: str, key: str, default: str = "") -> str:
    m = re.search(
        rf"""^\s*{re.escape(key)}\s*:\s*(?:'((?:[^'\n]|'')*)'|"([^"\n]*)"|([^"\n#]+?))\s*$""",
        text, re.M)
    if m:
        if m.group(1) is not None:
            return m.group(1).replace("''", "'").strip()
        return (m.group(2) if m.group(2) is not None else m.group(3)).strip()
    # Конфиги, записанные до 1.113.2: кавычки внутри значения ломали строку, и её читает
    # только снятие крайних кавычек целиком — иначе старый JQL пришлось бы вводить заново.
    m = re.search(rf'^\s*{re.escape(key)}\s*:\s*"(.+)"\s*$', text, re.M)
    return m.group(1).strip() if m else default


def yaml_str(s: str) -> str:
    """Скаляр для записи в конфиг: обычно в двойных кавычках, а при `"` внутри —
    в YAML-одинарных (там `"` легален, а `'` удваивается)."""
    s = str(s)
    return f'"{s}"' if '"' not in s else "'" + s.replace("'", "''") + "'"


def read_config(path: Path) -> dict:
    """Достаёт известные скалярные ключи и sync_roots из aurora.config.yaml."""
    cfg = {
        "name": "", "slug": "",
        "conf_url": "", "conf_space": "", "sync_roots": [], "web_pages": [],
        # Настройки обхода веб-страниц. Читаются и возвращаются на место при каждой
        # записи конфига: панель правит СПИСОК адресов, и перезапись блока целиком
        # стирала всё остальное. На живом проекте так пропала глубина обхода — человек
        # добавил два адреса, а зеркало молча перестало ходить по внутренним ссылкам.
        "web_depth": "", "web_assets": "", "web_max_pages": "",
        "jira_url": "", "jira_key": "", "jira_jql": "",
        "trust_statuses": "", "assumption_statuses": "",
        "trusted_sources": "", "trusted_branches": "",
        "threshold": "20",
    }
    if not path.is_file():
        return cfg
    text = path.read_text(encoding="utf-8")

    def scalar(key, default=""):
        return yaml_scalar(text, key, default)

    cfg["name"] = scalar("name")
    cfg["slug"] = scalar("slug")
    # confluence блок
    cm = re.search(r"confluence:(.*?)(?:\n  jira:|\Z)", text, re.S)
    cblock = cm.group(1) if cm else ""
    cfg["conf_url"] = (re.search(r'base_url:\s*"?([^"\n]+)', cblock) or [None, ""])[1].strip() if cblock else ""
    cfg["conf_space"] = (re.search(r'space:\s*"?([^"\n]+)', cblock) or [None, ""])[1].strip() if cblock else ""
    # web: пары url / галочка доверия. Список свой, не смешанный с корнями Confluence:
    # это разные источники, и доверие у них считается по разным правилам.
    for m in re.finditer(r"-\s*url\s*:\s*(\S+)\s*\n\s*trusted\s*:\s*(\S+)", text):
        cfg["web_pages"].append((m.group(1).strip().strip('"\''),
                                 m.group(2).strip().strip('"\'').lower()
                                 in ("true", "yes", "да")))
    wblock = (re.search(r"(?ms)^web:\s*$(.*?)(?=^\S|\Z)", text) or [None, ""])[1]
    for key, name in (("web_depth", "depth"), ("web_assets", "assets"),
                      ("web_max_pages", "max_pages")):
        m = re.search(rf"^\s+{name}\s*:\s*(\S+)", wblock, re.M)
        if m:
            cfg[key] = m.group(1).strip().strip('"\'')
    # sync_roots: пары page_id / title
    for m in re.finditer(r'page_id:\s*"?([^"\n]+?)"?\s*\n\s*title:\s*"?([^"\n]+?)"?\s*(?:\n|$)', cblock):
        cfg["sync_roots"].append((m.group(1).strip(), m.group(2).strip()))
    # jira блок
    jm = re.search(r"jira:(.*?)(?:\n  auth:|\Z)", text, re.S)
    jblock = jm.group(1) if jm else ""
    cfg["jira_url"] = (re.search(r'base_url:\s*"?([^"\n]+)', jblock) or [None, ""])[1].strip() if jblock else ""
    cfg["jira_key"] = (re.search(r'project_key:\s*"?([^"\n]+)', jblock) or [None, ""])[1].strip() if jblock else ""
    cfg["jira_jql"] = yaml_scalar(jblock, "default_jql") if jblock else ""
    for key in ("trust_statuses", "assumption_statuses"):
        # список читаем как есть, вместе с кавычками: статусы бывают с дефисом и пробелом
        m = re.search(rf'{key}:\s*\[([^\]]*)\]', jblock, re.M) if jblock else None
        cfg[key] = m.group(1).strip() if m else ""
    vm = re.search(r"\nverify:(.*?)(?:\n[a-z_]+:|\Z)", text, re.S)
    vblock = vm.group(1) if vm else ""
    for key in ("trusted_sources", "trusted_branches"):
        m = re.search(rf'{key}:\s*\[([^\]]*)\]', vblock, re.M) if vblock else None
        cfg[key] = m.group(1).strip() if m else ""
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


def top_blocks(text: str) -> list:
    """[(ключ, текст)] — разделы верхнего уровня вместе с комментариями над ними."""
    blocks, key, buf, pending = [], None, [], []
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:", line)
        if m:
            if key is not None:
                blocks.append((key, "\n".join(buf).rstrip("\n")))
            key, buf, pending = m.group(1), pending + [line], []
        elif key is not None and line.startswith((" ", "\t", "-")):
            buf.extend(pending)
            pending = []
            buf.append(line)
        else:
            pending.append(line)
    if key is not None:
        blocks.append((key, "\n".join(buf).rstrip("\n")))
    return blocks


def sub_blocks(block: str) -> list:
    """[(ключ, текст)] — ключи второго уровня раздела с их вложенным содержимым."""
    subs, key, buf, pending = [], None, [], []
    for line in block.splitlines()[1:]:
        m = re.match(r"^  ([A-Za-z_][\w-]*)\s*:", line)
        if m:
            if key is not None:
                subs.append((key, "\n".join(buf).rstrip("\n")))
            key, buf, pending = m.group(1), pending + [line], []
        elif key is not None and re.match(r"^(   |\t|  -)", line):
            buf.extend(pending)
            pending = []
            buf.append(line)
        else:
            pending.append(line)
    if key is not None:
        subs.append((key, "\n".join(buf).rstrip("\n")))
    return subs


def fill_trust_defaults(text: str) -> tuple:
    """(текст, что записано) — значения доверия по умолчанию в конфиг проекта.

    Правятся только пустые списки и отсутствующие ключи доверия: всё, что проект задал сам,
    и остальной текст конфига остаются как были. Ключ `verify.trusted_sections`, который
    никто не читал, убирается. Повторный вызов ничего не меняет.
    """
    from aurora_common import TRUST_DEFAULTS, yaml_list
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    done = []
    kept = [ln for ln in lines if not re.match(r"^\s+trusted_sections\s*:", ln)]
    if len(kept) != len(lines):
        lines = kept
        done.append("trusted_sections убран")

    def indent(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def block(head: str, level: int):
        """(начало, конец) блока `head:` на отступе level; None — блока нет."""
        for i, line in enumerate(lines):
            if line.rstrip() == " " * level + head + ":":
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or indent(lines[j]) > level):
                    j += 1
                while j > i + 1 and not lines[j - 1].strip():
                    j -= 1
                return i, j
        return None

    for key, parent, level in (("trust_statuses", "jira", 2), ("assumption_statuses", "jira", 2),
                               ("trusted_sources", "verify", 0), ("trusted_branches", "verify", 0)):
        value = f"{key}: [{yaml_list(TRUST_DEFAULTS[key])}]"
        at = next((i for i, ln in enumerate(lines) if re.match(rf"^\s*{key}\s*:", ln)), None)
        if at is not None:
            m = re.match(rf"^(\s*){key}\s*:\s*\[\s*\]\s*(#.*)?$", lines[at].rstrip("\n"))
            if m:
                lines[at] = f"{m.group(1)}{value}\n"
                done.append(key)
            continue
        where = block(parent, level)
        if where is None and parent == "verify":
            stop = next((i for i, ln in enumerate(lines) if ln.startswith("privacy:")), len(lines))
            if stop == len(lines):
                if lines and lines[-1].strip():
                    lines.append("\n")
                lines.append("verify:\n")
            else:
                lines[stop:stop] = ["verify:\n", "\n"]
            where = block("verify", 0)
        if where is None:
            continue          # блока jira нет — статусы задачи задавать не к чему
        lines.insert(where[1], " " * (level + 2) + value + "\n")
        done.append(key)
    return "".join(lines), done


# Ключи, которые движок больше не читает: форма их не пишет и из прежнего конфига не
# переносит. `verify.trusted_sections` форма писала, а не читал никто — доверие наследуется
# от источника, а не от раздела базы (решение 15.09.2026).
RETIRED = {("verify", "trusted_sections")}


def carry_unmanaged(old: str, new: str) -> str:
    """Перенести из прежнего конфига всё, чего шаблон настройки не пишет.

    Настройка собирает конфиг заново по шаблону, и всё, что шаблону неизвестно, пропадало
    при первом же сохранении формы: на живом проекте так ушли реестр из десяти видов
    документов (`artifacts:`) и настройки отчёта (`reports:`), а объявленная папка проекта
    (`paths → extra_structure_dirs`) пропала бы у следующего. Шаблон знает то, что правит
    форма; остальное — чужое, и форма не вправе его стирать.

    Разделы, которых нет в шаблоне, переносятся целиком в конец файла; ключи второго
    уровня, которых нет в разделе шаблона, — в конец своего раздела. Повторная запись даёт
    тот же текст.
    """
    if not old.strip():
        return new
    new_blocks = top_blocks(new)
    have_top = {k for k, _b in new_blocks}
    old_map = dict(top_blocks(old))
    out = new
    for key, block in new_blocks:
        was = old_map.get(key)
        if not was:
            continue
        have = {k for k, _b in sub_blocks(block)}
        lost = [b for k, b in sub_blocks(was) if k not in have and (key, k) not in RETIRED]
        if lost and block in out:
            out = out.replace(block, block.rstrip("\n") + "\n" + "\n".join(lost), 1)
    extra = [b.strip("\n") for k, b in top_blocks(old) if k not in have_top]
    if extra:
        out = out.rstrip("\n") + "\n\n" + "\n\n".join(extra) + "\n"
    return out


def write_config(path: Path, c: dict):
    # Поля доверия пустыми не пишутся: пустое поле — значения по умолчанию, и в конфиг они
    # попадают явно, чтобы настройку было видно и её правили, а не угадывали.
    from aurora_common import TRUST_DEFAULTS, yaml_list
    c = dict(c)
    for key, values in TRUST_DEFAULTS.items():
        if not str(c.get(key) or "").strip():
            c[key] = yaml_list(values)
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
    pages = ""
    for key, name in (("web_depth", "depth"), ("web_assets", "assets"),
                      ("web_max_pages", "max_pages")):
        if str(c.get(key) or "").strip():
            pages += f"\n  {name}: {c[key]}"
    pages += "\n  pages: []"
    if c.get("web_pages"):
        rows = "\n".join(f'    - url: {u}\n      trusted: {"true" if tr else "false"}'
                          for u, tr in c["web_pages"] if str(u).strip())
        if rows:
            pages = pages.replace("\n  pages: []", "\n  pages:\n" + rows)
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
    default_jql: {yaml_str(c['jira_jql'])}
    trust_statuses: [{c['trust_statuses']}]
    assumption_statuses: [{c['assumption_statuses']}]
  auth:
    mode: mcp_user

# Веб-страницы: свой список, отдельный от корней Confluence. Страница из интернета и
# страница корпоративной вики приходят из разных мест и доверяются по разным правилам.
# Задаче доверие даёт её статус, документу — тот, кто его подключил, поэтому галочка
# `trusted` стоит у КАЖДОЙ ссылки. Она уходит в шапку сохранённого файла, и `kb:trust`
# читает её оттуда.
web:{pages}

paths:
  knowledge_db: AuroraKnowledgeDB
  sources_confluence: Sources/Confluence
  sources_jira: Sources/JIRA

verify:
  # Доверие по происхождению (`kb:trust`): что собрано из договора, ТЗ, материалов
  # заказчика или ветки вики с описанием системы, пересказывает уже решённое.
  # trusted_sources — папки и файлы по пути; trusted_branches — верхние ветки вики по
  # названию (целым словом, приставки не мешают). Значения по умолчанию записаны при
  # настройке — правьте под проект; пустой список снова означает значения по умолчанию.
  trusted_sources: [{c['trusted_sources']}]
  trusted_branches: [{c['trusted_branches']}]

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
    old_text = path.read_text(encoding="utf-8") if path.is_file() else ""
    path.write_text(carry_unmanaged(old_text, text), encoding="utf-8")


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
    # Поля доверия очищаются: пустой список — настройка по умолчанию, и человек, стерев
    # поле, возвращается к ней. Прочие поля пустой ответ не трогает, как и раньше.
    for key in ("trust_statuses", "assumption_statuses", "trusted_sources", "trusted_branches"):
        if key in answers:
            c[key] = str(answers[key] or "").strip()
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
    if "web_pages" in answers:
        # Пустая строка адреса — человек добавил поле и передумал: молча выбрасываем.
        # Галочка приходит как есть: «не отмечено» — это осознанное «не доверять», а не
        # отсутствие ответа, и превращать её в доверие нельзя ни при каких условиях.
        c["web_pages"] = [(str(w.get("url") or "").strip(), bool(w.get("trusted")))
                          for w in (answers["web_pages"] or [])
                          if str(w.get("url") or "").strip()]
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
    print("  • проверка:  в панели `kit:doctor`")
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
