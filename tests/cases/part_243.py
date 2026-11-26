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
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import SCRIPTS, make_project, test  # noqa: F401

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


# ------------------------------------------------------------------ Момус по частям
#
# Флаг `momus_split` (AURORA_AGENT_MOMUS_SPLIT) выключен по умолчанию: пока разбитый Момус
# не сравнён со старым на живых карточках, эталон — прежний. Здесь проверяется, что
# выключенный флаг ничего не меняет, а включённый соблюдает схему, порядок, дедуп,
# параллельность и переживает сбой одного направления.

MOMUS_OLD_TEXT = ("ОПОРА «возврат в течение месяца»\n"
                  "НЕТ ОПОРЫ: возврат занимает десять дней\n"
                  "ВЕРДИКТ: БЕЗ ОПОРЫ 1")


def _momus_cfg(split: bool) -> dict:
    env = {"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
           "AURORA_AGENT_BACKEND_1_MODEL": "m",
           "AURORA_AGENT_REQUEST_TIMEOUT": "600"}
    if split:
        env["AURORA_AGENT_MOMUS_SPLIT"] = "1"
    return A.parse_config(env)


def _answer(aspect: str, findings: list, checked: int = 5) -> dict:
    return {"ok": True, "model": "m", "backend": 1, "seconds": 0.1,
            "text": json.dumps({"aspect": aspect, "checked": checked,
                                "findings": findings, "note": ""}, ensure_ascii=False)}


@test
def test_split_is_off_by_default_and_changes_nothing(_t: Path):
    """Выключенный флаг — прежний Момус: один вызов, те же ключи, тот же разбор.

    Побайтово прежний результат здесь означает: ровно один вызов модели, промпт — прежний
    `PROMPT_MOMUS`, в ответе нет ни одного нового ключа (`split`, `aspects`, `gaps`,
    `degraded`), а вердикт и число разобраны так же, как до разбиения.
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=False)
    assert cfg.get("momus_split") is False, "флаг разбиения включён по умолчанию"
    calls = []

    def call(cfg, role, msgs, **kw):
        calls.append(msgs[0]["content"])
        return {"ok": True, "model": "m", "backend": 1, "seconds": 0.1, "text": MOMUS_OLD_TEXT}

    out = R.run_momus(cfg, "ПАК", "Вопрос?", "Ответ.", call=call, answered_in=120)
    assert len(calls) == 1, f"при выключенном флаге ушло {len(calls)} вызовов вместо одного"
    assert calls[0].startswith(R.PROMPT_MOMUS[:40]), "промпт перестал быть прежним"
    assert "split" not in out and "aspects" not in out and "gaps" not in out, sorted(out)
    assert out["ok"] and out["clean"] is False and out["unsupported"] == 1, out
    assert out["text"] == MOMUS_OLD_TEXT, "текст Момуса переписан"
    assert R.momus_claims(out["text"]) == ["НЕТ ОПОРЫ: возврат занимает десять дней"], \
        R.momus_claims(out["text"])


@test
def test_every_aspect_has_a_schema_and_a_limit(_t: Path):
    """У каждого направления есть id, метка, задание и предел находок — и оно спрашивает
    ровно одно: иначе это снова «проверь всё», только разбитое на куски."""
    R = importlib.import_module("agent_runner")
    ids = [s["id"] for s in R.MOMUS_ASPECTS]
    assert ids == ["support", "contradictions", "quotes", "coverage"], ids
    assert len(set(ids)) == len(ids), ids
    for spec in R.MOMUS_ASPECTS:
        assert spec["label"] in ("НЕТ ОПОРЫ", "ПРОТИВОРЕЧИЕ", "ПРОПУЩЕНО"), spec
        assert spec["task"].strip() and spec["limit"] >= 1, spec
        prompt = R.momus_aspect_prompt(spec, "ПАК", "Вопрос?", "Ответ.")
        assert f'"aspect": "{spec["id"]}"' in prompt, "схема не называет направление"
        assert '"findings"' in prompt and '"severity"' in prompt, "схемы находки нет в промпте"
        assert str(spec["limit"]) in prompt, "предел находок не назван"
        assert "до 240" in prompt and "до 160" in prompt, "пределов длины нет в промпте"


@test
def test_an_aspect_off_the_schema_is_degraded_not_the_whole_momus(_t: Path):
    """Не по схеме — направление не состоялось, а не «чисто» и не отказ всей проверки.

    Строгость нужна, чтобы модель, ответившая прозой, не выдавалась за проверку без
    находок: пустой `findings` — это утверждение «здесь чисто», и оно должно быть
    заслужено схемой.
    """
    R = importlib.import_module("agent_runner")
    spec = R.MOMUS_ASPECTS[0]
    prose = R.parse_momus_aspect("Всё хорошо, замечаний нет.", spec)
    assert prose["ok"] is False and "JSON" in prose["why"], prose
    alien = R.parse_momus_aspect(json.dumps({"aspect": "coverage", "findings": []}, ensure_ascii=False), spec)
    assert alien["ok"] is False and "чужое" in alien["why"], alien
    long_claim = "у" * 900
    parsed = R.parse_momus_aspect(json.dumps({
        "aspect": spec["id"], "checked": 3,
        "findings": [{"claim": long_claim, "why": "п" * 500, "severity": "critical"}] * 20,
        "note": "н" * 500}, ensure_ascii=False), spec)
    assert parsed["ok"] is True, parsed
    assert len(parsed["findings"]) == spec["limit"], "предел находок не соблюдён"
    for f in parsed["findings"]:
        assert len(f["claim"]) <= 240 and len(f["why"]) <= 160, f
        assert f["severity"] == "low", "неизвестная важность не приведена к low"
    assert len(parsed["note"]) <= 200, parsed["note"]
    # Регистр не делает важность неизвестной: `HIGH` — это `high`.
    upper = R.parse_momus_aspect(json.dumps({
        "aspect": spec["id"], "checked": 1,
        "findings": [{"claim": "срок", "why": "нет", "severity": "HIGH"}]}, ensure_ascii=False), spec)
    assert upper["findings"][0]["severity"] == "high", upper["findings"][0]

    # Один аспект не разобрался: остальные живы, проверка ok, а направление названо.
    calls = []

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        calls.append(text)
        if '"aspect": "quotes"' in text:
            return {"ok": True, "model": "m", "backend": 1, "seconds": 0.1, "text": "не JSON"}
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        findings = ([{"claim": "срок не назван", "why": "в контексте нет", "severity": "high"}]
                    if aspect == "support" else [])
        return _answer(aspect, findings)

    out = R.run_momus(_momus_cfg(split=True), "ПАК", "Вопрос?", "Ответ.", call=call)
    assert out["ok"] is True and out["unsupported"] == 1, out
    assert out["degraded"] == ["quotes"], out["degraded"]
    assert "часть проверки не состоялась: quotes" in out["text"], out["text"]
    # Одно направление не состоялось — проверка частичная, и вердикт это говорит прямо.
    assert out["text"].splitlines()[-1] == "ВЕРДИКТ: БЕЗ ОПОРЫ 1 (проверка частичная)", \
        out["text"].splitlines()[-1]
    assert out["clean"] is False and out["partial"] is True and out["complete"] is False, out
    assert out["aspects"]["quotes"]["ok"] is False and out["aspects"]["support"]["ok"] is True, out["aspects"]


@test
def test_the_aggregator_is_deterministic_and_dedupes_claims(_t: Path):
    """Порядок фиксирован, дедуп по нормализованному claim, coverage не идёт в «без опоры».

    Одно и то же утверждение, названное двумя направлениями, попадает в итог один раз;
    противоречие при этом не теряется за порядком — метка становится сильнее. Пропуски —
    неполнота, а не ложь: они уезжают отдельным ключом и в счёт не входят.
    """
    R = importlib.import_module("agent_runner")
    results = [
        {"id": "coverage", "ok": True, "checked": 2, "seconds": 0.2,
         "findings": [{"claim": "Сроки продления", "why": "вопрос про продление", "severity": "low"}]},
        {"id": "quotes", "ok": True, "checked": 1, "seconds": 0.2,
         "findings": [{"claim": "«ВОЗВРАТ ЗА 10 ДНЕЙ»", "why": "цитаты нет", "severity": "high"}]},
        {"id": "contradictions", "ok": True, "checked": 4, "seconds": 0.2,
         "findings": [{"claim": "Возврат занимает 10 дней!", "why": "в контексте месяц", "severity": "high"}]},
        {"id": "support", "ok": True, "checked": 6, "seconds": 0.2,
         "findings": [{"claim": "возврат занимает 10 дней", "why": "срока нет", "severity": "high"},
                      {"claim": "оплата в день поставки", "why": "в контексте не сказано", "severity": "high"}]},
    ]
    first = R.merge_momus_aspects(results, "Ответ.")
    shuffled = [results[i] for i in (2, 0, 3, 1)]
    again = R.merge_momus_aspects(shuffled, "Ответ.")
    assert first["text"] == again["text"], "порядок направлений на входе изменил итог"
    assert first["unsupported"] == 3, first["unsupported"]     # support 2 + quotes 1, без coverage
    lines = [x for x in first["text"].splitlines() if x.strip()]
    assert lines[0].startswith("ПРОТИВОРЕЧИЕ: "), lines[0]
    # При подъёме метки сохраняются ОБА объяснения: своя причина и причина второго
    # направления — иначе апгрейд теряет, почему утверждение ещё и без опоры.
    assert any("срока нет" in x and "в контексте месяц" in x for x in lines), lines
    # Одно утверждение из двух направлений — одна строка; цитата «ВОЗВРАТ ЗА 10 ДНЕЙ» —
    # другое утверждение (её нет в источнике), она остаётся своей строкой.
    assert len([x for x in lines if "возврат занимает 10 дней" in x.lower()]) == 1, \
        [x for x in lines if "возврат" in x.lower()]
    assert len([x for x in lines if "возврат за 10 дней" in x.lower()]) == 1, lines
    assert any(x.startswith("ПРОПУЩЕНО: ") for x in lines), lines
    assert first["gaps"] and len(first["gaps"]) == 1, first["gaps"]
    # Пропуски делают проверку частичной: «ЧИСТО» и «без опоры 0» здесь запрещены.
    assert lines[-1] == "ВЕРДИКТ: БЕЗ ОПОРЫ 3 (проверка частичная)", lines[-1]
    assert first["partial"] is True and first["clean"] is False, first
    # Нормализация: регистр, пунктуация и пробелы не создают вторую находку.
    assert R._claim_key("Возврат  занимает 10 дней!") == R._claim_key("возврат занимает 10 дней"), \
        "ключ дедупа зависит от регистра и пунктуации"


@test
def test_aspects_run_in_parallel_with_a_width_of_three(_t: Path):
    """Направления идут разом, но не шире трёх: это тот же шлюз, что отвечает человеку."""
    R = importlib.import_module("agent_runner")
    assert R.MOMUS_SPLIT_WIDTH == 3, R.MOMUS_SPLIT_WIDTH
    live = {"now": 0, "peak": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=5.0)

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        try:
            barrier.wait()          # три первых направления обязаны встретиться
        except threading.BrokenBarrierError:
            pass
        time.sleep(0.05)
        with lock:
            live["now"] -= 1
        return _answer(aspect, [])

    out = R.run_momus(_momus_cfg(split=True), "ПАК", "Вопрос?", "Ответ.", call=call,
                      answered_in=300)
    assert live["peak"] == 3, f"одновременных вызовов {live['peak']} вместо трёх"
    assert out["parallel"] == 3 and len(out["aspects"]) == 4, out.get("parallel")
    assert out["unsupported"] == 0 and out["clean"] is True, out


@test
def test_each_aspect_gets_its_own_timeout_from_the_answer_time(_t: Path):
    """Срок направления считается от фактического времени ответа, с буфером и потолком 1800.

    Раньше `run_ask` передавал жёсткий ноль, и часовая генерация проверялась по тому же
    полу, что десятисекундная. Теперь: `1.5 × ответ + 300`, не ниже 360, не выше 1800.
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=False)
    assert R.MOMUS_BUFFER == 300 and R.MOMUS_ABS_CAP == 1800, (R.MOMUS_BUFFER, R.MOMUS_ABS_CAP)
    assert R.momus_timeout(cfg, 0) == 360, "без замера ответа пол не 360"
    assert R.momus_timeout(cfg, 100) == 450, "буфер не прибавлен к времени ответа"
    assert R.momus_timeout(cfg, 1200) == 1800, "потолок 1800 не держит"
    given = []

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        given.append(kw.get("request_timeout"))
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        return _answer(aspect, [])

    R.run_momus(_momus_cfg(split=True), "ПАК", "Вопрос?", "Ответ.", call=call, answered_in=200)
    assert given and all(abs(g - 600) < 1 for g in given), given   # 1.5 × 200 + 300


