"""Проверки движка Aurora, часть 50: обрыв связи — не поломка Pydantic AI.

Каркас и помощники — tests/harness.py. 18.10.2026 два вызова модели получили от адаптера «нет
соединения: Connection error.» (сеть мигнула), контрольный прямой запрос прошёл — и панель
показала красную полосу «Pydantic AI выбран, но вызовы идут мимо него» при исправном Pydantic AI;
вызов с инструментами при этом отказывали словами «Pydantic AI не сработал». Здесь проверяется, что
обрыв связи называется своим именем (DNS, отказ порта, таймаут, сертификат, прокси, сброс, 5xx,
ключ…), что тревога про Pydantic AI поднимается только при неисправности самого пути Pydantic AI
(или повторяемом расхождении «адаптерный клиент не доходит, а прямой доходит»), а у связи своя
тревога: на третьей неудаче подряд на одном бэкенде и до первого ответа. Сети, моделей и
настоящего домашнего каталога проверки не касаются.
"""
from __future__ import annotations

from pathlib import Path
import errno
import importlib
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import textwrap
import time

from harness import KIT, SCRIPTS, live_adapter_python, set_home, test  # noqa: F401

PLAIN = {"choices": [{"message": {"content": "ответ"}, "finish_reason": "stop"}],
         "usage": {"completion_tokens": 3}}
VIA = {"choices": [{"message": {"content": "ответ"}, "finish_reason": "stop"}],
       "usage": {"completion_tokens": 3}, "aurora": {"via": "pydantic_ai", "tools_called": []}}


def _ag():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("agent_core")


def _adapter():
    p = str(SCRIPTS / "agents")
    if p not in sys.path:
        sys.path.insert(0, p)
    return importlib.import_module("pydantic_ai_adapter")


