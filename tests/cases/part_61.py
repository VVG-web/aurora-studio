"""Проверки движка Aurora, часть 61. Каркас и помощники — tests/harness.py.

Раздел панели «Проверка соединения»: каждый путь записи (`/api/net/save`) — годные и негодные значения с кодами,
предел в десять probe, потерянное обновление (409), соседние разделы файла не стираются; проверка «на месте»
(`/api/net/probe`) — черновик probe до сохранения; значок в шапке; разметка и каталоги строк модуля.
Сервер панели — настоящий, на свободном порту; кит и домашняя папка — временные, личная настройка машины не
читается и не пишется; сеть — только подставной сервер на 127.0.0.1.
"""
from __future__ import annotations

import http.client
import http.server
import importlib
import json
import re
import sys
import threading
from pathlib import Path

from harness import KIT, SCRIPTS, set_home, test  # noqa: F401

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(KIT / "cockpit"))


class _Net:
    """Сервер панели на временном ките: `ck.KIT` смотрит во временную папку, пока проверка идёт."""

    def __init__(self, tmp: Path):
        from http.server import ThreadingHTTPServer
        self.restore_home = set_home(tmp / "home")
        self.ck = importlib.import_module("aurora_cockpit")
        self.cn = importlib.import_module("connectivity")
        self.kit = tmp / "kit"
        (self.kit / "local").mkdir(parents=True)
        self.old_kit = self.ck.KIT
        self.ck.KIT = str(self.kit)
        self.cn.reset_services()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.ck.Handler)
        self.srv.roots = [str(tmp / "roots")]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, method: str, path: str, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=60)
        headers = {"X-Aurora-Token": self.ck.TOKEN}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        return r.status, json.loads(r.read().decode("utf-8") or "{}")

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.cn.reset_services()
        self.ck.KIT = self.old_kit
        self.restore_home()

    @property
    def file(self) -> Path:
        return self.kit / "local" / "connectivity.json"


def _probe(profile="VPN-1", host="example.com", port=443, **kw) -> dict:
    return {"profile": profile, "type": "tcp", "host": host, "port": port, **kw}


@test
def test_net_panel_state_endpoint_gives_config_stamp_limits_and_snapshot(tmp: Path):
    """`/api/net/state`: пустая выключенная настройка, слепок файла, пределы формы, сводка; сети не трогает."""
    net = _Net(tmp)
    try:
        st, d = net.call("GET", "/api/net/state")
        assert st == 200, f"состояние раздела: {st} {d}"
        assert d["config"]["enabled"] is False and d["config"]["profiles"] == [] and d["config"]["probes"] == [], \
            "на чистом ките настройка не пустая и не выключенная"
        assert d["stamp"] == "" and d["limits"]["probes"] == 10, f"слепок пустого файла или предел: {d['stamp']} {d['limits']}"
        assert d["snapshot"]["enabled"] is False and d["snapshot"]["state"] == "unknown", "сводка выключенной проверки"
        assert set(d["policies"]) == {"fail_fast", "partial", "queue", "partial_queue"} and d["config"]["policy"]["auto"] == "partial_queue", \
            "политика прогонов без связи по умолчанию — не partial + queue"
        assert d["path"].startswith(str(net.kit)), "путь файла настройки указывает не на временный кит"
        assert not net.file.exists(), "чтение состояния создало файл настройки"
    finally:
        net.close()


