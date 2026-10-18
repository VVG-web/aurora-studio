"""Проверки движка Aurora, часть 51: панель различает неисправный Pydantic AI и обрыв связи.

Каркас и помощники — tests/harness.py. Движок пишет в `~/.aurora/adapter-alarm.json` тревоги двух
областей; сервер панели отдаёт их строками с областью и видом, страница рисует для каждой своё:
красную полосу «мимо Pydantic AI» с кнопками «Установка» или «Версия» — только для неисправного пути
Pydantic AI, а для обрыва связи — отдельную полосу с точным заголовком по виду и без единого слова
про Pydantic AI в кнопках. Сети и настоящего домашнего каталога проверки не касаются.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import json
import re
import shutil
import subprocess
import sys

from harness import KIT, SCRIPTS, set_home, test  # noqa: F401


def _mods():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("agent_core"), importlib.import_module("aurora_cockpit")


def _run_node(tmp: Path, name: str, code: str) -> dict:
    src = tmp / name
    src.write_text(code, encoding="utf-8")
    cp = subprocess.run([shutil.which("node"), str(src)], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip(), (cp.stderr or cp.stdout)[:1500]
    return json.loads(cp.stdout.strip().splitlines()[-1])


def _row(AG, why, **extra):
    row = why.row() if hasattr(why, "row") else {"why": why}
    row.update(at="2026-10-18T10:19:00Z", project="/work/PRJ-A", calls=3)
    row.update(extra)
    return row


@test
def test_the_panel_api_gives_the_area_and_kind_of_each_alarm(tmp: Path):
    """`adapter_alarm()` отдаёт по строке на тревогу: область (adapter | link), вид, подвид, лекарство
    (`fix`), подстановки и текст; строка прежнего вида читается как неисправность адаптера; текст
    переводится на английский на выходе сервера. Тревоги связи показывает та же точка, что и прежние."""
    AG, ck = _mods()
    restore = set_home(tmp / "home")
    try:
        assert ck.adapter_alarm() == []
        net = AG.Why("client_net", "tls", n=2, model="m-1", detail="")
        auth = AG.link_why("auth", 3, "m-2", 401)
        dns = AG.link_why("dns", 1, "m-3")
        AG._write_alarm({
            "agent:build": _row(AG, net, path="agent:build"),
            "bot:ревью": _row(AG, AG._adapter_gap(), path="bot:ревью"),
            "bot:старый": {"at": "2026-10-07T07:00:00Z", "why": "venv не установлен",
                           "project": "/x/PRJ", "calls": 1},
            "link:3": _row(AG, auth, path="bot:ревью"),
            "link:1": _row(AG, dns, path="agent:build"),
            "agent:venv": _row(AG, AG._venv_gap(), path="agent:venv")})
        rows = {r["path"] + "|" + r["area"]: r for r in ck.adapter_alarm()}
        assert set(rows) == {"agent:build|adapter", "bot:ревью|adapter", "bot:старый|adapter",
                             "bot:ревью|link", "agent:build|link", "agent:venv|adapter"}, set(rows)
        a = rows["agent:build|adapter"]
        assert (a["kind"], a["sub"], a["fix"]) == ("client_net", "tls", ""), a
        assert a["args"]["n"] == 2 and a["project"] == "PRJ-A" and a["calls"] == 3, a
        assert rows["bot:ревью|adapter"]["fix"] == "version" and rows["agent:venv|adapter"]["fix"] == "install"
        old = rows["bot:старый|adapter"]
        assert (old["kind"], old["fix"], old["why"]) == ("adapter_error", "install", "venv не установлен"), old
        auth_row = rows["bot:ревью|link"]
        assert (auth_row["kind"], auth_row["fix"], auth_row["args"]["status"]) == ("auth", "models", 401), auth_row
        dns_row = rows["agent:build|link"]
        assert (dns_row["kind"], dns_row["fix"], dns_row["args"]["n"]) == ("dns", "", 1), dns_row
        # на выходе сервера тексты переводятся по образцам; ни одной русской буквы в английском ответе
        en = ck.translate_payload({"alarm": ck.adapter_alarm()}, "en")["alarm"]
        for r in en:
            assert not re.search("[а-яА-ЯёЁ]", r["why"].replace("№", "")) or r["path"] == "bot:старый", r
        assert any("TLS" in r["why"] or "certificate" in r["why"] for r in en), en
        # забытая тревога связи (бэкенд убрали из настройки) через сутки не показывается; живая — показывается;
        # строка, испорченная вручную, панель не роняет
        old_at = "2026-10-01T10:00:00Z"
        AG._write_alarm({"link:9": _row(AG, AG.link_why("dns", 9, "m"), path="bot:давний", at=old_at),
                         "link:8": _row(AG, AG.link_why("dns", 8, "m"), path="bot:живой",
                                        at=AG.utc_stamp()),
                         "agent:плохой": {"at": "x", "why": 5, "kind": "dns", "args": "не словарь"}})
        paths = sorted(r["path"] for r in ck.adapter_alarm())
        assert "bot:давний" not in paths and "bot:живой" in paths, paths
    finally:
        AG._write_alarm({})
        restore()
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"adapter_alarm": adapter_alarm(),' in src and 'u.path == "/api/adapter/alarm"' in src
    assert 'self.send_json({"alarm": adapter_alarm()})' in src


BAR_HARNESS = r"""
let seq = 0, bars = {};
const t = (k, v) => k + (v ? JSON.stringify(v) : "");
const shown = [];
const show = (v) => shown.push(v);
const histWhen = (x) => "в " + x;
const el = (tag, attrs, ...kids) => {
  const n = {tag, attrs: attrs || {}, kids: kids.flat().filter(k => k !== null && k !== false && k !== undefined), id: ++seq};
  n.remove = () => { if (n.attrs.id && bars[n.attrs.id] === n) delete bars[n.attrs.id]; };
  return n;
};
const main = {prepend(...ns){ ns.forEach(n => { if (n.attrs.id) bars[n.attrs.id] = n; }); }};
const $ = (sel) => sel === "main" ? main : bars[sel.slice(1)] || null;
@BAR@
const text = (n) => !n ? "" : typeof n === "string" ? n : (n.kids || []).map(text).join(" ");
const buttons = (n) => !n || typeof n === "string" ? [] : [].concat(n.tag === "button" ? [n] : [], ...(n.kids || []).map(buttons));
const names = (n) => buttons(n).map(b => text(b));
const fails = [];
const check = (ok, what) => { if (!ok) fails.push(what); };
const ADAPTER = [
  {path: "agent:build", area: "adapter", kind: "venv", fix: "install", why: "venv python: ~/.aurora/venv/bin/python не найден", project: "PRJ-A", calls: 2, at: "2026-10-18T10:19:00Z", args: {}},
  {path: "bot:ревью", area: "adapter", kind: "adapter_file", sub: "project", fix: "version", why: "адаптер: .aurora/x не найден", project: "PRJ-A", calls: 1, at: "2026-10-18T10:19:00Z", args: {}},
  {path: "bot:сеть", area: "adapter", kind: "client_net", sub: "dns", fix: "", why: "HTTP-клиент адаптера не доходит", project: "PRJ-B", calls: 3, at: "2026-10-18T10:19:00Z", args: {n: 2, model: "m"}}];
