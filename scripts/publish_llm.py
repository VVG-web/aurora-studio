#!/usr/bin/env python3
"""publish_llm.py — модель для публикации: один раз собрать скрипт страницы или (по настройке) собрать страницу заново.

Почему так. Промпт публикации (`publish_prompt` вида) описывает оформление страницы, но пока его читал только человек:
`ship:publish` конвертировал markdown встроенным конвертером. Повторять промпт на каждой публикации моделью — токены на
каждый документ; не повторять — страница расходится с промптом. Компромисс: модель один раз читает промпт и заготовку и
пишет скрипт страницы; дальше публикация запускает скрипт и не тратит токены. Кому нужна свежая сборка каждый раз,
тот ставит `publish_engine: prompt` и платит токенами.

Модуль общий для генератора (`publish_script.py`) и самой публикации (`publish_doc.py`): один контракт, одна проверка.
Контракт скрипта: stdin — JSON с документом, stdout — JSON `{"title": "...", "storage": "..."}`.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from html.entities import name2codepoint

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import MADE_MARK, python_executable  # noqa: E402 — граница чистовика, одна на движок; один интерпретатор на подсистему

SCRIPT_VERSION = 1
ROLE = "worker"
MAX_TOKENS = 12000
DEADLINE_SECONDS = 900
FORBIDDEN = ("<script", "<iframe", "onclick=", "javascript:", "AURORA-PUBLISH", "made by ai", "сделано ии")
# Обёртка только для разбора XML: на страницу уходят сами теги, а URI пространства имён нужен лишь парсеру.
# Домены в ней нейтральные — иначе проверка «в поставке нет чужих адресов» считает их адресом заказчика.
AC_NS = ('xmlns:ac="http://example.com/ac" '
         'xmlns:ri="http://example.com/ri"')


def _cfg() -> dict:
    """Настройка моделей движка. Импорт внутри: публикация без модели не должна тянуть агент ядра."""
    import agent_core as AG
    return AG.config()


def _call(messages: list, role: str = ROLE, thinking: bool = True,
          max_tokens: int = MAX_TOKENS, deadline: float = DEADLINE_SECONDS) -> dict:
    """Один вызов модели. Возвращает `{"ok", "text", "model", "error"}`; сеть и кольцо — забота `call_role`."""
    import agent_core as AG
    try:
        r = AG.call_role(_cfg(), role, messages, thinking=thinking, max_tokens=max_tokens,
                         deadline=time.time() + deadline)
    except Exception as e:  # noqa: BLE001 — публикация получает причину, а не трассу
        return {"ok": False, "text": "", "model": "", "error": f"{type(e).__name__}: {e}"}
    if not r.get("ok"):
        why = str(r.get("error") or r.get("why") or "модель не ответила").strip()
        return {"ok": False, "text": "", "model": str(r.get("model") or ""), "error": why}
    text = str(r.get("text") or "").strip()
    if not text:
        return {"ok": False, "text": "", "model": str(r.get("model") or ""), "error": "пустой ответ модели"}
    return {"ok": True, "text": text, "model": str(r.get("model") or ""), "error": ""}


def strip_code(text: str) -> str:
    """Достать код из ответа модели: ```-блок, иначе от первой строки кода. Модели любят объяснения вокруг."""
    raw = str(text or "").strip()
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", raw, re.S | re.I)
    if m:
        raw = m.group(1).strip()
    else:
        lines = raw.splitlines()
        for i, line in enumerate(lines):
            if re.match(r"^\s*(#!|import\s|from\s+\S+\s+import\s|def\s+\w+\s*\(|class\s+\w+)", line):
                raw = "\n".join(lines[i:]).strip()
                break
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not raw.startswith("#!"):
        raw = "#!/usr/bin/env python3\n" + raw
    return raw + "\n"


def storage_problem(storage: str, kind: str = "") -> str:
    """Что не так со storage-страницей. Пусто — годится. Проверки дешёвые и без сети: публикация не должна узнать
    о поломке уже после отправки."""
    text = str(storage or "").strip()
    if not text:
        return "пустой storage"
    low = text.lower()
    for bad in FORBIDDEN:
        if bad.lower() in low:
            return f"в странице запрещённый фрагмент: {bad}"
    if not text.startswith("<"):
        return "storage не похож на XHTML: не начинается с тега"
    # Confluence storage приходит из markdown: `&nbsp;` и другие HTML-сущности там обычны, а XML их не знает.
    # Числовые ссылки разбираются, декларация XML внутри фрагмента — нет; проверяем то, что уйдёт на страницу.
    xml_text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
    xml_text = re.sub(r"&([A-Za-z][A-Za-z0-9]+);",
                      lambda m: m.group(0) if m.group(1) in ("amp", "lt", "gt", "quot", "apos")
                      else (f"&#{name2codepoint[m.group(1)]};" if m.group(1) in name2codepoint else m.group(0)),
                      xml_text)
    try:
        ET.fromstring(f"<root {AC_NS}>{xml_text}</root>")
    except ET.ParseError as e:
        return f"storage не разбирается как XML: {e}"
    if kind:
        from make_kinds import required_sections
        miss = [m for m in required_sections(kind) if m not in text]
        if miss:
            return f"в странице вида {kind} нет обязательных разделов: " + ", ".join(miss)
    if MADE_MARK and MADE_MARK in text:
        return "в страницу попал маркер производства"
    return ""


def document_data(path: str, text: str, title: str, kind: str, project: str) -> dict:
    """Вход скрипта: ровно то, что публикация знает о документе. Скрипт не читает файлы сам — ему приносят всё."""
    from aurora_common import body as md_body, frontmatter, utc_today
    fm = frontmatter(text) or {}
    return {
        "path": str(path).replace("\\", "/"),
        "text": text,
        "body": md_body(text),
        "frontmatter": fm,
        "title": title,
        "kind": kind,
        "project": project,
        "date": str(fm.get("updated") or fm.get("created") or utc_today()),
        "author": str(fm.get("author") or fm.get("published_by") or ""),
    }


def run_script(project: str, script: str, data: dict, timeout: int = 120) -> dict:
    """Запустить готовый скрипт страницы. Он получает JSON на stdin и обязан вернуть JSON на stdout."""
    import subprocess
    root = os.path.abspath(project)
    path = script if os.path.isabs(script) else os.path.join(root, script)
    if not os.path.isfile(path):
        raise RuntimeError(f"скрипта нет: {script}")
    env = dict(os.environ)
    scripts_dir = os.path.join(root, ".aurora", "scripts")
    env["PYTHONPATH"] = scripts_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"          # Windows-консоль иначе читает UTF-8 JSON как cp1251
    try:
        got = subprocess.run([python_executable(), path], input=json.dumps(data, ensure_ascii=False),
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             cwd=root, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"скрипт страницы не ответил за {timeout} с")
    if got.returncode != 0:
        why = (got.stderr or got.stdout or "").strip().splitlines()
        raise RuntimeError("скрипт страницы упал: " + (why[-1] if why else f"код {got.returncode}"))
    out = _last_json(got.stdout)
    if not isinstance(out, dict):
        raise RuntimeError("скрипт страницы вернул не JSON-объект")
    title = str(out.get("title") or "").strip()
    storage = str(out.get("storage") or "").strip()
    if not title:
        raise RuntimeError("скрипт страницы не вернул title")
    problem = storage_problem(storage, str(data.get("kind") or ""))
    if problem:
        raise RuntimeError(problem)
    return {"title": title, "storage": storage}


def _last_json(text: str):
    """Последний JSON-объект в выводе: скрипт может напечатать отладку перед ответом, но ответ обязан быть последним."""
    raw = str(text or "").strip()
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _prompt_block(project: str, rel: str) -> tuple[str, str, str]:
    """Прочитать файл промпта/заготовки относительно проекта. → (путь, текст, отпечаток)."""
    import hashlib
    root = os.path.abspath(project)
    path = os.path.join(root, rel) if not os.path.isabs(rel) else rel
    text = open(path, "r", encoding="utf-8", errors="replace").read() if os.path.isfile(path) else ""
    return str(rel).replace("\\", "/"), text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _kind_block(rec: dict) -> str:
    """Поля вида, которые влияют на страницу. Без адресов и служебных настроек — они не про вёрстку."""
    keys = ("title", "publish_title", "publish_path", "publish_prompt", "publish_template", "publish_engine",
            "publish_script", "out", "tech_agnostic", "ref_mask")
    return json.dumps({k: rec.get(k) for k in keys if str(rec.get(k) or "").strip()}, ensure_ascii=False, indent=2)


def build_script_messages(kind: str, rec: dict, project: str, sample_path: str, sample_text: str) -> list:
    """Сообщения для модели: один раз собрать скрипт страницы по промпту и заготовке."""
    prompt_rel = str(rec.get("publish_prompt") or "").strip()
    template_rel = str(rec.get("publish_template") or "").strip()
    _p, prompt_text, _ps = _prompt_block(project, prompt_rel)
    _t, template_text, _ts = _prompt_block(project, template_rel) if template_rel else ("", "", "")
    system = (
        "Ты — инженер Аврора Studio. Собери ОДИН Python-скрипт, который детерминированно превращает артефакт "
        "проекта в страницу Confluence по промпту публикации и заготовке.\n\n"
        "Контракт скрипта:\n"
        "- stdin: JSON {path, text, body, frontmatter, title, kind, project, date, author};\n"
        "- stdout: ровно один JSON {\"title\": \"...\", \"storage\": \"...\"} и больше ничего;\n"
        "- storage — валидный XHTML Confluence storage (можно ac:/ri:); title — заголовок страницы.\n\n"
        "Правила:\n"
        "- только стандартная библиотека Python 3; без сети, записи файлов и subprocess;\n"
        "- одинаковый вход — одинаковый выход; без времени, случайности и UUID, если они не выводятся из входа;\n"
        "- заголовок: `frontmatter.publish_title`, если поле есть и не пустое; иначе `title`;\n"
        "- текст автора не переписывай: оформляй `body` (он уже без производственных разделов);\n"
        "- не добавляй разделы «Уточнения», «Допущения», «Под вопросом», маркер производства и подписи об ИИ;\n"
        "- не добавляй HTML-скрипты, iframe, onclick и javascript:;\n"
        "- для вида `ac` обязательный скелет и порядок — из промпта публикации (Задача на разработку истории, "
        "История изменений, Описание, Критерии приемки);\n"
        "- заголовки разделов: h1 для скелета, h3 для подразделов критериев;\n"
        "- если чего-то не хватает, не выдумывай: оставь пустым или перенеси как есть.\n\n"
        "Верни только код скрипта, без ``` и без пояснений."
    )
    user = (
        f"Вид артефакта: {kind}\n\n"
        f"Настройка вида:\n{_kind_block(rec)}\n\n"
        f"Промпт публикации ({prompt_rel or 'не задан'}):\n{prompt_text or '(пусто)'}\n\n"
        f"Заготовка страницы ({template_rel or 'не задана'}):\n{template_text or '(пусто)'}\n\n"
        f"Последний документ вида: {sample_path}\n"
        f"--- документ ---\n{sample_text}\n--- конец документа ---\n\n"
        "Скрипт будет запускаться на других документах этого вида: не привязывайся к тексту образца, "
        "кроме структуры и полей, которые требует промпт."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_render_messages(kind: str, rec: dict, project: str, data: dict) -> list:
    """Сообщения для режима `prompt`: собрать страницу сейчас, без готового скрипта (тратит токены)."""
    prompt_rel = str(rec.get("publish_prompt") or "").strip()
    _p, prompt_text, _ps = _prompt_block(project, prompt_rel)
    system = (
        "Ты — инженер Аврора Studio. Собери страницу Confluence по промпту публикации и документу.\n"
        "Верни ровно один JSON {\"title\": \"...\", \"storage\": \"...\"}, без ``` и пояснений.\n"
        "storage — валидный XHTML Confluence storage; title — заголовок страницы.\n"
        "Заголовок: `frontmatter.publish_title`, если поле есть; иначе `title`. Текст автора не переписывай.\n"
        "Не добавляй производственные разделы, маркер производства и подписи об ИИ.\n"
        "Для вида `ac` соблюдай обязательный скелет из промпта."
    )
    user = (
        f"Вид артефакта: {kind}\n\nНастройка вида:\n{_kind_block(rec)}\n\n"
        f"Промпт публикации ({prompt_rel or 'не задан'}):\n{prompt_text or '(пусто)'}\n\n"
        f"Документ:\n{json.dumps(data, ensure_ascii=False)}\n"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def validate_script(project: str, kind: str, code: str, sample: dict, runs: int = 2) -> dict:
    """Проверить скрипт на образце: запустить, разобрать JSON, проверить storage и детерминизм. → отчёт."""
    import tempfile
    root = os.path.abspath(project)
    checks = []
    try:
        compile(code, "publish_script.py", "exec")
    except SyntaxError as e:
        return {"ok": False, "why": f"скрипт не компилируется: {e}", "checks": checks}
    checks.append("синтаксис Python")
    fd, tmp = tempfile.mkstemp(prefix="aurora-publish-", suffix=".py", dir=root)
    os.close(fd)
    try:
        open(tmp, "w", encoding="utf-8").write(code)
        first = None
        for _ in range(max(1, runs)):
            got = run_script(project, tmp, sample)
            if first is None:
                first = got
            elif got["storage"] != first["storage"] or got["title"] != first["title"]:
                return {"ok": False, "why": "скрипт недетерминирован: два запуска дали разную страницу",
                        "checks": checks + ["запуск 1", "запуск 2"]}
        checks.append(f"запуск {runs}×")
        if str(sample.get("frontmatter", {}).get("publish_title") or "").strip():
            want = str(sample["frontmatter"]["publish_title"]).strip()
            if first["title"] != want:
                return {"ok": False, "why": f"скрипт не взял publish_title из шапки: «{first['title']}» вместо «{want}»",
                        "checks": checks}
            checks.append("заголовок из шапки")
        return {"ok": True, "why": "", "checks": checks, "title": first["title"],
                "storage_len": len(first["storage"]), "storage": first["storage"]}
    except Exception as e:  # noqa: BLE001 — проверка возвращает причину, а не падает
        return {"ok": False, "why": f"{type(e).__name__}: {e}", "checks": checks}
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
