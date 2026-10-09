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
    expected = {"ADR-026": "ADR-026-owner-consent-for-executable-content.md",
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
        assert status and status.group(1).startswith("proposed"), (name, head.group(1))
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

        def run(self):
            raise ValueError("сбой посреди маршрута")

        def say(self, kind, text):
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