@test
def test_answer_core_gives_the_verifier_the_real_answer_time(_t: Path):
    """`answer_core` знает, сколько шёл ответ, и отдаёт это проверке.

    Проверка, написанная до правки (три аргумента), продолжает работать: её зовут как
    раньше, а не падают с `TypeError`.
    """
    C = importlib.import_module("answer_core")
    seen = {}

    def verifier(answer, pack, question, answered_in=None):
        seen["answered_in"] = answered_in
        return {"ok": True, "text": "ВЕРДИКТ: ЧИСТО", "unsupported": 0, "clean": True}

    C.verify("Ответ.", pack="ПАК", cards=[], cwd=str(_t), verifier=verifier, answered_in=137.5)
    assert seen["answered_in"] == 137.5, seen

    def legacy(answer, pack, question):
        return {"ok": True, "text": "ВЕРДИКТ: ЧИСТО", "unsupported": 0, "clean": True}

    out = C.verify("Ответ.", pack="ПАК", cards=[], cwd=str(_t), verifier=legacy, answered_in=9.0)
    assert out["momus"]["ok"] is True, out


@test
def test_verify_stage_is_not_cut_at_240_seconds(_t: Path):
    """Внешний срок этапа проверки считается от потолка Момуса, а не от прежних 240 с.

    Пока здесь стояло 240, весь запас `momus_timeout` (до 1800) был фиктивным: `run_stage`
    обрывал вызов раньше, чем Момус успевал воспользоваться своим сроком.
    """
    C = importlib.import_module("answer_core")
    assert C.VERIFY_CAP == 1800.0, C.VERIFY_CAP
    seen = {}
    saved = C.run_stage

    def spy(name, fn, **kw):
        seen[name] = kw.get("timeout")
        return saved(name, fn, **kw)

    C.run_stage = spy
    try:
        def verifier(answer, pack, question, answered_in=None):
            return {"ok": True, "text": "ВЕРДИКТ: ЧИСТО", "unsupported": 0, "clean": True}

        C.verify("Ответ.", pack="ПАК", cards=[], cwd=str(_t), verifier=verifier)
        assert seen["verify"] >= 1799, f"этап проверки срезан до {seen['verify']} с"
        C.verify("Ответ.", pack="ПАК", cards=[], cwd=str(_t), verifier=verifier,
                 verify_timeout=600)
        assert seen["verify"] == 600, seen["verify"]
        # Дедлайн короче потолка — режет дедлайн, и это правильно: он общий на шаг.
        C.verify("Ответ.", pack="ПАК", cards=[], cwd=str(_t), verifier=verifier,
                 deadline=time.time() + 30)
        assert 29 <= seen["verify"] <= 30.5, seen["verify"]
    finally:
        C.run_stage = saved


@test
def test_a_partial_check_never_says_clean_or_zero(_t: Path):
    """Деградация и пропуски: «проверка частичная», а не «чисто» и не «без опоры 0».

    Проверяется вся таблица правды: полная проверка без находок — «чисто»; полная с
    находками — «без опоры N»; частичная без находок — «проверка частичная, находок нет»;
    частичная с находками — «без опоры N» и слово «частичная»; ничего не ответило — не ok.
    """
    R = importlib.import_module("agent_runner")

    def snapshot(**kw):
        base = {"support": True, "contradictions": True, "quotes": True, "coverage": True}
        base.update(kw)
        results = []
        for spec in R.MOMUS_ASPECTS:
            good = base[spec["id"]]
            results.append({"id": spec["id"], "ok": good, "checked": 3 if good else 0,
                            "seconds": 0.1, "why": "" if good else "не ответило",
                            "findings": ([] if not good else
                                         ([{"claim": "срок не назван", "why": "нет",
                                            "severity": "high"}]
                                          if spec["id"] == "support" and base.get("_finding")
                                          else []))})
        return R.merge_momus_aspects(results, "Ответ.")

    full_clean = snapshot()
    assert (full_clean["ok"], full_clean["complete"], full_clean["partial"], full_clean["clean"]) \
        == (True, True, False, True), full_clean
    assert full_clean["text"].splitlines()[-1] == "ВЕРДИКТ: ЧИСТО", full_clean["text"]
    assert R.momus_summary(full_clean) == "чисто", R.momus_summary(full_clean)

    full_hit = snapshot(_finding=True)
    assert (full_hit["complete"], full_hit["partial"], full_hit["clean"]) == (True, False, False)
    assert full_hit["text"].splitlines()[-1] == "ВЕРДИКТ: БЕЗ ОПОРЫ 1", full_hit["text"]
    assert R.momus_summary(full_hit) == "без опоры 1", R.momus_summary(full_hit)

    partial_clean = snapshot(quotes=False)
    assert (partial_clean["ok"], partial_clean["complete"], partial_clean["partial"],
            partial_clean["clean"]) == (True, False, True, False), partial_clean
    assert partial_clean["text"].splitlines()[-1] == "ВЕРДИКТ: ЧАСТИЧНО — находок нет", \
        partial_clean["text"]
    assert "ЧИСТО" not in partial_clean["text"], partial_clean["text"]
    assert R.momus_summary(partial_clean) == "проверка частичная, находок нет", \
        R.momus_summary(partial_clean)

    partial_hit = snapshot(quotes=False, _finding=True)
    assert partial_hit["text"].splitlines()[-1] == "ВЕРДИКТ: БЕЗ ОПОРЫ 1 (проверка частичная)", \
        partial_hit["text"]
    assert R.momus_summary(partial_hit) == "проверка частичная, без опоры 1", \
        R.momus_summary(partial_hit)

    dead = R.merge_momus_aspects([{"id": s["id"], "ok": False, "why": "нет"} for s in R.MOMUS_ASPECTS],
                                 "Ответ.")
    assert dead["ok"] is False and dead["clean"] is False and dead["partial"] is False, dead
    assert "ВЕРДИКТ" not in dead["text"], dead["text"]
    assert R.momus_summary(dead).startswith("не состоялась:"), R.momus_summary(dead)

    # Строка отчёта берётся из того же правила — «без опоры 0» в отчёте не появляется.
    report = R.report_make({"sid": "s1", "stage": "done",
                            "state": {"momus": partial_clean, "stages": {"checked": "1"},
                                      "spec": {"title": "Т"}}})
    assert "проверка частичная, находок нет" in report, report[-800:]
    assert "без опоры 0" not in report, report[-800:]


@test
def test_findings_and_gaps_have_separate_key_spaces(_t: Path):
    """Дедуп не путает пространства: пропуск и находка с одним текстом — разные строки.

    Ошибка была в общем словаре `seen`: индекс, взятый из `gaps`, применялся к `lines`, и
    апгрейд до «ПРОТИВОРЕЧИЯ» мог задеть чужую строку или упасть на пустом списке.
    """
    R = importlib.import_module("agent_runner")
    claim = "срок продления не назван"
    results = [
        {"id": "coverage", "ok": True, "checked": 2, "seconds": 0.1, "findings": [
            {"claim": claim, "why": "вопрос про продление", "severity": "low"}]},
        {"id": "support", "ok": True, "checked": 4, "seconds": 0.1, "findings": [
            {"claim": claim, "why": "в контексте нет", "severity": "high"}]},
    ]
    out = R.merge_momus_aspects(results, "Ответ.")
    assert out["unsupported"] == 1, out["unsupported"]        # support
    assert len(out["gaps"]) == 1, out["gaps"]                 # coverage отдельно
    assert out["gaps"][0].startswith("ПРОПУЩЕНО: " + claim), out["gaps"]
    assert any(x.startswith("НЕТ ОПОРЫ: " + claim) for x in out["text"].splitlines()), out["text"]

    # Противоречие приходит вторым и поднимает метку первой находки, не задевая пропуск.
    results.append({"id": "contradictions", "ok": True, "checked": 4, "seconds": 0.1,
                    "findings": [{"claim": claim.upper(), "why": "в контексте две недели",
                                  "severity": "high"}]})
    again = R.merge_momus_aspects(results, "Ответ.")
    assert again["unsupported"] == 1, again["unsupported"]
    assert again["gaps"] == out["gaps"], again["gaps"]
    line = next(x for x in again["text"].splitlines() if claim in x.lower())
    assert line.startswith("ПРОТИВОРЕЧИЕ: "), line
    assert "в контексте нет" in line and "в контексте две недели" in line, line

    # Пустые находки: противоречие не апгрейдит чужую (пропуск) строку и ничего не падает.
    only_gaps = [{"id": "coverage", "ok": True, "checked": 1, "seconds": 0.1,
                  "findings": [{"claim": claim, "why": "неполнота", "severity": "low"}]},
                 {"id": "contradictions", "ok": True, "checked": 1, "seconds": 0.1,
                  "findings": [{"claim": claim, "why": "контекст иначе", "severity": "high"}]}]
    third = R.merge_momus_aspects(only_gaps, "Ответ.")
    assert third["unsupported"] == 1 and len(third["gaps"]) == 1, third
    assert all(x.startswith("ПРОПУЩЕНО: ") for x in third["gaps"]), third["gaps"]


@test
def test_zero_checked_and_non_list_findings_are_degraded(_t: Path):
    """`checked: 0` и `findings` не списком — не «чисто»: смотреть было нечего."""
    R = importlib.import_module("agent_runner")
    spec = R.MOMUS_ASPECTS[0]
    zero = R.parse_momus_aspect(json.dumps({"aspect": spec["id"], "checked": 0,
                                            "findings": []}, ensure_ascii=False), spec)
    assert zero["ok"] is False and "checked=0" in zero["why"], zero
    wrong = R.parse_momus_aspect(json.dumps({"aspect": spec["id"], "checked": 3,
                                             "findings": "нет"}, ensure_ascii=False), spec)
    assert wrong["ok"] is False and "не список" in wrong["why"], wrong
    missing = R.parse_momus_aspect(json.dumps({"aspect": spec["id"], "checked": 3},
                                              ensure_ascii=False), spec)
    assert missing["ok"] is False and "не список" in missing["why"], missing


