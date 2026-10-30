"""Проверки движка Aurora, часть 159: предел тяжёлых запросов провайдера в «Моделях».

Каркас и помощники — tests/harness.py. У провайдера два поля: «Тяжёлых одновременно» (`heavy`, 0 — предел
выучит движок на 529) и «Тяжёлый — от, токенов» (`heavy_tokens`, 0 — умолчание движка). Они живут в
настройке кита (`local/models.json`), пишутся только формой «Моделей» через `/api/models/save` со слепком
(устаревший — 409) и доходят до каждой цепочки ролей. Кит и HOME — временные.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, set_home, test

URL = "https://llm.example.com/v1"


def _models(heavy=2, heavy_tokens=32000) -> dict:
    return {"providers": [{"id": "gw", "name": "GW", "type": "openai", "url": URL, "key": "k", "width": 6,
                           "heavy": heavy, "heavy_tokens": heavy_tokens},
                          {"id": "home", "name": "Home", "type": "llama.cpp", "url": "http://localhost:8080/v1",
                           "heavy": -3}],
            "capabilities": {"llm": {"roles": [
                {"id": "worker", "backends": [{"provider": "gw", "model": "m"}, {"provider": "home", "model": "h"}]},
                {"id": "qa", "backends": [{"provider": "gw", "model": "m"}]}]}}}


@test
def test_the_heavy_limit_lives_in_the_provider_and_reaches_every_chain(tmp: Path):
    """Поля провайдера — целые не меньше нуля (мусор и минус — «не задано»); в конфигурацию движка они
    приходят у каждого бэкенда каждой роли, и правило тяжёлых у бэкенда берётся из них."""
    MC = importlib.import_module("model_config")
    AG = importlib.import_module("agent_core")
    data, _notes = MC.normalize(_models(heavy="2"))
    gw, home = data["providers"]
    assert (gw["heavy"], gw["heavy_tokens"]) == (2, 32000), gw
    assert (home["heavy"], home["heavy_tokens"]) == (0, 0), home
    cfg = MC.to_config(data)
    for role in ("worker", "qa"):
        first = cfg["chains"]["llm"][role][0]
        assert (first["heavy"], first["heavy_tokens"]) == (2, 32000), (role, first)
    assert cfg["backends"][0]["heavy"] == 2 and cfg["backends"][1]["heavy"] == 0, cfg["backends"]
    worker = cfg["chains"]["llm"]["worker"]
    for b in worker:
        AG.HEAVY_LEARNED.pop(b["n"], None)
    assert AG.heavy_rules(worker[0]) == [(2, 32000)] and AG.heavy_rules(worker[1]) == [], worker


@test
def test_heavy_fields_are_written_through_the_models_api_with_a_stamp(tmp: Path):
    """`/api/models` отдаёт поля провайдера, `/api/models/save` со слепком страницы записывает их в файл кита;
    страница с прежним слепком получает 409 и файл не меняет."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    restore = set_home(tmp / "home")
    ck = importlib.import_module("aurora_cockpit")
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    old_kit = ck.KIT
    ck.KIT = str(kit)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(tmp / "roots")]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=60)
        h = {"X-Aurora-Token": ck.TOKEN}
        data = None
        if body is not None:
            data, h["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        return r.status, json.loads(r.read().decode("utf-8") or "{}")
    try:
        st, d = call("GET", "/api/models")
        assert st == 200, d
        st, r = call("POST", "/api/models/save", {"models": _models(), "expect": d["stamp"]})
        assert st == 200 and r["ok"], f"{st} {r}"
        path = kit / "local" / "models.json"
        saved = json.loads(path.read_text(encoding="utf-8"))["providers"][0]
        assert (saved["heavy"], saved["heavy_tokens"]) == (2, 32000), saved
        st, d2 = call("GET", "/api/models")
        gw = next(p for p in d2["models"]["providers"] if p["id"] == "gw")
        assert (gw["heavy"], gw["heavy_tokens"]) == (2, 32000), gw
        keep = path.read_bytes()
        st, r = call("POST", "/api/models/save", {"models": _models(heavy=5), "expect": d["stamp"]})
        assert st == 409 and r["conflict"] is True and path.read_bytes() == keep, f"устаревший слепок: {st} {r}"
        st, r = call("POST", "/api/models/save", {"models": _models(heavy=0, heavy_tokens=0), "expect": d2["stamp"]})
        assert st == 200 and r["ok"], f"{st} {r}"
        saved = json.loads(path.read_text(encoding="utf-8"))["providers"][0]
        assert (saved["heavy"], saved["heavy_tokens"]) == (0, 0), "сброс предела формой не записан"
    finally:
        srv.shutdown()
        srv.server_close()
        ck.KIT = old_kit
        restore()


@test
def test_the_models_form_shows_both_heavy_fields_in_both_languages(tmp: Path):
    """Форма провайдера несёт оба поля (от нуля, без потолка); подписи и подсказки — в каталогах обоих языков,
    подсказка предупреждает, что «Замерить шлюзы» этот предел не покажет."""
    view = (KIT / "cockpit/modules/models/view.js").read_text(encoding="utf-8")
    assert 'inp("heavy", {type: "number", min: 0' in view and 'inp("heavy_tokens", {type: "number", min: 0' in view, \
        "в форме провайдера нет полей тяжёлых запросов"
    for lang, measure in (("ru", "Замерить шлюзы"), ("en", "Measure gateways")):
        cat = json.loads((KIT / f"cockpit/modules/models/i18n/{lang}.json").read_text(encoding="utf-8"))
        for key in ("models.p_heavy", "models.p_heavy_auto", "models.p_heavy_tokens", "models.help.p_heavy.what",
                    "models.help.p_heavy_tokens.what"):
            assert cat.get(key), (lang, key)
        heavy = " ".join(cat[f"models.help.p_heavy.{part}"] for part in ("what", "how", "result"))
        assert "529" in heavy and measure in heavy, (lang, heavy)


PROVIDER_HELP = ("p_id", "used_in", "p_slots", "list_n", "p_name", "p_type", "p_url", "p_key", "p_vpn", "p_health",
                 "p_health_path", "p_width", "p_heavy", "p_heavy_tokens", "p_parallel", "p_template")


@test
def test_every_provider_field_and_mark_explains_itself_in_both_languages(tmp: Path):
    """У каждого поля карточки провайдера и у меток «ID», «используется в ролях», «потоков в прогоне», «моделей»
    — справка при наведении (`data-help`): что это, пример и на что влияет, на обоих языках. Порог тяжёлого
    показан числом, которое действует (32 768 при пустом поле), — его отдаёт сервер, а не страница."""
    view = (KIT / "cockpit/modules/models/view.js").read_text(encoding="utf-8")
    cats = {lang: json.loads((KIT / f"cockpit/modules/models/i18n/{lang}.json").read_text(encoding="utf-8"))
            for lang in ("ru", "en")}
    for name in PROVIDER_HELP:
        assert f'"data-help": "models.help.{name}"' in view, f"у «{name}» нет справки при наведении"
        for lang, cat in cats.items():
            for part in ("what", "how", "result"):
                text = cat.get(f"models.help.{name}.{part}", "")
                assert text, (lang, name, part)
                # пример бывает адресом или JSON — язык видно по «что» и «на что влияет»
                if lang == "en" or part != "how":
                    assert bool(re.search("[А-Яа-яЁё]", text)) == (lang == "ru"), (lang, name, part, text)
    assert "title: t(\"models.p_" not in view, "подсказки полей снова живут в title, а не в справке"
    for lang, cat in cats.items():
        assert "32" in cat["models.help.p_heavy_tokens.how"], (lang, cat["models.help.p_heavy_tokens.how"])
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    ck = importlib.import_module("aurora_cockpit")
    AG = importlib.import_module("agent_core")
    MC = importlib.import_module("model_config")
    saved = ck.models_data
    ck.models_data = lambda: MC.empty()
    try:
        assert ck.models_state([])["heavy_tokens_default"] == AG.HEAVY_TOKENS == 32768
    finally:
        ck.models_data = saved
