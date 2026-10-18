"""Проверки движка Aurora, часть 36: пульс шага в Консоли (1.170.0).

Каркас и помощники — tests/harness.py. Ответ модели приходит целиком, и шаг молчит
десятки минут: «выполняется» без изменений не отличало «ждёт модель» от «завис». PRJ-C
09.10.2026: шлюз отдавал 4 токена в секунду вместо 45, двадцать минут в Консоли не менялось
ни слова, и человек спрашивал, ждать или перезапускать. Движок пишет идущие запросы к
моделям в папку прогона, сервер выносит вердикт, Консоль показывает строку пульса.
"""
from __future__ import annotations

from pathlib import Path
import contextlib
import io
import json
import os
import re
import sys
import threading
import time
from datetime import datetime

from harness import KIT, SCRIPTS, card, make_project, run, test, ui_source  # noqa: F401

COCKPIT = KIT / "cockpit"


def _path():
    for p in (str(SCRIPTS), str(COCKPIT)):
        if p not in sys.path:
            sys.path.insert(0, p)


@test
def test_a_model_request_is_in_the_pulse_while_it_waits(tmp: Path):
    """Запрос к модели на время ожидания лежит в `pulse-<pid>.json` папки прогона: шлюз,
    модель, срок; ответ убирает его и считает ответ или сбой. Без папки прогона файла нет."""
    _path()
    import agent_core as AG
    seen = {}
    saved = (AG._transport, os.environ.get("AURORA_RUN_DIR"), dict(AG._PULSE))
    pulse = tmp / f"pulse-{os.getpid()}.json"

    def slow(kind, backend, payload, timeout):
        seen["during"] = json.loads(pulse.read_text(encoding="utf-8")) if pulse.exists() else None
        if payload.get("fail"):
            return 503, None, "HTTP 503", 0.1
        return 200, {"choices": []}, "", 0.1
    try:
        AG._transport = slow
        AG._PULSE.update(seq=0, calls={}, answered=0, failed=0, last_answer=0.0, last_fail=0.0,
                         why="", path=None)
        os.environ["AURORA_RUN_DIR"] = str(tmp)
        backend = {"n": 1, "name": "Шлюз №1", "url": "http://example.com/v1", "key": ""}
        AG.default_transport("chat", backend, {"model": "m-1", "role": "worker"}, 1200)
        call = seen["during"]["inflight"]
        assert len(call) == 1 and call[0]["model"] == "m-1" and call[0]["limit"] == 1200, call
        assert call[0]["name"] == "Шлюз №1" and call[0]["role"] == "worker", call
        AG.default_transport("chat", backend, {"model": "m-1", "fail": True}, 1200)
        after = json.loads(pulse.read_text(encoding="utf-8"))
        assert after["inflight"] == [] and after["answered"] == 1 and after["failed"] == 1, after
        assert after["last_answer"] and "503" in after["why"], after
        AG._pulse_drop()
        assert not pulse.exists(), "пульс закончившегося процесса остался в папке прогона"
        os.environ.pop("AURORA_RUN_DIR")
        AG._PULSE["path"] = None
        AG.default_transport("chat", backend, {"model": "m-1"}, 5)
        assert not list(tmp.glob("pulse-*.json")), "без панели движок намусорил пульсом"
    finally:
        AG._transport = saved[0]
        if saved[1] is None:
            os.environ.pop("AURORA_RUN_DIR", None)
        else:
            os.environ["AURORA_RUN_DIR"] = saved[1]
        AG._PULSE.clear()
        AG._PULSE.update(saved[2])


class _Proc:
    def __init__(self, alive=True):
        self.alive = alive

    def poll(self):
        return None if self.alive else 0


def _job(tmp: Path, now: float, quiet: float, alive=True, parent="r1") -> dict:
    project = tmp / "proj"
    run = project / ".aurora" / "runs" / "run-1"
    run.mkdir(parents=True, exist_ok=True)
    return {"id": "j1", "project": str(project), "run_id": "run-1", "proc": _Proc(alive),
            "started": now - 1500, "last_out": now - quiet, "parent": parent, "done": False}


def _pulse(job: dict, pid: int, calls: list, last_answer: float = 0.0, answered=0, failed=0):
    folder = Path(job["project"]) / ".aurora" / "runs" / job["run_id"]
    (folder / f"pulse-{pid}.json").write_text(json.dumps(
        {"pid": pid, "inflight": calls, "answered": answered, "failed": failed,
         "last_answer": last_answer}), encoding="utf-8")


@test
def test_the_pulse_tells_waiting_from_hanging(tmp: Path):
    """Вердикт пульса: свежая строка — работает; молчит, но запрос к модели в сроке — ждёт
    модель; запрос дольше срока — срок вышел; молчит без запросов — тишина, со сроком
    сторожа; процесса нет — не найден. Пульс мёртвого процесса не считается."""
    _path()
    import aurora_cockpit as ck
    import watchdog as WD
    now = time.time()
    job = _job(tmp, now, quiet=10)
    assert ck.job_pulse(job, now)["state"] == "ok"
    job = _job(tmp, now, quiet=400)
    call = {"since": now - 700, "limit": 1200, "n": 1, "name": "Шлюз №1", "model": "m-1"}
    _pulse(job, os.getpid(), [call, dict(call, since=now - 30)], last_answer=now - 900,
           answered=2, failed=9)
    p = ck.job_pulse(job, now)
    assert p["state"] == "wait" and p["calls"] == 2 and p["oldest"] == 700, p
    assert p["limit"] == 1200 and p["where"] == "Шлюз №1 · m-1" and p["answer_ago"] == 900, p
    assert (p["answered"], p["failed"]) == (2, 9), p
    _pulse(job, os.getpid(), [dict(call, since=now - 1300)])
    assert ck.job_pulse(job, now)["state"] == "late"
    _pulse(job, os.getpid(), [], last_answer=now - 20)
    assert ck.job_pulse(job, now)["state"] == "ok", "свежий ответ модели — шаг работает"
    job = _job(tmp, now, quiet=WD.QUIET - 100)
    _pulse(job, os.getpid(), [])
    assert ck.job_pulse(job, now)["state"] == "ok", "молчит меньше порога сторожа — работает"
    job = _job(tmp, now, quiet=WD.QUIET + 600)
    _pulse(job, 2 ** 22 + 12345, [call])          # процесса с таким номером нет
    p = ck.job_pulse(job, now)
    assert p["state"] == "quiet" and p["calls"] == 0, "пульс мёртвого процесса посчитан"
    assert p["watchdog"] and p["watchdog_in"] == WD.HARD - WD.QUIET - 600, p
    assert ck.job_pulse(_job(tmp, now, quiet=5, alive=False), now)["state"] == "gone"


@test
def test_every_console_stream_carries_and_draws_the_pulse(tmp: Path):
    """Пульс приходит с заданием, маршрутом и цепочкой и рисуется во всех четырёх потоках
    Консоли; строка — под консолью, тексты — в каталогах."""
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'reply["pulse"] = None if reply["done"] else job_pulse(job)' in src
    assert '"pulse": None if run.done_flag else pulse_of(run.job)' in src
    assert 'got["pulse"] = cron_pulse()' in src
    ui = ui_source()
    assert 'id="consolePulse"' in ui and "function drawPulse(" in ui
    for where in ("drawPulse((d && !d.done && !d.error) ? d.pulse : null, me)",   # команда
                  "drawPulse(d.done ? null : d.pulse, me);",                       # маршрут, подключение
                  "drawPulse(d.run ? d.pulse : null, me);"):                       # цепочка
        assert where in ui, f"поток Консоли без пульса: {where}"
    for lang in ("ru", "en"):
        cat = json.loads((COCKPIT / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("pulse.ok", "pulse.wait", "pulse.late", "pulse.quiet", "pulse.gone",
                    "pulse.calls", "pulse.checked"):
            assert cat.get(key), f"{lang}: нет строки {key}"


@test
def test_a_dead_vector_gateway_is_reported_once_by_many_threads(tmp: Path):
    """Шестнадцать потоков узнают упавший шлюз векторов разом — строка о нём одна, с номером
    шлюза, а не шестнадцать одинаковых с его адресом."""
    _path()
    import kb_embed as E
    saved = (E.endpoints, E.AG.http_json, E.load_index)
    gate = threading.Barrier(16)

    def dead(url, body, key, timeout):
        try:
            gate.wait(timeout=5)
        except threading.BrokenBarrierError:
            pass
        return None, None, "URLError: timed out", 0
    E.endpoints = lambda cfg: [{"url": "http://example.com/v1", "key": "", "n": 1}]
    E.AG.http_json = dead
    E.load_index = lambda: {}
    E.DEAD.clear()
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            threads = [threading.Thread(target=E.embed, args=(["вопрос"], {"request_timeout": 1}, "m"))
                       for _ in range(16)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(10)
        lines = [ln for ln in err.getvalue().splitlines() if "до конца прогона без него" in ln]
        assert len(lines) == 1, f"строк об упавшем шлюзе: {len(lines)}"
        assert "№1" in lines[0] and "example.com" not in lines[0], lines[0]
    finally:
        E.endpoints, E.AG.http_json, E.load_index = saved
        E.DEAD.clear()


# ---------------------------------------------------------------- «довести до конца»
# Шаги маршрута и цепочки (`AURORA_PERSIST=1`) идут без потолков по времени: вызов модели не
# сдаётся по сроку и обрыву, срок запроса на медленном шлюзе растёт, бюджета шага нет, маршрут
# ждёт сеть и перезапускает зависший шаг без предела. PRJ-C 09.10.2026: «система должна сама
# ловить сбои, перезапускаться и доводить прогон до конца, если только человек не прервёт».

ANSWER = {"choices": [{"message": {"content": "ответ"}, "finish_reason": "stop"}],
          "usage": {"completion_tokens": 10}}


def _ring(AG):
    cfg = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://gw.example.com/v1",
                           "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    cfg["persist"] = True
    for d in (AG.DOWN, AG.SLOW, AG.LAST_OK):
        d.clear()
    return cfg


@test
def test_a_slow_gateway_gets_more_time_instead_of_a_failure(tmp: Path):
    """Шлюз не уложился в срок — в маршруте это не сбой источника: запрос повторяется, и
    срок на этом шлюзе каждый раз вдвое больше; ответ приходит, медленный шлюз не в карантине.
    В строках хода — запрос, повтор с новым сроком и ответ, с именем источника."""
    _path()
    import agent_core as AG
    cfg = _ring(AG)
    seen = []

    def slow(kind, b, payload, timeout):
        if kind == "slots":
            return 404, None, "нет /slots", 0.0
        seen.append(timeout)
        return (None, None, "timed out", 0.0) if len(seen) < 3 else (200, ANSWER, "", 2.0)
    out = io.StringIO()
    saved = os.environ.get("AURORA_RUN_DIR")
    try:
        os.environ["AURORA_RUN_DIR"] = str(tmp)
        AG.work_on("Sources/Confluence/US-1.md")
        with contextlib.redirect_stdout(out):
            r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=slow,
                             deadline=time.time() + 1, sleep=lambda s: None, request_timeout=100)
    finally:
        AG.work_on("")
        if saved is None:
            os.environ.pop("AURORA_RUN_DIR", None)
        else:
            os.environ["AURORA_RUN_DIR"] = saved
    assert r["ok"] and seen == [100, 200, 400], (seen, r.get("log"))
    assert 1 not in AG.DOWN and 1 not in AG.SLOW, "медленный, но ответивший шлюз выключен"
    text = out.getvalue()
    assert "↗ US-1.md · разбор: запрос → №1 m" in text, text
    assert "⟳ US-1.md · разбор: №1 m не ответил за 1 мин 40 с — повторю" in text, text
    assert "✓ US-1.md · разбор: ответ №1 m за 2 с" in text, text


@test
def test_a_dropped_connection_is_waited_out_and_only_settings_end_a_call(tmp: Path):
    """Обрыв связи в маршруте — ожидание и повтор, а не сбой; без маршрута — прежний ответ
    «повторять незачем». Конец вызова в маршруте — только там, где ожидание не поможет:
    модель круг за кругом отвечает пусто."""
    _path()
    import agent_core as AG
    cfg = _ring(AG)
    saved = AG.DOWN_FOR
    calls = []

    def flaky(kind, b, payload, timeout):
        if kind == "slots":
            return 404, None, "нет /slots", 0.0
        calls.append(1)
        return (None, None, "Connection refused", 0.0) if len(calls) < 3 else (200, ANSWER, "", 1.0)
    try:
        AG.DOWN_FOR = 0                       # карантин в тесте не ждём
        r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=flaky,
                         sleep=lambda s: None)
        assert r["ok"] and len(calls) == 3, r.get("log")
        cfg.pop("persist")
        calls.clear()
        AG.DOWN.clear()
        r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=flaky,
                         deadline=time.time() + 5, sleep=lambda s: None)
        assert not r["ok"] and len(calls) == 1, "без маршрута обрыв вдруг стал ожиданием"
        cfg["persist"] = True
        empty = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        calls.clear()
        AG.DOWN.clear()

        def blank(kind, b, payload, timeout):
            if kind == "slots":
                return 404, None, "нет /slots", 0.0
            calls.append(1)
            return 200, empty, "", 0.5
        r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=blank,
                         sleep=lambda s: None)
        assert not r["ok"] and len(calls) == AG.PERSIST_EMPTY_RINGS, (len(calls), r["log"][-2:])
    finally:
        AG.DOWN_FOR = saved
        AG.DOWN.clear()


