"""Проверки движка Aurora, часть 16: английский режим панели — обход живого сервера.

Каркас и помощники — tests/harness.py. Здесь сервер панели поднимается по-настоящему, его
страницы данных обходятся с `?lang=en`, и всё, что осталось русским, должно быть названо в
перечне исключений с причиной: остальное — пропущенный перевод.
"""
from __future__ import annotations

from pathlib import Path
import http.client
import importlib
import json
import re
import sys
import threading

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401

CYR = re.compile("[а-яА-ЯёЁ]")

# Исключения: (страница, путь в ответе) → причина. Русское слово здесь — не текст, который
# забыли перевести, а код, имя или данные, и причина записана рядом: исключение без причины
# перестаёт быть исключением.
EXCUSES = {
    ("/api/state", ".commands[].kind"): "код «скрипт» / «модель»: страница показывает его своим словом (kindChip)",
    ("/api/state", ".commands[].alias"): "русские псевдонимы команд — имена, которые вводят руками",
    ("/api/scenarios", ".scenarios[].group"): "ключ группы маршрутов («база», «продуктивность»), а не подпись",
    ("/api/scenarios", ".scenarios[].steps[].cycle"): "метки разметки маршрута («цикл:», «конец цикла»)",
    ("/api/cron", ".routes[].group"): "тот же ключ группы маршрутов, по нему раздел заготавливает цепочку",
    ("/api/kit/status", ".notes[]"): "журнал изменений пишется по-русски: это документ, граница описана в CHANGELOG 1.149.0",
    ("/api/about", ".releases[]"): "то же: записи CHANGELOG",
    ("/api/config", ".text"): "сам файл конфигурации проекта",
    ("/api/cron", ".tasks[].bot_last.summary"): "начало ответа бота — текст модели на языке проекта",
    ("/api/i18n", ".strings.arg.ops_impact"): "пример имени карточки на языке проекта",
    ("/api/i18n", ".strings.proj.f_trust_statuses_ph"): "значения по умолчанию — названия статусов Jira как есть",
    ("/api/i18n", ".strings.proj.f_assumption_statuses_ph"): "то же",
    ("/api/i18n", ".strings.proj.f_trusted_branches_ph"): "значения по умолчанию — названия веток Confluence как есть",
    ("/api/i18n", ".strings.lang.russian"): "язык называют на нём самом",
    ("/api/i18n", ".languages[].name"): "то же",
}


def _scrub(text: str) -> str:
    """Без того, что остаётся как есть и в английской фразе: «кавычки-ёлочки» и пути к файлам."""
    text = re.sub(r"«[^»]*»", "", text)
    return " ".join(tok for tok in text.split() if "/" not in tok and not tok.endswith(".md"))


def _walk(obj, path=""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v, f"{path}[]")


# Страницы данных панели, которые отвечают без сети и без записи. Остальные GET-пути названы
# ниже с причиной: новый путь обязан попасть в один из двух списков — проверка это требует.
CRAWLED = (
    "/api/state", "/api/health", "/api/config", "/api/extras", "/api/mcp/kit", "/api/mcp", "/api/roots",
    "/api/kinds", "/api/artifacts", "/api/about", "/api/agent", "/api/kit/status", "/api/git",
    "/api/git/head", "/api/activity", "/api/jobs", "/api/scenarios", "/api/skins", "/api/modules",
    "/api/files/tree", "/api/graph", "/api/ask/threads", "/api/i18n", "/api/route/state",
    "/api/runlog", "/api/agent/pydantic", "/api/context/suggest", "/api/skin", "/api/cron",
    "/api/history", "/api/routes", "/api/bots", "/api/env", "/api/adapter/alarm",
)
NOT_CRAWLED = {
    "/api/ping": "ответ — слово «ok», не текст",
    "/api/mcp/probe": "запускает чужой сервер MCP",
    "/api/agent/ping": "ходит в сеть к шлюзу модели",
    "/api/gitmods": "ходит в сеть на GitHub за версиями модулей Git-провайдеров",
    "/api/gitsync": "нужен проект; ответ — состояние git и настройка, сообщения — коды раздела",
    "/api/agent/models": "ходит в сеть к шлюзу модели",
    "/api/run/logs": "вывод запуска — вывод движка, он остаётся русским",
    "/api/run/file": "содержимое файла проекта",
    "/api/run/steps": "вывод запуска — вывод движка, он остаётся русским",
    "/api/report": "отчёт — вывод движка, он остаётся русским",
    "/api/report/file": "содержимое файла проекта",
    "/api/card": "содержимое карточки проекта",
    "/api/ask/thread": "разговор — данные проекта",
    "/api/files/read": "содержимое файла проекта",
    "/api/files/clean": "перечень временных файлов проекта",
    "/api/doc": "документация проекта и кита — тексты документов",
    "/api/job": "вывод запуска — вывод движка, он остаётся русским",
    "/api/cron/run": "журнал прогона цепочки — вывод шагов движка, он остаётся русским",
    "/api/history/run": "журнал запуска — вывод движка, он остаётся русским",
    "/api/route/live": "журнал идущего маршрута — вывод движка, он остаётся русским",
    "/api/models": "настройка моделей — данные человека: имена провайдеров и ролей",
    "/api/bots/file": "файл бота проекта — промпт и шапка, написанные человеком",
    "/api/bots/coach": "разговор с моделью о промпте бота — слова человека и ответы модели",
    "/api/llm/calls": "тексты промптов движка — они русские, как и сами промпты; надписи каталога — на языке запроса",
    "/api/harness": "спрашивает версии у команд ассистентов машины; пояснения — в каталоге строк раздела",
}


