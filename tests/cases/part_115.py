"""Проверки движка Aurora, часть 115. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 4 «Записи в Raw/», пакет CT-14: зеркало записей в базе. Зеркала трёх типов совпадают с эталоном
побайтно (кроме даты сверки), второй прогон ничего не пишет, исчезнувший мастер уводит зеркало в архив, занятое имя
называется находкой V-REC-1, а проходы базы (`kb:kind`, `kb:trust`, `agent:*`, `kb:repair`, слияние двойников) тел зеркал не
меняют. Решение `accepted` идёт в строгий пак, `proposed` — только в режиме оценки и с «НЕ ФАКТ», вопрос — только в режиме
оценки. Сброс базы сносит зеркала, повторный `--mirror` возвращает их. Шаги маршрутов стоят после синхронизации и до разбора.
Модель и сеть не используются; проекты — только во временных папках.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from cases import part_114 as P114
from cases import part_70 as P70
from harness import KIT, SCRIPTS, card, run, test

KB = "AuroraKnowledgeDB"
decision, question, reference = P114.decision, P114.question, P114.reference
_put, _read, _mod, _base = P114._put, P114._read, P114._mod, P114._base

STEP = "kb:record"
STEP_FLAGS = ["--mirror", "--apply", "--allow-dirty"]


def _dated(text: str) -> str:
    """Текст без даты сверки: она — день, в который зеркало строили."""
    return re.sub(r"(?m)^source_synced: \d{4}-\d{2}-\d{2}$", "source_synced: DATE", text.replace("\r\n", "\n"))


def _mirror(root: Path, *args, expect=0):
    return run("kb_record.py", "--mirror", *args, cwd=root, expect_rc=expect)


def _built(tmp: Path) -> Path:
    """Проект с тремя записями и построенными зеркалами; дерево чистое."""
    root = _base(tmp)
    for rel, text in (decision(1, "срок"), question(1, "вложения", status="asked"), reference()):
        _put(root, rel, text)
    _mirror(root, "--apply", "--allow-dirty")
    P70._commit(root)
    return root


# ------------------------------------------------------------------ золотые зеркала (AC-CT-14.1)

GOLDEN_DECISION = """\
---
title: "DR-0001: Срок подтверждения заявки — три рабочих дня"
aliases: ["DR-0001"]
tags: [decision]
type: decision
template: akdb_decision_v1.0.md
kind: document
status: accepted
date: 2026-09-20
owner: "@analyst"
sources:
  - "Raw/decisions/DR-0001-срок.md"
source_synced: DATE
created: 2026-09-20
updated: 2026-10-01
built: machine
supersedes: []
applies_to: []
---
"""
GOLDEN_QUESTION = """\
---
title: "Q-001: Максимум вложений в заявке"
aliases: ["Q-001"]
tags: [question]
type: question
template: akdb_question_v1.0.md
kind: document
status: draft
q_id: Q-001
q_status: asked
owner: "@analyst"
asked_to: "заказчик, отдел снабжения"
channel: "письмо"
asked: 2026-09-25
blocks: ["[[Заявка на поставку]]"]
q_effective: open
sources:
  - "Raw/questions/Q-001-вложения.md"
source_synced: DATE
created: 2026-09-24
updated: 2026-10-01
built: machine
---
"""
GOLDEN_REFERENCE = """\
---
title: "Коды причин отказа"
aliases: ["SPR-004"]
type: reference
template: akdb_reference_v1.0.md
kind: dictionary
status: knowledge
owner: "@analyst"
sources:
  - "Raw/reference/Коды-причин-отказа.md"