@test
def test_split_keeps_the_old_result_contract(_t: Path):
    """Разбитый Момус отдаёт те же ключи, что старый: потребители не переучиваются.

    `model`, `backend`, `why`, `quotes`, `suspect` — как раньше; `complete` и `partial`
    добавлены, а `verdict_mismatch` появляется по прежнему правилу (в разбитом его нет:
    вердикт считается из находок).
    """
    R = importlib.import_module("agent_runner")
    calls = []

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        calls.append(text)
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        findings = ([{"claim": "«цитата, которой нет»", "why": "в контексте нет", "severity": "high"}]
                    if aspect == "quotes" else [])
        out = _answer(aspect, findings)
        out["model"], out["backend"] = "m-qa", 2
        return out

    answer = "Ответ: «цитата, которой нет» — так и было."
    out = R.run_momus(_momus_cfg(split=True), "ПАК без этой цитаты", "Вопрос?", answer, call=call)
    for key in ("ok", "clean", "unsupported", "text", "model", "backend", "seconds", "why"):
        assert key in out, f"старого ключа {key} нет в результате"
    assert out["model"] == "m-qa" and out["backend"] == 2, out
    assert out["complete"] is True and out["partial"] is False, out
    assert out["quotes"]["checked"] == 5, out["quotes"]          # checked направления quotes
    assert out["quotes"]["missing"] == ["«цитата, которой нет»"], out["quotes"]
    assert "suspect" not in out, out.get("suspect")              # одна из пяти — не «все»
    assert "verdict_mismatch" not in out, out.get("verdict_mismatch")
    assert out["text"].splitlines()[-1] == "ВЕРДИКТ: БЕЗ ОПОРЫ 1", out["text"]


@test
def test_split_works_through_sections_and_chunks(_t: Path):
    """Совместимость с потребителями: раздел «Под вопросом» и проверка по кускам источника.

    `run_momus_chunks` зовёт `run_momus` — с флагом он получает разбиение и обязан
    сохранить прежнюю логику сужения (что не подтвердил первый кусок, проверяет второй).
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=True)

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        narrowed = "Утверждения для проверки:" in text
        if aspect != "support":
            return _answer(aspect, [])
        if narrowed:
            return _answer(aspect, [])                    # второй кусок подтвердил
        return _answer(aspect, [{"claim": "срок не назван", "why": "в первом куске нет",
                                 "severity": "high"}])

    out = R.run_momus_chunks(cfg, ["ПАК-1", "ПАК-2"], "Вопрос?", "Ответ.", call=call)
    assert out["ok"] is True and out["clean"] is True, out["text"]
    assert out["unsupported"] == 0, out["unsupported"]

    # Раздел «Под вопросом»: находки списком, пропуски и деградация названы словами.
    part = R.merge_momus_aspects([
        {"id": "support", "ok": True, "checked": 2, "seconds": 0.1,
         "findings": [{"claim": "срок не назван", "why": "нет", "severity": "high"}]},
        {"id": "coverage", "ok": True, "checked": 2, "seconds": 0.1,
         "findings": [{"claim": "продление", "why": "вопрос про него", "severity": "low"}]},
        {"id": "quotes", "ok": False, "why": "не ответило", "seconds": 0.1},
        {"id": "contradictions", "ok": True, "checked": 2, "seconds": 0.1, "findings": []},
    ], "Ответ.")
    section = "\n".join(R.momus_section(part))
    assert "- НЕТ ОПОРЫ: срок не назван" in section, section
    assert "- ПРОПУЩЕНО: продление" in section, section
    assert "Проверка частичная: не состоялись направления quotes" in section, section
    assert R.momus_claims(part["text"]) == ["НЕТ ОПОРЫ: срок не назван — нет"], \
        R.momus_claims(part["text"])


@test
def test_report_ask_survives_split_clean_and_partial_zero(_t: Path):
    """Отчёт «Спросить» на разбитом Момусе: без `model` не падает, «без опоры — 0» не пишет.

    Чистый разбитый результат может не назвать модель (все направления ответили одной) —
    прежняя ветка брала `mo['model']` напрямую и падала бы с KeyError. А частичная
    проверка без находок печатала «без опоры — 0», что читается как «всё хорошо».
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=False)
    clean = R.merge_momus_aspects(
        [{"id": s["id"], "ok": True, "checked": 2, "seconds": 0.1, "findings": []}
         for s in R.MOMUS_ASPECTS], "Ответ.")
    assert "model" not in clean and clean["clean"] is True, clean
    res = {"ok": True, "answer": "Ответ.", "cards": [], "total": 0, "model": "m",
           "backend": 1, "seconds": 1.0, "momus": clean}
    text = R.report_ask(res, "Вопрос?", cfg)
    assert "Момус: чисто" in text and "модель проверки" in text, text[-600:]

    part = R.merge_momus_aspects(
        [{"id": s["id"], "ok": s["id"] != "quotes", "checked": 2, "seconds": 0.1, "findings": []}
         for s in R.MOMUS_ASPECTS], "Ответ.")
    assert part["ok"] and part["partial"] and part["unsupported"] == 0, part
    text2 = R.report_ask({**res, "momus": part}, "Вопрос?", cfg)
    # Полная проверка с находками говорит по-старому: «без опоры — N» — это контракт
    # отчёта (part_08), а не пожелание. Новые слова — только для частичной проверки.
    hit = R.merge_momus_aspects(
        [{"id": s["id"], "ok": True, "checked": 2, "seconds": 0.1,
          "findings": ([{"claim": "срок не назван", "why": "нет", "severity": "high"}]
                       if s["id"] == "support" else [])}
         for s in R.MOMUS_ASPECTS], "Ответ.")
    text_hit = R.report_ask({**res, "momus": hit}, "Вопрос?", cfg)
    assert "Момус: без опоры — 1" in text_hit, text_hit[-600:]
    assert "проверка частичная" not in text_hit, text_hit[-600:]
    assert "проверка частичная, находок нет" in text2, text2[-600:]
    assert "без опоры — 0" not in text2 and "БЕЗ ОПОРЫ 0" not in text2, text2[-600:]
    assert "сверен частично" in text2, text2[-600:]


@test
def test_the_flag_reaches_the_working_config_through_the_model_settings(_t: Path):
    """Флаг разбиения доезжает до рабочего конфига, а не только до `parse_config`.

    Рабочий путь — `model_config.load` (миграция из `.env` в `local/models.json`) и
    `to_config`, а не `parse_config`. Пока `momus_split` там не было, переменная окружения
    ни на что не влияла: путь разбиения оставался мёртвым в любом рабочем прогоне.
    """
    MC = importlib.import_module("model_config")
    R = importlib.import_module("agent_runner")
    env = {"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
           "AURORA_AGENT_BACKEND_1_MODEL": "m",
           "AURORA_AGENT_REQUEST_TIMEOUT": "600",
           "AURORA_AGENT_MOMUS_SPLIT": "1"}
    # Тест обязан вернуть окружение в прежнее состояние целиком, а не одну переменную:
    # иначе выставленные бэкенды и срок утекают в соседние проверки одного прогона.
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            os.environ[k] = v

        # Рабочий путь: миграция в файл настройки и чтение обратно.
        data = MC.load(_t, env)
        assert data["settings"]["momus_split"] is True, data["settings"]
        assert MC.load(_t, {})["settings"]["momus_split"] is True, "флаг не сохранился в файле"
        cfg = MC.to_config(data)
        assert cfg["momus_split"] is True, "флаг потерялся по дороге в рабочий конфиг"
        calls = []

        def call(cfg, role, msgs, **kw):
            text = msgs[0]["content"]
            calls.append(text)
            aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
            return _answer(aspect, [])

        out = R.run_momus(cfg, "ПАК", "Вопрос?", "Ответ.", call=call)
        assert len(calls) == len(R.MOMUS_ASPECTS) == 4, \
            f"через рабочую настройку ушло {len(calls)} вызовов вместо четырёх"
        assert out.get("split") is True and out["ok"], out

        # Выключенный флаг в самой настройке — прежний путь, даже если окружение молчит.
        data["settings"]["momus_split"] = False
        os.environ.pop("AURORA_AGENT_MOMUS_SPLIT", None)
        cfg_off = MC.to_config(data)
        calls.clear()
        R.run_momus(cfg_off, "ПАК", "Вопрос?", "Ответ.", call=lambda *a, **k: (
            calls.append(1) or {"ok": True, "model": "m", "backend": 1, "seconds": 0.1,
                                "text": "ВЕРДИКТ: ЧИСТО"}))
        assert len(calls) == 1, f"при выключенном флаге ушло {len(calls)} вызовов"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_bad_findings_items_and_a_weaker_label_do_not_flip_the_truth(_t: Path):
    """Мусор внутри `findings` — не «чисто», а метка противоречия не понижается цитатой.

    Два остатка прежних блокеров:
    * элементы списка проверяются поимённо: строка, пустой `claim`, числовой `claim` —
      направление не состоялось, а не «находок нет» (иначе модель отвечает мусором и
      получает «чисто»);
    * `ПРОТИВОРЕЧИЕ` сильнее `НЕТ ОПОРЫ`: одно и то же утверждение, названное сначала
      противоречием, а потом цитатой, остаётся противоречием, и все причины сохранены.
    """
    R = importlib.import_module("agent_runner")
    spec = R.MOMUS_ASPECTS[0]
    cases = [
        (["not-a-dict"], "не объект"),
        ([{"claim": ""}], "без строкового claim"),
        ([{"claim": 123}], "без строкового claim"),
        ([{"claim": "нормально", "why": 5}], "why не строка"),
    ]
    for findings, needle in cases:
        got = R.parse_momus_aspect(json.dumps({"aspect": spec["id"], "checked": 4,
                                               "findings": findings}, ensure_ascii=False), spec)
        assert got["ok"] is False and needle in got["why"], (findings, got)
    ok = R.parse_momus_aspect(json.dumps({"aspect": spec["id"], "checked": 4,
                                          "findings": [{"claim": "срок", "why": ""}]},
                                         ensure_ascii=False), spec)
    assert ok["ok"] is True and ok["findings"][0]["claim"] == "срок", ok

    claim = "возврат за 10 дней"
    results = [
        {"id": "support", "ok": True, "checked": 3, "seconds": 0.1,
         "findings": [{"claim": claim, "why": "срока нет", "severity": "high"}]},
        {"id": "contradictions", "ok": True, "checked": 3, "seconds": 0.1,
         "findings": [{"claim": "Возврат за 10 дней!", "why": "в контексте месяц",
                       "severity": "high"}]},
        {"id": "quotes", "ok": True, "checked": 1, "seconds": 0.1,
         "findings": [{"claim": "«возврат за 10 дней»", "why": "цитаты нет",
                       "severity": "high"}]},
    ]
    out = R.merge_momus_aspects(results, "Ответ.")
    assert out["unsupported"] == 1, out["unsupported"]
    line = next(x for x in out["text"].splitlines() if "возврат за 10 дней" in x.lower())
    assert line.startswith("ПРОТИВОРЕЧИЕ: "), line
    for why in ("срока нет", "в контексте месяц", "цитаты нет"):
        assert why in line, (why, line)


