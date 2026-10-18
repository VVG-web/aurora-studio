"""Проверки движка Aurora, часть 67. Каркас и помощники — tests/harness.py.

Итог третьего просмотра (после переноса на мастер с движковым классификатором обрыва): один классификатор (переходник
`connectivity.classify_error` над `agent_core.link_kind`, отметка `conn` в `CAUSES`), метка `AURORA-NET` последней строкой
при выходе шага, карантин снимается лишь тот, что посажен за обрыв соединения, неверная проверка ручки не запирает роль,
зависшая проверка не множит потоки, раскладки конфига, которые блок не перепишет, шаги на прежних данных после
отложенного повторяются, причины на языке интерфейса, «Прервать» до рождения процесса.
Сети нет; кит, проект и домашняя папка — временные.
"""
from __future__ import annotations

import importlib
import io
import json
import socket
import ssl
import sys
import threading
import time
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

from harness import KIT, SCRIPTS, set_home, test  # noqa: F401

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(KIT / "cockpit"))

from cases.part_64 import NetPanel, _Fast, _Net, _project, _sc, _script  # noqa: E402
from cases.part_65 import MSGS, OK_BODY, _agent, _models_net  # noqa: E402


def _g():
    return importlib.import_module("conn_gate")


def _cn():
    return importlib.import_module("connectivity")


# ------------------------------------------------------------------ один классификатор

