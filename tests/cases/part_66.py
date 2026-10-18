"""Проверки движка Aurora, часть 66. Каркас и помощники — tests/harness.py.

Доработки по независимому просмотру фазы C: проверка, которую нельзя выполнить, не объявляет связь потерянной; ключ по открытому
каналу не идёт через прокси окружения; ожидание держит последнее известное «недоступно»; карантин за обрыв снимается, когда связь
вернулась; сводка обновляется не чаще раза в интервал и одним потоком; частичный маршрут не считается законченным; расписание
получает отложенное от маршрута; «Остановить» не теряется; пройденное при продолжении связи не требует; имена источников и
комментарии в блоке; копия битого файла закрыта правами. Сети нет; кит, проект и домашняя папка — временные.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import sys
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

from harness import KIT, SCRIPTS, set_home, test  # noqa: F401

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(KIT / "cockpit"))

from cases.part_60 import _Stub  # noqa: E402
from cases.part_64 import NetPanel, _Fast, _Net, _net_scheduler, _project, _reset_gate_caches, _sc, _script, _step, _wait_cron  # noqa: E402
from cases.part_65 import MSGS, OK_BODY, _agent, _models_net  # noqa: E402


def _g():
    return importlib.import_module("conn_gate")


def _cn():
    return importlib.import_module("connectivity")


# ------------------------------------------------------------------ проверки

@test
def test_a_probe_that_cannot_run_is_inconclusive_and_never_declares_the_link_lost(tmp: Path):
    """Нет программы ping, сбой самой проверки: раунд не засчитывается, профиль остаётся «неизвестным», провайдеры не блокируются."""
    CN, G = _cn(), _g()
    real = CN.subprocess.run

    def no_ping(*a, **k):
        raise FileNotFoundError("ping")
    CN.subprocess.run = no_ping
    try:
        res = CN._check_icmp({"host": "example.com"}, 1)
    finally:
        CN.subprocess.run = real
    assert not res["ok"] and res["code"] == "no_ping" and res["inconclusive"] is True, res
    boom = CN.run_probe({"type": "tcp", "id": "x"}, 1, {"tcp": lambda p, t: (_ for _ in ()).throw(RuntimeError("сломалось"))})
    assert boom["inconclusive"] is True and boom["code"] == "other", boom
    net, _cfg = _models_net(tmp)
    try:
        checked = next(r for r in net.svc.snapshot()["profiles"] if r["name"] == "VPN-1")["checked"]
        net.special["v1"] = {"ok": False, "code": "no_ping", "ms": 0, "detail": "", "inconclusive": True}
        for _ in range(3):
            net.set()
        prof = next(r for r in net.svc.snapshot()["profiles"] if r["name"] == "VPN-1")
        assert prof["state"] != "offline" and prof["checked"] == checked and prof["probes"][0]["ok"] is None, \
            f"раунды, которые нельзя выполнить, засчитаны как «нет ответа»: {prof}"
        assert not G.providers_down(net.svc, fresh=True), "проверка, которую нельзя выполнить, заблокировала провайдеров"
        net.special.clear()
        net.set(v1=False)                     # настоящий отказ делает профиль недоступным
        assert G.providers_down(net.svc, fresh=True), "настоящий отказ не засчитан"
        net.special["v1"] = {"ok": False, "code": "no_ping", "ms": 0, "detail": "", "inconclusive": True}
        net.set()
        assert next(r for r in net.svc.snapshot()["profiles"] if r["name"] == "VPN-1")["state"] == "offline", \
            "неисполнимая проверка вернула профиль в «доступен»"
    finally:
        net.close()
    fresh = _Net(tmp / "fresh", enabled=False)
    try:
        fresh.special["v1"] = {"ok": False, "code": "no_ping", "ms": 0, "detail": "", "inconclusive": True}
        fresh.svc.check(force=True)
        snap = fresh.svc.snapshot()
        assert next(r for r in snap["profiles"] if r["name"] == "VPN-1")["state"] == "unknown", snap["profiles"]
    finally:
        fresh.close()


@test
def test_a_probe_with_a_key_over_cleartext_bypasses_the_proxy_of_the_environment(tmp: Path):
    """Проверка ручки с ключом по http идёт напрямую: прокси из окружения ключ не видит; без ключа прокси окружения соблюдается."""
    CN = _cn()
    target, proxy = _Stub(), _Stub()
    names = ("HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
    saved = {k: os.environ.get(k) for k in names}
    try:
        for k in names:
            os.environ.pop(k, None)
        os.environ["HTTP_PROXY"] = os.environ["http_proxy"] = f"http://127.0.0.1:{proxy.port}"
        url = f"http://127.0.0.1:{target.port}/v1/models"
        res = CN._check_http({"url": url, "_headers": {"Authorization": "Bearer key-123456"}}, 3)
        assert res["ok"] and target.hits == 1 and proxy.hits == 0, f"ключ ушёл через прокси окружения: {res} {target.hits} {proxy.hits}"
        res = CN._check_http({"url": url}, 3)
        assert res["ok"] and proxy.hits == 1, "проверка без ключа должна идти тем же путём, что и работа: через прокси окружения"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        target.close()
        proxy.close()


@test
def test_a_provider_check_with_a_key_but_no_key_is_unknown_not_down(tmp: Path):
    """Проверка «с ключом» без ключа невозможна: провайдер «неизвестен» (проверку собрать нельзя), а не «не прошёл»."""
    CN = _cn()
    prov = {"id": "gw", "name": "GW", "url": "https://llm.example.com/v1", "key": "", "profile": "", "check": "auth",
            "path": "/models", "local": False}
    assert CN.provider_probe(prov) is None, "проверка с ключом без ключа собралась"
    assert CN.provider_probe(dict(prov, key="k-123456")) is not None
    net = _Net(tmp, providers=[prov])
    try:
        row = next(r for r in net.svc.snapshot()["providers"] if r["id"] == "gw")
        assert row["state"] == "unknown" and row["why"] == "check_unusable", row
    finally:
        net.close()


@test
def test_the_state_file_is_private(tmp: Path):
    """Файл состояния проверок закрыт для чужих (права 600)."""
    net = _Net(tmp)
    try:
        path = Path(net.svc.state_file)
        assert path.is_file()
        if sys.platform != "win32":
            assert oct(path.stat().st_mode & 0o777) == "0o600", oct(path.stat().st_mode & 0o777)
    finally:
        net.close()


@test
def test_waiting_holds_the_last_known_offline_until_a_positive_round(tmp: Path):
    """Проверка не может завершиться (зависло имя): обычная сводка забывает «недоступно» через три интервала, ожидание — нет."""
    G = _g()
    proj = _project(tmp)
    net, _cfg = _models_net(tmp, up={"v1": False, "v2": False})
    try:
        late = {"ok": False, "code": "late", "ms": 3000, "detail": "", "late": True}
        for pid in ("v1", "v2", "net1"):
            net.special[pid] = late                         # дальше раунды не засчитываются: проверка зависает
        net.clock.now += 400                                # данные состарились сильнее трёх интервалов
        net.svc.check()
        snap = net.svc.snapshot()
        assert next(r for r in snap["profiles"] if r["name"] == "VPN-1")["state"] == "unknown", "состарившееся осталось «недоступно»"
        assert G.check_command("sync:confluence", str(proj), svc=net.svc)["ok"], "незнание остановило дело"
        held = G.check_command("sync:confluence", str(proj), svc=net.svc, hold=True)
        assert not held["ok"] and held["blocked"][0]["name"] == "VPN-1", f"ожидание отпущено без положительного раунда: {held}"
        assert G.providers_down(net.svc, fresh=True, hold=True), "провайдеры за профилем отпущены без положительного раунда"
        assert G.llm_outage(net.svc, fresh=True, hold=True), "ожидание модели отпущено"
        net.special.clear()
        net.set(v1=True, v2=True)                           # положительный раунд
        assert G.check_command("sync:confluence", str(proj), svc=net.svc, hold=True)["ok"], "положительный раунд не отпустил ожидание"
    finally:
        net.close()


@test
def test_the_summary_refreshes_once_per_interval_and_once_for_a_crowd_of_threads(tmp: Path):
    """Двадцать потоков пула не гонят двадцать проверок; устаревшее на месте перепроверяется не чаще раза в десять секунд."""
    G = _g()
    net, _cfg = _models_net(tmp)
    try:
        base = net.probes
        net.clock.now += 400                                # состояние устарело — пора перепроверять на месте
        _reset_gate_caches()
        threads = [threading.Thread(target=G.providers_down, args=(net.svc,)) for _ in range(16)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        spent = net.probes - base
        assert 0 < spent <= 3, f"толпа потоков запустила проверки {spent} раз (профилей три)"
        net.clock.now += 400
        G._DOWN.update(at=0.0, key=None, val={})            # кэш решения остыл, перепроверка на месте — ещё нет
        G.providers_down(net.svc)
        assert net.probes - base == spent, "перепроверка на месте пошла раньше срока"
        G._ENSURE.update(at=0.0, key=None)
        G._DOWN.update(at=0.0, key=None, val={})
        G.providers_down(net.svc)
        assert net.probes - base > spent, "по истечении срока перепроверка не состоялась"
    finally:
        net.close()


@test
def test_watchdog_does_not_trust_a_summary_that_watches_nothing(tmp: Path):
    """Облачный провайдер без привязки, без проверки ручки и интернет без адресов: сводка «в порядке» пуста, сторож спрашивает прямо."""
    G = _g()
    CN = _cn()
    prov = {"id": "cloud", "name": "Cloud", "url": "https://llm.example.com/v1", "key": "k", "profile": "", "check": "off",
            "path": "/models", "local": False}
    proj = _project(tmp)
    net, _cfg = _models_net(tmp)
    try:
        assert CN.save(net.kit, {"enabled": True, "profiles": [{"name": "VPN-1"}], "probes": [
            {"id": "v1", "profile": "VPN-1", "type": "tcp", "host": "127.0.0.1", "port": 1}]})["ok"]
        net.svc.providers_fn = lambda: [prov]
        os.remove(str(net.kit / "local" / "models.json"))
        import model_config as MC
        assert MC.save(net.kit, {"providers": [{"id": "cloud", "name": "Cloud", "type": "openai", "url": "https://llm.example.com/v1",
                                                "key": "k"}], "capabilities": {"llm": {"roles": [
                                                    {"id": "worker", "backends": [{"provider": "cloud", "model": "m"}]}]}}})["ok"]
        plain = tmp / "plain"
        plain.mkdir()
        from harness import make_project
        bare = make_project(plain)
        state = G.watch_state(str(bare), net.svc)
        assert state["known"] is False, f"сводка без единого наблюдателя за моделью принята за ответ: {state}"
        assert G.watch_state(str(proj), net.svc)["known"] is False, "провайдер без привязки и проверки не покрыт, а сводка «известна»"
    finally:
        net.close()


# ------------------------------------------------------------------ роутер: карантин за обрыв и диагностика

@test
def test_quarantine_set_before_the_check_knew_is_lifted_when_the_connection_returns(tmp: Path):
    """Первый обрыв после падения VPN садится в карантин (проверка ещё не знает); когда она узнала и связь вернулась — карантин снят."""
    AG = _agent()
    AG._NET_SEEN.clear()
    refused = "ConnectionRefusedError: [Errno 111] Connection refused"
    net, cfg = _models_net(tmp)
    calls: list = []
    mode = {"refuse": True}

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        calls.append(b["n"])
        if b["n"] == 1 and mode["refuse"]:
            return (None, None, refused, 0.01)
        return (200, OK_BODY, "", 0.1)
    try:
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and r["backend"] == 2 and AG.DOWN.get(1, 0) > time.time(), "карантин за настоящий отказ не поставлен"
        net.set(v1=False)                                          # проверка узнала: профиль лежит
        calls.clear()
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and calls == [2], f"недоступный провайдер получил запрос: {calls}"
        net.set(v1=True)                                           # связь вернулась
        mode["refuse"] = False
        calls.clear()
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and r["backend"] == 1 and calls == [1] and 1 not in AG.DOWN, \
            f"карантин пережил возвращение связи: {AG.DOWN} {calls}"
        assert any("карантин за обрыв снят" in line for line in r["log"]), r["log"]
    finally:
        AG.DOWN.clear()
        AG._NET_SEEN.clear()
        net.close()


@test
def test_diagnostics_ask_the_providers_live_and_a_blocked_call_leaves_a_machine_line(tmp: Path):
    """`agent:ping`/`probe`/`width` (без ожидания) не останавливает проверка соединений; отказ вызова печатает `AURORA-NET` раз на набор."""
    AG = _agent()
    G = _g()
    AG._NET_MARK_SAID.clear()
    net, cfg = _models_net(tmp, up={"v1": False})
    calls: list = []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        calls.append(b["n"])
        return (200, OK_BODY, "", 0.1)
    try:
        one = dict(cfg, backends=[b for b in cfg["backends"] if b["n"] == 1], chains={"llm": {"worker": [b for b in cfg["backends"] if b["n"] == 1]}})
        r = AG.call_role(one, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=AG.NO_WAIT)
        assert r["ok"] and calls == [1], f"диагностика остановлена проверкой соединений: {calls} {r}"
        calls.clear()
        out = io.StringIO()
        with redirect_stdout(out):
            r1 = AG.call_role(one, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
            r2 = AG.call_role(one, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert not r1["ok"] and r1.get("net_blocked") and not r2["ok"] and calls == [], (r1, calls)
        lines = [ln for ln in out.getvalue().splitlines() if ln.startswith(G.NET_MARK)]
        assert len(lines) == 1, f"метка напечатана {len(lines)} раз вместо одного: {out.getvalue()!r}"
        mark = G.parse_marker(lines)
        assert mark["blocked"][0]["code"] == "llm_down" and mark["blocked"][0]["providers"] == ["gw-a"] \
            and mark["blocked"][0]["profiles"] == ["VPN-1"], mark
        assert "все провайдеры моделей недоступны" in G.reason_line(mark["blocked"][0])
    finally:
        AG._NET_MARK_SAID.clear()
        net.close()


# ------------------------------------------------------------------ маршрут

@test
def test_route_stop_pressed_while_the_connection_was_awaited_never_starts_the_step(tmp: Path):
    """«Остановить» пришло в тот же миг, когда связь вернулась: шаг не начинается, маршрут — «остановлен»."""
    rr = importlib.import_module("route_runner")
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp)
    try:
        stop = threading.Event()

        class Racy(rr.RouteRun):
            def _net_step(self, st):
                stop.set()                                   # человек нажал, пока шла проверка, а проверка ответила «можно»
                return None
        ck = NetPanel(tmp, _script(), [])
        res = Racy(ck, str(proj), _sc("sync:confluence", "kb:lint"), True, stop=stop).run()
        assert ck.calls == [] and not res["ok"] and res["reason"] == "stopped", f"шаг запущен после остановки: {ck.calls} {res}"
    finally:
        net.close()
        restore()


@test
def test_route_resume_does_not_ask_the_network_for_steps_it_already_did(tmp: Path):
    """Продолжение: шаг, уже пройденный, связи не требует — ни отказа fail_fast, ни ожидания; диалог панели его тоже не считает."""
    rr = importlib.import_module("route_runner")
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    try:
        ck = NetPanel(tmp, _script(), [])
        res = rr.RouteRun(ck, str(proj), _sc("sync:confluence", "kb:repair"), True, net="fail_fast",
                          resume={"skipSigs": ["sync:confluence --apply"]}).run()
        assert res["ok"] and [c[1] for c in ck.calls] == ["kb:repair"], f"пройденное остановило продолжение: {res} {ck.calls}"
        steps = [{"cmd": "sync:confluence", "args": ["--apply"], "cycle": False, "pos": 0},
                 {"cmd": "kb:repair", "args": ["--apply"], "cycle": False, "pos": 1},
                 {"cmd": "kb:repair", "args": ["--apply"], "cycle": False, "pos": 2},
                 {"cmd": "agent:build", "args": [], "cycle": True, "pos": 3}]
        left = rr.pending_steps(steps, ["sync:confluence --apply", "kb:repair --apply @1", "agent:build"])
        assert [(s["cmd"], s["pos"]) for s in left] == [("kb:repair", 2), ("agent:build", 3)], \
            "повторяющаяся подпись ключуется позицией, а шаги цикла идут всегда"
    finally:
        net.close()
        restore()


@test
def test_route_defers_in_plan_order_and_refusal_before_the_first_step_commits_nothing(tmp: Path):
    """Отложенное идёт в порядке плана (блок цикла раньше следующего за ним шага); отказ до первого шага ничего не коммитит."""
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    try:
        with _Fast() as rr:
            sc = {"id": "net", "title": "Связь", "when": "", "group": "база", "steps": [
                {"manual": False, "cycle": "цикл:", "why": ""}, _step("sync:confluence"), _step("agent:build"),
                {"manual": False, "cycle": "конец цикла", "why": ""}, _step("ship:publish"), _step("kb:lint")]}
            script = _script()
            script["ship:publish"] = [(0, ["опубликовано"])]
            ck = NetPanel(tmp, script, [sc])
            ck.on_call = lambda cmd: net.set(v1=True) if cmd == "kb:lint" else None           # связь вернулась в самом конце
            res = rr.RouteRun(ck, str(proj), sc, True).run()
            order = [c[1] for c in ck.calls]
            assert res["ok"] and order == ["agent:build", "kb:lint", "sync:confluence", "agent:build", "ship:publish"], \
                f"публикация обогнала блок, который её готовит: {order}"
            net.set(v1=False)
            ck = NetPanel(tmp, _script(), [])
            res = rr.RouteRun(ck, str(proj), _sc("sync:confluence"), True, net="fail_fast").run()
            assert res["reason"] == "offline" and ck.commits == [], f"отказ до первого шага закоммитил работу человека: {ck.commits}"
            stop = threading.Event()
            threading.Timer(0.3, stop.set).start()
            res = rr.RouteRun(ck, str(proj), _sc("sync:confluence"), True, net="queue", stop=stop).run()
            assert res["reason"] == "stopped" and ck.commits == [], f"остановка в ожидании закоммитила: {ck.commits}"
    finally:
        net.close()
        restore()


@test
def test_route_that_skipped_steps_for_the_connection_is_partial_not_complete(tmp: Path):
    """Политика partial: итог «частично» — состояние остаётся для «Продолжить», база не помечена обновлённой."""
    rr = importlib.import_module("route_runner")
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    try:
        marks = lambda: list(Path(proj).rglob("kb_updated.json"))      # noqa: E731
        ck = NetPanel(tmp, _script(), [])
        logs: list = []
        res = rr.RouteRun(ck, str(proj), _sc("kb:lint", "sync:confluence", "kb:repair", sid="update"), True, net="partial", log=logs.append).run()
        assert res["ok"] and res["partial"] is True and res["net_skipped"] == ["sync:confluence"], res
        assert isinstance(ck.route_state, dict) and ck.route_state["reason"] == "offline" and "sync:confluence --apply" not in ck.route_state["done"], \
            f"состояние для «Продолжить маршрут» не оставлено: {ck.route_state}"
        assert not marks(), "база помечена обновлённой, хотя шаг синка пропущен"
        assert any("частично" in line for line in logs), "итог не назвал маршрут частичным"
        assert any("частично" in m for m in ck.commits), ck.commits
        net.set(v1=True)                                            # то же с полной связью — всё как у законченного
        ck = NetPanel(tmp, _script(), [])
        res = rr.RouteRun(ck, str(proj), _sc("kb:lint", "sync:confluence", "kb:repair", sid="update"), True, net="partial").run()
        assert res["ok"] and res["partial"] is False and ck.route_state == "cleared", (res, ck.route_state)
    finally:
        net.close()
        restore()


@test
def test_marker_with_any_failure_code_is_a_net_verdict_and_waits_back_off(tmp: Path):
    """Метка `AURORA-NET` при коде 1 (а не только 2) — тоже отказ по связи; повторный отказ не запускает скрипт вхолостую."""
    G = _g()
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp)
    try:
        with _Fast() as rr:
            marker = G.net_marker([{"code": "profile_offline", "name": "VPN-1", "source": "Confluence"}], ["Confluence"])
            script = _script()
            script["sync:confluence"] = [(1, ["нашла", marker])]
            ck = NetPanel(tmp, script, [])
            res = rr.RouteRun(ck, str(proj), _sc("sync:confluence", "kb:lint"), True, net="partial").run()
            assert res["ok"] and res["net_skipped"] == ["sync:confluence"], f"метка при коде 1 не узнана: {res}"
            # проверка маршрута говорит «можно», а скрипт отказывает: паузы растут, остановка слышна
            script["sync:confluence"] = [(2, ["отказ", marker])]
            ck = NetPanel(tmp, script, [])
            stop = threading.Event()
            threading.Timer(1.2, stop.set).start()
            started = time.time()
            res = rr.RouteRun(ck, str(proj), _sc("sync:confluence"), True, net="queue", stop=stop).run()
            took = time.time() - started
            runs = len(ck.calls)
            assert res["reason"] == "stopped" and runs <= 8 and took < 20, f"спор проверки и скрипта: {runs} запусков за {took:.1f} с, {res}"
    finally:
        net.close()
        restore()


# ------------------------------------------------------------------ расписание

@test
def test_cron_route_hands_its_deferred_steps_to_the_chain_so_other_projects_do_not_wait(tmp: Path):
    """Маршрут в цепочке расписания не ждёт связь сам: отложенное возвращается цепочке, остальные проекты идут, повтор — в конце."""
    restore = set_home(tmp / "home")
    proj, other = _project(tmp, "a"), _project(tmp, "b")
    projects = [{"name": "A", "path": str(proj)}, {"name": "B", "path": str(other)}]
    steps = [{"kind": "route", "project": str(proj), "route": "net", "write": True},
             {"kind": "command", "project": str(other), "cmd": "kb:lint", "args": []}]
    net = _Net(tmp, up={"v1": False})
    try:
        with _Fast():
            import shutil
            cron = importlib.import_module("cron")
            shutil.rmtree(cron.runs_dir(), ignore_errors=True)
            Path(cron.state_file()).unlink(missing_ok=True)
            from cases.part_18 import _task
            ck = NetPanel(tmp, _script(), [_sc("kb:lint", "sync:confluence", sid="net")])
            cron.save_tasks([_task(steps=steps)])
            sched = cron.Scheduler(ck, lambda: projects)
            seen: list = []

            def hook(cmd):
                seen.append((cmd, Path(ck.calls[-1][0]).parent.name if ck.calls else ""))
                if cmd == "kb:lint" and len(ck.calls) == 1:               # проект B дошёл раньше, чем вернулась связь у A
                    net.set(v1=True)
            ck.on_call = hook
            sched.enqueue("a1b2c3d4")
            _wait_cron(sched)
            run = cron.list_runs()[0]
            order = [(Path(c[0]).parent.name, c[1]) for c in ck.calls]
            assert order == [("a", "kb:lint"), ("b", "kb:lint"), ("a", "sync:confluence")], \
                f"проект B ждал связь A или отложенное не повторено: {order}"
            assert [i["status"] for i in run["items"]] == ["passed", "passed"] and run["items"][0].get("retry") and run["status"] == "passed", run["items"]
    finally:
        net.close()
        restore()


@test
def test_cron_script_refusal_on_the_retry_pass_is_final(tmp: Path):
    """Скрипт источника отказал по связи при коде 1 или 2: пункт откладывается; повторный отказ — итог «нет связи», не новый круг."""
    G = _g()
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    projects = [{"name": "A", "path": str(proj)}]
    steps = [{"kind": "command", "project": str(proj), "cmd": "sync:confluence", "args": []}]
    net = _Net(tmp)
    try:
        with _Fast():
            marker = G.net_marker([{"code": "profile_offline", "name": "VPN-1", "source": "Confluence"}], ["Confluence"])
            from cases.part_18 import _task
            script = _script()
            script["sync:confluence"] = [(2, ["отказ", marker])]
            cron, ck, sched = _net_scheduler(tmp, script, projects, _task(steps=steps), net)
            sched.enqueue("a1b2c3d4")
            _wait_cron(sched)
            run = cron.list_runs()[0]
            assert [c[1] for c in ck.calls] == ["sync:confluence", "sync:confluence"], f"повтор не состоялся или зациклился: {ck.calls}"
            assert [i["status"] for i in run["items"]] == ["offline"] and run["items"][0]["note"].startswith("net:"), run["items"]
    finally:
        net.close()
        restore()


# ------------------------------------------------------------------ панель и страница

@test
def test_panel_cancel_wins_over_a_returning_connection_and_a_waiting_job_has_a_pulse(tmp: Path):
    """«Прервать» в тот же миг, что вернулась связь, останавливает ожидание; пульс ожидающего задания — «ждёт», а не «процесс пропал»."""
    from harness import _cockpit_on
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    ck, restore = _cockpit_on(net.kit)
    G = _g()
    real = G.wait_restored
    try:
        job = {"id": "j1", "out": [], "cancel": threading.Event(), "started": time.time(), "last_out": time.time()}
        G.wait_restored = lambda *a, **k: (job["cancel"].set(), True)[1]
        assert ck.net_wait_for(job, str(proj), "sync:confluence") is False, "остановка проиграла возвращению связи"
        G.wait_restored = real
        waiting = {"id": "j2", "out": [], "cancel": threading.Event(), "started": time.time() - 600, "last_out": time.time() - 600,
                   "waiting": True, "project": str(proj), "cmd": "sync:confluence", "args": []}
        pulse = ck.job_pulse(waiting)
        assert pulse["alive"] is True and pulse["state"] == "wait", f"ожидающее задание названо пропавшим: {pulse}"
        gone = ck.job_pulse(dict(waiting, waiting=False))
        assert gone["state"] == "gone", "без ожидания и без процесса задание по-прежнему «пропало»"
    finally:
        G.wait_restored = real
        net.close()
        restore()


@test
def test_panel_route_dialog_skips_steps_a_resumed_route_has_done(tmp: Path):
    """Диалог связи при продолжении маршрута не перечисляет пройденные шаги."""
    from harness import _cockpit_on
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    ck, restore = _cockpit_on(net.kit)
    saved = (ck.scenarios, ck.all_scenarios, ck.command_by_name)
    ck.scenarios = lambda: [{"id": "update", "title": "Обновить", "when": "", "group": "база", "steps": [
        {"manual": False, "cmd": c, "why": "", "flags": []} for c in ("kb:lint", "sync:confluence", "kb:repair")]}]
    ck.all_scenarios = ck.scenarios
    ck.command_by_name = lambda name: {"runnable": True, "flags": [], "fixed_flags": [], "script": "x.py", "cmd": name, "ns": name.split(":")[0]}
    try:
        full = ck.net_check_route(str(proj), "update", True)
        assert [i["cmd"] for i in full["items"]] == ["sync:confluence"], full
        done = ck.net_check_route(str(proj), "update", True, {"skipSigs": ["sync:confluence"]})
        assert done["ok"] and done["items"] == [], f"пройденное шаг названо в диалоге: {done}"
    finally:
        ck.scenarios, ck.all_scenarios, ck.command_by_name = saved
        net.close()
        restore()


@test
def test_page_closes_the_net_dialog_by_escape_and_shows_a_partial_route(_t):
    """Страница: Escape и щелчок мимо окна — отмена, новый вопрос закрывает прежний, отказ снимает метку «идёт», частичный маршрут виден."""
    panel = (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    assert "if (NET_CLOSE) NET_CLOSE(null);" in panel and 'e.target.id==="netOverlay"' in panel, "диалог связи не закрывается Escape или щелчком"
    assert "if (NET_CLOSE) NET_CLOSE(null);\n  return new Promise" in panel, "новый вопрос не закрывает прежний"
    assert "if (res.cancelled){" in panel and 'net.cancelled' in panel, "после отмены метка «идёт» остаётся в консоли"
    assert "const part = !bad &&" in panel and "route.rc_partial" in panel and "route.toast_partial" in panel, "частичный маршрут страница не показывает"
    ru = json.loads((KIT / "cockpit" / "i18n" / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit" / "i18n" / "en.json").read_text(encoding="utf-8"))
    for key in ("route.passed_partly", "route.how_partial", "route.rc_partial", "route.toast_partial", "route.net_handed"):
        assert key in ru and key in en, key


# ------------------------------------------------------------------ записи

@test
def test_source_names_exclude_line_breaks_controls_and_comments_in_names_are_not_comments(tmp: Path):
    """Имя источника без разделителей строк и управляющих знаков (они рвут файл для других читалок); `#` внутри имени — не комментарий."""
    G = _g()
    for bad in ("a\u0085b", "a b", "a b", "a\x7fb", "a￾b", "a\ud800b", "a\tb", "a\nb", "", "  ", "x" * 121):
        assert not G.valid_source_id(bad), f"негодное имя принято: {bad!r}"
        out, errors = G.clean_access({bad: {"mode": "local"}})
        assert errors and errors[0]["code"] == "bad_source", (bad, errors)
    for good in ("Wiki (2)", "C# docs", "Документы", "a.b-c_d", "Web"):
        assert G.valid_source_id(good), good
    text = G.write_access("p: 1\n", {"C# docs": {"mode": "local"}, "Web": {"mode": "internet"}})
    assert G.parse_access(text)[0] == {"C# docs": {"mode": "local"}, "Web": {"mode": "internet"}}
    assert G.lossy_kinds(text) == [], f"ключ с решёткой принят за комментарий: {G.lossy_kinds(text)}"
    for commented in ('source_access:\n  # заметка\n  Web:\n    mode: "local"\n', 'source_access:\n  Web:  # свой\n    mode: "local"\n',
                      'source_access:\n  Web:\n    mode: "local" # потом\n'):
        assert G.lossy_kinds(commented) == ["comments"], (commented, G.lossy_kinds(commented))
    assert G.lossy_kinds('source_access:\n  "a # b":\n    mode: "local"\n') == [], "решётка в кавычках — не комментарий"
    proj = _project(tmp)
    res = G.save_access(str(proj), {"a b": {"mode": "local"}})
    assert not res["ok"] and res["code"] == "bad_source", res


@test
def test_copy_of_a_broken_models_file_is_private_from_the_first_byte_kept_three_deep_and_required(tmp: Path):
    """Копия битого файла с ключами создаётся закрытой; хранятся три свежие; не удалось сделать копию — файл не заменяется."""
    MC = importlib.import_module("model_config")
    AC = importlib.import_module("aurora_common")
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    path = MC.models_path(kit)
    real_slug, real_open = AC.utc_slug, os.open
    modes: list = []

    def spy_open(p, flags, mode=0o777, *a, **k):
        if ".broken-" in str(p):
            modes.append(mode)
        return real_open(p, flags, mode, *a, **k)
    os.open = spy_open
    try:
        for i in range(5):
            path.write_text('{"providers": [ "SECRET-%d"' % i, encoding="utf-8")
            AC.utc_slug = lambda i=i: f"t{i}"
            assert MC.save(kit, {"providers": []}, None, True)["ok"]
        copies = sorted(p.name for p in path.parent.glob("models.json.broken-*"))
        assert copies == ["models.json.broken-t2", "models.json.broken-t3", "models.json.broken-t4"], copies
        assert modes and set(modes) == {0o600}, f"копия создавалась с правами {modes}"
        # не удалось оставить копию — замена отменена, файл цел
        path.write_text('{"providers": [ "KEEP"', encoding="utf-8")
        before = path.read_bytes()

        def fail_open(p, flags, mode=0o777, *a, **k):
            if ".broken-" in str(p):
                raise OSError("нет места")
            return real_open(p, flags, mode, *a, **k)
        os.open = fail_open
        AC.utc_slug = lambda: "t9"
        res = MC.save(kit, {"providers": []}, None, True)
        assert not res["ok"] and res["code"] == "write" and path.read_bytes() == before, f"файл заменён без копии: {res}"
    finally:
        os.open, AC.utc_slug = real_open, real_slug


@test
def test_connectivity_save_keeps_a_copy_of_a_broken_file_and_reports_unwritable_text(tmp: Path):
    """Файл настройки соединений, который не разобрать: замена по согласию оставляет копию; текст, который не записать, — отказ с кодом."""
    CN = _cn()
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    path = kit / "local" / "connectivity.json"
    path.write_text('{"profiles": [ ', encoding="utf-8")
    before = path.read_bytes()
    res = CN.save(kit, {"enabled": False, "profiles": [], "probes": []})
    assert not res["ok"] and res["code"] == "broken_file" and path.read_bytes() == before
    assert CN.save(kit, {"enabled": False, "profiles": [], "probes": []}, replace_broken=True)["ok"]
    copies = list(path.parent.glob("connectivity.json.broken-*"))
    assert len(copies) == 1 and copies[0].read_bytes() == before, copies
    res = CN.save(kit, {"enabled": False, "profiles": [{"name": "VPN-1", "note": "a\ud800b"}], "probes": []})
    assert not res["ok"] and res["code"] in ("write", "bad_note", "bad_profile", "bad_config"), f"суррогат в тексте: {res}"
