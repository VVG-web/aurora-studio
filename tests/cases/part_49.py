"""Проверки движка Aurora, часть 49. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 3 «Обязательность»: пакет CT-11 — правила шаблонов в запросах модели (перечень разделов и
выдержка из инструкции выбора типа приходят из реестра проекта, а не из текста запроса; форма тезиса для типа карточки —
правило 6) и пакет CT-12 — «касание = приведение» и строгость линтера для карточек со штампом `template:`.
Модель и сеть не используются: ответы подставные; проекты — только во временных папках.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, card, make_project, run, test  # noqa: F401

from cases import part_42 as P42  # noqa: E402 — фикстурные шаблоны (свой тип проекта)

KB = "AuroraKnowledgeDB"
SECTIONS = [("Concepts", "concept"), ("Glossary", "glossary"), ("Processes", "process"), ("Systems", "system"),
            ("Roles", "role"), ("Statuses", "status-model"), ("Requirements", "requirement"), ("Reference", "reference")]
# Длины запросов до пакета CT-11 при пустых подстановках (замер на коммите перед пакетом): рост считается от них.
OLD_CHARS = {"build": 4034, "meeting": 1066, "plan": 1536, "distill": 1685, "redistill": 556}
PAGE = ("## Определение\n\n" + "Заявка на поставку — документ, которым заказчик поручает поставщику передать товар. " * 3
        + "\n\n## Правила\n\n" + "- срок подтверждения три рабочих дня, отмена возможна до подтверждения\n" * 4)


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _disable(root: Path, *types: str) -> None:
    """Выключить типы в конфиге проекта (блок `cards:`)."""
    cfg = root / "aurora.config.yaml"
    block = "cards:\n" + "".join(f'  {t}:\n    enabled: "false"\n' for t in types)
    cfg.write_text(cfg.read_text(encoding="utf-8").rstrip("\n") + "\n" + block, encoding="utf-8")


class _RunDir:
    """Журнал сбоев прогона — во временную папку, прежнее значение возвращается."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.mkdir(parents=True, exist_ok=True)
        self.was = os.environ.get("AURORA_RUN_DIR")
        os.environ["AURORA_RUN_DIR"] = str(self.path)
        return self.path

    def __exit__(self, *exc):
        if self.was is None:
            os.environ.pop("AURORA_RUN_DIR", None)
        else:
            os.environ["AURORA_RUN_DIR"] = self.was


def _answer(text: str) -> dict:
    return {"ok": True, "text": text, "backend": 1, "model": "m", "tps": 9, "log": []}


def _cfg():
    return _mod("agent_core").parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})


# ------------------------------------------------------------------ CT-11: перечень разделов и инструкция

@test
def test_prompt_build_sections_from_registry(tmp: Path):
    """AC-CT-11.2: перечень разделов для поля `to` приходит из реестра проекта: выключенный тип из него уходит, свой тип
    проекта в него входит; в самих запросах (`PROMPT_BUILD`, `PROMPT_MEETING`, `PROMPT_PLAN_SOURCE`) литерала перечня нет."""
    R = _mod("agent_runner")
    root = make_project(tmp)
    r = str(root)
    assert R.build_sections(r) == SECTIONS, R.build_sections(r)
    assert R.types_for_prompt(r) == ", ".join(f'"{s}" ({t})' for s, t in SECTIONS)
    for name in ("PROMPT_BUILD", "PROMPT_MEETING", "PROMPT_PLAN_SOURCE"):
        prompt = getattr(R, name)
        assert "{section_list}" in prompt and "{guide}" in prompt, name
        for sec, _tid in SECTIONS:
            assert sec not in prompt, f"{name}: раздел {sec} вписан в текст запроса"
    _disable(root, "role")
    off = R.types_for_prompt(r)
    assert "Roles" not in off and '"Glossary" (glossary)' in off, off
    guide = R.guide_for_prompt(r)
    assert "`akdb_role`" not in guide and "`akdb_glossary`" in guide, "строка выключенного типа осталась в таблице инструкции"
    assert "cited-document" not in guide and "`decision`" not in guide, "в таблице типы, которых разбор не предлагает"
    P42._put(root / "Templates" / "akdb_policy_v1.0.md", P42._template_text(P42.POLICY))
    assert R.types_for_prompt(r).endswith('"Policies" (policy)'), R.types_for_prompt(r)