class _Env:
    """Переменные окружения на время проверки; всё возвращается на место."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _World:
    """Всё, что проверки подменяют в движке, и возврат на место: домашняя папка, папка прогона,
    транспорт адаптера и прямой HTTP, счётчики процесса. Настоящий `~/.aurora` не трогается."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.AG = _ag()

    def __enter__(self):
        AG = self.AG
        self.restore_home = set_home(self.tmp / "home")
        (self.tmp / "run").mkdir(parents=True, exist_ok=True)
        self.env = _Env(AURORA_RUN_DIR=str(self.tmp / "run"), AURORA_AGENT_CACHE="0")
        self.env.__enter__()
        self.saved = {n: getattr(AG, n) for n in ("pydantic_transport", "http_json")}
        self.dicts = {n: dict(getattr(AG, n)) for n in ("ADAPTER", "USAGE", "RUN_TASK", "FAILS")}
        self.sets = {n: set(getattr(AG, n)) for n in ("_BYPASS_SAID", "_LINK_SAID", "_RETRY_SAID")}
        self.reset()
        return self

    def reset(self):
        AG = self.AG
        for d in (AG.DOWN, AG.DOWN_WHY, AG.SLOW, AG.LAST_OK, AG.COOL, AG.COOL_HITS, AG.ERR_HITS,
                  AG.LINK_RUN, AG.GAP_RUN, AG._PROBE, AG._BYPASS_SAID, AG._LINK_SAID, AG._RETRY_SAID):
            d.clear()
        AG.USAGE.update(bypass=0, bypass_why="", link_retried=0, link_retried_kind="", link_failed=0,
                        link_why="", calls=0, failed=0, errors={})
        AG.ADAPTER.update(name="pydantic_ai", fallback_why="")
        AG.RUN_TASK["name"] = "agent:test"
        AG._write_alarm({})

    def __exit__(self, *a):
        AG = self.AG
        for n, v in self.saved.items():
            setattr(AG, n, v)
        for n, v in self.dicts.items():
            getattr(AG, n).clear()
            getattr(AG, n).update(v)
        for n, v in self.sets.items():
            getattr(AG, n).clear()
            getattr(AG, n).update(v)
        for d in (AG.DOWN, AG.DOWN_WHY, AG.SLOW, AG.LAST_OK, AG.COOL, AG.COOL_HITS, AG.ERR_HITS,
                  AG.LINK_RUN, AG.GAP_RUN, AG._PROBE):
            d.clear()
        self.env.__exit__()
        self.restore_home()

    def cfg(self, n: int = 1):
        env = {}
        for i in range(1, n + 1):
            env[f"AURORA_AGENT_BACKEND_{i}_URL"] = f"http://gw{i}.example.com/v1"
            env[f"AURORA_AGENT_BACKEND_{i}_MODEL"] = "model-x"
        cfg = self.AG.parse_config(env)
        assert cfg["adapter"] == "pydantic_ai"
        return cfg

    def failures(self) -> list:
        path = self.tmp / "run" / self.AG.FAIL_FILE
        return ([json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
                if path.exists() else [])

    def ask(self, cfg=None, tools=False, keep_down=False, **kw):
        # Каждый вызов начинается «спустя пятнадцать минут»: карантин прошлого отказа вышел, иначе
        # кольцо пропустило бы бэкенд и транспорт не позвали бы вовсе (`keep_down` — не выпускать).
        if not keep_down:
            self.AG.DOWN.clear()
        return self.AG.call_role(cfg or self.cfg(), "worker", [{"role": "user", "content": "вопрос"}],
                                 sleep=self.AG.NO_WAIT, deadline=time.time() + 30, tools=tools, **kw)


def _drop(kind="connect_timeout", proxy_fell=""):
    """Ответ адаптера «не дошли до сервера» — как его строит `pydantic_transport`."""
    AG = _ag()
    link = {"kind": kind, "proxy_fell": proxy_fell, "raw": "Connection error."}
    return None, None, AG.AdapterFail(AG.ADAPTER_FAIL + AG.ADAPTER_NO_CONNECT + ": Connection error.",
                                      link=link), 0.01


def _direct(calls, reached=True, probe_error="URLError: <urlopen error [Errno 61] Connection refused>"):
    """`http_json` движка: GET без тела — проверка достижимости (и `/slots`), запрос с телом — вызов модели.
    Всё, что движок спросил, ложится в `calls` как (адрес, тело, ключ)."""
    def fn(url, body, key, timeout):
        calls.append((url, body, key))
        if body is None:
            return (404, None, "HTTP 404", 0.01) if reached else (None, None, probe_error, 0.01)
        return 200, json.loads(json.dumps(PLAIN)), "", 0.1
    return fn


def _chat(calls):
    """Запросы с телом — то есть настоящие вызовы модели напрямую."""
    return [c for c in calls if c[1] is not None]


# ------------------------------------------------------------------ (а) одиночный обрыв

@test
def test_a_one_off_adapter_drop_is_not_a_pydantic_ai_alarm(tmp: Path):
    """Адаптер не дошёл до шлюза один раз: прямой HTTP НЕ вызывается (обрыв им не лечится), движок проверяет
    шлюз сам (GET без тела и без ключа), повторяет вызов через адаптер — тот проходит. Красной полосы про
    Pydantic AI нет, в итоге прогона — сведение «обрыв связи … это не поломка Pydantic AI», в журнале
    сбоев — этап «обрыв связи», а не «мимо Pydantic AI»."""
    import run_summary as RS
    with _World(tmp) as w:
        AG = w.AG
        calls, attempts = [], []
        AG.pydantic_transport = lambda b, p, t: (attempts.append(1) or (
            _drop("connect_timeout") if len(attempts) == 1 else (200, json.loads(json.dumps(VIA)), "", 0.1)))
        AG.http_json = _direct(calls)
        r = w.ask()
        assert r["ok"] and r["via"] == "pydantic_ai" and r["bypass"] == "", r
        assert len(attempts) == 2, "повтор через адаптер не сделан"
        assert not _chat(calls), f"при обрыве связи ушёл прямой запрос на чат: {_chat(calls)}"
        probes = [c for c in calls if c[0].endswith("/models")]
        assert len(probes) == 1 and probes[0][1] is None and probes[0][2] == "", probes
        assert AG.read_alarm() == {}, f"единичный обрыв поднял тревогу: {AG.read_alarm()}"
        assert AG.USAGE["bypass"] == 0 and AG.USAGE["link_retried"] == 1, AG.USAGE
        assert AG.ADAPTER["fallback_why"] == "", "обрыв записан как причина обхода"
        recs = w.failures()
        assert [x["stage"] for x in recs] == ["обрыв связи"], recs
        assert recs[0]["kind"] == "connect_timeout" and "не поломка Pydantic AI" in recs[0]["why"], recs
        assert "повторён" in recs[0]["why"] and "прямым" not in recs[0]["why"], recs
        assert not any(x["stage"] == "мимо Pydantic AI" for x in recs), recs
        lines = RS.render(RS.from_agent([], AG.USAGE, 1.0, None))
        sentence = next(x for x in lines if "Обрыв связи" in x)
        assert "1 вызовов не дошли до шлюза (таймаут соединения) и были повторены" in sentence, sentence
        assert "это не поломка Pydantic AI" in sentence, sentence
        assert not any("Мимо Pydantic AI" in x or "мимо Pydantic AI" in x for x in lines), lines
        # сведение, а не ошибка: ошибок в итоге нет
        assert lines[-1].strip() == "Ошибки: не было", lines
        # по-английски — то же сведение
        en = RS.render(RS.from_agent([], AG.USAGE, 1.0, None), lang="en")
        assert any("did not reach the gateway (connection timeout)" in x and "not a Pydantic AI fault" in x
                   for x in en), en
        assert w.ask()["via"] == "pydantic_ai" and AG.read_alarm() == {}


# ------------------------------------------------------------------ (б) повторяемое расхождение

@test
def test_three_adapter_gaps_with_a_live_check_name_the_adapter_client_and_a_success_clears(tmp: Path):
    """Адаптер не доходит, а проверка движка доходит, три раза подряд без единого прохода через адаптер —
    это уже не мигание, а расхождение `client_net`: текст называет причину и что проверить. Только тогда
    (и ни раньше) вызов без инструментов получает явный резерв — прямой HTTP; до того он отказывает, и ни
    одного запроса на чат нет. Проверка шлюза — не чаще раза в десять секунд. Любой успех через адаптер
    обнуляет счёт и снимает тревогу."""
    with _World(tmp) as w:
        AG = w.AG
        calls = []
        AG.http_json = _direct(calls)
        AG.pydantic_transport = lambda b, p, t: _drop("dns")
        assert AG.ALARM_AFTER == 3, "порог серии — одно число на движок"
        for i in (1, 2):
            r = w.ask()
            assert not r["ok"] and not r.get("bypass") and AG.read_alarm() == {}, (i, r["log"])
            assert any("не доходит до бэкенда №1 (model-x)" in x for x in r["log"]), r["log"]
        assert AG.GAP_RUN[1]["count"] == 2 and not _chat(calls), "резерв раньше срока"
        assert AG.ADAPTER["fallback_why"] == ""
        r = w.ask()
        assert r["ok"] and "не доходит до бэкенда №1 (model-x)" in r["bypass"], r
        assert len(_chat(calls)) == 1, "резерв — один прямой запрос"
        row = AG.read_alarm()["agent:test"]
        assert (row["area"], row["kind"], row["sub"]) == ("adapter", "client_net", "dns"), row
        assert "имя шлюза не разрешается в процессе адаптера" in row["why"] and "DNS" in row["why"], row
        assert "а проверка движка доходит" in row["why"], row
        assert row["fix"] == "" and "venv" not in row["why"].lower(), row
        assert AG.USAGE["bypass"] == 1, AG.USAGE
        assert any(x["stage"] == "мимо Pydantic AI" and x["kind"] == "client_net" for x in w.failures())
        probes = [c for c in calls if c[0].endswith("/models")]
        assert len(probes) == 1, f"шлюз проверяли {len(probes)} раз за десять секунд"
        # успех через адаптер: тревога снята, счёт заново
        AG.pydantic_transport = lambda b, p, t: (200, json.loads(json.dumps(VIA)), "", 0.1)
        assert w.ask()["via"] == "pydantic_ai"
        assert AG.read_alarm() == {} and AG.GAP_RUN == {}, (AG.read_alarm(), AG.GAP_RUN)
        AG.pydantic_transport = lambda b, p, t: _drop("dns")
        for _ in range(2):
            w.ask()
        assert AG.read_alarm() == {}, "после успеха счёт не обнулился"


@test
def test_an_obvious_adapter_side_cause_alarms_at_once_and_names_itself(tmp: Path):
    """Прокси окружения, не подошедший клиенту адаптера, и сертификат, не принятый его клиентом при живой
    проверке движка, не пройдут сами: тревога с первого раза, вызов без инструментов получает резерв, и
    каждая причина названа своим текстом."""
    with _World(tmp) as w:
        AG = w.AG
        seen = {}
        for name, link in (("proxy", ("other", "ImportError")), ("tls", ("tls", "")),
                           ("proxy-kind", ("proxy", ""))):
            w.reset()
            calls = []
            AG.http_json = _direct(calls)
            AG.pydantic_transport = lambda b, p, t, link=link: _drop(link[0], link[1])
            r = w.ask()
            row = AG.read_alarm()["agent:test"]
            assert r["ok"] and r["bypass"] and row["kind"] == "client_net", (name, r)
            assert len(_chat(calls)) == 1, (name, "резерв — один прямой запрос")
            seen[name] = row
        assert seen["proxy"]["sub"] == "proxy" and "прокси окружения не подошёл клиенту адаптера (ImportError)" \
            in seen["proxy"]["why"] and "NO_PROXY" in seen["proxy"]["why"], seen["proxy"]
        assert seen["tls"]["sub"] == "tls" and "ошибка сертификата" in seen["tls"]["why"] \
            and "SSL_CERT_FILE" in seen["tls"]["why"], seen["tls"]
        assert seen["proxy-kind"]["sub"] == "proxy"
        assert len({seen[k]["why"] for k in ("proxy", "tls")}) == 2


@test
def test_an_unreachable_gateway_is_a_link_failure_not_a_pydantic_ai_alarm_for_every_call(tmp: Path):
    """Адаптер не дошёл, и проверка движка не дошла: это связь или шлюз. Вызов без инструментов, как и с
    инструментами, отказывает с названным видом (DNS), прямой запрос на чат не уходит, тревоги про Pydantic AI
    нет; на третьей неудаче подряд поднимается тревога связи, первый ответ бэкенда её снимает."""
    with _World(tmp) as w:
        AG = w.AG
        for tools in (False, True):
            w.reset()
            calls = []
            AG.http_json = _direct(calls, reached=False,
                                   probe_error="URLError: <urlopen error [Errno 8] nodename nor servname provided, or not known>")
            AG.pydantic_transport = lambda b, p, t: _drop("other")
            for i in (1, 2, 3):
                AG._PROBE.clear()
                r = w.ask(tools=tools)
                assert not r["ok"] and r["link"]["kind"] == "dns", (tools, i, r["log"])
                assert not r.get("bypass") and not _chat(calls), (tools, i)
                assert "имя шлюза не найдено (DNS)" in r["log"][-1], r["log"][-1]
            alarm = AG.read_alarm()
            assert list(alarm) == ["link:1"] and alarm["link:1"]["kind"] == "dns", (tools, alarm)
            AG.pydantic_transport = lambda b, p, t: (200, json.loads(json.dumps(VIA)), "", 0.1)
            assert w.ask(tools=tools)["ok"] and AG.read_alarm() == {}, tools


# ------------------------------------------------------------------ (в) вызов с инструментами

@test
def test_a_call_with_tools_is_repeated_through_the_adapter_not_refused_as_a_bypass(tmp: Path):
    """Вызову с инструментами и MCP прямой запрос не поможет. При обрыве, который уже прошёл (шлюз
    достижим), он повторяется через адаптер и получает ответ: ни отказа «Pydantic AI не сработал», ни
    тревоги. Прямой запрос на чат он не делает — только дешёвую проверку достижимости."""
    with _World(tmp) as w:
        AG = w.AG
        calls = {"adapter": 0}
        direct = []

        def adapter(b, p, t):
            calls["adapter"] += 1
            return _drop("connect_timeout") if calls["adapter"] == 1 else (
                200, json.loads(json.dumps(VIA)), "", 0.1)
        AG.pydantic_transport = adapter
        AG.http_json = lambda url, body, key, timeout: (direct.append((url, body)) or
                                                       (404, None, "HTTP 404", 0.01))
        r = w.ask(tools=True)
        assert r["ok"] and r["via"] == "pydantic_ai" and not r["bypass"], r
        assert calls["adapter"] == 2, "повтор через адаптер не сделан"
        assert not any("не сработал" in x or "не принимаю" in x for x in r["log"]), r["log"]
        asked = [(u, b) for u, b in direct if not u.endswith("/slots")]      # `/slots` — занятость, не вызов
        assert [u.rsplit("/", 1)[-1] for u, _b in asked] == ["models"], direct
        assert all(b is None for _u, b in asked), "вызову с инструментами ушёл прямой запрос на чат"
        assert AG.read_alarm() == {} and AG.USAGE["bypass"] == 0, AG.read_alarm()


@test
def test_a_call_with_tools_fails_with_the_real_cause_when_the_gateway_is_unreachable(tmp: Path):
    """Шлюз недостижим и для адаптера, и для прямого пути: это связь. Вызов с инструментами не получает
    ложного «Pydantic AI не сработал» и не уходит прямым запросом; причина названа по виду, а на третьей
    неудаче подряд поднимается тревога СВЯЗИ (не Pydantic AI), которую снимает первый ответ бэкенда."""
    with _World(tmp) as w:
        AG = w.AG
        chat = []

        def direct(url, body, key, timeout):
            if body is not None:
                chat.append(url)
            return None, None, "URLError: <urlopen error [Errno 61] Connection refused>", 0.01
        AG.pydantic_transport = lambda b, p, t: _drop("refused")
        AG.http_json = direct
        for i in (1, 2):
            r = w.ask(tools=True)
            assert not r["ok"] and r["link"]["kind"] == "refused" and r["link"]["n"] == 1, r
            assert not r.get("bypass"), "обрыв связи назван обходом Pydantic AI"
            assert not any("не сработал" in x or "не принимаю" in x for x in r["log"]), r["log"]
            assert "отказ в соединении (порт закрыт)" in r["log"][-1], r["log"]
            assert AG.read_alarm() == {}, (i, "тревога после единичной неудачи")
        assert not chat, "вызов с инструментами ушёл прямым запросом на чат"
        w.ask(tools=True)
        alarm = AG.read_alarm()
        assert list(alarm) == ["link:1"], alarm
        row = alarm["link:1"]
        assert (row["area"], row["kind"], row["calls"]) == ("link", "refused", 3), row
        assert "бэкенд №1 (model-x): отказ в соединении" in row["why"], row
        assert "Pydantic AI" not in row["why"].replace("не поломка Pydantic AI", ""), row
        # первый ответ этого бэкенда снимает тревогу связи
        AG.pydantic_transport = lambda b, p, t: (200, json.loads(json.dumps(VIA)), "", 0.1)
        assert w.ask(tools=True)["ok"]
        assert AG.read_alarm() == {} and AG.LINK_RUN == {}, (AG.read_alarm(), AG.LINK_RUN)


@test
def test_a_call_with_tools_names_the_adapter_client_when_only_the_direct_path_reaches(tmp: Path):
    """Шлюз достижим прямым путём, но адаптер не доходит и со второго раза: это расхождение `client_net`
    (область adapter), а не обрыв связи. Тревога про Pydantic AI — с третьего раза; тревоги связи нет."""
    with _World(tmp) as w:
        AG = w.AG
        AG.pydantic_transport = lambda b, p, t: _drop("dns")
        AG.http_json = lambda url, body, key, timeout: (404, None, "HTTP 404", 0.01)
        for i in (1, 2):
            r = w.ask(tools=True)
            assert not r["ok"] and any("не доходит до бэкенда №1" in x for x in r["log"]), r["log"]
            assert AG.read_alarm() == {}, i
        w.ask(tools=True)
        row = AG.read_alarm()["agent:test"]
        assert (row["area"], row["kind"], row["sub"]) == ("adapter", "client_net", "dns"), row
        assert "link:1" not in AG.read_alarm(), "расхождение адаптера принято за обрыв связи"


@test
def test_a_route_retries_an_adapter_only_drop_without_quarantine_until_it_is_proven(tmp: Path):
    """Режим «довести до конца»: шлюз достижим прямым путём, а адаптер не доходит. Бэкенд при этом жив,
    поэтому в карантин он не уходит (прямые вызовы без инструментов к нему идут), а вызов с инструментами
    повторяется через адаптер; на третьем свидетельстве расхождение доказано — тревога про Pydantic AI,
    ожидание отменено, и вызов отказывает с названной причиной, а не крутится вечно."""
    with _World(tmp) as w:
        AG = w.AG
        cfg = w.cfg()
        cfg["persist"] = True
        attempts = []
        AG.pydantic_transport = lambda b, p, t: (attempts.append(1) or _drop("dns"))
        AG.http_json = lambda url, body, key, timeout: (404, None, "HTTP 404", 0.01)
        r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}], tools=True,
                         sleep=AG.NO_WAIT)
        assert not r["ok"] and len(attempts) == 6, (len(attempts), r["log"])      # 3 захода × 2 через адаптер
        assert 1 not in AG.DOWN, "бэкенд, до которого не дошёл только адаптер, отправлен в карантин"
        row = AG.read_alarm()["agent:test"]
        assert (row["kind"], row["sub"]) == ("client_net", "dns"), row
        assert r["log"][-1].startswith("дедлайн исчерпан: HTTP-клиент адаптера не доходит до бэкенда №1"), r["log"][-1]
        assert not r.get("link") and "link:1" not in AG.read_alarm(), "расхождение адаптера принято за обрыв связи"


