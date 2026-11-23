#!/usr/bin/env python3
"""part_261 - конвейер производства: финальная сборка и детерминированная форма (1.231.17).

Цепочка одна и та же на все прогоны: идея → обогащение → план → писатель → Момус →
критик → ремонт → предфинальный Момус → финальный писатель → повторный Момус → форма →
markdown. Финальный писатель собирает документ заново по полному шаблону, а форма
решает, можно ли писать файл. Проверки работают на временном проекте и подменённой
модели: живая сеть и настоящая база не нужны.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from harness import KIT, card, make_project, test

sys.path.insert(0, str(KIT / "scripts"))

TEMPLATE = ("# AC\n\n## Критерии приёмки\n\n"
            "Правило формы: критерий без способа проверки считается недоказанным.\n\n"
            "## Проверка\n")


def _cfg() -> dict:
    import agent_core as A
    return A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u",
                           "AURORA_AGENT_BACKEND_1_MODEL": "m"})


def _project(tmp: Path, template: str = TEMPLATE) -> Path:
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text(template, encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip()
                   + '\nartifacts:\n  ac:\n    title: "AC"\n'
                     '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n',
                   encoding="utf-8")
    for i in range(3):
        card(root, f"Concepts/Заявка-{i}.md",
             "Срок заявки десять дней. Проверяется отчётом.", status="knowledge")
    return root


def _base(text: str, **extra) -> dict:
    return {"ok": True, "text": text, "backend": 1, "model": "m", "log": [],
            "seconds": 0.05, "tokens_in": 20, "tokens_out": 7, **extra}


def _planner(plan: str = "1. Критерии приёмки\n2. Проверка",
             assumptions: list | None = None) -> dict:
    return _base(json.dumps({
        "questions": [], "plan": plan,
        "assumptions": assumptions or [],
        "steps": [{"id": "section-1", "title": "Критерии приёмки",
                   "section": "Критерии приёмки"},
                  {"id": "section-2", "title": "Проверка", "section": "Проверка"}],
    }, ensure_ascii=False))


def _question() -> dict:
    return _base(json.dumps({
        "questions": [{"q": "Какой срок заявки?", "why": "меняет критерий",
                       "rec": "10 дней"}],
        "plan": ""}, ensure_ascii=False))


def _written(version: int = 1, link: str = "[[Заявка-0]]",
             check_head: bool = True) -> str:
    """Документ по форме `TEMPLATE`: все обязательные разделы на месте."""
    text = ("# AC\n\n## Критерии приёмки\n\n"
            f"Версия {version}: срок заявки 10 дней, см. {link}. "
            "Критерий проверяется отчётом.\n\n")
    if check_head:
        text += "## Проверка\n\nСмотрим отчёт и журнал операций, затем закрываем заявку.\n"
    return text


def _critic(ok: bool = True, issues: list | None = None) -> dict:
    return _base(json.dumps({"ok": ok, "issues": issues or []}, ensure_ascii=False))


CLEAN = "1. ОПОРА «срок»\n\nВЕРДИКТ: ЧИСТО"


def _happy(workers: list, roles: list | None = None, qa_texts: list | None = None,
           critic: list | None = None):
    """Подменённая модель счастливого пути: черновик, финал, критик и три Момуса."""
    seen_roles = roles if roles is not None else []
    qa_seen = qa_texts if qa_texts is not None else []
    critic_queue = list(critic or [_critic(True)])

    def fake(cfg, role, messages, **kw):
        seen_roles.append(role)
        if role == "planner":
            return _planner()
        if role == "worker":
            workers.append(1)
            return _base(_written(len(workers)))
        if role == "critic":
            return critic_queue.pop(0) if critic_queue else _critic(True)
        if role == "qa":
            qa_seen.append(messages[0]["content"])
            return _base(CLEAN)
        raise AssertionError(role)
    return fake


@test
def test_make_p1_order_and_final_writer_on_happy_path(tmp: Path):
    """Порядок цепочки буквальный: финальный писатель после предфинального Момуса и до повторного."""
    root = _project(tmp)
    import agent_runner as R
    workers, roles = [], []
    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=_happy(workers, roles), momus=True)
    assert res["ok"], res
    assert roles == ["planner", "worker", "qa", "critic", "qa", "worker", "qa"], roles
    st = res["state"]
    assert st["stages"].get("finalizing"), st["stages"]
    assert st["stages"].get("checked"), st["stages"]
    assert st["pipeline"].get("form_ok") is True, st["pipeline"]
    assert st["best_version"] == 2, st["best_version"]
    doc = (root / st["path"]).read_text(encoding="utf-8")
    assert "status: ready" in doc, doc[:400]
    assert "finalizing:" in doc and "form_ok: true" in doc, doc[:600]


@test
def test_final_writer_gets_full_template_and_all_inputs(tmp: Path):
    """В финальном сообщении — полный шаблон, план, черновик, замечания, Момус, уточнения и допущения."""
    root = _project(tmp)
    import agent_runner as R
    worker_prompts = []
    assumptions = ["срок заявки принят равным десяти дням"]
    rounds = []

    def fake(cfg, role, messages, **kw):
        if role == "planner":
            rounds.append(1)
            if len(rounds) == 1:
                return _question()
            return _planner(assumptions=assumptions)
        if role == "worker":
            worker_prompts.append(messages[0]["content"])
            return _base(_written(len(worker_prompts)))
        if role == "critic":
            return _critic(False, ["нет способа проверки"])
        if role == "qa":
            return _base("ПРЕДФИНАЛ: опора есть\n\nВЕРДИКТ: ЧИСТО")
        raise AssertionError(role)

    first = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", False,
                       call=fake, momus=True)
    assert first["ok"] and first["stage"] == "planning", first
    sid = first["sid"]
    second = R.run_make(_cfg(), str(root), "", "", sid, "10 дней", False,
                        call=fake, momus=True)
    assert second["ok"] and second["stage"] == "done", second
    before_final = R.load_session(str(root), sid)
    assert before_final.get("final_check") == "critic_rejected"
    best = int(before_final.get("best_version") or 1)
    third = R.run_make(_cfg(), str(root), "", "", sid, "", True, call=fake, momus=True)
    assert third["ok"], third
    st = third["state"]
    prompt = worker_prompts[-1]                       # последний вызов — финальный писатель
    assert TEMPLATE in prompt, "финальный писатель не получил шаблон целиком"
    assert "Правило формы" in prompt, "финальный писатель не получил инструкции разделов"
    assert "1. Критерии приёмки" in prompt and "2. Проверка" in prompt, "в промпте нет плана"
    assert f"Версия {best}" in prompt, "в промпте нет лучшего черновика"
    assert "нет способа проверки" in prompt, "в промпте нет замечаний критика"
    assert "ВЕРДИКТ: ЧИСТО" in prompt and "ПРЕДФИНАЛ" in prompt, "в промпте нет разбора Момуса"
    assert "Какой срок заявки?" in prompt and "10 дней" in prompt, "в промпте нет уточнений"
    assert assumptions[0] in prompt, "в промпте нет принятых допущений"
    assert st["stages"].get("finalizing") and st["pipeline"].get("form_ok") is True, st


@test
def test_final_writer_missing_section_retries_once(tmp: Path):
    """Финальный текст без обязательного раздела получает ровно вторую попытку."""
    root = _project(tmp)
    import agent_runner as R
    workers = []
    finals = [0]
    prompts = []

    def fake(cfg, role, messages, **kw):
        if role == "planner":
            return _planner()
        if role == "worker":
            workers.append(1)
            prompts.append(messages[0]["content"])
            if len(workers) == 2:                     # первый финальный текст — без «Проверки»
                finals[0] += 1
                return _base(_written(2, check_head=False))
            return _base(_written(len(workers)))
        if role == "critic":
            return _critic(True)
        if role == "qa":
            return _base(CLEAN)
        raise AssertionError(role)

    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=fake, momus=True)
    assert res["ok"], res
    assert len(workers) == 3, workers
    st = res["state"]
    assert st["best_version"] == 3, st["best_version"]
    assert "Версия 1" in prompts[-1] and "Версия 2" not in prompts[-1], \
        "вторая финальная попытка собрана поверх первого плохого кандидата"
    doc = (root / st["path"]).read_text(encoding="utf-8")
    assert "status: ready" in doc, doc[:400]
    assert "## Проверка" in doc, doc


@test
def test_two_form_failures_leave_partial_and_no_ready(tmp: Path):
    """Оба финальных текста мимо формы: работа остаётся partial, `ready` не появляется."""
    root = _project(tmp)
    import agent_runner as R
    workers = []

    def fake(cfg, role, messages, **kw):
        if role == "planner":
            return _planner()
        if role == "worker":
            workers.append(1)
            if len(workers) == 1:
                return _base(_written(1))
            return _base(_written(len(workers), check_head=False))
        if role == "critic":
            return _critic(True)
        if role == "qa":
            return _base(CLEAN)
        raise AssertionError(role)

    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=fake, momus=True)
    assert not res["ok"] and res["status"] == "partial", res
    assert "финальный текст не прошёл проверку формы" in res.get("why", ""), res
    assert res.get("path") and (root / res["path"]).is_file(), res
    doc = (root / res["path"]).read_text(encoding="utf-8")
    assert "status: ready" not in doc and "status: draft" in doc, doc[:400]
    st = res["state"]
    assert st["pipeline"].get("form_ok") is not True, st["pipeline"]
    assert st["best_version"] == 1, st["best_version"]
    assert "finalizing" not in st["stages"] and "checked" not in st["stages"], st["stages"]
    report = R.report_make(res)
    assert "Форма не принята" in report and "missing-heading" in report, report


@test
def test_final_writer_residue_and_service_heads_block_ready(tmp: Path):
    """Остатки шаблона и служебные разделы не проходят форму; чистый повтор даёт ready."""
    root = _project(tmp)
    import agent_runner as R
    spec = {"template": "Templates/AC.md"}
    for bad, code in ((_written() + "\n{{Название}}\n", "template-residue"),
                      (_written() + "\n[ПРИМЕР] критерий\n", "template-residue"),
                      (_written() + "\n## Допущения\n\nпринято\n", "service-head"),
                      (_written() + "\n## Замечания критика\n\nнет\n", "service-head"),
                      (_written() + "\nTODO: дописать\n", "todo")):
        problems = R.final_form_problems(str(root), spec, bad)
        assert [p["code"] for p in problems if p["code"] == code], (code, problems)
    workers, prompts = [], []

    def fake(cfg, role, messages, **kw):
        if role == "planner":
            return _planner()
        if role == "worker":
            workers.append(1)
            prompts.append(messages[0]["content"])
            if len(workers) == 2:
                return _base(_written(2) + "\n{{Название}}\n[ПРИМЕР]\n"
                                            "## Допущения\n\nпринято\nTODO: дописать\n")
            return _base(_written(len(workers)))
        if role == "critic":
            return _critic(True)
        if role == "qa":
            return _base(CLEAN)
        raise AssertionError(role)

    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=fake, momus=True)
    assert res["ok"], res
    assert len(workers) == 3, workers
    assert "Нарушения формы:" in prompts[2]
    for code in ("template-residue", "service-head", "todo"):
        assert code in prompts[2], (code, prompts[2][-800:])
    doc = (root / res["state"]["path"]).read_text(encoding="utf-8")
    assert "status: ready" in doc, doc[:400]
    for leftover in ("{{Название}}", "[ПРИМЕР]", "TODO", "## Допущения"):
        assert leftover not in doc, f"остаток «{leftover}» попал в файл"


@test
def test_final_momus_checks_final_text(tmp: Path):
    """Повторный Момус получает финальный текст, а не старый черновик."""
    root = _project(tmp)
    import agent_runner as R
    workers, qa_seen = [], []
    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=_happy(workers, qa_texts=qa_seen), momus=True)
    assert res["ok"], res
    st = res["state"]
    assert len(qa_seen) == 3, qa_seen
    assert len(workers) == 2, workers
    assert "Версия 1" in qa_seen[0], "начальный Момус смотрел не черновик"
    assert "Версия 2" in qa_seen[2] and "Версия 1" not in qa_seen[2], \
        "повторный Момус получил старый черновик"
    manifest = json.loads((root / "Workspaces" / res["sid"] / "run/manifest.json")
                          .read_text(encoding="utf-8"))
    assert any(int(c.get("version") or 0) == 2 and c.get("role") == "momus_repeat"
               for c in manifest.get("checks") or []), manifest.get("checks")
    assert st["momus"].get("clean"), st["momus"]


@test
def test_final_momus_unsupported_blocks_ready(tmp: Path):
    """Повторный Момус нашёл утверждение без опоры — документ остаётся draft."""
    root = _project(tmp)
    import agent_runner as R
    workers, qa_seen = [], []

    def fake(cfg, role, messages, **kw):
        if role == "planner":
            return _planner()
        if role == "worker":
            workers.append(1)
            return _base(_written(len(workers)))
        if role == "critic":
            return _critic(True)
        if role == "qa":
            qa_seen.append(messages[0]["content"])
            if len(qa_seen) == 3:
                return _base("1. НЕТ ОПОРЫ срок\n\nВЕРДИКТ: БЕЗ ОПОРЫ 1")
            return _base(CLEAN)
        raise AssertionError(role)

    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=fake, momus=True)
    assert res["ok"], res
    st = res["state"]
    assert int(st["momus"].get("unsupported") or 0) == 1, st["momus"]
    doc = (root / st["path"]).read_text(encoding="utf-8")
    assert "status: ready" not in doc and "status: draft" in doc, doc[:400]


@test
def test_final_writer_keeps_links_and_numbers(tmp: Path):
    """Ссылки и числа из черновика сохраняются; потерянная ссылка — lost-link и вторая попытка."""
    root = _project(tmp)
    import agent_runner as R
    problems = R.final_form_problems(
        str(root), {"template": "Templates/AC.md"},
        _written(5, link=""), draft="Срок 10 дней, см. [[Заявка-0]].")
    assert [p["code"] for p in problems] == ["lost-link"], problems
    workers, prompts, docs = [], [], []

    def fake(cfg, role, messages, **kw):
        if role == "planner":
            return _planner()
        if role == "worker":
            workers.append(1)
            prompts.append(messages[0]["content"])
            if len(workers) == 2:
                text = _written(2, link="без ссылки")
            else:
                text = _written(len(workers))
            docs.append(text)
            return _base(text)
        if role == "critic":
            return _critic(True)
        if role == "qa":
            return _base(CLEAN)
        raise AssertionError(role)

    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=fake, momus=True)
    assert res["ok"], res
    assert "[[Заявка-0]]" in docs[0] and "10 дней" in docs[0], docs[0][:300]
    assert "lost-link" in prompts[2], prompts[2][-600:]
    doc = (root / res["state"]["path"]).read_text(encoding="utf-8")
    assert "[[Заявка-0]]" in doc and "10 дней" in doc, doc


@test
def test_make_blocks_missing_empty_headingless_template(tmp: Path):
    """Шаблона нет, он пуст, без заголовков или сверх лимита — отказ до вызова модели."""
    root = _project(tmp)
    import agent_runner as R
    called = []

    def fake(cfg, role, messages, **kw):
        called.append(role)
        raise AssertionError(role)

    tpl = root / "Templates/AC.md"
    cases = [
        (None, "шаблона нет на диске"),
        ("   \n\n", "пуст"),
        ("---\ntitle: x\n# комментарий\n---\n<!-- ИНСТРУКЦИЯ: удалить -->\n", "нет заголовков"),
        ("x" * (R.MK.TEMPLATE_LIMIT + 1), "длиннее"),
    ]
    for content, why in cases:
        if content is None:
            tpl.unlink(missing_ok=True)
        else:
            tpl.write_text(content, encoding="utf-8")
        res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                         call=fake, momus=False)
        assert not res["ok"] and why in res["why"], (why, res)
    assert not called, called


@test
def test_make_blocks_changed_template_on_resume(tmp: Path):
    """Продолжение сессии с изменённым шаблоном блокируется до первого вызова модели."""
    root = _project(tmp)
    import agent_runner as R
    called = []

    def planner(cfg, role, messages, **kw):
        called.append(role)
        return _question()

    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", False,
                     call=planner, momus=False)
    assert res["ok"] and res["stage"] == "planning", res
    (root / "Templates/AC.md").write_text(TEMPLATE + "\n## Новый раздел\n", encoding="utf-8")
    called.clear()
    again = R.run_make(_cfg(), str(root), "", "", res["sid"], "10 дней", False,
                       call=planner, momus=False)
    assert not again["ok"] and "изменился с начала сессии" in again["why"], again
    assert not called, called


@test
def test_final_gate_blocks_ready_without_finalizing(tmp: Path):
    """`checked` без `finalizing` не даёт ready: финального писателя не было."""
    root = _project(tmp)
    import agent_runner as R
    stages = {k: "2026-11-23T00:00:00Z" for k in R.MAKE_STAGES if k != "finalizing"}
    st = {"kind": "ac", "idea": "срок заявки", "sid": "s-no-final",
          "spec": {"title": "AC", "out": "Artifacts/ac", "template": "Templates/AC.md"},
          "stages": stages, "issues": [], "pipeline": {"form_ok": True},
          "pipeline_version": 2,
          "momus": {"ok": True, "clean": True, "unsupported": 0},
          "draft": _written(), "path": None}
    doc = (root / R.write_artifact(str(root), st)).read_text(encoding="utf-8")
    assert "status: draft" in doc, doc[:400]
    assert "finalizing: —" in doc and "finished:" not in doc, doc[:600]


@test
def test_write_artifact_rechecks_template_and_form(tmp: Path):
    """`form_ok=True` в сессии не заменяет проверку формы у единственной точки записи."""
    root = _project(tmp)
    import agent_runner as R
    st = {"kind": "ac", "idea": "срок заявки", "sid": "s-write-gate",
          "spec": {"title": "AC", "out": "Artifacts/ac", "template": "Templates/AC.md"},
          "stages": {k: "2026-11-23T00:00:00Z" for k in R.MAKE_STAGES},
          "issues": [], "pipeline": {"form_ok": True}, "pipeline_version": 2,
          "legacy_ready": False,
          "momus": {"ok": True, "clean": True, "unsupported": 0},
          "draft": "# AC\n\nСлишком коротко.", "path": None}
    text = (root / R.write_artifact(str(root), st)).read_text(encoding="utf-8")
    assert "status: draft" in text and "status: ready" not in text, text[:400]
    codes = {p["code"] for p in st.get("form_problems") or []}
    assert {"missing-heading", "empty-body"} <= codes, codes

    missing = {**st, "sid": "s-write-gate-missing", "path": None,
               "spec": {**st["spec"], "template": "Templates/Missing.md"},
               "pipeline": {"form_ok": True}, "draft": _written()}
    missing.pop("form_problems", None)
    text2 = (root / R.write_artifact(str(root), missing)).read_text(encoding="utf-8")
    assert "status: draft" in text2, text2[:400]
    assert [p["code"] for p in missing.get("form_problems") or []] == ["template-problem"], \
        missing.get("form_problems")

    empty = root / "Templates/Empty.md"
    empty.write_text("   \n", encoding="utf-8")
    empty_state = {**st, "sid": "s-write-gate-empty", "path": None,
                   "spec": {**st["spec"], "template": "Templates/Empty.md"},
                   "pipeline": {"form_ok": True}, "draft": _written()}
    empty_state.pop("form_problems", None)
    text3 = (root / R.write_artifact(str(root), empty_state)).read_text(encoding="utf-8")
    assert "status: draft" in text3, text3[:400]
    assert "пуст" in empty_state["form_problems"][0]["detail"], empty_state["form_problems"]

    original = (root / "Templates/AC.md").read_text(encoding="utf-8")
    sha = R.MK.template_sha(str(root), "Templates/AC.md")
    changed = {**st, "sid": "s-write-gate-changed", "path": None,
               "spec": {**st["spec"], "template_sha256": sha},
               "pipeline": {"form_ok": True}, "draft": _written()}
    changed.pop("form_problems", None)
    (root / "Templates/AC.md").write_text(original + "\n## Новый раздел\n", encoding="utf-8")
    text4 = (root / R.write_artifact(str(root), changed)).read_text(encoding="utf-8")
    assert "status: draft" in text4, text4[:400]
    assert "изменился" in changed["form_problems"][0]["detail"], changed["form_problems"]
    (root / "Templates/AC.md").write_text(original, encoding="utf-8")


@test
def test_template_changed_between_start_and_final_writer_blocks_ready(tmp: Path):
    """Удалённый, опустевший или заменённый шаблон не пропускает финальный текст."""
    import agent_runner as R
    for mode in ("delete", "empty", "changed"):
        case = tmp / mode
        case.mkdir()
        root = _project(case)
        tpl = root / "Templates/AC.md"
        workers = []

        def fake(cfg, role, messages, **kw):
            if role == "planner":
                return _planner()
            if role == "worker":
                workers.append(1)
                if len(workers) == 2:                 # финальный писатель
                    if mode == "delete":
                        tpl.unlink()
                    elif mode == "empty":
                        tpl.write_text("", encoding="utf-8")
                    else:
                        tpl.write_text(TEMPLATE + "\n## Новый раздел\n", encoding="utf-8")
                return _base(_written(len(workers)))
            if role == "critic":
                return _critic(True)
            if role == "qa":
                return _base(CLEAN)
            raise AssertionError(role)

        res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                         call=fake, momus=False)
        assert not res["ok"] and res["status"] == "partial", (mode, res)
        assert "template-problem" in res.get("why", ""), (mode, res.get("why"))
        assert len(workers) == 3, (mode, workers)
        doc = (root / res["path"]).read_text(encoding="utf-8")
        assert "status: draft" in doc and "status: ready" not in doc, (mode, doc[:400])
        assert (res["state"].get("pipeline") or {}).get("form_ok") is not True, mode


@test
def test_legacy_checked_without_finalizing_is_ready(tmp: Path):
    """Старый артефакт с `checked` без `finalizing` читается как готовый и не числится незавершённым."""
    root = _project(tmp)
    import agent_runner as R
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as AC
    old = root / "Artifacts/ac/Старый.md"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(
        "---\ntitle: \"Старый\"\ntype: ac\nstatus: ready\ncreated: 2026-01-01\n"
        "updated: 2026-01-01\nbuilt: machine\nsession: s-old\npipeline:\n"
        "  enriched: 2026-01-01T00:00:00Z\n  planned: 2026-01-01T00:00:00Z\n"
        "  drafted: 2026-01-01T00:00:00Z\n  reviewed: 2026-01-01T00:00:00Z\n"
        "  checked: 2026-01-01T00:00:00Z\n---\n\n# AC\n\nТекст.\n",
        encoding="utf-8")
    assert AC.unfinished(str(root))["count"] == 0, AC.unfinished(str(root))
    # Старый `checked` без `finalizing`, но со `status: draft`, готовым не считается:
    # это брошенный черновик, и панель должна его показать.
    abandoned = root / "Artifacts/ac/Брошенный.md"
    abandoned.write_text(
        "---\ntitle: \"Брошенный\"\ntype: ac\nstatus: draft\ncreated: 2026-01-01\n"
        "updated: 2026-01-01\nbuilt: machine\nsession: s-abandoned\npipeline:\n"
        "  enriched: 2026-01-01T00:00:00Z\n  planned: 2026-01-01T00:00:00Z\n"
        "  drafted: 2026-01-01T00:00:00Z\n  reviewed: 2026-01-01T00:00:00Z\n"
        "  checked: 2026-01-01T00:00:00Z\n---\n\n# AC\n\nТекст.\n",
        encoding="utf-8")
    left = AC.unfinished(str(root))
    assert left["count"] == 1 and left["items"][0]["stopped"] == "status", left
    # Новый артефакт с незаполненной финальной сборкой и новый с проваленной формой
    # числятся незавершёнными: это работа, которую видно.
    fresh = root / "Artifacts/ac/Новый.md"
    fresh.write_text(
        "---\ntitle: \"Новый\"\ntype: ac\nstatus: draft\ncreated: 2026-01-01\n"
        "updated: 2026-01-01\nbuilt: machine\nsession: s-new\npipeline:\n"
        "  enriched: 2026-01-01T00:00:00Z\n  planned: 2026-01-01T00:00:00Z\n"
        "  drafted: 2026-01-01T00:00:00Z\n  reviewed: 2026-01-01T00:00:00Z\n"
        "  finalizing: \u2014\n  checked: \u2014\n  form_ok: false\n---\n\n# AC\n",
        encoding="utf-8")
    noform = root / "Artifacts/ac/Без формы.md"
    noform.write_text(
        "---\ntitle: \"Без формы\"\ntype: ac\nstatus: draft\ncreated: 2026-01-01\n"
        "updated: 2026-01-01\nbuilt: machine\nsession: s-noform\npipeline:\n"
        "  enriched: 2026-01-01T00:00:00Z\n  planned: 2026-01-01T00:00:00Z\n"
        "  drafted: 2026-01-01T00:00:00Z\n  reviewed: 2026-01-01T00:00:00Z\n"
        "  finalizing: 2026-01-01T00:00:00Z\n  checked: 2026-01-01T00:00:00Z\n"
        "  form_ok: false\n---\n\n# AC\n",
        encoding="utf-8")
    seen = AC.unfinished(str(root))
    stopped = {Path(item["path"]).name: item["stopped"] for item in seen["items"]}
    assert stopped.get("Новый.md") == "finalizing", seen
    assert stopped.get("Без формы.md") == "form", seen
    assert stopped.get("Брошенный.md") == "status", seen
    assert "Старый.md" not in stopped, seen
    stages = {k: "2026-11-23T00:00:00Z" for k in R.MAKE_STAGES if k != "finalizing"}
    st = {"kind": "ac", "idea": "срок заявки", "sid": "s-legacy",
          "spec": {"title": "AC", "out": "Artifacts/ac", "template": "Templates/AC.md"},
          "stages": stages, "issues": [], "pipeline": {"form_ok": True},
          "momus": {"ok": True, "clean": True, "unsupported": 0},
          "legacy_ready": True, "started": "2026-11-23T00:00:00Z",
          "draft": _written(), "path": None}
    doc = (root / R.write_artifact(str(root), st)).read_text(encoding="utf-8")
    assert "status: ready" in doc and "finished:" in doc, doc[:500]
    names = {Path(item["path"]).name for item in AC.unfinished(str(root))["items"]}
    assert "срок-заявки.md" not in names, names
    synthetic = dict(st, sid="s-legacy-synthetic", path=None,
                     stages={**stages, "finalizing": stages["checked"]})
    doc2 = (root / R.write_artifact(str(root), synthetic)).read_text(encoding="utf-8")
    assert "status: ready" in doc2 and "finished:" in doc2, doc2[:500]


@test
def test_legacy_rejected_or_draft_does_not_become_ready(tmp: Path):
    """Старый `checked` не поднимает `critic_rejected` и файл со `status: draft`."""
    root = _project(tmp)
    import agent_runner as R
    stages = {k: "2026-11-23T00:00:00Z" for k in R.MAKE_STAGES if k != "finalizing"}
    base = {"kind": "ac", "idea": "старый документ",
            "spec": {"title": "AC", "out": "Artifacts/ac",
                                   "template": "Templates/AC.md"},
            "stages": stages, "issues": [],
            "momus": {"ok": True, "clean": True, "unsupported": 0},
            "draft": _written(), "path": None}
    rejected = {**base, "sid": "s-old-rejected", "issues": ["критик не принял"],
                "final_check": "critic_rejected"}
    text = (root / R.write_artifact(str(root), rejected)).read_text(encoding="utf-8")
    assert "status: draft" in text and "status: ready" not in text, text[:400]
    refused = {**rejected, "sid": "s-old-rejected-finalizing", "legacy_ready": True,
               "pipeline_version": 2,
               "stages": {**stages, "finalizing": stages["checked"]}}
    refused.pop("form_problems", None)
    text1 = (root / R.write_artifact(str(root), refused)).read_text(encoding="utf-8")
    assert "status: draft" in text1 and "status: ready" not in text1, text1[:400]

    old = root / "Artifacts/ac/Старый-черновик.md"
    old.write_text(
        "---\ntitle: \"Старый черновик\"\ntype: ac\nstatus: draft\ncreated: 2026-01-01\n"
        "updated: 2026-01-01\nbuilt: machine\nsession: s-old-draft\npipeline:\n"
        "  enriched: 2026-01-01T00:00:00Z\n  planned: 2026-01-01T00:00:00Z\n"
        "  drafted: 2026-01-01T00:00:00Z\n  reviewed: 2026-01-01T00:00:00Z\n"
        "  checked: 2026-01-01T00:00:00Z\n---\n\n# AC\n\nТекст.\n",
        encoding="utf-8")
    draft = {**base, "sid": "s-old-draft", "path": str(old.relative_to(root))}
    text2 = (root / R.write_artifact(str(root), draft)).read_text(encoding="utf-8")
    assert "status: draft" in text2 and "status: ready" not in text2, text2[:400]


@test
def test_final_form_problems_reports_each_gate(tmp: Path):
    """Прямые коды формы: граница чистовика, YAML, порядок, пустое тело и шаблон."""
    root = _project(tmp)
    import agent_runner as R
    from aurora_common import MADE_MARK
    spec = {"template": "Templates/AC.md"}
    valid = _written()
    cases = {
        "made-mark": MADE_MARK + "\n" + valid,
        "frontmatter": "---\ntitle: x\n---\n" + valid,
        "heading-order": ("# AC\n\n## Проверка\n\nСмотрим отчёт и журнал операций, "
                          "затем закрываем заявку.\n\n## Критерии приёмки\n\n"
                          "Срок заявки десять дней, проверяется отчётом."),
        "empty-body": "# AC\n\n## Критерии приёмки\n\n## Проверка\n",
    }
    for code, text in cases.items():
        got = [p["code"] for p in R.final_form_problems(str(root), spec, text)]
        assert code in got, (code, got)
    assert R.MK.template_sha(str(root), "Templates/Missing.md") == ""
    assert [p["code"] for p in R.final_form_problems(
        str(root), {"template": "Templates/Missing.md"}, valid)] == ["template-problem"]


@test
def test_finalizer_runs_without_momus(tmp: Path):
    """`momus=False`: финальный писатель и форма выполнены, `checked` и ready не появляются."""
    root = _project(tmp)
    import agent_runner as R
    workers, roles = [], []
    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=_happy(workers, roles), momus=False)
    assert res["ok"], res
    assert roles == ["planner", "worker", "critic", "worker"], roles
    st = res["state"]
    assert len(workers) == 2, workers
    assert st["stages"].get("finalizing"), st["stages"]
    assert not st["stages"].get("checked"), st["stages"]
    assert st["pipeline"].get("form_ok") is True, st["pipeline"]
    assert st["momus"].get("skipped"), st["momus"]
    doc = (root / st["path"]).read_text(encoding="utf-8")
    assert "status: draft" in doc and "status: ready" not in doc, doc[:400]


@test
def test_report_and_header_show_finalizing_and_form_ok(tmp: Path):
    """В шапке и отчёте видны финальная сборка и итог формы."""
    root = _project(tmp)
    import agent_runner as R
    res = R.run_make(_cfg(), str(root), "ac", "срок заявки", "", "", True,
                     call=_happy([]), momus=True)
    assert res["ok"], res
    st = res["state"]
    doc = (root / st["path"]).read_text(encoding="utf-8")
    head = doc.split("---", 2)[1]
    assert re.search(r"^  finalizing: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", head, re.M), head
    assert "  form_ok: true" in head, head
    text = R.report_make(res)
    assert "| финальный писатель |" in text, text
    assert "Финальная сборка:" in text, text