@test
def test_build_prompt_carries_the_sections_and_the_guide(tmp: Path):
    """Запрос разбора, ушедший модели, содержит перечень разделов реестра и шаги инструкции; формат ответа не менялся."""
    R = _mod("agent_runner")
    root = make_project(tmp)
    (root / "Raw/project").mkdir(parents=True, exist_ok=True)
    (root / "Raw/project/Заявка.md").write_text(PAGE, encoding="utf-8")
    _disable(root, "role")
    seen = []

    def model(cfg_, role, messages, **kw):
        seen.append(messages[0]["content"])
        return _answer('{"empty": "тест"}')

    with _RunDir(tmp / "run"):
        R.solve_source(_cfg(), str(root), "Raw", "Raw/project/Заявка.md", False, False, model, 0)
    prompt = seen[0]
    assert R.types_for_prompt(str(root)) in prompt and "Roles" not in prompt, "перечень разделов не из реестра"
    assert "## Шаг 2. Выберите тип" in prompt and "## Шаг 3. Границы между соседними типами" in prompt
    assert '{"cards": [{"title": "<имя>", "sections": "1,3-5", "to": "<раздел>",' in prompt, "формат ответа изменился"
    assert '{"empty": "<почему знания не вышло, одна фраза>"}' in prompt


@test
def test_guide_for_prompt_keeps_the_steps_that_fit(tmp: Path):
    """Выдержка берёт шаги по важности (2, 3, 0, 6), пока влезают в бюджет; короткая инструкция проекта берётся шагами
    0, 2, 3, 6 в исходном порядке; инструкция без заголовков шагов — целиком, но не длиннее 6000 знаков."""
    R = _mod("agent_runner")
    root = make_project(tmp)
    r = str(root)
    kit = R.guide_for_prompt(r)
    steps = re.findall(r"(?m)^## Шаг (\d+)\.", kit)
    assert steps == ["2", "3"] and len(kit) <= R.GUIDE_PROMPT_CHARS, (steps, len(kit))
    prompts = root / "Prompts"
    prompts.mkdir(exist_ok=True)
    short = "\n\n".join(f"## Шаг {n}. Заголовок {n}\n\nТекст шага {n}." for n in range(8))
    (prompts / "KB_classify.md").write_text(short + "\n", encoding="utf-8")
    got = R.guide_for_prompt(r)
    assert re.findall(r"(?m)^## Шаг (\d+)\.", got) == ["0", "2", "3", "6"], got
    (prompts / "KB_classify.md").write_text("Выбирайте тип по смыслу.\n" + "строка " * 2000, encoding="utf-8")
    flat = R.guide_for_prompt(r)
    assert flat.startswith("Выбирайте тип по смыслу.") and 5990 <= len(flat) <= R.GUIDE_HEADLESS_CHARS, len(flat)
    (prompts / "KB_classify.md").write_text("   \n", encoding="utf-8")
    assert "## Шаг 2." in R.guide_for_prompt(r), "пустой файл проекта должен уступить инструкции кита"


@test
def test_prompt_sections_without_templates_are_the_legacy_eight(tmp: Path):
    """Шаблонов нет ни в проекте, ни в ките (реестр пуст) — разделы разбора прежние восемь, как до реестра."""
    R = _mod("agent_runner")
    C = _mod("card_types")
    was = R.CT.registry_for
    try:
        R.CT.registry_for = lambda *a, **k: C.Registry()
        assert set(R.build_sections(".")) == set(SECTIONS) and len(R.build_sections(".")) == 8, R.build_sections(".")
    finally:
        R.CT.registry_for = was


@test
def test_prompt_length_budget(tmp: Path):
    """AC-CT-11.4: рост запроса разбора — не больше 4000 знаков, запроса тезиса — не больше 1000; правило 6 любого типа
    кита с подписью — не больше 960 знаков. Цифры замера — в отчёте пакета."""
    R = _mod("agent_runner")
    root = make_project(tmp)
    r = str(root)
    sl, gb = R.types_for_prompt(r), R.guide_block(r)
    build = len(R.PROMPT_BUILD.format(source="", sections="", section_list=sl, guide=gb))
    meeting = len(R.PROMPT_MEETING.format(part=1, parts=1, source="", when="", candidates="", turns="", section_list=sl, guide=gb))
    plan = len(R.PROMPT_PLAN_SOURCE.format(source="", size=0, outline="", section_list=sl, guide=gb))
    assert build - OLD_CHARS["build"] <= 4000, build - OLD_CHARS["build"]
    assert meeting - OLD_CHARS["meeting"] <= 4000 and plan - OLD_CHARS["plan"] <= 4000, (meeting, plan)
    longest = 0
    for tid in _mod("card_types").registry(r).types:
        line = R.type_rules_line(r, tid)
        longest = max(longest, len(line))
        assert len(line) <= 960, (tid, len(line))
    distill = len(R.PROMPT_DISTILL.format(title="", body="", type_rules=R.type_rules_line(r, "process")))
    redistill = len(R.PROMPT_REDISTILL.format(title="", was="", body="", type_rules=R.type_rules_line(r, "process")))
    assert distill - OLD_CHARS["distill"] <= 1000 and redistill - OLD_CHARS["redistill"] <= 1000, (distill, redistill)
    assert longest > 0


