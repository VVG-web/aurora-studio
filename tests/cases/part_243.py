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
import sys
import threading
import time
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

    `AG.config()` в обычном режиме собирает настройку через `model_config.to_config`.
    Пока `momus_split` там не было, переменная окружения ни на что не влияла: путь
    разбиения оставался мёртвым в любом рабочем прогоне, а тесты этого не видели, потому
    что включали флаг через `parse_config`.
    """
    MC = importlib.import_module("model_config")
    R = importlib.import_module("agent_runner")
    env = {"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
           "AURORA_AGENT_BACKEND_1_MODEL": "m",
           "AURORA_AGENT_REQUEST_TIMEOUT": "600",
           "AURORA_AGENT_MOMUS_SPLIT": "1"}
    data = MC.from_env(env)
    assert data["settings"]["momus_split"] is True, data["settings"]
    saved = {k: os.environ.get(k) for k in ("AURORA_AGENT_MOMUS_SPLIT",)}
    try:
        for k, v in env.items():
            os.environ[k] = v
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
    assert out2["gaps"] == [], out2["gaps"]
    assert out2["clean"] is True and out2["partial"] is False, out2


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
    """Направления не тянут этап дольше общего срока, а незавершённые названы.

    Четыре направления по 1800 с при ширине 3 давали до 3600 с: `ThreadPoolExecutor`
    ждал все задачи, отмены не было, и внешний таймаут бросал поток, который продолжал
    держать слот шлюза. Теперь у проверки один общий срок, остаток передаётся каждому
    направлению, а не успевшие попадают в `degraded`.
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=True)
    started = time.time()

    def call(cfg, role, msgs, **kw):
        time.sleep(0.4)
        text = msgs[0]["content"]
        aspect = next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text)
        return _answer(aspect, [])

    out = R.run_momus(cfg, "ПАК", "Вопрос?", "Ответ.", call=call,
                      deadline=time.time() + 0.5)
    spent = time.time() - started
    assert spent < 1.6, f"проверка вернулась через {spent:.1f} с при общем сроке 0.5 с"
    assert out["partial"] is True and out["complete"] is False, out
    assert out["degraded"], out["degraded"]
    assert any("общий срок" in (out["aspects"][x].get("why") or "")
               or out["aspects"][x]["ok"] is False for x in out["degraded"]), out["aspects"]
    assert sum(1 for x in out["degraded"] if x) == len(out["degraded"])

    # Ни один поток не должен остаться работать после возврата: считаем живые вызовы.
    live = {"now": 0, "peak": 0}
    lock = threading.Lock()

    def slow(cfg, role, msgs, **kw):
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        try:
            time.sleep(0.5)
            return _answer("support", [])
        finally:
            with lock:
                live["now"] -= 1

    R.run_momus(cfg, "ПАК", "Вопрос?", "Ответ.", call=slow, deadline=time.time() + 0.3)
    time.sleep(0.7)
    assert live["now"] == 0, f"после возврата осталось {live['now']} активных вызовов"


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
def test_chunked_check_does_not_ask_for_quotes_or_coverage(_t: Path):
    """Проверка по кускам сужает направления, а не гоняет все четыре по синтетике.

    Второй и следующий куски проверяют список «Утверждения для проверки: …». Требовать там
    цитаты и покрытие бессмысленно: `coverage` честно вернёт «не покрыто» на каждой строке
    списка, и ответ станет частичным на ровном месте.
    """
    R = importlib.import_module("agent_runner")
    cfg = _momus_cfg(split=True)
    asked = []

    def call(cfg, role, msgs, **kw):
        text = msgs[0]["content"]
        asked.append(next(s["id"] for s in R.MOMUS_ASPECTS if f'"aspect": "{s["id"]}"' in text))
        return _answer(asked[-1], [])

    out = R.run_momus_chunks(cfg, ["ПАК1", "ПАК2"], "Вопрос?", "Ответ.", call=call,
                             answered_in=280.0)
    assert set(asked) == {"support", "contradictions"}, asked
    assert sorted(out["aspects"]) == ["contradictions", "support"], out["aspects"]
    assert out["partial"] is False and out["clean"] is True, out


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