const KINDS = %KINDS%;
const MODELS_FIX = %MODELS%;
const link = (kind, n) => ({path: "agent:build", area: "link", kind, fix: MODELS_FIX.includes(kind) ? "models" : "",
  why: "бэкенд №" + n + ": текст " + kind, project: "PRJ-A", calls: 3, at: "2026-10-18T10:19:00Z",
  args: {n, model: "m", status: kind === "auth" ? 401 : kind === "server_error" ? 502 : ""}});
(() => {
  // только неисправность адаптера: своя полоса, кнопки по лекарству (Установка | Версия), без кнопки у client_net
  drawAdapterAlarm(ADAPTER);
  check(!!bars.adapterbar && !bars.linkbar, "adapter: есть полоса адаптера и нет полосы связи");
  const bar = bars.adapterbar;
  check(text(bar).includes("alarm.adapter") && text(bar).includes("bot:сеть"), "adapter: заголовок и строки");
  check(JSON.stringify(names(bar)) === JSON.stringify(["alarm.adapter_engine", "alarm.adapter_open"]),
        "adapter: кнопки «Версия» и «Установка» по лекарствам, got " + JSON.stringify(names(bar)));
  buttons(bar).forEach(b => b.attrs.onclick());
  check(JSON.stringify(shown) === JSON.stringify(["version", "install"]), "adapter: кнопки ведут куда сказано");
  // расхождение клиента адаптера без лекарства из кнопок: кнопок нет вовсе
  drawAdapterAlarm([ADAPTER[2]]);
  check(names(bars.adapterbar).length === 0, "client_net: кнопок про Установку нет");
  // обрыв связи, каждый вид: своя полоса, заголовок по виду, ни слова про Pydantic AI в кнопках
  for (const kind of KINDS){
    shown.length = 0;
    drawAdapterAlarm([link(kind, 4)]);
    check(!!bars.linkbar && !bars.adapterbar, kind + ": полоса связи есть, полосы адаптера нет");
    const bar = bars.linkbar, txt = text(bar);
    check(txt.includes("alarm.link." + kind) && txt.includes('"n":4'), kind + ": заголовок по виду с номером бэкенда");
    check(!txt.includes("alarm.adapter"), kind + ": в полосе связи нет надписей про адаптер");
    const want = MODELS_FIX.includes(kind) ? ["alarm.link_models"] : [];
    check(JSON.stringify(names(bar)) === JSON.stringify(want), kind + ": кнопки " + JSON.stringify(names(bar)));
    check(!names(bar).some(n => n.includes("alarm.adapter")), kind + ": кнопки про Pydantic AI в полосе связи");
    check(text(bar).includes(MODELS_FIX.includes(kind) ? "alarm.link_tail_cfg" : "alarm.link_tail"), kind + ": хвост по лекарству");
    buttons(bar).forEach(b => b.attrs.onclick());
    check(shown.length === want.length && shown.every(v => v === "models"), kind + ": кнопка ведёт в «Модели»");
  }
  // обе беды сразу: две полосы, у каждой своё
  drawAdapterAlarm([ADAPTER[0], link("dns", 1), link("auth", 2)]);
  check(!!bars.adapterbar && !!bars.linkbar, "обе: две полосы");
  check(!text(bars.linkbar).includes("alarm.adapter") && !text(bars.adapterbar).includes("alarm.link"), "обе: тексты не смешаны");
  // тревог нет — полос нет; прежние убраны
  drawAdapterAlarm([]);
  check(!bars.adapterbar && !bars.linkbar, "пусто: обе полосы убраны");
  drawAdapterAlarm(null);
  check(!bars.adapterbar && !bars.linkbar, "null: полос нет");
  console.log(JSON.stringify({fails}));
})();
"""


@test
def test_the_panel_draws_the_right_bar_for_the_adapter_and_for_the_link(tmp: Path):
    """Страница: неисправный Pydantic AI — красная полоса с кнопками по лекарству («Установка», «Версия»;
    у расхождения клиента адаптера кнопок нет), обрыв связи — отдельная полоса с заголовком по виду и без
    кнопок про Pydantic AI (у ключа, 404 и срока — кнопка «Модели»); обе беды рисуются двумя полосами.
    Под node; без него — только ключи каталогов и текст страницы."""
    AG, _ck = _mods()
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    a = panel.index("function drawAdapterAlarm(rows){")
    func = panel[a:panel.index("async function refreshAdapterAlarm(){")]
    assert '"#linkbar"' in func and 't("alarm.link." + r.kind' in func
    link_part = func[func.index("if (link.length)"):func.index("if (adapter.length)")]
    assert "alarm.adapter" not in link_part, "в полосе связи надписи про адаптер"
    assert "drawAdapterAlarm(d.alarm)" in panel and "drawAdapterAlarm(S.state.adapter_alarm)" in panel
    kinds = sorted(AG.LINK_KINDS)
    models_fix = sorted(k for k in kinds if AG.CAUSES[k]["fix"] == "models")
    assert models_fix == ["auth", "not_found", "rate_limit", "silent"], models_fix
    if not shutil.which("node"):
        return
    code = BAR_HARNESS.replace("@BAR@", func).replace("%KINDS%", json.dumps(kinds)) \
        .replace("%MODELS%", json.dumps(models_fix))
    res = _run_node(tmp, "alarm_bars.mjs", code)
    assert res["fails"] == [], "\n".join(res["fails"])
