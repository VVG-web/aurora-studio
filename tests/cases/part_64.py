"""Проверки движка Aurora, часть 64. Каркас и помощники — tests/harness.py.

Блокировка функций кита по проверке соединений (фаза C): вердикт по требованиям команды (`conn_gate`), четыре политики
(`fail_fast`, `partial`, `queue`, `partial_queue`), ожидание восстановления без предела, маршрут и расписание, отказ
синка источника с точной причиной. Проверка выключена — поведение прежнее. Сети нет: проверки подставные, часы поддельные;
кит, проект и домашняя папка — временные.
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
import time
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(KIT / "cockpit"))

from cases.part_18 import FakePanel, _task  # noqa: E402


def _g():
    return importlib.import_module("conn_gate")


def _cn():
    return importlib.import_module("connectivity")


class _Clock:
    """Поддельные часы сервиса: время идёт, только когда тест его двигает."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def _reset_gate_caches() -> None:
    """Короткие кэши решений «кого не трогать»: тест двигает связь быстрее, чем они остывают."""
    G = _g()
    G._DOWN.update(at=0.0, key=None, val={})
    G._ENSURE.update(at=0.0, key=None)
    G._NOTED.clear()


class _Net:
    """Проверка соединений на подставных проверках: профили и интернет включают и выключают, часы двигает тест.

    Сервис кладётся в общий реестр процесса, поэтому маршрут, расписание и панель видят именно его."""

    def __init__(self, tmp: Path, up: dict | None = None, policy: str = "", providers: list | None = None, enabled: bool = True):
        CN = _cn()
        self.CN = CN
        self.kit = tmp / "kit"
        (self.kit / "local").mkdir(parents=True, exist_ok=True)
        cfg = {"enabled": enabled, "settings": {"interval": 60, "fail_after": 1, "ok_after": 1},
               "profiles": [{"name": "VPN-1"}, {"name": "VPN-2"}],
               "probes": [{"id": "v1", "profile": "VPN-1", "type": "tcp", "host": "127.0.0.1", "port": 1},
                          {"id": "v2", "profile": "VPN-2", "type": "tcp", "host": "127.0.0.1", "port": 2},
                          {"id": "net1", "profile": "internet", "type": "tcp", "host": "127.0.0.1", "port": 3}]}
        if policy:
            cfg["policy"] = {"auto": policy}
        assert CN.save(self.kit, cfg)["ok"], "настройка соединений теста не записалась"
        self.up = {"v1": True, "v2": True, "net1": True}
        self.up.update(up or {})
        self.clock = _Clock()

        self.probes = 0                       # сколько раз проверки реально спрашивали (считает checker)
        self.special: dict = {}               # {id probe: готовый ответ} — для «проверка не смогла», «не успела»

        def checker(p, timeout):
            self.probes += 1
            if p["id"] in self.special:
                return dict(self.special[p["id"]])
            ok = bool(self.up.get(p["id"], True))
            return {"ok": ok, "code": "ok" if ok else "timeout", "ms": 3, "detail": ""}
        self.svc = CN.AvailabilityService(self.kit, clock=self.clock, checkers={"tcp": checker, "http": checker, "icmp": checker},
                                          providers_fn=lambda: providers or [], state_file=str(tmp / "net-state.json"))
        CN.reset_services()
        CN._SERVICES[str(self.kit)] = self.svc
        CN._SERVICES[str(CN.kit_dir() or "")] = self.svc          # процессы без явного кита (роутер модели, сторож) видят его же
        _reset_gate_caches()
        if enabled:
            self.svc.check()

    def set(self, **flags) -> None:
        """Включить или выключить проверки (v1=False — VPN-1 лёг) и дать сервису новый раунд."""
        self.up.update(flags)
        self.clock.now += 61
        self.svc.check()
        _reset_gate_caches()

    def later(self, delay: float, **flags) -> threading.Thread:
        def go():
            time.sleep(delay)
            self.set(**flags)
        th = threading.Thread(target=go, daemon=True)
        th.start()
        return th

    def close(self) -> None:
        self.CN.reset_services()