@test
def test_unknown_or_disabled_section_is_not_created(tmp: Path):
    """AC-CT-11.1: подставная модель называет раздел, которого нет в реестре, или раздел выключенного типа, — карточка не
    создаётся, вторая попытка идёт с замечанием, источник остаётся в плане (не отмечен разобранным); «похожий» раздел
    за модель не подбирается."""
    R = _mod("agent_runner")
    for bad, disable in (("Policies", ()), ("Roles", ("role",))):
        sub = tmp / bad
        sub.mkdir()
        root = make_project(sub, git=True)
        if disable:
            _disable(root, *disable)
        (root / "Raw/project").mkdir(parents=True, exist_ok=True)
        (root / "Raw/project/Заявка.md").write_text(PAGE, encoding="utf-8")
        seen = []

        def model(cfg_, role, messages, **kw):
            seen.append(messages[0]["content"])
            return _answer(json.dumps({"cards": [{"title": "Заявка на поставку", "sections": "1-2", "to": bad}]}, ensure_ascii=False))

        with _RunDir(sub / "run"):
            step = R.solve_source(_cfg(), str(root), "Raw", "Raw/project/Заявка.md", True, False, model, 0)
        assert len(seen) == 2 and f"раздела «{bad}» нет среди допустимых" in seen[1], (bad, len(seen), seen[-1][-400:])
        assert step["status"] == "отклонено проверкой" and step.get("content_fail"), step
        assert not list((root / KB).rglob("Заявка-на-поставку*.md")), "карточка создана в разделе вне реестра"
        man = root / KB / "meta" / "manifest.json"
        done = json.loads(man.read_text(encoding="utf-8")).get("sources", {}).get("Raw/project/Заявка.md", {}) if man.exists() else {}
        assert not done.get("done"), "источник отмечен разобранным"


@test
def test_unknown_section_from_the_source_planner_is_refused(tmp: Path):
    """Источник без заголовков: планировщик называет `to_section` вне реестра — карточка не создаётся, шаг — сбой по
    содержанию, источник остаётся в плане."""
    R = _mod("agent_runner")
    root = make_project(tmp, git=True)
    (root / "Raw/project").mkdir(parents=True, exist_ok=True)
    paras = "\n\n".join(f"Абзац {i} про процесс согласования заявки и сроки. " * 3 for i in range(1, 7))
    (root / "Raw/project/Лента.md").write_text(paras, encoding="utf-8")

    def model(cfg_, role, messages, **kw):
        assert "{section_list}" not in messages[0]["content"] and R.types_for_prompt(str(root)) in messages[0]["content"]
        return _answer(json.dumps({"parts": [{"title": "Согласование", "from": 1, "to": 6, "to_section": "Nowhere"}]}))

    step = {"alias": "Лента.md", "status": "", "note": "", "backends": [], "degraded": False}
    with _RunDir(tmp / "run"):
        made = R.plan_source(_cfg(), str(root), "Raw/project/Лента.md", paras, step, True, model, 0.0)
    assert made == 0 and step["status"] == "сбой" and step.get("content_fail"), (made, step)
    assert not list((root / KB).rglob("Согласование*.md"))


