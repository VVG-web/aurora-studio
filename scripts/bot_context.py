"""bot_context.py — контекст проекта для бота: ссылки из задания → понятия → поиск Авроры.

Бот проверяет артефакты проекта по расписанию, но до 1.163.0 знал о проекте только то,
что сам успевал найти инструментами по ходу работы, — а «Продуктивность» с первого шага
кладёт в задание пак знаний. Здесь бот получает то же самое, до начала работы:

1. **Ссылки задания.** Страницы Confluence (`pageId=…`, `/display/…`), задачи Jira
   (`ABC-123`) и карточки базы (`[[имя]]`), названные в задании бота, читаются движком:
   заголовок и начало текста.
2. **Понятия.** Модель роли «Планировщик» называет до 15 понятий, терминов и кодов, по
   которым в базе нужно искать знание для этой работы. Модель не ответила — понятия берутся
   правилом: коды, аббревиатуры, «кавычки», заголовки ссылок.
3. **Поиск.** Гибридный поиск Авроры (`ctx_pack`: слова и смысл по индексу `kb:embed`) по
   базе **проекта знаний** бота — своего или выбранного в поле «Проект знаний». Режим
   `generate` — только доверенное знание; `evaluate` — всё, с пометками доверия.

Пак идёт в задание бота перед самой работой; по ходу работы бот ищет в той же базе сам —
`kb_search`, `kb_context` и инструментами сервера `aurora`.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODES = ("generate", "evaluate", "off")
MAX_LINKS = 8
LINK_TEXT = 3000             # знаков с одной ссылки — для понятий, не для работы
MAX_CONCEPTS = 15
PACK_BUDGET = 40_000         # знаков пака: контекст, а не вся база

CONF_LINK = re.compile(r"https?://[^\s)\]>\"']*?(?:pageId=(\d+)|/display/[^\s)\]>\"']+)")
CONF_ID = re.compile(r"\bpageId=(\d+)")
JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9_]{1,15}-\d{1,7})\b")
WIKI = re.compile(r"\[\[([^\]|#]+)")
CODE = re.compile(r"\b(?:[A-Z]{2,}[A-Za-z]*[-.](?:\d+[.\-]?)+\d*|[A-ZА-ЯЁ]{2,}\d*)\b")
QUOTED = re.compile(r"«([^»]{3,60})»")

PROMPT = """Ниже — задание бота и материалы по ссылкам из него. Бот будет работать с проектом,
и ему нужен контекст из базы знаний этого проекта.

Назови до {n} ключевых понятий, терминов, кодов артефактов (US-…, ALG-…, AC-…), систем,
процессов и ролей, по которым в базе знаний проекта нужно найти знание для этой работы.
Только то, что действительно относится к предмету работы; общие слова («проверка», «отчёт»,
«задача») не называй.

Ответь строго JSON-объектом: {{"concepts": ["...", "..."]}}

## Задание бота

{body}

## Материалы по ссылкам

