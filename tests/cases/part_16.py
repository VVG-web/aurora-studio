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
    ("/api/kit/status", ".notes[]"): "журнал изменений пишется по-русски: это документ, граница описана в CHANGELOG 1.149.0",
    ("/api/about", ".releases[]"): "то же: записи CHANGELOG",
    ("/api/config", ".text"): "сам файл конфигурации проекта",
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
    "/api/runlog", "/api/agent/pydantic", "/api/context/suggest", "/api/skin",
)
NOT_CRAWLED = {
    "/api/ping": "ответ — слово «ok», не текст",
    "/api/mcp/probe": "запускает чужой сервер MCP",
    "/api/agent/ping": "ходит в сеть к шлюзу модели",
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
