"""Проверки движка Aurora, часть 146. Каркас и помощники — tests/harness.py.

Выпуск A «Confluence»: общий выбор вида документа для скрипта публикации и панели, точная запись секции `artifacts:`
конфига, и справка — вики, навыки, реестр команд и каталоги говорят одно и то же о создании (шаблон) и публикации
(родитель и промпт). Сеть и модель не используются; проекты — только во временных папках.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, test


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


# ------------------------------------------------------------------ вид документа: одно правило на скрипт и панель

@test
def test_kind_of_takes_the_type_first_then_the_deepest_result_folder(tmp: Path):
    """`make_kinds.kind_of`: `type:` шапки важнее папки; из вложенных папок побеждает самая глубокая; нет вида — пусто.
    Скрипт публикации и состояние кнопки в панели берут вид этим же кодом."""
    MK = _mod("make_kinds")
    root = make_project(tmp)
    kinds = {"all": {"out": "Artifacts"}, "us": {"out": "Artifacts/us"}, "opz": {"out": "Deliverables/work/"}}
    for rel in ("Artifacts/us/US-1.md", "Artifacts/other/x.md", "Deliverables/work/ОПЗ.md", "Workspaces/черновик.md"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x", encoding="utf-8")

    def kind(rel: str, type_hint: str = "") -> str:
        return MK.kind_of(kinds, type_hint, str(root / rel), str(root))[0]

    assert kind("Artifacts/us/US-1.md") == "us", "вложенная папка не победила"
    assert kind("Artifacts/other/x.md") == "all"
    assert kind("Deliverables/work/ОПЗ.md") == "opz", "слэш в конце out сломал сопоставление"
    assert kind("Workspaces/черновик.md") == "", "файл вне папок видов получил вид"
    assert kind("Artifacts/us/US-1.md", "all") == "all", "type: шапки не важнее папки"
    assert kind("Artifacts/us/US-1.md", '"ALL"') == "all", "type: в кавычках и другим регистром не распознан"
    assert kind("Artifacts/us/US-1.md", "нет-такого") == "us", "неизвестный type: спрятал папку"
    assert MK.kind_of(kinds, "", "", "")[0] == "", "без шапки и пути вид нашёлся"
    assert kinds["us"] is MK.kind_of(kinds, "us")[1]
    # папка-соседка с тем же началом имени вид не получает: `Artifacts/us-old` — не `Artifacts/us`
    (root / "Artifacts/us-old").mkdir()
    (root / "Artifacts/us-old/y.md").write_text("y", encoding="utf-8")
    assert kind("Artifacts/us-old/y.md") == "all"
    # скрипт публикации и панель берут вид этим кодом, а не своими копиями правила
    assert "MK.kind_of(" in (SCRIPTS / "publish_doc.py").read_text(encoding="utf-8")
    assert "AG_KINDS.kind_of(" in (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")


@test
def test_write_kinds_block_is_exact_and_idempotent(tmp: Path):
    """`make_kinds.write_kinds_block`: секция заменяется, остальное — байт в байт (BOM, CRLF, пустые строки, комментарии
    вне секции); секции нет — дописывается в стиле файла; повторная запись текста не меняет."""
    MK = _mod("make_kinds")
    kinds = {"ac": {"title": "AC", "template": "T/a.md", "out": "Artifacts/ac", "publish_prompt": "Prompts/p.md",
                    "task": {"project": "PRJ", "labels": ["a", "b"]}}}
    plain = 'project:\n  name: "Т"\n\nartifacts:\n  old:\n    title: "старый"\n\n# дальше\ncards:\n  x: 1\n'
    new = MK.write_kinds_block(plain, kinds)
    assert new.startswith('project:\n  name: "Т"\n\nartifacts:\n  ac:\n') and "старый" not in new
    assert new.endswith('\n\n# дальше\ncards:\n  x: 1\n'), f"хвост после секции изменён:\n{new}"
    assert MK.write_kinds_block(new, kinds) == new, "повторная запись изменила текст"
    for variant in (new.replace("\n", "\r\n"), "﻿" + new, "﻿" + new.replace("\n", "\r\n")):
        assert MK.write_kinds_block(variant, kinds) == variant, "BOM или CRLF потеряны/искажены"
    # читается обратно движком
    back = MK.write_kinds_block("", kinds)
    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text(new, encoding="utf-8")
    got = MK.read_kinds(str(root))["ac"]
    assert got["publish_prompt"] == "Prompts/p.md" and got["task"] == {"project": "PRJ", "labels": ["a", "b"]}, got
    assert back.startswith("artifacts:\n  ac:\n") and back.endswith("\n"), back
    # секции нет: дописана в конце, в стиле последней строки (CRLF)
    crlf = MK.write_kinds_block('project:\r\n  name: "Т"\r\n', kinds)
    assert crlf.startswith('project:\r\n  name: "Т"\r\n\r\nartifacts:\r\n') and "\n" not in crlf.replace("\r\n", ""), repr(crlf)
    # пустой реестр: секция остаётся, но пустая
    assert MK.write_kinds_block(new, {}).rstrip().endswith("cards:\n  x: 1") and "ac:" not in MK.write_kinds_block(new, {})


# ------------------------------------------------------------------ справка и каталоги

@test
def test_docs_and_catalogs_say_the_same_about_creation_and_publication(tmp: Path):
    """Вики (ru и en), навыки, реестр команд и каталоги называют промпт создания и промпт публикации, штамп `published_sha`
    и честно говорят, что модель по промпту публикации пока не вызывается."""
    ru = " ".join((KIT / "wiki/ru/Artifacts-and-Deliverables.md").read_text(encoding="utf-8").split())     # перенос строки — не повод для отказа
    en = " ".join((KIT / "wiki/Artifacts-and-Deliverables.md").read_text(encoding="utf-8").split())
    for text, parts in ((ru, ("`publish_prompt`", "`publish_url`", "`published_sha`", "Настройки проекта → Артефакты",
                              "модель по нему не вызывается", "Взять с диска", "Создание — шаблон", "родитель и промпт")),
                        (en, ("`publish_prompt`", "`publish_url`", "`published_sha`", "Project settings → Artifacts",
                              "no model runs by it", "Take the disk's", "Creation is the template", "the parent and the prompt"))):
        for part in parts:
            assert part in text, f"в вики нет «{part}»"
    for rel in ("skills/aurora-vault/references/workflows.md", "skills/aurora-vault/references/frontmatter.md"):
        assert "published_sha" in (KIT / rel).read_text(encoding="utf-8"), rel
    assert "publish_url" in (KIT / "skills/aurora-vault/references/workflows.md").read_text(encoding="utf-8")
    # описание `make:kinds` одно во всех местах, где оно есть
    row = next(ln for ln in (KIT / "commands.txt").read_text(encoding="utf-8").splitlines() if ln.startswith("make | make:kinds |"))
    assert "промпт создания, родитель и промпт публикации" in row, row
    assert "промпт создания, родитель и промпт публикации" in (KIT / "docs/commands.md").read_text(encoding="utf-8")
    assert "промпт создания, родитель и промпт публикации" in (KIT / "skills/aurora-vault/SKILL.md").read_text(encoding="utf-8")
    assert "creation prompt, parent and publishing prompt" in (KIT / "docs/en/commands.md").read_text(encoding="utf-8")
    data = json.loads((KIT / "cockpit/i18n/data/en.json").read_text(encoding="utf-8"))
    assert "creation prompt, parent and publishing prompt" in data["commands"]["make:kinds"]
    # новые сообщения сервера переведены (без них английская страница видела бы русский отказ)
    for msg in ("Опубликовать некуда: у вида этого документа не задана родительская страница в Confluence.",
                "Опубликовать нельзя: в настройках проекта не заданы адрес Confluence и пространство.",
                "виды артефактов изменились на диске с момента открытия"):
        assert data["messages"].get(msg), f"нет перевода сообщения: {msg}"
    # английские каталоги не содержат кириллицы в новых надписях формы и «Файлов»
    en_cat = json.loads((KIT / "cockpit/i18n/en.json").read_text(encoding="utf-8"))
    ru_cat = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    fresh = [k for k in en_cat if k.startswith(("kinds.l_", "kinds.frame_", "editor.gate_", "editor.publish_fix", "editor.stale_unknown"))] + [
        "kinds.about", "kinds.publish_prompt_hint", "kinds.f_publish_prompt", "kinds.drift", "kinds.drift_help", "kinds.take_disk",
        "kinds.keep_mine", "editor.publish_gate", "editor.published_dirty"]
    for key in fresh:
        assert key in ru_cat and key in en_cat, f"надпись {key} есть не в обоих каталогах"
        assert not re.search("[а-яА-ЯёЁ]", en_cat[key]), f"русский текст в английском каталоге: {key}: {en_cat[key]}"