@test
def test_one_classifier_names_the_drop_and_the_probe_codes_come_from_it(tmp: Path):
    """`classify_error` — переходник над `agent_core.link_kind`; обрывы с отметкой `conn` в `CAUSES` — те, после которых
    спрашивают проверку соединений; коды результата probe получаются из видов движка."""
    CN = _cn()
    AG = importlib.import_module("agent_core")
    marked = {k for k, row in AG.CAUSES.items() if row.get("conn")}
    assert marked == AG.NET_KINDS == {"dns", "refused", "connect_timeout", "unreachable", "tls", "proxy", "reset"}, marked
    assert all(AG.CAUSES[k]["area"] == "link" for k in marked) and marked <= AG.LINK_ALARM_KINDS
    cases = [
        (socket.gaierror(-2, "Name or service not known"), "dns"),
        (ConnectionRefusedError(61, "Connection refused"), "refused"),
        (TimeoutError("timed out"), "timeout"),
        (OSError(113, "No route to host"), "unreachable"),
        (urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")), "refused"),
        (urllib.error.URLError(socket.gaierror(-3, "Temporary failure in name resolution")), "dns"),
        (ssl.SSLCertVerificationError("certificate verify failed"), "tls"),
        (ConnectionResetError(54, "Connection reset by peer"), "reset"),
        (ValueError("что-то своё"), "other"),
    ]
    for exc, code in cases:
        got = CN.classify_error(exc)
        assert got[0] == code, f"{type(exc).__name__}: ждали {code}, вышло {got}"
        assert AG.link_kind(exc) in ("", *AG.LINK_KINDS), "движок назвал вид, которого нет в таблице"
    assert CN.classify_error(ValueError("секрет-12345"))[1] == "ValueError", "в подробность попал текст чужой ошибки"
    src = (SCRIPTS / "connectivity.py").read_text(encoding="utf-8")
    assert "_ERRNO_" not in src and "gaierror" not in src.split("def classify_error")[1].split("def _result")[0], \
        "в connectivity остался собственный разбор ошибок"
    ru = json.loads((KIT / "cockpit" / "modules" / "connectivity" / "i18n" / "ru.json").read_text(encoding="utf-8"))
    en = json.loads((KIT / "cockpit" / "modules" / "connectivity" / "i18n" / "en.json").read_text(encoding="utf-8"))
    assert "connectivity.code.reset" in ru and "connectivity.code.reset" in en and "reset" in CN.CODE_TEXT


# ------------------------------------------------------------------ метка отказа

@test
def test_marker_is_printed_last_at_exit_and_only_when_the_blocked_state_still_stands(tmp: Path):
    """Метка `AURORA-NET` — последней строкой при выходе шага и только если после отказа ни один вызов не прошёл; код не ноль;
    разбор берёт метку из хвоста вывода, а не из любого места."""
    AG = _agent()
    AR = importlib.import_module("agent_runner")
    G = _g()
    net, cfg = _models_net(tmp, up={"v1": False, "v2": False})
    calls: list = []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        calls.append(b["n"])
        return (200, OK_BODY, "", 0.1)
    try:
        AG.NET_BLOCKED["row"] = None
        assert AR._net_exit(0) == 0 and AR._net_exit(1) == 1, "без отказа по связи код выхода тронут"
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert not r["ok"] and r["link"]["why"] and r["net_blocked"] and AG.NET_BLOCKED["row"]["code"] == "llm_down", r
        bots = importlib.import_module("bots")
        assert bots._model_problem(r)["code"] == "link_down", f"отказ по связи назван «модель не ответила»: {bots._model_problem(r)}"
        out = io.StringIO()
        with redirect_stdout(out):
            rc = AR._net_exit(0)
        lines = out.getvalue().splitlines()
        assert rc == 2 and len(lines) == 1 and lines[0].startswith(G.NET_MARK), (rc, lines)
        assert G.parse_marker(["шум"] * 5 + lines)["blocked"][0]["profiles"] == ["VPN-1", "VPN-2"]
        # метка далеко в начале вывода, а дальше много другого — это не отказ этого шага
        assert G.parse_marker(lines + ["строка"] * 70) == {}, "метка из далёкого начала вывода принята за итог"
        # связь вернулась и вызов прошёл — запись снята
        net.set(v1=True, v2=True)
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and AG.NET_BLOCKED["row"] is None and AR._net_exit(0) == 0
    finally:
        AG.NET_BLOCKED["row"] = None
        net.close()


# ------------------------------------------------------------------ роутер

@test
def test_only_a_connection_quarantine_is_lifted_when_the_link_returns(tmp: Path):
    """Серверная ошибка 5xx, посадившая провайдера в карантин, остаётся после возвращения связи; карантин за отказ соединения снят."""
    AG = _agent()
    AG._NET_SEEN.clear()
    net, cfg = _models_net(tmp)
    try:
        why = AG.link_why("server_error", 1, "m1", 503)
        AG.DOWN[1] = time.time() + 900
        AG.DOWN_WHY[1] = (AG.DOWN[1], why)
        net.set(v1=False)
        calls: list = []

        def transport(kind, b, payload, timeout):
            if kind == "slots":
                return (404, None, "нет /slots", 0.0)
            calls.append(b["n"])
            return (200, OK_BODY, "", 0.1)
        AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        net.set(v1=True)
        calls.clear()
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert 1 in AG.DOWN and calls == [2], f"серверный карантин снят вместе со связью: {AG.DOWN} {calls}"
        assert not any("карантин за обрыв" in line for line in r["log"]), r["log"]
    finally:
        AG.DOWN.clear()
        AG.DOWN_WHY.clear()
        AG._NET_SEEN.clear()
        net.close()


@test
def test_a_failing_provider_handle_check_alone_does_not_lock_the_role(tmp: Path):
    """Не прошла только проверка ручки (профили и интернет в порядке): вызовы пробуются; ожидание прогона её не держит."""
    AG = _agent()
    G = _g()
    CN = _cn()
    MC = importlib.import_module("model_config")
    data = {"providers": [{"id": "gw-a", "name": "GWA", "type": "openai", "url": "http://127.0.0.1:1/v1", "key": "k", "vpn": "VPN-1",
                           "health": {"check": "reach", "path": "/nope"}}],
            "capabilities": {"llm": {"roles": [{"id": "worker", "backends": [{"provider": "gw-a", "model": "m1"}]}]}}}
    provs = CN.providers_of(None, MC.normalize(data)[0])
    net = _Net(tmp, providers=provs)
    assert MC.save(net.kit, data)["ok"]
    cfg = MC.to_config(MC.load(net.kit))
    calls: list = []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        calls.append(b["n"])
        return (200, OK_BODY, "", 0.1)
    try:
        net.special["provider:gw-a"] = {"ok": False, "code": "status", "ms": 3, "detail": "HTTP 404"}      # ручка не отвечает, как задумано
        for _ in range(2):
            net.set()
        assert G.providers_down(net.svc, fresh=True)["gw-a"]["why"] == "provider_check", "ручка не признана недоступной"
        assert G.llm_outage(net.svc, fresh=True) == {}, "неверная проверка ручки держит ожидание прогона"
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and calls == [1], f"роль заперта проверкой ручки: {r}"
        assert any("пробую вызовы" in line for line in r["log"]), r["log"]
        # а лежащий профиль по-прежнему останавливает
        net.set(v1=False)
        calls.clear()
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert not r["ok"] and r.get("net_blocked") and calls == [], r
    finally:
        net.close()


@test
def test_a_connection_failure_the_check_explains_does_not_grow_the_alarm_streak(tmp: Path):
    """Обрыв, который объяснила проверка соединений, серию неудач для тревоги связи не растит."""
    AG = _agent()
    refused = "ConnectionRefusedError: [Errno 111] Connection refused"
    net, cfg = _models_net(tmp)

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        if b["n"] == 1:
            net.up["v1"] = False
            return (None, None, refused, 0.01)
        return (200, OK_BODY, "", 0.1)
    try:
        AG.LINK_RUN.clear()
        r = AG.call_role(cfg, "worker", MSGS, transport=transport, deadline=time.time() + 30, sleep=lambda s: None)
        assert r["ok"] and 1 not in AG.LINK_RUN, f"объяснённый обрыв растит серию тревоги: {AG.LINK_RUN}"
    finally:
        AG.LINK_RUN.clear()
        AG.DOWN.clear()
        net.close()


# ------------------------------------------------------------------ проверка: потоки, подписи, состояние

@test
def test_a_hung_probe_neither_multiplies_threads_nor_stalls_a_crowd_of_waiters(tmp: Path):
    """Зависшая проверка адреса: второй поток на тот же адрес не заводится; «свежая» перепроверка не чаще раза в несколько секунд."""
    CN, G = _cn(), _g()
    release = threading.Event()
    started: list = []

    def hang(p, timeout):
        started.append(p["id"])
        release.wait(5)
        return {"ok": True, "code": "ok", "ms": 1, "detail": ""}
    cfg = {"settings": {"timeout": 1}, "profiles": [{"name": "VPN-1"}]}
    probes = [{"id": "v1", "profile": "VPN-1", "type": "tcp", "host": "127.0.0.1", "port": 1}]
    try:
        first = CN.run_probes(probes, cfg, budget=0.6, checkers={"tcp": hang})
        again = CN.run_probes(probes, cfg, budget=0.6, checkers={"tcp": hang})
        assert first["v1"].get("late") and again["v1"].get("late"), (first, again)
        assert started == ["v1"], f"на висящий адрес заведено потоков: {len(started)}"
    finally:
        release.set()
        time.sleep(0.1)
    net, _cfg = _models_net(tmp)
    try:
        base = net.probes
        net.clock.now += 400
        for _ in range(5):
            G._DOWN.update(at=0.0, key=None, val={})
            G.providers_down(net.svc, fresh=True)
        assert net.probes - base <= 3, f"свежая перепроверка пошла {net.probes - base} раз подряд (профилей три)"
    finally:
        net.close()


@test
def test_shared_state_keeps_the_entries_of_another_kit_until_they_are_old(tmp: Path):
    """Файл состояния общий на машину: запись чужого профиля не стирается раундом этого кита; давно не обновлявшаяся — уходит."""
    CN = _cn()
    now = 1_800_000_000.0
    iso = lambda ts: CN.utc_stamp(ts)                                                        # noqa: E731
    state = {"version": 1, "events": [],
             "profiles": {"VPN-B": {"state": "online", "round_at": iso(now - 3600)}, "OLD": {"state": "online", "round_at": iso(now - 30 * 86400)}},
             "probes": {"b1": {"profile": "VPN-B", "at": iso(now - 3600), "ok": True}, "o1": {"profile": "OLD", "at": iso(now - 30 * 86400), "ok": True}}}
    CN.prune_state(state, {"profiles": [{"name": "VPN-A"}]}, (), now)
    assert "VPN-B" in state["profiles"] and "b1" in state["probes"], "чужой профиль стёрт свежим раундом"
    assert "OLD" not in state["profiles"] and "o1" not in state["probes"], "давно не обновлявшаяся запись осталась"


@test
def test_surrogates_unsupported_layouts_and_corrupt_old_files_end_in_a_coded_refusal(tmp: Path):
    """Одинокий суррогат в тексте настройки не рвёт запись; раскладки конфига, которые блок не перепишет, — отказ `unsupported`;
    старый файл с испорченными типами не роняет запись."""
    CN, G = _cn(), _g()
    MC = importlib.import_module("model_config")
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    res = CN.save(kit, {"enabled": False, "profiles": [{"name": "VPN-1", "note": "a\ud800b"}], "probes": []})
    assert res["ok"], res
    saved = json.loads((kit / "local" / "connectivity.json").read_text(encoding="utf-8"))
    assert "\ud800" not in saved["profiles"][0]["note"], "суррогат записан"
    assert not CN.valid_url("https://example.com/a\ud800")
    # порча типов в прежних файлах: запись проходит, а не обрывается
    (kit / "local" / "connectivity.json").write_text(json.dumps({"enabled": False, "profiles": 5, "probes": True, "x": 1}), encoding="utf-8")
    assert CN.save(kit, {"enabled": False, "profiles": [], "probes": []})["ok"], "испорченные типы старого файла уронили запись"
    (kit / "local" / "models.json").write_text(json.dumps({"providers": 5, "capabilities": {"llm": {"roles": 5}}, "settings": 3}), encoding="utf-8")
    page = {"providers": [{"id": "gw", "name": "GW", "type": "openai", "url": "https://llm.example.com/v1", "key": "k"}],
            "capabilities": {"llm": {"roles": [{"id": "worker", "backends": [{"provider": "gw", "model": "m"}]}]}}}
    assert MC.save(kit, page)["ok"], "испорченные типы старого models.json уронили запись"
    # раскладки блока
    base = "project:\n  name: T\n"
    cases = {"flow": base + "source_access: {\n  Web: {mode: local}\n}\n", "anchor": base + "source_access: &sa\n  Web:\n    mode: local\n",
             "documents": base + "source_access:\n  Web:\n    mode: local\n...\n", "scalar": base + "source_access: |\n  text\n"}
    for kind, text in cases.items():
        want = {"flow": "flow", "anchor": "anchor", "documents": "documents", "scalar": "anchor"}[kind]
        assert G.unsupported_layout(text) == want, (kind, G.unsupported_layout(text))
    assert G.unsupported_layout(base + "source_access: {Web: {mode: local}}\n") == "", "однострочная запись в скобках — это «потеря», не отказ"
    assert G.unsupported_layout("---\n" + base + "source_access:\n  Web:\n    mode: local\n") == "", "начало документа ---"
    proj = _project(tmp)
    cfg = proj / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "source_access: &sa\n  Web:\n    mode: local\n", encoding="utf-8")
    keep = cfg.read_bytes()
    res = G.save_access(str(proj), {"Web": {"mode": "internet"}}, replace_lossy=True)
    assert not res["ok"] and res["code"] == "unsupported" and cfg.read_bytes() == keep, res


