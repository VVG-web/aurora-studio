"""Проверки движка Aurora, часть 159: предел тяжёлых запросов провайдера в «Моделях».

Каркас и помощники — tests/harness.py. У провайдера два поля: «Тяжёлых одновременно» (`heavy`, 0 — предел
выучит движок на 529) и «Тяжёлый — от, токенов» (`heavy_tokens`, 0 — умолчание движка). Они живут в
настройке кита (`local/models.json`), пишутся только формой «Моделей» через `/api/models/save` со слепком
(устаревший — 409) и доходят до каждой цепочки ролей. Кит и HOME — временные.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

from harness import KIT, set_home, test

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
        for key in ("models.p_heavy", "models.p_heavy_hint", "models.p_heavy_tokens", "models.p_heavy_tokens_hint"):
            assert cat.get(key), (lang, key)
        assert "529" in cat["models.p_heavy_hint"] and measure in cat["models.p_heavy_hint"], (lang, cat["models.p_heavy_hint"])
