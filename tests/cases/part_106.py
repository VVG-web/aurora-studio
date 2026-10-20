"""Проверки движка Aurora, часть 106. Каркас и помощники — tests/harness.py.

`kb:ingest-office` в маршрутах панели, в итоге прогона и в `kb:doctor`: шаг стоит перед разбором во всех
трёх маршрутах, файлы без текста запоминаются, строка итога «вложения · транскрипты · знаки» одинакова на
двух языках, доктор называет файлы без транскрипта и транскрипты низкого качества. Нейтральные данные
(PRJ-A, «Страница верхнего уровня»); сети и модели нет.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
from pathlib import Path

from docfix import docx, para
from harness import KIT, SCRIPTS, make_project, run, test  # noqa: F401

PROSE = ("Подсистема-1 принимает заявки PRJ-A от заказчика и проверяет их по правилам, описанным в разделе 3. "
         "Если проверка прошла, заявка получает статус «принята», иначе возвращается автору с причиной отказа. ")
ASSETS = "Sources/Confluence/Страница верхнего уровня_assets"
OUT = "Raw/Extracted/Confluence/Страница верхнего уровня"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _routes() -> dict:
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    return {r["id"]: r for r in ck.all_scenarios()}


@test
def test_the_ingest_step_stands_before_parsing_in_update_fix_and_rebuild(tmp: Path):
    routes = _routes()
    for rid in ("update", "fix", "rebuild"):
        cmds = [s.get("cmd", "") for s in routes[rid]["steps"] if not s.get("manual")]
        assert cmds.count("kb:ingest-office") == 1, (rid, cmds)
    update = [s.get("cmd", "") for s in routes["update"]["steps"]]
    assert update.index("kb:ingest-office") < update.index("agent:build"), "перевод в текст стоит после разбора"
    rebuild = [s.get("cmd", "") for s in routes["rebuild"]["steps"]]
    assert rebuild.index("kb:ingest-office") < rebuild.index("kb:reset") < rebuild.index("agent:build"), \
        "перевод должен идти до сноса базы: упавшая конвертация не оставит пустую базу"
    fix = routes["fix"]["steps"]
    first = next(i for i, s in enumerate(fix) if s.get("cmd") == "kb:ingest-office")
    unparsed = next(i for i, s in enumerate(fix) if "--unparsed" in (s.get("flags") or []))
    assert first == 0 and first < unparsed, "заготовку-указатель решают по транскриптам: они нужны до `--unparsed`"
    texts = {s["why"] for rid in ("update", "fix", "rebuild") for s in routes[rid]["steps"]
             if s.get("cmd") == "kb:ingest-office"}
    assert len(texts) == 1 and "вложения страниц" in next(iter(texts)), texts


@test
def test_a_file_without_text_is_remembered_so_repeat_runs_and_the_doctor_leave_it_alone(tmp: Path):
    OI = _mod("office_ingest")
    root = make_project(tmp)
    page = root / "Sources/Confluence/Страница верхнего уровня.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntitle: Страница\n---\n\n" + PROSE * 3, encoding="utf-8")
    docx(root / ASSETS / "иконка.docx", para("Лого"))
    docx(root / ASSETS / "спецификация.docx", para(PROSE * 3))
    cp = run("office_ingest.py", cwd=root, expect_rc=0)
    assert "Без транскрипта (текста слишком мало" in cp.stdout, cp.stdout
    memo = json.loads((root / "Raw/Extracted/.skipped.json").read_text(encoding="utf-8"))
    assert list(memo) == [f"{ASSETS}/иконка.docx"], memo
    stamp = (root / "Raw/Extracted/.skipped.json").stat().st_mtime_ns
    cp = run("office_ingest.py", cwd=root, expect_rc=0)
    assert "собрано: 0 · пропущено (не изменились): 1" in cp.stdout and "Без транскрипта (текста слишком мало" in cp.stdout
    assert (root / "Raw/Extracted/.skipped.json").stat().st_mtime_ns == stamp, "повторный запуск переписал память"
    os.chdir(root)
    assert OI.inventory()["loose"] == [], "известный пустой файл назван «без транскрипта»"
    doc = run("aurora_doctor.py", cwd=root, expect_rc=None).stdout
    assert "без транскрипта" not in doc, doc
    # файл изменился — пробуется снова: теперь в нём есть текст, транскрипт появился, запись ушла
    docx(root / ASSETS / "иконка.docx", para(PROSE * 3))
    run("office_ingest.py", cwd=root, expect_rc=0)
    assert (root / OUT / "иконка.docx.md").exists() and not (root / "Raw/Extracted/.skipped.json").exists()
    # --force пробует и известные пустые
    docx(root / ASSETS / "иконка.docx", para("Лого"))
    run("office_ingest.py", cwd=root, expect_rc=0)
    assert not (root / OUT / "иконка.docx.md").exists(), "транскрипт устаревшего содержимого остался"
    # вложение исчезло (зеркало на месте) — запись о нём уходит
    (root / ASSETS / "иконка.docx").unlink()
    run("office_ingest.py", cwd=root, expect_rc=0)
    assert not (root / "Raw/Extracted/.skipped.json").exists(), "запись об исчезнувшем файле осталась"


@test
def test_run_summary_counts_attachments_and_renders_the_line_in_both_languages(tmp: Path):
    RS = _mod("run_summary")
    s = RS.empty()
    s.update(attachments=7, transcripts=5, transcript_chars=6683, low_quality=1)
    other = RS.empty()
    other["docs_done"] = 3
    merged = RS.merge([s, other])
    assert (merged["attachments"], merged["transcripts"], merged["transcript_chars"], merged["low_quality"]) == \
        (7, 5, 6683, 1), "счётчики вложений потерялись при сложении итогов шагов"
    ru = "\n".join(RS.render(merged))
    en = "\n".join(RS.render(merged, title="Run summary", lang="en"))
    assert "Вложения: найдено 7 · транскриптов 5 · знаков 6" in ru and "низкого качества 1" in ru, ru
    assert "Attachments: found 7 · transcripts 5 · characters 6" in en and "low quality 1" in en, en
    assert not re.search("[А-Яа-я]", en), "в английском итоге осталась кириллица: " + en
    quiet = "\n".join(RS.render(RS.empty()))
    assert "Вложения" not in quiet, "строка вложений выводится, когда их нет"


@test
def test_doctor_names_files_without_a_transcript_and_transcripts_of_low_quality(tmp: Path):
    root = make_project(tmp)
    docx(root / "Raw/customer/Регламент.docx", para(PROSE * 2))
    docx(root / "Raw/customer/Мусор.docx", para("l1|i!ii ,,.. ;;; ### ~~~ ^^^ ¦¦¦ ░░░ ▒▒▒ ▓▓▓ ///"))
    run("office_ingest.py", str(root / "Raw/customer/Мусор.docx"), cwd=root, expect_rc=0)
    out = run("aurora_doctor.py", cwd=root, expect_rc=None).stdout
    assert re.search(r"WARN: без транскрипта: 1 \(Raw/customer/Регламент\.docx\)", out), out
    assert re.search(r"WARN: транскриптов низкого качества: 1 \(Raw/customer/Мусор\.md\)", out), out
    run("office_ingest.py", cwd=root, expect_rc=0)
    out = run("aurora_doctor.py", cwd=root, expect_rc=None).stdout
    assert "без транскрипта" not in out, out
    assert "WARN: транскриптов низкого качества: 1" in out, "мусорный транскрипт перестал называться"
    # вложения и транскрипты есть — доктор даёт сводную строку
    page = root / "Sources/Confluence/Страница верхнего уровня.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntitle: Страница\n---\n\n" + PROSE * 3, encoding="utf-8")
    docx(root / ASSETS / "спецификация.docx", para(PROSE * 3))
    run("office_ingest.py", cwd=root, expect_rc=0)
    out = run("aurora_doctor.py", cwd=root, expect_rc=None).stdout
    assert re.search(r"вложения: 1 · транскриптов: 1 · знаков: \d+ · перевод в текст: kb:ingest-office", out), out