@test
def test_route_steps_run_without_a_time_budget(tmp: Path):
    """Шагу маршрута панель ставит `AURORA_PERSIST=1`: у агента нет бюджета по времени, строка
    начала говорит «без предела»; у кнопки — прежний бюджет."""
    _path()
    import agent_core as AG
    saved = os.environ.get("AURORA_PERSIST")
    try:
        os.environ.pop("AURORA_PERSIST", None)
        cfg = AG.apply_persist({"budget_min": 20})
        assert cfg["budget_min"] == 20 and AG.budget_text(cfg) == "бюджет 20 мин"
        os.environ["AURORA_PERSIST"] = "1"
        cfg = AG.apply_persist({"budget_min": 20})
        assert cfg["persist"] and cfg["budget_min"] >= 10 ** 6, cfg
        assert "без предела" in AG.budget_text(cfg)
    finally:
        if saved is None:
            os.environ.pop("AURORA_PERSIST", None)
        else:
            os.environ["AURORA_PERSIST"] = saved
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    at = src.index('env["AURORA_PERSIST"] = "1"')
    assert "if parent:" in src[at - 300:at], "«довести до конца» не только у шагов маршрута"
    runner = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    assert "бюджет {cfg['budget_min']} мин\"" not in runner.replace("исчерпан", ""), \
        "строка начала шага печатает бюджет мимо `budget_text`"


@test
def test_the_watchdog_leaves_a_step_that_waits_for_a_model(tmp: Path):
    """Шаг молчит дольше часа, но пульс говорит «ждёт модель» — сторож его не снимает;
    молчит без запросов — снимает. Маршрут узнаёт по итогу агента, что работа шла."""
    _path()
    import watchdog as WD
    import route_runner as RR
    state = {"s": "wait"}

    class Ck:
        @staticmethod
        def job_pulse(job):
            return {"state": state["s"]}
    now = 1_000_000.0
    w = WD.Watchdog(Ck, probe_fn=lambda project: {"ok": True, "down": []}, clock=lambda: now)
    job = {"id": "j", "project": "", "out": [], "last_out": now - WD.HARD - 60, "started": 0}
    assert w.verdict(job) == "", "сторож снял шаг, который ждёт ответа модели в сроке"
    state["s"] = "quiet"
    assert w.verdict(job), "молчащий без запросов шаг сторож оставил висеть"
    assert RR.made_progress(["  Документы: обработано 2 · пропущено 0"]), \
        "итог агента не засчитан работой — маршрут примет медленный шлюз за обрыв сети"
    assert not RR.looks_offline(["    ⚠ US-1.md · разбор: №1 m: URLError: timed out — подожду и "
                                 "повторю"]), "пережитый шагом обрыв принят за обрыв сети"
    assert RR.looks_offline(["  сбой: URLError: timed out"]), "настоящий обрыв больше не виден"


@test
def test_the_install_blocks_do_not_blink_on_every_redraw(tmp: Path):
    """«Установка»: надстройки, модули Git и MCP в ассистентах строятся один раз, а перерисовка
    раздела переставляет их как есть — без нового запроса и колеса. Ядро перерисовывает
    разделы проекта только по частям здоровья выбранного проекта: на старте части всех
    проектов шли десятками, и блоки мигали (1.170.1)."""
    view = (COCKPIT / "modules" / "install" / "view.js").read_text(encoding="utf-8")
    body = view[view.index("export async function refresh(ctx){"):view.index("/* Надстройки движка")]
    for card in ("extrasCard(ctx)", "gitModsCard(ctx)", "harnessCard(ctx)"):
        assert card not in body, f"перерисовка раздела строит заново {card} — блок снова мигает"
    assert "const keep = cards(ctx);" in body and "box.append(keep.extras, keep.gitmods, keep.harness);" in body
    assert "CARDS.lang !== ctx.lang" in view, "смена языка не перестроит блоки"
    ui = ui_source()
    frame = ui[ui.index("function healthChanged(p){"):ui.index("async function prefetchHealth(")]
    assert "if (p && p === S.project) HEALTH_MINE = true;" in frame and "if (!mine) return;" in frame, \
        "разделы проекта снова перерисовываются по здоровью чужих проектов"
    assert "healthChanged(p);" in ui, "часть здоровья не сообщает, чей это проект"


def _make_with_template(tmp: Path, rel_src: Path, env: dict, fail: dict | None = None):
    """Прогон agent:make на шаблоне из поставки заглушкой вместо модели.

    → (промпты ролей, итог, аргументы ctx_pack). Модель не нужна: заглушка запоминает, что
    ей передали, и отвечает минимально-правильно; `fail` — {роль: строки журнала}, чтобы
    изобразить отказ по размеру."""
    _path()
    import agent_core as A, agent_runner as R
    from harness import stub_messages

    tmp.mkdir(parents=True, exist_ok=True)
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/T.md").write_text(rel_src.read_text(encoding="utf-8"), encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "Док"\n'
                          '    template: "Templates/T.md"\n    out: "Artifacts/ac"\n',
                        encoding="utf-8")
    for i in range(6):
        card(root, f"Concepts/Заявка-{i}.md",
             f"Заявка проходит статусы. Срок десять дней. см. [[Заявка-{(i + 1) % 6}]]",
             status="knowledge", kind="knowledge")
    seen, packs = {}, []

    def fake(cfg_, role, messages, **kw):
        seen[role] = stub_messages(messages, kw)[0]["content"]
        base = {"backend": 1, "model": "m", "tps": 9, "log": []}
        if fail and role in fail:
            return dict(base, ok=False, text="", log=fail[role])
        if role == "planner":
            return dict(base, ok=True, text=json.dumps({"questions": [], "plan": "1. Раздел"},
                                                       ensure_ascii=False))
        if role == "critic":
            return dict(base, ok=True, text=json.dumps({"ok": True, "issues": []}))
        return dict(base, ok=True, text="# Док\n\nЗаявка создана.")

    real = R.run_command

    def spy(cwd, script, args, *a, **kw):
        if script == "ctx_pack.py":
            packs.append(list(args))
        return real(cwd, script, args, *a, **kw)

    cfg = A.parse_config(dict({"AURORA_AGENT_BACKEND_1_URL": "u",
                               "AURORA_AGENT_BACKEND_1_MODEL": "m"}, **env))
    R.run_command = spy
    try:
        res = R.run_make(cfg, str(root), "ac", "статусы заявки", "", "", True, call=fake,
                         momus=False)
    finally:
        R.run_command = real
    return seen, res, packs


@test
def test_the_template_skeleton_is_only_real_sections(tmp: Path):
    """Скелет шаблона — настоящие заголовки будущего документа, не всё, что начинается с `#`.

    Шапка шаблона (YAML с `#`-комментариями), HTML-комментарии с примерами («удалите
    блоки-примеры») и ограждённые блоки заголовками не являются; BOM не прячет первый."""
    _path()
    import agent_runner as R

    assert R.template_headings("\ufeff# A\n## B") == ["# A", "## B"]
    assert R.template_headings("---\n# комментарий\ntitle: x\n---\n# A\n") == ["# A"]
    assert R.template_headings("# A\n<!--\n## в комментарии\n-->\n## B <!-- x -->\n") == \
        ["# A", "## B"]
    assert R.template_headings("# A\n```md\n## в коде\n```\n~~~\n## и тут\n~~~\n## B") == \
        ["# A", "## B"]
    assert R.template_headings("# A\n<!-- не закрыт\n## B") == ["# A"]
    assert R.template_headings("# A\n<!--\nОПЦИОНАЛЬНО: не заголовок\n-->\n") == ["# A"]
    assert R.template_headings("#не заголовок\n####### семь\n") == []

    text = (KIT / "scaffold/TemplatesCommon/us_v1.0.md").read_text(encoding="utf-8")
    heads = R.template_headings(text)
    assert len(heads) >= 20, heads
    for must in ("## Acceptance criteria", "## How to demo", "## Пользовательские сценарии",
                 "### Что сознательно не демонстрируем", "#### ✅ Готово"):
        assert must in heads, f"в скелете US нет раздела {must}"
    ghosts = [h for h in heads if "Паспорт шаблона" in h or "TemplatesCommon/" in h
              or "Критерии успешного приёма" in h or "Критерии неуспешного приёма" in h]
    assert not ghosts, f"в скелет попали строки шапки или примера: {ghosts}"
    block, got = R.template_block(text, 20000, True)
    assert got == heads and "### Что сознательно не демонстрируем — ОПЦИОНАЛЬНО" in block
    assert "## Acceptance criteria — ОПЦИОНАЛЬНО" not in block, \
        "заголовок обязательного раздела помечен необязательным"
    assert "название по смыслу" in block, "про `{{…}}` в заголовках скелет молчит"
    for f in sorted((KIT / "scaffold").glob("Templates*/*.md")):
        assert len(R.template_headings(f.read_text(encoding="utf-8"))) >= 3, f.name


@test
def test_agent_make_gives_every_template_section_to_every_role(tmp: Path):
    """Планировщик, воркер и критик видят ВСЕ разделы шаблона, а не первые 8000 знаков.

    Раньше в промпт шёл `template[:8000]`: общие шаблоны US и AC (25 КБ) обрезались на
    четверти, разделы «Acceptance criteria» и «How to demo» до модели не доходили, а
    воркер и критик сверялись с одним и тем же огрызком — форма не проверялась никем.
    Узкое окно (объявленное) делает из шаблона скелет заголовков для планировщика и
    критика, но не отрезает ни одного раздела; воркер получает шаблон целиком, а пак
    собирается с вычетом шаблона из бюджета."""
    _path()
    import agent_core as A, agent_runner as R

    templates = sorted(p for d in sorted((KIT / "scaffold").glob("Templates*"))
                       for p in d.glob("*.md"))
    assert len(templates) >= 10 and any(p.name == "us_v1.0.md" for p in templates), templates
    big = 0
    for n, src in enumerate(templates):
        text = src.read_text(encoding="utf-8")
        heads = R.template_headings(text)
        assert len(heads) >= 3, f"{src.name}: заголовков {heads}"
        big += len(text) > 8000
        (tmp / f"w{n}").mkdir()
        seen, res, packs = _make_with_template(tmp / f"w{n}", src, {})
        assert res.get("ok"), f"{src.name}: {res.get('why')}"
        assert all("--budget" not in a for a in packs), "окна не объявлены, а пак ограничен"
        for role in ("planner", "worker", "critic"):
            lost = [h for h in heads if h not in seen[role]]
            assert not lost, f"{src.name}: в промпт «{role}» не попали разделы {lost[:3]}"
        if len(text) > 8000:
            assert text[8000:8400] in seen["worker"], \
                f"{src.name}: воркер видит только начало шаблона"
    assert big >= 3, "в поставке не осталось шаблонов длиннее прежнего среза — тест ничего не проверяет"

    # узкое окно: планировщик и критик получают скелет, воркер — шаблон целиком
    src = KIT / "scaffold/TemplatesCommon/us_v1.0.md"
    text = src.read_text(encoding="utf-8")
    heads = R.template_headings(text)
    (tmp / "narrow").mkdir()
    seen, res, packs = _make_with_template(tmp / "narrow", src,
                                           {"AURORA_AGENT_BACKEND_1_CONTEXT": "20000"})
    assert res.get("ok"), res.get("why")
    for role in ("planner", "critic"):
        assert "скелет" in seen[role] and text[8000:8400] not in seen[role], \
            f"«{role}» при узком окне получил полный шаблон — окно не учтено"
        assert all(h in seen[role] for h in heads), f"в скелете «{role}» нет заголовков"
        assert "Паспорт шаблона" not in seen[role] and "Критерии неуспешного" not in seen[role], \
            f"в скелете «{role}» строки шапки или примера"
    assert text[8000:8400] in seen["worker"], "воркеру обрезали шаблон — инструкции разделов потеряны"
    # Пак: бюджет уменьшен на шаблон (иначе воркер упирается в окно, а пак узнаёт об этом поздно)
    assert len(packs) == 1 and packs[0].count("--budget") == 1, packs
    budget = int(packs[0][packs[0].index("--budget") + 1])
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "20000"})
    whole = A.prompt_budget(cfg)
    assert whole == 54000 and R.MIN_PACK <= budget <= whole - len(text), (budget, whole, len(text))


