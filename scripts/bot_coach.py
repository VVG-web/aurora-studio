"""bot_coach.py — разговор с моделью о промпте бота: критический разбор и правки по просьбе.

Бот работает по расписанию без человека, и промпт, написанный «как для человека», ломается
тихо: модель ищет скрипт, которого нет, ждёт переменную окружения, ходит прямым HTTP —
и честно пишет в отчёте, что не вышло. Узнаётся это после прогона. Разбор показывает это
до прогона.

Движок собирает факты, на которые опирается модель, — не её догадки:
- инструменты, которые бот получит на самом деле: встроенные (`read_file`, `kb_search`…),
  инструменты своих MCP-серверов (их список спрашивается у самих серверов, с описаниями),
  серверы машины и проекта, которые можно подключить;
- признаки того, чего у бота нет: переменные окружения, скрипты и командная строка, прямые
  REST-запросы, файловые замки;
- какие серверы бота промпт не упоминает вовсе (кандидаты в лишние);
- срок прогона, лимит вызовов инструментов, роль и модель, проект знаний, разрешена ли
  запись в Jira.

Первый разбор — по разделам: что выполнится, что нет и почему, каких инструментов не хватает,
что лишнее, риски, что улучшить. Дальше — разговор: по просьбе человека модель возвращает
промпт целиком (между `<<<PROMPT` и `PROMPT>>>`), панель ставит его в редактор, и разговор
идёт уже об изменённом. Предложение подключить или убрать серверы — строкой `<<<META …>>>`,
панель показывает его кнопками.

Разговор хранится в `.aurora/state/bots/coach/<бот>.json` (вне git) и продолжается после
перезапуска панели.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

COACH_ROLE = "critic"           # разбор — роль «Критик: проверка решений»
HISTORY_KEEP = 40               # реплик в разговоре; дальше — с головы
INVENTORY_TTL = 6 * 3600        # список инструментов серверов живёт столько, «Обновить» — сразу
PROMPT_BLOCK = re.compile(r"<<<PROMPT\s*\n(.*?)\n\s*PROMPT>>>", re.S)
META_BLOCK = re.compile(r"<<<META\s*(\{.*?\})\s*META>>>", re.S)

BUILTIN = [
    ("read_file", "прочитать файл проекта знаний бота (шаблон, промпт, артефакт)"),
    ("list_dir", "что лежит в папке проекта знаний бота"),
    ("kb_search", "поиск по базе знаний проекта (слова и смысл), до 12 карточек"),
    ("kb_context", "пак знаний по теме — только доверенные карточки"),
    ("artifact_spec", "настройки вида документа: шаблон, промпт, папка результата"),
    ("save_output", "сохранить файл результата в папку прогона бота; вернёт полный путь"),
]

ENV_RE = re.compile(r"`?\$?\{?\b([A-Z][A-Z0-9]*_[A-Z0-9_]{2,})\b\}?`?")
REST_RE = re.compile(r"(?:\b(?:GET|POST|PUT|DELETE|PATCH)\s+)?(/rest/api/[\w/{}<>.\-?=&]+|/wiki/rest/[\w/]+)")
SHELL_RE = re.compile(r"(?:`|\b)([\w.\-]+\.(?:sh|py|ps1|bat))\b|\b(trap|crontab|curl|wget|chmod|"
                      r"sudo|systemctl|pip install|npm install)\b|\b(export [A-Z_]+=)")
LOCK_RE = re.compile(r"\b(?:lock[- _]?file|LOCK_FILE|PID|flock|lockfile)\b", re.I)


class CoachError(Exception):
    """Отказ разбора — уходит человеку текстом причины."""


def state_file(project, rel: str) -> Path:
    stem = Path(rel).stem
    return Path(project) / ".aurora" / "state" / "bots" / "coach" / f"{stem}.json"


def load(project, rel: str) -> dict:
    try:
        data = json.loads(state_file(project, rel).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(project, rel: str, data: dict) -> None:
    path = state_file(project, rel)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    from aurora_common import replace_file
    replace_file(tmp, path)


# ------------------------------------------------------------------ факты

def params_line(schema) -> str:
    """Параметры инструмента одной строкой: `page*` — обязательный, варианты — через «|».
    Без них модель не проверит вызов из промпта: имя параметра или его форма — догадка."""
    if not isinstance(schema, dict):
        return ""
    need = set(schema.get("required") or [])
    out = []
    for name, p in (schema.get("properties") or {}).items():
        p = p if isinstance(p, dict) else {}
        kind = p.get("type") or ""
        if p.get("enum"):
            kind = "|".join(str(x) for x in p["enum"])
        elif isinstance(kind, list):
            kind = "|".join(str(x) for x in kind)
        elif kind == "array":
            kind = "[" + str((p.get("items") or {}).get("type") or "") + "]"
        bit = name + ("*" if name in need else "") + (f": {kind}" if kind else "")
        if p.get("description"):
            bit += f" ({str(p['description'])[:80]})"
        out.append(bit)
    return "; ".join(out)[:600]


def _tool(name: str, description: str, schema) -> dict:
    return {"name": name, "description": " ".join(str(description or "").split())[:400],
            "params": params_line(schema)}


def inventory(project, meta: dict, probe=None, cached: dict | None = None) -> dict:
    """Инструменты бота на самом деле → {builtin, selected: {сервер: {ok, tools|error}},
    available: {сервер: что делает}}. Серверы бота спрашиваются у самих серверов (с
    описаниями инструментов); сервер Авроры — по его списку без запуска."""
    import agent_core as AG
    import aurora_mcp as M
    all_servers = (AG.mcp_config(str(project), with_aurora=True).get("mcpServers") or {})
    chosen = [n for n in meta.get("mcp") or [] if n in all_servers]
    selected, to_probe = {}, {}
    for name in chosen:
        if name == AG.AURORA_MCP and "command" in all_servers[name] and \
                str(all_servers[name].get("args", [""])[0]).endswith("aurora_mcp.py"):
            selected[name] = {"ok": True, "tools": [_tool(t["name"], t["description"],
                                                          t.get("inputSchema")) for t in M.TOOLS]}
        else:
            to_probe[name] = all_servers[name]
    old = (cached or {}).get("selected") or {}
    fresh = {n: s for n, s in to_probe.items() if n not in old}
    if fresh:
        got = (probe or AG.mcp_probe)(str(project), servers=fresh, describe=True, timeout=60)
        for name in fresh:
            row = (got.get("servers") or {}).get(name) or {}
            tools = [_tool(t.get("name", ""), t.get("description"), t.get("schema"))
                     for t in row.get("list") or [] if isinstance(t, dict)]
            selected[name] = ({"ok": True, "tools": tools} if row.get("ok") else
                              {"ok": False, "error": row.get("error") or got.get("error")
                               or "сервер не ответил"})
    for name in to_probe:
        if name not in selected:
            selected[name] = old[name]
    available = {n: str((s or {}).get("about") or "")[:200] for n, s in all_servers.items()
                 if n not in chosen}
    return {"builtin": [{"name": n, "description": d} for n, d in BUILTIN],
            "selected": selected, "available": available, "at": time.time()}


def flags(body: str, inv: dict) -> dict:
    """Чего у бота нет, а промпт на это рассчитывает; какие серверы промпт не упоминает."""
    text = body or ""
    env = sorted({m for m in ENV_RE.findall(text)
                  if not re.match(r"^(US|ALG|AC|UC|RU|SW|SPR)_", m)})
    rest = sorted(set(REST_RE.findall(text)))[:20]
    shell = sorted({a or b or c for a, b, c in SHELL_RE.findall(text)})[:20]
    locks = bool(LOCK_RE.search(text))
    low = text.lower()
    unused, mentioned = [], {}
    for server, row in (inv.get("selected") or {}).items():
        names = [t["name"] for t in row.get("tools") or []]
        hit = [n for n in names if n.lower() in low or f"{server}_{n}".lower() in low]
        mentioned[server] = hit
        if server.lower() not in low and not hit:
            unused.append(server)
    return {"env": env, "rest": rest, "shell": shell, "locks": locks,
            "unused_servers": unused, "mentioned_tools": mentioned}


def settings(project, meta: dict) -> dict:
    """Условия прогона: срок, лимит вызовов, модель, проект знаний, запись в Jira."""
    import bots as B
    out = {"role": meta.get("role") or B.ROLE, "model": meta.get("model") or "",
           "tool_calls": B.TOOL_CALLS, "cron": meta.get("cron") or "",
           "knowledge": meta.get("knowledge") or "", "context": meta.get("context") or "generate",
           "skills": list(meta.get("skills") or []), "attachments": list(meta.get("attachments") or [])}
    try:
        import agent_core as AG
        out["budget_min"] = AG.config().get("budget_min")
    except Exception:  # noqa: BLE001
        out["budget_min"] = None
    try:
        import atlassian_ops as A
        root = B.knowledge_root(project, meta.get("knowledge") or "") or str(project)
        out["jira_write"] = A.settings(root)["jira_write"]
    except Exception:  # noqa: BLE001
        out["jira_write"] = None
    return out


def facts_text(inv: dict, fl: dict, st: dict) -> str:
    L = ["## Инструменты, которые бот получит (собрал движок — им верь)", "",
         "Встроенные (есть всегда, если стоит Pydantic AI):"]
    L += [f"- `{t['name']}` — {t['description']}" for t in inv["builtin"]]
    L += ["", "MCP-серверы бота (в задании модели их инструменты называются `<сервер>_<имя>`; "
          "в скобках — параметры, `*` — обязательный):"]
    if not inv["selected"]:
        L.append("- нет ни одного")
    for server, row in inv["selected"].items():
        if not row.get("ok"):
            L.append(f"- `{server}` — НЕ ОТВЕЧАЕТ: {row.get('error')}")
            continue
        L.append(f"- `{server}` — инструментов {len(row.get('tools') or [])}:")
        L += [f"  - `{t['name']}`" + (f"({t['params']})" if t.get("params") else "()")
              + (f" — {t['description']}" if t.get("description") else "")
              for t in row.get("tools") or []]
    L += ["", "Серверы машины и проекта, которые можно подключить боту:"]
    L += [f"- `{n}` — {about or 'без описания'}" for n, about in inv["available"].items()] or ["- нет"]
    L += ["", "## Чего у бота нет, а промпт на это рассчитывает (признаки в тексте)", ""]
    L.append("- Переменные окружения: " + (", ".join(fl["env"]) or "не найдены"))
    L.append("- Скрипты и командная строка: " + (", ".join(fl["shell"]) or "не найдены"))
    L.append("- Прямые REST-запросы: " + (", ".join(fl["rest"]) or "не найдены"))
    L.append("- Файловые замки и PID: " + ("упомянуты" if fl["locks"] else "нет"))
    L.append("- Серверы бота, которые промпт не упоминает: " + (", ".join(fl["unused_servers"]) or "нет"))
    L += ["", "## Условия прогона", ""]
    L.append(f"- Модель: " + (f"«{st['model']}» без запасных" if st["model"] else f"роль «{st['role']}»"))
    L.append(f"- Срок прогона: {st.get('budget_min') or '?'} мин; вызовов инструментов — не больше "
             f"{st['tool_calls']}")
    L.append(f"- Расписание: {st['cron'] or 'нет (только вручную)'}")
    L.append(f"- Проект знаний: {st['knowledge'] or 'свой'}; пак до работы: {st['context']}")
    L.append("- Запись в Jira через сервер `aurora`: " + {
        True: "разрешена проектом",
        False: "запрещена проектом — `jira_publish` работает только с `dry_run: true`, без него "
               "откажет; разрешает её человек строкой `jira_write: true` в разделе `mcp:` "
               "aurora.config.yaml",
        None: "не узнать"}[st.get("jira_write")])
    L.append(f"- Навыки: {', '.join(st['skills']) or 'нет'}; вложения: {', '.join(st['attachments']) or 'нет'}")
    return "\n".join(L)


SYSTEM = """Ты — критик и редактор промптов ботов «Авроры». Бот работает по расписанию, без
человека: вопросов не задаёт, ответов не ждёт. У него есть ТОЛЬКО инструменты из фактов ниже.
Скриптов, командной строки, переменных окружения, файловых замков и прямых HTTP-запросов у
бота нет — всё, что промпт поручает им, не выполнится, если нет инструмента с той же работой
(например, у сервера `aurora` есть время, память между прогонами, замок, ревью страницы,
поиск и публикация в Jira).