@test
def test_net_panel_save_accepts_valid_values_and_refuses_invalid_with_codes(tmp: Path):
    """`/api/net/save`: годное записывается, негодное — 400 с кодом и ничего не меняет в файле."""
    net = _Net(tmp)
    try:
        good = {"enabled": True, "settings": {"interval": 30, "timeout": 2, "fail_after": 2, "ok_after": 1},
                "policy": {"auto": "fail_fast"},
                "profiles": [{"name": "VPN-1", "aggregate": "quorum", "quorum": 2, "note": "для себя"}],
                "internet": {"aggregate": "any_healthy"},
                "probes": [_probe(), _probe(port=80), {"profile": "internet", "type": "http", "url": "https://example.com/",
                                                       "expect_status": [200, "3xx"], "expect_body": "ok"}]}
        st, d = net.call("POST", "/api/net/save", {"config": good, "expect": ""})
        assert st == 200 and d["ok"], f"годная настройка не записалась: {st} {d}"
        assert d["state"]["config"]["settings"]["interval"] == 30 and d["state"]["stamp"] == d["stamp"], "ответ записи без новой настройки и слепка"
        saved = json.loads(net.file.read_text(encoding="utf-8"))
        assert saved["profiles"][0]["quorum"] == 2 and saved["probes"][2]["expect_status"] == [200, "3xx"], "значения записаны не как присланы"
        assert saved["policy"]["auto"] == "fail_fast", "политика не записана"
        before = net.file.read_bytes()
        cases = {
            "bad_name": dict(good, profiles=[{"name": "bad name"}]),
            "bad_port": dict(good, probes=[_probe(port=0)]),
            "bad_host": dict(good, probes=[_probe(host="http://example.com")]),
            "bad_url": dict(good, probes=[{"profile": "internet", "type": "http", "url": "ftp://example.com/"}]),
            "bad_policy": dict(good, policy={"auto": "whenever"}),
            "bad_interval": dict(good, settings={"interval": 1}),
            "bad_timeout": dict(good, settings={"timeout": float("nan")}),
            "bad_aggregate": dict(good, profiles=[{"name": "VPN-1", "aggregate": "some"}]),
            "unknown_profile": dict(good, probes=[_probe(profile="VPN-7")]),
            "dup_profile": dict(good, profiles=[{"name": "VPN-1"}, {"name": "VPN-1"}]),
            "reserved_name": dict(good, profiles=[{"name": "internet"}]),
        }
        for code, cfg in cases.items():
            st, d = net.call("POST", "/api/net/save", {"config": cfg, "expect": net.cn.file_stamp(net.kit)})
            assert st == 400 and d["code"] == code and not d["ok"], f"{code}: ждали 400 и этот код, получили {st} {d.get('code')}"
            assert net.file.read_bytes() == before, f"{code}: отказ изменил файл настройки"
        st, d = net.call("POST", "/api/net/save", {"config": "не объект"})
        assert st == 400 and d["code"] == "bad_config", f"не объект: {st} {d}"
    finally:
        net.close()


@test
def test_net_panel_save_limit_of_ten_probes_names_the_reason(tmp: Path):
    """Одиннадцатый probe на кит — отказ `probe_limit` с понятным пределом; десять — проходят."""
    net = _Net(tmp)
    try:
        ten = {"enabled": True, "profiles": [{"name": "VPN-1"}], "probes": [_probe(port=1000 + i) for i in range(10)]}
        st, d = net.call("POST", "/api/net/save", {"config": ten})
        assert st == 200 and d["ok"], f"десять probe не записались: {d}"
        eleven = dict(ten, probes=ten["probes"] + [_probe(port=2000)])
        st, d = net.call("POST", "/api/net/save", {"config": eleven, "expect": net.cn.file_stamp(net.kit)})
        assert st == 400 and d["code"] == "probe_limit" and "10" in d["error"], f"11-й probe: {st} {d}"
        assert len(json.loads(net.file.read_text(encoding="utf-8"))["probes"]) == 10, "отказ изменил файл"
        st, state = net.call("GET", "/api/net/state")
        assert state["limits"]["probes"] == 10, "страница не знает предела"
    finally:
        net.close()


@test
def test_net_panel_save_concurrent_edit_gives_409_and_keeps_the_fresh_file(tmp: Path):
    """Потерянное обновление: страница открыта, файл изменили, сохранение со старым слепком — 409, файл цел."""
    net = _Net(tmp)
    try:
        base = {"enabled": True, "profiles": [{"name": "VPN-1"}], "probes": [_probe()]}
        assert net.call("POST", "/api/net/save", {"config": base})[0] == 200
        _st, opened = net.call("GET", "/api/net/state")                    # две страницы открыли одно и то же
        mine = dict(base, profiles=[{"name": "VPN-1", "note": "моя правка"}])
        other = dict(base, profiles=[{"name": "VPN-1", "note": "чужая правка"}])
        st, d = net.call("POST", "/api/net/save", {"config": other, "expect": opened["stamp"]})
        assert st == 200 and d["ok"], f"первая страница не записала: {d}"
        fresh = net.file.read_bytes()
        st, d = net.call("POST", "/api/net/save", {"config": mine, "expect": opened["stamp"]})
        assert st == 409 and d["conflict"] is True and d["code"] == "conflict", f"устаревший слепок не отклонён: {st} {d}"
        assert net.file.read_bytes() == fresh, "409 изменил файл — чужая правка потеряна"
        st, d = net.call("POST", "/api/net/save", {"config": mine})
        assert st == 409 and d["conflict"] is True, f"запрос без слепка перезаписал существующий файл: {st} {d}"
        assert net.file.read_bytes() == fresh, "запрос без слепка изменил файл"
        st, d = net.call("POST", "/api/net/save", {"config": mine, "expect": net.cn.file_stamp(net.kit), "project": str(tmp / "no-such-project")})
        assert st == 200 and d["ok"], "после перечитывания слепок верный, а запись отклонена (или неизвестный проект уронил ответ)"
    finally:
        net.close()


