"""Проверки движка Aurora, часть 62. Каркас и помощники — tests/harness.py.

Источники и провайдеры в проверке доступности (фаза B): режим доступа источника — блок `source_access:` конфига проекта
(чтение, запись, слепок, соседние разделы конфига, умолчания: веб «без VPN, требует интернет», остальное «как раньше»);
привязка провайдера к профилю и проверка его ручки в `local/models.json` (старые файлы читаются как есть, запись не
стирает неизвестное, слепок и 409); метаданные команд `command_deps.json` (сверка с реестром, `requires_vpn_profiles`
вычисляется из источников и провайдеров, ярлыки типов 1/2/3); закладка «Команды» и группа «Настройки проекта».
Сети нет; кит, проект и домашняя папка — временные.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(KIT / "cockpit"))


def _g():
    return importlib.import_module("conn_gate")


def _project(tmp: Path, sources: bool = True, sub: str = "") -> Path:
    """Проект с включёнными источниками: веб и Confluence (запись `sources:`) либо прежние два зеркала (без секции)."""
    if sub:
        (tmp / sub).mkdir()
        tmp = tmp / sub
    proj = make_project(tmp)
    if sources:
        cfg = proj / "aurora.config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8")
                       + "\nsources:\n  - id: Web\n    module: web\n    path: Sources/Web\n"
                         "  - id: Confluence\n    module: confluence-dc\n    path: Sources/Confluence\n", encoding="utf-8")
    return proj


# ------------------------------------------------------------------ режим доступа источников

@test
def test_source_access_defaults_web_needs_internet_everything_else_as_before(tmp: Path):
    """Нет записи: у веба умолчание модуля «без VPN, требует интернет»; остальные источники — «как раньше» (доступны)."""
    G = _g()
    proj = _project(tmp)
    by = {s["id"]: s for s in G.source_instances(str(proj))}
    web, conf = by["Web"], by["Confluence"]
    assert (web["mode"], web["origin"], web["internet"], web["vpn"]) == ("internet", "manifest", True, ""), \
        f"умолчание веба: {web}"
    assert (conf["mode"], conf["origin"], conf["internet"], conf["vpn"]) == ("", "default", False, ""), \
        f"источник без настройки должен остаться «как раньше»: {conf}"
    assert G.needs_of_source(conf) == {"profile": "", "internet": False}, "источник без настройки что-то требует"
    assert G.needs_of_source(web) == {"profile": "", "internet": True}, "веб не требует интернет"
    manifest = json.loads((KIT / "connectors" / "web" / "connector.json").read_text(encoding="utf-8"))
    assert manifest["access"] == {"mode": "internet"}, "умолчание веба не объявлено в его манифесте"
    src = (SCRIPTS / "conn_gate.py").read_text(encoding="utf-8")
    for product in ("confluence", "jira", "Confluence", "Jira"):
        assert product not in src, f"движок узнаёт источник по названию продукта («{product}»), а не по манифесту"
    # прежний проект без секции sources: — два исторических зеркала, оба «как раньше»
    legacy = _project(tmp, sources=False, sub="legacy")
    ids = {s["id"]: s["mode"] for s in G.source_instances(str(legacy))}
    assert ids and all(m == "" for m in ids.values()), f"прежние зеркала без настройки изменили поведение: {ids}"


@test
def test_source_access_block_roundtrip_keeps_the_rest_of_the_config(tmp: Path):
    """Запись блока `source_access:` не трогает остальной конфиг ни байтом: комментарии, CRLF, BOM, соседние разделы."""
    G = _g()
    base = ("﻿# шапка проекта\r\nproject:\r\n  name: \"Test\"\r\n\r\ncards:\r\n  glossary:\r\n    title: \"X\"\r\n\r\n"
            "sources:\r\n  - id: Web\r\n    module: web\r\n    path: Sources/Web\r\n\r\n# хвост\r\nartifacts:\r\n  us:\r\n    out: Artifacts/us\r\n")
    access = {"Web": {"mode": "internet", "internet": True}, "Old Wiki (2)": {"mode": "vpn", "vpn": "VPN-1", "offline_cache": True}}
    out = G.write_access(base, access)
    assert out.startswith("﻿") and out.startswith(base.rstrip("\r\n") ), "метка порядка байтов или начало файла изменились"
    for chunk in ("# шапка проекта\r\nproject:", "cards:\r\n  glossary:\r\n    title: \"X\"", "# хвост\r\nartifacts:\r\n  us:\r\n    out: Artifacts/us\r\n"):
        assert chunk in out, f"запись блока задела соседний раздел: {chunk!r}"
    assert "\n" not in out.replace("\r\n", ""), "в файле с CRLF появились строки с одним LF"
    found, problems = G.parse_access(out)
    assert found == access and not problems, f"блок не читается обратно: {found} {problems}"
    # повторная запись того же — файл не меняется; пустые режимы убирают блок, остальное цело
    assert G.write_access(out, access) == out, "запись неидемпотентна"
    cleared = G.write_access(out, {})
    assert "source_access" not in cleared and cleared.count("sources:") == 1 and "artifacts:" in cleared, "очистка задела соседей"
    assert G.write_access(base, {}) == base, "пустые режимы изменили файл без блока"


@test
def test_source_access_parse_names_problems_and_never_raises_on_hand_edits(tmp: Path):
    """Ручная правка блока: негодное называется, годное остаётся, разбор не падает."""
    G = _g()
    text = ('project:\n  name: "T"\nsource_access:\n  Web:\n    mode: "teleport"\n    internet: "maybe"\n    colour: "red"\n'
            '  Wiki:\n    mode: vpn\n    vpn: "bad name!"\n    offline_cache: "true"\n  Odd: oops\n  - stray\n')
    found, problems = G.parse_access(text)
    assert found["Wiki"].get("mode") == "vpn" and found["Wiki"].get("offline_cache") is True and "vpn" not in found["Wiki"], found
    assert "mode" not in found.get("Web", {}), "неизвестный режим принят"
    joined = " ".join(problems)
    for word in ("teleport", "maybe", "colour", "bad name", "Odd"):
        assert word in joined, f"проблема про «{word}» не названа: {problems}"
    assert G.parse_access("")[0] == {} and G.parse_access("project:\n  name: X\n") == ({}, []), "пустой конфиг"


@test
def test_source_access_save_validates_conflicts_and_keeps_config_bytes(tmp: Path):
    """Запись режимов: негодное — код, устаревший слепок — конфликт, годное — только блок; файл не в UTF-8 не трогается."""
    G = _g()
    proj = _project(tmp)
    cfg = proj / "aurora.config.yaml"
    before = cfg.read_bytes()
    state = G.access_state(str(proj))
    res = G.save_access(str(proj), {"Web": {"mode": "internet"}, "Confluence": {"mode": "vpn", "vpn": "VPN-1", "internet": False,
                                                                           "offline_cache": True}}, state["digest"])
    assert res["ok"], res
    after = cfg.read_bytes()
    assert after.startswith(before.rstrip(b"\n")) and b"source_access:" in after, "блок не дописан к прежнему конфигу"
    assert G.parse_access(after.decode("utf-8"))[0]["Confluence"] == {"mode": "vpn", "vpn": "VPN-1", "internet": False,
                                                                       "offline_cache": True}, "режим не записан как прислан"
    conf = {s["id"]: s for s in G.source_instances(str(proj))}["Confluence"]
    assert (conf["mode"], conf["vpn"], conf["origin"], conf["offline_cache"]) == ("vpn", "VPN-1", "config", True), conf
    assert G.needs_of_source(conf) == {"profile": "VPN-1", "internet": False}, "режим vpn не требует профиль"
    # негодные значения — коды, файл не меняется
    for code, access in (("bad_mode", {"Web": {"mode": "warp"}}), ("bad_name", {"Web": {"mode": "vpn", "vpn": "a b"}}),
                         ("bad_flag", {"Web": {"internet": "yes"}}), ("bad_field", {"Web": {"colour": 1}}),
                         ("bad_source", {"": {"mode": "local"}}), ("bad_access", [])):
        keep = cfg.read_bytes()
        res = G.save_access(str(proj), access, G.access_state(str(proj))["digest"])
        assert not res["ok"] and res["code"] == code, f"{code}: {res}"
        assert cfg.read_bytes() == keep, f"{code}: отказ изменил конфиг"
    # потерянное обновление: слепок, снятый до чужой правки
    opened = G.access_state(str(proj))["digest"]
    assert G.save_access(str(proj), {"Web": {"mode": "local"}}, opened)["ok"]
    keep = cfg.read_bytes()
    res = G.save_access(str(proj), {"Web": {"mode": "vpn", "vpn": "VPN-2"}}, opened)
    assert res.get("conflict") and res["code"] == "conflict" and cfg.read_bytes() == keep, f"устаревший слепок принят: {res}"
    # режим vpn без выбранного профиля — «как раньше» (профиль не задан → доступен)
    assert G.save_access(str(proj), {"Confluence": {"mode": "vpn"}}, G.access_state(str(proj))["digest"])["ok"]
    conf = {s["id"]: s for s in G.source_instances(str(proj))}["Confluence"]
    assert G.needs_of_source(conf) == {"profile": "", "internet": False}, "vpn без профиля стал блокировать"
    cfg.write_bytes("project:\n  name: \"Тест\"\n".encode("cp1251"))
    keep = cfg.read_bytes()
    res = G.save_access(str(proj), {"Web": {"mode": "local"}})
    assert not res["ok"] and res["code"] == "not_utf8" and cfg.read_bytes() == keep, f"конфиг не в UTF-8 перезаписан: {res}"


@test
def test_source_access_panel_api_state_save_and_conflict(tmp: Path):
    """`/api/net/access`: источники проекта с режимами, профили машины; запись — 200/400/409; соседние разделы конфига целы."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    restore = set_home(tmp / "home")
    ck = importlib.import_module("aurora_cockpit")
    CN = importlib.import_module("connectivity")
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    assert CN.save(kit, {"enabled": True, "profiles": [{"name": "VPN-1"}, {"name": "VPN-2"}], "probes": []})["ok"]
    proj = _project(tmp)
    old_kit = ck.KIT
    ck.KIT = str(kit)
    CN.reset_services()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(tmp)]
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
        q = "?project=" + str(proj)
        st, d = call("GET", "/api/net/access" + q)
        assert st == 200 and d["profiles"] == ["VPN-1", "VPN-2"] and {s["id"] for s in d["sources"]} == {"Web", "Confluence"}, d
        assert d["modes"] == ["internet", "vpn", "local", "mixed"] and d["explicit"] == {}, d
        cfg_before = (proj / "aurora.config.yaml").read_text(encoding="utf-8")
        st, d2 = call("POST", "/api/net/access/save", {"project": str(proj), "expect": d["digest"],
                                                         "access": {"Confluence": {"mode": "vpn", "vpn": "VPN-2"}}})
        assert st == 200 and d2["ok"] and d2["state"]["explicit"]["Confluence"]["vpn"] == "VPN-2", f"{st} {d2}"
        cfg_after = (proj / "aurora.config.yaml").read_text(encoding="utf-8")
        assert cfg_after.startswith(cfg_before.rstrip("\n")), "форма стёрла или переписала соседние разделы конфига"
        st, d3 = call("POST", "/api/net/access/save", {"project": str(proj), "expect": d["digest"], "access": {}})
        assert st == 409 and d3["conflict"] is True, f"устаревший слепок: {st} {d3}"
        st, d4 = call("POST", "/api/net/access/save", {"project": str(proj), "expect": d2["state"]["digest"], "access": {"Web": {"mode": "x"}}})
        assert st == 400 and d4["code"] == "bad_mode", f"негодный режим: {st} {d4}"
        st, d5 = call("POST", "/api/net/access/save", {"project": str(tmp / "nope"), "access": {}})
        assert st in (400, 403, 404), "запись в неизвестный проект принята"
        st, d6 = call("GET", "/api/net/commands" + q)
        cmds = d6["commands"]
        assert st == 200 and cmds["sync:confluence"]["profiles"] == ["VPN-2"] and cmds["sync:confluence"]["sources"] == ["Confluence"], \
            f"requires_vpn_profiles команды не вычислен из источников: {cmds['sync:confluence']}"
        assert cmds["sync:web"]["profiles"] == [] and cmds["sync:web"]["internet"] is True, "веб-синк: интернет без профиля"
        assert cmds["kb:lint"]["offline"] is True and cmds["kb:lint"]["types"] == [3], "локальная команда должна работать без связи (тип 3)"
        assert cmds["agent:build"]["llm"] is True and 2 in cmds["agent:build"]["types"], "команда с моделью без метки LLM"
    finally:
        srv.shutdown()
        srv.server_close()
        CN.reset_services()
        ck.KIT = old_kit
        restore()