@test
def test_distill_prompt_carries_the_type_rules(tmp: Path):
    """Правило 6 запроса тезиса — форма тезиса для типа карточки из шаблона; правила 1–5 не тронуты; карточке типа вне
    реестра правило не добавляется, остальной запрос тот же."""
    R = _mod("agent_runner")
    root = make_project(tmp)
    head = ('---\ntitle: "{t}"\ntype: {ty}\nkind: knowledge\nstatus: draft\nsources:\n  - "Sources/Confluence/Док.md"\n'
            'created: 2026-10-01\nupdated: 2026-10-01\nbuilt: machine\n---\n\n')
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Док.md").write_text("Текст.\n", encoding="utf-8")
    seen = {}
    for name, ty in (("Заявка", "concept"), ("Свой тип", "no-such-type")):
        sec = "Concepts"
        path = root / KB / sec / f"{name}.md"
        path.write_text(head.format(t=name, ty=ty) + "## Источник (перенесено дословно)\n\n" + PAGE, encoding="utf-8")

        def model(cfg_, role, messages, name=name, **kw):
            seen[name] = messages[0]["content"]
            return _answer("Заявка на поставку — документ, которым заказчик поручает поставщику передать товар.")

        with _RunDir(tmp / f"run-{ty}"):
            R.distill_card(_cfg(), str(path), call=model, momus=False)
    rule = "6. Форма тезиса для типа этой карточки:"
    assert rule in seen["Заявка"] and "Тип: concept (Понятие)." in seen["Заявка"], seen["Заявка"][-1400:]
    assert rule not in seen["Свой тип"], "правило 6 добавлено карточке неизвестного типа"
    assert "5. Ссылок `[[…]]` не ставь" in seen["Заявка"] and "1. Только то, что есть в тексте выше." in seen["Заявка"]


# ------------------------------------------------------------------ CT-12: «касание = приведение», строгость линтера

def _C():
    return _mod("card_types")


def _write_header_of(text: str) -> str:
    return text.split("\n---\n", 1)[0]


@test
def test_a_conformant_card_gets_the_stamp_on_touch(tmp: Path):
    """AC-CT-12.1 (вход записи): правка (`body`, `header`, `repair`) карточки без `template:`, которая после неё проходит
    шаблон в строгом режиме, получает штамп сразу после `type:`; карточка с нарушением штампа не получает; уже стоящий
    штамп не снимается; создание штамп не ставит (там он обязателен); тип вне реестра штампа не получает."""
    C = _C()
    root = P42._project(tmp)
    reg = P42._reg(root)
    cases = tmp / "cases"
    cases.mkdir()

    def card(name: str, text: str) -> str:
        path = root / "AuroraKnowledgeDB" / "Concepts" / f"{name}.md"
        P42._put(path, text)
        return str(path)

    good = card("Заявка", P42.mk(template=None))
    assert C.write_card_text(good, P42.mk(template=None) + "\nДополнение к тексту источника.\n", op="body", reg=reg)
    got = (root / "AuroraKnowledgeDB/Concepts/Заявка.md").read_text(encoding="utf-8")
    head = _write_header_of(got).split("\n")
    assert head[head.index("type: concept") + 1] == "template: akdb_concept_v1.0.md", head
    assert not [v for v in C.validate_card(got, path="AuroraKnowledgeDB/Concepts/Заявка.md", reg=reg, level="strict")
                if v.severity == "error"], "штампованная карточка не проходит шаблон"

    for op in ("header", "repair"):
        path = card(f"Заявка-{op}", P42.mk(template=None))
        assert C.write_card_text(path, P42.mk(template=None, updated="2026-10-02"), op=op, reg=reg)
        assert "template: akdb_concept_v1.0.md" in Path(path).read_text(encoding="utf-8"), op

    bad = card("Плохая", P42.mk(template=None, status="imported"))
    assert C.write_card_text(bad, P42.mk(template=None, status="imported") + "\nДополнение.\n", op="body", reg=reg)
    assert "template:" not in Path(bad).read_text(encoding="utf-8"), "карточка с нарушением получила штамп"

    odd = card("Чужой", P42.mk(template=None, type="nosuch"))
    C.write_card_text(odd, P42.mk(template=None, type="nosuch") + "\nДополнение.\n", op="body", reg=reg)
    assert "template:" not in Path(odd).read_text(encoding="utf-8"), "карточка типа вне реестра получила штамп"

    kept = card("Со штампом", P42.mk())
    C.write_card_text(kept, P42.mk() + "\nДополнение.\n", op="body", reg=reg)
    assert Path(kept).read_text(encoding="utf-8").count("template: akdb_concept_v1.0.md") == 1
    try:
        C.write_card_text(kept, P42.mk(template=None) + "\nЕщё дополнение.\n", op="body", reg=reg)
        raise AssertionError("штамп снят с карточки")
    except C.TemplateViolation:
        pass
    # повтор того же текста ничего не пишет и штамп второй раз не ставит
    again = Path(good).read_text(encoding="utf-8")
    assert C.write_card_text(good, again, op="body", reg=reg) is False and Path(good).read_text(encoding="utf-8") == again


