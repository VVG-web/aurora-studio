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
    assert R.types_for_prompt(r) == ", ".join(f"{s} ({t})" for s, t in SECTIONS)
    for name in ("PROMPT_BUILD", "PROMPT_MEETING", "PROMPT_PLAN_SOURCE"):
        prompt = getattr(R, name)
        assert "{section_list}" in prompt and "{guide}" in prompt, name
        for sec, _tid in SECTIONS:
            assert sec not in prompt, f"{name}: раздел {sec} вписан в текст запроса"
    _disable(root, "role")
    off = R.types_for_prompt(r)
    assert "Roles" not in off and "Glossary (glossary)" in off, off
    guide = R.guide_for_prompt(r)
    assert "`akdb_role`" not in guide and "`akdb_glossary`" in guide, "строка выключенного типа осталась в таблице инструкции"
    assert "cited-document" not in guide and "`decision`" not in guide, "в таблице типы, которых разбор не предлагает"
    P42._put(root / "Templates" / "akdb_policy_v1.0.md", P42._template_text(P42.POLICY))
    assert R.types_for_prompt(r).endswith("Policies (policy)"), R.types_for_prompt(r)


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