# ------------------------------------------------------------------ (г) таблица классификации

# (статус, текст ошибки прямого пути) → вид
DIRECT = [
    (None, "URLError: <urlopen error [Errno 8] nodename nor servname provided, or not known>", "dns"),
    (None, "URLError: <urlopen error [Errno -2] Name or service not known>", "dns"),
    (None, "URLError: <urlopen error [Errno 11001] getaddrinfo failed>", "dns"),
    (None, "URLError: <urlopen error [Errno 61] Connection refused>", "refused"),
    (None, "URLError: <urlopen error [Errno 111] Connection refused>", "refused"),
    (None, "URLError: <urlopen error [WinError 10061] No connection could be made because the "
           "target machine actively refused it>", "refused"),
    (None, "URLError: <urlopen error timed out>", "connect_timeout"),
    (None, "URLError: <urlopen error [Errno 60] Operation timed out>", "connect_timeout"),
    (None, "URLError: <urlopen error [WinError 10060] A connection attempt failed because the "
           "connected party did not properly respond after a period of time>", "connect_timeout"),
    (None, "URLError: <urlopen error [Errno 51] Network is unreachable>", "unreachable"),
    (None, "URLError: <urlopen error [Errno 65] No route to host>", "unreachable"),
    (None, "URLError: <urlopen error [Errno 101] Network is unreachable>", "unreachable"),
    (None, "URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
           "unable to get local issuer certificate (_ssl.c:1006)>", "tls"),
    (None, "URLError: <urlopen error Tunnel connection failed: 407 Proxy Authentication Required>", "proxy"),
    (None, "ConnectionResetError: [Errno 54] Connection reset by peer", "reset"),
    (None, "RemoteDisconnected: Remote end closed connection without response", "reset"),
    (None, "ConnectionResetError: [WinError 10054] An existing connection was forcibly closed by "
           "the remote host", "reset"),
    (None, "TimeoutError: timed out", "silent"),
    (None, "JSONDecodeError: Expecting value: line 1 column 1 (char 0)", "bad_reply"),
    (None, "URLError: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of "
           "protocol (_ssl.c:2406)>", "reset"),
    (None, "TimeoutError: The read operation timed out", "silent"),
    (None, "TimeoutError: The handshake operation timed out", "connect_timeout"),
    (None, "URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
           "Hostname mismatch, certificate is not valid for 'llm-proxy.example.com'>", "tls"),
    (None, "что-то непонятное", "other"),
    (502, "bad gateway", "server_error"),
    (503, "HTTP 503", "server_error"),
    (401, "unauthorized", "auth"),
    (403, "forbidden", "auth"),
    (429, "слишком часто", "rate_limit"),
    (404, "no such model", "not_found"),
    (400, "bad request", ""),
    (422, "unprocessable", ""),
]