@test
def test_distill_stamps_a_conformant_card_and_leaves_the_other(tmp: Path):
    """AC-CT-12.1: `agent:distill` на фикстуре штампует карточку, которая после тезиса проходит валидатор, и не штампует ту,
    что не проходит (в сводке `kb:cards` она остаётся «до-шаблонной»)."""
    R = _mod("agent_runner")
    C = _C()
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Док.md").write_text("Текст.\n", encoding="utf-8")
    head = ('---\ntitle: "{t}"\naliases: []\ntype: concept\nkind: knowledge\nstatus: {s}\nsources:\n  - "Sources/Confluence/Док.md"\n'
            'source_synced: 2026-10-01\ncreated: 2026-10-01\nupdated: 2026-10-01\nbuilt: machine\n---\n\n')
    paths = {}
    for name, status in (("Исправная", "draft"), ("С нарушением", "imported")):
        paths[name] = root / KB / "Concepts" / f"{name}.md"
        paths[name].write_text(head.format(t=name, s=status) + "## Источник (перенесено дословно)\n\n" + PAGE, encoding="utf-8")

    def model(cfg_, role, messages, **kw):
        return _answer("Заявка на поставку — документ, которым заказчик поручает поставщику передать товар.")

    with _RunDir(tmp / "run"):
        R.run_distill(_cfg(), str(root), apply=True, limit=0, momus=False, call=model)
    good = paths["Исправная"].read_text(encoding="utf-8")
    bad = paths["С нарушением"].read_text(encoding="utf-8")
    assert "template: akdb_concept_v1.0.md" in good and "distilled: " in good, good[:500]
    assert "template:" not in bad and "distilled: " in bad, bad[:500]
    reg = C.registry(str(root), kit_root=str(KIT))
    stats = C.stats_of(C.check_cards(str(root), reg, level="advisory"), reg)
    assert stats["stamped"] == 1 and stats["legacy"] == 1, stats


@test
def test_lint_template_violation_of_a_stamped_card_is_an_error(tmp: Path):
    """AC-CT-12.2/12.3 и хук pre-commit: нарушение шаблона у карточки со штампом — ошибка `kb:lint` (код 1, группа в списке,
    число ошибок растёт, `--only` по этому файлу тоже видит ошибку); то же нарушение без штампа — сведение. В итоговой строке
    после «ошибок M» стоит «до-шаблонных: K», повторного «ошибок <число>» нет; на базе без штампов итоговая строка прежняя."""
    root = P42._lint_project(tmp / "p")
    kb = root / KB / "Concepts"
    first = P42._lint(root, "--full")
    base_errors = int(re.search(r"ошибок (\d+)", P42._summary_line(first.stdout)).group(1))
    assert "нарушает шаблон" not in first.stdout
    P42._put(kb / "Нарушает.md", P42.mk(status="imported", title='"Нарушает"'))
    cp = P42._lint(root, "--full")
    line = P42._summary_line(cp.stdout)
    errors = int(re.search(r"ошибок (\d+)", line).group(1))
    assert cp.returncode == 1 and errors > base_errors, (cp.returncode, line)
    assert re.fullmatch(r"kb_lint: карточек \d+, ошибок \d+ · до-шаблонных: \d+", line), line
    assert len(re.findall(r"ошибок\s+\d", line)) == 1 and len(re.findall(r"карточек\s+\d", line)) == 1, line
    assert "нарушения шаблона у карточек со штампом" in cp.stdout and "карточка с template: нарушает шаблон — V-06" in cp.stdout
    only = P42._lint(root, "--only", f"{KB}/Concepts/Нарушает.md", "--summary")
    only_errors = int(re.search(r"ошибок (\d+)", only.stdout).group(1))
    assert only.returncode == 1 and only_errors >= 2, only.stdout        # битая ссылка и нарушение шаблона
    # строку читает настоящий хук pre-commit: число — из итоговой строки, а не из раздела о шаблонах
    rc, out, base, _inst = P42._hook_commit(tmp / "hook", cp.stdout, baseline=errors + 9, base_cards=100)
    assert rc == 0 and f"база чище ({errors + 9} → {errors})" in out and base == str(errors), (rc, out, base)
    # без штампа то же нарушение — сведение: число ошибок прежнее, штампованных в базе больше нет у нарушителя
    P42._put(kb / "Нарушает.md", P42.mk(status="imported", title='"Нарушает"', template=None))
    cp2 = P42._lint(root, "--full")
    assert "нарушает шаблон" not in cp2.stdout
    assert int(re.search(r"ошибок (\d+)", P42._summary_line(cp2.stdout)).group(1)) == errors - 1, "ошибка шаблона осталась без штампа"
    only2 = P42._lint(root, "--only", f"{KB}/Concepts/Нарушает.md", "--summary")
    assert int(re.search(r"ошибок (\d+)", only2.stdout).group(1)) == only_errors - 1, only2.stdout