@test
def test_net_panel_save_keeps_sections_the_form_does_not_manage(tmp: Path):
    """Форма не стирает соседнее: раздел файла и лишние поля профиля и probe, которых она не знает, остаются."""
    net = _Net(tmp)
    try:
        net.file.write_text(json.dumps({
            "version": 1, "enabled": False, "x_machine_note": {"who": "owner", "list": [1, 2, 3]},
            "profiles": [{"name": "VPN-1", "x_color": "teal"}], "probes": [dict(_probe(id="p1"), x_owner_tag="keep")]}), encoding="utf-8")
        _st, opened = net.call("GET", "/api/net/state")
        cfg = {k: v for k, v in opened["config"].items() if k != "x_machine_note"}      # страница раздела не знала
        cfg["enabled"] = True
        cfg["profiles"] = [{"name": "VPN-1"}]                                           # и прислала профиль без лишнего поля
        cfg["probes"] = [_probe(id="p1", port=8443)]
        st, d = net.call("POST", "/api/net/save", {"config": cfg, "expect": opened["stamp"]})
        assert st == 200, d
        saved = json.loads(net.file.read_text(encoding="utf-8"))
        assert saved["x_machine_note"] == {"who": "owner", "list": [1, 2, 3]}, "форма стёрла неизвестный раздел файла"
        assert saved["profiles"][0]["x_color"] == "teal" and saved["probes"][0]["x_owner_tag"] == "keep", \
            "форма стёрла неизвестные поля профиля и probe"
        assert saved["enabled"] is True and saved["probes"][0]["port"] == 8443, "то, что форма меняет, не записано"
    finally:
        net.close()


@test
def test_net_panel_probe_endpoint_checks_a_draft_before_saving_and_writes_nothing(tmp: Path):
    """«Проверить» у ещё не сохранённого probe: ответ, время, код; в настройку и состояние ничего не пишется."""
    net = _Net(tmp)
    stub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), type("H", (http.server.BaseHTTPRequestHandler,), {
        "do_GET": lambda self: (self.send_response(200), self.end_headers(), self.wfile.write(b"pong")),
        "log_message": lambda self, *a: None}))
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    try:
        port = stub.server_address[1]
        st, d = net.call("POST", "/api/net/probe", {"draft": {"type": "tcp", "host": "127.0.0.1", "port": port}})
        assert st == 200 and d["result"]["ok"] is True and isinstance(d["result"]["ms"], int), f"TCP-черновик: {d}"
        st, d = net.call("POST", "/api/net/probe", {"draft": {"type": "http", "url": f"http://127.0.0.1:{port}/", "expect_body": "pong"}})
        assert d["result"]["ok"] is True, f"HTTP-черновик с текстом: {d}"
        st, d = net.call("POST", "/api/net/probe", {"draft": {"type": "http", "url": f"http://127.0.0.1:{port}/", "expect_body": "нет"}})
        assert d["result"]["ok"] is False and d["result"]["code"] == "body", f"нет ожидаемого текста: {d}"
        st, d = net.call("POST", "/api/net/probe", {"draft": {"type": "tcp", "host": "127.0.0.1", "port": 99999}})
        assert d["result"]["ok"] is False and d["result"]["problems"][0]["code"] == "bad_port", f"негодный черновик: {d}"
        assert not net.file.exists() and not (tmp / "home" / ".aurora" / "connectivity-state.json").exists(), \
            "проверка черновика записала настройку или состояние"
        # сохранённый probe — отдельной кнопкой, и тоже без записи состояния
        cfg = {"enabled": True, "profiles": [{"name": "VPN-1"}], "probes": [_probe(host="127.0.0.1", port=port, id="p1")]}
        assert net.call("POST", "/api/net/save", {"config": cfg})[0] == 200
        st, d = net.call("POST", "/api/net/probe", {"probe": "p1"})
        assert d["result"]["ok"] is True, f"сохранённый probe: {d}"
        st, d = net.call("POST", "/api/net/probe", {"probe": "nope"})
        assert d["result"]["problems"][0]["code"] == "unknown_probe", f"несуществующий probe: {d}"
        assert not (tmp / "home" / ".aurora" / "connectivity-state.json").exists(), "проверка одного probe записала состояние"
        # проверка всего («Проверить сейчас») состояние пишет, даже пока проверка выключена: владелец смотрит до включения
        st, d = net.call("POST", "/api/net/probe", {})
        assert st == 200 and d["snapshot"]["profiles"][0]["probes"][0]["ok"] is True, f"проверка всего: {d}"
        assert (tmp / "home" / ".aurora" / "connectivity-state.json").is_file(), "проверка всего не записала состояние"
    finally:
        stub.shutdown()
        stub.server_close()
        net.close()


