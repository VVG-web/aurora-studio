"""Проверки движка Aurora, часть 29: где Аврора зовёт модели — каталог вызовов (1.164.0).

Каркас и помощники — tests/harness.py. Вопрос «какая модель отвечает за эту кнопку, по
какому промпту и с какими инструментами» задавали разработчику: ответ был размазан по
`agent_runner.py`, `bots.py`, `review_run.py` и настройке моделей. Каталог
`scripts/llm_calls.json` отвечает на него в панели («Модели → Где работают», подсказки к
кнопкам) и в справке. Каталог, который разошёлся с кодом, хуже никакого — поэтому сверка с
кодом здесь обязательна.
"""
from __future__ import annotations

import copy
import json
import re
import sys

from harness import KIT, SCRIPTS, test  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


@test
def test_every_model_call_in_the_code_is_in_the_catalogue(_t):
    """Каждое место вызова модели в движке описано в каталоге, роли по шагам совпадают с
    теми, что код зовёт, промпты есть в файлах, команды — в реестре, прямые запросы
    отмечены «http» с причиной, страница справки собрана из каталога."""
    _path()
    import llm_calls as LC
    problems = LC.check()
    assert not problems, "каталог вызовов разошёлся с кодом:\n" + "\n".join(problems)
    found = LC.scan()
    assert "agent_runner.py:run_make" in found and found["bots.py:_run"]["variable"]
    assert found["office_ingest.py:conv_pdf_ocr"]["http"], "OCR зовёт шлюз напрямую — сверка этого не видит"


@test
def test_the_catalogue_check_catches_a_new_call_and_a_wrong_role(_t):
    """Сверка — не формальность: новое место вызова без описания, роль, которую код не
    зовёт, отсутствующий промпт, неизвестная команда, прямой запрос без пометки — каждое
    даёт замечание."""
    _path()
    import llm_calls as LC
    cat, found = LC.load(), LC.scan()

    def problems(cat=cat, found=found):
        return LC.check(cat, found, docs=False)
    assert problems() == []
    extra = dict(found, **{"agent_runner.py:solve_new": {"roles": ["worker"], "variable": False,
                                                          "http": False}})
    assert any("solve_new" in p for p in problems(found=extra)), "новое место вызова прошло молча"
    bad = copy.deepcopy(cat)
    twins = next(c for c in bad["calls"] if c["id"] == "twins")
    twins["steps"][0]["role"] = "planner"
    got = problems(cat=bad)
    assert any("twins" in p and "planner" in p for p in got), got
    assert any("twins" in p and "critic" in p for p in got), "роль кода без описания прошла молча"
    bad = copy.deepcopy(cat)
    next(c for c in bad["calls"] if c["id"] == "golden")["steps"][0]["prompt"] = "agent_runner.py:PROMPT_NOPE"
    bad["calls"][0]["commands"] = ["agent:nope"]
    got = problems(cat=bad)
    assert any("PROMPT_NOPE" in p for p in got) and any("agent:nope" in p for p in got), got
    bad = copy.deepcopy(cat)
    next(c for c in bad["calls"] if c["id"] == "ocr")["engine"] = "pydantic_ai"
    assert any("conv_pdf_ocr" in p and "http" in p for p in problems(cat=bad)), "прямой запрос не замечен"


@test
def test_the_panel_shows_where_each_model_works(_t):
    """Панель: вызовы на языке интерфейса, маршруты вычислены по командам маршрутов,
    живые цепочки ролей — из настройки кита (имена провайдеров, без ключей), промпты
    движка приложены."""
    _path()
    import aurora_cockpit as ck
    saved = ck.models_data
    ck.models_data = lambda: {
        "providers": [{"id": "gw", "name": "Шлюз", "key": "SECRET-KEY"}],
        "capabilities": {
            "llm": {"roles": [
                {"id": "worker", "name": "Писатель: разбор, тезисы, ответы", "builtin": True,
                 "thinking": False, "backends": [{"provider": "gw", "model": "fast"},
                                                  {"provider": "gw", "model": "off", "enabled": False}]},
                {"id": "critic", "name": "Мой критик", "builtin": True, "backends": []}]},
            "ocr": {"roles": []}, "embeddings": {"roles": []}}}
    try:
        d = ck.llm_calls_state("en")
    finally:
        ck.models_data = saved
    assert "SECRET-KEY" not in json.dumps(d), "ключ провайдера ушёл в ответ"
    w = d["roles"]["worker"]
    assert w["chain"] == [{"provider": "Шлюз", "model": "fast"}] and w["default_name"] and w["thinking"] is False
    assert d["roles"]["critic"]["default_name"] is False and d["roles"]["critic"]["chain"] == []
    assert d["default_role"]["llm"] == "worker"
    build = next(c for c in d["calls"] if c["id"] == "build")
    assert build["title"] == "Breaking sources into cards", build["title"]
    assert "update" in [r["id"] for r in build["routes"]] and build["routes"][0]["title"].isascii()
    ask = next(c for c in d["calls"] if c["id"] == "ask")
    assert "Ты отвечаешь на вопрос" in d["prompts"][ask["steps"][0]["prompt"]]
    assert ask["steps"][0]["chosen"] is True


@test
def test_buttons_that_call_models_carry_the_models_hint(_t):
    """Подсказка «Модели» стоит там, где модель зовут: у каждого раздела из каталога — кнопка с
    `data-llm`; окно запуска, палитра, переходы `goCmd`/`goRoute` и маршруты помечены; ядро
    показывает блок по `data-llm` в той же подсказке, что и `data-help`."""
    _path()
    import llm_calls as LC
    panel = (COCKPIT / "ui/panel.js").read_text(encoding="utf-8")
    assert 'closest("[data-help],[data-llm]")' in panel
    assert '"data-llm": h.llm' in panel and 'llm: "route:" + id' in panel and "llm: cmd" in panel
    assert '"/api/llm/calls"' in panel and "llmFor(dd, r.cmd)" in panel, "окно запуска без моделей"
    views = {p.parent.name: p.read_text(encoding="utf-8") for p in (COCKPIT / "modules").glob("*/view.*")}
    views["quickstart"] += (COCKPIT / "modules/quickstart/routes.js").read_text(encoding="utf-8")
    for c in LC.load()["calls"]:
        for ui in c.get("ui") or []:
            text = views.get(ui, "") + (COCKPIT / "modules" / ui / "view.html").read_text(encoding="utf-8")
            keys = set(re.findall(r'data-llm(?:"\s*:\s*|=)"([\w:-]+)"', text))
            assert keys & {c["id"], *c.get("commands", [])}, f"в разделе {ui} нет подсказки для «{c['id']}»"
    assert '"data-llm": "route:" + sc.id' in views["quickstart"]
    assert '"data-llm": r.cmd' in views["commands"]
    ref = (COCKPIT / "modules/reference/view.js").read_text(encoding="utf-8")
    assert '"docs/llm-calls.md"' in ref and '"docs/en/llm-calls.md"' in ref
    for lang in ("ru", "en"):
        cat = json.loads((COCKPIT / f"modules/models/i18n/{lang}.json").read_text(encoding="utf-8"))
        assert "models.tab.calls" in cat and "models.calls.about" in cat, lang
