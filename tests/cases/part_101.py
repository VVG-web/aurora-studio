"""Проверки движка Aurora, часть 101: бэкенд после обрыва связи в карантине минуту, а не пятнадцать.

Каркас и помощники — tests/harness.py. Ночной прогон 18.10.2026: сеть или VPN мигнули, первый же отказ
посадил шлюз в карантин на 15 минут (`DOWN_FOR`), и все вызовы четверть часа обходили живой шлюз; серия
«три неудачи подряд» для тревоги связи росла на единицу за карантин, красная полоса поднималась через
полчаса или не поднималась вовсе. Теперь обрыв связи (DNS, отказ порта, сброс, сертификат, прокси,
недоступная сеть, таймаут соединения, неназванная беда) держит бэкенд в карантине `DOWN_LINK_FOR`
секунд, а ответ шлюза (5xx), молчание по сроку и всё прочее — прежние `DOWN_FOR`. Время в проверках
подставное: часы движка двигает только ожидание кольца. Сети, моделей и настоящего домашнего каталога
проверки не касаются.
"""
from __future__ import annotations

from pathlib import Path
import contextlib
import io
import json
import time

from harness import test  # noqa: F401
from cases.part_50 import PLAIN, VIA, _World, _direct, _drop

REFUSED = "URLError: <urlopen error [Errno 61] Connection refused>"
# вид обрыва → текст, по которому движок его узнаёт (сеть подставная, текст — как у urllib и httpx)
KINDS = {
    "dns": "URLError: <urlopen error [Errno 8] nodename nor servname provided, or not known>",
    "refused": REFUSED,
    "connect_timeout": "ConnectTimeout: handshake operation timed out",
    "unreachable": "URLError: <urlopen error [Errno 101] Network is unreachable>",
    "reset": "ConnectionResetError: [Errno 104] Connection reset by peer",
    "tls": "SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
    "proxy": "ProxyError: Tunnel connection failed: 407 Proxy Authentication Required",
    "other": "ValueError: что-то непонятное",
}


class _Clock:
    """Часы движка (`agent_core.time`): время идёт, только когда его двигает ожидание кольца (`sleep`)
    или, при `tick`, сам опрос часов. Остальной `time` — настоящий; возврат на место — в `__exit__`."""

    def __init__(self, AG, start: float = 1_800_000_000.0, tick: float = 0.0):
        self.AG, self.t0, self.now, self.tick = AG, start, start, tick

    def time(self) -> float:
        self.now += self.tick
        return self.now

    def monotonic(self) -> float:
        return self.time() - self.t0 + 5000.0

    def sleep(self, seconds) -> None:
        self.now += seconds

    def at(self) -> float:
        """Секунд с начала проверки."""
        return round(self.now - self.t0, 3)

    def __getattr__(self, name):
        return getattr(time, name)

    def __enter__(self):
        self.real = self.AG.time
        self.AG.time = self
        return self

    def __exit__(self, *a):
        self.AG.time = self.real


