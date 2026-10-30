"""Проверки движка Aurora, часть 158: шлюз отвечает 529 «перегружен».

Каркас и помощники — tests/harness.py. Рабочий шлюз отсекает пачку больших запросов: один-два встают в
очередь, остальные получают HTTP 529 (очередь больше двух и некэшированных токенов больше N). Прежде 529
шёл как сбой 5xx: третий подряд сажал основной шлюз в карантин на 15 минут, и работа уходила запасной
модели. Теперь 529 — пауза без карантина, а движок сам сужает число тяжёлых запросов разом. Шлюзы — на
127.0.0.1 и подставной транспорт; настоящий HOME не трогается.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from cases import part_36 as P36
from harness import test

BIG = "Текст карточки для разбора. " * 2500     # ≈ 70 000 знаков — тяжёлый запрос по оценке движка


def _forget(AG):
    """Состояние кольца живёт в процессе: тест не оставляет его следующим."""
    for d in (AG.DOWN, AG.SLOW, AG.LAST_OK, AG.COOL, AG.COOL_HITS, AG.ERR_HITS, AG.HEAVY_LEARNED, AG.SIZES):
        d.clear()


@test
def test_a_529_is_a_pause_and_not_a_quarantine(tmp: Path):
    """Три 529 подряд — три паузы и ответ на том же основном шлюзе; провайдер не в карантине, в журнале —
    «шлюз перегружен» и «карантин не ставлю». Слова «too many tokens» в отказе не делают его «запросом
    длиннее окна». Прежде третий 529 сажал шлюз в карантин на 15 минут."""
    P36._path()
    import agent_core as AG
    busy = (529, {}, {"error": {"message": "queue is busy: too many tokens"}})
    gw = P36._Gateway([busy, busy, busy, P36._OK])
    sleeps = []
    try:
        with P36._Env():
            cfg = P36._channel_cfg(AG, gw.url)
            r = P36._ask(AG, cfg, sleeps)
            assert r["ok"] and r["backend"] == 1 and gw.hits == 4, (gw.hits, r["log"])
            assert 1 not in AG.DOWN, "529 посадил основной шлюз в карантин"
            log = " ".join(r["log"])
            assert "шлюз перегружен" in log and "карантин не ставлю" in log, r["log"]
            assert "длиннее окна" not in log, "529 принят за запрос длиннее окна модели"
            assert len(sleeps) == 3, sleeps
    finally:
        gw.close()
        _forget(AG)


@test
def test_a_529_is_named_overload_and_says_what_to_set(tmp: Path):
    """529 — свой вид «перегружен»: сервер жив (серию неудач связи не растит), причина называет поле
    «Тяжёлых одновременно» в «Моделях». Прочие 5xx остаются сбоем сервера."""
    P36._path()
    import agent_core as AG
    why = AG.classify_failure("queue is busy", 529, n=2, model="m-1")
    assert why.kind == "overloaded" and why.fix == "models" and "overloaded" in AG.LINK_REACHED_KINDS, why
    assert "HTTP 529" in why.ru and "Тяжёлых одновременно" in why.ru and "Heavy at once" in why.en, (why.ru, why.en)
    assert AG.classify_failure("bad gateway", 502).kind == "server_error", "прочие 5xx перестали быть сбоем сервера"


@test
def test_a_burst_of_529_teaches_how_many_heavy_requests_the_gateway_takes(tmp: Path):
    """Шесть тяжёлых ушли разом, шлюз принял два и отказал четырём: каждый 529 снижает предел, и он
    становится два — столько, сколько шлюз держит, — пришли ли отказы до или после освобождения мест.
    Короткий и меньший отказанного запросы предел не держит. Пять ответов на тяжёлые поднимают предел на
    один; дойдя до ширины шлюза, выученное снимается."""
    P36._path()
    import agent_core as AG
    b = {"n": 7}
    try:
        for give_at_once in (False, True):
            _forget(AG)
            assert all(AG.heavy_take(b, 30000) for _ in range(6)), "без правила тяжёлые не пускаются"
            caps = []
            for _ in range(4):
                caps.append(AG.heavy_overload(b, 30000)[0])
                if give_at_once:
                    AG.heavy_give(7, 30000)
            if not give_at_once:
                for _ in range(4):
                    AG.heavy_give(7, 30000)
            assert caps == [5, 4, 3, 2], (give_at_once, caps)
            assert AG.HEAVY_LEARNED[7]["from"] == 30000, AG.HEAVY_LEARNED
            assert not AG.heavy_take(b, 30000), "третий тяжёлый ушёл к шлюзу, который держит два"
            assert AG.heavy_take(b, 900) and AG.heavy_take(b, 20000), "запрос меньше отказанного упёрся в предел"
            AG.heavy_give(7, 900)
            AG.heavy_give(7, 20000)
        for _ in range(2):
            AG.heavy_give(7, 30000)
        for _ in range(AG.HEAVY_GROW_AFTER):
            AG.heavy_answered(b, 30000, 4)
        assert AG.HEAVY_LEARNED[7]["cap"] == 3, AG.HEAVY_LEARNED
        AG.heavy_answered(b, 900, 4)
        assert AG.HEAVY_LEARNED[7]["ok"] == 0, "ответ на короткий запрос засчитан тяжёлому"
        for _ in range(AG.HEAVY_GROW_AFTER):
            AG.heavy_answered(b, 30000, 4)
        assert 7 not in AG.HEAVY_LEARNED, "предел дошёл до ширины шлюза и не снят"
    finally:
        _forget(AG)


@test
def test_the_heavy_limit_from_the_form_holds_heavy_requests_and_wakes_the_waiting_one(tmp: Path):
    """Предел человека («Тяжёлых одновременно» 2, тяжёлый — от 10 000 токенов): третий тяжёлый ждёт, короткий
    идёт сразу, освободившееся место будит ждущего. Порог не задан — умолчание движка."""
    P36._path()
    import agent_core as AG
    b = {"n": 8, "heavy": 2, "heavy_tokens": 10000}
    try:
        assert AG.heavy_take(b, 15000) and AG.heavy_take(b, 15000)
        assert not AG.heavy_take(b, 15000), "предел человека не держит третий тяжёлый"
        assert AG.heavy_take(b, 5000), "короткий запрос упёрся в предел тяжёлых"
        timer = threading.Timer(0.2, AG.heavy_give, (8, 15000))
        began = time.monotonic()
        timer.start()
        assert AG.heavy_take(b, 15000, 10.0), "освободившееся место не досталось ждущему"
        assert time.monotonic() - began < 8.0, "ждущий проспал освобождение места"
        timer.join()
        assert AG.heavy_rules({"n": 9, "heavy": 3}) == [(3, AG.HEAVY_TOKENS)]
        assert AG.heavy_rules({"n": 9}) == [], "без предела и без 529 правил быть не должно"
    finally:
        _forget(AG)


@test
def test_parallel_heavy_calls_to_a_gateway_that_takes_two_all_get_answers(tmp: Path):
    """Шесть параллельных вызовов с большим запросом к шлюзу, который держит два тяжёлых и отсекает остальные
    отказом 529: все шесть получают ответ на основном шлюзе, карантина нет, а отказов не больше, чем в первой
    пачке (4) — дальше движок держит предел сам. Прежде отказы шли пачка за пачкой, и третий подряд сажал
    шлюз в карантин."""
    P36._path()
    import agent_core as AG
    lock = threading.Lock()
    state = {"now": 0, "refused": 0, "answered": 0}

    def gateway(kind, backend, payload, timeout):
        if kind != "chat":
            return 404, None, "", 0.0
        with lock:
            if state["now"] >= 2:
                state["refused"] += 1
                return 529, None, "HTTP 529: queue is busy", 0.0
            state["now"] += 1
        time.sleep(0.15)
        with lock:
            state["now"] -= 1
            state["answered"] += 1
        return 200, {"choices": [{"message": {"content": "ответ"}, "finish_reason": "stop"}]}, "", 0.15

    def nap(_s):
        time.sleep(0.02)
        AG.COOL.clear()                       # прошедшее ожидание: паузы вышли

    keep_sem = dict(AG._SEM)
    results: list = [None] * 6
    try:
        cfg = P36._channel_cfg(AG, "https://llm.example.com/v1")
        cfg["parallel"], cfg["backends"][0]["width"] = AG.AUTO, 6
        AG._SEM.clear()                       # семафор ширины — на процесс: прежний тест мог завести его уже
        msgs = [{"role": "user", "content": BIG}]

        def one(i):
            results[i] = AG.call_role(cfg, "worker", msgs, transport=gateway,
                                      deadline=time.time() + 60, sleep=nap)
        threads = [threading.Thread(target=one, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(90)
        bad = [r and r["log"][-3:] for r in results if not (r and r["ok"] and r["backend"] == 1)]
        assert not bad, bad
        assert state["answered"] == 6 and 1 not in AG.DOWN, (state, AG.DOWN)
        assert state["refused"] <= 4, f"движок не держит выученный предел: отказов {state['refused']}"
    finally:
        _forget(AG)
        AG._SEM.clear()
        AG._SEM.update(keep_sem)
