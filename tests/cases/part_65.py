"""Проверки движка Aurora, часть 65. Каркас и помощники — tests/harness.py.

Блокировка функций кита по проверке соединений (фаза C), вторая половина: роутер модели (недоступный провайдер не трогают
и в карантин не садят, при недоступности всех — отказ или ожидание), ожидание связи в прогонах агента, сторож шагов по
сводке связи, запуск из панели (диалог «встать в очередь», ожидающее задание и его остановка, диалог маршрута).
Сети нет: проверки подставные, транспорт модели подставной; кит, проект и домашняя папка — временные.
"""
from __future__ import annotations

import http.client
import importlib
import json
import os
import sys
import threading
import time
from pathlib import Path

from harness import KIT, SCRIPTS, _cockpit_on, set_home, test  # noqa: F401

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(KIT / "cockpit"))

from cases.part_64 import _Net, _project, _reset_gate_caches  # noqa: E402

OK_BODY = {"choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}]}
MSGS = [{"role": "user", "content": "x"}]


def _models_data(vpn_a: str = "VPN-1", vpn_b: str = "VPN-2") -> dict:
    return {"providers": [
        {"id": "gw-a", "name": "GWA", "type": "openai", "url": "http://127.0.0.1:1/v1", "key": "k", "vpn": vpn_a},
        {"id": "gw-b", "name": "GWB", "type": "openai", "url": "http://127.0.0.1:2/v1", "key": "k", "vpn": vpn_b}],
        "capabilities": {"llm": {"roles": [
            {"id": "worker", "backends": [{"provider": "gw-a", "model": "m1"}, {"provider": "gw-b", "model": "m2"}]},
            {"id": "critic", "backends": [{"provider": "gw-a", "model": "m1"}]}]}}}


def _models_net(tmp: Path, up: dict | None = None, enabled: bool = True):
    """Сеть с двумя провайдерами модели (gw-a за VPN-1, gw-b за VPN-2) и настройка моделей кита под неё → (net, cfg)."""
    MC, CN = importlib.import_module("model_config"), importlib.import_module("connectivity")
    data = MC.normalize(_models_data())[0]
    net = _Net(tmp, up=up, providers=CN.providers_of(None, data), enabled=enabled)
    assert MC.save(net.kit, _models_data())["ok"]
    return net, MC.to_config(MC.load(net.kit))


def _agent():
    AG = importlib.import_module("agent_core")
    AG.DOWN.clear()
    AG.COOL.clear()
    AG.SLOW.clear()
    return AG


# ------------------------------------------------------------------ роутер модели

@test
def test_router_skips_an_unavailable_provider_without_quarantining_it(tmp: Path):
    """Профиль провайдера лежит: к нему не ходят (запроса нет), карантина нет, вызов идёт на следующего из цепочки."""
    AG = _agent()
    net, cfg = _models_net(tmp, up={"v1": False})
    calls: list = []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        calls.append(b["n"])
        return (200, OK_BODY, "", 0.1)
    try:
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and r["backend"] == 2 and calls == [2], f"недоступный провайдер получил запрос: {calls} {r}"
        assert any("провайдер недоступен" in line and "VPN-1" in line for line in r["log"]), f"причина пропуска не названа: {r['log']}"
        assert not AG.DOWN, f"недоступный по связи провайдер посажен в карантин: {AG.DOWN}"
        net.set(v1=True)                                              # вернулся — сразу снова первый
        calls.clear()
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and r["backend"] == 1 and calls == [1], f"вернувшийся провайдер не подхвачен: {calls}"
    finally:
        net.close()


@test
def test_router_blocks_when_every_provider_of_the_role_is_unavailable(tmp: Path):
    """Недоступны все: разовый вызов отказывает сразу с причиной, запросов нет; запасной вне цепочки роли не подставляется."""
    AG = _agent()
    net, cfg = _models_net(tmp, up={"v1": False})
    calls: list = []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        calls.append(b["n"])
        return (200, OK_BODY, "", 0.1)
    try:
        before, failed = AG.USAGE["errors"].get(AG.LINK_FAILURE, 0), AG.USAGE["link_failed"]
        r = AG.call_role(cfg, "critic", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert not r["ok"] and r.get("net_blocked") and calls == [], f"цепочка роли недоступна, а запрос ушёл: {calls} {r}"
        assert r["net_blocked"]["providers"] == ["gw-a"] and "VPN-1" in r["net_blocked"]["why"], r["net_blocked"]
        assert any("не поломка провайдеров" in line and AG.LINK_FAILURE in line for line in r["log"]), r["log"]
        assert AG.USAGE["errors"].get(AG.LINK_FAILURE, 0) == before + 1 and AG.USAGE["link_failed"] == failed + 1, \
            "отказ не попал в счёт обрывов связи (итог прогона)"
        looks = importlib.import_module("agent_runner").looks_offline({"steps": [{"status": "сбой", "note": r["log"][-1]}]})
        assert looks, "прогон агента не узнаёт отказ по связи как обрыв связи и не станет ждать"
        assert not AG.DOWN
        # у роли worker запасной gw-b жив — он идёт, а gw-a для critic по-прежнему не подставляется «наугад»
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and r["backend"] == 2
        net.set(v2=False)
        calls.clear()
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert not r["ok"] and r.get("net_blocked") and calls == [], f"оба провайдера цепочки лежат: {r}"
    finally:
        net.close()


@test
def test_router_of_a_route_step_waits_for_the_network_and_other_policies_refuse(tmp: Path):
    """Шаг, доводящий дело до конца, при недоступных всех ждёт возвращения и идёт сам; политика fail_fast или partial — отказ."""
    AG = _agent()
    net, cfg = _models_net(tmp, up={"v1": False, "v2": False})
    seen: list = []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        seen.append((b["n"], net.up["v1"]))
        return (200, OK_BODY, "", 0.1)
    saved_poll, saved_env = AG.NET_POLL, os.environ.pop("AURORA_NET_POLICY", None)
    AG.NET_POLL = 0.05
    try:
        cfg = dict(cfg, persist=True)
        net.later(0.5, v1=True)
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, sleep=lambda s: None)
        assert r["ok"] and r["backend"] == 1 and seen == [(1, True)], f"вызов не дождался связи: {seen} {r}"
        assert any("жду восстановления связи" in line for line in r["log"]), r["log"]
        for policy in ("fail_fast", "partial"):
            net.set(v1=False)
            os.environ["AURORA_NET_POLICY"] = policy
            seen.clear()
            r = AG.call_role(cfg, "worker", MSGS, transport=transport, sleep=lambda s: None)
            assert not r["ok"] and r.get("net_blocked") and seen == [], f"{policy}: вызов ждёт вместо отказа: {r}"
    finally:
        AG.NET_POLL = saved_poll
        os.environ.pop("AURORA_NET_POLICY", None)
        if saved_env is not None:
            os.environ["AURORA_NET_POLICY"] = saved_env
        net.close()


@test
def test_router_does_not_quarantine_a_provider_whose_failure_the_check_explains(tmp: Path):
    """Соединение не установилось, а проверка соединений знает почему (профиль лёг): карантина нет. Без проверки — карантин как прежде."""
    AG = _agent()
    refused = "ConnectionRefusedError: [Errno 111] Connection refused"
    net, cfg = _models_net(tmp)
    calls: list = []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        calls.append(b["n"])
        if b["n"] == 1:
            net.up["v1"] = False                                      # VPN-1 упал посреди вызова
            return (None, None, refused, 0.01)
        return (200, OK_BODY, "", 0.1)
    try:
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and r["backend"] == 2 and calls == [1, 2], (calls, r)
        assert 1 not in AG.DOWN, f"провайдер посажен в карантин за обрыв, который объясняет VPN: {AG.DOWN}"
        assert any("карантин не ставлю" in line and "VPN-1" in line for line in r["log"]), r["log"]
    finally:
        net.close()
    # отказ соединения, а профиль провайдера не лежит: это беда провайдера, карантин как раньше
    AG = _agent()
    net, cfg = _models_net(tmp / "same")
    calls.clear()
    net_ok = lambda kind, b, payload, timeout: ((404, None, "нет /slots", 0.0) if kind == "slots"         # noqa: E731
                                                 else ((calls.append(b["n"]), (None, None, refused, 0.01))[1] if b["n"] == 1
                                                       else (calls.append(b["n"]), (200, OK_BODY, "", 0.1))[1]))
    try:
        r = AG.call_role(cfg, "worker", MSGS, transport=net_ok, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and r["backend"] == 2 and AG.DOWN.get(1, 0) > time.time(), f"карантин за настоящий отказ пропал: {AG.DOWN}"
    finally:
        net.close()
    # проверка выключена — поведение прежнее целиком: карантин и ни одного вопроса сервису
    AG = _agent()
    net, cfg = _models_net(tmp / "off", up={"v1": False}, enabled=False)
    calls.clear()
    try:
        r = AG.call_role(cfg, "worker", MSGS, transport=net_ok, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and r["backend"] == 2 and calls == [1, 2] and AG.DOWN.get(1, 0) > time.time(), (calls, AG.DOWN)
        assert not any("недоступен" in line for line in r["log"]), "выключенная проверка вмешалась в вызов"
    finally:
        net.close()
        AG.DOWN.clear()


# ------------------------------------------------------------------ прогон агента и сторож

@test
def test_agent_run_waits_for_the_link_by_the_check_and_falls_back_to_the_old_pause(tmp: Path):
    """Обрыв связи в прогоне агента: проверка знает, что лежат все модели, — ждём их возвращения; не знает — прежняя пауза."""
    AR = importlib.import_module("agent_runner")
    G = importlib.import_module("conn_gate")
    net, _cfg = _models_net(tmp, up={"v1": False, "v2": False})
    saved = AR.LINK_POLL
    AR.LINK_POLL = 0.05
    try:
        t0 = time.time()
        net.later(0.4, v1=True)
        assert AR.wait_for_link(time.time() + 60) is True and net.up["v1"] and time.time() - t0 < 30, "ожидание не дождалось"
        assert AR.wait_for_link(time.time() + 60) is False, "все модели живы, а ждали по проверке"
        # живой хотя бы один провайдер — беда не из связи: прежняя пауза
        net.set(v1=False)
        assert G.llm_outage(net.svc, fresh=True) and AR.wait_for_link(time.time() + 0.2) is True, "ожидание не ограничено сроком окна"
        net.set(v2=True)
        assert G.llm_outage(net.svc, fresh=True) == {}, "один живой провайдер не снял «все лежат»"
    finally:
        AR.LINK_POLL = saved
        net.close()
    off, _ = _models_net(tmp / "off", up={"v1": False, "v2": False}, enabled=False)
    try:
        assert AR.wait_for_link(time.time() + 5) is False, "выключенная проверка ждала вместо прежней паузы"
    finally:
        off.close()
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert src.count("if not wait_for_link(deadline):") == 2, "оба цикла ожидания связи должны идти через проверку соединений"
    assert src.count("time.sleep(OFFLINE_WAIT)") == 2, "прежняя пауза должна остаться запасным путём"


@test
def test_watchdog_asks_the_check_for_the_link_instead_of_probing_again(tmp: Path):
    """Сторож шагов: проверка соединений знает нужное делу — берёт её сводку; не знает — прямые запросы, как раньше."""
    G = importlib.import_module("conn_gate")
    WD = importlib.import_module("watchdog")
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net, _cfg = _models_net(tmp)
    AG = importlib.import_module("agent_core")
    real_models = AG.models_of
    AG.models_of = lambda *a, **k: (_ for _ in ()).throw(AssertionError("сторож опросил модель напрямую, хотя сводка известна"))
    try:
        got = WD.probe(str(proj))
        assert got == {"ok": True, "down": []}, got
        state = G.watch_state(str(proj), net.svc)
        assert state["known"] and "CONFLUENCE" in state["covered"], f"источник за профилем должен считаться покрытым: {state}"
        net.set(v1=False)
        got = WD.probe(str(proj))
        assert not got["ok"] and "VPN-1" in got["down"], got
        net.set(v1=True, net1=False)
        got = WD.probe(str(proj))
        assert not got["ok"] and "интернет" in got["down"], got
    finally:
        AG.models_of = real_models
        net.close()
        restore()
    off, _ = _models_net(tmp / "off", enabled=False)
    try:
        assert G.watch_state(str(proj), off.svc) == {"known": False, "ok": True, "down": [], "covered": []}
    finally:
        off.close()


# ------------------------------------------------------------------ панель: диалоги и ожидающее задание

def _serve(ck, root: Path):
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(root)]
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
    return srv, call


@test
def test_panel_run_asks_before_starting_a_command_that_lacks_the_network(tmp: Path):
    """`/api/run`: не хватает связи — 409 с причиной и выбором «встать в очередь / отменить»; выбор очереди запускает ожидающее задание."""
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    ck, restore = _cockpit_on(net.kit)
    row = {"runnable": True, "flags": [], "fixed_flags": [], "script": "x.py", "cmd": "sync:confluence", "ns": "sync"}
    saved = (ck.command_by_name, ck.start_job, ck.version_gap)
    started: list = []
    ck.command_by_name = lambda name: dict(row, cmd=name)
    ck.version_gap = lambda p: ""
    ck.start_job = lambda project, cmd, extra, parent="", net_policy="", net_wait=False: (started.append((cmd, net_wait)), "job1")[1]
    srv, call = _serve(ck, tmp / "p")
    try:
        body = {"project": str(proj), "cmd": "sync:confluence", "args": []}
        st, d = call("POST", "/api/run", body)
        nb = d.get("net_blocked") or {}
        assert st == 409 and nb.get("cmd") == "sync:confluence" and nb["choices"] == ["queue", "cancel"] and not started, (st, d, started)
        assert nb["blocked"][0]["name"] == "VPN-1" and nb["blocked"][0]["source"] == "Confluence", nb
        st, d = call("POST", "/api/run", dict(body, net="queue"))
        assert st == 200 and d == {"job": "job1"} and started == [("sync:confluence", True)], (st, d, started)
        started.clear()
        st, d = call("POST", "/api/run", dict(body, route=True))                 # шаг маршрута идёт без диалога: у него политика
        assert st == 200 and started == [("sync:confluence", False)], (st, d, started)
        started.clear()
        st, d = call("POST", "/api/run", dict(body, cmd="kb:lint"))                # команде, которой сеть не нужна, диалога нет
        assert st == 200 and started == [("kb:lint", False)], (st, d, started)
        started.clear()
        net.set(v1=True)
        st, d = call("POST", "/api/run", body)
        assert st == 200 and started == [("sync:confluence", False)], f"связь есть, а запуск ждёт: {st} {d} {started}"
        # проверка выключена владельцем — диалога нет, даже если профиль «лежит»
        net.set(v1=False)
        (net.kit / "local" / "connectivity.json").write_text(json.dumps({"enabled": False, "profiles": [], "probes": []}), encoding="utf-8")
        started.clear()
        st, d = call("POST", "/api/run", body)
        assert st == 200 and started == [("sync:confluence", False)], f"выключенная проверка спрашивает: {st} {d}"
    finally:
        srv.shutdown()
        srv.server_close()
        ck.command_by_name, ck.start_job, ck.version_gap = saved
        net.close()
        restore()


@test
def test_panel_waiting_job_starts_nothing_until_the_network_returns_and_can_be_stopped(tmp: Path):
    """Задание «в очереди за связью»: процесса нет, в консоли — причина; вернулась связь — команда стартует; «Прервать» снимает ожидание."""
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    ck, restore = _cockpit_on(net.kit)
    marker = tmp / "ran.txt"
    script = tmp / "x.py"
    script.write_text(f"print('запущено', flush=True)\nopen({str(marker)!r}, 'w').write('1')\n", encoding="utf-8")
    row = {"runnable": True, "flags": [], "fixed_flags": [], "script": "x.py", "cmd": "sync:confluence", "ns": "sync"}
    saved = (ck.command_by_name, ck.script_path, ck.write_runlog, ck.mark_running)
    ck.command_by_name = lambda name: row
    ck.script_path = lambda project, name: str(script)
    ck.write_runlog = lambda *a, **k: None
    ck.mark_running = lambda *a, **k: None
    rr = importlib.import_module("route_runner")
    poll = rr.NET_POLL_S
    rr.NET_POLL_S = 0.05

    def until(cond, seconds=20):
        end = time.time() + seconds
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.05)
        return False
    jobs: list = []
    try:
        jid = ck.start_job(str(proj), "sync:confluence", [], net_wait=True)
        jobs.append(jid)
        job = ck.JOBS[jid]
        assert until(lambda: job.get("waiting")), f"задание не встало в очередь: {job}"
        assert any("⏳" in line and "VPN-1" in line for line in job["out"]), job["out"]
        assert "proc" not in job and not marker.exists(), "команда стартовала, пока связи нет"
        assert ck.stop_job(jid).get("ok"), "«Прервать» не снимает ожидание"
        assert until(lambda: job["done"]) and job["rc"] == -15 and not marker.exists(), f"ожидание не прервалось: {job}"
        assert any("прервано" in line for line in job["out"]), job["out"]
        # связь вернулась — то же ожидание заканчивается запуском
        jid = ck.start_job(str(proj), "sync:confluence", [], net_wait=True)
        jobs.append(jid)
        job = ck.JOBS[jid]
        assert until(lambda: job.get("waiting")) and not marker.exists()
        net.set(v1=True)
        assert until(lambda: job["done"]) and job["rc"] == 0 and marker.exists(), f"связь вернулась, а команда не пошла: {job}"
        assert any("связь вернулась" in line for line in job["out"]), job["out"]
    finally:
        for j in jobs:
            until(lambda j=j: ck.JOBS[j]["done"], 10)
        rr.NET_POLL_S = poll
        ck.command_by_name, ck.script_path, ck.write_runlog, ck.mark_running = saved
        net.close()
        restore()


@test
def test_panel_route_run_asks_which_steps_lack_the_network_and_passes_the_choice_on(tmp: Path):
    """`/api/route/run`: недостающие шаги названы, выбор «только доступное / очередь / отмена»; выбор становится политикой прогона."""
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    ck, restore = _cockpit_on(net.kit)
    saved = (ck.version_gap, ck.route_start, ck.scenarios, ck.command_by_name, ck.all_scenarios)
    started: list = []
    # Маршруты и реестр команд — из пробного кита без их файлов: подставляем маршрут «update» из трёх шагов.
    ck.scenarios = lambda: [{"id": "update", "title": "Обновить", "when": "", "group": "база", "steps": [
        {"manual": False, "cmd": c, "why": "", "flags": []} for c in ("kb:lint", "sync:confluence", "kb:repair")]}]
    ck.all_scenarios = ck.scenarios
    ck.command_by_name = lambda name: {"runnable": True, "flags": [], "fixed_flags": [], "script": "x.py", "cmd": name,
                                       "ns": name.split(":")[0]}
    ck.version_gap = lambda p: ""
    ck.route_start = lambda project, sc_id, write, resume=None, lang="ru", net="": (started.append((sc_id, net)), {"route": "r1"})[1]
    srv, call = _serve(ck, tmp / "p")
    try:
        body = {"project": str(proj), "scId": "update", "write": True}
        st, d = call("POST", "/api/route/run", body)
        nb = d.get("net_blocked") or {}
        assert st == 409 and nb["choices"] == ["partial", "queue", "cancel"] and not started, (st, d)
        assert [i["cmd"] for i in nb["items"]] == ["sync:confluence"] and nb["items"][0]["blocked"][0]["name"] == "VPN-1", \
            f"в диалоге не те шаги: {nb['items']}"
        for choice in ("partial", "queue"):
            started.clear()
            st, d = call("POST", "/api/route/run", dict(body, net=choice))
            assert st == 200 and started == [("update", choice)], (st, d, started)
        net.set(v1=True)
        started.clear()
        st, d = call("POST", "/api/route/run", body)
        assert st == 200 and started == [("update", "")], f"связь есть, а маршрут спрашивает: {st} {d}"
    finally:
        srv.shutdown()
        srv.server_close()
        ck.version_gap, ck.route_start, ck.scenarios, ck.command_by_name, ck.all_scenarios = saved
        net.close()
        restore()


@test
def test_panel_page_has_the_net_dialog_and_all_its_strings(_t):
    """Страница: диалог связи на месте, 409 `net_blocked` перехватывается для команд и маршрутов, строки есть в ru и en."""
    panel = (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    html = (KIT / "cockpit" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'id="netOverlay"' in html and 'id="netDialog"' in html, "в разметке нет диалога связи"
    assert "function askNet(" in panel and "net_blocked" in panel, "страница не перехватывает отказ по связи"
    assert panel.count("net_blocked") >= 3, "диалог связи нужен и запуску команды, и маршруту"
    ru = json.loads((KIT / "cockpit" / "i18n" / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit" / "i18n" / "en.json").read_text(encoding="utf-8"))
    for key in ("net.dialog_title", "net.btn_queue", "net.btn_partial", "net.btn_cancel", "route.net_skip", "route.net_defer",
                "route.net_fail", "route.net_queue_line", "route.net_restored", "route.wait_net_line", "route.net_cycle_stop"):
        assert key in ru and key in en, f"нет строки {key} в ru или en"
    assert panel.count("t(\"net.reason.") + panel.count("net.reason.") >= 3, "причины блокировки не из каталога"
    _reset_gate_caches()