@test
def test_source_names_that_clash_with_object_properties_are_plain_keys_on_the_page(_t):
    """Страница: блок режимов — объект без прототипа (`constructor`, `__proto__` — обычные имена); `--help` скрипта сети не требует."""
    panel = (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    assert "Object.assign(Object.create(null)" in panel and "ACC = fresh(d.explicit)" in panel, "ACC — обычный объект с прототипом"
    assert "unsupported: () => t(\"access.err.unsupported\")" in panel
    SC = importlib.import_module("sources_core")
    argv = sys.argv
    sys.argv = ["confluence_export.py", "--help"]
    try:
        assert SC.net_refusal() == "", "справка проверяет связь"
    finally:
        sys.argv = argv


# ------------------------------------------------------------------ маршрут и расписание

@test
def test_route_reasons_come_in_the_language_of_the_interface(tmp: Path):
    """Причина «связи не хватает» в журнале маршрута и в метке ожидания — словами каталога интерфейса (английский тоже)."""
    rr = importlib.import_module("route_runner")
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    try:
        ck = NetPanel(tmp, _script(), [])
        en = json.loads((KIT / "cockpit" / "i18n" / "en.json").read_text(encoding="utf-8"))
        ck.strings = dict(en)
        logs: list = []
        res = rr.RouteRun(ck, str(proj), _sc("kb:lint", "sync:confluence"), True, net="partial", lang="en", log=logs.append).run()
        text = "\n".join(logs)
        assert "VPN-1 is unavailable" in text and "недоступен" not in text.split("Skipped")[0], text
        assert res["net_skipped"] == ["sync:confluence"]
        res = rr.RouteRun(NetPanel(tmp, _script(), []), str(proj), _sc("sync:confluence"), True, net="fail_fast").run()
        assert "VPN-1" in res["note"] and "sync:confluence" in res["note"], res["note"]
    finally:
        net.close()
        restore()


@test
def test_steps_after_a_deferred_one_are_redone_so_a_partial_route_never_passes_for_complete(tmp: Path):
    """Отложенный синк выполнен в конце, а разбор после него шёл на старом зеркале: маршрут частичный, шаги после отложенного
    назначены к повтору (не «сделано»), «Продолжить» и расписание проходят их заново."""
    rr = importlib.import_module("route_runner")
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    net = _Net(tmp, up={"v1": False})
    try:
        with _Fast():
            ck = NetPanel(tmp, _script(), [])
            ck.on_call = lambda cmd: net.set(v1=True) if cmd == "kb:lint" else None
            res = rr.RouteRun(ck, str(proj), _sc("sync:confluence", "kb:repair", "kb:lint", sid="update"), True).run()
            assert [c[1] for c in ck.calls] == ["kb:repair", "kb:lint", "sync:confluence"], ck.calls
            assert res["ok"] and res["partial"] is True and set(res["redo"]) == {"kb:repair --apply", "kb:lint --apply"}, res
            assert isinstance(ck.route_state, dict) and "kb:repair --apply" not in ck.route_state["done"] \
                and "sync:confluence --apply" in ck.route_state["done"], f"состояние не назначило повтор: {ck.route_state}"
            assert not list(Path(proj).rglob("kb_updated.json")), "база помечена обновлённой по шагам на старых данных"
            # «Продолжить»: по состоянию проходятся ровно шаги после отложенного
            net.set(v1=True)
            ck = NetPanel(tmp, _script(), [])
            res = rr.RouteRun(ck, str(proj), _sc("sync:confluence", "kb:repair", "kb:lint", sid="update"), True,
                              resume={"skipSigs": ["sync:confluence --apply"]}).run()
            assert [c[1] for c in ck.calls] == ["kb:repair", "kb:lint"] and res["partial"] is False and res["ok"], (res, ck.calls)
            assert ck.route_state == "cleared"
    finally:
        net.close()
        restore()


@test
def test_cron_removes_redone_steps_from_the_done_list_of_a_deferred_route(tmp: Path):
    """Маршрут в цепочке расписания отдал отложенное: шаги после него убраны из «сделанного», повтор пройдёт их заново."""
    from cases.part_18 import _task
    from cases.part_64 import _wait_cron
    restore = set_home(tmp / "home")
    proj = _project(tmp)
    projects = [{"name": "A", "path": str(proj)}]
    steps = [{"kind": "route", "project": str(proj), "route": "net", "write": True}]
    net = _Net(tmp, up={"v1": False})
    try:
        with _Fast():
            import shutil
            cron = importlib.import_module("cron")
            shutil.rmtree(cron.runs_dir(), ignore_errors=True)
            Path(cron.state_file()).unlink(missing_ok=True)
            ck = NetPanel(tmp, _script(), [_sc("sync:confluence", "kb:repair", sid="net")])
            ck.on_call = lambda cmd: net.set(v1=True) if cmd == "kb:repair" else None
            cron.save_tasks([_task(steps=steps)])
            sched = cron.Scheduler(ck, lambda: projects)
            sched.enqueue("a1b2c3d4")
            _wait_cron(sched)
            order = [c[1] for c in ck.calls]
            assert order == ["kb:repair", "sync:confluence", "kb:repair"], f"шаг после отложенного не повторён: {order}"
            run = cron.list_runs()[0]
            assert run["items"][0]["status"] == "passed" and run["items"][0].get("retry"), run["items"]
    finally:
        net.close()
        restore()


@test
def test_stop_pressed_before_the_process_is_born_cancels_the_job(tmp: Path):
    """«Прервать» до рождения процесса (ожидание кончилось, Popen ещё нет) запоминается: команда не стартует."""
    from harness import _cockpit_on
    proj = _project(tmp)
    net = _Net(tmp)
    ck, restore = _cockpit_on(net.kit)
    marker = tmp / "ran.txt"
    script = tmp / "x.py"
    script.write_text(f"open({str(marker)!r}, 'w').write('1')\n", encoding="utf-8")
    row = {"runnable": True, "flags": [], "fixed_flags": [], "script": "x.py", "cmd": "kb:lint", "ns": "kb"}
    saved = (ck.command_by_name, ck.script_path, ck.write_runlog, ck.mark_running, ck.engine_fallback_note)
    ck.command_by_name = lambda name: row
    ck.script_path = lambda project, name: str(script)
    ck.write_runlog = lambda *a, **k: None
    ck.mark_running = lambda *a, **k: None
    gate = threading.Event()

    def slow_note(*a, **k):
        gate.wait(5)                       # процесс «рождается»: до Popen остаётся окно
        return ""
    ck.engine_fallback_note = slow_note
    try:
        jid = ck.start_job(str(proj), "kb:lint", [])
        job = ck.JOBS[jid]
        time.sleep(0.2)
        assert "proc" not in job and ck.stop_job(jid).get("ok"), "остановка до рождения процесса отвергнута"
        gate.set()
        end = time.time() + 20
        while not job["done"] and time.time() < end:
            time.sleep(0.05)
        assert job["done"] and job["rc"] == -15 and not marker.exists(), f"команда запущена после остановки: {job}"
    finally:
        gate.set()
        ck.command_by_name, ck.script_path, ck.write_runlog, ck.mark_running, ck.engine_fallback_note = saved
        net.close()
        restore()


@test
def test_agent_run_wait_has_one_total_limit_not_one_per_wait(tmp: Path):
    """Не в режиме «до конца»: предел ожидания связи общий на все паузы (по числу уже потраченных), а не заново на каждую."""
    AR = importlib.import_module("agent_runner")
    G = _g()
    net, _cfg = _models_net(tmp, up={"v1": False, "v2": False})
    caps: list = []
    real = G.wait_llm_back
    G.wait_llm_back = lambda cap, **k: caps.append(cap - time.time()) or True
    try:
        AR.wait_for_link(time.time() + 10 ** 6, 0)
        AR.wait_for_link(time.time() + 10 ** 6, AR.OFFLINE_TRIES - 1)
        AR.wait_for_link(time.time() + 10 ** 6, AR.OFFLINE_TRIES + 5)
        full, last, over = caps
        assert abs(full - AR.OFFLINE_WAIT * AR.OFFLINE_TRIES) < 5 and abs(last - AR.OFFLINE_WAIT) < 5 and abs(over - AR.OFFLINE_WAIT) < 5, caps
    finally:
        G.wait_llm_back = real
        net.close()


@test
def test_a_denied_lock_create_is_retried_as_a_file_pending_delete(tmp: Path):
    """Windows: замок, снятый секунду назад, ещё «ждёт удаления» (держит дескриптор сканер или соседний
    процесс), и создание поверх него отказывает «Отказано в доступе». `lock_take` ждёт и берёт замок
    (на CI тест «замок не ломается под новым владельцем» падал именно так); настоящий запрет на запись
    поднимается после ~1 с, а не глотается."""
    import os
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    AC = importlib.import_module("aurora_common")
    target = str(tmp / "state" / "denied.lock")
    real_open, real_sleep = os.open, time.sleep
    calls = {"n": 0}
    try:
        def flaky(path, flags, *a, **k):
            if str(path) == target and calls["n"] < 3:
                calls["n"] += 1
                raise PermissionError(13, "Отказано в доступе")
            return real_open(path, flags, *a, **k)
        os.open = flaky
        time.sleep = lambda s: None
        ok, held = AC.lock_take(target, {"who": "test"})
        assert ok and held == {} and calls["n"] == 3, (ok, held, calls)
        assert (tmp / "state" / "denied.lock").is_file()
        # постоянный запрет — не молчание: после ~1 с ожидания ошибка поднимается
        os.remove(target)
        calls["n"] = 0

        def always(path, flags, *a, **k):
            if str(path) == target:
                calls["n"] += 1
                raise PermissionError(13, "Отказано в доступе")
            return real_open(path, flags, *a, **k)
        os.open = always
        try:
            AC.lock_take(target)
        except PermissionError:
            assert 40 <= calls["n"] <= 60, calls
        else:
            raise AssertionError("постоянный отказ в доступе проглочен")
    finally:
        os.open, time.sleep = real_open, real_sleep