@test
def test_lint_summary_of_a_base_without_stamps_is_unchanged(tmp: Path):
    """AC-CT-12.3: на базе, где нет ни одной карточки с `template:`, итоговая строка линтера и его код возврата такие же,
    как до пакета: «карточек N, ошибок M» без счётчика, нарушения шаблона ошибками не становятся."""
    root = P42._lint_project(tmp / "p")
    kb = root / KB / "Concepts"
    # единственная штампованная карточка теряет штамп: в базе «до-шаблонные» все
    P42._put(kb / "Заявка-на-поставку.md", P42._example(P42.CATALOGUE[0]).replace("template: akdb_concept_v1.0.md\n", ""))
    cp = P42._lint(root, "--full")
    line = P42._summary_line(cp.stdout)
    assert re.fullmatch(r"kb_lint: карточек \d+, ошибок \d+", line), line
    assert "нарушает шаблон" not in cp.stdout and "до-шаблонных" not in cp.stdout
    # те же ошибки, что считал линтер: две битые ссылки у двух карточек
    assert int(re.search(r"ошибок (\d+)", line).group(1)) == 2 and cp.returncode == 1


@test
def test_a_misplaced_card_is_not_stamped_and_stamped_errors_are_stable(tmp: Path):
    """Штамп «проходит валидатор» не ставится карточке не в своём разделе; ошибка штампованной карточки не несёт номера строки
    (остаток починки сравнивает строки дословно); выключенный тип — настройка проекта, а не ошибка карточки."""
    C = _C()
    root = P42._project(tmp)
    reg = P42._reg(root)
    # раздел не тот: тип concept в Processes
    folder = root / "AuroraKnowledgeDB" / "Processes"
    folder.mkdir(parents=True)
    text = P42.mk(template=None)
    assert C.stamp_if_conformant(text, "AuroraKnowledgeDB/Concepts/Х.md", reg) != text, "в своём разделе карточка штампуется"
    assert C.stamp_if_conformant(text, "AuroraKnowledgeDB/Processes/Х.md", reg) == text, "карточка не в своём разделе получила штамп"
    # номер строки в ошибке нет: сдвиг шапки не делает ошибку «новой»
    bad = P42.mk(status="imported")
    shifted = bad.replace('title: "Заявка на поставку"\n', 'title: "Заявка на поставку"\nsummary: "Строка выше сдвигает остальные."\n', 1)
    a = [e for e in C.stamped_card_errors("AuroraKnowledgeDB/Concepts/Х.md", bad, reg) if "V-06" in e]
    b = [e for e in C.stamped_card_errors("AuroraKnowledgeDB/Concepts/Х.md", shifted, reg) if "V-06" in e]
    assert a and a == b and not re.search(r"Х\.md:\d", a[0]), (a, b)
    # тип выключен: карточка не виновата
    (root / "aurora.config.yaml").write_text("project:\n  name: Test\n", encoding="utf-8")
    _disable(root, "concept")
    off = C.registry_for(str(root))
    assert not off.types["concept"].enabled
    assert not [e for e in C.stamped_card_errors("AuroraKnowledgeDB/Concepts/Х.md", P42.mk(), off) if "V-03" in e]


@test
def test_section_list_in_the_prompt_is_bounded_and_todo_names_the_new_group(tmp: Path):
    """Перечень разделов в запросе не длиннее предела, сколько бы типов ни завёл проект; новая группа линтера в «Что осталось»
    — решение человека, а не работа маршрута."""
    R = _mod("agent_runner")
    was = R.build_sections
    try:
        R.build_sections = lambda cwd=".": [(f"Раздел{i}", f"тип-{i}") for i in range(500)]
        text = R.types_for_prompt(".")
    finally:
        R.build_sections = was
    assert len(text) <= R.SECTION_LIST_CHARS + 4 and text.endswith("…"), len(text)
    T = _mod("aurora_todo")
    group = "нарушения шаблона у карточек со штампом"
    assert group not in {title for title, _w, _h in T.HUMAN}, "решает движок, а не человек (решение заказчика 15.09)"
    assert group in T.STUCK_WHY, "новая группа линтера без пояснения в «Что осталось»"