def _quiet(fn, *a, **kw):
    """Вызов, у которого консольные строки хода и тревоги не мешают выводу проверки."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)


def _ask(AG, cfg, clock, **kw):
    kw.setdefault("deadline", clock.now + 300)
    return _quiet(AG.call_role, cfg, "worker", [{"role": "user", "content": "вопрос"}], sleep=clock.sleep, **kw)


def _gateway(AG, clock, down_text, up_at=None, status=None):
    """`http_json` движка: шлюз лежит до `up_at` секунд от начала проверки, потом отвечает. → список
    (секунд от начала, что показывала панель из тревог связи в этот миг) на каждый настоящий запрос чата."""
    tries = []

    def fn(url, body, key, timeout):
        if body is None:                      # `/slots`: занятость, не вызов модели
            return 404, None, "HTTP 404", 0.01
        tries.append((clock.at(), sorted(k for k in AG.read_alarm() if k.startswith("link:"))))
        if up_at is not None and clock.at() >= up_at:
            return 200, json.loads(json.dumps(PLAIN)), "", 0.1
        return (status, None, down_text, 0.01) if status else (None, None, down_text, 0.01)
    AG.http_json = fn
    return tries


def _direct_cfg(w):
    cfg = w.cfg()
    cfg["adapter"] = "openai_compat"
    return cfg


@test
def test_every_link_drop_kind_quarantines_the_backend_for_a_minute(tmp: Path):
    """Восемь видов обрыва связи — по одному вызову на каждый: бэкенд уходит в карантин ровно на
    `DOWN_LINK_FOR` (минута), а не на `DOWN_FOR` (15 минут); причина карантина названа, и следующий
    вызов, пропуская бэкенд, говорит «вернёмся через N с (кнопка снимает сразу)» и ту же причину."""
    with _World(tmp) as w:
        AG = w.AG
        assert AG.DOWN_LINK_FOR == 60 and AG.DOWN_FOR == 900, (AG.DOWN_LINK_FOR, AG.DOWN_FOR)
        assert AG.LINK_FAST_KINDS == frozenset(KINDS), sorted(AG.LINK_FAST_KINDS)
        cfg = _direct_cfg(w)
        for kind, text in KINDS.items():
            w.reset()
            with _Clock(AG) as clock:
                _gateway(AG, clock, text)
                # срок короче паузы кольца: вызов, который ждёт ответа (таймаут соединения), не уходит на второй круг
                r = _ask(AG, cfg, clock, deadline=clock.now + 5)
                assert not r["ok"] and r["link"]["kind"] == kind, (kind, r["log"])
                assert AG.DOWN[1] - clock.now == 60, (kind, AG.DOWN[1] - clock.now)
                held = AG.DOWN_WHY.get(1)
                assert held and held[0] == AG.DOWN[1] and held[1].kind == kind, (kind, AG.DOWN_WHY)
                clock.sleep(20)
                r2 = _ask(AG, cfg, clock, deadline=clock.now + 15)      # срок короче карантина: ждать нечего
                skipped = [x for x in r2["log"] if "не отвечал, вернёмся через 40 с (кнопка снимает сразу)" in x]
                assert skipped and "Причина: " in skipped[0], (kind, r2["log"])
                assert r2["log"][-1].startswith("дедлайн исчерпан, обрыв связи"), (kind, r2["log"][-1])


@test
def test_the_other_failures_keep_the_long_quarantine(tmp: Path):
    """Не обрыв связи — прежние 15 минут: молчание по сроку (сервер принял запрос и думает), три 5xx
    подряд (сбой шлюза), всё, что `down_for` не называет связью. Ключ и «нет такой модели» по-прежнему
    в карантин не садят — это настройка, а не связь."""
    with _World(tmp) as w:
        AG = w.AG
        cfg = _direct_cfg(w)
        # молчание по сроку
        with _Clock(AG) as clock:
            _gateway(AG, clock, "TimeoutError: The read operation timed out")
            r = _ask(AG, cfg, clock)
            # такой вызов ждёт карантин внутри себя (надежда есть): первый пропуск — после одной паузы кольца
            first = next(x for x in r["log"] if "не отвечал, вернёмся через" in x)
            assert "вернёмся через 890 с" in first, first
        # 502 три раза подряд: пауза пары, потом карантин провайдеру
        w.reset()
        with _Clock(AG) as clock:
            tries = _gateway(AG, clock, "HTTP 502", status=502)
            r = _ask(AG, cfg, clock, deadline=clock.now + 600)
            assert len(tries) == 3 and 1 in AG.DOWN, (len(tries), r["log"])
            assert AG.DOWN[1] - clock.now == 900, AG.DOWN[1] - clock.now
        # ключ и 404 — карантина нет
        for status in (401, 404):
            w.reset()
            with _Clock(AG) as clock:
                _gateway(AG, clock, f"HTTP {status}", status=status)
                _ask(AG, cfg, clock)
                assert 1 not in AG.DOWN, (status, AG.DOWN)
        # правило по видам: связь — минута, ответ шлюза, настройка и молчание — прежний срок
        for kind in KINDS:
            assert AG.down_for(AG.link_why(kind, 1, "m")) == AG.DOWN_LINK_FOR, kind
        for kind in ("server_error", "auth", "not_found", "bad_reply", "silent", "rate_limit"):
            assert AG.down_for(AG.link_why(kind, 1, "m", 502)) == AG.DOWN_FOR, kind
        assert AG.down_for(None) == AG.DOWN_FOR


@test
def test_a_dead_gateway_raises_the_link_alarm_in_three_minutes_and_an_answer_clears_it(tmp: Path):
    """Шлюз лежит, вызовы идут один за другим. Пропуск на карантине неудачей не считается, но через минуту
    бэкенд пробуется снова: запросы уходят в 0, 60 и 120 с — по одному на окно, без опроса впустую, — и
    третья неудача подряд поднимает тревогу «Шлюз моделей недоступен» (`link:1`). Когда шлюз вернулся, ответ
    снимает тревогу и обнуляет серию. На прежнем сроке (15 минут) второй вызов даже не пробовал шлюз."""
    with _World(tmp) as w:
        AG = w.AG
        cfg = _direct_cfg(w)
        with _Clock(AG) as clock:
            tries = _gateway(AG, clock, REFUSED, up_at=200)
            for i in range(3):
                r = _ask(AG, cfg, clock)
                assert not r["ok"] and r["link"]["kind"] == "refused", (i, r["log"])
                assert r["log"][-1].startswith("дедлайн исчерпан, обрыв связи: отказ в соединении") or \
                    "круг не оставил надежды" in r["log"][-2], (i, r["log"][-3:])
                assert AG.LINK_RUN[1]["fails"] == i + 1, (i, AG.LINK_RUN)
            assert [t for t, _alarm in tries] == [0.0, 60.0, 120.0], tries
            assert all(alarm == [] for _t, alarm in tries), f"тревога поднялась раньше третьей неудачи: {tries}"
            alarm = AG.read_alarm()
            assert list(alarm) == ["link:1"] and alarm["link:1"]["calls"] == 3, alarm
            assert alarm["link:1"]["kind"] == "refused" and alarm["link:1"]["area"] == "link", alarm
            # четвёртый вызов: шлюз ещё лежит (180 с) — тревога держится; пятый (240 с) — шлюз вернулся
            r = _ask(AG, cfg, clock)
            assert not r["ok"] and list(AG.read_alarm()) == ["link:1"], r["log"]
            r = _ask(AG, cfg, clock)
            assert r["ok"] and r["backend"] == 1, r["log"]
            assert [t for t, _a in tries] == [0.0, 60.0, 120.0, 180.0, 240.0], tries
            assert AG.read_alarm() == {} and AG.LINK_RUN == {}, (AG.read_alarm(), AG.LINK_RUN)
            assert 1 not in AG.DOWN or AG.DOWN[1] <= clock.now, AG.DOWN


@test
def test_a_connect_timeout_is_retried_once_a_minute_inside_the_call(tmp: Path):
    """Таймаут соединения идёт по ветке «не уложился в срок», но это не медленный ответ, а соединение, которое
    не установилось: карантин на минуту (с названной причиной), и вызов, у которого есть время, пробует шлюз
    снова через минуту, а не каждый круг и не через пятнадцать минут. Молчание по сроку (сервер принял запрос)
    остаётся с прежним сроком."""
    with _World(tmp) as w:
        AG = w.AG
        cfg = _direct_cfg(w)
        with _Clock(AG) as clock:
            tries = _gateway(AG, clock, KINDS["connect_timeout"])
            r = _ask(AG, cfg, clock)
            assert not r["ok"] and r["link"]["kind"] == "connect_timeout", r["log"][-3:]
            at = [t for t, _a in tries]
            assert at[:5] == [0.0, 60.0, 120.0, 180.0, 240.0], at
            assert all(b - a == 60.0 for a, b in zip(at, at[1:])), f"шлюз спрашивали чаще раза в минуту: {at}"
            assert [bool(a) for _t, a in tries[:5]] == [False, False, False, True, True], tries[:5]
            assert any("Причина: " in x and "не отвечал, вернёмся через 5" in x for x in r["log"]), r["log"][:6]


@test
def test_a_route_that_sees_it_through_asks_a_dead_gateway_once_a_minute(tmp: Path):
    """Режим «доводить до конца» (`persist`, срока у вызова нет): один вызов ходит по кольцу с паузой
    10 секунд и спрашивает лежащий шлюз ровно раз в минуту — между попытками бэкенд на карантине, к шлюзу
    никто не идёт. Тревога связи поднимается на третьей неудаче, а ответ возвращает вызов с результатом и
    снимает её. Рост нагрузки на шлюз исключён: окон по 60 секунд, а запросов — по одному на окно."""
    with _World(tmp) as w:
        AG = w.AG
        cfg = _direct_cfg(w)
        cfg["persist"] = True
        with _Clock(AG) as clock:
            tries = _gateway(AG, clock, REFUSED, up_at=250)
            r = _quiet(AG.call_role, cfg, "worker", [{"role": "user", "content": "вопрос"}], sleep=clock.sleep)
            assert r["ok"] and r["backend"] == 1, r["log"][-6:]
            at = [t for t, _a in tries]
            assert at == [0.0, 60.0, 120.0, 180.0, 240.0, 300.0], at
            assert [bool(a) for _t, a in tries] == [False, False, False, True, True, True], tries
            assert any("не отвечал, вернёмся через" in x and "Причина: отказ в соединении" in x for x in r["log"]), \
                r["log"][:8]
            assert AG.read_alarm() == {} and AG.LINK_RUN == {}, "ответ не снял тревогу связи"


@test
def test_an_adapter_drop_asks_the_gateway_probe_once_per_attempt_not_per_ring(tmp: Path):
    """Путь Pydantic AI: адаптер не дошёл до шлюза, контрольная проверка движка (`probe_gateway`, кэш 10
    секунд) тоже. Это обрыв связи — карантин на минуту, и пока он идёт, ни адаптер, ни проверка шлюз не
    трогают: на три попытки (0, 60, 120 с) — три вызова адаптера и три проверки, на круги по 10 секунд
    они не множатся. Когда шлюз вернулся, вызов проходит."""
    with _World(tmp) as w:
        AG = w.AG
        cfg = w.cfg()
        cfg["persist"] = True
        with _Clock(AG) as clock:
            adapter, probes = [], []

            def pydantic(b, p, t):
                adapter.append(clock.at())
                if clock.at() >= 150:
                    return 200, json.loads(json.dumps(VIA)), "", 0.1
                return _drop("refused")

            base = _direct([], reached=False)

            def direct(url, body, key, timeout):
                if body is None and url.endswith("/models"):
                    probes.append(clock.at())
                return base(url, body, key, timeout)
            AG.pydantic_transport = pydantic
            AG.http_json = direct
            r = _quiet(AG.call_role, cfg, "worker", [{"role": "user", "content": "вопрос"}], sleep=clock.sleep)
            assert r["ok"], r["log"][-6:]
            assert adapter == [0.0, 60.0, 120.0, 180.0], adapter
            assert probes == [0.0, 60.0, 120.0], probes
            assert AG.DOWN.get(1, 0) <= clock.now and AG.read_alarm() == {}, (AG.DOWN, AG.read_alarm())


@test
def test_the_return_to_primary_button_clears_the_short_quarantine_too(tmp: Path):
    """Кнопка «Вернуться на основного» (файл-флаг) снимает отметку сразу, не дожидаясь минуты: бэкенд
    спрашивают в этом же вызове."""
    with _World(tmp) as w:
        AG = w.AG
        cfg = _direct_cfg(w)
        saved = AG.RETRY_FLAG
        AG.RETRY_FLAG = tmp / "retry-primary"
        try:
            with _Clock(AG) as clock:
                tries = _gateway(AG, clock, REFUSED, up_at=10)
                assert not _ask(AG, cfg, clock)["ok"] and AG.DOWN[1] - clock.now == 60
                clock.sleep(10)
                AG.RETRY_FLAG.write_text("", encoding="utf-8")
                r = _ask(AG, cfg, clock)
                assert r["ok"] and not AG.RETRY_FLAG.exists(), r["log"]
                assert [t for t, _a in tries] == [0.0, 10.0], tries
        finally:
            AG.RETRY_FLAG = saved


@test
def test_a_call_that_cannot_wait_does_not_spin_the_ring_on_a_minute_quarantine(tmp: Path):
    """Диагностика (`agent:ping`, `agent:width`) передаёт `NO_WAIT`: паузу она не переждёт. Минутный карантин
    короче её срока, и без оговорки в кольце он казался бы «надеждой»: круги крутились бы вхолостую, пока
    минута не пройдёт. Теперь для нетерпеливого вызова карантин — отказ с причиной в первом же круге.
    Часы идут сами (по тику на опрос), чтобы вхолостую крутящийся круг не повесил проверку."""
    with _World(tmp) as w:
        AG = w.AG
        cfg = _direct_cfg(w)
        with _Clock(AG, tick=0.01) as clock:
            tries = _gateway(AG, clock, REFUSED)
            AG.DOWN[1] = clock.now + 60
            r = _quiet(AG.call_role, cfg, "worker", [{"role": "user", "content": "?"}], sleep=AG.NO_WAIT,
                       deadline=clock.now + 90)
            assert not r["ok"] and not tries, (tries, r["log"])
            assert sum("не отвечал, вернёмся" in x for x in r["log"]) == 1, r["log"]
            assert "повторять незачем" in r["log"][-2], r["log"][-3:]


@test
def test_a_dead_gateway_call_still_ends_with_the_link_label_the_agent_loop_waits_on(tmp: Path):
    """Цикл «до конца очереди» (`agent_runner.looks_offline`) узнаёт обрыв связи по метке в итоговой строке
    вызова. Метка на месте и после смены срока карантина — и у вызова, дошедшего до шлюза, и у вызова,
    пропустившего бэкенд на карантине."""
    import sys
    from harness import SCRIPTS
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import importlib
    AR = importlib.import_module("agent_runner")
    with _World(tmp) as w:
        AG = w.AG
        cfg = _direct_cfg(w)
        with _Clock(AG) as clock:
            _gateway(AG, clock, REFUSED)
            first = _ask(AG, cfg, clock)
            held = _ask(AG, cfg, clock, deadline=clock.now + 15)      # карантин ещё идёт, срок короче минуты
            for r in (first, held):
                assert not r["ok"] and r["log"][-1].startswith(f"дедлайн исчерпан, {AG.LINK_FAILURE}"), r["log"][-3:]
                assert AR.looks_offline({"steps": [{"status": "сбой", "note": r["log"][-1]}]}), r["log"][-1]