def _project(tmp: Path, sub: str = "p") -> Path:
    """Проект с двумя источниками: Confluence — за VPN-1, веб — через интернет (умолчание модуля)."""
    (tmp / sub).mkdir(exist_ok=True)
    proj = make_project(tmp / sub)
    cfg = proj / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8")
                   + "\nsources:\n  - id: Web\n    module: web\n    path: Sources/Web\n"
                     "  - id: Confluence\n    module: confluence-dc\n    path: Sources/Confluence\n"
                     'source_access:\n  "Confluence":\n    mode: "vpn"\n    vpn: "VPN-1"\n', encoding="utf-8")
    return proj


class NetPanel(FakePanel):
    """Панель без процессов, помнящая политику, с которой запущен каждый шаг, и зовущая крючок на каждом запуске."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.policies: list = []
        self.on_call = None

    def start_job(self, project, cmd, args, parent="", net_policy="", net_wait=False):
        self.policies.append(net_policy)
        if self.on_call:
            self.on_call(cmd)
        return super().start_job(project, cmd, args, parent)


def _step(cmd, flags=("--apply",)):
    return {"manual": False, "cmd": cmd, "why": "", "flags": list(flags)}


def _sc(*cmds, sid="net"):
    return {"id": sid, "title": "Связь", "when": "", "group": "база", "steps": [_step(c) for c in cmds]}


SCRIPT = {"kb:lint": [(0, ["чисто"])], "kb:repair": [(0, ["починено"])], "sync:confluence": [(0, ["скачано"])],
          "sync:web": [(0, ["скачано"])], "agent:build": [(0, ["Источников в плане: 0"])]}


def _script() -> dict:
    return {k: list(v) for k, v in SCRIPT.items()}


class _Fast:
    """Опрос ожидания связи в тесте — сотые доли секунды, а не пять."""

    def __enter__(self):
        self.rr = importlib.import_module("route_runner")
        self.saved = self.rr.NET_POLL_S
        self.rr.NET_POLL_S = 0.05
        return self.rr

    def __exit__(self, *exc):
        self.rr.NET_POLL_S = self.saved


# ------------------------------------------------------------------ вердикт и политика

@test
def test_gate_verdict_blocks_only_on_known_unavailability(tmp: Path):
    """`unknown` не блокирует; недоступный профиль источника, интернет и все провайдеры модели — блокируют с названной причиной."""
    G = _g()
    snap = {"enabled": True, "state": "partial", "internet": {"monitored": True, "state": "online"},
            "profiles": [{"name": "VPN-1", "state": "offline"}, {"name": "VPN-2", "state": "unknown"}],
            "providers": [{"id": "gw", "state": "offline", "why": "profile_offline", "profile": "VPN-1"},
                          {"id": "loc", "state": "online", "why": ""}]}
    src = lambda sid, prof, **kw: {"id": sid, "profile": prof, "internet": False, "offline_cache": False, **kw}  # noqa: E731
    v = G.evaluate({"sources": [src("Wiki", "VPN-1")], "offline_capable": False}, snap)
    assert not v["ok"] and v["blocked"] == [{"code": "profile_offline", "name": "VPN-1", "source": "Wiki"}], v
    assert G.evaluate({"sources": [src("Other", "VPN-2")], "offline_capable": False}, snap)["ok"], "unknown заблокировал дело"
    assert G.evaluate({"sources": [src("Plain", "")], "offline_capable": False}, snap)["ok"], "источник без профиля заблокирован"
    # из кэша источник доступен только команде, которая умеет без сети
    cached = src("Wiki", "VPN-1", offline_cache=True)
    assert G.evaluate({"sources": [cached], "offline_capable": True}, snap)["ok"], "команда, умеющая без сети, не прошла по кэшу"
    assert not G.evaluate({"sources": [cached], "offline_capable": False}, snap)["ok"], "кэш выручил команду, которой нужна сеть"
    # модель: блокирует, только если недоступны ВСЕ провайдеры, на которых работает кит
    assert G.evaluate({"llm": True, "providers_used": ["gw", "loc"]}, snap)["ok"], "живой локальный провайдер не спас команду"
    down = G.evaluate({"llm": True, "providers_used": ["gw"]}, snap)
    assert not down["ok"] and down["blocked"][0]["code"] == "llm_down" and down["blocked"][0]["profiles"] == ["VPN-1"], down
    assert G.evaluate({"llm": True, "providers_used": []}, snap)["ok"] is True
    # интернет: блокирует только если за ним следят (есть probe) и он лежит
    off = dict(snap, internet={"monitored": True, "state": "offline"})
    assert not G.evaluate({"internet": True}, off)["ok"] and G.evaluate({"internet": True}, snap)["ok"]
    unmon = dict(snap, internet={"monitored": False, "state": "offline"})
    assert G.evaluate({"internet": True}, unmon)["ok"], "интернет без probe заблокировал дело"
    assert G.evaluate({"internet": True, "profiles": ["VPN-1"]}, dict(snap, enabled=False))["ok"], "выключенная проверка блокирует"
    text = G.blocked_text("sync:wiki", v)
    assert "VPN-1" in text and "Wiki" in text and text.startswith("sync:wiki: нужное недоступно"), text


@test
def test_gate_policy_decides_what_to_do_and_overrides_stack(tmp: Path):
    """Четыре политики дают отказ, пропуск, ожидание, откладывание; приоритет: задание, окружение процесса, кит, умолчание."""
    import os
    G, CN = _g(), _cn()
    bad, good = {"ok": False, "blocked": [{"code": "profile_offline", "name": "VPN-1"}]}, {"ok": True, "blocked": []}
    assert [G.decide(p, bad) for p in CN.POLICIES] == ["fail", "skip", "wait", "defer"], "политика → действие"
    assert {G.decide(p, good) for p in CN.POLICIES} == {"run"}, "доступное дело остановлено"
    assert G.decide("nonsense", bad) == "defer", "неизвестная политика не откладывает"
    net = _Net(tmp, policy="queue")
    saved = os.environ.pop(G.NET_POLICY_ENV, None)
    try:
        assert G.policy_for("", net.svc) == "queue", "настройка кита не прочитана"
        assert G.policy_for("fail_fast", net.svc) == "fail_fast", "переопределение задания не главное"
        os.environ[G.NET_POLICY_ENV] = "partial"
        assert G.policy_for("", net.svc) == "partial", "окружение процесса не главнее кита"
        assert G.policy_for("queue", net.svc) == "queue", "переопределение задания слабее окружения"
        os.environ[G.NET_POLICY_ENV] = "junk"
        assert G.policy_for("", net.svc) == "queue", "негодное окружение принято"
    finally:
        os.environ.pop(G.NET_POLICY_ENV, None)
        if saved is not None:
            os.environ[G.NET_POLICY_ENV] = saved
        net.close()
    plain = _Net(tmp / "x", enabled=True)
    try:
        assert G.kit_policy(plain.svc) == CN.POLICY_DEFAULT == "partial_queue", "умолчание — сделать доступное, остальное отложить"
    finally:
        plain.close()


@test
def test_gate_wait_has_no_limit_and_listens_for_stop_and_wake(tmp: Path):
    """Ожидание восстановления: предела по времени нет, выход — связь вернулась или человек остановил; «проверить сейчас» будит."""
    G = _g()
    clock = {"t": 0.0}
    slept = []

    def sleep(sec):
        slept.append(sec)
        clock["t"] += sec
    polls = []

    def check():
        polls.append(clock["t"])
        return {"ok": clock["t"] >= 36 * 3600}                    # сутки с половиной — предела нет
    assert G.wait_restored(check, poll=20, sleep=sleep, clock=lambda: clock["t"]) is True
    assert clock["t"] >= 36 * 3600 and len(polls) > 6000, "ожидание оборвалось по времени"
    stop = threading.Event()
    calls = []

    def never():
        calls.append(1)
        if len(calls) == 3:
            stop.set()
        return {"ok": False}
    assert G.wait_restored(never, stop=stop, poll=2, sleep=lambda s: None) is False and len(calls) == 3, "стоп не услышан"
    # «проверить сейчас»: пауза обрывается, вердикт пересчитывается
    wake = iter([False, True, True, True, True])
    n = []
    res = G.wait_restored(lambda: (n.append(1), {"ok": len(n) >= 4})[1], poll=100, sleep=lambda s: n.append(0), woken=lambda: next(wake, True))
    assert res is True and n.count(0) <= 2, f"пробуждение не прервало паузу: {n}"
    # дробный опрос не растягивается до секунды (иначе тесты и «стоп» глохнут)
    waited = []
    G.wait_restored(lambda: {"ok": len(waited) > 2}, poll=0.05, sleep=waited.append)
    assert waited and max(waited) < 0.5, waited


@test
def test_gate_steps_are_checked_once_for_the_route_and_each_gets_its_verdict(tmp: Path):
    """`check_steps`: один предзапуск на весь маршрут, вердикт на каждый шаг; выключенная проверка пропускает всё."""
    G = _g()
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    try:
        rows = G.check_steps(["kb:lint", "sync:confluence", "sync:web", "agent:build"], str(proj), svc=net.svc)
        by = {r["cmd"]: r for r in rows}
        assert by["kb:lint"]["ok"] and by["sync:web"]["ok"] and by["agent:build"]["ok"], f"доступное заблокировано: {rows}"
        assert not by["sync:confluence"]["ok"] and by["sync:confluence"]["blocked"][0]["name"] == "VPN-1", by["sync:confluence"]
        net.set(v1=True)
        assert all(r["ok"] for r in G.check_steps(["sync:confluence", "kb:lint"], str(proj), svc=net.svc)), "связь вернулась, а шаг заблокирован"
        net.set(net1=False)
        by = {r["cmd"]: r for r in G.check_steps(["sync:web", "kb:lint"], str(proj), svc=net.svc)}
        assert not by["sync:web"]["ok"] and by["sync:web"]["blocked"][0]["code"] == "internet_offline", "веб без интернета не заблокирован"
        assert by["kb:lint"]["ok"], "локальная команда заблокирована без интернета"
    finally:
        net.close()
    off = _Net(tmp / "off", up={"v1": False}, enabled=False)
    try:
        assert all(r["ok"] for r in G.check_steps(["sync:confluence"], str(proj), svc=off.svc)), "выключенная проверка блокирует"
        assert G.check_command("sync:confluence", str(proj), svc=off.svc)["ok"]
    finally:
        off.close()


# ------------------------------------------------------------------ маршрут

@test
def test_route_fail_fast_refuses_before_the_first_step(tmp: Path):
    """`fail_fast`: если шагу маршрута не хватает связи, маршрут не начинается — ни одной команды, причина названа."""
    rr = importlib.import_module("route_runner")
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    try:
        ck = NetPanel(tmp, _script(), [_sc("kb:lint", "sync:confluence", "kb:repair")])
        logs: list = []
        res = rr.RouteRun(ck, str(proj), _sc("kb:lint", "sync:confluence", "kb:repair"), True, net="fail_fast", log=logs.append).run()
        assert not res["ok"] and res["reason"] == "offline" and res["failed"] == "sync:confluence", res
        assert ck.calls == [], f"маршрут начался, хотя связи не хватает: {ck.calls}"
        assert "VPN-1" in res["note"] and "Confluence" in res["note"], f"в итоге нет причины: {res['note']}"
        # связь есть — тот же маршрут проходит целиком
        net.set(v1=True)
        ck = NetPanel(tmp, _script(), [])
        res = rr.RouteRun(ck, str(proj), _sc("kb:lint", "sync:confluence", "kb:repair"), True, net="fail_fast").run()
        assert res["ok"] and [c[1] for c in ck.calls] == ["kb:lint", "sync:confluence", "kb:repair"], (res, ck.calls)
        assert ck.policies == ["fail_fast"] * 3, f"процесс шага не узнал политику: {ck.policies}"
    finally:
        net.close()
        restore()


@test
def test_route_partial_runs_what_is_available_and_names_what_was_skipped(tmp: Path):
    """`partial`: недоступный шаг пропущен, остальные идут; итог называет пропущенное и не рапортует «чисто»."""
    rr = importlib.import_module("route_runner")
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    try:
        ck = NetPanel(tmp, _script(), [])
        res = rr.RouteRun(ck, str(proj), _sc("kb:lint", "sync:confluence", "kb:repair"), True, net="partial").run()
        assert [c[1] for c in ck.calls] == ["kb:lint", "kb:repair"], f"доступное не пошло или недоступное запущено: {ck.calls}"
        assert res["ok"] and res["reason"] == "passed" and res["net_skipped"] == ["sync:confluence"], res
        assert ck.policies == ["partial", "partial"]
    finally:
        net.close()
        restore()


@test
def test_route_queue_waits_for_the_network_then_goes_whole(tmp: Path):
    """`queue`: до первого шага дожидается всего нужного и идёт целиком; «Остановить» во время ожидания слышно."""
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    try:
        with _Fast() as rr:
            ck = NetPanel(tmp, _script(), [])
            seen: list = []
            ck.on_call = lambda cmd: seen.append((cmd, net.up["v1"]))
            net.later(0.4, v1=True)
            logs: list = []
            res = rr.RouteRun(ck, str(proj), _sc("kb:lint", "sync:confluence", "kb:repair"), True, net="queue", log=logs.append).run()
            assert res["ok"], res
            assert seen == [("kb:lint", True), ("sync:confluence", True), ("kb:repair", True)], \
                f"шаги пошли до возвращения связи или не пошли все: {seen}"
            assert any("VPN-1" in line for line in logs), "в журнале нет причины ожидания"
            # человек остановил, пока связи нет
            net.set(v1=False)
            ck = NetPanel(tmp, _script(), [])
            stop = threading.Event()
            threading.Timer(0.3, stop.set).start()
            res = rr.RouteRun(ck, str(proj), _sc("kb:lint", "sync:confluence"), True, net="queue", stop=stop).run()
            assert not res["ok"] and res["reason"] == "stopped" and ck.calls == [], f"остановка во время ожидания: {res} {ck.calls}"
    finally:
        net.close()
        restore()


@test
def test_route_partial_queue_defers_the_blocked_step_and_runs_it_after_restoration(tmp: Path):
    """По умолчанию доступное идёт сразу, недоступный шаг откладывается и повторяется в конце — после восстановления."""
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    try:
        with _Fast() as rr:
            ck = NetPanel(tmp, _script(), [])
            ck.on_call = lambda cmd: net.set(v1=True) if cmd == "kb:repair" else None        # связь вернулась посреди маршрута
            logs: list = []
            res = rr.RouteRun(ck, str(proj), _sc("kb:lint", "sync:confluence", "kb:repair"), True, log=logs.append).run()
            assert res["ok"] and res["reason"] == "passed", res
            assert [c[1] for c in ck.calls] == ["kb:lint", "kb:repair", "sync:confluence"], \
                f"отложенный шаг не ушёл в конец или не повторён: {ck.calls}"
            assert ck.policies == ["partial_queue", "partial_queue", "queue"], f"политика шагов: {ck.policies}"
            # связи нет и в конце: повтор ждёт её, а не падает
            net.set(v1=False)
            ck = NetPanel(tmp, _script(), [])
            net.later(0.5, v1=True)
            res = rr.RouteRun(ck, str(proj), _sc("kb:lint", "sync:confluence"), True).run()
            assert res["ok"] and [c[1] for c in ck.calls] == ["kb:lint", "sync:confluence"], (res, ck.calls)
    finally:
        net.close()
        restore()


@test
def test_route_cycle_with_a_blocked_step_stops_the_lap_and_reruns_the_block_after_restoration(tmp: Path):
    """Блок «цикл:» не крутится вхолостую: недоступный шаг откладывает блок целиком, после восстановления он идёт заново."""
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    try:
        with _Fast() as rr:
            sc = {"id": "net", "title": "Связь", "when": "", "group": "база", "steps": [
                {"manual": False, "cycle": "цикл:", "why": ""}, _step("sync:confluence"), _step("agent:build"),
                {"manual": False, "cycle": "конец цикла", "why": ""}, _step("kb:lint")]}
            ck = NetPanel(tmp, _script(), [sc])
            ck.on_call = lambda cmd: net.set(v1=True) if cmd == "agent:build" else None
            logs: list = []
            res = rr.RouteRun(ck, str(proj), sc, True, log=logs.append).run()
            assert res["ok"], res
            order = [c[1] for c in ck.calls]
            assert order[:2] == ["agent:build", "kb:lint"] and order.count("sync:confluence") == 1 and order[-2:] == ["sync:confluence", "agent:build"], \
                f"блок с недоступным шагом: {order}"
            assert any("цикл" in line.lower() and "связ" in line.lower() for line in logs), "оборот остановился молча"
    finally:
        net.close()
        restore()


@test
def test_route_script_refusal_is_a_net_verdict_not_a_failure(tmp: Path):
    """Скрипт источника отказал по связи строкой `AURORA-NET`: маршрут применяет политику, а не считает шаг упавшим."""
    G = _g()
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp)                                              # перед шагом связь «есть» — отказывает сам скрипт
    try:
        with _Fast() as rr:
            marker = G.NET_MARK + " " + json.dumps({"blocked": [{"code": "profile_offline", "name": "VPN-1", "source": "Confluence"}],
                                                     "sources": ["Confluence"]})
            script = _script()
            script["sync:confluence"] = [(2, ["Синк источника «Confluence» не запущен: VPN-1 недоступен", marker]), (0, ["скачано"])]
            ck = NetPanel(tmp, script, [])
            res = rr.RouteRun(ck, str(proj), _sc("sync:confluence", "kb:lint"), True, net="partial").run()
            assert res["ok"] and res["net_skipped"] == ["sync:confluence"] and [c[1] for c in ck.calls] == ["sync:confluence", "kb:lint"], \
                f"отказ скрипта: {res} {ck.calls}"
            script["sync:confluence"] = [(2, ["отказ", marker]), (0, ["скачано"])]
            ck = NetPanel(tmp, script, [])
            res = rr.RouteRun(ck, str(proj), _sc("sync:confluence", "kb:lint"), True, net="queue").run()
            assert res["ok"] and [c[1] for c in ck.calls] == ["sync:confluence", "sync:confluence", "kb:lint"], \
                f"после ожидания шаг не повторён: {ck.calls}"
            # настоящая поломка (код 2 без метки) остаётся поломкой
            ck = NetPanel(tmp, {"sync:confluence": [(2, ["упало по-настоящему"])], "kb:lint": [(0, [])]}, [])
            res = rr.RouteRun(ck, str(proj), _sc("sync:confluence", "kb:lint"), True, net="partial").run()
            assert not res["ok"] and res["failed"] == "sync:confluence" and not res["net_skipped"], res
    finally:
        net.close()
        restore()


@test
def test_route_with_the_check_off_runs_exactly_as_before(tmp: Path):
    """Проверка соединений выключена или не настроена: никаких проверок, политики и лишних аргументов запуска."""
    rr = importlib.import_module("route_runner")
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False}, enabled=False)               # профиль «лежит», но функция выключена владельцем
    try:
        ck = FakePanel(tmp, _script(), [])                          # прежняя панель: `start_job` не знает про политику
        res = rr.RouteRun(ck, str(proj), _sc("kb:lint", "sync:confluence", "kb:repair"), True, net="fail_fast").run()
        assert res["ok"] and [c[1] for c in ck.calls] == ["kb:lint", "sync:confluence", "kb:repair"], (res, ck.calls)
        assert res["net_skipped"] == []
    finally:
        net.close()
        restore()


# ------------------------------------------------------------------ расписание

def _net_scheduler(tmp: Path, script: dict, projects: list, task: dict, net: _Net):
    import shutil
    cron = importlib.import_module("cron")
    shutil.rmtree(cron.runs_dir(), ignore_errors=True)
    Path(cron.state_file()).unlink(missing_ok=True)
    ck = NetPanel(tmp, script, [_sc("kb:lint")])
    cron.save_tasks([task])
    return cron, ck, cron.Scheduler(ck, lambda: projects)


def _wait_cron(sched, seconds: float = 30) -> None:
    end = time.time() + seconds
    while time.time() < end:
        with sched.lock:
            if not sched.current and not sched.queue:
                return
        time.sleep(0.05)
    raise AssertionError("цепочка не закончилась")


@test
def test_cron_task_carries_the_net_policy_and_rejects_unknown_ones(tmp: Path):
    """Поле задания `net`: пусто (по настройке кита) или одна из четырёх политик; негодное — отказ при сохранении."""
    cron = importlib.import_module("cron")
    ck = FakePanel(tmp, {"kb:lint": [(0, [])]}, [_sc("kb:lint", sid="fix")])
    projects = [{"name": "A", "path": "/p/a"}]
    for policy in ("", "fail_fast", "partial", "queue", "partial_queue"):
        task, err = cron.clean_task(ck, _task(net=policy), projects)
        assert not err and task["net"] == policy, (policy, task, err)
    assert cron.clean_task(ck, _task(net="whenever"), projects)[1] == "bad_net"
    task, err = cron.clean_task(ck, _task(), projects)
    assert not err and task["net"] == "", "по умолчанию политика не навязана заданием"
    view = (KIT / "cockpit" / "modules" / "cron" / "view.js").read_text(encoding="utf-8")
    assert "cron.net." in view and "net:" in view, "в редакторе расписания нет выбора политики"


@test
def test_cron_applies_each_policy_to_a_step_that_lacks_the_network(tmp: Path):
    """Шаг цепочки без связи: `fail_fast` — «offline», `partial` — частичный итог, `partial_queue` — повтор в конце, `queue` — ожидание."""
    restore = set_home(tmp / "home")
    proj, other = _project(tmp, "a"), _project(tmp, "b")
    projects = [{"name": "A", "path": str(proj)}, {"name": "B", "path": str(other)}]
    steps = [{"kind": "command", "project": str(proj), "cmd": "sync:confluence", "args": []},
             {"kind": "command", "project": str(other), "cmd": "kb:lint", "args": []}]
    net = _Net(tmp, up={"v1": False})
    saved_poll = importlib.import_module("route_runner").NET_POLL_S
    importlib.import_module("route_runner").NET_POLL_S = 0.05
    try:
        # fail_fast: шаг не запускается, статус — offline, остальное идёт
        cron, ck, sched = _net_scheduler(tmp, _script(), projects, _task(net="fail_fast", steps=steps), net)
        sched.enqueue("a1b2c3d4")
        _wait_cron(sched)
        run = cron.list_runs()[0]
        st = {i["cmd"]: i for i in run["items"]}
        assert st["sync:confluence"]["status"] == "offline" and st["sync:confluence"]["note"].startswith("net:"), run["items"]
        assert st["kb:lint"]["status"] == "passed" and run["status"] == "failed", run
        assert "sync:confluence" not in [c[1] for c in ck.calls], "шаг без связи запущен"
        # partial: пропущен, итог цепочки не провален
        cron, ck, sched = _net_scheduler(tmp, _script(), projects, _task(net="partial", steps=steps), net)
        sched.enqueue("a1b2c3d4")
        _wait_cron(sched)
        run = cron.list_runs()[0]
        assert [i["status"] for i in run["items"]] == ["partial", "passed"] and run["status"] == "passed", run["items"]
        assert [c[1] for c in ck.calls] == ["kb:lint"]
        # partial_queue (умолчание): шаг отложен, остальное идёт, к отложенному вернулись после восстановления
        cron, ck, sched = _net_scheduler(tmp, _script(), projects, _task(steps=steps), net)
        ck.on_call = lambda cmd: net.set(v1=True) if cmd == "kb:lint" else None
        sched.enqueue("a1b2c3d4")
        _wait_cron(sched)
        run = cron.list_runs()[0]
        assert [i["status"] for i in run["items"]] == ["passed", "passed"] and run["items"][0].get("retry") and run["status"] == "passed", run["items"]
        assert [c[1] for c in ck.calls] == ["kb:lint", "sync:confluence"], f"порядок: {[c[1] for c in ck.calls]}"
        assert ck.policies[0] == "partial_queue", "процесс шага цепочки не узнал политику"
        # queue: ждёт связь и идёт сам
        net.set(v1=False)
        cron, ck, sched = _net_scheduler(tmp, _script(), projects, _task(net="queue", steps=steps), net)
        net.later(0.5, v1=True)
        sched.enqueue("a1b2c3d4")
        _wait_cron(sched)
        run = cron.list_runs()[0]
        assert [i["status"] for i in run["items"]] == ["passed", "passed"], run["items"]
        assert [c[1] for c in ck.calls] == ["sync:confluence", "kb:lint"], "ожидание не сохранило порядок шагов"
        assert any("⏳" in line for line in run["items"][0]["log"]), "в журнале шага нет ожидания связи"
    finally:
        importlib.import_module("route_runner").NET_POLL_S = saved_poll
        net.close()
        restore()


@test
def test_cron_with_the_check_off_ignores_the_policy_field(tmp: Path):
    """Выключенная проверка: шаг запускается как раньше, даже если у задания задана политика."""
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    projects = [{"name": "A", "path": str(proj)}]
    steps = [{"kind": "command", "project": str(proj), "cmd": "sync:confluence", "args": []}]
    net = _Net(tmp, up={"v1": False}, enabled=False)
    try:
        cron, ck, sched = _net_scheduler(tmp, _script(), projects, _task(net="fail_fast", steps=steps), net)
        sched.enqueue("a1b2c3d4")
        _wait_cron(sched)
        run = cron.list_runs()[0]
        assert [i["status"] for i in run["items"]] == ["passed"] and [c[1] for c in ck.calls] == ["sync:confluence"], run["items"]
        assert ck.policies == [""], f"выключенная проверка передала шагу политику: {ck.policies}"
    finally:
        net.close()
        restore()


# ------------------------------------------------------------------ синк источника

@test
def test_source_sync_refuses_with_the_exact_reason_and_never_for_a_source_without_a_mode(tmp: Path):
    """Скрипт синка не стартует, если профиль источника лежит: код 2, причина и машинная строка; веб без VPN и источник без режима идут."""
    import io
    import os
    from contextlib import redirect_stdout
    G = _g()
    SC = importlib.import_module("sources_core")
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    cwd, argv = os.getcwd(), sys.argv
    gate_env = os.environ.pop(G.GATE_OFF_ENV, None)
    try:
        os.chdir(proj)
        assert G.module_of_script("confluence_export.py", str(proj)) == "confluence-dc" and G.module_of_script("nope.py", str(proj)) == ""
        why = G.sync_refusal("confluence_export.py", str(proj), net.svc)
        assert "VPN-1" in why and "Confluence" in why and "а не таймаут" in why, why
        mark = G.parse_marker(why.splitlines())
        assert mark["sources"] == ["Confluence"] and mark["blocked"][0]["name"] == "VPN-1", mark
        assert G.parse_marker(["обычная строка", G.NET_MARK + " not json", G.NET_MARK + ' {"x": 1}']) == {}, "метка разбирается нестрого"
        # веб без VPN — «требует интернет»: интернет есть → идёт; интернета нет → отказ именно по интернету
        assert G.sync_refusal("web_export.py", str(proj), net.svc) == "", "веб без VPN остановлен при живом интернете"
        net.set(net1=False)
        assert "интернет" in G.sync_refusal("web_export.py", str(proj), net.svc), "веб без интернета не остановлен"
        net.set(net1=True)
        # источник без режима и неизвестный скрипт — как всегда
        assert G.sync_refusal("jira_export.py", str(proj), net.svc) == "", "источник, которого нет в проекте, остановлен"
        # `run_main`: отказ — код 2 до вызова main
        sys.argv = ["confluence_export.py"]
        called = []
        out = io.StringIO()
        real_service = _cn().service
        _cn().service = lambda kit=None: net.svc
        try:
            with redirect_stdout(out):
                rc = SC.run_main(lambda: called.append(1) or 0)
        finally:
            _cn().service = real_service
        assert rc == 2 and not called and "AURORA-NET" in out.getvalue(), (rc, called, out.getvalue())
        # владелец отключил проверку на стенде
        os.environ[G.GATE_OFF_ENV] = "0"
        assert G.sync_refusal("confluence_export.py", str(proj), net.svc) == ""
    finally:
        os.environ.pop(G.GATE_OFF_ENV, None)
        if gate_env is not None:
            os.environ[G.GATE_OFF_ENV] = gate_env
        sys.argv = argv
        os.chdir(cwd)
        net.close()
        restore()
    off = _Net(tmp / "off", up={"v1": False}, enabled=False)
    try:
        assert G.sync_refusal("confluence_export.py", str(proj), off.svc) == "", "выключенная проверка остановила синк"
    finally:
        off.close()