# ------------------------------------------------------------------ провайдеры моделей

@test
def test_models_provider_binding_defaults_and_old_files_read_unchanged(tmp: Path):
    """Старый `models.json` читается без изменений: провайдер получает `vpn: ""` и проверку «не проверять»."""
    MC = importlib.import_module("model_config")
    old = {"version": 1, "providers": [{"id": "gw", "name": "GW", "type": "openai", "url": "https://llm.example.com/v1", "key": "k"}],
           "capabilities": {"llm": {"roles": [{"id": "worker", "backends": [{"provider": "gw", "model": "m"}]}]}}}
    clean, notes = MC.normalize(old)
    p = clean["providers"][0]
    assert p["vpn"] == "" and p["health"] == {"check": "off", "path": "/models"} and not notes, f"умолчания привязки: {p} {notes}"
    assert clean["capabilities"]["llm"]["roles"][0]["backends"][0]["provider"] == "gw", "старая цепочка изменилась"
    cfg = MC.to_config(clean)
    assert cfg["backends"][0]["vpn"] == "" and cfg["chains"]["llm"]["worker"][0]["vpn"] == "", "to_config не несёт привязку"
    # привязка и проверка читаются; негодное имя профиля снимается с замечанием, путь — по умолчанию
    bound = dict(old["providers"][0], vpn="VPN-1", health={"check": "auth", "path": "/v1/models"})
    ok, _ = MC.normalize({"providers": [bound]})
    assert ok["providers"][0]["vpn"] == "VPN-1" and ok["providers"][0]["health"] == {"check": "auth", "path": "/v1/models"}, ok["providers"][0]
    bad, notes = MC.normalize({"providers": [dict(bound, vpn="a b", health={"check": "soon", "path": "models"})]})
    assert bad["providers"][0]["vpn"] == "" and bad["providers"][0]["health"] == {"check": "off", "path": "/models"} and len(notes) == 2, \
        f"негодная привязка или проверка: {bad['providers'][0]} {notes}"
    # привязка видна и в списке провайдеров соединений (без ключа в состоянии)
    CN = importlib.import_module("connectivity")
    rows = CN.providers_of(None, ok)
    assert rows[0]["profile"] == "VPN-1" and rows[0]["check"] == "auth" and rows[0]["local"] is False, rows