@test
def test_chunks_do_not_report_clean_over_a_partial_check(_t: Path):
    """Проверка по кускам не выдаёт «чисто», если первый кусок был частичным.

    Прежний `clean = not total` считался только по числу оставшихся находок. Первый кусок
    мог потерять направление (`degraded`) или найти пропуски (`gaps`), а второй снять
    единственную находку — и человек получал «чисто» при живых признаках неполноты.
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=True)

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        narrowed = "Утверждения для проверки:" in text
        if aspect == "support" and not narrowed:
            # Первый кусок потерял главное направление: модель отошла от схемы.
            return {"ok": True, "model": "m", "backend": 1, "seconds": 0.1, "text": "не JSON"}
        return _answer(aspect, [])

    first = R.run_momus(cfg, "ПАК-1", "Вопрос?", "Ответ.", call=call,
                        aspects=("support", "contradictions"))
    assert first["partial"] is True and first["degraded"] == ["support"], first
    out = R.run_momus_chunks(cfg, ["ПАК-1", "ПАК-2"], "Вопрос?", "Ответ.", call=call)
    assert out["unsupported"] == 0, out["unsupported"]
    assert out["clean"] is False and out["partial"] is True, out
    assert out["degraded"] == ["support"], out["degraded"]
    assert out["text"].splitlines()[-1] == "ВЕРДИКТ: ЧАСТИЧНО — находок нет", out["text"]
    assert R.momus_summary(out) == "проверка частичная, находок нет", R.momus_summary(out)
    report = R.report_ask({"ok": True, "answer": "Ответ.", "cards": [], "total": 0,
                           "model": "m", "backend": 1, "seconds": 1.0, "momus": out},
                          "Вопрос?", cfg)
    assert "чисто" not in report.lower(), report[-500:]

    # Пропуски покрытия остаются за полной проверкой: куски их не спрашивают вовсе,
    # поэтому «чисто» второго куска не перебивает найденный первым пропуск.
    def call_gaps(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        if aspect == "coverage":
            return _answer(aspect, [{"claim": "продление", "why": "вопрос про него",
                                     "severity": "low"}])
        return _answer(aspect, [])

    full = R.run_momus(cfg, "ПАК", "Вопрос?", "Ответ.", call=call_gaps)
    assert full["gaps"] and full["partial"] is True and full["clean"] is False, full
    out2 = R.run_momus_chunks(cfg, ["ПАК-1", "ПАК-2"], "Вопрос?", "Ответ.", call=call_gaps)
    # Покрытие проверяется один раз — на настоящем ответе в первом куске, — и найденный
    # там пропуск обязан дожить до итога: второй кусок его не отменяет.
    assert out2["gaps"] == full["gaps"], (out2["gaps"], full["gaps"])
    assert out2["clean"] is False and out2["partial"] is True, out2


@test
def test_a_partial_momus_makes_the_whole_answer_partial(_t: Path):
    """Частичная проверка Момуса — частичный ответ, а не `full`.

    `answer_core.verify` смотрел только на `ok`, поэтому частичный результат
    (`ok=True`, `degraded=["quotes"]`) оставлял ответ в статусе `full`: потребитель
    ответа не знал, что половина не проверена, и `degraded_note` молчал.
    """
    C = importlib.import_module("answer_core")
    cfg = _momus_cfg(split=False)

    def call(cfg, role, messages, **kw):
        return {"ok": True, "text": "Ответ по базе.", "model": "m", "backend": 1,
                "seconds": 2.0, "tokens_in": 100, "tokens_out": 20,
                "req": {"role": role, "request_timeout": 600}}

    def verifier(answer, pack, question, answered_in=None):
        return {"ok": True, "clean": False, "partial": True, "complete": False,
                "degraded": ["quotes"], "unsupported": 0, "gaps": [],
                "text": "ВЕРДИКТ: ЧАСТИЧНО — находок нет"}

    out = C.answer("Вопрос?", cfg=cfg, cwd=str(_t), call=call, pack="ПАК",
                   prompt="Ответь.", verifier=verifier)
    assert out["ok"] is True, out
    assert out["status"] == "partial", out["status"]
    assert "momus_partial:quotes" in out["degraded"], out["degraded"]
    assert out["degraded_note"] and "momus_partial" in out["degraded_note"], out.get("degraded_note")
    assert out["checks"]["momus"]["partial"] is True, out["checks"]

    # Полная проверка не делает ответ частичным.
    def full(answer, pack, question, answered_in=None):
        return {"ok": True, "clean": True, "partial": False, "complete": True,
                "degraded": [], "unsupported": 0, "gaps": [], "text": "ВЕРДИКТ: ЧИСТО"}

    out2 = C.answer("Вопрос?", cfg=cfg, cwd=str(_t), call=call, pack="ПАК",
                    prompt="Ответь.", verifier=full)
    assert out2["status"] == "full" and not out2["degraded"], (out2["status"], out2["degraded"])


@test
def test_split_has_a_common_deadline_and_cancels_the_tail(_t: Path):
    """Направления не тянут этап дольше общего срока, и хвост не живёт после возврата.

    Четыре направления по 1800 с при ширине 3 давали до 3600 с: `ThreadPoolExecutor`
    ждал все задачи, отмены не было, и внешний таймаут бросал поток, который продолжал
    держать слот шлюза. Теперь остаток считается АБСОЛЮТНЫМ дедлайном в момент старта
    направления: задача из очереди не получает полный срок заново.
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=True)
    span = 1.2
    started = time.time()
    deadline = started + span
    calls = []
    live = {"now": 0}
    lock = threading.Lock()

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        rt = float(kw.get("request_timeout") or 0)
        with lock:
            live["now"] += 1
            calls.append((aspect, time.time(), rt))
        try:
            # Вызов честно уважает свой срок — как настоящий `call_role`.
            time.sleep(max(0.0, min(rt, 1.0)))
            return _answer(aspect, [])
        finally:
            with lock:
                live["now"] -= 1

    out = R.run_momus(cfg, "ПАК", "Вопрос?", "Ответ.", call=call, deadline=deadline)
    spent = time.time() - started
    assert spent <= span + 0.35, f"вернулась через {spent:.2f} с при сроке {span} с"
    # Ни один вызов не имеет права работать после возврата: иначе слот шлюза занят.
    assert live["now"] == 0, f"после возврата осталось {live['now']} активных вызовов"
    for aspect, at, rt in calls:
        assert at + rt <= deadline + 0.25, (aspect, at - started, rt)
    assert len(calls) == 3, calls        # четвёртое направление не влезло в остаток
    assert out["partial"] is True and out["complete"] is False, out
    assert out["degraded"], out["degraded"]
    # Направление, не стартовавшее из очереди, названо причиной, а не пустым «упало».
    assert any("общий срок" in (out["aspects"][x].get("why") or "")
               for x in out["degraded"]), out["aspects"]
    assert out["aspects"]["coverage"]["ok"] is False, out["aspects"]
    assert "не стартовало" in out["aspects"]["coverage"]["why"], out["aspects"]


@test
def test_a_call_that_ignores_its_timeout_is_reported_as_a_timeout(_t: Path):
    """Отмена кооперативная: медленный вызов не остановить, но причину назвать обязаны.

    `ThreadPoolExecutor` снимает только очередь; запущенный поток переживёт этап, если сам
    не уважает срок. Требовать «ноль активных вызовов» для такого вызова нельзя, а вот
    вернуться вовремя и сказать человеку про срок — можно и нужно. Без `timed_out`
    `answer_core` писала `momus_failed`, и человек искал поломку шлюза вместо срока.
    """
    R = importlib.import_module("agent_runner")
    C = importlib.import_module("answer_core")
    cfg = _momus_cfg(split=True)
    span = 1.6
    started = time.time()

    def slow(cfg, role, msgs, **kw):
        time.sleep(2.0)                       # срок игнорируется намеренно
        return _answer("support", [])

    out = R.run_momus(cfg, "ПАК", "Вопрос?", "Ответ.", call=slow,
                      deadline=started + span)
    assert time.time() - started <= span + 0.8, "этап ждал вызов, который его не уважает"
    assert out["ok"] is False and out["timed_out"] is True, out
    assert out["given"] and out["given"] >= span - 0.1, out["given"]
    assert all(a.get("timed_out") for a in out["aspects"].values()), out["aspects"]
    assert any("общий срок" in (a.get("why") or "") for a in out["aspects"].values()), out

    checks = C.verify("Ответ.", pack="ПАК", cards=[], cwd=str(_t),
                      verifier=lambda *a, **k: out)
    assert "momus_timeout" in checks["degraded"], checks["degraded"]

    report = R.report_ask({"ok": True, "answer": "Ответ.", "cards": [], "total": 0,
                           "model": "m", "backend": 1, "seconds": 1.0,
                           "momus": checks["momus"]}, "Вопрос?", cfg)
    assert "не успел проверить" in report, report[-500:]
    assert f"{int(out['given'])} с" in report, report[-500:]
    assert "не проверил ответ" not in report, report[-500:]


@test
def test_the_stage_timeout_names_the_limit_that_actually_stood(_t: Path):
    """Таймаут этапа называет фактический предел, а не `request_timeout` из настройки.

    Отчёт брал `mo.get('given') or cfg['request_timeout']`, а `verify` не клал `given` —
    и человек читал «не уложилась в 600 с» там, где стояло 0.1 с (или 1800 в обычном
    случае) и учился не тому рычагу.
    """
    C = importlib.import_module("answer_core")
    R = importlib.import_module("agent_runner")

    def sleepy(answer, pack, question, answered_in=None):
        time.sleep(1.0)
        return {"ok": True, "text": "ВЕРДИКТ: ЧИСТО", "unsupported": 0, "clean": True}

    checks = C.verify("Ответ.", pack="ПАК", cards=[], cwd=str(_t), verifier=sleepy,
                      verify_timeout=0.2)
    mo = checks["momus"]
    assert not mo["ok"] and mo["timed_out"], mo
    assert 0 < mo["given"] <= 0.5, mo

    report = R.report_ask({"ok": True, "answer": "Ответ.", "cards": [], "total": 0,
                           "model": "m", "backend": 1, "seconds": 1.0, "momus": mo},
                          "Вопрос?", _momus_cfg(split=False))
    assert "0 с" in report or "0.2 с" in report, report[-400:]
    assert "600 с" not in report, "отчёт назвал настройку вместо фактического предела"


@test
def test_split_names_what_it_could_not_check(_t: Path):
    """Отчёт называет, какие направления не состоялись, и не печатает пустую модель.

    «Часть направлений не состоялась» без имён не говорит аналитику, что перепроверять
    руками. Плюс `model`/`backend` в результате могут отсутствовать (все направления
    упали) — ключи обязаны быть, иначе отчёт и панель читают их через `.get` наугад.
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=True)

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        if '"aspect": "quotes"' in text:
            raise RuntimeError("битый fragment")
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        findings = [] if aspect != "support" else [
            {"claim": "Срок — 5 дней", "why": "срок в контексте не назван"}]
        return _answer(aspect, findings)

    out = R.run_momus(cfg, "ПАК", "Вопрос?", "Ответ.", call=call)
    assert out["model"] == "m" and out["backend"] == 1, (out["model"], out["backend"])
    assert R.momus_why(out) == "не состоялись: цитаты", R.momus_why(out)

    res = {"ok": True, "answer": "Ответ.", "cards": [], "total": 0, "model": "m",
           "backend": 1, "seconds": 1.0, "momus": out}
    text = R.report_ask(res, "Вопрос?", cfg)
    assert "цитаты" in text, text[-800:]
    assert "без опоры 1" in text, text[-800:]

    # Все направления упали: ключей нет, но типы остаются предсказуемыми.
    def dead(cfg, role, msgs, **kw):
        raise RuntimeError("шлюз недоступен")

    out2 = R.run_momus(cfg, "ПАК", "Вопрос?", "Ответ.", call=dead)
    assert out2["model"] == "" and out2["backend"] == 0, (out2["model"], out2["backend"])
    # Полный отказ — «не состоялась», а не «частичная»: разницы для читателя нет, но
    # `degraded` обязан назвать все направления, иначе непонятно, что именно не проверено.
    assert out2["ok"] is False and out2["partial"] is False and out2["clean"] is False, out2
    assert out2["degraded"] == [s["id"] for s in R.MOMUS_ASPECTS], out2["degraded"]
    assert "опора" in R.momus_why(out2), R.momus_why(out2)


@test
def test_chunk_contract_first_chunk_checks_everything(_t: Path):
    """Контракт направлений в кусках: весь ответ проверяется целиком, огрызки — узко.

    Первый кусок — настоящий ответ, и `quotes` с `coverage` проверяются именно там: на
    синтетическом списке «Утверждения для проверки: …» покрытие честно нашло бы «не
    покрыто» на каждой строке, а цитаты искало бы в огрызке. Поэтому следующие куски
    спрашивают только `support` и `contradictions` — это сужение, записанное в контракт,
    а не пропущенная проверка.
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=True)
    asked = []

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        narrowed = "Утверждения для проверки:" in text
        asked.append((aspect, narrowed))
        if aspect == "support" and not narrowed:
            return _answer(aspect, [{"claim": "срок не назван", "why": "нет",
                                     "severity": "high"}])
        return _answer(aspect, [])

    out = R.run_momus_chunks(cfg, ["ПАК1", "ПАК2"], "Вопрос?", "Ответ.", call=call,
                             answered_in=280.0)
    assert {a for a, narrow in asked if not narrow} == {s["id"] for s in R.MOMUS_ASPECTS}, asked
    assert {a for a, narrow in asked if narrow} == {"support", "contradictions"}, asked
    assert set(out["aspects"]) == {s["id"] for s in R.MOMUS_ASPECTS}, out["aspects"]
    assert out["clean"] is True and out["partial"] is False, out
    assert out["gaps"] == [], out["gaps"]