class _Panel:
    """Настоящий сервер панели на свободном порту, один проект и язык en."""

    def __init__(self, roots):
        sys.path.insert(0, str(KIT / "cockpit"))
        sys.path.insert(0, str(SCRIPTS))
        from http.server import ThreadingHTTPServer
        self.ck = importlib.import_module("aurora_cockpit")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.ck.Handler)
        self.srv.roots = [str(r) for r in roots]
        # Расписание без `main()` ищет проекты в папке рядом с китом, то есть на машине
        # разработчика — в его настоящих проектах: обход видел ответ живого бота. Здесь
        # планировщик смотрит только в корни проверки.
        self.ck.SCHED = self.ck.CRON.Scheduler(self.ck, lambda: self.ck.find_projects(self.srv.roots))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def get(self, path: str):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=120)
        c.request("GET", path + ("&" if "?" in path else "?") + "lang=en&t=" + self.ck.TOKEN)
        r = c.getresponse()
        body = r.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            return body

    def close(self):
        self.srv.shutdown()
        self.ck.SCHED = None


def _leftovers(panel: _Panel, project: Path, tag: str) -> list:
    found = []
    for ep in CRAWLED:
        data = panel.get(f"{ep}?project={project}&q=test")
        if isinstance(data, str):
            data = {"": data}
        for path, text in _walk(data):
            if not CYR.search(text) or path.endswith(".ru"):
                continue
            if (ep, path) in EXCUSES:
                continue
            if CYR.search(_scrub(text)):
                found.append(f"{tag} {ep} {path or '(строка)'}: {text[:90]!r}")
    return found


@test
def test_the_english_panel_server_sends_no_russian_outside_the_documented_exceptions(tmp: Path):
    """Обход живого сервера в английском режиме: русского не остаётся, кроме названных исключений.

    Глазами русский текст ищет человек, а регрессию — тот, кто перебирает страницы данных
    сервера. Обход идёт по двум проектам: чистому и сломанному (нет `AGENTS.md`, вид
    артефакта без шаблона и с недопустимой папкой, лишняя папка верхнего уровня) — найденное
    доктором и реестром видов появляется только в таком проекте. Названия, пути, значения
    по умолчанию, журнал изменений и коды перечислены в `EXCUSES` с причиной;
    всё остальное — текст, которому не хватает перевода в `cockpit/i18n/data/en.json`.
    """
    set_home(tmp / "home")
    (tmp / "clean_parent").mkdir()
    (tmp / "messy_parent").mkdir()
    clean = make_project(tmp / "clean_parent")
    messy = make_project(tmp / "messy_parent")
    (messy / "AGENTS.md").unlink(missing_ok=True)
    cfg = messy / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8")
                   + "\nartifacts:\n  us:\n    template: Templates/missing.md\n    out: ../outside\n"
                     "  custom:\n    title: My kind\n", encoding="utf-8")
    (messy / "stray-folder").mkdir()

    panel = _Panel([tmp])
    try:
        left = _leftovers(panel, clean, "чистый") + _leftovers(panel, messy, "сломанный")
    finally:
        panel.close()
    assert not left, "в английском режиме остался русский текст (нет перевода?):\n" + "\n".join(left)


@test
def test_every_get_route_of_the_panel_is_either_crawled_or_excused(tmp: Path):
    """Новый GET-путь панели обязан попасть в обход английского режима или в список исключений.

    Обход ловит только те страницы данных, о которых знает. Путь, добавленный завтра, иначе
    молча уйдёт в английский режим русским — пока кто-нибудь не заметит глазами.
    """
    src = (KIT / "cockpit" / "aurora_cockpit.py").read_text(encoding="utf-8")
    get = src[src.index("    def do_GET(self):"):src.index("    def do_POST(self):")]
    routes = set(re.findall(r'u\.path == "(/api/[\w/]+)"', get))
    assert len(routes) > 30, f"GET-пути не найдены: {sorted(routes)}"
    unknown = sorted(routes - set(CRAWLED) - set(NOT_CRAWLED))
    assert not unknown, ("GET-путь не назван ни в обходе, ни среди исключений с причиной "
                         f"(tests/cases/part_16.py): {unknown}")
    gone = sorted((set(CRAWLED) | set(NOT_CRAWLED)) - routes)
    assert not gone, f"в списках путь, которого в сервере больше нет: {gone}"