{links}
"""


def links_of(text: str) -> dict:
    """Ссылки задания: {confluence: [номер или адрес], jira: [ключ], cards: [имя]}."""
    conf = []
    for m in CONF_LINK.finditer(text or ""):
        conf.append(m.group(1) or m.group(0))
    for m in CONF_ID.finditer(text or ""):
        if m.group(1) not in conf:
            conf.append(m.group(1))
    jira = [k for k in dict.fromkeys(JIRA_KEY.findall(text or ""))
            if not re.match(r"^(US|ALG|AC|UC|BR|FR|NFR|SPR|UI|RU|SW)-", k)]
    cards = [c.strip() for c in dict.fromkeys(WIKI.findall(text or "")) if c.strip()]
    return {"confluence": list(dict.fromkeys(conf)), "jira": jira, "cards": cards}


def read_links(kb_root: str, found: dict, say=print) -> list:
    """Заголовок и начало текста каждой ссылки — [{kind, ref, title, text}]. Недоступная
    ссылка не роняет бота: строка с причиной идёт в журнал, работа продолжается."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    out = []
    todo = ([("confluence", r) for r in found["confluence"]] + [("jira", k) for k in found["jira"]]
            + [("card", c) for c in found["cards"]])[:MAX_LINKS]
    for kind, ref in todo:
        try:
            if kind == "card":
                import aurora_mcp as M
                path = M.card_path(kb_root, ref)
                if not path:
                    raise ValueError("карточки нет в базе")
                with open(path, encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                out.append({"kind": kind, "ref": ref, "title": ref, "text": text[:LINK_TEXT]})
                continue
            import atlassian_ops as A
            if kind == "confluence":
                page = A.op_confluence_page({"page": ref}, kb_root)
                out.append({"kind": kind, "ref": ref, "title": page.get("title", ""),
                            "text": page.get("text", "")[:LINK_TEXT]})
            else:
                it = A.op_jira_issue({"issue": ref}, kb_root)
                out.append({"kind": kind, "ref": ref, "title": it.get("summary", ""),
                            "text": (it.get("description") or "")[:LINK_TEXT // 2]})
        except Exception as e:  # noqa: BLE001 — ссылка не прочиталась: сказать и дальше
            say(f"  контекст: ссылка {ref} не прочитана — {str(e)[:160]}")
    return out


def concepts_by_rule(body: str, links: list) -> list:
    """Понятия без модели: коды, аббревиатуры, «кавычки», заголовки ссылок."""
    found = [x["title"] for x in links if x.get("title")]
    found += QUOTED.findall(body or "")
    found += CODE.findall(body or "")
    stop = {"MCP", "JSON", "JQL", "API", "URL", "HTTP", "UTC", "ID", "LLM", "AI", "OK", "MD"}
    out = []
    for c in found:
        c = " ".join(str(c).split())
        if c and c.upper() not in stop and c not in out:
            out.append(c)
    return out[:MAX_CONCEPTS]


def concepts_by_model(cfg: dict, call, body: str, links: list) -> list:
    """Понятия моделью роли «Планировщик». Не ответила или ответила не JSON — пусто."""
    text = "\n\n".join(f"### {x['kind']} {x['ref']}: {x['title']}\n{x['text']}" for x in links) \
        or "(ссылок нет)"
    prompt = PROMPT.format(n=MAX_CONCEPTS, body=(body or "")[:8000], links=text[:16000])
    try:
        r = call(cfg, "planner", [{"role": "user", "content": prompt}], thinking=False,
                 max_tokens=800)
    except Exception:  # noqa: BLE001
        return []
    if not r.get("ok"):
        return []
    raw = (r.get("text") or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        return []
    items = data.get("concepts") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    out = []
    for c in items:
        c = " ".join(str(c).split())[:80]
        if c and c not in out:
            out.append(c)
    return out[:MAX_CONCEPTS]


def search(kb_root: str, concepts: list, mode: str, budget: int = PACK_BUDGET) -> tuple:
    """Гибридный поиск Авроры по базе проекта знаний → (пак, число карточек)."""
    script = os.path.join(kb_root, ".aurora", "scripts", "ctx_pack.py")
    if not os.path.isfile(script):
        script = os.path.join(HERE, "ctx_pack.py")
    topic = "; ".join(concepts)
    try:
        p = subprocess.run([sys.executable, script, "--mode", mode, "--no-log",
                            "--budget", str(budget), "--", topic], cwd=kb_root,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return "", 0
    pack = (p.stdout or "").strip()
    if "## " not in pack:
        return "", 0
    m = re.search(r"карточек (\d+)", pack)
    return pack, int(m.group(1)) if m else pack.count("\n## ")


def gather(kb_root: str, body: str, cfg: dict, call, mode: str = "generate",
           name: str = "", say=print) -> dict:
    """Контекст проекта для бота → {block, concepts, links, cards, mode}. `mode=off` — пусто."""
    if mode == "off":
        return {"block": "", "concepts": [], "links": [], "cards": 0, "mode": mode}
    found = links_of(body)
    links = read_links(kb_root, found, say)
    concepts = concepts_by_model(cfg, call, body, links) if call else []
    how = "моделью"
    if not concepts:
        concepts, how = concepts_by_rule(body, links), "правилом"
    pack, cards = search(kb_root, concepts, mode) if concepts else ("", 0)
    say(f"  контекст проекта «{name or os.path.basename(kb_root)}»: ссылок {len(links)}, "
        f"понятий {len(concepts)} ({how}), карточек {cards}, режим {mode}")
    if not pack and not links:
        return {"block": "", "concepts": concepts, "links": links, "cards": 0, "mode": mode}
    head = [f"## Контекст проекта «{name or os.path.basename(kb_root)}» из базы знаний Авроры",
            "", "Собран до начала работы: ссылки задания, понятия, гибридный поиск по базе. "
            "Опирайся на него; не хватит — ищи сам (`kb_search`, `kb_context`, сервер `aurora`).",
            ""]
    if concepts:
        head += ["Понятия поиска: " + "; ".join(concepts), ""]
    if links:
        head += ["### Ссылки из задания", ""]
        head += [f"- {x['kind']} {x['ref']}: {x['title']}" for x in links]
        head += [""]
    return {"block": "\n".join(head) + (pack + "\n\n" if pack else ""), "concepts": concepts,
            "links": links, "cards": cards, "mode": mode}