@test
def test_a_partial_later_chunk_is_not_lost(_t: Path):
    """Частичный ВТОРОЙ кусок так же ломает «полную проверку», как и первый.

    Правда считалась только по первому куску: второй мог потерять направление, а итог
    говорил `complete=True`, `partial=False`, `degraded=[]`. Карточный конвейер получал
    полную проверку там, где половина куска не состоялась.
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=True)

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        narrowed = "Утверждения для проверки:" in text
        if widened_ok(aspect, narrowed):
            return _answer(aspect, [{"claim": "срок не назван", "why": "нет",
                                     "severity": "high"}])
        if aspect == "support":
            # Второй кусок потерял главное направление: модель отошла от схемы.
            return {"ok": True, "model": "m", "backend": 1, "seconds": 0.1, "text": "не JSON"}
        return _answer(aspect, [])

    def widened_ok(aspect: str, narrowed: bool) -> bool:
        return aspect == "support" and not narrowed

    # Первый кусок полный: все направления ответили, находка одна.
    first = R.run_momus(cfg, "ПАК1", "Вопрос?", "Ответ.", call=call)
    assert first["complete"] is True and first["degraded"] == [], first

    out = R.run_momus_chunks(cfg, ["ПАК1", "ПАК2"], "Вопрос?", "Ответ.", call=call)
    assert out["degraded"] == ["support"], out["degraded"]
    # `aspects` собирается по всем кускам: потеря направления на втором видна в его строке,
    # иначе он выглядел бы состоявшимся, а `checked` относился бы только к первому.
    assert out["aspects"]["support"]["ok"] is False, out["aspects"]["support"]
    assert out["aspects"]["support"]["chunks"] == 2, out["aspects"]["support"]
    assert out["aspects"]["contradictions"]["ok"] is True, out["aspects"]["contradictions"]
    assert out["complete"] is False and out["partial"] is True, out
    assert out["clean"] is False, out
    assert out["text"].splitlines()[-1] == "ВЕРДИКТ: БЕЗ ОПОРЫ 1 (проверка частичная)", out["text"]
    assert R.momus_summary(out).startswith("проверка частичная"), R.momus_summary(out)


@test
def test_checked_counts_the_answer_once_not_once_per_aspect(_t: Path):
    """`checked` — сколько утверждений ответа просмотрено, а не сумма просмотров.

    Три модельных направления смотрят один и тот же ответ. Сумма давала «проверено 15»
    там, где пять утверждений посмотрели трижды, и по этому числу человек судил о полноте.
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=True)

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        return _answer(aspect, [], checked=7)

    out = R.run_momus(cfg, "ПАК", "Вопрос?", "Ответ.", call=call)
    assert out["checked"] == 7, out["checked"]
    assert all(a["checked"] == 7 for a in out["aspects"].values()), out["aspects"]


@test
def test_a_claim_of_pure_punctuation_is_not_a_clean_check(_t: Path):
    """Claim из одной пунктуации — невалидная находка, а не «чисто».

    Проверка схемы стояла ДО нормализации: `_short` срезал кавычки, тире и точки, ключ
    дедупа получался пустым, находка молча выпадала — и направление, ответившее мусором,
    выдавало себя за «здесь чисто». `merge` теперь тоже не пропускает пустой ключ молча.
    """
    R = importlib.import_module("agent_runner")
    spec = R.MOMUS_ASPECTS[0]
    for junk in ("...", "---", "!!!", "«»", "  ", "— …"):
        payload = {"aspect": spec["id"], "checked": 5,
                   "findings": [{"claim": junk, "why": "пусто", "severity": "high"}]}
        parsed = R.parse_momus_aspect(json.dumps(payload, ensure_ascii=False), spec)
        assert parsed["ok"] is False and "claim" in parsed["why"], (junk, parsed)

    # Тот же мусор на уровне агрегатора: направление не состоялось, вердикта «чисто» нет.
    cfg = _momus_cfg(split=True)

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        if aspect == "support":
            return {"ok": True, "model": "m", "backend": 1, "seconds": 0.1,
                    "text": json.dumps({"aspect": aspect, "checked": 5,
                                        "findings": [{"claim": "...", "why": "—"}]},
                                       ensure_ascii=False)}
        return _answer(aspect, [])

    out = R.run_momus(cfg, "ПАК", "Вопрос?", "Ответ.", call=call)
    assert out["clean"] is False and out["partial"] is True, out
    assert out["degraded"] == ["support"], out["degraded"]
    assert "ЧИСТО" not in out["text"], out["text"]

    # Защита второго эшелона: мусорная находка, дошедшая до merge минуя разбор.
    merged = R.merge_momus_aspects(
        [{"id": "support", "ok": True, "checked": 3,
          "findings": [{"claim": "!!!", "why": "", "severity": "low"}]},
         {"id": "contradictions", "ok": True, "checked": 1, "findings": []}],
        "Ответ.")
    assert merged["clean"] is False and merged["degraded"] == ["support"], merged


_QA_PACK = "# Пак\n\n## Карточка\n\nТестовый факт."
_QA_TEXT = {
    "planner": '{"title":"План","steps":[{"id":"s1","title":"Шаг"}]}',
    "worker": "Готовый факт [[Карточка]].",
    "critic": '{"ok":true,"issues":[]}',
    "qa": "ВЕРДИКТ: ЧИСТО",
    "synth": "Синтез: факт [[Карточка]].",
}


class _QAClock:
    now = 1_000_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _qa_cfg(total=1200, step=300):
    cfg = _cfg(520)
    cfg["ask_pipeline"] = {"total_seconds": total, "step_seconds": step, "max_workers": 1}
    cfg["backends"][0].update(provider="test-provider", fallback=False)
    return cfg


def _qa_run(tmp, *, total=1200, step=300, durations=None, outcomes=None, hook=None,
            outer_timeout=False, cfg=None, momus=True, pack=_QA_PACK, state=None, event_hook=None):
    import ask_pipeline as P

    resume = state is not None
    if not resume:
        tmp.mkdir(parents=True, exist_ok=True)
    root = state.cwd if resume else make_project(tmp)
    state = state or P.ask_state.AskState(root, "qa-deadline", "Тест")
    clock = _QAClock()
    elapsed = time.perf_counter()
    calls, stages, events = {}, {}, []
    durations = {"planner": 26, "worker": 11, "critic": 13, "qa": 34, "synth": 10,
                 **(durations or {})}

    def invoke(cfg, role, messages, **kw):
        kind = "synth" if "Синтезируй" in messages[0]["content"] else role
        calls[kind] = {**kw, "at": clock()}
        clock.advance(durations[kind])
        if hook:
            hook(kind, clock, state)
        return {"ok": True, "text": _QA_TEXT[kind], "backend": 1, "model": "m",
                "seconds": durations[kind], **(outcomes or {}).get(kind, {})}

    def stage(name, fn, **kw):
        stages[name.removeprefix("ask:")] = kw
        at = clock()
        if name == "ask:qa" and outer_timeout:
            clock.advance(kw["timeout"])
            return P.RC.StageResult(False, timed_out=True, seconds=clock() - at,
                                    error=P.RC.human_error("timeout", "synthetic watchdog"))
        value = fn()
        return P.RC.StageResult(True, value=value, seconds=clock() - at)

    def emit(event):
        events.append(event)
        if event_hook:
            event_hook(event, state)

    with (patch.object(P, "time", SimpleNamespace(time=clock, sleep=time.sleep)),
          patch.object(P.RC, "run_stage", side_effect=stage),
          patch.object(A, "pulse_threads")):
        res = P.run(cfg or _qa_cfg(total, step), root, "Тест", pack=pack, call=invoke,
                    state=state, emit=emit, momus=momus, resume=resume)
    assert time.perf_counter() - elapsed < 2
    return SimpleNamespace(res=res, state=state, calls=calls, stages=stages, events=events)


def _qa_record(run):
    return next(c for c in run.state.manifest()["steps"][0]["checks"] if c["role"] == "qa")


def _qa_events(state, kind):
    return [row for line in state.events_path.read_text().splitlines()
            if (row := json.loads(line))["kind"] == kind]


@test
def test_qa_budget_keeps_the_synthesis_reserve_and_minimum(_tmp: Path):
    import ask_budget as QB

    assert [QB.synthesis_reserve(total, step) for total, step in ((1200, 300), (97, 60), (8, 8))] == [36, 10, 8]
    assert QB.synthesis_reserve(1200, 20) == 20
    for want, left, end, cap, given, outer in (
        (540, 826, 862, 1800, 540, 545), (540, 400, 436, 1800, 400, 400),
        (360, 37, 47, 1800, 37, 37), (360, 7, 17, 1800, 7, 7),
        (360, 0, 10, 1800, 0, 0), (1800, 10, 20, 1800, 10, 10),
        (1800, 2000, 2000, 1800, 1800, 1800), (360, 37, 15, 1800, 37, 15),
    ):
        budget = QB.qa_budget(want, left, end, cap)
        assert (budget.given_s, budget.outer_s) == (given, outer), budget
        assert budget.evidence(0, 360)["capped"] is (given < want), budget


@test
def test_qa_deadline_short_wall_uses_role_budget(tmp: Path):
    run = _qa_run(tmp)
    assert run.stages["qa"]["timeout"] == 365, run.stages
    call = run.calls["qa"]
    assert call["request_timeout"] == call["deadline"] - call["at"] == 360, call
    assert "request_timeout" not in run.calls["worker"] and "request_timeout" not in run.calls["critic"]
    check = _qa_record(run)
    assert check["budget"] == {"want_s": 360, "given_s": 360, "used_s": 34, "capped": False,
                               "role_floor_s": 360, "deadline_left_s": 1114}, check
    assert check["requested"] == {"model": "m", "provider": "test-provider", "source": "prefer"}, check
    assert check["model_source"] == "actual" and check["actual"]["provider"] == "test-provider", check
    fragment = run.state.plan()["steps"][0]["pack"]
    assert fragment == "# Пак\n\n---\n\n" + _QA_PACK.removeprefix("# Пак\n\n"), fragment
    assert check["source"] == {"sha256": importlib.import_module("ask_state").sha256(fragment), "cards": 1}, check
    timing = run.state.state()["timing"]
    assert timing == {"answer_seconds": 94, "deadline_left_s": 1106, "answer_deadline_s": 1164,
                      "synthesis_reserve_s": 36, "synthesis_seconds": 10}, timing
    answer = _qa_events(run.state, "answer")[0]
    assert all(answer[key] == value for key, value in timing.items()) and answer["seconds"] == 94, answer
    assert run.state.state()["status"] == "done" and run.res["completion"]["checks"]["passed"] == 2
    assert run.res["status"] == "full", run.res