@test
def test_engine_words_shown_in_the_panel_have_an_english_label(tmp: Path):
    """Слова линтера и статистики на экране — через таблицу `ENGINE_WORDS`, а не как есть.

    Названия видов ошибок («битые ссылки»), причины недоверия («задачи ещё в работе») и
    заглушки («(нет status)») печатает движок по-русски. Панель ищет по ним числа, поэтому
    ключами остаются они, а подпись человеку берётся из каталога строк. Новый вид ошибки в
    `kb_lint.py` без строки в таблице показывался бы в английском режиме русским.
    """
    import ast
    lint = ast.parse((SCRIPTS / "kb_lint.py").read_text(encoding="utf-8"))
    titles = set()
    for node in ast.walk(lint):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "kinds" for t in node.targets) \
                and isinstance(node.value, ast.List):
            for tup in node.value.elts:
                if isinstance(tup, ast.Tuple) and len(tup.elts) == 3 \
                        and isinstance(tup.elts[1], ast.Constant):
                    titles.add(tup.elts[1].value)
    assert len(titles) >= 10, f"виды ошибок линтера не найдены: {titles}"
    stats = (SCRIPTS / "aurora_stats.py").read_text(encoding="utf-8")
    words = set(titles) | {"прочее"} | set(re.findall(r'trust_why\["([^"]+)"\]', stats)) \
        | {"(нет status)", "(нет kind)"}
    panel = (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    table = dict(re.findall(r'^\s*"([^"]+)": "(word\.[\w.]+)",', panel, re.M))
    lacking = sorted(w for w in words if w not in table)
    assert not lacking, f"слова движка без подписи в ENGINE_WORDS (panel.js): {lacking}"
    ru = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit/i18n/en.json").read_text(encoding="utf-8"))
    for ru_word, key in table.items():
        assert ru.get(key) == ru_word, f"{key}: русская подпись должна совпадать со словом движка"
        assert en.get(key) and not CYR.search(en[key].replace("(", "")), f"{key}: нет английской подписи"


@test
def test_reference_opens_english_documents_in_the_english_panel(tmp: Path):
    """«Справка» на английском читает издания из `docs/en/`, а не русский текст под английским заголовком.

    Английские издания есть у документов `docs/readme/`, справочника команд, плана и
    требований к панели. У остальных (скиллы для агента, журнал изменений) перевода нет,
    и страница говорит об этом строкой, а не молчит.
    """
    view = (KIT / "cockpit/modules/reference/view.js").read_text(encoding="utf-8")
    docs = re.findall(r'\["((?:docs|skills)/[^"]+|CHANGELOG\.md)", "reference\.doc\.\w+"\]', view)
    assert len(docs) >= 15, f"оглавление справки не найдено: {docs}"
    mapped = dict(re.findall(r'"(docs/[\w./-]+\.md)": "(docs/en/[\w./-]+\.md)"', view))
    for path in docs:
        if path.startswith("docs/readme/"):
            mapped[path] = "docs/en/" + path[len("docs/"):]
    assert len(mapped) >= 9, mapped
    for ru_path, en_path in mapped.items():
        assert (KIT / ru_path).is_file(), f"{ru_path}: нет русского оригинала"
        assert (KIT / en_path).is_file(), f"{ru_path} → {en_path}: нет английского издания"
    for key in ("reference.ru_only",):
        for lang in ("ru", "en"):
            cat = json.loads((KIT / f"cockpit/modules/reference/i18n/{lang}.json").read_text(encoding="utf-8"))
            assert cat.get(key), f"{lang}: нет строки {key}"


@test
def test_route_summary_reads_in_english_for_the_english_panel(tmp: Path):
    """Итог маршрута в консоли английской панели — по-английски; русский вид не меняется.

    Итог составляет `run_summary`, и панель просит его с `lang=en`. Вывод агента в терминале
    (`render` без языка) остаётся русским, как весь вывод движка.
    """
    sys.path.insert(0, str(SCRIPTS))
    RS = importlib.import_module("run_summary")
    s = RS.empty()
    s.update(seconds=75, model_calls=3, tokens_in=1000, tokens_out=500, gen_seconds=10,
             docs_done=2, cards_known=True, cards_created=4)
    ru = "\n".join(RS.render(s))
    en = "\n".join(RS.render(s, "Run summary", "en"))
    assert "Модель: токенов" in ru and "1 мин 15 с" in ru, ru
    assert not CYR.search(en), en
    assert "1 min 15 s" in en and "created 4" in en, en
    steps = [{"cmd": "sync:confluence", "rc": 2}]
    out = RS.route(str(tmp), "", 5, steps, lang="en")
    assert not CYR.search("\n".join(out["lines"])), out["lines"]
    assert "step sync:confluence failed (code 2)" in "\n".join(out["lines"])
