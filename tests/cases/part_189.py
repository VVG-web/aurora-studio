#!/usr/bin/env python3
"""part_189 — пакет требований истории (CR-15…CR-17): список, критерии, связи, пробелы."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from harness import SCRIPTS, make_project, run, test

sys.path.insert(0, str(SCRIPTS))
import code_pack as CP  # noqa: E402
import aurora_cockpit as CK  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


STORY = """---
title: "US-1.1 Приём заявки"
---

# US-1.1 Приём заявки

## Описание

Оператор принимает заявку.

## Критерии приёмки

### Приём

1. Система принимает заявку по POST /api/orders и создаёт её в статусе «принята».
2. Ошибка валидации — ответ 400 со списком полей.

### Событие

3. Событие о принятой заявке уходит в топик orders.accepted.
"""

AC = """---
title: "AC-1.1 Критерии приёмки"
covers: US-1.1
---

# AC-1.1

## Критерии приёмки

| Критерий | Как проверить |
|---|---|
| Дубль внешнего номера отклоняется с кодом DUPLICATE_ORDER | запрос с тем же external_id |
| Внешний номер уникален | два запроса с одним номером |
"""

ALG = """---
title: "ALG-12 Приём заявки"
covers: US-1.1
---

# ALG-12 Приём заявки

Алгоритм приёма.
"""

JIRA = """---
title: "Jira BENCH-1 US-1.1"
---

# BENCH-1 US-1.1

Задача трекера.
"""


@test
def test_code_pack_list_and_story(_t):
    """Список историй, пакет, критерии, связанные артефакты и пробелы."""
    root = make_project(_t)
    _write(root / "Sources/Confluence/US-1.1.md", STORY)
    _write(root / "Sources/JIRA/US-1.1.md", JIRA)
    _write(root / "Artifacts/ac/US-1.1-ac.md", AC)
    _write(root / "Artifacts/algorithms/ALG-12.md", ALG)
    listed = run("code_pack.py", "--project", str(root), "--list", cwd=root, expect_rc=0)
    assert "US-1.1" in listed.stdout and "источников 3" in listed.stdout, listed.stdout
    assert "критериев 5" in listed.stdout, listed.stdout
    data = json.loads(run("code_pack.py", "--project", str(root), "--story", "US-1.1",
                         "--json", cwd=root, expect_rc=0).stdout)
    assert data["found"] is True
    assert len(data["sources"]) == 3, data["sources"]
    assert len(data["criteria"]) == 5, data["criteria"]
    assert any(r["path"] == "Artifacts/algorithms/ALG-12.md" for r in data["related"]), data["related"]
    codes = {g["code"] for g in data["gaps"]}
    assert "no_criteria" not in codes and "no_jira" not in codes, codes
    md = run("code_pack.py", "--project", str(root), "--story", "US-1.1", cwd=root, expect_rc=0)
    assert "## Критерии приёмки" in md.stdout and "## Связанные артефакты" in md.stdout, md.stdout
    missing = run("code_pack.py", "--project", str(root), "--story", "US-9.9", cwd=root, expect_rc=1)
    assert "не найдена" in missing.stdout.lower(), missing.stdout
    out = root / "Artifacts/requirements/US-1.1.md"
    run("code_pack.py", "--project", str(root), "--story", "US-1.1", "--out",
        "Artifacts/requirements/US-1.1.md", cwd=root, expect_rc=0)
    assert out.is_file() and "## Критерии приёмки" in out.read_text(encoding="utf-8")
    api = CK.code_pack_state(str(root))
    assert api["stories"] and api["stories"][0]["story"] == "US-1.1", api
    one = CK.code_pack_state(str(root), "US-1.1")
    assert one["pack"]["found"] and len(one["pack"]["criteria"]) == 5, one["pack"]


@test
def test_code_pack_criteria_forms(_t):
    """Нумерованный список, таблица и Gherkin разбираются; id устойчив по тексту."""
    numbered = CP.parse_criteria(STORY, "US-1.1", "x.md")
    assert len(numbered) == 3 and numbered[0]["group"] == "Приём", numbered
    assert numbered[0]["id"].startswith("US-1.1/приём/1#"), numbered[0]
    table = CP.parse_criteria(AC, "US-1.1", "ac.md")
    assert len(table) == 2, table
    gherkin = CP.parse_criteria("## Критерии приёмки\n\nСценарий: заявка принята\nСценарий: дубль отклонён\n",
                                "US-1.1", "g.md")
    assert len(gherkin) == 2, gherkin
    assert gherkin[0]["group"] == "gherkin", gherkin[0]