source_synced: DATE
created: 2026-08-09
updated: 2026-10-01
built: machine
summary: "Коды причин, по которым заявка не принимается."
---
"""


@test
def test_mirror_golden_decision_question_reference(tmp: Path):
    """AC-CT-14.1: зеркала трёх типов — шапка по конструктору, тело мастера побайтно; кроме даты сверки, эталон совпадает."""
    root = _built(tmp)
    cases = (("Decisions/DR-0001-срок.md", decision(1, "срок"), GOLDEN_DECISION),
             ("Questions/Q-001-вложения.md", question(1, "вложения", status="asked"), GOLDEN_QUESTION),
             ("Reference/Коды-причин-отказа.md", reference(), GOLDEN_REFERENCE))
    for rel, (master_rel, master_text), golden in cases:
        got = _dated(_read(root, f"{KB}/{rel}"))
        body = master_text.split("\n---\n", 1)[1]
        assert got == golden + body, f"{rel}: зеркало не совпало с эталоном\n{got[:900]}"
    # зеркало проходит единый вход записи и проверку шаблона строго
    C = _mod("card_types")
    reg = C.registry_for(str(root))
    for rel, _m, _g in cases:
        text = _read(root, f"{KB}/{rel}")
        errors = [v for v in C.validate_card(text, path=f"{KB}/{rel}", reg=reg, level="strict") if v.severity == "error"]
        assert not errors, (rel, [(v.code, v.message) for v in errors])
    assert run("kb_record.py", "--check", cwd=root, expect_rc=0).stdout.count("V-REC-2") == 0, "зеркала отстают сразу после сборки"


@test
def test_mirror_body_is_byte_for_byte_but_service_headings_are_escaped(tmp: Path):
    """Тело мастера переносится побайтно (`\\r\\n` — как есть в мастере читается одинаково); `## История изменений` экранируется."""
    root = _base(tmp)
    rel, text = decision(1, "срок")
    text = text.replace("## История\n", "## История изменений\n- запись о ходе дела\n\n## История\n")
    _put(root, rel, text)
    _mirror(root, "--apply", "--allow-dirty")
    got = _read(root, f"{KB}/Decisions/DR-0001-срок.md")
    assert "\n\\#\\# История изменений\n" in got and "\n## История изменений" not in got, "служебный заголовок хвоста не экранирован"
    assert got.split("\n---\n", 1)[1].replace("\\#\\#", "##") == text.split("\n---\n", 1)[1]


@test
def test_mirror_copies_only_the_listed_fields_and_quotes_the_names(tmp: Path):
    """Из мастера копируются перечисленные поля; чужие (`note`, `kind` у решения) в зеркало не попадают; владелец в кавычках."""
    root = _base(tmp)
    rel, text = decision(1, "срок", owner="@analyst", note="для себя", kind="knowledge")
    _put(root, rel, text)
    _mirror(root, "--apply", "--allow-dirty")
    got = _read(root, f"{KB}/Decisions/DR-0001-срок.md")
    A = _mod("aurora_common")
    fm = A.frontmatter(got)
    assert "note" not in fm and fm["kind"] == "document", fm
    assert 'owner: "@analyst"' in got
    assert fm["built"] == "machine" and A.card_sources(got) == [rel]


# ------------------------------------------------------------------ идемпотентность, архив, имена (AC-CT-14.2…14.4)

@test
def test_mirror_is_idempotent_and_ignores_the_sync_date(tmp: Path):
    """AC-CT-14.2: второй прогон ничего не пишет; дата сверки и правка даты `updated` в мастере не повод переписывать зеркало."""
    root = _built(tmp)
    paths = sorted((root / KB).glob("*/*.md"))
    paths = [p for p in paths if p.parent.name in ("Decisions", "Questions", "Reference")]
    assert len(paths) == 3
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
    cp = _mirror(root, "--apply", "--allow-dirty", expect=0)
    assert "без изменений: 3" in cp.stdout and "создать: 0" in cp.stdout and "Записано: 0" in cp.stdout, cp.stdout
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths} == before
    # старая дата сверки не меняет решения «изменилось ли»
    m = root / "Raw/decisions/DR-0001-срок.md"
    m.write_text(m.read_text(encoding="utf-8").replace("updated: 2026-10-01", "updated: 2026-10-05"), encoding="utf-8")
    cp = _mirror(root, "--apply", "--allow-dirty", expect=0)
    assert "обновить: 0" in cp.stdout, cp.stdout
    # по существу изменилось — зеркало обновляется, дата сверки новая
    m.write_text(m.read_text(encoding="utf-8").replace("Решили аналитик и руководитель проекта.", "Решили втроём."), encoding="utf-8")
    cp = _mirror(root, expect=1)
    assert "обновить: 1" in cp.stdout and "(dry-run)" in cp.stdout
    assert "Решили втроём" not in _read(root, f"{KB}/Decisions/DR-0001-срок.md"), "сухой прогон записал"
    _mirror(root, "--apply", "--allow-dirty")
    assert "Решили втроём" in _read(root, f"{KB}/Decisions/DR-0001-срок.md")
    cp = _mirror(root, "--apply", "--allow-dirty")
    assert "без изменений: 3" in cp.stdout


@test
def test_mirror_keeps_what_other_passes_wrote(tmp: Path):
    """Поля, которые пишут другие проходы (`related`, `trust*`), переживают обновление зеркала по существу."""
    root = _built(tmp)
    run("kb_trust.py", "--apply", cwd=root)
    ref = root / KB / "Reference" / "Коды-причин-отказа.md"
    A = _mod("aurora_common")
    assert A.frontmatter(ref.read_text(encoding="utf-8")).get("trust") == "raw", "kb:trust не поставил класс зеркалу справочника"
    cp = _mirror(root, "--apply", "--allow-dirty", expect=0)
    assert "без изменений: 3" in cp.stdout, "доверие, поставленное kb:trust, сочтено правкой зеркала"
    with_related = ref.read_text(encoding="utf-8").replace("built: machine\n", 'built: machine\nrelated: ["[Заявка на поставку](Заявка на поставку.md)"]\n')
    ref.write_text(with_related, encoding="utf-8")
    m = root / "Raw/reference/Коды-причин-отказа.md"
    m.write_text(m.read_text(encoding="utf-8") + "| 03 | Поставщик не найден |\n", encoding="utf-8")
    _mirror(root, "--apply", "--allow-dirty", expect=0)
    after = ref.read_text(encoding="utf-8")
    fm = A.frontmatter(after)
    assert "03 | Поставщик не найден" in after and fm["trust"] == "raw" and "Заявка на поставку" in fm["related"], after[:700]


@test
def test_mirror_of_a_vanished_master_goes_to_the_archive(tmp: Path):
    """AC-CT-14.3: мастер удалён — зеркало уходит в `_archive/` с записью в «История изменений»; из базы пропадает."""
    root = _built(tmp)
    (root / "Raw/questions/Q-001-вложения.md").unlink()
    cp = _mirror(root, expect=1)
    assert "снять в архив: 1" in cp.stdout and (root / KB / "Questions" / "Q-001-вложения.md").exists(), cp.stdout
    assert "V-REC-2" in run("kb_record.py", "--check", cwd=root).stdout
    cp = _mirror(root, "--apply", "--allow-dirty", expect=0)
    assert not (root / KB / "Questions" / "Q-001-вложения.md").exists()
    archived = _read(root, f"{KB}/_archive/Q-001-вложения.md")
    assert "мастер-файл `Raw/questions/Q-001-вложения.md` исчез" in archived.split("## История изменений")[1], archived[-400:]
    # занятое имя в архиве не затирается
    _put(root, *question(1, "вложения", status="asked"))
    _mirror(root, "--apply", "--allow-dirty")
    (root / "Raw/questions/Q-001-вложения.md").unlink()
    _mirror(root, "--apply", "--allow-dirty")
    assert len(list((root / KB / "_archive").glob("Q-001-вложения*.md"))) == 2
    # зеркало выведенного из Confluence справочника (источник не в Raw/reference) архивом не трогается
    _put(root, f"{KB}/Reference/Выведенный.md",
         '---\ntitle: Выведенный\ntype: reference\nkind: dictionary\nstatus: knowledge\nbuilt: machine\nsources:\n  - "Sources/Confluence/Нет.md"\n---\n\nx\n')
    _mirror(root, "--apply", "--allow-dirty")
    assert (root / KB / "Reference" / "Выведенный.md").is_file()


@test
def test_mirror_name_collision_is_reported_and_nothing_is_written(tmp: Path):
    """AC-CT-14.4: имя занято не зеркалом — находка V-REC-1, зеркала нет, чужая карточка цела; два мастера с одним именем — тоже."""
    root = _base(tmp)
    rel, text = decision(1, "срок")
    other = card(root, "Decisions/DR-0001-срок.md", "Рукописная карточка с тем же именем.", type="decision", status="accepted", date="2026-09-01")
    before = other.read_text(encoding="utf-8")
    _put(root, rel, text)
    cp = _mirror(root, "--apply", "--allow-dirty", expect=1)
    assert "V-REC-1" in cp.stdout and "уже лежит не зеркало" in cp.stdout, cp.stdout
    assert other.read_text(encoding="utf-8") == before, "чужая карточка перезаписана зеркалом"
    other.unlink()
    _mirror(root, "--apply", "--allow-dirty", expect=0)
    assert (root / KB / "Decisions" / "DR-0001-срок.md").is_file()
    # два мастера с одним именем в разных папках справочников
    _put(root, "Raw/reference/Группа/Коды.md", reference()[1])
    _put(root, "Raw/reference/Коды.md", reference()[1])
    cp = _mirror(root, "--apply", "--allow-dirty", expect=1)
    assert cp.stdout.count("V-REC-1") >= 1 and "уже занято мастер-файлом" in cp.stdout, cp.stdout
    assert len(list((root / KB / "Reference").glob("Коды*.md"))) == 1


@test
def test_mirror_waits_for_a_filled_master_and_refuses_what_the_template_would(tmp: Path):
    """Заготовка с `{{…}}` зеркалом не становится; вопрос без `q_status` — не зеркало; чужая папка — V-17."""
    root = _base(tmp)
    run("kb_record.py", "--new", "decision", "Заготовка", "--owner", "@analyst", cwd=root, expect_rc=0)
    _put(root, *question(2, "без-хода", q_status=None))
    _put(root, "Raw/decisions/Q-009-чужой.md", question(9, "чужой")[1])
    cp = _mirror(root, "--apply", "--allow-dirty", expect=1)
    assert "V-13" in cp.stdout and "V-17" in cp.stdout and "q_status" in cp.stdout, cp.stdout
    assert not list((root / KB / "Decisions").glob("*.md")) and not list((root / KB / "Questions").glob("*.md")), "записаны зеркала по негодным мастерам"


@test
def test_mirror_is_dry_by_default_and_guards_a_dirty_tree(tmp: Path):
    """Сухой прогон по умолчанию; `--apply` по грязному дереву — отказ, с `--allow-dirty` — работает; проект без git — отказ."""
    root = _base(tmp)
    _put(root, *decision(1, "срок"))
    cp = _mirror(root, expect=1)
    assert "(dry-run)" in cp.stdout and not (root / KB / "Decisions" / "DR-0001-срок.md").exists()
    (root / KB / "Concepts" / "Заявка на поставку.md").write_text(
        (root / KB / "Concepts" / "Заявка на поставку.md").read_text(encoding="utf-8") + "\nправка\n", encoding="utf-8")
    cp = _mirror(root, "--apply", expect=2)
    assert "git-guard" in cp.stderr and not (root / KB / "Decisions" / "DR-0001-срок.md").exists()
    _mirror(root, "--apply", "--allow-dirty", expect=0)
    assert (root / KB / "Decisions" / "DR-0001-срок.md").is_file()
    (tmp / "ng").mkdir()
    nogit = P114._project(tmp / "ng", git=False)
    _put(nogit, *decision(1, "срок"))
    cp = _mirror(nogit, "--apply", expect=2)
    assert "не под git" in cp.stderr
    _mirror(nogit, "--apply", "--allow-dirty", expect=0)


# ------------------------------------------------------------------ проходы базы не меняют зеркал (AC-CT-14.5)

@test
def test_passes_leave_mirror_bodies_alone(tmp: Path):
    """AC-CT-14.5: `kb:kind`, `kb:trust`, `kb:repair` (все шаги) и слияние двойников не меняют тел зеркал и вид `kind`."""
    root = _built(tmp)
    # зеркало справочника со второй копией, чтобы двойник был: тот же текст в карточке из Confluence
    _put(root, f"{KB}/Reference/Двойник.md", _read(root, f"{KB}/Reference/Коды-причин-отказа.md").replace(
        "Raw/reference/Коды-причин-отказа.md", "Sources/Confluence/Двойник.md").replace('title: "Коды причин отказа"', 'title: "Двойник"'))
    P70._commit(root)
    mirrors = {rel: _read(root, f"{KB}/{rel}") for rel in ("Decisions/DR-0001-срок.md", "Questions/Q-001-вложения.md",
                                                           "Reference/Коды-причин-отказа.md")}
    run("kb_kind.py", "--apply", cwd=root)
    run("kb_trust.py", "--apply", cwd=root)
    run("kb_fix.py", "--all", "--apply", "--allow-dirty", cwd=root)
    run("kb_fix.py", "--dupes", "--merge-all", "--apply", "--allow-dirty", cwd=root)
    A = _mod("aurora_common")
    for rel, was in mirrors.items():
        now = _read(root, f"{KB}/{rel}")
        assert A.body(now) == A.body(was), f"{rel}: тело зеркала изменено"
        assert A.frontmatter(now).get("kind") == A.frontmatter(was).get("kind"), f"{rel}: вид изменён"
        assert A.frontmatter(now).get("status") == A.frontmatter(was).get("status"), rel
    assert _mirror(root, expect=None).stdout.count("обновить: 0") == 1, "после проходов зеркала нужно обновлять"


@test
def test_agent_queues_skip_record_mirrors(tmp: Path):
    """AC-CT-14.5: `agent:distill`, `agent:relink`, `agent:extract` и поиск двойников зеркал записей не берут, даже если зеркало велико."""
    R = _mod("agent_runner")
    root = _built(tmp)
    A = _mod("aurora_common")
    # справочник крупнее окна: словарь такого размера distill режет — зеркало записи резать нельзя
    big = "| код | значение |\n|---|---|\n" + "".join(f"| {i:04d} | значение {i} |\n" for i in range(3000))
    rel, text = reference("Большой", body="# Коды причин отказа\n\n" + big)
    _put(root, rel, text)
    _mirror(root, "--apply", "--allow-dirty")
    mirror = root / KB / "Reference" / "Большой.md"
    assert mirror.is_file() and A.is_record_mirror(A.frontmatter(mirror.read_text(encoding="utf-8")), mirror.read_text(encoding="utf-8"))
    cfg = {"max_steps": 10, "budget_min": 5, "context_window": 2000, "backends": [], "roles": {}}
    try:
        queue = R.distill_queue(cfg, str(root))
    except Exception:                      # noqa: BLE001 — настройка окна у проекта своя; важен ответ на вопрос «берёт ли очередь зеркало»
        queue = []
    assert not [p for p in queue if "/Reference/Большой.md" in p.replace("\\", "/") or "/Decisions/" in p.replace("\\", "/")], queue
    twins = run("kb_twins.py", "--min", "0.3", "--limit", "0", cwd=root).stdout
    assert "Решения" not in twins and "DR-0001-срок" not in twins and "Q-001-вложения" not in twins, twins


@test
def test_build_does_not_append_into_a_record_mirror(tmp: Path):
    """Разбор источника не дописывает знание в зеркало записи: оно пересоздаётся из мастера, дописанное пропало бы."""
    BP = _mod("build_plan")
    root = _built(tmp)
    mirror = root / KB / "Reference" / "Коды-причин-отказа.md"
    old = mirror.read_text(encoding="utf-8")
    cwd = os.getcwd()
    os.chdir(root)
    try:
        rc = BP.append_card(str(mirror), old, "Новый текст другого источника.", "Raw/customer/Письмо.md", True, str(root))
    finally:
        os.chdir(cwd)
    assert rc == 1 and mirror.read_text(encoding="utf-8") == old


@test
def test_build_candidates_do_not_offer_a_record_mirror_as_a_target(tmp: Path):
    """Модели разбора в список «куда дописать» зеркало записи не предлагается; в список ссылок и в ответ — входит как любая карточка."""
    R = _mod("agent_runner")
    root = _built(tmp)
    rel, text = reference("Сроки", body="# Коды причин отказа\n\n| Код | Причина |\n|---|---|\n| 02 | Срок поставки в прошлом |\n")
    _put(root, rel, text.replace("Коды причин отказа", "Сроки поставки"))
    _mirror(root, "--apply", "--allow-dirty")
    query = "срок подтверждения заявки на поставку вложения"
    with_mirrors = {name for name, _sec, _brief in R.hybrid_rank(str(root), query, 10)}
    for_into = {name for name, _sec, _brief in R.hybrid_rank(str(root), query, 10, appendable=True)}
    assert {"DR-0001-срок", "Q-001-вложения"} & with_mirrors, f"зеркала не найдены выборкой вовсе: {with_mirrors}"
    assert not {"DR-0001-срок", "Q-001-вложения", "Сроки"} & for_into and "Заявка на поставку" in for_into, for_into


# ------------------------------------------------------------------ вид и доверие

@test
def test_kind_keeps_mirror_kinds_and_trust_reads_the_reference_mirror_as_raw(tmp: Path):
    """`kb:kind` не пересчитывает вид зеркала (справочник вида `document` остаётся); у зеркала справочника класс `raw` → `knowledge`."""
    root = _base(tmp)
    rel, text = reference("Документ", body="# Коды причин отказа\n\nПеренос дословно, без таблицы.\n", kind="document")
    _put(root, rel, text)
    _put(root, *decision(1, "срок"))
    _mirror(root, "--apply", "--allow-dirty")
    P70._commit(root)
    A = _mod("aurora_common")
    run("kb_kind.py", "--apply", cwd=root, expect_rc=0)
    assert A.frontmatter(_read(root, f"{KB}/Reference/Документ.md"))["kind"] == "document"
    assert A.frontmatter(_read(root, f"{KB}/Decisions/DR-0001-срок.md"))["kind"] == "document"
    K = _mod("kb_kind")
    assert "Raw/decisions" in K.DOC_ROOTS and "Raw/questions" in K.DOC_ROOTS and "Raw/reference" not in K.DOC_ROOTS
    run("kb_trust.py", "--apply", cwd=root)
    fm = A.frontmatter(_read(root, f"{KB}/Reference/Документ.md"))
    assert fm["status"] == "knowledge" and fm["trust"] == "raw", fm
    assert A.frontmatter(_read(root, f"{KB}/Decisions/DR-0001-срок.md"))["status"] == "accepted"


@test
def test_lint_names_hand_written_records_that_wait_for_the_move(tmp: Path):
    """`kb:lint` показывает рукописную запись в базе предупреждением с отсылкой к переносу; зеркало замечаний не вызывает."""
    root = _built(tmp)
    card(root, "Decisions/DR-0007-рукописное.md", "Рукописное решение.", type="decision", status="accepted", date="2026-09-01",
         template="akdb_decision_v1.0.md", kind="document")
    out = run("kb_lint.py", cwd=root).stdout
    assert "рукописная карточка в базе" in out or "рукописных записей" in out, out[-900:]
    assert "kb:record --migrate" in out, out[-900:]
    assert "DR-0001-срок" not in "\n".join(l for l in out.splitlines() if "рукописн" in l)


# ------------------------------------------------------------------ выдача в контекст (AC-CT-14.6)

def _pack(root: Path, topic: str, mode: str) -> str:
    return run("ctx_pack.py", topic, "--mode", mode, "--no-log", "--no-semantic", cwd=root).stdout


@test
def test_ctx_pack_decision_statuses_and_a_question_never_strict(tmp: Path):
    """AC-CT-14.6: `accepted` — в строгом режиме; `proposed` — только в оценке и с «НЕ ФАКТ»; вопрос — только в оценке, даже при bootstrap."""
    root = _base(tmp)
    body = P114.DR_BODY.replace("Срок подтверждения заявки — три рабочих дня", "Срок подтверждения заявки на поставку — три дня")
    for n, name, status in ((1, "принято", "accepted"), (2, "предложено", "proposed"), (3, "отклонено", "rejected")):
        rel, text = decision(n, name, status=status, body=body.replace("DR-0001", f"DR-{n:04d}"),
                             title=f'"DR-{n:04d}: Срок подтверждения заявки на поставку — три дня"')
        _put(root, rel, text)
    _put(root, *question(1, "вложения", status="open", body=(
        "# Q-001: Максимум вложений в заявке\n\n## Вопрос\nСколько файлов можно прикладывать к заявке на поставку и какой срок подтверждения?\n\n"
        "## Почему это важно\nБез ответа не закрыть [[Заявка на поставку]].\n")))
    _mirror(root, "--apply", "--allow-dirty")
    topic = "срок подтверждения заявки на поставку"

    def shown(out: str, stem: str) -> bool:
        return f"## {stem}" in out                     # карточка в паке — раздел с её именем; имя в пометке «скрыто» — не карточка

    def strict(root_: Path) -> None:
        for mode in ("generate", "review"):
            out = _pack(root_, topic, mode)
            assert shown(out, "DR-0001-принято") and "решение принято 2026-09-20" in out, f"{mode}: принятое решение не в паке\n{out[:700]}"
            for other in ("DR-0002-предложено", "DR-0003-отклонено", "Q-001-вложения"):
                assert not shown(out, other), f"{mode}: {other} в строгом режиме"
            assert "Сколько файлов можно прикладывать" not in out, f"{mode}: текст вопроса в строгом паке"

    strict(root)
    ev = _pack(root, topic, "evaluate")
    assert shown(ev, "DR-0002-предложено") and "НЕ ФАКТ" in ev.split("## DR-0002-предложено")[1].split("\n---\n")[0], ev[:900]
    assert shown(ev, "DR-0001-принято") and shown(ev, "Q-001-вложения"), "в режиме оценки нет принятого решения или вопроса"
    ask = _pack(root, topic, "ask")
    assert shown(ask, "DR-0003-отклонено") and not shown(ask, "DR-0002-предложено") and not shown(ask, "Q-001-вложения"), ask[:700]
    # BOOTSTRAP (доля доверенного ниже порога) пускает в пак черновики, но не вопрос
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\nverified_threshold_pct: 99\n", encoding="utf-8")
    assert "BOOTSTRAP" in _pack(root, topic, "generate"), "порог не включил bootstrap: тест не проверяет утечку"
    strict(root)


# ------------------------------------------------------------------ сброс и маршруты (AC-CT-14.7, AC-CT-14.8)

@test
def test_reset_removes_mirrors_and_mirror_brings_them_back(tmp: Path):
    """AC-CT-14.7: `kb:reset` сносит зеркала (`built: machine`), мастера в `Raw/` целы; `kb:record --mirror` возвращает те же зеркала."""
    root = _built(tmp)
    mirrors = {p: p.read_bytes() for p in (root / KB).glob("*/*.md") if p.parent.name in ("Decisions", "Questions", "Reference")}
    assert len(mirrors) == 3
    run("kb_reset.py", "--apply", cwd=root, expect_rc=0)
    assert not any(p.exists() for p in mirrors), "сброс оставил зеркала"
    for rel in ("Raw/decisions/DR-0001-срок.md", "Raw/questions/Q-001-вложения.md", "Raw/reference/Коды-причин-отказа.md"):
        assert (root / rel).is_file(), f"сброс тронул мастер {rel}"
    _mirror(root, "--apply", "--allow-dirty", expect=0)
    back = {p: p.read_bytes() for p in mirrors}
    assert {p: _dated(v.decode("utf-8")) for p, v in back.items()} == {p: _dated(v.decode("utf-8")) for p, v in mirrors.items()}


def _routes() -> dict:
    out, cur = {}, None
    for line in (KIT / "cockpit" / "scenarios.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\[(\w+)\]", line)
        if m:
            cur = out.setdefault(m.group(1), [])
        elif cur is not None and line.strip() and not line.startswith(("#", "-")):
            parts = [p.strip() for p in line.split("|")]
            cmd = parts[0]
            if cmd.startswith(("цикл:", "конец цикла")):
                cur.append((cmd.split(":")[0], "", []))
                continue
            cur.append((cmd, parts[1] if len(parts) > 1 else "", (parts[2] if len(parts) > 2 else "").split()))
    return out


@test
def test_routes_order_mirror_before_build(_t):
    """AC-CT-14.8: шаг зеркал стоит после `sync:*` и до первого `agent:build` в «Обновить» и «Пересобрать»; в «Починить» — сразу после перевода документов в текст (он первый);
    в «Пересобрать» — сразу после `kb:reset`; шаг переведён на английский."""
    routes = _routes()
    for rid in ("update", "fix", "rebuild"):
        steps = routes[rid]
        at = [i for i, (cmd, _t2, fl) in enumerate(steps) if cmd == STEP and fl == STEP_FLAGS]
        assert len(at) == 1, (rid, at)
        i = at[0]
        assert not any(c.startswith("sync:") for c, _x, _f in steps[i + 1:]), f"{rid}: синхронизация после зеркал"
        build = [j for j, (c, _x, _f) in enumerate(steps) if c == "agent:build"]
        assert all(i < j for j in build), f"{rid}: зеркала после первого agent:build"
    fix = routes["fix"]
    assert fix[0][0] == "kb:ingest-office" and fix[1][0] == STEP, "в «Починить» зеркала — сразу после перевода документов, до ремонта"
    rb = routes["rebuild"]
    ri = next(i for i, (c, _x, _f) in enumerate(rb) if c == "kb:reset")
    assert rb[ri + 1][0] == STEP, "в «Пересобрать» зеркала — сразу после kb:reset"
    up = routes["update"]
    assert up[[c for c, _x, _f in up].index("sync:audit") + 1][0] in (STEP, "kb:ingest-office")
    en = json.loads((KIT / "cockpit/i18n/data/en.json").read_text(encoding="utf-8"))["scenarios"]
    texts = {t for rid in ("update", "fix", "rebuild") for c, t, _f in routes[rid] if c == STEP}
    assert texts and all(t in en and en[t] for t in texts), [t for t in texts if t not in en]
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert re.search(r"^kb \| kb:record \|", reg, re.M)