@test
def test_the_engine_tells_every_link_failure_of_the_direct_path_apart(tmp: Path):
    """Классификатор движка по ошибке прямого пути (`http_json`) и по статусу: DNS, отказ порта, таймаут
    соединения, сеть недоступна, TLS, прокси, сброс, молчание по сроку, 5xx, 401/403, 429, 404, ответ
    не JSON, прочее — и «не связь» (400/422: сервер достижим и ответил по делу). Каждый вид называет
    бэкенд (№ и модель), не печатает адрес и несёт свой текст."""
    AG = _ag()
    for st, text, want in DIRECT:
        assert AG.link_kind(text, st) == want, (st, text, AG.link_kind(text, st), want)
    # исключение целиком (с цепочкой причин) разбирается так же, как его текст
    for exc, want in ((ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), "refused"),
                      (socket.gaierror(-2, "Name or service not known"), "dns"),
                      (ssl.SSLCertVerificationError(1, "certificate verify failed"), "tls"),
                      (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), "reset"),
                      (ConnectionResetError(errno.ECONNRESET, "reset"), "reset"),
                      (TimeoutError("timed out"), "silent")):
        assert AG.link_kind(exc) == want, (exc, AG.link_kind(exc), want)
    outer = RuntimeError("Connection error.")
    outer.__cause__ = socket.gaierror(-3, "Temporary failure in name resolution")
    assert AG.link_kind(outer) == "dns"
    # чистая точка входа: исключение или текст (+ статус) → вид, область и тексты ru/en; «не связь» → None
    why = AG.classify_failure(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"), n=3, model="m-1")
    assert (why.kind, why.area, why.fix) == ("refused", "link", "") and why.args["n"] == 3, why
    assert why.ru.startswith("бэкенд №3 (m-1): отказ в соединении") \
        and why.en.startswith("backend №3 (m-1): connection refused"), (why.ru, why.en)
    assert AG.classify_failure("bad request", 400) is None and AG.classify_failure("слишком часто", 429).kind == "rate_limit"
    assert AG.classify_failure("странное").kind == "other" and "бэкенд №?" in AG.classify_failure("странное").ru
    # вид, названный адаптером по классам исключений, главнее текста
    assert AG.link_kind(AG.Refusal("Connection error.", kind="tls")) == "tls"
    assert AG.link_kind(AG.Refusal("Connection error.", kind="не вид")) == "other"
    texts = {}
    for kind in sorted(AG.LINK_KINDS):
        why = AG.link_why(kind, 7, "qwen-test", 503 if kind == "server_error" else None)
        assert why.area == "link" and why.kind == kind, why
        assert "бэкенд №7 (qwen-test)" in why and why.body and "бэкенд №7" not in why.body, why
        assert not re.search(r"https?://|://|\bapi[_-]?key\b|sk-", why, re.I), why
        assert "Pydantic AI" not in str(why).replace("не поломка Pydantic AI", "") \
            .replace("это не поломка Pydantic AI", ""), why
        texts[kind] = str(why)
    assert len(set(texts.values())) == len(texts), "два вида с одним текстом"
    assert "HTTP 503" in texts["server_error"] and "HTTP 429" in texts["rate_limit"]
    # молчание и лимит — сервер жив: серия неудач их не считает
    assert AG.LINK_REACHED_KINDS == {"silent", "rate_limit"}
    assert not AG.LINK_REACHED_KINDS & AG.LINK_ALARM_KINDS


@test
def test_the_adapter_names_a_connection_failure_by_exception_classes_and_errno(tmp: Path):
    """В venv адаптера вид обрыва определяется по классам исключений цепочки (httpx, ssl, socket) и
    кодам ОС, без текста и без движка: DNS, отказ порта, таймаут соединения, сеть недоступна, TLS,
    прокси, сброс, молчание по сроку, прочее. Причина, спрятанная в `__cause__`, находится."""
    AD = _adapter()

    def named(name, *bases):
        return type(name, bases or (Exception,), {})

    def chain(*errs):
        """Цепочка: первое исключение — внешнее, остальные — причины одна за другой."""
        for outer, inner in zip(errs, errs[1:]):
            outer.__cause__ = inner
        return errs[0]

    def kind(e) -> str:
        return AD.link_kind(AD.exc_objs(e))
    wrap = named("ModelAPIError")("Connection error.")
    api = named("APIConnectionError")("Connection error.")
    connect = named("ConnectError")("x")
    cases = {
        "dns": chain(wrap, api, connect, socket.gaierror(-2, "Name or service not known")),
        "refused": chain(named("ModelAPIError")("x"), named("ConnectError")("x"),
                         ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")),
        "unreachable": chain(named("ModelAPIError")("x"), named("ConnectError")("x"),
                             OSError(errno.ENETUNREACH, "Network is unreachable")),
        "connect_timeout": chain(named("ModelAPIError")("x"), named("ConnectTimeout")("x")),
        "tls": chain(named("ModelAPIError")("x"), named("ConnectError")("x"),
                     ssl.SSLCertVerificationError(1, "certificate verify failed")),
        "proxy": chain(named("ModelAPIError")("x"), named("ProxyError")("x")),
        "reset": chain(named("ModelAPIError")("x"), named("RemoteProtocolError")("x")),
        "silent": chain(named("ModelAPIError")("x"), named("ReadTimeout")("x")),
        "other": chain(named("ModelAPIError")("x"), named("ConnectError")("x")),
    }
    for want, exc in cases.items():
        assert kind(exc) == want, (want, kind(exc))
    # оборванный посреди TLS — обрыв сети, а не сертификат; ошибка рукопожатия — TLS
    assert kind(ssl.SSLEOFError(8, "EOF occurred in violation of protocol")) == "reset"
    assert kind(ssl.SSLZeroReturnError(6, "TLS/SSL connection has been closed")) == "reset"
    assert kind(chain(named("ModelAPIError")("x"), named("ConnectError")("x"),
                      ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"))) == "tls"
    # код ОС работает и без знакомого имени класса
    assert kind(OSError(errno.ECONNREFUSED, "x")) == "refused"
    assert kind(OSError(errno.EHOSTUNREACH, "x")) == "unreachable"
    assert kind(OSError(errno.ETIMEDOUT, "x")) == "connect_timeout"
    assert kind(ConnectionResetError(errno.ECONNRESET, "x")) == "reset"
    # те же слова, что у таблицы причин движка: адаптер не придумывает своих
    AG = _ag()
    for k in set(cases) | {"unreachable"}:
        assert k in AG.LINK_KINDS, k
    # цикл в цепочке не зацикливает разбор; ответ без единого знакомого корня — «прочее»
    loop = named("A")("x")
    loop.__cause__ = named("B")("y")
    loop.__cause__.__cause__ = loop
    assert kind(loop) == "other"


@test
def test_a_live_adapter_names_a_closed_port_and_a_dropped_connection(tmp: Path):
    """Живой клиент OpenAI из venv адаптера (нет venv — пропуск; в CI он обязателен): закрытый порт —
    `refused` («не дошли», `where=adapter`), обрыв после запроса — `reset` (`where=server`)."""
    vpy = live_adapter_python()
    if vpy is None:
        return
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Drop(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.connection.close()

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Drop)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_url = f"http://127.0.0.1:{dead.getsockname()[1]}/v1"
    dead.close()
    code = (
        "import asyncio, json, sys\n"
        "sys.dont_write_bytecode = True\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import pydantic_ai_adapter as AD\n"
        "run_task, _ = AD.runtime()\n"
        "async def go():\n"
        "    base = {'key': '', 'model': 'm', 'timeout': 10,\n"
        "            'messages': [{'role': 'user', 'content': 'привет'}]}\n"
        "    out = await run_task(dict(base, url=sys.argv[2]))\n"
        "    print(json.dumps({k: out.get(k) for k in ('where', 'kind', 'proxy_fell')}))\n"
        "asyncio.run(go())\n")
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env["NO_PROXY"] = "127.0.0.1"
    outs = []
    try:
        for url in (dead_url, f"http://127.0.0.1:{srv.server_port}/v1"):
            cp = subprocess.run([str(vpy), "-c", code, str(KIT / "scripts" / "agents"), url], env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=90, stdin=subprocess.DEVNULL)
            assert cp.returncode == 0, cp.stderr[-400:]
            outs.append(json.loads(cp.stdout.strip().splitlines()[-1]))
    finally:
        srv.shutdown()
        srv.server_close()
    assert outs[0]["where"] == "adapter" and outs[0]["kind"] == "refused", outs[0]
    assert outs[1]["where"] == "server" and outs[1]["kind"] == "reset", outs[1]


# ------------------------------------------------------------------ (ж) тревога связи

@test
def test_the_link_alarm_rises_on_the_third_failure_in_a_row_and_a_success_clears_it(tmp: Path):
    """Тревога связи: три неудачи подряд на одном бэкенде без ответа между ними — и только тогда;
    одна не мигает, ответ в середине обнуляет серию, молчание по сроку и лимит скорости серию не
    считают, другой бэкенд считается отдельно. Первый ответ бэкенда тревогу снимает."""
    with _World(tmp) as w:
        AG = w.AG
        be = {"n": 1, "url": "http://gw1.example.com/v1", "key": ""}
        be2 = {"n": 2, "url": "http://gw2.example.com/v1", "key": ""}
        refused = "URLError: <urlopen error [Errno 61] Connection refused>"
        # один обрыв — тишина
        assert AG.link_note(be, "m", None, refused).kind == "refused"
        assert AG.read_alarm() == {} and AG.LINK_RUN[1]["fails"] == 1
        # ответ в середине обнуляет серию
        AG.link_ok(1)
        AG.link_note(be, "m", None, refused)
        AG.link_note(be, "m", 400, "bad request")            # сервер ответил по делу — он достижим
        AG.link_note(be, "m", None, refused)
        assert AG.read_alarm() == {}, "серия пережила ответ сервера"
        # молчание по сроку и лимит скорости — сервер жив, серию не растят
        for _ in range(4):
            AG.link_note(be, "m", None, "TimeoutError: timed out")
            AG.link_note(be, "m", 429, "slow down")
        assert AG.read_alarm() == {}
        # другой бэкенд считается отдельно
        AG.link_note(be2, "m", None, refused)
        AG.link_note(be2, "m", None, refused)
        AG.link_note(be, "m", None, refused)
        assert AG.read_alarm() == {}, "серии двух бэкендов сложились"
        # три подряд на одном — тревога связи с видом, бэкендом и счётом
        AG.link_note(be, "m", None, "URLError: <urlopen error timed out>")
        AG.link_note(be, "m", None, "URLError: <urlopen error timed out>")
        alarm = AG.read_alarm()
        assert list(alarm) == ["link:1"], alarm
        row = alarm["link:1"]
        assert (row["area"], row["kind"], row["fix"], row["calls"]) == ("link", "connect_timeout", "", 3), row
        assert row["args"]["n"] == 1 and "таймаут соединения" in row["why"], row
        assert row["path"] == "agent:test"
        # четвёртая обновляет счёт; ответ — снимает
        AG.link_note(be, "m", None, "URLError: <urlopen error timed out>")
        assert AG.read_alarm()["link:1"]["calls"] == 4
        AG.link_ok(1)
        assert AG.read_alarm() == {}, "ответ бэкенда не снял тревогу связи"
        assert 1 not in AG.LINK_RUN and AG.LINK_RUN[2]["fails"] == 2
        # подставной транспорт (тесты, замеры) серии не ведёт и тревоги не пишет
        for _ in range(5):
            AG.link_note(be, "m", None, refused, live=False)
        assert AG.read_alarm() == {}


@test
def test_the_link_alarm_does_not_flash_on_a_lone_failure_between_answers(tmp: Path):
    """Сквозная проверка через `call_role` с настоящим транспортом: обрыв — ответ — обрыв — ответ…
    тревоги не рождают; три отказа подряд — рождают; ответ снимает. Строка итога называет причину
    первой ошибкой, а консоль — один раз, пока бэкенд не ответит."""
    import io
    import contextlib
    import run_summary as RS
    with _World(tmp) as w:
        AG = w.AG
        AG.ADAPTER["name"] = "openai_compat"
        cfg = w.cfg()
        cfg["adapter"] = "openai_compat"
        script = []

        def direct(url, body, key, timeout):
            if body is None:                  # `/slots`: занятость, не вызов модели
                return 404, None, "HTTP 404", 0.01
            step = script.pop(0)
            if step == "ok":
                return 200, json.loads(json.dumps(PLAIN)), "", 0.05
            return None, None, "URLError: <urlopen error [Errno 61] Connection refused>", 0.01
        AG.http_json = direct
        for step in ("fail", "ok", "fail", "ok", "fail", "ok"):
            script[:] = [step]
            w.ask(cfg)
        assert AG.read_alarm() == {}, "чередование обрывов и ответов подняло тревогу"
        w.reset()
        AG.ADAPTER["name"] = "openai_compat"
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            for _ in range(4):
                script[:] = ["fail"]
                r = w.ask(cfg)
        assert not r["ok"] and AG.read_alarm()["link:1"]["calls"] == 4
        assert said.getvalue().count("⛔ Нет связи с моделью") == 1, said.getvalue()
        assert "отказ в соединении" in said.getvalue() and "не поломка Pydantic AI" in said.getvalue()
        assert r["link"]["why"].startswith("бэкенд №1 (model-x): отказ в соединении"), r["link"]
        assert r["log"][-1].startswith("дедлайн исчерпан: отказ в соединении"), r["log"][-3:]
        assert "ни один бэкенд не ответил осмысленно" not in " ".join(r["log"])
        # итог прогона: реальная причина — первой строкой ошибок, про Pydantic AI — ни слова
        s = RS.from_agent([], AG.USAGE, 1.0, None)
        assert s["link_failed"] == 4 and list(s["errors"])[0].startswith("⛔ обрыв связи: бэкенд №1"), s
        lines = RS.render(s)
        assert any(x.strip().startswith("⛔ Обрыв связи: вызовов без ответа 4") for x in lines), lines
        text = "\n".join(lines).replace("не поломка Pydantic AI", "")
        assert "мимо" not in text and "Pydantic" not in text, lines
        # ответ — тревоги нет; следующий отказ начинает серию заново и снова скажет в консоль
        script[:] = ["ok"]
        w.ask(cfg)
        assert AG.read_alarm() == {} and AG.LINK_RUN == {}
        assert AG._LINK_SAID == set()


@test
def test_route_waiting_for_the_network_reports_a_real_cause_per_attempt(tmp: Path):
    """Маршрут «доводит до конца»: пока шлюза нет, вызов ходит по кольцу и в каждой строке хода и
    журнала называет реальную причину связи, а не «не отвечал»; когда бэкенд вернулся — ответ."""
    import io
    import contextlib
    with _World(tmp) as w:
        AG = w.AG
        cfg = w.cfg()
        cfg["adapter"] = "openai_compat"
        cfg["persist"] = True
        answers = ["URLError: <urlopen error [Errno 8] nodename nor servname provided, or not known>",
                   "URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed>"]

        def direct(url, body, key, timeout):
            if body is None:                  # `/slots`: занятость, не вызов модели
                return 404, None, "HTTP 404", 0.01
            if answers:
                return None, None, answers.pop(0), 0.01
            return 200, json.loads(json.dumps(PLAIN)), "", 0.05
        AG.http_json = direct

        def sleep(s):
            AG.DOWN.clear()                    # прошло время карантина
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}], sleep=sleep)
        assert "имя шлюза не найдено (DNS)" in out.getvalue(), out.getvalue()      # строка хода в Консоли
        assert r["ok"], r["log"]
        joined = "\n".join(r["log"])
        assert "имя шлюза не найдено (DNS)" in joined and "ошибка TLS или сертификата" in joined, joined
        assert "не отвечал, вернёмся" not in joined.split("имя шлюза")[0]


@test
def test_a_blip_that_cuts_many_parallel_calls_at_once_is_one_event_not_an_alarm(tmp: Path):
    """Мигнувшая сеть рвёт разом все запросы в пути: 24 параллельных вызова — это одно событие, а не 24
    «подряд». Запрос, начатый раньше, чем учтён предыдущий обрыв, в серию не идёт — ни у связи, ни у
    расхождения «адаптерный клиент не доходит, а прямой доходит»; запрос, начатый после, — новое
    свидетельство, и серия растёт."""
    import threading
    with _World(tmp) as w:
        AG = w.AG
        be = {"n": 1, "url": "http://gw1.example.com/v1", "key": ""}
        refused = "URLError: <urlopen error [Errno 61] Connection refused>"
        # связь: шесть запросов, начатых до первого обрыва, — одна неудача. Сдвиг на секунду: у часов
        # Windows шаг 15 мс, и «до» с «после» в один тик не различить.
        t0 = time.monotonic() - 1.0
        threads = [threading.Thread(target=AG.link_note, args=(be, "m", None, refused, True, t0))
                   for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert AG.LINK_RUN[1]["fails"] == 1 and AG.read_alarm() == {}, AG.LINK_RUN
        for _ in range(2):                              # новые попытки, начатые после обрыва
            AG.link_note(be, "m", None, refused, True, time.monotonic() + 1.0)
        assert AG.LINK_RUN[1]["fails"] == 3 and list(AG.read_alarm()) == ["link:1"], AG.LINK_RUN
        AG.link_ok(1)
        # расхождение клиента адаптера: то же правило
        link = {"kind": "dns", "proxy_fell": "", "raw": ""}
        t0 = time.monotonic() - 1.0
        gaps = [AG.net_gap(be, "m", link, t0) for _ in range(6)]
        assert AG.GAP_RUN[1]["count"] == 1 and not any(g.armed for g in gaps), AG.GAP_RUN
        later = [AG.net_gap(be, "m", link, time.monotonic() + 1.0) for _ in range(2)]
        assert [g.armed for g in later] == [False, True], later


@test
def test_a_cause_survives_copying_and_pickling(tmp: Path):
    """Причина — строка с видом: копирование и pickle (отчёты, потоки, подпроцессы) не теряют ни текст,
    ни вид, ни область, ни признак готового текста."""
    import copy
    import pickle
    AG = _ag()
    for why in (AG.Why("dns", n=1, model="m"), AG.Why.raw("текст"), AG.Why("client_net", "tls", n=2),
                AG.cause_of(AG._venv_gap())):
        for twin in (copy.deepcopy(why), copy.copy(why), pickle.loads(pickle.dumps(why))):
            assert twin == why and (twin.kind, twin.sub, twin.area, twin.fix, twin.plain) == \
                (why.kind, why.sub, why.area, why.fix, why.plain), (why, twin)
            assert twin.args == why.args


@test
def test_a_quarantined_backend_keeps_naming_the_real_cause(tmp: Path):
    """Отказ связи сажает бэкенд в карантин на пятнадцать минут, и следующие вызовы его пропускают, не
    звоня в сеть. Причина карантина при этом не теряется: пропущенный бэкенд называет её в журнале
    вызова, итог и бот получают «обрыв связи», а не «ни один бэкенд не ответил осмысленно»."""
    import run_summary as RS
    with _World(tmp) as w:
        AG = w.AG
        cfg = w.cfg()
        cfg["adapter"] = "openai_compat"
        calls = []
        def direct(url, body, key, timeout):
            if body is None:                  # `/slots`: занятость, не вызов модели
                return 404, None, "HTTP 404", 0.01
            calls.append(url)
            return None, None, "URLError: <urlopen error [Errno 61] Connection refused>", 0.01
        AG.http_json = direct
        first = w.ask(cfg)
        assert first["link"]["kind"] == "refused" and AG.DOWN.get(1, 0) > time.time(), first
        for i in range(3):
            r = w.ask(cfg, keep_down=True)
            assert not r["ok"] and r["link"]["kind"] == "refused" and r["link"]["n"] == 1, (i, r["log"])
            assert any("не отвечал, вернёмся" in x and "Причина: отказ в соединении" in x for x in r["log"]), r["log"]
            assert r["log"][-1].startswith("дедлайн исчерпан: отказ в соединении"), r["log"][-1]
        assert len(calls) == 1, "карантин не удержал: бэкенд спрашивали заново"
        assert AG.USAGE["link_failed"] == 4, AG.USAGE
        s = RS.from_agent([], AG.USAGE, 1.0, None)
        assert s["link_failed"] == 4 and "ни один бэкенд" not in json.dumps(s, ensure_ascii=False), s
        sys.path.insert(0, str(SCRIPTS))
        BOTS = importlib.import_module("bots")
        assert BOTS._model_problem(r)["code"] == "link_down"


@test
def test_a_hung_adapter_process_raises_the_pydantic_ai_alarm_without_claiming_a_direct_call(tmp: Path):
    """Завис сам процесс адаптера — это неисправность пути Pydantic AI, и тревога про него нужна. Но вызов
    остался без ответа, и строка в консоли не говорит про «прямой HTTP», которого не было."""
    import io
    import contextlib
    with _World(tmp) as w:
        AG = w.AG
        cause = AG.Why("process_hang", "call", sec=330)
        AG.pydantic_transport = lambda b, p, t: (None, None, AG.Refusal(str(cause), cause=cause), 0.5)
        AG.http_json = lambda *a: (404, None, "HTTP 404", 0.01)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r = w.ask()
        assert not r["ok"] and not r.get("link"), r
        row = AG.read_alarm()["agent:test"]
        assert (row["area"], row["kind"], row["sub"]) == ("adapter", "process_hang", "call"), row
        shown = out.getvalue()
        assert "через него не прошёл" in shown and "процесс адаптера не дал ответа за 330 с" in shown, shown
        assert "прямой HTTP" not in shown and "link:1" not in AG.read_alarm(), shown
        assert not AG.USAGE["link_failed"], "зависший адаптер записан обрывом связи"


@test
def test_a_not_yet_proven_adapter_gap_is_not_recorded_as_the_reason_of_a_bypass(tmp: Path):
    """Первое «адаптер не доходит, а прямой доходит» у вызова с инструментами — ещё не доказанное расхождение:
    в общее поле причины обхода оно не пишется, и отчёт агента не объявляет, будто работали без адаптера."""
    with _World(tmp) as w:
        AG = w.AG
        AG.pydantic_transport = lambda b, p, t: _drop("dns")
        AG.http_json = lambda url, body, key, timeout: (404, None, "HTTP 404", 0.01)
        r = w.ask(tools=True)
        assert not r["ok"] and AG.ADAPTER["fallback_why"] == "", AG.ADAPTER["fallback_why"]
        for _ in range(2):
            w.ask(tools=True)
        assert AG.ADAPTER["fallback_why"].startswith("HTTP-клиент адаптера не доходит"), AG.ADAPTER


@test
def test_a_link_alarm_is_written_only_while_its_streak_is_alive_and_args_cannot_break_it(tmp: Path):
    """Тревога связи, собранная потоком, у которого ответ другого потока уже обнулил серию, не пишется
    (иначе она висела бы до следующего ответа). Подстановки с именами `kind` и `sub` сборку причины не
    ломают, а адрес из текста чужой ошибки в причину не попадает."""
    with _World(tmp) as w:
        AG = w.AG
        why = AG.link_why("dns", 1, "m")
        AG._raise_link_alarm(1, why, 3)                       # серии нет: ответ её уже обнулил
        assert AG.read_alarm() == {}, AG.read_alarm()
        AG.LINK_RUN[1] = {"fails": 3, "kind": "dns", "at": 0.0}
        AG._raise_link_alarm(1, why, 3)
        assert list(AG.read_alarm()) == ["link:1"]
        again = AG.alarm_cause(dict(AG.read_alarm()["link:1"], args={"kind": "x", "sub": "y", "n": 1, "model": "m"}))
        assert again.kind == "dns" and "бэкенд №1 (m)" in again, again
        assert AG.Why("dns", n=1, model="m", kind="чужое", sub="чужое").kind == "dns"
        cause = AG._out_cause({"ok": False, "where": "adapter", "error": "ошибка по адресу https://gw.example.com/v1/x?k=1"},
                              "ошибка по адресу https://gw.example.com/v1/x?k=1")
        assert "://" not in cause and "<адрес>" in cause, cause


# ------------------------------------------------------------------ (д) причины области adapter

def _fake_adapter(tmp: Path, body: str) -> Path:
    path = tmp / "fake_adapter.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


@test
def test_each_adapter_cause_has_its_own_text_and_cure(tmp: Path):
    """Каждая неисправность самого пути Pydantic AI называется своим текстом и ведёт к своему лекарству:
    нет venv — «Установка», нет файла адаптера — «Версия», пакет не стоит или не импортируется,
    версия не прошла проверку, процесс не запускается, умер при запуске, завис, закрылся, труба
    закрыта, адаптер вернул ошибку, ответ не разобран, адаптер не включён."""
    AG = _ag()
    restore = set_home(tmp / "home")
    saved = (AG.VENV, AG._adapter_file, AG._adapter_argv, AG.adapter_selfcheck, AG.ADAPTER_START,
             AG.subprocess.Popen, dict(AG._HUB))
    got = {}
    b = {"n": 1, "url": "http://127.0.0.1:9/v1", "key": ""}
    msg = {"model": "m", "messages": [{"role": "user", "content": "?"}]}

    def fresh():
        AG._HUB.update(proc=None, pending={}, broken="", deaths=0, seq=0)

    def hub_cause():
        fresh()
        proc, why = AG.adapter_hub()
        assert proc is None, "адаптер поднялся, а ждали отказ"
        return why
    try:
        # 1. нет venv / нет файла адаптера (проект и кит)
        AG.VENV = tmp / "home" / ".aurora" / "venv"
        adapter = tmp / "project" / ".aurora" / "scripts" / "agents" / "pydantic_ai_adapter.py"
        AG._adapter_file = lambda: adapter
        got["venv"] = hub_cause()
        py = AG._venv_python()
        py.parent.mkdir(parents=True)
        py.write_text("", encoding="utf-8")
        got["adapter_file.project"] = hub_cause()
        AG._adapter_file = lambda: tmp / "kit" / "scripts" / "agents" / "pydantic_ai_adapter.py"
        got["adapter_file.kit"] = hub_cause()
        # 2. процесс запускается, но пакета нет / он не импортируется
        AG._adapter_argv = lambda: [sys.executable, str(fake)]
        fake = _fake_adapter(tmp, """
            import json
            print(json.dumps({"ready": False, "error": "pydantic-ai не импортируется: ModuleNotFoundError",
                              "kind": "import_failed", "sub": "missing", "exc": "ModuleNotFoundError",
                              "module": "pydantic_ai"}), flush=True)
        """)
        got["import_failed.missing"] = hub_cause()
        fake = _fake_adapter(tmp, """
            import json
            print(json.dumps({"ready": False, "error": "pydantic-ai не импортируется: ImportError",
                              "kind": "import_failed", "sub": "broken", "exc": "ImportError", "module": ""}),
                  flush=True)
        """)
        got["import_failed.broken"] = hub_cause()
        # 3. версия не прошла проверку совместимости
        fake = _fake_adapter(tmp, """
            import json, sys
            print(json.dumps({"ready": True, "version": "9.9.9"}), flush=True)
            sys.stdin.read()
        """)
        AG.adapter_selfcheck = lambda version="", force=False: {"ok": False, "problems": ["одно сообщение"]}
        got["incompatible"] = hub_cause()
        AG.adapter_selfcheck = lambda version="", force=False: {"ok": True, "problems": []}
        # 4. процесс умер при запуске, завис при запуске, не запускается вовсе
        fake = _fake_adapter(tmp, "import sys\nsys.exit(7)\n")
        got["process_died.start"] = hub_cause()
        fake = _fake_adapter(tmp, "import time\ntime.sleep(30)\n")
        AG.ADAPTER_START = 0.3
        got["process_hang.start"] = hub_cause()
        AG.ADAPTER_START = saved[4]

        def no_popen(*a, **k):
            raise PermissionError("нет доступа")
        AG.subprocess.Popen = no_popen
        got["process_start"] = hub_cause()
        AG.subprocess.Popen = saved[5]
        # 5. процесс закрылся посреди вызова, упал повторно за прогон
        fake = _fake_adapter(tmp, """
            import json, sys
            print(json.dumps({"ready": True, "version": "9.9.9"}), flush=True)
            sys.stdin.readline()
        """)
        fresh()
        st, body, err, _dt = AG.pydantic_transport(b, dict(msg), 20)
        assert st is None and isinstance(err, AG.AdapterFail), err
        got["process_died.closed"] = err.cause
        for _ in range(AG.ADAPTER_DEATHS - 1):
            fresh_deaths = AG._HUB["deaths"]
            AG.pydantic_transport(b, dict(msg), 20)
            assert AG._HUB["deaths"] >= fresh_deaths
        deadline = time.time() + 10
        while not AG._HUB["broken"] and time.time() < deadline:
            time.sleep(0.05)
        got["process_died.repeated"] = AG._HUB["broken"]
        # 6. труба закрыта, процесс завис посреди вызова
        fresh()
        fake = _fake_adapter(tmp, """
            import json, sys, time
            print(json.dumps({"ready": True, "version": "9.9.9"}), flush=True)
            time.sleep(60)
        """)
        proc, pending = AG.adapter_hub()
        assert proc is not None
        real_stdin = proc.stdin

        class Broken:
            def write(self, line):
                raise BrokenPipeError(32, "Broken pipe")

            def flush(self):
                pass
        proc.stdin = Broken()
        st, body, err, _dt = AG.pydantic_transport(b, dict(msg), 20)
        got["process_died.pipe"] = err.cause
        proc.stdin = real_stdin
        st, body, err, _dt = AG.pydantic_transport(b, dict(msg), -29.8)        # ждёт 0,2 с
        assert st is None and "timed out" in err and AG.looks_like_timeout(err), err
        got["process_hang.call"] = err.cause
        proc.kill()
        # 7. адаптер вернул ошибку; ответ без описания
        hub = {"proc": object(), "pending": {}, "out": None}

        class Pipe:
            def write(self, line):
                task = json.loads(line)
                slot = hub["pending"][task["id"]]
                slot["out"] = hub["out"]
                slot["done"].set()

            def flush(self):
                pass
        fake_proc = type("P", (), {"stdin": Pipe()})()
        real_hub = AG.adapter_hub
        AG.adapter_hub = lambda: (fake_proc, hub["pending"])
        try:
            hub["out"] = {"ok": False, "where": "adapter", "error": "UnexpectedModelBehavior: пусто"}
            got["adapter_error"] = AG.pydantic_transport(b, dict(msg), 5)[2].cause
            hub["out"] = {"ok": False, "where": "adapter"}
            got["reply_bad"] = AG.pydantic_transport(b, dict(msg), 5)[2].cause
        finally:
            AG.adapter_hub = real_hub
    finally:
        AG.VENV, AG._adapter_file, AG._adapter_argv, AG.adapter_selfcheck, AG.ADAPTER_START = saved[:5]
        AG.subprocess.Popen = saved[5]
        p = AG._HUB.get("proc")
        if p is not None:
            try:
                p.kill()
            except OSError:
                pass
        AG._HUB.clear()
        AG._HUB.update(saved[6])
        restore()
    # 8. адаптер не включён в процессе: ответ пришёл прямым HTTP без причины
    with _World(tmp / "w") as w:
        w.AG.pydantic_transport = lambda b_, p_, t_: (200, json.loads(json.dumps(PLAIN)), "", 0.1)
        w.AG.http_json = lambda *a: (200, json.loads(json.dumps(PLAIN)), "", 0.1)
        w.ask()
        row = w.AG.read_alarm()["agent:test"]
        got["not_enabled"] = w.AG.alarm_cause(row)
    # 9. HTTP-клиент адаптера не доходит до шлюза: четыре подвида
    for sub in ("proxy", "tls", "dns", "other"):
        got["client_net." + sub] = AG.Why("client_net", sub, n=1, model="m", detail="")
    want_fix = {"venv": "install", "adapter_file.project": "version", "adapter_file.kit": "version",
                "import_failed.missing": "install", "import_failed.broken": "install",
                "incompatible": "install", "process_start": "install", "process_died.start": "install",
                "process_hang.start": "install", "process_died.closed": "install",
                "process_died.repeated": "install", "process_died.pipe": "install",
                "process_hang.call": "install", "adapter_error": "install", "reply_bad": "install",
                "not_enabled": "", "client_net.proxy": "", "client_net.tls": "", "client_net.dns": "",
                "client_net.other": ""}
    assert set(got) == set(want_fix), set(want_fix) ^ set(got)
    for cid, why in got.items():
        assert isinstance(why, AG.Why), (cid, why)
        assert AG.cause_id(why.kind, why.sub) == cid and why.area == "adapter", (cid, why.kind, why.sub)
        assert why.fix == want_fix[cid], (cid, why.fix)
    assert len({str(w_) for w_ in got.values()}) == len(got), "две причины с одним текстом"
    # тексты называют своё: файл, пакет, версию, код выхода, сроки, подвид
    assert "venv python:" in got["venv"] and "«Установка» → Pydantic AI" in got["venv"]
    assert "адаптер: .aurora/scripts/agents/" in got["adapter_file.project"] \
        and "«Версия» → «Подготовить проект»" in got["adapter_file.project"]
    assert "кит неполный" in got["adapter_file.kit"]
    assert "не установлен пакет pydantic_ai" in got["import_failed.missing"]
    assert "не импортируется в venv (ImportError)" in got["import_failed.broken"]
    assert "Pydantic AI 9.9.9 не прошёл проверку совместимости" in got["incompatible"]
    assert "(PermissionError)" in got["process_start"]
    assert "(код 7)" in got["process_died.start"]
    assert "завис" in got["process_hang.call"] and "после запуска" in got["process_hang.start"]
    assert "посреди вызова" in got["process_died.closed"] and "BrokenPipeError" in got["process_died.pipe"]
    assert "падал 3 раза" in got["process_died.repeated"]
    assert "UnexpectedModelBehavior: пусто" in got["adapter_error"]
    assert "нет ни результата" in got["reply_bad"]
    assert "не включён" in got["not_enabled"]
    for sub, word in (("proxy", "прокси окружения"), ("tls", "сертификата"), ("dns", "DNS"),
                      ("other", "причину не удалось")):
        assert word in got["client_net." + sub], sub


@test
def test_a_plain_text_cause_keeps_its_text_and_is_read_by_prefix(tmp: Path):
    """Причина, пришедшая обычной строкой (подставной транспорт, прежний файл тревоги), остаётся
    как есть, а вид читается по началу: «venv python: » и «адаптер: » — свои, остальное — ошибка адаптера
    с общей подсказкой. Строка тревоги без вида (до 1.176.0) читается как область adapter."""
    AG = _ag()
    c = AG.cause_of("ответ не разобран")
    assert c == "ответ не разобран" and c.kind == "adapter_error" and c.plain and c.area == "adapter"
    assert AG.cause_of(AG._venv_gap()).kind == "venv" and AG.cause_of("venv python: x").kind == "venv"
    assert AG.cause_of("адаптер: .aurora/x не найден — движок проекта неполный").sub == "project"
    assert AG.cause_of("адаптер: ~/kit не найден — кит неполный: обновите кит").sub == "kit"
    assert AG.cause_of("адаптер не включён в этом процессе — ошибка движка").kind == "not_enabled"
    legacy = AG.alarm_cause({"at": "2026-10-07T07:00:00Z", "why": "процесс падал 3 раза", "calls": 1})
    assert (legacy.area, legacy.kind) == ("adapter", "adapter_error") and legacy == "процесс падал 3 раза"
    # строка тревоги с видом пересобирается из таблицы, чужой вид — читается по тексту
    row = AG.Why("dns", n=3, model="m").row()
    again = AG.alarm_cause(row)
    assert again == AG.Why("dns", n=3, model="m") and again.area == "link"
    assert AG.alarm_cause({"kind": "нет-такого", "why": "текст"}) == "текст"


# ------------------------------------------------------------------ сопутствующие потребители

@test
def test_the_summary_names_drops_and_failures_apart_from_the_adapter(tmp: Path):
    """Итог прогона: повторённый после обрыва вызов — сведение (не ошибка), потерянные вызовы — первая
    ошибка с реальной причиной, «мимо Pydantic AI» — только для неисправного адаптера; шаги маршрута
    складываются; у каждого вида связи есть русское и английское название."""
    RS = importlib.import_module("run_summary") if str(SCRIPTS) in sys.path else None
    if RS is None:
        sys.path.insert(0, str(SCRIPTS))
        RS = importlib.import_module("run_summary")
    AG = _ag()
    assert {k for k, row in AG.CAUSES.items() if row["area"] == "link" and k != "retried"} == set(AG.LINK_KINDS)
    for kind in AG.LINK_KINDS:
        assert RS.link_label(kind) == AG.CAUSES[kind]["label"] and RS.link_label(kind, "en") \
            == AG.CAUSES[kind]["label_en"] != AG.CAUSES[kind]["label"], kind
    a = RS.from_agent([], {"link_retried": 2, "link_retried_kind": "dns"}, 5.0, None)
    b = RS.from_agent([], {"link_failed": 3, "link_why": "бэкенд №2 (m): отказ в соединении",
                           "bypass": 1, "bypass_why": "адаптер вернул ошибку: x"}, 7.0, None)
    assert a["link_retried"] == 2 and a["errors"] == {}, a
    merged = RS.merge([a, b])
    assert merged["link_retried"] == 2 and merged["link_failed"] == 3 and merged["link_kind"] == "dns"
    errs = list(RS.render(merged))
    first = next(x for x in errs if x.startswith("    · "))
    assert first.startswith("    · ⛔ обрыв связи: бэкенд №2 (m): отказ"), errs
    assert any("мимо Pydantic AI: адаптер вернул ошибку: x" in x for x in errs), errs
    ru = "\n".join(RS.render(merged))
    assert "2 вызовов не дошли до шлюза (имя шлюза не найдено (DNS)) и были повторены" in ru
    en = "\n".join(RS.render(merged, lang="en"))
    assert "2 calls did not reach the gateway (gateway name not found (DNS)) and were repeated" in en
    assert "3 calls got no answer" in en and "not a Pydantic AI fault" in en


@test
def test_a_bot_that_lost_the_connection_gets_its_own_problem_not_no_tools(tmp: Path):
    """Бот, чей вызов не дошёл до шлюза, получает проблему «Нет связи с моделью» с реальной причиной, а не
    «Бот ответил без инструментов» (мимо Pydantic AI) и не общее «Модель не ответила»."""
    sys.path.insert(0, str(SCRIPTS))
    BOTS = importlib.import_module("bots")
    link = {"kind": "tls", "n": 2, "why": "бэкенд №2 (m): ошибка TLS или сертификата шлюза", "count": 3}
    prob = BOTS._model_problem({"ok": False, "log": ["№2 m: что-то"], "link": link})
    assert prob["code"] == "link_down" and "ошибка TLS" in prob["detail"], prob
    assert "Pydantic AI исправен" in prob["fix"] and "open_models" in prob["actions"]
    assert BOTS._model_problem({"ok": False, "log": ["№2 m: что-то"]})["code"] == "model_failed"
    # «мимо Pydantic AI» остаётся только для неисправного пути
    assert BOTS.problem("no_tools", "адаптер вернул ошибку: x")["code"] == "no_tools"
    for lang in ("ru", "en"):
        cat = json.loads((KIT / "cockpit/modules/bots/i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert cat["bots.p.link_down"] and cat["bots.fix.link_down"], lang


@test
def test_the_cause_table_is_complete_and_every_text_is_translated(tmp: Path):
    """Таблица причин: у каждой строки область, лекарство из допустимых, текст с подстановками, которые
    движок умеет заполнять, и английский текст рядом (`en`, у видов связи ещё `label_en`) — панель отдаёт
    тексты по-русски и переводит по этой же таблице. Названия видов связи, лекарства и области не разъезжаются."""
    AG = _ag()
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    known = {"path", "module", "exc", "version", "first", "code", "times", "sec", "error", "detail", "n",
             "model", "status", "label", "count"}
    for cid, row in AG.CAUSES.items():
        assert row["area"] in ("adapter", "link") and row["fix"] in ("", "install", "version", "models"), cid
        assert set(re.findall(r"\{(\w+)\}", row["ru"])) <= known, (cid, row["ru"])
        assert sorted(re.findall(r"\{(\w+)\}", row["ru"])) == sorted(re.findall(r"\{(\w+)\}", row["en"])), cid
        if row["area"] == "link":
            assert row.get("label") and row.get("label_en"), cid
        kind, _dot, sub = cid.partition(".")
        why = AG.Why(kind, sub, n=3, model="m-1", path="p", module="mod", exc="E", version="1.0",
                     first="f", code=7, times=3, sec=60, error="e", detail="d", status=502,
                     label=AG.CAUSES["dns"]["label"], ref="dns")
        en = ck.translate_message(str(why), "en")
        assert not re.search("[а-яА-ЯёЁ]", en.replace("№", "")), (cid, en)
        assert str(why) != en, cid
        # те же два текста отдаёт сама причина — для тех, кто панель не использует
        assert why.ru == str(why) and not re.search("[а-яА-ЯёЁ]", why.en.replace("№", "")), (cid, why.en)
    # области: тревогу про Pydantic AI поднимают только adapter; связь — своя
    assert {k for k, r in AG.CAUSES.items() if r["area"] == "adapter"} >= {"venv", "client_net.dns"}
    assert not any(AG.CAUSES[k]["area"] == "adapter" for k in AG.LINK_KINDS | {"retried"})
    sys.path.insert(0, str(SCRIPTS))
    KI = importlib.import_module("kit_i18n")
    assert KI.cause_problems() == [], KI.cause_problems()                  # проверка полноты таблицы
    # каталоги панели: заголовок тревоги связи по каждому виду, на обоих языках
    for lang in ("ru", "en"):
        cat = json.loads((KIT / "cockpit/i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for kind in AG.LINK_KINDS:
            assert cat.get("alarm.link." + kind), (lang, kind)
        for key in ("alarm.link_row", "alarm.link_tail", "alarm.link_tail_cfg", "alarm.link_models"):
            assert cat.get(key), (lang, key)
        assert "Pydantic" not in "".join(cat["alarm.link." + k] for k in AG.LINK_KINDS), lang


@test
def test_the_direct_http_payload_never_carries_the_internal_tools_flag(tmp: Path):
    """Служебные поля (`needs_adapter`, `adapter`, `tools_root`…) адресованы транспорту: в HTTP-запрос к
    шлюзу они не уходят — ни в явном резерве при неисправном клиенте адаптера, ни при поломке адаптера у
    вызова с инструментами."""
    with _World(tmp) as w:
        AG = w.AG
        calls = []
        AG.http_json = _direct(calls)
        # неисправный клиент адаптера, причина очевидна (прокси): вызов без инструментов получает резерв
        AG.pydantic_transport = lambda b, p, t: _drop("other", "ImportError")
        assert w.ask()["ok"]
        # поломка самого адаптера: вызов с инструментами всё же ушёл прямым запросом и был отвергнут
        AG.pydantic_transport = lambda b, p, t: (None, None, AG.ADAPTER_FAIL + "ответ не разобран", 0.0)
        r = w.ask(tools=True)
        assert not r["ok"] and r["bypass"] == "ответ не разобран", r
        chat = [c[1] for c in _chat(calls)]
        assert len(chat) == 2, calls
        for body in chat:
            assert not {"needs_adapter", "adapter", "tools_root", "mcp", "guard", "role"} & set(body), body
        assert "needs_adapter" in AG.ADAPTER_ONLY