@test
def test_a_template_refusal_is_not_taken_for_a_dropped_connection(tmp: Path):
    """Шаг маршрута с кодом 1, чей вывод называет карточку «Настройка SSL» в строке об отказе шаблона, не считается обрывом
    сети (иначе маршрут ждал бы сеть без предела); настоящий обрыв по-прежнему узнаётся."""
    sys.path.insert(0, str(KIT / "cockpit"))
    rr = importlib.import_module("route_runner")
    line = ("  ! не записана AuroraKnowledgeDB/Concepts/Настройка SSL.md: карточка не записана (body) — Настройка SSL.md "
            "нарушает шаблон: V-LINK: «AuroraKnowledgeDB/Concepts/Настройка SSL.md» — ссылка на файл вне базы знаний")
    assert not rr.looks_offline(["шаг", line, "⚠ шаблон типа не принял записей: 1 — строки «!» выше называют карточки"])
    assert rr.looks_offline(["шаг", line, "Connection refused"])


@test
def test_an_edit_that_hands_the_thesis_back_is_not_a_return_to_raw(tmp: Path):
    """Правка карточки с тезисом, которая снимает `distilled` и оставляет `distilled_was` (ремонт вычистил служебное из
    тезиса, источник обновлён), — это «тезис ждёт», и вход её принимает; без памяти об отметке мусорное тело по-прежнему нельзя."""
    C = _C()
    root = P42._project(tmp)
    reg = P42._reg(root)
    path = root / "AuroraKnowledgeDB" / "Concepts" / "Заявка.md"
    P42._put(path, P42.mk())
    waiting = P42.mk(distilled=None, distilled_was="2026-10-01",
                     body="## Источник (перенесено дословно)\n\n### Sources/Confluence/Процессы/Заявка на поставку.md\n\nТекст источника.\n")
    assert C.write_card_text(str(path), waiting, op="repair", reg=reg) is True
    P42._put(path, P42.mk())
    bare = P42.mk(distilled=None, body="## Свойства\n- мусор без определения\n")
    try:
        C.write_card_text(str(path), bare, op="repair", reg=reg)
    except C.TemplateViolation:
        pass
    else:
        raise AssertionError("отметка тезиса снята без памяти о ней, и мусорное тело принято")


@test
def test_a_thesis_of_empty_sentences_leaves_the_card_waiting_for_a_thesis(tmp: Path):
    """`kb:repair --tautologies`: тезис, состоявший из одной пустой фразы («X — [[X]].»), после чистки не оставляет
    карточку с пустой собственной частью: отметка тезиса снимается (с памятью о ней), `agent:distill` напишет тезис заново."""
    root = make_project(tmp, git=True)
    p = root / KB / "Concepts" / "Заявители.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Док.md").write_text("Текст.\n", encoding="utf-8")
    p.write_text('---\ntitle: "Заявители"\naliases: []\ntype: concept\nkind: knowledge\nstatus: draft\nsources:\n'
                 '  - "Sources/Confluence/Док.md"\nsource_synced: 2026-10-01\ncreated: 2026-10-01\nupdated: 2026-10-01\n'
                 'built: machine\ndistilled: 2026-10-01\n---\n\nЗаявители — [[Заявители]].\n\n## Источник (перенесено дословно)\n\n'
                 'Заявители подают заявки.\n', encoding="utf-8")
    cp = run("kb_fix.py", "--tautologies", "--apply", "--allow-dirty", cwd=root)
    text = p.read_text(encoding="utf-8")
    head = text.split("\n---\n", 1)[0]
    assert cp.returncode == 0 and "не записана" not in cp.stderr, cp.stdout + cp.stderr
    assert "distilled_was: 2026-10-01" in head and "\ndistilled:" not in head, head
    assert "Заявители подают заявки." in text and "[[Заявители]]." not in text.split("## Источник")[0], text


@test
def test_a_broken_registry_stops_the_card_writing_tasks_before_any_model_call(tmp: Path):
    """Шаблоны найдены, но ни один не принят: задачи агента, пишущие карточки, не зовут модель (каждая запись была бы
    отвергнута, а вызовы оплачены впустую); читающие задачи не затронуты."""
    R = _mod("agent_runner")
    C = _mod("card_types")
    proj = P42._project(tmp)
    for f in (proj / "TemplatesCommon").glob("akdb_*.md"):
        f.write_text(f.read_text(encoding="utf-8").replace("card_type:", "card_typo:"), encoding="utf-8")
    real_kit = C._find_kit
    C._find_kit = lambda: ("", "")
    try:
        why = R.registry_gate(str(proj))
        assert "ни один не принят" in why and "модель не зовётся" in why, why
        assert {"build", "distill", "extract", "relink"} <= set(R.CARD_WRITING_TASKS) and "ask" not in R.CARD_WRITING_TASKS
        assert R.registry_gate(str(tmp / "empty")) == "", "проект без шаблонов (их просто нет) — не поломка реестра"
    finally:
        C._find_kit = real_kit
    (tmp / "ok").mkdir()
    ok = make_project(tmp / "ok")
    assert R.registry_gate(str(ok)) == ""


