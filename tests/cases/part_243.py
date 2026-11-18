"""Проверки движка Aurora, часть 243: сроки ролей с запасом (1.231.9).

Правило, которое проверяется: срок одного запроса не опускается ниже пола роли и не
поднимается выше её потолка, а внутри границ считается по ожидаемому выходу и замеренной
скорости — `CONNECT_RESERVE + ROLE_OUT_TOKENS / tps`. Запас посчитан по живым замерам
15–18.11.2026: qa p90 179 с, p99 359 с; разбор p99 234 с; план p99 212 с; критик p99 60 с.

Ещё проверяется, что «модель молчит» и «модель умерла» различаются вопросом к шлюзу
(`/running` у llama-swap, `/slots` у llama.cpp), а не общим временем ожидания.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from harness import SCRIPTS, test  # noqa: F401

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
A = importlib.import_module("agent_core")


def _cfg(base: int = 300) -> dict:
    return A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
                           "AURORA_AGENT_BACKEND_1_MODEL": "m",
                           "AURORA_AGENT_REQUEST_TIMEOUT": str(base)})


def _with_usage(tokens_out: int, gen_seconds: float, fn):
    saved = dict(A.USAGE)
    try:
        A.USAGE.update(tokens_out=tokens_out, gen_seconds=gen_seconds)
        return fn()
    finally:
        A.USAGE.clear()
        A.USAGE.update(saved)


@test
def test_every_role_has_a_floor_and_a_cap(_t: Path):
    """Ни одна роль не осталась без пола и потолка: иначе медленную модель снова оборвут."""
    roles = ["worker", "planner", "critic", "qa", "fast", "compressor", "document", "index"]
    for role in roles:
        assert role in A.ROLE_FLOOR, f"у роли {role} нет пола срока"
        assert role in A.ROLE_CAP, f"у роли {role} нет потолка срока"
        assert role in A.ROLE_OUT_TOKENS, f"у роли {role} нет ожидаемого выхода"
        assert A.ROLE_FLOOR[role] <= A.ROLE_CAP[role], f"у роли {role} пол выше потолка"
        assert A.ROLE_CAP[role] <= A.HARD_CAP, f"потолок роли {role} выше жёсткого предела"
    # qa: пол = 2 × p90 (179); потолок генерационной роли — общий предел, а не p99
    assert A.ROLE_FLOOR["qa"] == 360 and A.ROLE_CAP["qa"] == A.HARD_CAP, \
        (A.ROLE_FLOOR["qa"], A.ROLE_CAP["qa"])
    for role in ("worker", "planner", "critic", "compressor"):
        assert A.ROLE_CAP[role] == A.HARD_CAP, f"потолок {role} ниже общего предела: {A.ROLE_CAP[role]}"
    for role in ("fast", "document", "index"):
        assert A.ROLE_CAP[role] == 600, f"короткая роль {role} получила лишний потолок"


@test
def test_slow_generation_is_not_cut_by_the_old_caps(_t: Path):
    """Медленный расчёт (20–34 тыс. токенов на 10–15 ток/с) дотягивает до общего предела.

    20 тыс. токенов на 10 ток/с — это 2000 с, 34 тыс. на 15 ток/с — 2267 с: прежние
    потолки 600 и 900 обрывали такую модель на середине, хотя она честно считала.
    """
    cfg = _cfg(300)
    # 10 ток/с: тяжёлые роли упираются в общий предел, а не в прежние 600/900
    wk = _with_usage(10 * 600, 600.0, lambda: A.request_timeout_for(cfg, "worker"))
    qa = _with_usage(10 * 600, 600.0, lambda: A.request_timeout_for(cfg, "qa"))
    assert wk == A.HARD_CAP and qa == A.HARD_CAP, (wk, qa)
    # компрессор (8 тыс. токенов) прежний потолок 600 тоже обходил бы: 20 + 800 = 820
    comp = _with_usage(10 * 600, 600.0, lambda: A.request_timeout_for(cfg, "compressor"))
    assert comp == 820, f"потолок компрессора остался прежним: {comp}"
    # планировщик и критик просят мало токенов: им хватает базы, и потолок их не режет
    for role in ("planner", "critic"):
        value = _with_usage(10 * 600, 600.0, lambda r=role: A.request_timeout_for(cfg, r))
        assert value == 300, f"{role} ждёт больше, чем стоит его работа: {value}"
    # на 1 ток/с планировщик (2 тыс. токенов) доходит до общего предела, критик (800) — до 820
    plan = _with_usage(1 * 600, 600.0, lambda: A.request_timeout_for(cfg, "planner"))
    crit = _with_usage(1 * 600, 600.0, lambda: A.request_timeout_for(cfg, "critic"))
    assert plan == A.HARD_CAP and crit == 820, (plan, crit)
    # 15 ток/с: qa (20 тыс. токенов) и разбор (24 тыс.) выше старых потолков 900 и 600,
    # хотя до общего предела ещё не дошли: 20 + 20000/15 и 20 + 24000/15
    qa = _with_usage(15 * 600, 600.0, lambda: A.request_timeout_for(cfg, "qa"))
    wk = _with_usage(15 * 600, 600.0, lambda: A.request_timeout_for(cfg, "worker"))
    assert qa == 1353.3 and wk == 1620.0, (qa, wk)
    assert qa > 900 and wk > 600, "старые потолки всё ещё режут медленную генерацию"
    # короткие роли остаются короткими: их выход — сотни токенов
    fast = _with_usage(10 * 600, 600.0, lambda: A.request_timeout_for(cfg, "fast"))
    assert fast <= 600, f"короткая роль ждёт как генерационная: {fast}"
    # и потолок Момуса — тот же общий предел, а не 900
    R = importlib.import_module("agent_runner")
    assert R.MOMUS_ABS_CAP == A.HARD_CAP == 1800, (R.MOMUS_ABS_CAP, A.HARD_CAP)


@test
def test_slow_model_gets_more_time_and_fast_model_does_not(_t: Path):
    """Срок растёт от медленной модели и упирается в потолок; быстрая не получает лишнего."""
    cfg = _cfg(300)
    slow = _with_usage(5 * 600, 600.0, lambda: A.request_timeout_for(cfg, "worker"))
    fast = _with_usage(500 * 600, 600.0, lambda: A.request_timeout_for(cfg, "worker"))
    assert slow == A.HARD_CAP, f"медленный шлюз не поднял срок разбора до общего предела: {slow}"
    assert fast == 300, f"быстрый шлюз получил лишний срок: {fast}"
    slow_qa = _with_usage(5 * 600, 600.0, lambda: A.request_timeout_for(cfg, "qa"))
    fast_qa = _with_usage(500 * 600, 600.0, lambda: A.request_timeout_for(cfg, "qa"))
    assert slow_qa == 1800 and fast_qa == 360, (slow_qa, fast_qa)
    # Никогда не выше жёсткого предела, сколько бы ни ждали.
    for role in ("worker", "qa", "compressor"):
        value = _with_usage(1 * 600, 600.0, lambda r=role: A.request_timeout_for(cfg, r))
        assert value <= A.HARD_CAP, (role, value)


@test
def test_timeout_never_below_the_configured_base(_t: Path):
    """Настройка — нижняя граница: подняв `request_timeout`, человек получает не меньше."""
    for base in (120, 300, 600, 1200):
        cfg = _cfg(base)
        for role in ("worker", "qa", "critic"):
            value = _with_usage(0, 0.0, lambda r=role: A.request_timeout_for(cfg, r))
            assert value >= min(base, A.HARD_CAP), (base, role, value)


@test
def test_working_tells_a_slow_model_from_a_dead_one(tmp: Path):
    """`working` спрашивает шлюз: llama-swap `/running`, llama.cpp `/slots`, иначе «не знаю»."""
    calls: list = []

    def fake(url, payload, key, timeout):
        calls.append(url)
        if url.endswith("/running"):
            return 200, {"running": [{"model": "sage/Sage-A1.5", "state": "generating"}]}, "", 0.1
        return 404, None, "нет", 0.1

    saved = A.http_json
    A.http_json = fake
    try:
        be = {"url": "http://h:8100/v1", "key": ""}
        assert A.working(be, "Sage-A1.5") is True, "занятая модель названа мёртвой"
        assert A.working(be, "other-model") is False, "чужая модель названа работающей"
    finally:
        A.http_json = saved

    def slots(url, payload, key, timeout):
        if url.endswith("/slots"):
            return 200, [{"id": 0, "is_processing": True}], "", 0.1
        return 404, None, "нет", 0.1

    A.http_json = slots
    try:
        assert A.working({"url": "http://h:8080/v1", "key": ""}, "Sage-A1.5") is True, \
            "занятый слот llama.cpp не считается работой"
    finally:
        A.http_json = saved

    def nowhere(url, payload, key, timeout):
        return None, None, "URLError", 0.1

    A.http_json = nowhere
    try:
        assert A.working({"url": "http://h:8080/v1", "key": ""}, "m") is False, \
            "молчащий шлюз назван работающим"
    finally:
        A.http_json = saved
    assert calls and calls[0].endswith("/running"), calls


@test
def test_momus_floor_matches_the_qa_floor(_t: Path):
    """Пол Момуса и пол роли qa — одно правило, а не два разных числа."""
    R = importlib.import_module("agent_runner")
    cfg = _cfg(300)
    qa_floor = A.ROLE_FLOOR["qa"]
    assert R.MOMUS_FLOOR == qa_floor == 360, (R.MOMUS_FLOOR, qa_floor)
    assert R.MOMUS_ABS_CAP == A.ROLE_CAP["qa"] == 1800, (R.MOMUS_ABS_CAP, A.ROLE_CAP["qa"])
