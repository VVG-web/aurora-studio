#!/usr/bin/env python3
"""part_278 — режим поставляемого документа (`agent:make --deliverable`).

Сборка идёт на временном проекте с подставной моделью: сеть не нужна, живая база не
трогается. Документ пишется в `Deliverables/work/<doc>_v<version>.md`, ПМИ получает в
промпт критика согласованные требования, а `make:assemble` остаётся объявленной командой
и указывает на новый режим.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from harness import KIT, card, make_project, test

sys.path.insert(0, str(KIT / "scripts"))

DOC_TEXT = ("# ПМИ\n\n## Проверки\n\n"
            "Проверка срока согласования [[Заявка-1]]: REQ-001 проверяется отчётом "
            "приёмки, срок десять дней.")


def _cfg() -> dict:
    import agent_core as A
    return A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u",
                           "AURORA_AGENT_BACKEND_1_MODEL": "m"})


class _Fake:
    """Подставная модель: план, документ и вердикт критика без сети."""

    def __init__(self, critic=None, draft: str = DOC_TEXT, qa_text: str = ""):
        self.critic = critic or (lambda n, prompt: {"ok": True, "issues": []})
        self.draft = draft
        self.qa_text = qa_text
        self.calls: list = []                 # [(роль, текст промпта)]

    def __call__(self, cfg, role, messages, **kw):
        prompt = "\n".join(str((m or {}).get("content") or "") for m in messages)
        self.calls.append((role, prompt))
        base = {"backend": 1, "model": "m", "tps": 9, "log": [], "seconds": 0.1}
        if role == "planner":
            return dict(base, ok=True, text=json.dumps(
                {"questions": [], "plan": "1. Проверки"}, ensure_ascii=False))
        if role == "critic":
            count = sum(1 for got, _text in self.calls if got == "critic")
            return dict(base, ok=True, text=json.dumps(self.critic(count, prompt),
                                                       ensure_ascii=False))
        if role == "qa" and self.qa_text:
            return dict(base, ok=True, text=self.qa_text)
        return dict(base, ok=True, text=self.draft)

    def prompts(self, role: str) -> list:
        return [text for got, text in self.calls if got == role]


def _project(tmp: Path, deliverable: bool = True) -> Path:
    """Проект с видом `pmi`; `deliverable` включает режим поставляемого документа."""
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/PMI.md").write_text("# ПМИ\n\n## Проверки\n", encoding="utf-8")
    lines = ['artifacts:', '  pmi:', '    title: "ПМИ"',
             '    template: "Templates/PMI.md"', '    out: "Artifacts/pmi"']
    if deliverable:
        lines.append('    deliverable: "true"')
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip() + "\n"
                        + "\n".join(lines) + "\n", encoding="utf-8")
    card(root, "Concepts/Заявка-1.md",
         "Заявка проходит согласование. Срок десять дней.", status="knowledge", kind="knowledge")
    return root


def _requirements(root: Path) -> None:
    for req_id, status in (("REQ-001", "agreed"), ("REQ-002", "agreed"),
                           ("REQ-003", "rejected")):
        card(root, f"Requirements/{req_id}-Требование.md", "Текст требования.",
             req_status=status, req_id=req_id)


def _run(root: Path, fake: _Fake, doc: str = "ОПЗ", version: str = "2.1", **kw) -> dict:
    import agent_runner as R
    kw = {"momus": False, **kw}
    return R.run_make(_cfg(), str(root), "pmi", "статусы заявки и срок", "", "", True,
                      call=fake, doc=doc, version=version, deliverable=True, **kw)


def _cli(root: Path, *argv: str):
    return subprocess.run([sys.executable, str(root / ".aurora/scripts/agent_runner.py"), *argv],
                          cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120)


@test
def test_deliverable_mode_requires_doc(tmp: Path):
    """Без `--doc` режим отказывает до сессии и до модели: код 1 и понятный текст."""
    root = _project(tmp)
    import agent_runner as R
    doc, version, why = R.deliverable_args(
        SimpleNamespace(doc="", version="", deliverable=True, session=""), str(root))
    assert doc == "" and version == "" and "--doc" in why, (doc, version, why)
    cp = _cli(root, "--task", "make", "--kind", "pmi", "--idea", "x", "--deliverable")
    assert cp.returncode == 1 and "--doc" in cp.stderr, cp.stdout + cp.stderr
    cp = _cli(root, "--task", "make", "--kind", "pmi", "--idea", "x", "--doc", "ОПЗ")
    assert cp.returncode == 1 and "--deliverable" in cp.stderr, cp.stdout + cp.stderr


@test
def test_deliverable_mode_requires_kind_flag(tmp: Path):
    """Вид без `deliverable: true` режим не открывает: это настройка вида, а не каждого документа."""
    root = _project(tmp, deliverable=False)
    import agent_runner as R
    spec = R.make_spec(str(root), "pmi", deliverable=True)
    assert spec.get("error") and "не объявлен как поставляемый" in spec["error"], spec
    res = R.run_make(_cfg(), str(root), "pmi", "статусы заявки и срок", "", "", True,
                     call=_Fake(), momus=False, doc="ОПЗ", version="1.0", deliverable=True)
    assert not res["ok"] and "deliverable: true" in res["why"], res
    assert not list((root / "Deliverables/work").glob("*.md")), "закрытый режим что-то записал"


@test
def test_deliverable_mode_name_and_header(tmp: Path):
    """`<doc>_v<version>.md` в `Deliverables/work`; шапка называет вид, документ, версию и шаблон."""
    root = _project(tmp)
    fake = _Fake()
    res = _run(root, fake, doc="ОПЗ", version="2.1")
    assert res["ok"] and res["stage"] == "done", res
    path = root / res["state"]["path"]
    assert path.name == "ОПЗ_v2.1.md", path
    assert path.parent.name == "work" and path.parent.parent.name == "Deliverables", path
    text = path.read_text(encoding="utf-8")
    for line in ('type: pmi', "deliverable: true", 'doc: "ОПЗ"', 'doc_version: "2.1"',
                 'version: "2.1"', 'template: "Templates/PMI.md"', "based_on:", "status:",
                 "pipeline:"):
        assert line in text, f"в шапке нет «{line}»:\n{text[:900]}"
    assert "[[Заявка-1]]" in text, "документ потерял основание"


@test
def test_deliverable_mode_default_version(tmp: Path):
    """Без `--version` версия `1.0`, и она же стоит в шапке."""
    root = _project(tmp)
    res = _run(root, _Fake(), doc="ПМИ", version="")
    assert res["ok"], res
    path = root / res["state"]["path"]
    assert path.name == "ПМИ_v1.0.md", path
    assert 'doc_version: "1.0"' in path.read_text(encoding="utf-8")


@test
def test_deliverable_mode_rejects_path_in_doc(tmp: Path):
    """`..`, `/`, `\\` в имени документа не выводят файл из `Deliverables/work`."""
    root = _project(tmp)
    import agent_runner as R
    for bad in ("../ОПЗ", "a/b", "a\\b", "..", "ОПЗ..v2"):
        why = R.deliverable_problem(bad, "1.0")
        assert why, f"«{bad}» принято как имя документа"
        res = R.run_make(_cfg(), str(root), "pmi", "статусы заявки и срок", "", "", True,
                         call=_Fake(), momus=False, doc=bad, version="1.0", deliverable=True)
        assert not res["ok"] and res["stage"] == "error", (bad, res)
    cp = _cli(root, "--task", "make", "--kind", "pmi", "--idea", "x",
              "--doc", "../ОПЗ", "--deliverable")
    assert cp.returncode == 1, cp.stdout + cp.stderr
    assert not list((root / "Deliverables/work").glob("*.md")), "отказ всё-таки записал файл"
    assert not (tmp / "ОПЗ.md").exists() and not (root / "ОПЗ.md").exists()


@test
def test_deliverable_mode_existing_name(tmp: Path):
    """Второй документ с тем же именем не получает `-2`: имя поставляемого документа не переименовывают молча."""
    root = _project(tmp)
    first = _run(root, _Fake(), doc="ОПЗ", version="2.1")
    assert first["ok"], first
    path = root / first["state"]["path"]
    before = path.read_bytes()
    second = _run(root, _Fake(), doc="ОПЗ", version="2.1")
    assert not second["ok"] and "уже есть" in second["why"], second
    assert path.read_bytes() == before, "существующий документ перезаписан"
    assert not list((root / "Deliverables/work").glob("ОПЗ_v2.1-*.md")), \
        "появился суффикс -2"


@test
def test_deliverable_mode_resume_keeps_doc(tmp: Path):
    """Продолжение берёт имя и версию из сессии и не даёт сменить их по дороге."""
    root = _project(tmp)
    import agent_runner as R
    first = _run(root, _Fake(), doc="ОПЗ", version="2.1")
    assert first["ok"], first
    sid = first["sid"]
    bad = R.deliverable_args(SimpleNamespace(doc="Другой", version="", deliverable=True,
                                             session=sid), str(root))
    assert bad[2] and "сессия уже собрана как ОПЗ_v2.1" in bad[2], bad
    doc, version, why = R.deliverable_args(
        SimpleNamespace(doc="", version="", deliverable=True, session=sid), str(root))
    assert (doc, version, why) == ("ОПЗ", "2.1", ""), (doc, version, why)
    res = R.run_make(_cfg(), str(root), "", "", sid, "", True, call=_Fake(), momus=False,
                     doc=doc, version=version, deliverable=True)
    assert res["ok"], res
    assert res["state"]["doc"] == "ОПЗ" and res["state"]["version"] == "2.1", res["state"]
    assert (root / res["state"]["path"]).name == "ОПЗ_v2.1.md"
    # сессия обычного вида поставляемым документом не продолжается
    R.save_session(str(root), "pmi-plain", {"sid": "pmi-plain", "kind": "pmi", "idea": "x",
                                            "spec": {"deliverable": False, "title": "ПМИ"},
                                            "stages": {}})
    plain = R.deliverable_args(SimpleNamespace(doc="", version="", deliverable=True,
                                               session="pmi-plain"), str(root))
    assert plain[2] and "--deliverable" in plain[2], plain


@test
def test_pmi_block_lists_agreed_requirements(tmp: Path):
    """В промпте критика названы все `req_status: agreed` и нет отклонённых."""
    root = _project(tmp)
    _requirements(root)
    import agent_runner as R
    assert [req["id"] for req in R.pmi_requirements(str(root))] == ["REQ-001", "REQ-002"]
    fake = _Fake()
    res = _run(root, fake, doc="ПМИ", version="1.0")
    assert res["ok"], res
    critic_prompt = fake.prompts("critic")[0]
    assert "REQ-001" in critic_prompt and "REQ-002" in critic_prompt, critic_prompt
    assert "REQ-003" not in critic_prompt and "rejected" not in critic_prompt, critic_prompt


@test
def test_pmi_block_empty_without_kb(tmp: Path):
    """Нет базы требований — блок говорит «не найдены», и сборка не падает."""
    root = _project(tmp)
    shutil.rmtree(root / "AuroraKnowledgeDB/Requirements")
    import agent_runner as R
    block = R.pmi_block(str(root), "pmi", True)
    assert "карточки требований не найдены" in block, block
    assert R.pmi_block(str(root), "opz", True) == "" and R.pmi_block(str(root), "pmi", False) == ""
    fake = _Fake()
    res = _run(root, fake, doc="ПМИ", version="1.0")
    assert res["ok"], res
    assert "карточки требований не найдены" in fake.prompts("critic")[0]


@test
def test_pmi_missing_check_goes_to_repair(tmp: Path):
    """Пропуск пункта проверки приходит замечанием критика и уходит в существующий ремонт."""
    root = _project(tmp)
    _requirements(root)

    def critic(count, prompt):
        if count == 1:
            return {"ok": False, "issues": ["REQ-002: в ПМИ нет пункта проверки"]}
        return {"ok": True, "issues": []}

    fake = _Fake(critic=critic)
    res = _run(root, fake, doc="ПМИ", version="1.0")
    assert res["ok"], res
    revise = [text for text in fake.prompts("worker") if "Исправь документ" in text]
    assert revise and "REQ-002" in revise[0], revise
    assert len(fake.prompts("critic")) >= 2, "повторный критик не проверил исправление"


@test
def test_make_assemble_is_deprecated(_t):
    """`make:assemble` помечена устаревшей и во всех четырёх точках указывает на новый режим."""
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    line = next(row for row in reg.splitlines() if row.startswith("make | make:assemble"))
    assert "устарело" in line and "agent:make --deliverable" in line, line
    flows = (KIT / "skills/aurora-vault/references/workflows.md").read_text(encoding="utf-8")
    section = flows[flows.index("## assemble"):flows.index("## publish")]
    assert "Устарело с 1.231.17" in section and "agent:make --kind" in section, section
    assert "--deliverable" in section and "<doc>_v<version>.md" in section, section
    skill = (KIT / "skills/aurora-vault/SKILL.md").read_text(encoding="utf-8")
    assert "`make:assemble" in skill and "agent:make --deliverable" in skill, "SKILL.md потерял команду"
    data = json.loads((KIT / "cockpit/i18n/data/en.json").read_text(encoding="utf-8"))
    assert "agent:make --deliverable" in data["commands"]["make:assemble"], data["commands"]["make:assemble"]


@test
def test_make_assemble_still_declared(_t):
    """Команда не удалена, реестр читается, зависимости не сломаны."""
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    rows = [row for row in reg.splitlines() if row.startswith("make | make:assemble")]
    assert len(rows) == 1 and len(rows[0].split("|")) == 7, rows
    deps = json.loads((KIT / "command_deps.json").read_text(encoding="utf-8"))
    assert deps["commands"]["make:assemble"]["requires_llm"] is True, deps["commands"].get("make:assemble")


@test
def test_pmi_coverage_counts_requirements_in_text(tmp: Path):
    """Счёт покрытия не зависит от критика: id в тексте есть — покрыто, нет — missing и отчёт."""
    root = _project(tmp)
    _requirements(root)
    import agent_runner as R
    text = ("# ПМИ\n\n## Проверки\n\n"
            "Проверка REQ-001: отчёт приёмки подтверждает срок.")
    cov = R.pmi_coverage(str(root), text)
    assert cov == {"required": 2, "covered": 1, "missing": ["REQ-002"]}, cov
    fake = _Fake(draft=text)
    res = _run(root, fake, doc="ПМИ", version="1.0")
    assert res["ok"], res
    path = root / res["state"]["path"]
    doc = path.read_text(encoding="utf-8")
    assert 'pmi_coverage: "1/2"' in doc, doc[:1200]
    assert 'pmi_missing: ["REQ-002"]' in doc, doc[:1200]
    assert "## Покрытие ПМИ" in doc and "REQ-002" in doc, doc
    assert "status: draft" in doc, "поставляемый документ получил ready без приёмки"
    assert res["state"]["pmi_coverage"] == cov, res["state"].get("pmi_coverage")
    report = R.report_make(res)
    assert "Покрытие ПМИ: 1/2" in report and "REQ-002" in report, report


@test
def test_deliverable_never_ready_even_when_pipeline_clean(tmp: Path):
    """Чистая цепочка и Момус не ставят `ready`: у поставляемого документа приёмка впереди."""
    root = _project(tmp)
    draft = ("# ПМИ\n\n## Проверки\n\n"
             "Срок согласования — десять дней, подтверждается отчётом приёмки.")
    fake = _Fake(draft=draft, qa_text="ВЕРДИКТ: ЧИСТО")
    res = _run(root, fake, doc="ПМИ", version="1.0", momus=True)
    assert res["ok"] and res["stage"] == "done", res
    assert res["state"]["stages"].get("checked"), "чистая цепочка не дошла до Момуса"
    doc = (root / res["state"]["path"]).read_text(encoding="utf-8")
    assert "status: draft" in doc, "автоматическая сборка поставила ready"
    assert "deliverable: true" in doc