@test
def test_qa_deadline_slow_writer_outer_timeout_keeps_evidence(tmp: Path):
    run = _qa_run(tmp, durations={"worker": 160, "critic": 99}, outer_timeout=True)
    check = _qa_record(run)
    assert check["verdict"] == "failed" and check["timed_out"] is True, check
    assert check["model_source"] == "requested" and check["model"] and check["provider"], check
    assert check["budget"]["given_s"] == check["budget"]["want_s"] == 540, check
    assert check["budget"]["used_s"] == run.stages["qa"]["timeout"] == 545, check
    assert check["transport"]["control_returned"] and not check["transport"]["abandoned"], check
    assert check["transport"]["request_stopped"] is None and check["transport"]["slot_released"] is None
    assert check["ttft_ms"] is None and check["latency_ms"] is None and check["slot_wait_ms"] is None
    assert run.res["degraded"] == ["check_failed:qa:s1"] and "synth" in run.calls
    assert run.res["status"] == "partial" and run.res["answer"].startswith("Синтез:"), run.res
    assert "Момус: не выполнен (лимит 540 с)" in run.res["answer"], run.res
    assert run.state.state()["timing"]["answer_seconds"] == 840


@test
def test_qa_deadline_configured_97_clamps_both_guards_and_reserves_synthesis(tmp: Path):
    for outer in (False, True):
        run = _qa_run(tmp / str(outer), total=97, step=60, outer_timeout=outer,
                      durations={"qa": 40 if outer else 34})
        check = _qa_record(run)
        assert run.stages["qa"]["timeout"] == 37, run.stages
        assert check["budget"]["want_s"] == 360 and check["budget"]["given_s"] == 37, check
        assert check["budget"]["used_s"] == (37 if outer else 34) and check["budget"]["capped"], check
        assert run.res["status"] == ("partial" if outer else "full"), run.res
        assert check["verdict"] == ("failed" if outer else "pass"), check
        if not outer:
            call = run.calls["qa"]
            assert call["request_timeout"] == call["deadline"] - call["at"] == 37, call
        timing = run.state.state()["timing"]
        assert timing["answer_seconds"] == (97 if outer else 94), timing
        assert timing["answer_deadline_s"] == 87 and timing["synthesis_reserve_s"] == 10, timing
        assert timing["synthesis_seconds"] == 10 and run.res["answer"].startswith("Синтез:"), run.res


@test
def test_qa_deadline_skips_at_seven_but_runs_at_exactly_ten(tmp: Path):
    for left in (7, 10):
        run = _qa_run(tmp / str(left), total=60 + left, step=60, durations={"qa": 1})
        check = _qa_record(run)
        assert check["status"] == "done" and check["budget"]["given_s"] == left, check
        if left == 7:
            assert "qa" not in run.calls and "qa" not in run.stages, run.calls
            assert check["verdict"] == "missing" and check["skip_reason"] == "remaining_below_min", check
            assert check["budget"]["used_s"] == 0 and check["timed_out"] is False, check
            assert "check_missing:qa:s1" in run.res["degraded"], run.res
            assert "Момус: пропущено, остаток 7 с" in run.res["answer"], run.res
        else:
            assert run.calls["qa"]["request_timeout"] == left and check["verdict"] == "pass", check
            assert run.res["status"] == "full", run.res


@test
def test_qa_deadline_transport_timeout_is_not_an_outer_timeout_or_error_text(tmp: Path):
    for code in ("timeout", "deadline", "unavailable"):
        run = _qa_run(tmp / code, outcomes={"qa": {"ok": False, "error": {
            "code": code, "message": "timeout deadline"}, "model": "other", "provider": "actual"}})
        check = _qa_record(run)
        assert check["status"] == "failed" and check["verdict"] == "failed", check
        assert check["timed_out"] is (code != "unavailable"), check
        assert not check["transport"]["control_returned"] and not check["transport"]["abandoned"], check
        assert check["model_source"] == ("actual" if code == "unavailable" else "requested"), check
        assert check["model"] == ("other" if code == "unavailable" else "m"), check


@test
def test_qa_deadline_mechanically_keeps_answer_when_end_expires(tmp: Path):
    def expire_after_check(kind, clock, state):
        if kind == "qa":
            clock.advance(1200 - (clock() - _QAClock.now))

    run = _qa_run(tmp, hook=expire_after_check)
    assert run.res["ok"] and run.res["status"] == "partial", run.res
    assert "Готовый факт" in run.res["answer"] and "Проверки по шагам" in run.res["answer"]
    assert "synth_fallback" in run.res["degraded"] and "synth" not in run.calls


@test
def test_qa_deadline_synthesis_cannot_deliver_a_late_model_result(tmp: Path):
    run = _qa_run(tmp, total=97, step=60, outer_timeout=True, durations={"synth": 11})
    assert run.calls["synth"]["deadline"] - run.calls["synth"]["at"] == 10, run.calls
    assert run.res["status"] == "partial" and not run.res["answer"].startswith("Синтез:"), run.res
    assert "Готовый факт" in run.res["answer"] and "synth_fallback" in run.res["degraded"], run.res


@test
def test_qa_deadline_no_sources_or_answer_does_not_produce_partial(tmp: Path):
    import ask_pipeline as P

    with patch.object(P.AC, "search_context", return_value={"ok": True, "pack": "# Пак\n\n"}):
        no_sources = _qa_run(tmp / "no-sources", pack="")
    assert no_sources.res["error"]["code"] == "search_empty" and not no_sources.calls, no_sources.res
    empty = _qa_run(tmp / "empty", outcomes={"worker": {"text": " "}})
    assert empty.res["error"]["code"] == "no_answer" and "synth" not in empty.calls, empty.res
    assert any("Ответ: пустой" in w for w in empty.res["steps"][0]["warnings"]), empty.res
    tiny = _qa_run(tmp / "tiny", total=8)
    assert tiny.res["error"]["code"] == "deadline" and not tiny.calls, tiny.res
    assert tiny.state.state()["timing"]["synthesis_reserve_s"] == 8
    for run in (no_sources, empty, tiny):
        assert not run.res["ok"] and run.res["status"] == "error" and run.res["answer"] == "", run.res
        assert not run.state.answer_path.exists() and not any(e["kind"] == "done" for e in run.events)


@test
def test_qa_deadline_empty_step_with_ready_step_uses_p1_partial(tmp: Path):
    import ask_pipeline as P

    root = make_project(tmp)
    state = P.ask_state.AskState(root, "resume-empty-step", "Тест")
    state.create("Тест", {"steps": [{"id": "ready", "pack": _QA_PACK}, {"id": "empty", "pack": ""}]})
    state.write_step("ready", "# Шаг\n\n## Ответ\n\nГотовый факт.\n", answer_chars=len("Готовый факт."))
    state.write_step("ready", "Критик: опора", role="critic", check=True, verdict="pass")
    state.write_step("ready", "ВЕРДИКТ: ЧИСТО", role="qa", check=True, verdict="pass")
    run = _qa_run(tmp, state=state)
    assert run.res["status"] == "partial" and list(run.calls) == ["synth"], run.res
    assert run.res["degraded"] == ["step_failed:empty"], run.res
    assert run.res["steps"][1]["error"]["code"] == "no_sources", run.res


@test
def test_qa_deadline_resume_preserves_diagnostics_and_the_p1_verdict(tmp: Path):
    first = _qa_run(tmp, total=67, step=60)
    check = _qa_record(first)
    resumed = _qa_run(tmp, state=first.state)
    assert _qa_record(resumed) == check and list(resumed.calls) == ["synth"], resumed.res
    assert resumed.res["status"] == "partial" and resumed.res["degraded"] == ["check_missing:qa:s1"], resumed.res
    done = next(e for e in resumed.events if e["kind"] == "done")
    assert done["status"] == "partial" and "timing" not in done, done
    assert "Момус: пропущено, остаток 7 с" in done["answer"], done


@test
def test_qa_deadline_p1_still_owns_missing_remarks_unsupported_and_off(tmp: Path):
    unsupported = _qa_run(tmp / "unsupported", outcomes={
        "qa": {"text": "ВЕРДИКТ: БЕЗ ОПОРЫ 3"}, "critic": {"text": '{"ok":true,"issues":["замечание"]}'}})
    assert _qa_record(unsupported)["verdict"] == "unsupported" and _qa_record(unsupported)["unsupported"] == 3
    assert unsupported.res["degraded"] == ["critic_remarks:s1", "momus_unsupported:s1:3"], unsupported.res
    assert "Момус: без опоры 3" in unsupported.res["answer"] and unsupported.res["status"] == "partial"
    missing = _qa_run(tmp / "missing", outcomes={"qa": {"text": "без вердикта"}})
    assert missing.res["degraded"] == ["check_missing:qa:s1"] and _qa_record(missing)["status"] == "done"
    assert _qa_record(missing)["skip_reason"] == "" and not _qa_record(missing)["timed_out"]
    off = _qa_run(tmp / "off", momus=False)
    assert off.res["status"] == "full" and "qa" not in off.calls
    assert off.res["completion"]["checks"]["required"] == 1, off.res


@test
def test_qa_deadline_empty_ring_is_configuration_failure_not_timeout(tmp: Path):
    cfg = _qa_cfg()
    cfg["backends"] = []
    run = _qa_run(tmp, cfg=cfg)
    check = _qa_record(run)
    assert "qa" not in run.calls and check["verdict"] == "failed" and not check["timed_out"], check
    assert "qa_config" in check["error"] and check["budget"]["used_s"] == 0, check
    assert run.res["degraded"] == ["check_failed:qa:s1"], run.res


@test
def test_qa_deadline_source_counts_card_boundaries_not_inner_headings(tmp: Path):
    pack = "# Пак\n\n---\n\n## Первая\n\n## Описание\n\nФакт.\n\n---\n\n## Вторая\n\nДругой факт."
    run = _qa_run(tmp, pack=pack)
    assert _qa_record(run)["source"]["cards"] == 2, _qa_record(run)
    assert "## Описание" in run.state.plan()["steps"][0]["pack"]


@test
def test_qa_deadline_cancel_vetoes_each_following_call_and_delivery(tmp: Path):
    for stop_at, before, calls in (
        ("planner", "worker", ["planner"]),
        ("worker", "critic", ["planner", "worker"]),
        ("critic", "qa", ["planner", "worker", "critic"]),
        ("synth", "delivery", ["planner", "worker", "critic", "qa", "synth"]),
    ):
        def stop(kind, clock, state):
            if kind == stop_at:
                state.set_control(cancel=True)

        run = _qa_run(tmp / stop_at, hook=stop)
        assert list(run.calls) == calls and run.res["status"] == "cancelled", (run.calls, run.res)
        assert not run.res["ok"] and run.res["answer"] == "" and not run.state.answer_path.exists(), run.res
        assert not any(e["kind"] == "done" for e in run.events), run.events
        if stop_at != "planner":
            assert "Готовый факт" in run.state.step_path("s1").read_text()
        assert any(event.get("cancelled_before") == before for kind in ("cancel", "cancel_checkpoint")
                   for event in _qa_events(run.state, kind)), run.res

    def cancel_after_check(event, state):
        if event["kind"] == "check" and event["role"] == "qa":
            state.set_control(cancel=True)

    before_synth = _qa_run(tmp / "before-synthesis", event_hook=cancel_after_check)
    assert "synth" not in before_synth.calls and before_synth.res["status"] == "cancelled", before_synth.res
    assert _qa_record(before_synth)["verdict"] == "pass" and not before_synth.state.answer_path.exists()