@test
def test_models_save_keeps_unknown_fields_and_refuses_stale_pages(tmp: Path):
    """Запись `models.json`: неизвестное переносится из файла, слепок устаревшей страницы — конфликт, ключ не теряется."""
    MC = importlib.import_module("model_config")
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    path = MC.models_path(kit)
    path.write_text(json.dumps({"version": 1, "x_machine": {"keep": 1}, "providers": [
        {"id": "gw", "name": "GW", "type": "openai", "url": "https://llm.example.com/v1", "key": "k1", "x_note": "keep me"}],
        "capabilities": {}}), encoding="utf-8")
    stamp = MC.file_stamp(kit)
    page = {"providers": [{"id": "gw", "name": "GW", "type": "openai", "url": "https://llm.example.com/v1", "key": "k1",
                           "vpn": "VPN-1", "health": {"check": "reach", "path": "/models"}}]}
    res = MC.save(kit, page, stamp)
    assert res["ok"], res
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["x_machine"] == {"keep": 1} and saved["providers"][0]["x_note"] == "keep me", "форма «Моделей» стёрла неизвестное"
    assert saved["providers"][0]["vpn"] == "VPN-1" and saved["providers"][0]["key"] == "k1", "привязка не записана или ключ потерян"
    keep = path.read_bytes()
    res = MC.save(kit, dict(page, providers=[]), stamp)
    assert res.get("conflict") and path.read_bytes() == keep, f"устаревший слепок принят: {res}"
    assert MC.save(kit, page)["ok"], "запись без слепка (прежние клиенты) должна работать как раньше"
    assert not list(path.parent.glob("*.tmp")), "остался временный файл"