@test
def test_the_form_fuse_checks_the_message_that_goes_to_the_model(tmp: Path):
    """Предохранитель сверяет заголовки с ГОТОВЫМ сообщением роли, а не с блоком шаблона.

    Блок и его заголовки берутся из одного текста, поэтому сверка «заголовки в блоке» не
    ловила ничего. Настоящие дефекты — промпт без `{template}` и срез блока на пути к
    модели — теперь останавливают производство до вызова модели и называют роль."""
    _path()
    import agent_runner as R

    src = KIT / "scaffold/TemplatesCommon/us_v1.0.md"
    text = src.read_text(encoding="utf-8")
    heads = R.template_headings(text)
    # узкая проверка самой сверки
    assert R.form_lost("до\nX\nпосле", "X", ["# H"]) == ["# H"]
    assert R.form_lost("до\n# H\nпосле", "X", ["# H"]) == ["# H"], \
        "заголовок из плана или пака принят за заголовок шаблона"
    assert R.form_lost("# H\nX", "# H\nX", ["# H"]) == []
    assert R.form_lost("пусто", "X", []) == ["(шаблон целиком)"]

    for role, name, bare in (
            ("critic", "PROMPT_MAKE_CRITIC", "Проверь «{title}».\nПлан:\n{plan}\nДокумент:\n{draft}\n{agnostic_check}"),
            ("worker", "PROMPT_MAKE_WRITE", "Напиши «{title}» по плану {plan}\n{extra}\n{pack}\n{agnostic}"),
            ("planner", "PROMPT_MAKE_PLAN", "{method}\n{title}\n{idea}\n{extra}\n{pack}\n{answers}")):
        d = tmp / name
        d.mkdir()
        real = getattr(R, name)
        # Заголовки шаблона в самом сообщении есть (в плане) — прежняя сверка пропустила бы.
        setattr(R, name, bare)
        try:
            seen, res, _ = _make_with_template(d, src, {})
        finally:
            setattr(R, name, real)
        assert not res.get("ok") and f"«{role}»" in res["why"] and "не попали разделы" in res["why"], \
            (role, res)
        assert role not in seen, f"модель «{role}» вызвана, хотя шаблон в сообщение не попал"

    # блок режут на пути к сообщению (срез шаблона), заголовки — полные
    (tmp / "cut").mkdir()
    real = R.template_block
    R.template_block = lambda t, b, s: (t[:300], heads)
    try:
        seen, res, _ = _make_with_template(tmp / "cut", src, {})
    finally:
        R.template_block = real
    # причина называет первые потерянные разделы (не больше пяти) и «и ещё N»
    cut = src.read_text(encoding="utf-8")[:300]
    lost = [h for h in heads if h not in cut]
    assert len(lost) > 5, "срез не потерял достаточно разделов для проверки"
    assert not res.get("ok") and "«planner»" in res["why"] and lost[0] in res["why"] \
        and f"и ещё {len(lost) - 5}" in res["why"], (lost, res)
    assert "planner" not in seen, "модель вызвана, хотя шаблон в промпт не попал"


@test
def test_a_template_that_does_not_fit_the_window_is_refused_by_name(tmp: Path):
    """Узкое окно: отказ называет шаблон и нужное окно; молчаливых срезов нет.

    Шаблон идёт воркеру целиком, поэтому, если он не помещается даже в самое широкое окно
    кольца, `agent:make` говорит об этом до сбора пака. Отказ `fits` на воркере (окно
    подошло по расчёту, но запрос вышел больше) тоже получает пометку про шаблон. Шаблон
    длиннее предела чтения останавливает производство, а не режется."""
    _path()
    src = KIT / "scaffold/TemplatesCommon/us_v1.0.md"
    seen, res, packs = _make_with_template(
        tmp / "tiny", src, {"AURORA_AGENT_BACKEND_1_CONTEXT": "8000"})
    assert not res.get("ok"), res
    why = res["why"]
    assert "«Templates/T.md»" in why and "не помещается в окно" in why and "8000" in why \
        and "≥" in why, why
    assert not seen and not packs, "пак собран или модель вызвана при заведомо тесном окне"

    # окно подошло по расчёту, шлюз/расчёт `fits` отказал на воркере: причина называет шаблон
    log = ["№1 m: запрос ≈25001 токенов при объявленном окне 20000 — не отправляю, чтобы "
           "не гасить провайдера ошибкой"]
    seen, res, _ = _make_with_template(tmp / "fits", src,
                                       {"AURORA_AGENT_BACKEND_1_CONTEXT": "20000"},
                                       fail={"worker": log})
    assert not res.get("ok") and "запрос ≈25001" in res["why"], res
    assert "шаблон «Templates/T.md»" in res["why"] and "идёт воркеру целиком" in res["why"] \
        and "окном ≥" in res["why"], res["why"]
    # Другая причина отказа пометки про шаблон не получает
    seen, res, _ = _make_with_template(tmp / "other", src, {}, fail={"worker": ["№1 m: 500"]})
    assert not res.get("ok") and "шаблон «" not in res["why"], res["why"]

    # шаблон длиннее предела чтения
    big = tmp / "big.md"
    big.write_text("# Раздел\n" + "х" * 200_100 + "\n## Хвост\n", encoding="utf-8")
    (tmp / "big_run").mkdir()
    seen, res, _ = _make_with_template(tmp / "big_run", big, {})
    assert not res.get("ok") and "«Templates/T.md»" in res["why"] \
        and "200 000" in res["why"], res
    assert not seen, "модель вызвана на обрезанном шаблоне"


