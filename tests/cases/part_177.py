"""Проверки движка Aurora, часть 177: одно число команд на всю панель.

Каркас и помощники — tests/harness.py. Мостик, значок меню и «О ките» показывали 111 команд, а «Команды» — «из 110»:
каждый считал сам, и движковая `kit:skills` попадала в одно число и не попадала в другое. Иногда Мостик показывал и
70: реестр собирался, пока обновление кита переписывало `commands.txt` (git пишет VERSION раньше реестра), и этот
список жил в памяти панели до следующей смены версии. Теперь движковую команду и число команд определяет сервер, а
реестр пересобирается, когда меняется сам `commands.txt`. Реестр и кэш — во временной папке; кит не меняется.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

from harness import KIT, panel_sources, set_home, test

ROWS = ["kit  | kit:skills | — | скрипт | procedure.md | 1.0.0 | навыки агента",
        "kb   | kb:one     | — | модель | procedure.md | 1.0.0 | первая",
        "kb   | kb:two     | — | модель | procedure.md | 1.0.0 | вторая"]


def _modules():
    for p in (str(KIT / "scripts"), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("aurora_cockpit"), importlib.import_module("kit_commands")


class _Registry:
    """Реестр команд — из временного файла, кэш реестра — во временной папке; всё возвращается на место."""

    def __init__(self, ck, K, tmp: Path):
        self.ck, self.K, self.file = ck, K, tmp / "commands.txt"
        self.cache, self.stamp = tmp / "registry-cache.json", 1_900_000_000

    def write(self, rows: list) -> None:
        self.file.write_text("# реестр\n" + "\n".join(rows) + "\n", encoding="utf-8")
        self.stamp += 10                                   # правка — заметно позже прежней
        os.utime(self.file, (self.stamp, self.stamp))

    def __enter__(self):
        ck, K = self.ck, self.K
        self.keep = (K.REGISTRY, ck.REGISTRY_CACHE, K.read_registry)
        K.REGISTRY, ck.REGISTRY_CACHE = str(self.file), str(self.cache)
        ck.CACHE.pop("registry", None)
        return self

    def __exit__(self, *a):
        ck, K = self.ck, self.K
        K.REGISTRY, ck.REGISTRY_CACHE, K.read_registry = self.keep
        ck.CACHE.pop("registry", None)
        ck.CACHE.pop("registry_stamp", None)


@test
def test_the_panel_counts_commands_once_and_by_the_same_rule_as_the_list(tmp: Path):
    """Сервер помечает движковые команды (`engine`) и сам считает число команд человека: «О ките», ответ состояния
    (обе формы) и раздел «Команды» получают одно число — ровно столько строк, сколько в «Командах». У панели нет своего
    списка движковых команд, Мостик и значок меню берут число сервера, а не длину всего реестра."""
    ck, K = _modules()
    with _Registry(ck, K, tmp) as reg:
        reg.write(ROWS)
        rows = ck.registry()
        assert [(r["cmd"], r["engine"]) for r in rows] == [("kit:skills", True), ("kb:one", False), ("kb:two", False)]
        assert ck.registry_total() == 2 and ck.about()["commands"] == 2, ck.about()["commands"]
    ui = panel_sources()
    assert "const ENGINE_CMDS" not in ui and "isEngineCmd = r => !!(r && r.engine)" in ui, "у панели снова свой список"
    assert "metric(extra ? st.commands_total : waitMark()" in ui, "Мостик считает команды сам"
    assert 'setBadge("commands", S.state.commands_total)' in ui, "значок меню считает команды сам"
    assert "total: ctx.state.commands_total ?? all.length" in ui, "«Команды» показывают «из» своё число"
    assert "S.state.commands_total = st.commands_total" in ui, "смена языка теряет число команд"
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert src.count('"commands_total": registry_total()') == 2, "состояние отдаёт число команд не в обеих формах"
    assert '"commands_total": registry_total(), "extra": True' in src, \
        "полный ответ состояния не говорит, что реестр в нём, — плитки Мостика ждут его вечно"


@test
def test_the_registry_follows_the_commands_file_and_never_keeps_a_half_written_one(tmp: Path):
    """Обновление кита переписало `commands.txt`, а VERSION уже прежний по времени: панель собирает реестр заново
    (прежде старый список жил до следующей смены версии). Реестр, переписанный во время чтения, и пустой реестр
    отдаются, но не запоминаются — следующий запрос читает дописанный файл. Кэш на диске пишется целиком."""
    ck, K = _modules()
    with _Registry(ck, K, tmp) as reg:
        reg.write(ROWS[:2])
        assert ck.registry_total() == 1
        reg.write(ROWS)
        assert ck.registry_total() == 2, "реестр не заметил, что commands.txt переписан"
        read = reg.keep[2]
        reg.write(ROWS)                                    # обновление кита началось: реестр переписывается

        def torn(*args, **kwargs):                      # тот же вызов, что у настоящего (`with_dev=True` от панели)
            got = read(*args, **kwargs)
            reg.write(ROWS + ["kb | kb:three | — | модель | procedure.md | 1.0.0 | третья"])
            return got[:1]
        K.read_registry = torn
        assert len(ck.registry()) == 1, "недописанный реестр не отдан"
        assert "registry" not in ck.CACHE, "реестр, переписанный во время чтения, запомнен"
        K.read_registry = read
        assert ck.registry_total() == 3, "после дописанного файла реестр не собран заново"
        if reg.cache.exists():
            assert json.loads(reg.cache.read_text(encoding="utf-8"))["rows"], "кэш реестра записан не целиком"
        assert not list(tmp.glob("*.tmp")), "временный файл кэша остался"
        reg.file.unlink()
        assert ck.registry() == [] and "registry" not in ck.CACHE, "пустой реестр запомнен"


@test
def test_the_state_and_about_answers_carry_the_same_command_count(tmp: Path):
    """Ответы панели по HTTP: «О ките», вторая половина состояния и полное состояние несут одно число команд, равное
    числу не движковых строк реестра; полное состояние помечено как содержащее реестр."""
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    restore = set_home(tmp / "home")
    ck, K = _modules()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(tmp / "roots")]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def get(path):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=120)
        c.request("GET", path, headers={"X-Aurora-Token": ck.TOKEN})
        r = c.getresponse()
        return json.loads(r.read().decode("utf-8") or "{}")
    try:
        with _Registry(ck, K, tmp) as reg:
            reg.write(ROWS)
            extra, full, about = get("/api/state?part=extra"), get("/api/state"), get("/api/about")
            human = [r for r in extra["commands"] if not r["engine"]]
            assert extra["commands_total"] == full["commands_total"] == about["commands"] == len(human) == 2, \
                (extra.get("commands_total"), full.get("commands_total"), about.get("commands"))
            assert full.get("extra") is True and "commands_total" not in get("/api/state?part=core")
    finally:
        srv.shutdown()
        srv.server_close()
        restore()