@test
def test_qa_deadline_pause_holds_the_next_call_with_a_usable_budget(tmp: Path):
    import ask_pipeline as P

    state = P.ask_state.AskState(make_project(tmp), "pause-with-reserve", "Тест")
    state.create("Тест", {"steps": [{"id": "s1", "pack": _QA_PACK}]})
    state.set_control(pause=True)
    paused = []

    def resume_at_pause(event, state):
        if event["kind"] == "paused":
            assert state.control()["pause"] and not state.step_path("s1").exists()
            paused.append(event["step"])
            state.set_control(pause=False)

    run = _qa_run(tmp, state=state, cfg=_qa_cfg(97, 60), event_hook=resume_at_pause)
    assert paused == ["s1"] and run.res["status"] == "full", run.res
    assert list(run.calls) == ["worker", "critic", "qa", "synth"], run.calls


def _qa_short_watchdog(P, entered):
    real_stage = P.RC.run_stage

    class AfterTransportEntered(queue.Queue):
        def get(self, block=True, timeout=None):
            # Start the real, short queue timeout after the fake transport is
            # known to own the slot. A loaded machine cannot race the fixture.
            assert entered.wait(1), "QA не дошёл до офлайн-транспорта"
            return super().get(block=block, timeout=timeout)

    def stage(name, fn, **kw):
        if name != "ask:qa":
            return real_stage(name, fn, **kw)
        kw["timeout"] = 0.02  # reliable_core applies its real 0.1-second floor.
        with patch.object(P.RC, "queue", SimpleNamespace(Queue=AfterTransportEntered, Empty=queue.Empty)):
            return real_stage(name, fn, **kw)

    return stage


def _qa_stub_model(role, messages):
    kind = "synth" if "Синтезируй" in messages[0]["content"] else role
    return {"ok": True, "model": "m", "provider": "test-provider", "seconds": 0.01,
            "text": _QA_TEXT[kind]}


class _QASlots:
    def __init__(self):
        self.acquires = 0
        self.releases = 0

    def acquire(self, **_kw):
        assert self.acquires == self.releases, "повторная выдача живого слота"
        self.acquires += 1
        return True

    def release(self):
        assert self.acquires - self.releases == 1, "двойное освобождение слота"
        self.releases += 1


@test
def test_ask_rc_qa_does_not_admit_new_requests_at_the_deadline(tmp: Path):
    import ask_pipeline as P

    root = make_project(tmp)
    observed = {}
    for case, persist in (("fallback", False), ("fallback", True), ("template", True),
                          ("ring", True), ("slot", False), ("probe", False)):
        cfg = _qa_cfg()
        first, = cfg["backends"]
        second = {**first, "n": 2, "provider": "second-provider", "model": "m2",
                  "models": {**first["models"], "qa": "m2"}}
        if case == "fallback":
            cfg["backends"].append(second)
        cfg["chains"] = {"llm": {"qa": cfg["backends"]}}
        cfg["persist"] = persist
        clock = _QAClock()
        cutoff = clock() + 360
        physical = []
        messages = [{"role": "user", "content": "Проверка"}]

        class Slots(_QASlots):
            def acquire(self, **kw):
                if case == "slot" and not self.acquires:
                    clock.advance(360)
                return super().acquire(**kw)

        slots = Slots()

        def transport(op, backend, payload, timeout):
            if op == "slots":
                if case == "probe" and clock() < cutoff:
                    clock.advance(360)
                return 404, {}, None, 0.0
            physical.append((backend["n"], clock() - cutoff))
            if clock() >= cutoff:
                return 200, {"choices": [{"message": {"content": _QA_TEXT["qa"]}}]}, None, 0.01
            clock.advance(360)
            if case == "template":
                return 400, {}, "unknown argument: chat_template_kwargs", 360
            if case == "ring":
                return 200, {}, None, 360
            return 401, {}, "denied", 360

        def call(cfg, role, messages, **kw):
            return A.call_role(cfg, role, messages, transport=transport, sleep=clock.advance, **kw)

        def stage(_name, fn, **_kw):
            return P.RC.StageResult(True, value=fn())

        fake_time = SimpleNamespace(time=clock, monotonic=clock, sleep=clock.advance)
        state = P.ask_state.AskState(root, f"qa-limit-{case}-{persist}")
        with (patch.object(P, "time", fake_time), patch.object(A, "time", fake_time),
              patch.object(P.RC, "run_stage", side_effect=stage), patch.object(A, "_net_gate", return_value=None),
              patch.object(A, "_slot_semaphore", return_value=slots), patch.object(A, "persist_mode", return_value=False),
              patch.dict(A.DOWN, clear=True), patch.dict(A.COOL, clear=True)):
            result = P._run_qa(state, cfg, {"id": "s1", "pack": _QA_PACK}, messages, call, 1,
                               P._RunWindow(clock(), clock() + 550, 50), lambda: False, 0)
        observed[(case, persist)] = (physical, slots.acquires, slots.releases, result["_qa"]["timed_out"])
    assert observed == {
        ("fallback", False): ([(1, -360.0)], 1, 1, True),
        ("fallback", True): ([(1, -360.0)], 1, 1, True),
        ("template", True): ([(1, -360.0)], 1, 1, True),
        ("ring", True): ([(1, -360.0)], 1, 1, True),
        ("slot", False): ([], 1, 1, True),
        ("probe", False): ([], 0, 0, True),
    }, observed


@test
def test_ask_qa_native_transport_starts_no_new_request_after_budget(_tmp: Path):
    for case in ("direct_fallback", "adapter_retry", "hub_send"):
        clock = _QAClock()
        fake_time = SimpleNamespace(time=clock, monotonic=clock, sleep=clock.advance)
        deadline, requests, pending = clock() + 10, [], {}

        def reply(_backend, _payload, _timeout):
            requests.append("adapter")
            if case == "direct_fallback":
                clock.advance(10)
                return None, None, A.ADAPTER_FAIL + "ответ не разобран", 10
            failure = A.AdapterFail(A.ADAPTER_FAIL + "обрыв", link={"kind": "dns"})
            return None, None, failure, 0

        def probe(_backend):
            clock.advance(10)
            return True, ""

        def http(url, payload, _key, _timeout):
            if payload:
                requests.append(url)
            return 404, {}, None, 0

        def send(_line):
            requests.append("hub")
            raise AssertionError("attempted to submit an expired request")

        def hub():
            clock.advance(10)
            return SimpleNamespace(stdin=SimpleNamespace(write=send)), pending

        cfg = {**_qa_cfg(), "adapter": "pydantic_ai", "persist": True}
        slots = _QASlots()
        native = A.pydantic_transport if case == "hub_send" else reply
        with (patch.object(A, "time", fake_time), patch.object(A, "http_json", side_effect=http),
              patch.object(A, "_cache_on", return_value=False), patch.object(A, "probe_gateway", side_effect=probe),
              patch.object(A, "adapter_hub", side_effect=hub), patch.object(A, "pydantic_transport", side_effect=native),
              patch.object(A, "_slot_semaphore", return_value=slots), patch.dict(A.ADAPTER),
              patch.dict(A.DOWN, clear=True), patch.dict(A.COOL, clear=True)):
            res = A.call_role(cfg, "qa", [{"role": "user", "content": "Проверка"}],
                              deadline=deadline, hard_deadline=deadline, sleep=A.NO_WAIT)
        assert requests == ([] if case == "hub_send" else ["adapter"]), (case, requests)
        assert not res["ok"] and res["timed_out"] and not pending, (case, res, pending)
        assert slots.acquires == slots.releases == 1


@test
def test_ask_d_hub_carries_the_deadline_to_first_and_fallback_requests(tmp: Path):
    # Run the actual JSONL adapter in a child. The fake SDK records each model
    # dispatch independently of the parent clock and never opens a connection.
    child = tmp / "offline_adapter.py"
    child.write_text(r'''
import json
import sys
import time
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace as Box

directory, case, trace, start = sys.argv[1:]
sys.path.insert(0, directory)
import pydantic_ai_adapter as adapter

now = float(start) + (10 if case == "first" else 0)
adapter.time = Box(time=lambda: now, monotonic=time.monotonic)
adapter.http_client = object

def request(kind, settings):
    with Path(trace).open("a", encoding="utf-8") as log:
        log.write(json.dumps([kind, now - float(start), settings["timeout"]]) + "\n")

class Agent:
    def __init__(self, *_args, **_kw):
        pass

    @asynccontextmanager
    async def run_stream(self, prompt, *, model_settings, **_kw):
        global now
        request("stream", model_settings)
        now += 10 if case == "expired_fallback" else 3
        raise TimeoutError("fake stream read timed out")
        yield

    async def run(self, prompt, *, model_settings, **_kw):
        request("plain", model_settings)
        return Box(output="ВЕРДИКТ: ЧИСТО", usage=None, all_messages=list)

def stub(**_kw):
    return None

sys.modules.update({
    "pydantic_ai": Box(Agent=Agent, __version__="offline", capture_run_messages=lambda: nullcontext([]),
                       exceptions=Box(ModelHTTPError=type("HTTPError", (Exception,), {}),
                                      UnexpectedModelBehavior=type("Unexpected", (Exception,), {}))),
    "openai": Box(AsyncOpenAI=stub),
    "pydantic_ai.models.openai": Box(OpenAIChatModel=lambda *_a, **_kw: None),
    "pydantic_ai.providers.openai": Box(OpenAIProvider=stub),
    "pydantic_ai.messages": Box(**dict.fromkeys(("ModelRequest", "ModelResponse", "SystemPromptPart",
                                                "TextPart", "ThinkingPart", "UserPromptPart"), Box)),
    "pydantic_ai.usage": Box(UsageLimits=Box),
})
sys.exit(adapter.main())
''', encoding="utf-8")

    def no_direct(_url, payload, _key, _timeout):
        assert not payload, "QA bypassed the fake SDK transport"
        return 404, {}, None, 0

    observed = {}
    for case in ("first", "expired_fallback", "bounded_fallback", "no_hard_deadline"):
        clock = _QAClock()
        fake_time = SimpleNamespace(time=clock, monotonic=clock, sleep=clock.advance)
        trace = tmp / f"{case}.jsonl"
        argv = [sys.executable, str(child), str(SCRIPTS / "agents"), case, str(trace), str(clock())]
        deadline = clock() + 10
        cfg = {**_qa_cfg(), "adapter": "pydantic_ai", "persist": True}
        slots = _QASlots()
        hub = dict(proc=None, broken="", seq=0)
        with (patch.object(A, "_HUB", hub), patch.object(A, "_adapter_argv", return_value=argv),
              patch.object(A, "adapter_selfcheck", return_value={"ok": True}),
              patch.object(A, "time", fake_time), patch.object(A, "_cache_on", return_value=False),
              patch.object(A, "_net_gate", return_value=None), patch.object(A, "http_json", side_effect=no_direct),
              patch.object(A, "_slot_semaphore", return_value=slots), patch.dict(A.ADAPTER)):
            try:
                result = A.call_role(cfg, "qa", [{"role": "user", "content": "Проверка"}],
                                     deadline=deadline, request_timeout=10, sleep=A.NO_WAIT,
                                     hard_deadline=None if case == "no_hard_deadline" else deadline)
                assert hub["seq"] == 1 and not hub["pending"], (case, hub, result)
                assert slots.acquires == slots.releases == 1, (case, result)
            finally:
                proc = hub["proc"]
                if proc is not None:
                    proc.stdin.close()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    until = time.monotonic() + 5
                    while hub["proc"] is proc and time.monotonic() < until:
                        time.sleep(0.01)
                    assert hub["proc"] is None, "hub reader did not finish"
        requests = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()] if trace.exists() else []
        observed[case] = (result["ok"], bool(result.get("timed_out")), result.get("via"), requests)

    assert observed == {
        "first": (False, True, None, []),
        "expired_fallback": (False, True, None, [["stream", 0, 10]]),
        "bounded_fallback": (True, False, "pydantic_ai", [["stream", 0, 10], ["plain", 3, 7]]),
        "no_hard_deadline": (True, False, "pydantic_ai", [["stream", 0, 10], ["plain", 3, 10]]),
    }, observed