@test
def test_decision_records_follow_the_madr_format(tmp: Path):
    """Записи docs/decisions: файл, статус и все семь разделов MADR на месте (BL-ARC-01).

    ADR порядка разблокировки лежат в открытой папке и до приёмки владельцем остаются
    `proposed`; без обязательных разделов запись превращается в пожелание, а без статуса
    непонятно, принята ли она. Таблица в README должна называть ровно существующие файлы."""
    import re
    base = KIT / "docs" / "decisions"
    readme = (base / "README.md").read_text(encoding="utf-8")
    sections = ["Контекст и проблема", "Факторы решения", "Рассмотренные варианты", "Решение",
                "Последствия", "Проверка", "Дополнительно"]
    expected = {"ADR-026": "ADR-026-executable-content-trust.md",
                "ADR-059": "ADR-059-deployment-profiles.md",
                "ADR-060": "ADR-060-dependency-policy.md",
                "ADR-066": "ADR-066-version-and-release-policy.md"}
    for num, name in expected.items():
        path = base / name
        assert path.is_file(), f"нет записи {name}"
        text = path.read_text(encoding="utf-8")
        head = re.match(r"---\n(.*?)\n---\n", text, re.S)
        assert head, f"{name}: нет шапки YAML"
        status = re.search(r'^status:\s*"?(.+?)"?\s*$', head.group(1), re.M)
        assert status and status.group(1).startswith(("proposed", "accepted")), (name, head.group(1))
        assert re.search(r"^date: \d{4}-\d{2}-\d{2}$", head.group(1), re.M), name
        assert re.search(rf"^# {num}\. ", text, re.M), f"{name}: заголовок не с номером {num}"
        found = re.findall(r"^## (.+)$", text, re.M)
        assert found == sections, (name, found)
        assert f"({name})" in readme, f"README не ссылается на {name}"
        # ссылки между записями ведут на существующие файлы
        for link in re.findall(r"\]\((ADR-[^)#]+\.md)\)", text):
            assert (base / link).is_file(), (name, link)
    # публичная папка: ни имён моделей как авторов, ни клиентских проектов
    for path in base.glob("*.md"):
        low = path.read_text(encoding="utf-8").lower()
        assert "co-authored" not in low and "claude" not in low, path.name
    assert "decisions/README.md" in (KIT / "docs" / "README.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- Cron: сбой не заклинивает цепочку

def _cron_ck():
    """Минимальная «панель» для планировщика: проекты свободны, движок не отстаёт."""
    class Ck:
        DEFAULT_LANG = "ru"
        marks: list = []

        def mark_running(self, *a):
            self.marks.append(a)

        def project_activity(self, project):
            return {}

        def version_gap(self, project):
            return ""
    ck = Ck()
    ck.marks = []                   # вызовы mark_running — для проверки «отметка снята»
    return ck


def _wait_idle(sch, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        with sch.lock:
            if not sch.current and not sch.queue:
                break
        time.sleep(0.02)
    t = getattr(sch, "run_thread", None)
    if t is not None:
        t.join(5)


@test
def test_cron_step_exception_defers_the_project_not_the_chain(tmp: Path):
    """Исключение в шаге одного проекта (не штатный отказ, а непойманное) помечает пункт
    упавшим с причиной и не обрывает цепочку: шаг другого проекта идёт."""
    _path()
    from harness import set_home
    restore = set_home(tmp)
    try:
        import cron as CRON
        import route_runner as RR
        projects = [{"path": "/p/a", "name": "A"}, {"path": "/p/b", "name": "B"}]
        CRON.save_tasks([{"id": "t1", "name": "цепочка", "order": "projects", "on_fail": "next",
                          "steps": [{"kind": "cmd", "project": CRON.ALL, "cmd": "kb:x", "args": []}]}])
        calls = []

        def fake_job(ck, project, cmd, args, **kw):
            calls.append(project)
            if project == "/p/a":
                raise RuntimeError("взрыв в шаге")
            return {"rc": 0, "lines": [], "run_id": "r"}
        saved = RR.run_job
        RR.run_job = fake_job
        try:
            sch = CRON.Scheduler(_cron_ck(), lambda: projects)
            assert sch.enqueue("t1") == {"ok": True}
            _wait_idle(sch)
        finally:
            RR.run_job = saved
        got = CRON.list_runs(1)[0]
        by = {it["project"]: it for it in got["items"]}
        assert by["/p/a"]["status"] == "failed" and "взрыв в шаге" in by["/p/a"]["note"], by["/p/a"]
        assert any("RuntimeError" in line for line in by["/p/a"]["log"]), "трассировки нет в журнале шага"
        assert by["/p/b"]["status"] == "passed", "исключение в шаге оборвало чужой проект"
        assert got["status"] == "failed" and "/p/b" in calls and sch.current is None
    finally:
        restore()


@test
def test_cron_final_save_failure_does_not_wedge_the_scheduler(tmp: Path):
    """Запись итога прогона в `finally` падает (диск, занятый файл): планировщик всё равно
    освобождается и следующая цепочка стартует. Раньше `current` оставался занятым навсегда."""
    _path()
    from harness import set_home
    restore = set_home(tmp)
    try:
        import cron as CRON
        import route_runner as RR
        projects = [{"path": "/p/a", "name": "A"}]
        step = [{"kind": "cmd", "project": "/p/a", "cmd": "kb:x", "args": []}]
        CRON.save_tasks([{"id": "t1", "name": "первая", "order": "projects", "on_fail": "next", "steps": step},
                         {"id": "t2", "name": "вторая", "order": "projects", "on_fail": "next", "steps": step}])
        ran = []
        orig_save, orig_job = CRON.save_run, RR.run_job
        orig_pause = CRON.FINAL_SAVE_PAUSE

        def broken_save(run):
            if run.get("finished"):
                raise OSError("диск полон")
            orig_save(run)

        def fake_job(ck, project, cmd, args, **kw):
            ran.append(project)
            return {"rc": 0, "lines": [], "run_id": "r"}
        CRON.save_run, RR.run_job, CRON.FINAL_SAVE_PAUSE = broken_save, fake_job, 0
        try:
            sch = CRON.Scheduler(_cron_ck(), lambda: projects)
            assert sch.enqueue("t1")["ok"] and sch.enqueue("t2")["ok"]
            _wait_idle(sch)
        finally:
            CRON.save_run, RR.run_job, CRON.FINAL_SAVE_PAUSE = orig_save, orig_job, orig_pause
        assert sch.current is None and not sch.queue, "планировщик заклинило"
        assert len(ran) == 2, f"вторая цепочка не стартовала: {ran}"
        assert any("запись прогона не удалась" in line for line in sch.stream), sch.stream[-5:]
    finally:
        restore()


@test
def test_cron_final_save_retry_writes_the_result(tmp: Path):
    """Первая запись итога падает, вторая (после паузы) проходит: в файле есть status и
    finished — итог не теряется из-за разового сбоя."""
    _path()
    from harness import set_home
    restore = set_home(tmp)
    try:
        import cron as CRON
        import route_runner as RR
        projects = [{"path": "/p/a", "name": "A"}]
        CRON.save_tasks([{"id": "t1", "name": "цепочка", "order": "projects", "on_fail": "next",
                          "steps": [{"kind": "cmd", "project": "/p/a", "cmd": "kb:x", "args": []}]}])
        orig_save, orig_job, orig_pause = CRON.save_run, RR.run_job, CRON.FINAL_SAVE_PAUSE
        fails = []

        def flaky_save(run):
            if run.get("finished") and not fails:
                fails.append(1)
                raise OSError("файл занят")
            orig_save(run)
        CRON.save_run, CRON.FINAL_SAVE_PAUSE = flaky_save, 0
        RR.run_job = lambda ck, project, cmd, args, **kw: {"rc": 0, "lines": [], "run_id": "r"}
        try:
            sch = CRON.Scheduler(_cron_ck(), lambda: projects)
            assert sch.enqueue("t1")["ok"]
            _wait_idle(sch)
        finally:
            CRON.save_run, RR.run_job, CRON.FINAL_SAVE_PAUSE = orig_save, orig_job, orig_pause
        got = CRON.list_runs(1)[0]
        assert fails and got.get("finished") and got["status"] == "passed", got
    finally:
        restore()


@test
def test_cron_next_skips_a_task_removed_from_the_schedule(tmp: Path):
    """Задание стоит в очереди первым, но уже убрано из расписания: оно пропускается, а
    следующее запускается в том же разборе очереди, не через тик."""
    _path()
    from harness import set_home
    restore = set_home(tmp)
    try:
        import cron as CRON
        import route_runner as RR
        projects = [{"path": "/p/a", "name": "A"}]
        CRON.save_tasks([{"id": "t2", "name": "вторая", "order": "projects", "on_fail": "next",
                          "steps": [{"kind": "cmd", "project": "/p/a", "cmd": "kb:x", "args": []}]}])
        ran = []
        orig_job = RR.run_job

        def fake_job(ck, project, cmd, args, **kw):
            ran.append(project)
            return {"rc": 0, "lines": [], "run_id": "r"}
        RR.run_job = fake_job
        try:
            sch = CRON.Scheduler(_cron_ck(), lambda: projects)
            sch.queue.extend([("ghost", "manual", ""), ("t2", "manual", "")])
            sch._next()
            _wait_idle(sch)
        finally:
            RR.run_job = orig_job
        assert ran == ["/p/a"] and not sch.queue, (ran, sch.queue)
    finally:
        restore()


@test
def test_cron_write_json_retries_a_busy_replace(tmp: Path):
    """Подмена файла истории идёт через `aurora_common.replace_file`: первые попытки с
    `PermissionError` (файл читают) не теряют запись, а повторяются; так же — `meta.json`
    маршрута."""
    _path()
    import aurora_common as AC
    import cron as CRON
    import route_runner as RR
    orig = os.replace
    orig_rf = AC.replace_file

    def check(name, write):
        d = tmp / name
        d.mkdir()
        calls = []

        def busy(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("занят читателем")
            orig(src, dst)
        real = AC.os.replace
        AC.os.replace = busy
        # `windows=None` на POSIX не повторяет: включаем повтор, как на Windows
        AC.replace_file = lambda a, b, **k: orig_rf(a, b, windows=True, pause=0, **k)
        try:
            write(d)
        finally:
            AC.os.replace, AC.replace_file = real, orig_rf
        assert len(calls) == 3, (name, calls)
        assert any(f.suffix == ".json" for f in d.iterdir()), (name, list(d.iterdir()))
    check("cron", lambda d: CRON._write_json(str(d / "x.json"), {"a": 1}))
    check("meta", lambda d: RR.write_meta(str(d), {"a": 1}))


@test
def test_cron_watchdog_frees_a_run_whose_thread_died(tmp: Path):
    """Страховка: прогон числится идущим, а потока нет — сторож освобождает планировщик и
    пишет в историю, что прогон потерян; живой поток он не трогает."""
    _path()
    from harness import set_home
    restore = set_home(tmp)
    try:
        import cron as CRON
        ck = _cron_ck()
        sch = CRON.Scheduler(ck, lambda: [])
        run = {"id": "20261009-100000-aaaa", "task": "t1", "name": "x", "status": "running",
               "started": CRON.now_iso(), "finished": "",
               "items": [{"project": "/p/a", "status": "running"}, {"project": "/p/b", "status": "passed"}]}
        CRON.save_run(run)
        gate = threading.Event()
        alive = threading.Thread(target=gate.wait, daemon=True)
        alive.start()
        sch.current, sch.run_thread = run, alive
        sch._unwedge()
        assert sch.current is run, "сторож снял прогон с живым потоком"
        gate.set()
        alive.join(5)
        sch._unwedge()
        assert sch.current is None
        assert any("без потока" in line for line in sch.stream), sch.stream   # видно в Консоли
        saved = CRON._read_json(CRON.run_path(run["id"]), {})
        assert saved["status"] == "failed" and saved["error"] == "run_thread_lost", saved
        # отметка «идёт работа» снята, пункт не остался «идущим» (иначе обновление кита отказывает)
        assert ("cron-" + run["id"], "", "", False) in ck.marks, ck.marks
        by = {it["project"]: it for it in saved["items"]}
        assert by["/p/a"]["status"] == "interrupted" and by["/p/a"]["note"] == "run_thread_lost", by
        assert by["/p/b"]["status"] == "passed", by
    finally:
        restore()


@test
def test_route_safe_run_always_clears_the_running_flag(tmp: Path):
    """Исключение в `run()` маршрута закрывает прогон записью (`failed`), а если не пишется и
    закрытие — всё равно снимает признак «идёт», иначе кнопка «Пройти» отказывает до перезапуска."""
    _path()
    import route_runner as RR

    class Fake:
        done, run_id, result, done_flag = 3, "r1", None, False

        def __init__(self, close_ok):
            self.close_ok, self.said = close_ok, []

        say_ok = True

        def run(self):
            raise ValueError("сбой посреди маршрута")

        def say(self, kind, text):
            if not self.say_ok:
                raise OSError("журнал не пишется")
            self.said.append(text)

        def _close(self, res):
            if not self.close_ok:
                raise OSError("meta.json не пишется")
            self.done_flag = True
            return res
    for ok in (True, False):
        f = Fake(ok)
        res = RR.RouteRun.safe_run(f)
        assert res["reason"] == "failed" and "сбой посреди маршрута" in res["note"], res
        assert f.done_flag is True, f"признак «идёт» остался (закрытие пишется: {ok})"
        # причина и хвост стека — в журнале (оттуда они попадают в пункт Cron)
        said = "\n".join(f.said)
        assert "ValueError" in said and "in run" in said, f.said
    f = Fake(True)
    f.say_ok = False                   # журнал не пишется — прогон всё равно закрывается записью
    res = RR.RouteRun.safe_run(f)
    assert f.done_flag is True and res["reason"] == "failed", res


@test
def test_scheduled_routes_are_pruned_from_the_registry(tmp: Path):
    """Маршруты, запущенные расписанием, не копятся в реестре панели: законченных остаётся не
    больше десяти, идущие не трогаются."""
    _path()
    import aurora_cockpit as ck

    class R:
        def __init__(self, n, done):
            self.run_id, self.done_flag = f"id{n}", done
    saved = dict(ck.ROUTES)
    try:
        ck.ROUTES.clear()
        for n in range(15):
            ck.route_register(R(n, True), "/p")
            time.sleep(0.001)
        live = R(99, False)
        ck.route_register(live, "/p")
        done = [r for r in ck.ROUTES.values() if r["run"].done_flag]
        assert len(done) <= 10 and "id99" in ck.ROUTES, (len(done), list(ck.ROUTES))
    finally:
        ck.ROUTES.clear()
        ck.ROUTES.update(saved)


@test
def test_cron_side_writes_do_not_break_the_tick(tmp: Path):
    """`recover` и `_missed` пишут прогон через защищённую запись: сбой диска не обрывает тик
    и запуск панели, а задание, чей прогон не создался, видно в Консоли."""
    _path()
    from harness import set_home
    restore = set_home(tmp)
    try:
        import cron as CRON
        sch = CRON.Scheduler(_cron_ck(), lambda: [])
        stale = {"id": "20261001-100000-bbbb", "task": "t1", "name": "x", "status": "running",
                 "started": CRON.now_iso(), "finished": "", "panel": 0,
                 "items": [{"project": "/p/a", "status": "running"}]}
        CRON.save_run(stale)
        orig = CRON.save_run

        def broken(run):
            raise OSError("диск полон")
        CRON.save_run = broken
        try:
            sch.recover()                                        # не бросает
            sch.queue.clear()                                    # продолжение свежего — не предмет проверки
            sch._missed({"id": "t1", "name": "x"}, datetime.now(), "already_running")
            CRON.save_tasks([{"id": "t9", "name": "девятая", "order": "projects", "on_fail": "next",
                              "steps": [{"kind": "cmd", "project": "/p/a", "cmd": "kb:x", "args": []}]}])
            sch.queue.append(("t9", "manual", ""))
            sch._next()                                          # _new_run падает на save_run
        finally:
            CRON.save_run = orig
        assert sch.current is None and not sch.queue, (sch.current, sch.queue)
        assert any("запись прогона не удалась" in line for line in sch.stream), sch.stream
        assert any("прогон не создан" in line for line in sch.stream), sch.stream
    finally:
        restore()


@test
def test_route_run_ids_differ_within_one_second(tmp: Path):
    """Ключ реестра ROUTES — `run_id`: два маршрута, стартовавшие в одну секунду (кнопка и
    расписание в разных проектах), получают разные номера, суффикс «-route» остаётся."""
    _path()
    import route_runner as RR

    class Ck:
        def runs_dir(self, project):
            return str(tmp / "runs")

        def who(self, project):
            return ""

        def kit_version(self):
            return ""
    orig, orig_texts = RR._slug, RR.Texts
    RR._slug, RR.Texts = (lambda: "20261009-100000Z"), (lambda ck, lang: None)
    try:
        ids = {RR.RouteRun(Ck(), str(tmp), {"id": "update", "title": "t"}).run_id for _ in range(8)}
    finally:
        RR._slug, RR.Texts = orig, orig_texts
    assert len(ids) == 8, ids
    assert all(re.fullmatch(r"20261009-100000Z-[0-9a-f]{4}-route", i) for i in ids), ids
    src = (KIT / "cockpit" / "route_runner.py").read_text(encoding="utf-8")
    assert ").safe_run()" in src.split("def run_route")[1].split("def main")[0], "CLI идёт мимо safe_run"


@test
def test_cron_run_detail_shows_run_level_error(tmp: Path):
    """Деталь прогона: ответ-ошибка API (без id) — «не найден», а `error` самого прогона
    показывается подписью из каталога; ключи есть в обоих языках."""
    base = KIT / "cockpit" / "modules" / "cron"
    js = (base / "view.js").read_text(encoding="utf-8")
    assert "run.error && !run.id" in js and "note(ctx, run.error)" in js
    for lang in ("ru", "en"):
        cat = json.loads((base / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        assert "cron.note.run_thread_lost" in cat and "cron.save_error" in cat, lang


def _route_ck(tmp: Path):
    """«Панель» для планировщика с настоящим `RouteRun`: папка прогонов, подпись, каталог строк
    и реестр маршрутов (в него расписание кладёт маршрут перед запуском)."""
    ck = _cron_ck()
    ck.registered = []
    ck.runs_dir = lambda project: str(tmp / "runs")
    ck.who = lambda project: ""
    ck.kit_version = lambda: ""
    ck.i18n_catalogue = lambda lang: {"strings": {}}
    ck.route_register = lambda route, project: ck.registered.append(route)
    return ck


@test
def test_cron_route_step_exception_closes_the_route_run(tmp: Path):
    """Маршрут в цепочке Cron идёт через `safe_run`: исключение в `RouteRun.run` закрывает прогон
    маршрута записью (`done_flag`, `meta.json` в статусе failed), пункт цепочки — failed с
    причиной, планировщик свободен. С прямым `run()` маршрут остался бы «идущим»."""
    _path()
    from harness import set_home
    restore = set_home(tmp)
    try:
        import cron as CRON
        import route_runner as RR
        CRON.save_tasks([{"id": "t1", "name": "ночь", "order": "projects", "on_fail": "next",
                          "steps": [{"kind": "route", "project": str(tmp / "proj"), "route": "update"}]}])

        def boom(self):
            raise RuntimeError("взрыв в маршруте")
        saved = RR.scenario, RR.RouteRun.run
        RR.scenario, RR.RouteRun.run = (lambda ck, rid: {"id": rid, "title": "Обновить"}), boom
        try:
            ck = _route_ck(tmp)
            sch = CRON.Scheduler(ck, lambda: [{"path": str(tmp / "proj"), "name": "P"}])
            assert sch.enqueue("t1") == {"ok": True}
            _wait_idle(sch)
        finally:
            RR.scenario, RR.RouteRun.run = saved
        assert ck.registered, "маршрут не дошёл до реестра панели"
        for route in ck.registered:          # проект с провалом пробуется ещё раз в конце цепочки
            assert route.done_flag is True, "маршрут остался «идущим» — кнопка «Пройти» отказала бы"
            assert RR.read_meta(route.base).get("status") == "failed", RR.read_meta(route.base)
        got = CRON.list_runs(1)[0]
        it = got["items"][0]
        assert it["status"] == "failed" and "взрыв в маршруте" in it["note"], it
        assert any("RuntimeError" in line for line in it["log"]), "причины нет в журнале пункта"
        assert sch.current is None
    finally:
        restore()


@test
def test_the_button_route_goes_through_safe_run(tmp: Path):
    """Кнопка «Пройти» тоже зовёт `safe_run`: исключение в маршруте закрывает его (`done_flag`),
    а не оставляет в реестре «идущим» до перезапуска панели."""
    _path()
    import aurora_cockpit as ck
    import route_runner as RR

    class Stub:
        done_flag, run_id, write, sc = False, "stub-route", True, {"id": "update"}
        made: list = []

        def __init__(self, *a, **kw):
            self.made.append(self)

        def run(self):
            raise RuntimeError("взрыв в маршруте")

        def safe_run(self):
            try:
                return self.run()
            except Exception:  # noqa: BLE001
                self.done_flag = True
                return {}
    saved = RR.scenario, RR.RouteRun, ck.RUNNING
    RR.scenario, RR.RouteRun = (lambda c, rid: {"id": rid, "title": "Обновить"}), Stub
    ck.RUNNING = str(tmp / "running.json")      # настоящий cockpit/.running.json не трогаем
    try:
        got = ck.route_start(str(tmp), "update", False)
        assert got == {"route": "stub-route"}, got
        end = time.time() + 5
        while time.time() < end and not (Stub.made and Stub.made[0].done_flag):
            time.sleep(0.02)
        for t in threading.enumerate():       # поток снимает отметку в подменённый файл — дождаться
            if t.name == "aurora-route":
                t.join(5)
        assert Stub.made and Stub.made[0].done_flag is True, "кнопка вызвала run() мимо safe_run"
    finally:
        RR.scenario, RR.RouteRun, ck.RUNNING = saved
        with ck.ROUTES_LOCK:
            ck.ROUTES.pop("stub-route", None)


@test
def test_cron_tick_runs_the_watchdog_and_starts_the_next_chain(tmp: Path):
    """Сторож вызывается из `tick`: прогон без живого потока снимается на ближайшем тике, и
    следующая цепочка из очереди стартует (без вызова в `tick` сторож был бы мёртвым кодом)."""
    _path()
    from harness import set_home
    restore = set_home(tmp)
    try:
        import cron as CRON
        import route_runner as RR
        CRON.save_tasks([{"id": "t9", "name": "девятая", "order": "projects", "on_fail": "next",
                          "steps": [{"kind": "cmd", "project": "/p/a", "cmd": "kb:x", "args": []}]}])
        calls = []

        def fake_job(ck, project, cmd, args, **kw):
            calls.append(project)
            return {"rc": 0, "lines": [], "run_id": "r"}
        saved = RR.run_job
        RR.run_job = fake_job
        try:
            sch = CRON.Scheduler(_cron_ck(), lambda: [{"path": "/p/a", "name": "A"}])
            run = {"id": "20261009-110000-cccc", "task": "t1", "name": "x", "status": "running",
                   "started": CRON.now_iso(), "finished": "", "items": []}
            CRON.save_run(run)
            dead = threading.Thread(target=lambda: None)
            dead.start()
            dead.join(5)
            sch.current, sch.run_thread = run, dead
            sch.queue.append(("t9", "manual", ""))
            sch.tick(datetime.now())
            _wait_idle(sch)
        finally:
            RR.run_job = saved
        assert calls == ["/p/a"], f"следующая цепочка не стартовала после сторожа: {calls}"
        saved_run = CRON._read_json(CRON.run_path(run["id"]), {})
        assert saved_run["error"] == "run_thread_lost", saved_run
    finally:
        restore()


# ---------------------------------------------------------------------------------
# Канал к провайдерам моделей (BL-SEC-19): 429/Retry-After, 5xx, прокси, откат на прямой HTTP.
# Шлюзы — подставные обработчики на 127.0.0.1 внутри теста; время ожидания подменено
# (`sleep` сам снимает паузы), настоящих провайдеров и сети нет.

class _Gateway:
    """Шлюз на 127.0.0.1: ответы берутся из очереди `script` по порядку, последний повторяется.
    Элемент — (код, заголовки, тело) либо "drop" (оборвать соединение без ответа)."""

    def __init__(self, script):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.script, self.hits = list(script), 0
        gw = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                step = gw.script[min(gw.hits, len(gw.script) - 1)]
                gw.hits += 1
                if step == "drop":
                    self.connection.close()
                    return
                code, headers, body = step
                out = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


_OK = (200, {}, {"choices": [{"message": {"content": "ответ"}, "finish_reason": "stop"}],
                 "usage": {"completion_tokens": 3}})


def _limit(wait="2"):
    return (429, {"Retry-After": wait}, {"error": {"message": "слишком часто"}})


def _channel_cfg(AG, *urls):
    env = {}
    for i, u in enumerate(urls, 1):
        env[f"AURORA_AGENT_BACKEND_{i}_URL"] = u
        env[f"AURORA_AGENT_BACKEND_{i}_MODEL"] = "m"
    cfg = AG.parse_config(env)
    cfg["adapter"] = "openai_compat"          # прямой HTTP: адаптер Pydantic AI здесь не нужен
    for d in (AG.DOWN, AG.SLOW, AG.LAST_OK, AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
        d.clear()
    return cfg


class _Env:
    """Шлюзы идут мимо прокси окружения владельца; всё возвращается на место."""

    def __enter__(self):
        self.keep = {k: os.environ.get(k) for k in ("NO_PROXY", "no_proxy")}
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        return self

    def __exit__(self, *a):
        for k, v in self.keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _ask(AG, cfg, sleeps, deadline=60, **kw):
    def sleep(s):
        sleeps.append(s)
        AG.COOL.clear()                       # прошедшее ожидание: паузы вышли
    return AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                        deadline=time.time() + deadline, sleep=sleep, **kw)


@test
def test_retry_after_is_read_from_every_spelling(tmp: Path):
    """Сколько просит подождать шлюз: секунды, дата HTTP, миллисекунды, `x-ratelimit-reset-*`
    (`1.5s`, `6m0s`); мусор — подсказки нет; потолок — час."""
    _path()
    import agent_core as AG
    now = 1_700_000_000.0
    assert AG.retry_hint({"Retry-After": "7"}) == 7.0
    assert AG.retry_hint({"retry-after": "0"}) == 0.0
    assert AG.retry_hint({"retry-after-ms": "1500"}) == 1.5
    date = "Tue, 14 Nov 2023 22:14:30 GMT"       # 1_700_000_070 с
    assert AG.retry_hint({"Retry-After": date}, now=now) == 70.0, "дата HTTP не разобрана"
    assert AG.retry_hint({"x-ratelimit-reset-requests": "1.5s"}) == 1.5
    assert AG.retry_hint({"x-ratelimit-reset-requests": "6m0s",
                          "x-ratelimit-reset-tokens": "20ms"}) == 360.0, \
        "исчерпанный ресурс не назван — берём самый дальний сброс, а не ближайший"
    # Лимит по токенам: у нетронутых запросов сброс малый, у исчерпанных токенов — большой.
    assert AG.retry_hint({"x-ratelimit-reset-requests": "1s", "x-ratelimit-remaining-requests": "59",
                          "x-ratelimit-reset-tokens": "30s",
                          "x-ratelimit-remaining-tokens": "0"}) == 30.0, "взят сброс неисчерпанного"
    assert AG.retry_hint({"retry-after": "nan"}) is None and AG.retry_hint({"retry-after": "-5"}) == 0.0
    assert AG.retry_hint({"x-ratelimit-reset-requests": "1m30s"}) == 90.0
    assert AG.retry_hint({"Retry-After": "скоро"}) is None and AG.retry_hint({}) is None
    assert AG.retry_hint(None) is None
    assert AG.retry_hint({"Retry-After": "999999"}) == AG.RATE_MAX_WAIT


@test
def test_direct_http_keeps_retry_after_on_a_429(tmp: Path):
    """Прямой HTTP не теряет заголовок: текст отказа остаётся строкой, а `retry_after` едет на
    нём же — форма кортежа транспорта прежняя."""
    _path()
    import agent_core as AG
    gw = _Gateway([_limit("3")])
    try:
        with _Env():
            st, body, err, _ = AG.http_json(gw.url + "/chat/completions", {"model": "m"}, "", 5)
    finally:
        gw.close()
    assert st == 429 and isinstance(err, str) and "слишком часто" in err, (st, err)
    assert err.retry_after == 3.0, f"Retry-After потерян: {getattr(err, 'retry_after', '—')}"


@test
def test_rate_limit_pauses_the_pair_and_the_call_goes_on(tmp: Path):
    """429 с Retry-After не сажает провайдера в карантин на 15 минут: вызов ждёт столько, сколько
    просил шлюз, и получает ответ на том же основном шлюзе."""
    _path()
    import agent_core as AG
    gw = _Gateway([_limit("2"), _limit("2"), _OK])
    sleeps = []
    try:
        with _Env():
            cfg = _channel_cfg(AG, gw.url)
            r = _ask(AG, cfg, sleeps)
    finally:
        gw.close()
        for d in (AG.DOWN, AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
            d.clear()
    assert r["ok"] and gw.hits == 3, (gw.hits, r["log"])
    assert 1 not in AG.DOWN, "429 посадил провайдера в карантин"
    assert len(sleeps) == 2 and all(2.0 <= s <= 2.4 for s in sleeps), \
        f"пауза не по просьбе шлюза (2 с + доля): {sleeps}"
    assert any("карантин не ставлю" in x for x in r["log"]), r["log"]


@test
def test_a_long_retry_after_gives_up_without_hammering_or_quarantine(tmp: Path):
    """Шлюз просит подождать дольше, чем есть у вызова: один запрос, внятная строка в журнале,
    провайдер не в карантине (следующий вызов, у которого время есть, спросит его снова)."""
    _path()
    import agent_core as AG
    gw = _Gateway([_limit("600")])
    sleeps = []
    try:
        with _Env():
            cfg = _channel_cfg(AG, gw.url)
            r = _ask(AG, cfg, sleeps, deadline=20, request_timeout=10)
            assert not r["ok"] and gw.hits == 1, f"шлюз долбят, пока он просит подождать: {gw.hits}"
            assert 1 not in AG.DOWN and AG.COOL.get((1, "m")), (AG.DOWN, AG.COOL)
            assert "повторять незачем" in " ".join(r["log"]) and "429" in " ".join(r["log"]), r["log"]
            assert not sleeps, f"ждали впустую, зная, что не дождёмся: {sleeps}"
    finally:
        gw.close()
        for d in (AG.DOWN, AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
            d.clear()


@test
def test_a_rate_limited_primary_does_not_hide_behind_the_fallback(tmp: Path):
    """Основной шлюз отвечает 429, запасной жив: этот вызов уходит запасному, но основной не
    «мёртв» — пока идёт его пауза, его не спрашивают, а после неё он снова первый."""
    _path()
    import agent_core as AG
    primary, spare = _Gateway([_limit("30"), _OK]), _Gateway([_OK])
    try:
        with _Env():
            cfg = _channel_cfg(AG, primary.url, spare.url)
            r = _ask(AG, cfg, [])
            assert r["ok"] and r["backend"] == 2 and primary.hits == 1, (r["log"], primary.hits)
            assert 1 not in AG.DOWN, "основной посажен в карантин за минутный лимит"
            r = _ask(AG, cfg, [])
            assert r["backend"] == 2 and primary.hits == 1, "основного спрашивают во время его паузы"
            AG.COOL.clear()                                   # пауза вышла
            r = _ask(AG, cfg, [])
            assert r["ok"] and r["backend"] == 1, f"основной не вернулся первым: {r['log']}"
    finally:
        primary.close()
        spare.close()
        for d in (AG.DOWN, AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
            d.clear()


@test
def test_a_spent_quota_is_not_waited_out(tmp: Path):
    """429 «деньги кончились» (`insufficient_quota`) ожиданием не лечится: как 401 — один запрос,
    никаких пауз."""
    _path()
    import agent_core as AG
    gw = _Gateway([(429, {}, {"error": {"message": "You exceeded your current quota",
                                        "code": "insufficient_quota"}})])
    sleeps = []
    try:
        with _Env():
            cfg = _channel_cfg(AG, gw.url)
            r = _ask(AG, cfg, sleeps, deadline=30)
    finally:
        gw.close()
    assert not r["ok"] and gw.hits == 1 and not sleeps, (gw.hits, sleeps, r["log"])
    assert not AG.COOL and 1 not in AG.DOWN, "исчерпанная квота названа паузой или карантином"


@test
def test_server_errors_back_off_and_only_repeated_ones_quarantine(tmp: Path):
    """5xx — сбой сервера, а не его смерть: две первые ошибки дают короткую паузу, третья подряд
    уже карантин; обрыв соединения — карантин сразу, как раньше. Ответ сбрасывает счёт."""
    _path()
    import agent_core as AG
    gw = _Gateway([(502, {}, {"error": {"message": "bad gateway"}}), (503, {}, {}), _OK])
    sleeps = []
    try:
        with _Env():
            cfg = _channel_cfg(AG, gw.url)
            r = _ask(AG, cfg, sleeps)
            assert r["ok"] and gw.hits == 3 and 1 not in AG.DOWN, (gw.hits, r["log"])
            assert len(sleeps) == 2 and all(1.0 <= s <= 10.0 for s in sleeps), sleeps
            assert not AG.COOL_HITS, "ответ не сбросил счёт сбоев"
            gw.script, gw.hits = [(500, {}, {"error": {"message": "boom"}})], 0
            r = _ask(AG, cfg, sleeps)
            assert not r["ok"] and gw.hits == AG.SERVER_ERR_DOWN, (gw.hits, r["log"])
            assert 1 in AG.DOWN, "три 5xx подряд не посадили провайдера в карантин"
            AG.DOWN.clear()
            AG.COOL_HITS.clear()
            gw.script, gw.hits = ["drop"], 0
            r = _ask(AG, cfg, sleeps, deadline=20)
            assert not r["ok"] and 1 in AG.DOWN and gw.hits == 1, \
                f"обрыв соединения перестал быть карантином: {r['log']}"
    finally:
        gw.close()
        for d in (AG.DOWN, AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
            d.clear()


@test
def test_pause_without_a_hint_grows_and_is_jittered(tmp: Path):
    """Без подсказки шлюза пауза растёт вдвое с каждым отказом подряд до потолка и размыта
    случайной долей; отказы, пришедшие во время уже идущей паузы, счёт не растят."""
    _path()
    import agent_core as AG
    saved = AG.random
    try:
        for d in (AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
            d.clear()
        rows = []
        for roll in (0.0, 1.0):
            AG.random = type("R", (), {"random": staticmethod(lambda r=roll: r)})
            AG.COOL.clear()
            AG.COOL_HITS.clear()
            row = []
            for _ in range(8):
                AG.COOL.clear()                  # пауза вышла — следующий отказ растит счёт
                row.append(AG.cool_down((1, "m"))[0])
            rows.append(row)
        low, high = rows
        assert low[:3] == [2.5, 5.0, 10.0] and low[-1] == AG.RATE_CAP / 2, low
        assert high[:3] == [5.0, 10.0, 20.0] and high[-1] == AG.RATE_CAP, high
        # во время идущей паузы счёт не растёт, а пауза не сокращается
        AG.COOL.clear()
        AG.COOL_HITS.clear()
        AG.random = type("R", (), {"random": staticmethod(lambda: 0.0)})
        first, h1 = AG.cool_down((1, "m"))
        again, h2 = AG.cool_down((1, "m"))
        assert h1 == h2 == 1 and AG.COOL[(1, "m")] >= time.time() + first - 1, (h1, h2)
    finally:
        AG.random = saved
        for d in (AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
            d.clear()


def _adapter():
    for p in (str(SCRIPTS), str(KIT / "scripts" / "agents")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib
    AD = importlib.import_module("pydantic_ai_adapter")
    return importlib.reload(AD)


@test
def test_adapter_follows_the_environment_proxy_like_urllib(tmp: Path):
    """Прокси для адаптера выбирается так же, как у прямого пути (`urllib`): по схеме адреса и
    `NO_PROXY`; свою машину (петлю) не проксируем."""
    AD = _adapter()
    table = {"http": "http://proxy.example.com:3128", "https": "http://proxy.example.com:3129"}
    no = lambda host: False  # noqa: E731
    assert AD.proxy_for("http://gw.example.com/v1", table, no) == table["http"]
    assert AD.proxy_for("https://gw.example.com:8443/v1", table, no) == table["https"]
    assert AD.proxy_for("ftp://gw.example.com/", table, no) is None, "схемы без прокси"
    assert AD.proxy_for("http://gw.example.com/v1", {}, no) is None
    for local in ("http://127.0.0.1:8000/v1", "http://localhost/v1", "http://[::1]:80/v1",
                  "http://gw.localhost/v1"):
        assert AD.proxy_for(local, table, no) is None, f"{local} ушёл на прокси"
    seen = []
    assert AD.proxy_for("http://gw.example.com:8080/v1", table,
                        lambda h: seen.append(h) or True) is None, "NO_PROXY не учтён"
    assert seen == ["gw.example.com:8080"], seen
    # Настоящий `urllib`: переменные окружения, исключение через no_proxy.
    keep = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")}
    try:
        for k in keep:
            os.environ.pop(k, None)
        os.environ["http_proxy"] = "http://proxy.example.com:3128"
        os.environ["no_proxy"] = "internal.example.com"
        assert AD.proxy_for("http://gw.example.com/v1") == "http://proxy.example.com:3128"
        assert AD.proxy_for("http://internal.example.com/v1") is None
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_adapter_client_really_goes_through_the_proxy(tmp: Path):
    """Клиент адаптера с переменной прокси в окружении доходит до шлюза через прокси: раньше
    явный транспорт не читал переменные, и за корпоративным прокси путь модели был мёртв, а
    прямой — жив. Прокси здесь — подставной 127.0.0.1, отвечающий на любой абсолютный адрес;
    клиент HTTP живёт в venv адаптера, поэтому проверка идёт его питоном (нет venv — пропуск,
    в CI `AURORA_REQUIRE_VENV=1` пропуск запрещает)."""
    import subprocess
    from harness import live_adapter_python
    vpy = live_adapter_python()
    if vpy is None:
        return
    code = (
        "import asyncio, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import pydantic_ai_adapter as AD\n"
        "async def go():\n"
        "    c = AD.http_client()\n"
        "    try:\n"
        "        r = await c.post('http://gw.example.com/v1/chat/completions',\n"
        "                         json={'model': 'm', 'messages': []})\n"
        "        return r.status_code, r.json()['choices'][0]['message']['content']\n"
        "    finally:\n"
        "        await c.aclose()\n"
        "print(asyncio.run(go()))\n")
    proxy = _Gateway([_OK])
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "no_proxy", "all_proxy")}
    env["http_proxy"] = proxy.url.rsplit("/v1", 1)[0]
    try:
        cp = subprocess.run([str(vpy), "-c", code, str(KIT / "scripts" / "agents")], env=env,
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=60, stdin=subprocess.DEVNULL)
    finally:
        proxy.close()
    assert cp.returncode == 0 and "(200, 'ответ')" in cp.stdout, (cp.stdout, cp.stderr[-400:])
    assert proxy.hits == 1, "запрос не прошёл через прокси"


@test
def test_adapter_hands_over_rate_headers_and_connection_failures(tmp: Path):
    """Адаптер отдаёт движку заголовки лимита из ответа сервера (нижним регистром, только
    нужные) и отличает «не дошли» (ConnectError: прямой путь может дойти) от «срок вышел»
    (повтор упрётся в то же)."""
    AD = _adapter()

    class Err(Exception):
        def __init__(self, headers=None, response=None):
            self.headers, self.response = headers, response
    assert AD.rate_headers(Err({"Retry-After": "7", "Set-Cookie": "x", "X-RateLimit-Reset-Tokens": "1s"})) \
        == {"retry-after": "7", "x-ratelimit-reset-tokens": "1s"}

    class Resp:
        headers = {"Retry-After": "9"}
    cause = Err(response=Resp())
    wrapper = Err()
    wrapper.__cause__ = cause
    assert AD.rate_headers(wrapper) == {"retry-after": "9"}, "заголовки причины потеряны"
    assert AD.rate_headers(Err()) == {}
    assert AD.connect_failed({"ModelAPIError", "APIConnectionError", "ConnectError"})
    assert not AD.connect_failed({"ModelAPIError", "APITimeoutError", "APIConnectionError"}), \
        "срок принят за обрыв соединения"
    assert not AD.connect_failed({"ModelAPIError", "ConnectTimeout"})
    assert not AD.connect_failed({"ModelAPIError", "RemoteProtocolError"})


@test
def test_adapter_rate_limit_reaches_the_ring_with_its_hint(tmp: Path):
    """Отказ 429 через адаптер приходит в кольцо с тем же `retry_after`, что и по прямому HTTP."""
    _path()
    import agent_core as AG
    hub = {"proc": object(), "pending": {}}
    saved = (AG.adapter_hub, AG._HUB.get("seq"))

    class Pipe:
        def write(self, line):
            task = json.loads(line)
            slot = hub["pending"][task["id"]]
            slot["out"] = {"ok": False, "where": "server", "status": 429, "body": None,
                           "error": "слишком часто", "headers": {"retry-after": "11"}}
            slot["done"].set()

        def flush(self):
            pass
    proc = type("P", (), {"stdin": Pipe()})()
    AG.adapter_hub = lambda: (proc, hub["pending"])
    try:
        st, body, err, _ = AG.pydantic_transport({"n": 1, "url": "http://gw.example.com/v1", "key": ""},
                                                 {"model": "m", "messages": [
                                                     {"role": "user", "content": "?"}]}, 5)
    finally:
        AG.adapter_hub = saved[0]
    assert st == 429 and err == "слишком часто" and err.retry_after == 11.0, (st, err)


@test
def test_a_call_that_cannot_wait_does_not_spin_the_ring(tmp: Path):
    """Проверка связи и замер ширины зовут вызов с пустым ожиданием. Пауза пары им не пройдёт,
    поэтому 429 и 5xx для них — отказ с причиной в журнале, и вызов возвращается сразу, а не
    крутит кольцо вхолостую до срока (на 4 с срока это были миллионы кругов и строк журнала).
    То же — для любой другой заглушки ожидания: круг, после которого время не сдвинулось, а
    пары всё ещё на паузе, ничего не изменит."""
    _path()
    import agent_core as AG
    cases = [("429 с Retry-After", [_limit("3")], "просит подождать"),
             ("502 без подсказки", [(502, {}, {"error": {"message": "bad gateway"}})], "пауза")]
    for name, script, word in cases:
        for sleep in (AG.NO_WAIT, lambda s: None):
            gw = _Gateway(script)
            try:
                with _Env():
                    cfg = _channel_cfg(AG, gw.url)
                    t0 = time.time()
                    r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                                     deadline=time.time() + 5, sleep=sleep)
                    took = time.time() - t0
            finally:
                gw.close()
            label = f"{name}, {'NO_WAIT' if sleep is AG.NO_WAIT else 'пустая заглушка'}"
            assert not r["ok"] and took < 2.0, (label, took)
            assert len(r["log"]) < 20, (label, len(r["log"]))
            assert gw.hits == 1, (label, "шлюз спрошен повторно", gw.hits)
            assert 1 not in AG.DOWN, label
            assert any(word in x for x in r["log"]), (label, r["log"])
            for d in (AG.DOWN, AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
                d.clear()


@test
def test_a_patient_call_still_waits_out_a_rate_limit(tmp: Path):
    """Нетерпеливость — только у заглушки `NO_WAIT`: обычный вызов, чей `sleep` ждёт по-настоящему,
    пережидает паузу, как и раньше (проверка, что починка не лишила терпения всех)."""
    _path()
    import agent_core as AG
    gw = _Gateway([_limit("0.4"), _OK])
    try:
        with _Env():
            cfg = _channel_cfg(AG, gw.url)
            r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                             deadline=time.time() + 20, sleep=lambda s: time.sleep(min(s, 0.6)))
    finally:
        gw.close()
        for d in (AG.DOWN, AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
            d.clear()
    assert r["ok"] and gw.hits == 2, (gw.hits, r["log"])


@test
def test_503_with_retry_after_pauses_the_pair_without_quarantine(tmp: Path):
    """503 «перегружен» с `Retry-After` — та же просьба подождать, что и 429: пауза пары на срок
    шлюза, без карантина провайдеру."""
    _path()
    import agent_core as AG
    gw = _Gateway([(503, {"Retry-After": "2"}, {"error": {"message": "overloaded"}}), _OK])
    sleeps = []
    try:
        with _Env():
            cfg = _channel_cfg(AG, gw.url)
            r = _ask(AG, cfg, sleeps)
    finally:
        gw.close()
        for d in (AG.DOWN, AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
            d.clear()
    assert r["ok"] and gw.hits == 2 and 1 not in AG.DOWN, (gw.hits, r["log"])
    assert len(sleeps) == 1 and 2.0 <= sleeps[0] <= 2.4, sleeps
    assert any("карантин не ставлю" in x for x in r["log"]), r["log"]


@test
def test_a_small_or_zero_hint_does_not_pin_the_pause(tmp: Path):
    """«Retry-After: 0» — не подсказка (повтор сразу был бы боем в лимит), а малый срок, который
    шлюз повторяет снова и снова, с третьего отказа подряд — лишь нижняя планка: пауза растёт."""
    _path()
    import agent_core as AG
    saved = AG.random
    try:
        AG.random = type("R", (), {"random": staticmethod(lambda: 0.0)})
        for d in (AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
            d.clear()
        zero, _ = AG.cool_down((1, "m"), 0.0)
        assert zero == AG.RATE_BASE / 2, f"подсказка 0 с оставила паузу пустой: {zero}"
        for d in (AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
            d.clear()
        row = []
        for _ in range(5):
            AG.COOL.clear()                      # пауза вышла — следующий отказ растит счёт
            row.append(AG.cool_down((1, "m"), 1.0)[0])
        assert row[:2] == [1.0, 1.0], row        # пока подсказка шлюза — она и есть пауза
        assert row[2] == 10.0 and row[3] == 20.0 and row[4] == 40.0, \
            f"малая подсказка держит паузу на месте: {row}"
    finally:
        AG.random = saved
        for d in (AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
            d.clear()


@test
def test_adapter_proxy_without_scheme_and_loopback_names(tmp: Path):
    """Прокси без схемы (`host:port`) `urllib` читает как http-прокси, и адаптер читает так же —
    иначе клиент отвергал значение и путь модели молча шёл мимо прокси. Петля — по адресу
    (`127.0.0.0/8`), а не по тексту: имя `127.example.com` — обычное имя и идёт через прокси."""
    AD = _adapter()
    no = lambda host: False  # noqa: E731
    table = {"http": "proxy.example.com:3128"}
    assert AD.proxy_for("http://gw.example.com/v1", table, no) == "http://proxy.example.com:3128"
    assert AD.proxy_for("http://127.example.com/v1", table, no) == "http://proxy.example.com:3128"
    for local in ("http://127.0.0.1:81/v1", "http://[::1]/v1"):
        assert AD.proxy_for(local, table, no) is None, local


@test
def test_adapter_tells_a_refused_port_from_a_drop_after_the_request(tmp: Path):
    """Клиент OpenAI заворачивает в `APIConnectionError` любую транспортную ошибку, поэтому
    различать надо по корню цепочки: отказ порта — «не дошли» (движок повторит прямым HTTP), обрыв
    после отправки запроса — «сервер не ответил» (шлюз запрос получил, второй раз его не шлём)."""
    AD = _adapter()

    def chain(*names):
        """Цепочка исключений: первое имя — внешнее, остальные — причины одна за другой."""
        err = None
        for name in reversed(names):
            e = type(name, (Exception,), {})("x")
            e.__cause__ = err
            err = e
        return err
    refused = AD.exc_chain(chain("ModelAPIError", "APIConnectionError", "ConnectError"))
    dropped = AD.exc_chain(chain("ModelAPIError", "APIConnectionError", "RemoteProtocolError"))
    assert AD.connect_failed(refused) and not AD.connect_failed(dropped), (refused, dropped)
    assert AD.exc_chain(chain("ModelAPIError", "APIConnectionError")) \
        and not AD.connect_failed(AD.exc_chain(chain("ModelAPIError", "APIConnectionError"))), \
        "обёртка без корня принята за «не дошли»"
    # Корень, спрятанный в `__context__` и в группе исключений; цикл не зацикливает обход.
    inner = chain("ConnectError")
    outer = type("ExceptionGroup", (Exception,), {"exceptions": (inner,)})("g")
    loop = chain("A", "B")
    loop.__cause__.__cause__ = loop
    assert "ConnectError" in AD.exc_chain(outer) and AD.exc_chain(loop) == {"A", "B"}
    # Живой клиент OpenAI из venv адаптера (нет venv — пропуск; в CI он обязателен).
    import subprocess
    import socket
    from harness import live_adapter_python
    vpy = live_adapter_python()
    if vpy is None:
        return
    code = (
        "import asyncio, socket, sys\n"
        "sys.dont_write_bytecode = True\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import pydantic_ai_adapter as AD\n"
        "run_task, _ = AD.runtime()\n"
        "async def go():\n"
        "    base = {'key': '', 'model': 'm', 'timeout': 10,\n"
        "            'messages': [{'role': 'user', 'content': 'привет'}]}\n"
        "    out = await run_task(dict(base, url=sys.argv[2]))\n"
        "    print(out.get('where'), out.get('error', '')[:40])\n"
        "asyncio.run(go())\n")
    gw = _Gateway(["drop"])
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_url = f"http://127.0.0.1:{dead.getsockname()[1]}/v1"
    dead.close()
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env["NO_PROXY"] = "127.0.0.1"
    outs = []
    try:
        for url in (gw.url, dead_url):
            cp = subprocess.run([str(vpy), "-c", code, str(KIT / "scripts" / "agents"), url],
                                env=env, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=90, stdin=subprocess.DEVNULL)
            assert cp.returncode == 0, cp.stderr[-400:]
            outs.append(cp.stdout.strip())
    finally:
        gw.close()
    assert outs[0].startswith("server"), f"обрыв после запроса принят за «не дошли»: {outs[0]}"
    assert outs[1].startswith("adapter") and "нет соединения" in outs[1], outs[1]
    assert gw.hits == 1, f"запрос ушёл на шлюз {gw.hits} раз"


@test
def test_adapter_connect_failure_makes_exactly_one_direct_request(tmp: Path):
    """Адаптер сообщил «нет соединения»: вызов без инструментов получает ровно одну контрольную попытку
    прямым HTTP, и её ответ принимается как «обрыв, спасённый прямым запросом» — это не обход адаптера и
    не тревога про Pydantic AI. Вызову с инструментами прямой запрос на чат не поможет: шлюз проверяется
    дешёвым GET, а при недоступности адаптера со второго раза отказ называет причину, не «мимо Pydantic AI»."""
    _path()
    import agent_core as AG
    hub = {"proc": object(), "pending": {}}

    class Pipe:
        def write(self, line):
            task = json.loads(line)
            slot = hub["pending"][task["id"]]
            slot["out"] = {"ok": False, "where": "adapter",
                           "error": "нет соединения: Connection error."}
            slot["done"].set()

        def flush(self):
            pass
    proc = type("P", (), {"stdin": Pipe()})()
    saved = (AG.adapter_hub, AG.note_bypass, dict(AG.ADAPTER))
    AG.adapter_hub = lambda: (proc, hub["pending"])
    said = []
    AG.note_bypass = lambda why, needs_tools=False: said.append((why, needs_tools))
    try:
        for tools in (False, True):
            gw = _Gateway([_OK])
            try:
                with _Env():
                    cfg = _channel_cfg(AG, gw.url)
                    cfg["adapter"] = "pydantic_ai"
                    del said[:]
                    r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                                     deadline=time.time() + 20, sleep=AG.NO_WAIT, tools=tools)
            finally:
                gw.close()
            assert not said, (tools, "обрыв связи объявлен обходом Pydantic AI", said)
            if tools:
                assert gw.hits == 0, (tools, "вызову с инструментами ушёл прямой запрос на чат", gw.hits)
                assert not r["ok"] and not r.get("bypass"), r["log"]
                assert not any("не сработал" in x or "не принимаю" in x for x in r["log"]), r["log"]
            else:
                assert gw.hits == 1, (tools, "прямых запросов", gw.hits)
                assert r["ok"] and not r["bypass"] and r["rescued"], r["log"]
    finally:
        AG.adapter_hub, AG.note_bypass = saved[0], saved[1]
        AG.ADAPTER.clear()
        AG.ADAPTER.update(saved[2])
        for d in (AG.DOWN, AG.COOL, AG.COOL_HITS, AG.ERR_HITS, AG.RESCUE_RUN, AG.LINK_RUN):
            d.clear()


@test
def test_a_lost_connection_is_never_recorded_as_the_reason_for_a_bypass(tmp: Path):
    """Адаптер сказал «нет соединения»: ни при ответе прямого пути, ни без него причина обхода не пишется —
    обрыв связи не поломка адаптера, и итог прогона не зовёт проверять venv. Поломка адаптера иного рода
    пишется как раньше, успешный вызов через адаптер её снимает."""
    import socket
    _path()
    import agent_core as AG
    hub = {"proc": object(), "pending": {}, "out": None}

    class Pipe:
        def write(self, line):
            task = json.loads(line)
            slot = hub["pending"][task["id"]]
            slot["out"] = hub["out"]
            slot["done"].set()

        def flush(self):
            pass
    proc = type("P", (), {"stdin": Pipe()})()
    saved = (AG.adapter_hub, dict(AG.ADAPTER))
    AG.adapter_hub = lambda: (proc, hub["pending"])
    payload = {"model": "m", "adapter": "pydantic_ai",
               "messages": [{"role": "user", "content": "?"}]}
    down = {"ok": False, "where": "adapter", "error": AG.ADAPTER_NO_CONNECT + ": Connection error."}
    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_url = f"http://127.0.0.1:{dead.getsockname()[1]}/v1"
    dead.close()
    try:
        with _Env():
            AG.ADAPTER.update(name="pydantic_ai", fallback_why="")
            hub["out"] = down
            st, _b, err, _t = AG._transport("chat", {"n": 1, "url": dead_url, "key": ""}, payload, 5)
            assert st is None and AG.ADAPTER["fallback_why"] == "", \
                f"обхода не было, а причина записана: {AG.ADAPTER['fallback_why']!r}"
            assert AG.link_kind(err) == "refused", f"закрытый порт назван иначе: {err!r}"
            gw = _Gateway([_OK])
            try:
                st, body, *_ = AG._transport("chat", {"n": 1, "url": gw.url, "key": ""}, payload, 5)
            finally:
                gw.close()
            assert st == 200 and AG.ADAPTER["fallback_why"] == "", AG.ADAPTER["fallback_why"]
            assert body["aurora"].get("rescued") and "bypass_why" not in body["aurora"], body["aurora"]
            # адаптер снова дошёл до сервера — счёт спасённых обрывов обнулён
            hub["out"] = {"ok": True, "text": "да", "usage": {}}
            st, *_ = AG._transport("chat", {"n": 1, "url": dead_url, "key": ""}, payload, 5)
            assert st == 200 and AG.ADAPTER["fallback_why"] == "" and not AG.RESCUE_RUN, AG.RESCUE_RUN
            # другая поломка адаптера (не «нет соединения») пишется как раньше
            hub["out"] = {"ok": False, "where": "adapter", "error": "ответ не разобран"}
            AG._transport("chat", {"n": 1, "url": dead_url, "key": ""}, payload, 5)
            assert "не разобран" in AG.ADAPTER["fallback_why"], AG.ADAPTER["fallback_why"]
        src = (KIT / "scripts" / "agents" / "pydantic_ai_adapter.py").read_text(encoding="utf-8")
        assert f'f"{AG.ADAPTER_NO_CONNECT}: ' in src, "адаптер и движок называют причину по-разному"
    finally:
        AG.adapter_hub = saved[0]
        AG.ADAPTER.clear()
        AG.ADAPTER.update(saved[1])
        AG.RESCUE_RUN.clear()


@test
def test_bypass_reason_travels_with_the_call_not_the_global_field(tmp: Path):
    """Прямой запрос идёт минуты, а общее поле причины обхода снимает любой параллельный вызов,
    дошедший через адаптер: первый вызов не должен остаться с пустой причиной («ошибка движка»).
    Причина едет в ответе (`aurora.bypass_why`); чужой `aurora` без словаря не мешает."""
    import threading
    import time as _t
    _path()
    import agent_core as AG
    ok = {"choices": [{"message": {"content": "ок"}, "finish_reason": "stop"}], "usage": {}}

    def adapter(backend, payload, timeout):
        if payload["messages"][0]["content"] == "A":
            return None, None, AG.ADAPTER_FAIL + "ответ не разобран", 0.0
        return 200, dict(ok, aurora={"via": "pydantic_ai"}), "", 0.0

    direct = {"aurora": "строка"}

    entered = threading.Event()

    def slow_direct(url, payload, key, timeout):
        entered.set()            # первый вызов дошёл до прямого запроса — дальше он «идёт минуты»
        _t.sleep(0.5)
        return 200, dict(ok, **direct), "", 0.5
    saved = (AG.pydantic_transport, AG.http_json, dict(AG.ADAPTER))
    AG.pydantic_transport, AG.http_json = adapter, slow_direct
    be = {"url": "http://example.com/v1", "key": "", "n": 1}

    def ask(text):
        return AG._transport("chat", be, {"model": "m", "adapter": "pydantic_ai",
                                          "messages": [{"role": "user", "content": text}]}, 5)
    got = {}
    try:
        AG.ADAPTER.update(name="pydantic_ai", fallback_why="")
        first = threading.Thread(target=lambda: got.update(a=ask("A")))
        first.start()
        assert entered.wait(5), "первый вызов не дошёл до прямого запроса"
        st, *_ = ask("B")        # пока первый ждёт прямой ответ, второй дошёл через адаптер
        first.join()
        assert st == 200 and AG.ADAPTER["fallback_why"] == "", AG.ADAPTER["fallback_why"]
        aur = got["a"][1]["aurora"]
        assert isinstance(aur, dict) and aur.get("bypass_why") == "ответ не разобран", aur
        # обрыв связи едет так же, но не как причина обхода: ответ несёт «спасённый обрыв»
        AG.pydantic_transport = lambda b, p, t: (None, None, AG.ADAPTER_FAIL
                                                 + AG.ADAPTER_NO_CONNECT + ": обрыв", 0.0)
        _st, body, *_ = ask("C")
        assert body["aurora"].get("rescued") and "bypass_why" not in body["aurora"], body["aurora"]
        AG.pydantic_transport = adapter
        _st, body, *_ = ask("B")
        assert "bypass_why" not in body["aurora"] and "rescued" not in body["aurora"], body["aurora"]
    finally:
        AG.pydantic_transport, AG.http_json = saved[0], saved[1]
        AG.ADAPTER.clear()
        AG.ADAPTER.update(saved[2])


@test
def test_bypass_reason_reaches_the_call_role_when_global_field_was_cleared(tmp: Path):
    """Сквозная проверка: пока прямой запрос идёт, соседний вызов через адаптер снимает общее поле
    причины. ⛔-строка, тревога панели и итог прогона берут причину из ответа (`_call_role`
    читает `aurora.bypass_why` раньше общего поля), а не «ошибку движка»."""
    _path()
    import agent_core as AG
    gw = _Gateway([_OK])
    real_http = AG.http_json
    saved = (AG.pydantic_transport, AG.http_json, AG.note_bypass, dict(AG.ADAPTER))
    said = []

    def wiped_http(url, payload, key, timeout):
        AG.ADAPTER["fallback_why"] = ""      # соседний поток дошёл через адаптер и снял поле
        return real_http(url, payload, key, timeout)
    AG.pydantic_transport = lambda b, p, t: (None, None, AG.ADAPTER_FAIL + "ответ не разобран", 0.0)
    AG.http_json = wiped_http
    AG.note_bypass = lambda why, needs_tools=False: said.append(why)
    try:
        with _Env():
            cfg = _channel_cfg(AG, gw.url)
            cfg["adapter"] = "pydantic_ai"
            AG.ADAPTER.update(name="pydantic_ai", fallback_why="")
            r = _ask(AG, cfg, [])
            assert r["ok"] and r["bypass"] == "ответ не разобран", r
            assert said == ["ответ не разобран"], said
    finally:
        gw.close()
        AG.pydantic_transport, AG.http_json, AG.note_bypass = saved[0], saved[1], saved[2]
        AG.ADAPTER.clear()
        AG.ADAPTER.update(saved[3])
        _clear_cool(AG)


def _clear_cool(AG):
    for d in (AG.DOWN, AG.COOL, AG.COOL_HITS, AG.ERR_HITS):
        d.clear()


@test
def test_rate_limit_storm_does_not_make_one_5xx_a_quarantine(tmp: Path):
    """Три 429 подряд и один 502: «три 5xx подряд» не набралось, провайдер в карантин не идёт.
    Счёт 5xx ведётся отдельно от счёта лимита, а 429 обнуляет серию 5xx."""
    _path()
    import agent_core as AG
    gw = _Gateway([_limit("1"), _limit("1"), _limit("1"),
                   (502, {}, {"error": {"message": "bad gw"}}), _OK])
    sleeps = []
    try:
        with _Env():
            cfg = _channel_cfg(AG, gw.url)
            r = _ask(AG, cfg, sleeps)
            assert r["ok"] and gw.hits == 5, (gw.hits, r["log"])
            assert 1 not in AG.DOWN, f"один 502 после шторма 429 посадил провайдера: {r['log']}"
            assert not any("карантин," in x or "в карантин" in x for x in r["log"]), r["log"]
        # то же на счётчиках: серии разных видов не склеиваются
        _clear_cool(AG)
        for _ in range(3):
            AG.COOL.clear()
            AG.cool_down((1, "m"), 1.0)
        AG.COOL.clear()
        assert AG.cool_down((1, "m"), None, server=True)[1] == 1, "счёт 5xx взят из счёта 429"
        AG.COOL.clear()
        AG.cool_down((1, "m"), 1.0)              # 429 между 5xx обнуляет серию
        AG.COOL.clear()
        assert AG.cool_down((1, "m"), None, server=True)[1] == 1
        for want in (2, 3):
            AG.COOL.clear()
            assert AG.cool_down((1, "m"), None, server=True)[1] == want
        AG.cool_clear((1, "m"))
        assert not AG.ERR_HITS and not AG.COOL_HITS, "ответ не сбросил счёт 5xx"
    finally:
        gw.close()
        _clear_cool(AG)


@test
def test_cool_down_survives_thousands_of_refusals(tmp: Path):
    """За сутки сплошных 429 счёт переваливает за тысячу: `2 ** 1025` не помещается во float и
    раньше ронял вызов `OverflowError`. Пауза остаётся в пределах потолка."""
    _path()
    import agent_core as AG
    try:
        _clear_cool(AG)
        for _ in range(2100):
            AG.COOL.clear()
            wait, hits = AG.cool_down((1, "m"))
            assert 0 < wait <= AG.RATE_CAP, wait
        assert hits == 2100
        AG.COOL.clear()
        wait, _ = AG.cool_down((1, "m"), 3.0)
        assert 0 < wait <= AG.RATE_MAX_WAIT
        AG.COOL.clear()
        assert AG.cool_down((1, "m"), None, server=True)[0] <= AG.RATE_CAP
    finally:
        _clear_cool(AG)


@test
def test_429_with_too_many_tokens_text_is_a_rate_limit_not_an_overflow(tmp: Path):
    """«Too many tokens, please wait» у шлюзов — лимит скорости: 429 ставит паузу паре и вызов
    повторяется, а не сдаётся как «запрос длиннее окна модели»."""
    _path()
    import agent_core as AG
    gw = _Gateway([(429, {"Retry-After": "2"},
                    {"error": {"message": "Too many tokens, please wait before trying again"}}),
                   _OK])
    sleeps = []
    try:
        with _Env():
            cfg = _channel_cfg(AG, gw.url)
            r = _ask(AG, cfg, sleeps)
    finally:
        gw.close()
        _clear_cool(AG)
    assert r["ok"] and gw.hits == 2 and len(sleeps) == 1, (gw.hits, sleeps, r["log"])
    assert not any("длиннее окна" in x for x in r["log"]), r["log"]
    # настоящее переполнение (400) по-прежнему распознаётся
    assert AG.looks_like_overflow("maximum context length is 8192 tokens", None)


@test
def test_every_live_mark_is_a_progress_line_of_the_route(tmp: Path):
    """Каждый знак строк хода (`say_live`) опознаётся маршрутом как строка хода: по ним сеть
    упавшей не считается, даже если текст шлюза в строке упоминает ssl или timeout."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    rr = importlib.import_module("route_runner")
    src = (SCRIPTS / "agent_core.py").read_text(encoding="utf-8")
    marks = set(re.findall(r'say_live\("(.)"', src))
    assert "⏳" in marks and len(marks) >= 5, marks
    for m in sorted(marks):
        line = f"    {m} источник · разбор: №1 m: ssl timeout"
        assert rr.LIVE_LINE.match(line), f"знак {m} не входит в LIVE_LINE"
        assert not rr.looks_offline([line]), f"строка хода {m} сочтена обрывом сети"


@test
def test_probe_tail_does_not_promise_a_quarantine_for_a_rate_limit(tmp: Path):
    """Хвост отчёта `agent:ping` не говорит, что любой отказ шлюза — карантин на 15 минут."""
    src = (SCRIPTS / "agent_probe.py").read_text(encoding="utf-8")
    assert "сажает такой шлюз" not in src, "подсказка устарела: 401/404/429 карантина не дают"
    assert "при HTTP 429 карантина нет" in src and "три 5xx подряд" in src


@test
def test_adapter_remembers_why_the_proxy_did_not_fit(tmp: Path):
    """Причина отката адаптера на прямой путь (класс ошибки, без текста) запоминается и едет
    движку в тексте «нет соединения» — stderr адаптера движок гасит."""
    AD = _adapter()

    class Base:
        pass

    class Plain(Base):
        def __init__(self, proxy=None, **kw):
            if proxy:
                raise ImportError("socks5://user:secret@proxy.example.com — нет пакета socksio")

    fake = type("httpx", (), {"AsyncBaseTransport": Base, "AsyncHTTPTransport": Plain,
                              "AsyncClient": staticmethod(lambda transport=None, **kw: transport)})
    keep = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY",
                                           "no_proxy", "NO_PROXY")}
    saved = AD.http_lib
    try:
        for k in keep:
            os.environ.pop(k, None)
        os.environ["http_proxy"] = "socks5://proxy.example.com:1080"
        os.environ["no_proxy"] = "internal.example.com"
        AD.http_lib = lambda: fake
        AD.PROXY_FELL["why"] = ""
        transport = AD.http_client()
        transport.route("http://gw.example.com/v1")
        assert AD.PROXY_FELL["why"] == "ImportError", AD.PROXY_FELL
        assert "secret" not in AD.PROXY_FELL["why"]
    finally:
        AD.http_lib = saved
        AD.PROXY_FELL["why"] = ""
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    src = (KIT / "scripts" / "agents" / "pydantic_ai_adapter.py").read_text(encoding="utf-8")
    assert "прокси окружения не подошёл клиенту: {PROXY_FELL" in src, "причина не добавлена в отказ"
