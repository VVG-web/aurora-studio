"""Проверки движка Aurora, часть 60. Каркас и помощники — tests/harness.py.

Проверка доступности соединений (`scripts/connectivity.py`): настройка профилей и probe, виды проверок
(TCP, HTTP, ICMP), правила агрегации, антидребезг, состояние на диске, предзапускная проверка,
глобальные состояния и команды `net:status` / `net:probe`.
Сеть настоящая только на 127.0.0.1 (подставные серверы потока), `ping` подменяется; домашняя папка,
файл состояния и кит — временные.
"""
from __future__ import annotations

import http.server
import importlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from harness import KIT, SCRIPTS, run, set_home, test  # noqa: F401

sys.path.insert(0, str(SCRIPTS))


def _cn():
    return importlib.import_module("connectivity")


class _Clock:
    """Поддельные часы: время идёт, только когда тест его двигает."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


class _Stub:
    """Подставной сервер на 127.0.0.1: отвечает заданными статусом и телом, считает обращения."""

    def __init__(self, status: int = 200, body: bytes = b"hello aurora", delay: float = 0.0, location: str = ""):
        outer = self
        self.hits = 0

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.hits += 1
                if delay:
                    time.sleep(delay)
                self.send_response(outer.status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *a):  # noqa: D401
                return

        self.status, self.body = status, body
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _kit(tmp: Path) -> Path:
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True, exist_ok=True)
    return kit


def _cfg(**kw) -> dict:
    base = {"enabled": True, "profiles": [{"name": "VPN-1"}], "probes": []}
    base.update(kw)
    return base


def _tcp(profile: str, port: int, **kw) -> dict:
    return {"profile": profile, "type": "tcp", "host": "127.0.0.1", "port": port, **kw}


# ------------------------------------------------------------------ настройка

@test
def test_connectivity_config_validation_gives_coded_errors(tmp: Path):
    """Негодные значения называются кодом и местом, а не молча принимаются или падают.

    Код стабилен: по нему панель берёт строку из каталога языка. Имена профилей, пределы порогов,
    адреса (без схемы у TCP, без логина в HTTP), порт, правило, политика — всё проверяется сервером.
    """
    CN = _cn()

    def codes(data) -> list:
        return [p["code"] for p in CN.clean(data)[1]]

    assert codes(_cfg(probes=[_tcp("VPN-1", 443)])) == [], "годная настройка дала замечания"
    bad = {
        "bad_name": _cfg(profiles=[{"name": "-vpn"}]),
        "reserved_name": _cfg(profiles=[{"name": "Internet"}]),
        "dup_profile": _cfg(profiles=[{"name": "VPN-1"}, {"name": "vpn-1"}]),
        "bad_aggregate": _cfg(profiles=[{"name": "VPN-1", "aggregate": "most"}]),
        "bad_quorum": _cfg(profiles=[{"name": "VPN-1", "aggregate": "quorum", "quorum": 99}]),
        "bad_fail_after": _cfg(profiles=[{"name": "VPN-1", "fail_after": 0}]),
        "bad_interval": _cfg(settings={"interval": 5}),
        "bad_timeout": _cfg(settings={"timeout": 31}),
        "bad_policy": _cfg(policy={"auto": "yolo"}),
        "bad_flag": _cfg(enabled="yes"),
        "unknown_profile": _cfg(probes=[_tcp("VPN-9", 443)]),
        "bad_type": _cfg(probes=[{"profile": "VPN-1", "type": "udp", "host": "example.com"}]),
        "bad_host": _cfg(probes=[{"profile": "VPN-1", "type": "tcp", "host": "http://example.com", "port": 80}]),
        "bad_port": _cfg(probes=[_tcp("VPN-1", 70000)]),
        "bad_url": _cfg(probes=[{"profile": "VPN-1", "type": "http", "url": "https://user:pw@example.com/"}]),
        "bad_status": _cfg(probes=[{"profile": "VPN-1", "type": "http", "url": "https://example.com/", "expect_status": ["9xx"]}]),
        "bad_probe_id": _cfg(probes=[dict(_tcp("VPN-1", 443), id="Bad Id")]),
    }
    for code, data in bad.items():
        assert code in codes(data), f"{code}: не названо; получено {codes(data)}"
    # хост, начинающийся с «-», не должен попасть в команду ping как ключ
    assert "bad_host" in codes(_cfg(probes=[{"profile": "VPN-1", "type": "icmp", "host": "-c9"}])), \
        "хост «-c9» прошёл проверку — ушёл бы в ping ключом"
    clean, _ = CN.clean({"enabled": True, "x_future": {"a": 1}, "profiles": [{"name": "VPN-1", "x_p": 5}],
                         "probes": [dict(_tcp("VPN-1", 443), x_q="keep")]})
    assert clean["x_future"] == {"a": 1} and clean["profiles"][0]["x_p"] == 5 and clean["probes"][0]["x_q"] == "keep", \
        "неизвестные поля не перенесены как есть"
    assert clean["probes"][0]["id"] == "p1", "id probe не заведён автоматически"


@test
def test_connectivity_limit_is_ten_probes_per_kit(tmp: Path):
    """Не больше десяти probe на кит целиком — одиннадцатый отклоняется кодом `probe_limit`, запись не идёт."""
    CN = _cn()
    kit = _kit(tmp)
    data = _cfg(probes=[_tcp("VPN-1", 1000 + i) for i in range(CN.MAX_PROBES)])
    assert CN.save(kit, data)["ok"], "десять probe не сохранились"
    assert len(CN.load(kit)["probes"]) == 10, "после записи не десять probe"
    data["probes"].append(_tcp("VPN-1", 2000))
    res = CN.save(kit, data)
    assert not res["ok"] and res["code"] == "probe_limit", f"одиннадцатый probe не отклонён: {res}"
    assert len(CN.load(kit)["probes"]) == 10, "отказ изменил файл"
    mixed = _cfg(probes=[_tcp("VPN-1", 1000 + i) for i in range(6)] + [_tcp("internet", 3000 + i) for i in range(5)])
    assert CN.save(kit, mixed)["code"] == "probe_limit", "предел считается не на кит целиком, а на профиль"


@test
def test_connectivity_save_keeps_neighbours_refuses_conflicts_and_broken_files(tmp: Path):
    """Запись не стирает чужое, не затирает свежую правку и не ломает файл человека.

    Раздел верхнего уровня и лишнее поле профиля, которых страница не знает, переносятся из прежнего файла;
    слепок `expect` устаревшей страницы — отказ `conflict`; не разбираемый файл заменяется только по просьбе.
    """
    CN = _cn()
    kit = _kit(tmp)
    path = CN.config_path(kit)
    path.write_text(json.dumps({"version": 1, "future_section": {"keep": [1, 2]},
                                "profiles": [{"name": "VPN-1", "note_x": "keep me"}], "probes": []}), encoding="utf-8")
    stamp = CN.file_stamp(kit)
    res = CN.save(kit, {"enabled": True, "profiles": [{"name": "VPN-1", "aggregate": "all_healthy"}], "probes": []}, stamp)
    assert res["ok"], res
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["future_section"] == {"keep": [1, 2]}, "неизвестный раздел файла пропал при записи"
    assert on_disk["profiles"][0]["note_x"] == "keep me", "неизвестное поле профиля пропало при записи"
    assert on_disk["profiles"][0]["aggregate"] == "all_healthy" and on_disk["enabled"] is True, "новое значение не записано"
    # устаревший слепок: страница открыта до чужой правки
    before = path.read_bytes()
    res = CN.save(kit, {"enabled": False, "profiles": [], "probes": []}, stamp)
    assert res.get("conflict") and res["code"] == "conflict", f"устаревший слепок принят: {res}"
    assert path.read_bytes() == before, "отказ по конфликту изменил файл"
    # запись без слепка проходит (первая запись), с верным — тоже
    assert CN.save(kit, {"enabled": False, "profiles": [], "probes": []}, CN.file_stamp(kit))["ok"], "верный слепок отклонён"
    # битый файл
    path.write_text("{not json", encoding="utf-8")
    res = CN.save(kit, {"enabled": True, "profiles": [], "probes": []})
    assert not res["ok"] and res["code"] == "broken_file", f"битый файл молча заменён: {res}"
    assert path.read_text(encoding="utf-8") == "{not json", "отказ изменил битый файл"
    assert CN.save(kit, {"enabled": True, "profiles": [], "probes": []}, None, replace_broken=True)["ok"], \
        "замена битого файла по просьбе не прошла"
    assert CN.load(kit)["enabled"] is True, "после замены файл не читается"
    assert not list(path.parent.glob("*.tmp")), "рядом остался временный файл записи"
    # проект вместо кита: настройка машины в него не пишется (его `local/` не закрыт от git)
    proj = tmp / "proj"
    proj.mkdir()
    (proj / "aurora.config.yaml").write_text("project:\n  name: X\n", encoding="utf-8")
    res = CN.save(proj, {"enabled": True, "profiles": [], "probes": []})
    assert not res["ok"] and res["code"] == "not_kit" and not (proj / "local").exists(), f"запись ушла в проект: {res}"


@test
def test_connectivity_test_run_never_reads_the_developers_personal_config(_t):
    """Прогон тестов не читает настоящий `local/connectivity.json` кита: он личный, как `models.json`."""
    CN = _cn()
    A = importlib.import_module("aurora_common")
    assert A.personal_kit_file(os.path.join(str(KIT), "local", "connectivity.json")), \
        "файл настройки соединений не объявлен личным — тест прочёл бы настройку машины"
    assert CN.load(KIT)["enabled"] is False and not CN.load(KIT)["probes"], "тест увидел настройку настоящего кита"
    res = CN.save(KIT, {"enabled": True, "profiles": [], "probes": []})
    assert not res["ok"] and res["code"] == "tests", f"тест записал в настоящий кит: {res}"


# ------------------------------------------------------------------ агрегация и антидребезг

@test
def test_connectivity_aggregation_rules_any_quorum_all(_t):
    """`any_healthy` — хоть один отвечает, `quorum(K)` — не меньше K, `all_healthy` — все; частичный ответ — degraded."""
    CN = _cn()
    any_, q2, all_ = ({"aggregate": a, "quorum": k} for a, k in (("any_healthy", 1), ("quorum", 2), ("all_healthy", 1)))
    assert [CN.level_of(any_, h, 3) for h in range(4)] == [0, 1, 1, 2], "any_healthy"
    assert [CN.level_of(q2, h, 3) for h in range(4)] == [0, 0, 1, 2], "quorum(2) из трёх"
    assert [CN.level_of(all_, h, 3) for h in range(4)] == [0, 0, 0, 2], "all_healthy"
    assert CN.level_of({"aggregate": "quorum", "quorum": 5}, 2, 2) == 2, "кворум больше числа probe должен считаться по числу probe"
    assert CN.level_of(any_, 0, 0) == 0, "профиль без probe не может быть доступным"


@test
def test_connectivity_antiflap_holds_state_between_checks(_t):
    """Состояние меняют только N неудач или M успехов подряд; мигание между ними состояния не трогает.

    `unknown` (первого раунда не было или данные старше трёх интервалов) снимает первый же раунд.
    """
    CN = _cn()
    rule = {"aggregate": "any_healthy", "quorum": 1, "fail_after": 3, "ok_after": 2}
    state = None
    t = 1_800_000_000.0

    def step(level):
        nonlocal state, t
        t += 60
        state, prev = CN.advance(state, level, rule, t, 60, "sig")
        return state["state"], prev

    assert step(2) == ("online", "unknown"), "первый раунд не снял unknown сразу"
    assert step(0)[0] == "online" and step(0)[0] == "online", "две неудачи из трёх уже уронили профиль"
    assert step(2)[0] == "online", "успех между неудачами не сбросил счёт"
    for _ in range(2):
        assert step(0)[0] == "online", "мигание уронило профиль"
    assert step(0) == ("offline", "online"), "три неудачи подряд не уронили профиль"
    assert step(2)[0] == "offline", "один успех вернул профиль"
    assert step(0)[0] == "offline" and step(2)[0] == "offline", "успех, прерванный неудачей, вернул профиль"
    assert step(2) == ("online", "offline"), "два успеха подряд не вернули профиль"
    # частичный ответ: degraded держится так же
    for _ in range(2):
        assert step(1)[0] == "online", "degraded засветился после одного-двух раундов"
    assert step(1) == ("degraded", "online"), "три частичных раунда подряд не дали degraded"
    assert step(2)[0] == "degraded", "один полный раунд вернул online"
    assert step(2) == ("online", "degraded"), "два полных раунда не вернули online"
    # данные старше трёх интервалов (и запаса на ход проверки) — состояние неизвестно, первый раунд решает сам
    t += 60 * 3 + CN.STALE_GRACE + 60
    state, prev = CN.advance(state, 0, rule, t, 60, "sig")
    assert (state["state"], prev) == ("offline", "unknown"), "устаревшее состояние не снято первым раундом"
    # другая подпись профиля (правка настройки) — тоже «неизвестно»
    state2, prev2 = CN.advance(state, 2, rule, t + 60, 60, "other")
    assert (state2["state"], prev2) == ("online", "unknown"), "правка профиля не сбросила прежние оценки"
    edge = 60 * 3 + CN.STALE_GRACE
    assert CN.profile_state(state, t + edge, 60) == "offline" and CN.profile_state(state, t + edge + 1, 60) == "unknown", \
        "граница «три интервала» проведена не там"


# ------------------------------------------------------------------ настоящие probe на 127.0.0.1

@test
def test_connectivity_tcp_and_http_probes_against_local_stubs(_t):
    """TCP и HTTP проверяются настоящим соединением: порт открыт/закрыт, статус, тело, перенаправление, срок."""
    CN = _cn()
    ok, redirect, down, slow = _Stub(), _Stub(302, b"", location="http://127.0.0.1:1/"), _Stub(503, b"down"), _Stub(delay=3)
    try:
        t = 2
        assert CN._check_tcp({"host": "127.0.0.1", "port": ok.port}, t)["ok"], "открытый порт не отвечает"
        res = CN._check_tcp({"host": "127.0.0.1", "port": _free_port()}, 6)         # Windows отказывает закрытому порту ~2 с
        assert not res["ok"] and res["code"] == "refused", f"закрытый порт: {res}"
        http = lambda srv, **kw: CN._check_http({"url": f"http://127.0.0.1:{srv.port}/", **kw}, t)  # noqa: E731
        assert http(ok)["ok"], "200 не принят по умолчанию (2xx, 3xx)"
        assert http(redirect)["ok"] and redirect.hits == 1, "перенаправление принято за ответ, но по нему сходили дальше"
        res = http(down)
        assert not res["ok"] and res["code"] == "status" and res["detail"] == "HTTP 503", f"503: {res}"
        assert http(down, expect_status=[503])["ok"], "ожидаемый 503 не принят"
        assert http(down, expect_status=["lt500"])["ok"] is False and http(ok, expect_status=["lt500"])["ok"], "lt500"
        assert http(ok, expect_body="aurora")["ok"], "ожидаемый текст найден, но проверка не пройдена"
        res = http(ok, expect_body="нет такого текста")
        assert not res["ok"] and res["code"] == "body", f"нет ожидаемого текста: {res}"
        res = CN._check_http({"url": f"http://127.0.0.1:{slow.port}/"}, 1)
        assert not res["ok"] and res["code"] == "timeout", f"медленный сервер: {res}"
        assert CN.status_matches(204, ["2xx"]) and CN.status_matches(401, [401, "5xx"]) and not CN.status_matches(404, ["2xx", 401]), \
            "status_matches"
    finally:
        for s in (ok, redirect, down, slow):
            s.close()


@test
def test_connectivity_icmp_goes_through_system_ping_without_privileges(_t):
    """ICMP — системный `ping` подпроцессом, одним пакетом: ключи по ОС; отсутствие ping — понятный код."""
    CN = _cn()
    assert CN.icmp_argv("example.com", 2, "win32") == ["ping", "-n", "1", "-w", "2000", "example.com"]
    assert CN.icmp_argv("example.com", 2, "darwin") == ["ping", "-c", "1", "-W", "2000", "example.com"]
    assert CN.icmp_argv("example.com", 2.5, "linux") == ["ping", "-c", "1", "-W", "3", "example.com"]
    calls = []

    class Fake:
        TimeoutExpired = subprocess.TimeoutExpired

        def __init__(self, rc=0, out="", exc=None):
            self.rc, self.out, self.exc = rc, out, exc

        def run(self, argv, **kw):
            calls.append((argv, kw))
            if self.exc:
                raise self.exc
            return subprocess.CompletedProcess(argv, self.rc, self.out, "")

    real = CN.subprocess
    try:
        probe = {"host": "example.com"}
        CN.subprocess = Fake(0)
        assert CN._check_icmp(probe, 2)["ok"], "ping с кодом 0 не принят"
        assert calls[0][1].get("shell") is not True and isinstance(calls[0][0], list), "ping запущен через оболочку"
        CN.subprocess = Fake(1, "Request timeout for icmp_seq 0")
        assert CN._check_icmp(probe, 2)["code"] == "timeout", "молчание ping не названо таймаутом"
        CN.subprocess = Fake(2, "ping: cannot resolve example.invalid: Unknown host")
        assert CN._check_icmp(probe, 2)["code"] == "dns", "неизвестное имя не названо dns"
        CN.subprocess = Fake(exc=FileNotFoundError())
        assert CN._check_icmp(probe, 2)["code"] == "no_ping", "нет ping в системе — код no_ping"
    finally:
        CN.subprocess = real


# ------------------------------------------------------------------ сервис, состояние, события

def _service(tmp: Path, cfg: dict, results: dict, clock=None, providers=None):
    """Сервис с поддельными проверками: `results` — {probe id: bool}; считает обращения."""
    CN = _cn()
    kit = _kit(tmp)
    assert CN.save(kit, cfg)["ok"], "настройка теста не записалась"
    seen: list = []

    def checker(p, timeout):
        seen.append(p["id"])
        return {"ok": bool(results.get(p["id"])), "code": "ok" if results.get(p["id"]) else "timeout", "ms": 3, "detail": ""}

    svc = CN.AvailabilityService(kit, clock=clock or _Clock(), checkers={"tcp": checker, "http": checker, "icmp": checker},
                                 providers_fn=(lambda: providers or []), state_file=str(tmp / "state.json"))
    return svc, seen, results


@test
def test_connectivity_global_states_tell_no_vpn_from_no_internet_from_provider(tmp: Path):
    """`offline` — только нет интернета; интернет есть, а все нужные VPN лежат — `no_vpn`; часть — `partial`.

    Глобальное состояние считается по ТРЕБУЕМЫМ зависимостям, а не по всем описанным профилям.
    """
    CN = _cn()
    cfg = _cfg(profiles=[{"name": "VPN-1"}, {"name": "VPN-2"}, {"name": "VPN-3"}],
               probes=[_tcp("VPN-1", 1), _tcp("VPN-2", 2), _tcp("VPN-3", 3), _tcp("internet", 9)])
    clock = _Clock()
    svc, _seen, res = _service(tmp, cfg, {"p1": True, "p2": True, "p3": True, "p4": True}, clock)
    req = lambda *names: {"profiles": list(names), "providers": [], "internet": True}  # noqa: E731

    def state(**flags):
        res.update(flags)
        for _ in range(3):                          # три раунда: антидребезг пройден в любую сторону
            clock.tick(61)
            svc.check()
        return svc.snapshot(req("VPN-1", "VPN-2"))["state"]

    assert state() == "online", "всё отвечает, а состояние не online"
    assert state(p1=False) == "partial", "упал один из двух нужных профилей — ждали partial"
    assert state(p2=False) == "no_vpn", "интернет есть, оба нужных профиля лежат — ждали no_vpn"
    assert state(p4=False) == "offline", "интернета нет — ждали offline (и он главнее VPN)"
    snap = svc.snapshot(req("VPN-1", "VPN-2"))
    assert any(r["code"] == "internet_offline" for r in snap["reasons"]), "причина «нет интернета» не названа"
    assert state(p4=True, p1=True, p2=True) == "online", "связь вернулась, а состояние не online"
    assert svc.snapshot(req("VPN-3"))["state"] == "online", "профиль, который не нужен делу, влияет на состояние"
    res.update(p3=False)
    for _ in range(3):
        clock.tick(61)
        svc.check()
    assert svc.snapshot(req("VPN-1"))["state"] == "online", "лежащий профиль VPN-3, на который никто не ссылается, испортил состояние"
    assert svc.snapshot(req("VPN-3"))["state"] == "no_vpn", "единственный нужный профиль лежит — ждали no_vpn"
    # degraded профиля с кворумом даёт degraded глобально
    assert CN.global_state({"A": "degraded"}, "online", True, ["A"], 0) == "degraded", "degraded профиля не дошёл до глобального"
    assert CN.global_state({"A": "online"}, "online", True, ["A"], 1) == "partial", "недоступный провайдер не дал partial"
    assert CN.global_state({"A": "unknown"}, "online", True, ["A"], 0) == "unknown", "неизвестный нужный профиль"


@test
def test_connectivity_unknown_internet_is_not_offline_and_nothing_hits_foreign_hosts(tmp: Path):
    """Нет ни одного probe у `internet` — интернет «не известен» и НЕ «нет интернета»; кит сам никуда не ходит."""
    CN = _cn()
    kit = _kit(tmp)
    assert CN.load(kit)["probes"] == [], "в пустой настройке есть probe по умолчанию — кит пошёл бы на чужие адреса"
    svc, seen, _res = _service(tmp, _cfg(profiles=[{"name": "VPN-1"}], probes=[_tcp("VPN-1", 1)]), {"p1": False}, _Clock())
    snap = svc.check()
    assert snap["internet"]["state"] == "unknown" and snap["internet"]["monitored"] is False, "интернет без probe не unknown"
    snap = svc.snapshot({"profiles": ["VPN-1"], "providers": [], "internet": True})
    assert snap["state"] != "offline", "интернет без probe принят за offline"
    assert seen == ["p1"], f"проверен кто-то кроме названных владельцем probe: {seen}"
    # выключенная проверка: ни обращений, ни записи состояния
    off = _cfg(enabled=False, profiles=[{"name": "VPN-1"}], probes=[_tcp("VPN-1", 1)])
    (tmp / "off").mkdir()
    svc2, seen2, _ = _service(tmp / "off", off, {"p1": True}, _Clock())
    svc2.check()
    svc2.ensure()
    svc2.tick()
    assert seen2 == [] and not (tmp / "off" / "state.json").exists(), "выключенная проверка ходила в сеть или писала состояние"
    assert svc2.snapshot()["enabled"] is False and svc2.snapshot()["state"] == "unknown", "выключенная проверка выдаёт состояние"


@test
def test_connectivity_providers_are_separate_from_vpn_state(tmp: Path):
    """Доступность VPN ≠ доступность провайдера: жив VPN, не прошла проверка ручки — провайдер недоступен.

    Провайдер без VPN в облаке зависит от интернета, локальный (127.0.0.1) — ни от чего.
    """
    CN = _cn()
    prov = [{"id": "gw", "name": "GW", "url": "https://llm.example.com/v1", "key": "k", "profile": "VPN-1",
             "check": "reach", "path": "/models", "local": False},
            {"id": "cloud", "name": "Cloud", "url": "https://api.example.org/v1", "key": "", "profile": "",
             "check": "off", "path": "/models", "local": False},
            {"id": "home", "name": "Home", "url": "http://127.0.0.1:8080/v1", "key": "", "profile": "",
             "check": "off", "path": "/models", "local": True}]
    cfg = _cfg(probes=[_tcp("VPN-1", 1), _tcp("internet", 9)])
    clock = _Clock()
    svc, seen, res = _service(tmp, cfg, {"p1": True, "p2": True, "provider:gw": True}, clock, prov)

    def rows():
        for _ in range(3):
            clock.tick(61)
            svc.check()
        return {r["id"]: r for r in svc.snapshot()["providers"]}

    r = rows()
    assert all(x["state"] == "online" for x in r.values()), f"всё отвечает, а провайдеры не online: {r}"
    res["provider:gw"] = False
    r = rows()
    assert r["gw"]["state"] == "offline" and r["gw"]["why"] == "provider_check", f"VPN жив, ручка нет: {r['gw']}"
    assert svc.snapshot()["profiles"][0]["state"] == "online", "упавшая ручка провайдера уронила сам VPN-профиль"
    res.update({"provider:gw": True, "p1": False})
    r = rows()
    assert r["gw"]["state"] == "offline" and r["gw"]["why"] == "profile_offline", f"VPN лежит: {r['gw']}"
    res["p2"] = False                                 # интернета нет
    r = rows()
    assert r["cloud"]["state"] == "offline" and r["cloud"]["why"] == "internet_offline", f"облако без интернета: {r['cloud']}"
    assert r["home"]["state"] == "online", "локальный провайдер зависит от интернета"
    # провайдер привязан к профилю, которого нет, — «не известно», но не блокировка
    prov2 = [dict(prov[0], profile="VPN-404", check="off")]
    (tmp / "p2").mkdir()
    svc2, _s, _r = _service(tmp / "p2", cfg, {"p1": True}, _Clock(), prov2)
    row = svc2.snapshot()["providers"][0]
    assert row["state"] == "unknown" and row["why"] == "no_profile", f"привязка к несуществующему профилю: {row}"
    # ручка провайдера: ключ уходит только по защищённому каналу и только на хост провайдера
    probe = CN.provider_probe(dict(prov[0], check="auth", key="secret-key-1"))
    assert probe and probe["url"].startswith("https://llm.example.com/") and probe["_headers"]["Authorization"] == "Bearer secret-key-1", \
        "проверка с ключом собрана не на хост провайдера"
    assert CN.provider_probe(dict(prov[0], url="http://llm.example.com/v1", check="auth", key="secret-key-1")) is None, \
        "ключ ушёл бы на хост по открытому http"
    assert CN.provider_probe(dict(prov[0], check="off")) is None, "проверка ручки выключена, а probe собран"
    stored = (tmp / "state.json").read_text(encoding="utf-8")
    assert "secret-key-1" not in stored and "Authorization" not in stored, "ключ провайдера попал в файл состояния"


@test
def test_connectivity_prelaunch_check_uses_cache_runs_in_parallel_and_has_a_budget(tmp: Path):
    """Предзапуск: свежее состояние берётся из кэша, устаревшее перепроверяется, probe идут разом, срок — ≤ бюджета."""
    CN = _cn()
    cfg = _cfg(settings={"interval": 60}, probes=[_tcp("VPN-1", 1), _tcp("VPN-1", 2), _tcp("VPN-1", 3)])
    clock = _Clock()
    svc, seen, res = _service(tmp, cfg, {"p1": True, "p2": True, "p3": True}, clock)
    svc.ensure()
    assert sorted(seen) == ["p1", "p2", "p3"], f"первая проверка не прошла все probe: {seen}"
    del seen[:]
    clock.tick(30)
    svc.ensure()
    assert seen == [], "свежее состояние (моложе интервала) перепроверено — кэш не работает"
    clock.tick(40)                                     # 70 с > интервала
    svc.ensure()
    assert sorted(seen) == ["p1", "p2", "p3"], "устаревшее состояние не перепроверено на месте"
    # параллельность и бюджет: три медленных probe укладываются в один срок, а не в три
    started = time.monotonic()
    slow = CN.run_probes([{"id": f"s{i}", "type": "tcp", "host": "x", "port": 1} for i in range(3)], CN.empty(), 3.0,
                         checkers={"tcp": lambda p, t: (time.sleep(0.6), {"ok": True, "code": "ok", "ms": 600, "detail": ""})[1]})
    took = time.monotonic() - started
    assert all(r["ok"] for r in slow.values()) and took < 1.6, f"probe идут по очереди: {took:.2f} с"
    started = time.monotonic()
    late = CN.run_probes([{"id": "hang", "type": "tcp", "host": "x", "port": 1}], CN.empty(), 0.3,
                         checkers={"tcp": lambda p, t: (time.sleep(1.5), {"ok": True, "code": "ok", "ms": 0, "detail": ""})[1]})
    assert not late["hang"]["ok"] and late["hang"]["code"] == "late" and time.monotonic() - started < 1.0, \
        f"зависший probe держит предзапуск дольше бюджета: {late}"


@test
def test_connectivity_state_file_is_shared_and_events_reach_subscribers(tmp: Path):
    """Состояние лежит в одном файле на машину: второй процесс видит его; смены состояния — в журнале и у подписчиков."""
    CN = _cn()
    cfg = _cfg(settings={"fail_after": 2, "ok_after": 1}, probes=[_tcp("VPN-1", 1)])
    clock = _Clock()
    svc, _seen, res = _service(tmp, cfg, {"p1": True}, clock)
    got = []
    svc.subscribe(got.append)
    svc.check()
    for _ in range(2):
        res["p1"] = False
        clock.tick(61)
        svc.check()
    res["p1"] = True
    clock.tick(61)
    svc.check()
    kinds = [(e["name"], e["from"], e["to"]) for e in got]
    assert kinds == [("VPN-1", "unknown", "online"), ("VPN-1", "online", "offline"), ("VPN-1", "offline", "online")], \
        f"события смены состояния: {kinds}"
    # второй сервис (другой процесс) читает то же состояние и журнал
    other = CN.AvailabilityService(svc.kit, clock=clock, providers_fn=lambda: [], state_file=svc.state_file)
    snap = other.snapshot()
    assert snap["profiles"][0]["state"] == "online" and len(snap["events"]) == 3, f"второй сервис не видит состояния: {snap['events']}"
    svc.unsubscribe(got.append)
    stored = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
    assert stored["profiles"]["VPN-1"]["state"] == "online" and "p1" in stored["probes"], "файл состояния без профиля или probe"
    assert not list(tmp.glob("state.json.*tmp")) and not (tmp / "state.json.lock").exists(), "после записи остались временный файл или замок"
    # сломанный файл состояния — подсказка, а не отказ
    (tmp / "state.json").write_text("garbage", encoding="utf-8")
    assert CN.read_state(tmp / "state.json")["profiles"] == {}, "битый файл состояния уронил чтение"


@test
def test_connectivity_snapshot_does_not_leak_addresses_or_query_strings(tmp: Path):
    """В сводке и выводе команды адрес probe — без параметров запроса (там бывают токены); по умолчанию только тип."""
    CN = _cn()
    probe = {"id": "p1", "profile": "VPN-1", "type": "http", "url": "https://svc.example.com/health?token=abc123&x=1"}
    assert CN.probe_target(probe) == "https://svc.example.com/health", f"параметры запроса остались в адресе: {CN.probe_target(probe)}"
    cfg = _cfg(probes=[{k: v for k, v in probe.items() if k != "id"}])
    svc, _s, _r = _service(tmp, cfg, {"p1": True}, _Clock())
    svc.check()
    snap = svc.snapshot({"profiles": ["VPN-1"], "providers": [], "internet": False})
    assert "abc123" not in json.dumps(snap), "токен из параметров запроса попал в сводку"
    plain = CN.render(snap)
    assert "svc.example.com" not in plain and "http" in plain, "обычный вывод показал адрес (нужен только с --targets)"
    assert "svc.example.com/health" in CN.render(snap, targets=True) and "abc123" not in CN.render(snap, targets=True), \
        "вывод с адресами потерял путь или показал токен"
    shown = CN.without_targets(snap)
    assert all("target" not in p for r in [shown["internet"]] + shown["profiles"] for p in r["probes"]), \
        "--json без --targets оставил адреса probe"
    assert snap["profiles"][0]["probes"][0]["target"], "без_адресов изменило исходную сводку"


# ------------------------------------------------------------------ команды и доктор

@test
def test_net_commands_are_registered_read_only_and_shipped(_t):
    """`net:status` и `net:probe` — в реестре, их скрипт едет в проекты; команды записи настроек нет (всё — в панели)."""
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    rows = [ln for ln in reg.splitlines() if ln.startswith("net |")]
    cmds = sorted(r.split("|")[1].strip() for r in rows)
    assert cmds == ["net:probe", "net:status"], f"в наборе net: должны быть только чтение и проверка: {cmds}"
    assert all("connectivity.py" in r for r in rows), "команды net: ведут не в connectivity.py"
    import re
    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert re.search(r"^scripts/connectivity\.py\s+=>\s+\.aurora/scripts/connectivity\.py$", man, re.M), \
        "connectivity.py не едет в проекты"
    doc = (KIT / "scripts" / "connectivity.py").read_text(encoding="utf-8")
    assert "Панель: `net:status`, `net:probe`" in doc, "в докстринге нет строки «Панель:» с командами"
    src = doc
    assert "--set" not in src and "net:set" not in reg, "появился путь записи настройки мимо панели (CLI-флаг или команда)"


@test
def test_net_status_and_probe_cli_report_disabled_state_and_change_nothing(tmp: Path):
    """CLI без включённой проверки: `--status` говорит «выключена», `--probe` ничего не делает и не пишет файлов."""
    set_home(tmp / "home")
    for flag in ("--status", "--probe"):
        cp = run("connectivity.py", flag, cwd=tmp)
        assert cp.returncode == 0, f"{flag}: код {cp.returncode}\n{cp.stdout}\n{cp.stderr}"
        assert "выключена" in cp.stdout, f"{flag}: не сказано, что проверка выключена:\n{cp.stdout}"
    assert not (tmp / "home" / ".aurora" / "connectivity-state.json").exists(), "CLI записал состояние у выключенной проверки"
    cp = run("connectivity.py", "--status", "--json", cwd=tmp)
    data = json.loads(cp.stdout)
    assert data["enabled"] is False and data["state"] == "unknown", f"--json: {data}"


@test
def test_kit_doctor_line_about_connectivity_only_when_enabled(tmp: Path):
    """`kit:doctor` называет состояние связи только у включённой проверки и сети при этом не трогает."""
    CN = _cn()
    from harness import make_project
    proj = make_project(tmp)
    home = tmp / "home"
    home.mkdir()
    restore = set_home(home)
    try:
        cp = subprocess.run([sys.executable, str(proj / ".aurora" / "scripts" / "aurora_doctor.py")], cwd=str(proj),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert "связь:" not in cp.stdout, f"доктор шумит про связь при выключенной проверке:\n{cp.stdout}"
        kit = _kit(tmp)
        assert CN.save(kit, _cfg(probes=[_tcp("internet", _free_port())]))["ok"], "настройка не записалась"
        # состояние в файле + кит по указателю проекта: доктор из копии движка читает сохранённое
        (proj / ".aurora" / "kit_path.txt").write_text(str(kit), encoding="utf-8")
        svc = CN.AvailabilityService(kit, providers_fn=lambda: [])
        svc.check(force=True, budget=None)
        cp = subprocess.run([sys.executable, str(proj / ".aurora" / "scripts" / "aurora_doctor.py")], cwd=str(proj),
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert "связь:" in cp.stdout and "net:status" in cp.stdout, f"доктор не назвал состояние связи:\n{cp.stdout}\n{cp.stderr}"
    finally:
        restore()


# ------------------------------------------------------------------ замечания независимого просмотра фазы A

@test
def test_connectivity_provider_key_never_reaches_state_errors_or_headers_of_bad_keys(tmp: Path):
    """Ключ провайдера не попадает в текст ошибки, сводку и файл состояния — даже когда библиотека называет его в ошибке.

    Ключ с переводом строки библиотека HTTP отвергает, называя заголовок целиком; движок не пересказывает текст чужих
    исключений, вычищает значения заголовков и вовсе не собирает проверку с негодным ключом.
    """
    CN = _cn()
    stub = _Stub()
    try:
        secret = "sk-test-secret-123"
        res = CN._check_http({"url": f"http://127.0.0.1:{stub.port}/", "_headers": {"Authorization": "Bearer " + secret + "\n"}}, 2)
        assert res["ok"] is False and secret not in json.dumps(res), f"ключ в результате проверки: {res}"
        res = CN._check_http({"url": f"http://127.0.0.1:{stub.port}/", "_headers": {"Authorization": "Bearer " + secret}}, 2)
        assert res["ok"] is True, f"годный ключ не принят: {res}"
        prov = {"id": "gw", "name": "GW", "url": "https://llm.example.com/v1", "profile": "", "check": "auth", "path": "/models",
                "local": False}
        for bad in (secret[:5] + "\n" + secret[5:], secret + "\x00x", "ключ-не-latin1"):
            assert CN.provider_probe(dict(prov, key=bad)) is None, f"проверка собрана с негодным ключом {bad!r}"
        probe = CN.provider_probe(dict(prov, key="  " + secret + "  "))
        assert probe["_headers"]["Authorization"] == "Bearer " + secret, "пробелы вокруг ключа не срезаны"
        # через раунд — в файл состояния
        state = CN.read_state(tmp / "none.json")
        cfg = CN.clean(_cfg(probes=[_tcp("VPN-1", 1)]))[0]
        CN.apply_round(state, cfg, "provider:gw", [{"id": "provider:gw", "type": "http", "url": probe["url"]}],
                       {"provider:gw": res}, 1_800_000_000.0)
        assert secret not in json.dumps(state), "ключ попал в состояние"
    finally:
        stub.close()
    # нечитаемая проверка — не «неизвестно навсегда», а названная причина
    svc, _seen, _res = _service(tmp, _cfg(profiles=[{"name": "VPN-1"}], probes=[_tcp("VPN-1", 1)]), {"p1": True}, _Clock(),
                                [dict(prov, key="плохой-ключ", profile="VPN-1")])
    svc.check()
    snap = svc.snapshot()
    row = snap["providers"][0]
    assert row["why"] == "check_unusable" and any(r["code"] == "check_unusable" for r in snap["reasons"]), f"{row} {snap['reasons']}"
    assert snap["state"] == "online", "непроверяемая ручка держит глобальное состояние в unknown"


@test
def test_connectivity_burst_of_checks_is_one_round_and_slow_rounds_do_not_reset_anti_flap(tmp: Path):
    """Три нажатия «Проверить сейчас» за секунду — одна оценка, а не три неудачи подряд; раунд, идущий дольше тика,
    не отправляет профиль в «неизвестно» (запас на ход проверки сверх трёх интервалов)."""
    CN = _cn()
    cfg = _cfg(settings={"interval": 60, "fail_after": 3, "ok_after": 1}, probes=[_tcp("VPN-1", 1)])
    clock = _Clock()
    svc, seen, res = _service(tmp, cfg, {"p1": True}, clock)
    svc.check()
    res["p1"] = False
    for _ in range(5):                    # пять проверок подряд за пять секунд
        clock.tick(1)
        svc.check()
    snap = svc.snapshot({"profiles": ["VPN-1"], "providers": [], "internet": False})
    assert snap["profiles"][0]["state"] == "online", "серия быстрых проверок уронила профиль: каждая считалась отдельной неудачей"
    for _ in range(3):                    # а проверки с шагом интервала — настоящие раунды
        clock.tick(61)
        svc.check()
    assert svc.snapshot()["profiles"][0]["state"] == "offline", "три раунда с шагом интервала не уронили профиль"
    # запас: раунд, пришедший на 3 интервала + 20 с позже прежнего, состояние не обнуляет
    cfg2 = _cfg(settings={"interval": 10}, probes=[_tcp("VPN-1", 1)])
    (tmp / "g").mkdir()
    svc2, _s, res2 = _service(tmp / "g", cfg2, {"p1": True}, _Clock())
    clk = svc2.clock
    svc2.check()
    clk.tick(3 * 10 + 20)
    assert svc2.snapshot()["profiles"][0]["state"] == "online", "состояние стало unknown раньше трёх интервалов с запасом"
    clk.tick(CN.STALE_GRACE)
    assert svc2.snapshot()["profiles"][0]["state"] == "unknown", "после запаса состояние не устарело"


@test
def test_connectivity_probe_that_missed_the_common_budget_writes_no_round(tmp: Path):
    """Probe, не уложившаяся в общий срок (зависшее разрешение имени), — «не знаем», а не «лежит»: раунд профиля не пишется."""
    CN = _cn()
    cfg = _cfg(probes=[_tcp("VPN-1", 1), _tcp("VPN-1", 2)])
    clock = _Clock()
    svc, _seen, res = _service(tmp, cfg, {"p1": True, "p2": True}, clock)
    svc.check()
    before = json.loads((tmp / "state.json").read_text(encoding="utf-8"))["profiles"]["VPN-1"]
    hang = {"tcp": lambda p, t: (time.sleep(1.2), {"ok": True, "code": "ok", "ms": 0, "detail": ""})[1] if p["id"] == "p2"
            else {"ok": False, "code": "timeout", "ms": 1, "detail": ""}}
    svc.checkers = hang
    clock.tick(61)
    svc.ensure(budget=0.4)
    after = json.loads((tmp / "state.json").read_text(encoding="utf-8"))["profiles"]["VPN-1"]
    assert after["hist"] == before["hist"] and after["round_at"] == before["round_at"], "неполный раунд записан в историю"
    got = CN.run_probes([{"id": "x", "type": "tcp", "host": "h", "port": 1}], CN.empty(), 0.3,
                        checkers={"tcp": lambda p, t: (time.sleep(1), {"ok": True, "code": "ok", "ms": 0, "detail": ""})[1]})
    assert got["x"]["late"] is True and got["x"]["code"] == "late", got
    # срок probe урезается до 80 % бюджета: молчащий адрес успевает ответить «нет ответа» до конца общего срока
    seen = []
    CN.run_probes([{"id": "y", "type": "tcp", "host": "h", "port": 1}], CN.empty(), 2.0,
                  checkers={"tcp": lambda p, t: (seen.append(t), {"ok": False, "code": "timeout", "ms": 0, "detail": ""})[1]})
    assert seen and seen[0] <= 1.6 + 1e-9, f"срок probe не урезан под бюджет: {seen}"


@test
def test_connectivity_config_with_nan_inf_and_server_only_keys_is_refused_cleanly(tmp: Path):
    """`NaN` и `Infinity` в файле не роняют ни чтение, ни запись; поля ответа сервера и служебные поля probe не пишутся."""
    CN = _cn()
    kit = _kit(tmp)
    CN.config_path(kit).write_text('{"enabled": true, "settings": {"interval": NaN, "timeout": Infinity}, "profiles": [], "probes": []}',
                                   encoding="utf-8")
    cfg = CN.load(kit)
    assert cfg["settings"]["interval"] == CN.DEFAULTS["interval"] and cfg["settings"]["timeout"] == CN.DEFAULTS["timeout"], cfg["settings"]
    res = CN.save(kit, {"enabled": True, "settings": {"interval": float("nan")}, "profiles": [], "probes": []}, None)
    assert not res["ok"] and res["code"] == "bad_interval", f"NaN принят или упал: {res}"
    assert not CN.save(kit, {"enabled": True, "settings": {"timeout": float("inf")}, "profiles": [], "probes": []})["ok"], "Infinity принят"
    saved = CN.save(kit, {"enabled": True, "notes": ["n"], "error": "e", "no_kit": True, "profiles": [{"name": "VPN-1"}],
                          "probes": [dict(_tcp("VPN-1", 443), _headers={"Authorization": "Bearer x"})]}, None)
    assert saved["ok"], saved
    on_disk = json.loads(CN.config_path(kit).read_text(encoding="utf-8"))
    assert not {"notes", "error", "no_kit"} & set(on_disk), f"поля ответа сервера записаны в файл: {sorted(on_disk)}"
    assert "_headers" not in on_disk["probes"][0], "служебное поле probe записано из присланного"
    probe = CN.try_probe({"type": "http", "url": "http://127.0.0.1:1/", "_headers": {"Authorization": "x"}})
    assert probe["ok"] is False, "черновик с лишним полем"
    assert CN.clean({"probes": [], "profiles": [{"name": "VPN-1"}], "x": 1})[0]["x"] == 1, "неизвестное поле верхнего уровня потеряно"


@test
def test_connectivity_icmp_windows_needs_ttl_and_posix_exit_two_is_not_silence(_t):
    """Windows отвечает кодом 0 и на «узел недоступен» от шлюза — настоящий ответ несёт TTL; на POSIX код 2 — не «нет ответа»."""
    CN = _cn()

    class Fake:
        TimeoutExpired = subprocess.TimeoutExpired

        def __init__(self, rc, out):
            self.rc, self.out = rc, out

        def run(self, argv, **kw):
            return subprocess.CompletedProcess(argv, self.rc, self.out, "")

    real, plat = CN.subprocess, CN.sys.platform
    try:
        CN.sys.platform = "win32"
        CN.subprocess = Fake(0, "Reply from 10.0.0.1: Destination host unreachable.")
        assert CN._check_icmp({"host": "example.com"}, 2)["ok"] is False, "ответ шлюза «недоступен» принят за успех"
        CN.subprocess = Fake(0, "Reply from 93.184.216.34: bytes=32 time=12ms TTL=56")
        assert CN._check_icmp({"host": "example.com"}, 2)["ok"] is True, "настоящий ответ с TTL не принят"
        CN.sys.platform = "linux"
        CN.subprocess = Fake(2, "ping: socket: Operation not permitted")
        res = CN._check_icmp({"host": "example.com"}, 2)
        assert res["ok"] is False and res["code"] == "other", f"код 2 без разрешения принят за тишину: {res}"
        CN.subprocess = Fake(1, "")
        assert CN._check_icmp({"host": "example.com"}, 2)["code"] == "timeout", "код 1 — это отсутствие ответа"
    finally:
        CN.subprocess, CN.sys.platform = real, plat


@test
def test_connectivity_force_check_never_sends_provider_keys_while_the_feature_is_off(tmp: Path):
    """«Проверить сейчас» у выключенной проверки идёт только по адресам владельца: ручки провайдеров (с ключом) не трогаются."""
    CN = _cn()
    prov = [{"id": "gw", "name": "GW", "url": "https://llm.example.com/v1", "key": "k", "profile": "", "check": "auth",
             "path": "/models", "local": False}]
    off = _cfg(enabled=False, profiles=[{"name": "VPN-1"}], probes=[_tcp("VPN-1", 1)])
    svc, seen, _res = _service(tmp, off, {"p1": True, "provider:gw": True}, _Clock(), prov)
    svc.check(force=True)
    assert seen == ["p1"], f"выключенная проверка ходила к провайдеру или не проверила адрес владельца: {seen}"
    (tmp / "on").mkdir()
    svc2, seen2, _r = _service(tmp / "on", dict(off, enabled=True), {"p1": True, "provider:gw": True}, _Clock(), prov)
    svc2.check()
    assert sorted(seen2) == ["p1", "provider:gw"], f"включённая проверка не ходит к ручке провайдера: {seen2}"


@test
def test_connectivity_hosts_refuse_brackets_and_global_no_vpn_needs_a_known_internet(_t):
    """IPv6 пишется без скобок; `no_vpn` — утверждение «интернет есть»: при неизвестном интернете это просто unknown."""
    CN = _cn()
    assert CN.valid_host("::1") and CN.valid_host("fd00::5") and CN.valid_host("10.0.0.1") and not CN.valid_host("[::1]"), "IPv6 в скобках"
    assert CN.global_state({"A": "offline"}, "unknown", True, ["A"], 0) == "unknown", "no_vpn без подтверждённого интернета"
    assert CN.global_state({"A": "offline"}, "online", True, ["A"], 0) == "no_vpn", "no_vpn при живом интернете"
    assert CN.global_state({"A": "offline"}, "unknown", False, ["A"], 0) == "no_vpn", "интернет не проверяется — no_vpn как раньше"
    assert CN.read_state(Path("/nonexistent-aurora-state.json"))["profiles"] == {}, "чтение отсутствующего файла"


@test
def test_connectivity_disabled_feature_does_zero_network_io(tmp: Path):
    """Выключенная проверка не открывает ни соединения, ни подпроцесса `ping`, ни запроса: подменённые проверки не зовутся."""
    CN = _cn()
    calls = []
    boom = lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError("сеть тронута"))  # noqa: E731
    saved = dict(CN.CHECKERS)
    try:
        for k in CN.CHECKERS:
            CN.CHECKERS[k] = boom
        off = _cfg(enabled=False, profiles=[{"name": "VPN-1"}], probes=[_tcp("VPN-1", 1), {"profile": "internet", "type": "icmp", "host": "example.com"}])
        kit = _kit(tmp)
        assert CN.save(kit, off)["ok"]
        svc = CN.AvailabilityService(kit, providers_fn=lambda: [{"id": "gw", "name": "GW", "url": "https://llm.example.com/v1", "key": "k",
                                                                  "profile": "", "check": "auth", "path": "/models", "local": False}],
                                     state_file=str(tmp / "state.json"))
        svc.ensure()
        svc.tick()
        svc.check()
        svc.snapshot()
        svc.start_monitor(tick=0.05)
        time.sleep(0.3)
        svc.stop_monitor()
        assert calls == [], f"выключенная проверка вызвала проверки: {calls}"
        assert not (tmp / "state.json").exists(), "выключенная проверка записала состояние"
    finally:
        CN.CHECKERS.update(saved)