{facts}

## Текущий промпт бота «{name}»

<<<CURRENT
{body}
CURRENT>>>

Как отвечать:
- Первый разбор — по разделам: **Выполнится** · **Не выполнится и почему** · **Не хватает
  инструментов** (что подключить из доступных серверов; чего нет нигде — что сделать) ·
  **Лишнее** (серверы и инструменты, которые не будут вызваны) · **Риски** (запись во внешние
  системы, срок прогона, лимит вызовов, пробный и боевой режим) · **Что улучшить**.
- Ссылайся на шаги промпта и на точные имена инструментов. Не выдумывай инструментов,
  которых нет в фактах.
- Промпт меняй ТОЛЬКО когда человек об этом просит. Тогда верни новый промпт ЦЕЛИКОМ между
  строками `<<<PROMPT` и `PROMPT>>>` (без шапки YAML бота) и коротко — что изменил.
- Нужно подключить или убрать MCP-серверы бота — добавь строку
  `<<<META {{"mcp_add": ["имя"], "mcp_remove": ["имя"]}} META>>>`; только серверы из фактов.
- Отвечай {lang}, по делу, без воды.
"""

FIRST = ("Разбери промпт критически: что бот выполнит имеющимися инструментами, что не "
         "выполнит и почему, каких инструментов не хватает, какие лишние, риски и что улучшить.")


def parse_reply(text: str) -> dict:
    """Ответ модели → {text без блоков, prompt|None, meta|None}."""
    prompt = None
    m = PROMPT_BLOCK.search(text or "")
    if m:
        prompt = m.group(1).strip("\n")
    meta = None
    mm = META_BLOCK.search(text or "")
    if mm:
        try:
            raw = json.loads(mm.group(1))
            meta = {k: [str(x) for x in raw.get(k) or [] if str(x).strip()]
                    for k in ("mcp_add", "mcp_remove")} if isinstance(raw, dict) else None
        except ValueError:
            meta = None
    clean = META_BLOCK.sub("", PROMPT_BLOCK.sub("[новый промпт — в редакторе]", text or "")).strip()
    return {"text": clean, "prompt": prompt, "meta": meta}


def turn(project, rel: str, meta: dict, body: str, message: str = "", pick: str = "",
         refresh: bool = False, lang: str = "ru", call=None, probe=None) -> dict:
    """Одна реплика разговора о промпте. Пустое сообщение в пустом разговоре — первый разбор."""
    import agent_core as AG
    data = load(project, rel)
    history = data.get("messages") or []
    message = (message or "").strip() or (FIRST if not history else "")
    if not message:
        raise CoachError("пустое сообщение")
    inv = data.get("inventory") if not refresh else None
    if not inv or time.time() - float(inv.get("at") or 0) > INVENTORY_TTL or \
            set((inv.get("selected") or {})) != set(meta.get("mcp") or []):
        inv = inventory(project, meta, probe=probe,
                        cached=None if refresh else data.get("inventory"))
    fl = flags(body, inv)
    st = settings(project, meta)
    system = SYSTEM.format(facts=facts_text(inv, fl, st), name=meta.get("name") or Path(rel).stem,
                           body=(body or "").strip() or "(пусто)",
                           lang="по-русски" if lang != "en" else "in English")
    cfg = AG.config()
    role = COACH_ROLE
    if pick.startswith("role:"):
        role = pick[5:] or COACH_ROLE
    elif pick.startswith("model:"):
        try:
            cfg = AG.pin_model(cfg, pick[6:], (role,))
        except ValueError as e:
            raise CoachError(str(e)) from None
    msgs = [{"role": "system", "content": system}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in history[-HISTORY_KEEP:]
             if m.get("role") in ("user", "assistant")]
    msgs.append({"role": "user", "content": message})
    started = time.time()
    r = (call or AG.call_role)(cfg, role, msgs, deadline=started + cfg.get("request_timeout", 300) * 2)
    if not r.get("ok"):
        raise CoachError("модель не ответила: " + "; ".join(str(x) for x in (r.get("log") or [])[-2:])[:300])
    got = parse_reply(r.get("text") or "")
    from aurora_common import utc_stamp
    history += [{"role": "user", "content": message, "at": utc_stamp()},
                {"role": "assistant", "content": got["text"], "at": utc_stamp(),
                 "model": r.get("model", ""), "seconds": int(time.time() - started),
                 "prompt_changed": bool(got["prompt"]), "prompt": got["prompt"] or "",
                 "meta": got["meta"]}]
    data.update(messages=history[-HISTORY_KEEP:], inventory=inv)
    save(project, rel, data)
    return {"ok": True, "reply": got["text"], "prompt": got["prompt"], "meta": got["meta"],
            "model": r.get("model", ""), "messages": data["messages"], "flags": fl}


def reset(project, rel: str) -> dict:
    """Начать разбор заново: разговор забыт, список инструментов серверов — тоже."""
    try:
        state_file(project, rel).unlink()
    except OSError:
        pass
    return {"ok": True}