@test
def test_models_panel_state_offers_profiles_and_stamp_and_conflicts_give_409(tmp: Path):
    """`/api/models`: имена профилей соединений (без адресов) и слепок; `/api/models/save` со старым слепком — 409."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    restore = set_home(tmp / "home")
    ck = importlib.import_module("aurora_cockpit")
    CN = importlib.import_module("connectivity")
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    assert CN.save(kit, {"enabled": True, "profiles": [{"name": "VPN-1"}],
                         "probes": [{"profile": "VPN-1", "type": "tcp", "host": "secret-host.example.com", "port": 443}]})["ok"]
    old_kit = ck.KIT
    ck.KIT = str(kit)
    CN.reset_services()
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
        assert st == 200 and d["net_profiles"] == ["VPN-1"] and d["health_checks"] == ["off", "reach", "auth"], d
        assert "secret-host.example.com" not in json.dumps(d), "адрес probe попал в ответ раздела «Модели»"
        models = {"providers": [{"id": "gw", "name": "GW", "type": "openai", "url": "https://llm.example.com/v1", "key": "k",
                                 "vpn": "VPN-1", "health": {"check": "reach", "path": "/models"}}], "capabilities": {}}
        st, r = call("POST", "/api/models/save", {"models": models, "expect": d["stamp"]})
        assert st == 200 and r["ok"], f"{st} {r}"
        st, r = call("POST", "/api/models/save", {"models": models, "expect": d["stamp"]})
        assert st == 409 and r["conflict"] is True, f"устаревший слепок: {st} {r}"
        st, r = call("POST", "/api/models/save", {"models": models})
        assert st == 409 and r["conflict"] is True, f"запрос без слепка перезаписал существующий файл: {st} {r}"
        (kit / "local" / "models.json").unlink()
        st, r = call("POST", "/api/models/save", {"models": models})
        assert st == 200 and r["ok"], "первая запись (файла ещё нет) без слепка должна проходить"
    finally:
        srv.shutdown()
        srv.server_close()
        CN.reset_services()
        ck.KIT = old_kit
        restore()


# ------------------------------------------------------------------ метаданные команд

@test
def test_command_deps_file_matches_the_registry_and_the_check_catches_drift(tmp: Path):
    """`command_deps.json`: запись на каждую команду реестра, формат полей, модули источников; расхождение краснит проверку."""
    G = _g()
    K = importlib.import_module("kit_commands")
    rows = K.read_registry()
    assert G.check_deps(rows, str(KIT)) == [], f"метаданные команд расходятся с реестром: {G.check_deps(rows, str(KIT))}"
    data = json.loads((KIT / "command_deps.json").read_text(encoding="utf-8"))
    cmds = data["commands"]
    assert cmds["sync:confluence"]["requires_sources"] == ["confluence-dc"] and cmds["sync:web"]["requires_sources"] == ["web"] \
        and cmds["sync:jira"]["requires_sources"] == ["jira-dc"], "sync:* не ведут на модули своих источников"
    assert cmds["agent:build"]["requires_llm"] is True and cmds["kb:lint"]["offline_capable"] is True, "умолчания по исполнителю"
    assert all(set(r) == set(G.DEPS_FIELDS) for r in cmds.values()), "поля метаданных не одинаковы у всех команд"
    # дрейф: команда без записи, лишняя запись, неверный тип и неизвестный модуль
    broken = json.loads(json.dumps(data))
    del broken["commands"]["kb:lint"]
    broken["commands"]["ghost:cmd"] = broken["commands"]["kb:build"]
    broken["commands"]["kb:build"]["requires_llm"] = "yes"
    broken["commands"]["sync:web"]["requires_sources"] = ["nope-module"]
    path = tmp / "deps.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    problems = " | ".join(G.check_deps(rows, str(KIT), path))
    for word in ("kb:lint", "ghost:cmd", "requires_llm", "nope-module"):
        assert word in problems, f"расхождение «{word}» не названо: {problems}"
    # формат реестра не тронут: семь колонок, как читает прежний kit_commands
    assert all(len([p for p in ln.split("|")]) == 7 for ln in (KIT / "commands.txt").read_text(encoding="utf-8").splitlines()
               if ln.strip() and not ln.startswith("#")), "в commands.txt появилась лишняя колонка"
    assert "command_deps.json" in (KIT / "engine_manifest.txt").read_text(encoding="utf-8"), "метаданные команд не едут в проекты"


@test
def test_command_requirements_are_computed_from_sources_and_providers(tmp: Path):
    """`requires_vpn_profiles` вычисляется: профили источников проекта и провайдеров; метки 1/2/3 — только подписи."""
    G = _g()
    proj = _project(tmp)
    assert G.save_access(str(proj), {"Confluence": {"mode": "vpn", "vpn": "VPN-1"}, "Web": {"mode": "internet"}}, None)["ok"]
    deps = G.load_deps()
    sources = G.source_instances(str(proj))
    provs = [{"id": "a", "profile": "VPN-2", "local": False}, {"id": "b", "profile": "", "local": False},
             {"id": "c", "profile": "", "local": True}]
    r = G.command_requirements("sync:confluence", str(proj), deps, sources, provs)
    assert r["profiles"] == ["VPN-1"] and r["types"] == [1] and r["llm"] is False and r["offline_capable"] is False, r
    r = G.command_requirements("agent:build", str(proj), deps, sources, provs)
    assert r["llm"] is True and r["llm_profiles"] == ["VPN-2"] and r["llm_internet"] is True and r["types"] == [2], r
    r = G.command_requirements("make:review-auto", str(proj), deps, sources, provs)
    assert r["types"] == [1, 2] and r["profiles"] == ["VPN-1"], f"команда с источником и моделью: {r}"
    r = G.command_requirements("kb:lint", str(proj), deps, sources, provs)
    assert r["types"] == [3] and r["offline_capable"] is True and r["profiles"] == [], r
    # источник не подключён в проекте — требований от него нет
    legacy = _project(tmp, sources=False, sub="l2")
    assert G.command_requirements("sync:web", str(legacy), deps)["sources"] == [], "требование от неподключённого источника"
    # required_for: профили и интернет по включённым источникам и провайдерам кита
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    (kit / "local" / "models.json").write_text(json.dumps({"providers": [
        {"id": "gw", "name": "GW", "type": "openai", "url": "https://llm.example.com/v1", "key": "k", "vpn": "VPN-2"},
        {"id": "unused", "name": "U", "type": "openai", "url": "https://u.example.org/v1", "key": "k", "vpn": "VPN-9"}],
        "capabilities": {"llm": {"roles": [{"id": "worker", "backends": [{"provider": "gw", "model": "m"}]}]}}}), encoding="utf-8")
    CN = importlib.import_module("connectivity")
    need = G.required_for(str(proj), CN.AvailabilityService(kit))
    assert need["profiles"] == ["VPN-1", "VPN-2"] and need["providers"] == ["gw"] and need["internet"] is True, \
        f"required_for: {need}"
    # ярлыки типов — на показ: логика на них не стоит
    assert G.net_types({"requires_sources": [], "requires_internet": False, "requires_llm": False}) == [3]


@test
def test_commands_tab_and_project_settings_show_the_new_columns_and_group(_t):
    """Закладка «Команды» показывает VPN, LLM, работу без связи и источники; «Настройки проекта» — режим доступа источников."""
    cmds = (KIT / "cockpit" / "modules" / "commands" / "view.js").read_text(encoding="utf-8")
    for word in ("/api/net/commands", "commands.net.vpn", "commands.net.llm", "commands.net.offline", "commands.net.sources"):
        assert word in cmds, f"в закладке «Команды» нет {word}"
    panel = (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    assert "async function renderSourceAccess(box)" in panel and 'sgroup(box, "project:access"' in panel, "нет группы режима доступа"
    assert "/api/net/access/save" in panel and "expect: digest" in panel, "группа не шлёт слепок блока"
    assert 'saveButton("access"' in panel and "renderProject()" not in panel[panel.index("async function renderSourceAccess"):panel.index("/* ---------------- типы карточек")], \
        "группа сбрасывает несохранённые правки соседних групп"
    ru = json.loads((KIT / "cockpit" / "i18n" / "ru.json").read_text(encoding="utf-8"))
    for key in ("access.mode.internet", "access.mode.vpn", "access.mode.local", "access.mode.mixed", "access.h_internet", "access.h_cache",
                "access.err.bad_mode", "dirty.access"):
        assert key in ru, f"в каталоге ядра нет {key}"
    assert not re.search(r"https?://(?!example\.)", ru["access.about"] + ru["access.h_vpn"]), "в подсказках настоящий адрес"