@test
def test_a_refused_thesis_gets_one_more_try_with_the_violation_in_the_request(tmp: Path):
    """Тезис, отвергнутый шаблоном, пересобирается один раз с текстом нарушения в запросе; принятый со второй попытки
    записывается, второй отказ паркует карточку."""
    R = _mod("agent_runner")
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Док.md").write_text("Текст.\n", encoding="utf-8")
    head = ('---\ntitle: "{t}"\naliases: []\ntype: concept\nkind: knowledge\nstatus: draft\nsources:\n  - "Sources/Confluence/Док.md"\n'
            'source_synced: 2026-10-01\ncreated: 2026-10-01\nupdated: 2026-10-01\nbuilt: machine\n---\n\n')
    paths = {}
    for name in ("Исправимая", "Упрямая"):
        paths[name] = root / KB / "Concepts" / f"{name}.md"
        paths[name].write_text(head.format(t=name) + "## Источник (перенесено дословно)\n\n" + PAGE, encoding="utf-8")
    seen = []

    def model(cfg_, role, messages, **kw):
        prompt = messages[0]["content"]
        seen.append(prompt)
        retry = "ПРОШЛЫЙ ТЕЗИС НЕ ПРИНЯТ ШАБЛОНОМ ТИПА" in prompt
        if "Упрямая" in prompt or not retry:
            return _answer("## Заголовок вместо определения\n- пункт списка")
        return _answer("Заявка на поставку — документ, которым заказчик поручает поставщику передать товар.")

    with _RunDir(tmp / "run"):
        R.run_distill(_cfg(), str(root), apply=True, limit=0, momus=False, call=model)
    good = paths["Исправимая"].read_text(encoding="utf-8")
    bad = paths["Упрямая"].read_text(encoding="utf-8")
    assert "distilled: " in good and "Заявка на поставку — документ" in good, good[:600]
    assert "distilled:" not in bad, "дважды отвергнутый тезис записан"
    assert sum("ПРОШЛЫЙ ТЕЗИС НЕ ПРИНЯТ" in p for p in seen) == 2, "повторная попытка — по одной на карточку"
    assert R.still_parked(str(root), str(paths["Упрямая"]), R.parked_cards(str(root))), "карточка не отложена после второго отказа"


@test
def test_guide_steps_are_taken_in_order_of_importance_without_skipping_ahead(tmp: Path):
    """Шаг инструкции, не влезший в бюджет, обрывает отбор: менее важный шаг на его место не берётся; тезис и ответ разбора
    при отказе шаблона ставят `content_fail`, чтобы источник отложил план."""
    R = _mod("agent_runner")
    root = make_project(tmp)
    prompts = root / "Prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "KB_classify.md").write_text(
        "## Шаг 0. Нулевой\n\n" + "а " * 10 + "\n\n## Шаг 2. Второй\n\n" + "б " * 20 + "\n\n## Шаг 3. Третий\n\n" + "в " * 400
        + "\n\n## Шаг 6. Шестой\n\n" + "г " * 10 + "\n", encoding="utf-8")
    was = R.GUIDE_PROMPT_CHARS
    try:
        R.GUIDE_PROMPT_CHARS = 300
        got = R.guide_for_prompt(str(root))
    finally:
        R.GUIDE_PROMPT_CHARS = was
    assert re.findall(r"(?m)^## Шаг (\d+)\.", got) == ["2"], got
    # отказ шаблона при сборке карточки — отказ по содержанию
    real = R.run_build_plan
    (root / "Raw/project").mkdir(parents=True, exist_ok=True)
    (root / "Raw/project/Заявка.md").write_text(PAGE, encoding="utf-8")

    def model(cfg_, role, messages, **kw):
        return _answer(json.dumps({"cards": [{"title": "Заявка на поставку", "sections": "1-2", "to": "Concepts"}]}, ensure_ascii=False))

    try:
        R.run_build_plan = lambda cwd, args, timeout=300: {"ok": False, "rc": 1, "out": "", "refused": "",
                                                           "why": "карточка не записана (create) — X.md нарушает шаблон: V-12"}
        with _RunDir(tmp / "run"):
            step = R.solve_source(_cfg(), str(root), "Raw", "Raw/project/Заявка.md", True, False, model, 0)
    finally:
        R.run_build_plan = real
    assert step.get("content_fail") and "нарушает шаблон" in step["note"], step