@test
def test_qa_deadline_late_real_call_keeps_the_verdict_and_releases_only_in_finally(tmp: Path):
    import ask_pipeline as P

    started = time.perf_counter()
    root = make_project(tmp)
    entered, let_finish = threading.Event(), threading.Event()
    slots = _QASlots()
    physical = []
    attempt = None

    def transport(op, backend, payload, timeout):
        if op != "chat":
            return 404, {}, None, 0.0
        physical.append((backend["n"], timeout))
        entered.set()
        assert let_finish.wait(2), "тест не выпустил офлайн-транспорт"
        answer = {"choices": [{"message": {"content": _QA_TEXT["qa"]}}]}
        return 200, answer, None, 0.01

    def call(cfg, role, messages, **kw):
        if role == "qa":
            return A.call_role(cfg, role, messages, **kw, transport=transport, sleep=A.NO_WAIT)
        return _qa_stub_model(role, messages)

    def run(ident):
        state = P.ask_state.AskState(root, ident, "Тест")
        result = P.run(_qa_cfg(), root, "Тест", pack=_QA_PACK, call=call, state=state)
        return SimpleNamespace(res=result, state=state)

    with (patch.object(P.RC, "run_stage", side_effect=_qa_short_watchdog(P, entered)),
          patch.object(P, "CANCEL_GRACE", 0), patch.object(A, "pulse_threads"),
          patch.object(A, "_slot_semaphore", return_value=slots),
          patch.dict(A.DOWN, clear=True), patch.dict(A.COOL, clear=True), patch.dict(A.INFLIGHT, clear=True)):
        try:
            first = run("late-first")
            saved = first.state.manifest_path.read_bytes()
            failed = _qa_record(first)
            assert first.res["status"] == "partial" and failed["verdict"] == "failed" and failed["timed_out"], failed
            assert failed["transport"] == {"control_returned": True, "cancel_sent": False,
                                            "request_stopped": False, "slot_released": False,
                                            "abandoned": True, "stuck_previous": False}, failed
            attempt, = P._QA_ACTIVE.values()
            assert attempt.id == failed["attempt_id"] and not attempt.done.is_set() and attempt.thread.is_alive()
            assert slots.acquires == A.INFLIGHT[1] == 1 and slots.releases == 0, physical
            for i in range(4):
                retry = run(f"blocked-{i}")
                check = _qa_record(retry)
                assert check["verdict"] == "missing" and check["status"] == "done", check
                assert check["skip_reason"] == "stuck_previous" and check["transport"]["stuck_previous"], check
                assert list(P._QA_ACTIVE) == [attempt.id] and slots.acquires == len(physical) == 1
            let_finish.set()
            attempt.thread.join(1)
            assert attempt.done.is_set() and not attempt.thread.is_alive() and not P._QA_ACTIVE
            assert slots.acquires == slots.releases == 1 and A.INFLIGHT[1] == 0
            assert first.state.manifest_path.read_bytes() == saved
            late, = _qa_events(first.state, "qa_late_done")
            assert late["late_done"] and late["attempt_id"] == attempt.id and late["transport"]["slot_released"], late
            assert "verdict" not in late and late["transport"]["request_stopped"] is None, late
            permit = run("after-release")
            assert permit.res["status"] == "full" and _qa_record(permit)["verdict"] == "pass", permit.res
            assert slots.acquires == slots.releases == len(physical) == 2 and A.INFLIGHT[1] == 0
        finally:
            let_finish.set()
            for live in tuple(P._QA_ACTIVE.values()):
                if live.thread:
                    live.thread.join(1)
    assert time.perf_counter() - started < 2


@test
def test_qa_deadline_during_request_cancel_reports_ack_and_slot_independently(tmp: Path):
    import ask_pipeline as P

    root = make_project(tmp)
    for acknowledge in (True, False):
        state = P.ask_state.AskState(root, f"cancel-{acknowledge}", "Тест")
        let_finish = threading.Event()
        qas, signals = [], []
        slots = _QASlots()

        def call(cfg, role, messages, **kw):
            if role != "qa":
                return _qa_stub_model(role, messages)
            qas.append((kw["attempt_id"], threading.current_thread()))
            slots.acquire()
            try:
                state.set_control(cancel=True)
                assert let_finish.wait(2), "тест не выпустил отменённый транспорт"
            finally:
                slots.release()
            return {**_qa_stub_model(role, messages), "transport": {"slot_released": True}}

        def cancel(attempt_id):
            signals.append(attempt_id)
            if acknowledge:
                let_finish.set()
            return acknowledge

        call.cancel = cancel
        with patch.object(P, "CANCEL_GRACE", 0.5 if acknowledge else 0), patch.object(A, "pulse_threads"):
            try:
                started = time.perf_counter()
                result = P.run(_qa_cfg(), root, "Тест", pack=_QA_PACK, call=call, state=state)
                assert time.perf_counter() - started < 1.5  # Not the 365-second watchdog.
                cancelled, = _qa_events(state, "qa_cancelled")
                facts = cancelled["transport"]
                assert result["status"] == "cancelled" and not result["ok"] and not state.answer_path.exists(), result
                assert not cancelled["timed_out"] and not facts["control_returned"] and facts["cancel_sent"], facts
                assert facts["request_stopped"] is acknowledge and facts["slot_released"] is acknowledge, facts
                assert signals == [qas[0][0]] == [cancelled["attempt_id"]], (qas, signals)
                assert slots.acquires == 1 and slots.releases == int(acknowledge)
                assert not state.check_path("s1", "qa").exists()
                if not acknowledge:
                    attempt, skip = P._reserve_qa(frozenset({1}), time.time() + 20, lambda: False, None, None)
                    assert attempt is None and skip == "stuck_previous", (attempt, skip)
            finally:
                let_finish.set()
                for _, thread in qas:
                    thread.join(1)
        assert not P._QA_ACTIVE and slots.acquires == slots.releases == 1


@test
def test_qa_deadline_global_cap_blocks_a_third_distinct_ring(_tmp: Path):
    import ask_pipeline as P

    entered = [threading.Event(), threading.Event()]
    let_finish = threading.Event()
    threads = []
    attempts = []

    def reserve(*backends):
        return P._reserve_qa(frozenset(backends), time.time() + 20, lambda: False, lambda *_: None, None)

    def block(event):
        event.set()
        assert let_finish.wait(2), "тест не выпустил слот"

    try:
        for event, backend in zip(entered, (1, 2)):
            attempt, skip = reserve(backend)
            assert attempt and not skip
            attempts.append(attempt)
            thread = threading.Thread(target=attempt.invoke, args=(lambda event=event: block(event),))
            threads.append(thread)
            thread.start()
            assert event.wait(1)
            assert attempt.detach(returned=True, deadline=time.time())["abandoned"]
        assert len(P._QA_ACTIVE) == 2 and all(a.abandoned for a in attempts)
        assert reserve(3) == (None, "stuck_previous")
        assert reserve(1, 3) == (None, "stuck_previous")
    finally:
        let_finish.set()
        for thread in threads:
            thread.join(1)
    assert not P._QA_ACTIVE and all(a.done.is_set() for a in attempts)


@test
def test_qa_deadline_a_blocked_cancel_hook_cannot_wait_past_grace_or_repeat(_tmp: Path):
    import ask_pipeline as P

    request_in, request_out, signal_in, signal_out = (threading.Event() for _ in range(4))
    late_logged = threading.Event()
    cancel_threads = []
    key = frozenset({1})
    deadline = time.time() + 20

    def cancel(_attempt_id):
        cancel_threads.append(threading.current_thread())
        signal_in.set()
        assert signal_out.wait(2)
        return True

    def request():
        request_in.set()
        assert request_out.wait(2)
        return {"transport": {"slot_released": True}}

    attempt, skip = P._reserve_qa(key, deadline, lambda: False, lambda *_: late_logged.set(), cancel)
    assert attempt and not skip
    thread = threading.Thread(target=attempt.invoke, args=(request,))
    thread.start()
    try:
        assert request_in.wait(1)
        started = time.perf_counter()
        with patch.object(P, "CANCEL_GRACE", 0.02):
            facts = attempt.detach(returned=True, deadline=deadline)
        assert time.perf_counter() - started < 1 and signal_in.wait(1)
        assert facts["cancel_sent"] and facts["abandoned"] and not facts["request_stopped"], facts
        request_out.set()
        thread.join(1)
        assert attempt.done.is_set() and not late_logged.is_set()
        assert P._reserve_qa(key, deadline, lambda: False, None, None) == (None, "stuck_previous")
    finally:
        request_out.set()
        signal_out.set()
        thread.join(1)
        for cancel_thread in cancel_threads:
            cancel_thread.join(1)
    assert late_logged.is_set() and not P._QA_ACTIVE


@test
def test_qa_deadline_actual_fallback_keeps_p1_notice_and_full(tmp: Path):
    import ask_pipeline as P

    cfg = _qa_cfg()
    first, = cfg["backends"]
    second = {**first, "n": 2, "provider": "second-provider", "model": "m2",
              "models": {**first["models"], "qa": "m2"}}
    cfg["backends"].append(second)
    cfg["chains"] = {"llm": {"worker": [first], "critic": [first], "planner": [first], "qa": [first, second]}}
    physical = []

    def transport(op, backend, payload, timeout):
        if op != "chat":
            return 404, {}, None, 0.0
        physical.append((backend["n"], payload["model"]))
        if backend["n"] == 1:
            return 401, {}, "denied", 0.01
        return 200, {"choices": [{"message": {"content": _QA_TEXT["qa"]}}]}, None, 0.01

    def call(cfg, role, messages, **kw):
        if role == "qa":
            return A.call_role(cfg, role, messages, **kw, transport=transport, sleep=A.NO_WAIT)
        return _qa_stub_model(role, messages)

    root = make_project(tmp)
    state = P.ask_state.AskState(root, "qa-route", "Тест")
    with (patch.object(A, "pulse_threads"), patch.dict(A.DOWN, clear=True),
          patch.dict(A.DOWN_WHY, clear=True), patch.dict(A.COOL, clear=True)):
        result = P.run(cfg, root, "Тест", pack=_QA_PACK, call=call, state=state)
    check = _qa_record(SimpleNamespace(state=state))
    assert physical == [(1, "m"), (2, "m2")], physical
    assert check["requested"] == {"model": "m", "provider": "test-provider", "source": "prefer"}, check
    assert check["actual"] == {"model": "m2", "provider": "second-provider"}, check
    assert check["model_source"] == "actual" and check["transport"]["slot_released"] is True, check
    assert result["status"] == "full" and result["degraded"] == [], result
    assert "routing_fallback:qa:1->2" in result["notices"], result