@test
def test_net_panel_brief_for_the_header_indicator(tmp: Path):
    """`/api/net/brief` — для значка в шапке: выключено — молчит; включено — состояние и лежащие профили."""
    net = _Net(tmp)
    try:
        st, d = net.call("GET", "/api/net/brief")
        assert st == 200 and d["enabled"] is False, f"выключенная проверка: {d}"
        cfg = {"enabled": True, "profiles": [{"name": "VPN-1"}], "probes": [_probe(host="127.0.0.1", port=1)]}
        assert net.call("POST", "/api/net/save", {"config": cfg})[0] == 200
        net.call("POST", "/api/net/probe", {})
        st, d = net.call("GET", "/api/net/brief")
        assert d["enabled"] is True and d["state"] in ("online", "unknown", "degraded", "partial", "no_vpn", "offline"), d
        assert set(d) >= {"enabled", "state", "reasons", "down"}, f"в сводке шапки нет полей: {sorted(d)}"
        assert "host" not in json.dumps(d) and "127.0.0.1" not in json.dumps(d), "в сводке шапки адреса probe"
    finally:
        net.close()


@test
def test_net_panel_never_writes_outside_its_temporary_kit_and_home(tmp: Path):
    """Запись раздела идёт только в `local/connectivity.json` временного кита; настоящий кит и HOME не тронуты."""
    real = KIT / "local" / "connectivity.json"
    existed = real.exists()
    net = _Net(tmp)
    try:
        assert net.call("POST", "/api/net/save", {"config": {"enabled": True, "profiles": [], "probes": []}})[0] == 200
        assert net.file.is_file() and str(net.file).startswith(str(tmp)), "файл настройки лёг не во временный кит"
        assert real.exists() == existed, "тест тронул настоящий local/connectivity.json кита"
        files = sorted(p.relative_to(tmp).as_posix() for p in (tmp / "home").rglob("*") if p.is_file()) if (tmp / "home").exists() else []
        assert all(f.startswith(".aurora/") for f in files), f"в домашней папке появилось лишнее: {files}"
    finally:
        net.close()


@test
def test_net_module_wiring_manifest_catalogs_and_header_indicator(_t):
    """Раздел — папка по контракту модулей; сохранение шлёт слепок и разбирает 409; правка не теряется при возврате;
    значок в шапке и маршруты `/api/net/*` есть; ядро не называет раздел по имени в адресах API."""
    mod = KIT / "cockpit" / "modules" / "connectivity"
    man = json.loads((mod / "module.json").read_text(encoding="utf-8"))
    assert man["id"] == "connectivity" and man["group"] == "machine" and "net:status" in man["commands"], man
    js = (mod / "view.js").read_text(encoding="utf-8")
    assert "export const lifecycle = 1;" in js, "раздел не объявил lifecycle"
    assert "expect: DATA.stamp" in js and "r.conflict" in js, "сохранение не шлёт слепок или не разбирает конфликт"
    assert "if (DIRTY && C){ draw(ctx); return; }" in js, "возврат в раздел стирает несохранённую правку"
    assert "delete send.notes" in js, "служебные поля страницы уходят в файл"
    for route in ("/api/net/state", "/api/net/save", "/api/net/probe", "/api/net/brief"):
        assert route in (KIT / "cockpit" / "aurora_cockpit.py").read_text(encoding="utf-8"), f"сервер не знает {route}"
    index = (KIT / "cockpit" / "ui" / "index.html").read_text(encoding="utf-8")
    panel = (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    assert 'id="connBar"' in index and "refreshNetBar" in panel, "в шапке нет значка состояния соединений"
    assert not re.search(r"/api/connectivity", panel + index), "ядро панели называет раздел по имени в адресе API"
    ru = json.loads((mod / "i18n" / "ru.json").read_text(encoding="utf-8"))
    for code in ("bad_name", "bad_port", "bad_url", "bad_policy", "probe_limit", "conflict", "broken_file", "dup_profile", "reserved_name",
                 "bad_aggregate", "bad_quorum", "bad_interval", "bad_timeout", "bad_fail_after", "bad_ok_after", "unknown_profile"):
        assert f"connectivity.err.{code}" in ru, f"в каталоге нет строки для кода ошибки {code}"
    for word in ("probe_what", "wiz1", "wiz2", "wiz3", "h_type.tcp", "h_type.http", "h_type.icmp", "internet_about", "empty_how"):
        assert f"connectivity.{word}" in ru, f"в каталоге нет подсказки {word}"
    assert "example.com" in ru["connectivity.h_url"] and "VPN-1" in ru["connectivity.wiz1"], "в подсказках нет примеров"
