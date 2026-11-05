#!/usr/bin/env python3
"""part_204 — Clean Room: модельный синтез с цитатами и publish-гейт (1.211.0)."""
from __future__ import annotations

import sys
from pathlib import Path

from harness import SCRIPTS, make_project, run, test

sys.path.insert(0, str(SCRIPTS))
import clean_room as CR  # noqa: E402
import code_fixtures as CF  # noqa: E402
import code_repos as CREPO  # noqa: E402
import publish_doc as PD  # noqa: E402

FACTS = [("GET /api/orders", "src/OrderController.java", 7),
         ("OrderController.list", "src/OrderController.java", 6)]

VALID = """## Что видно по коду

- Маршрут `GET /api/orders` ведёт в `OrderController.list` [c1].
- Метод возвращает строку [c2].

## Что не удалось установить по коду

- Причины решений и поведение вне индекса.
"""


def _ask(text: str):
    def ask(_messages, _deadline):
        return {"ok": True, "text": text, "error": ""}
    return ask


@test
def test_cleanroom_agent_requires_citations_and_gap(_t):
    """Синтез принимает только цитаты из фактов и непустой раздел «что не удалось»."""
    text, err = CR.synthesize("orders", "cr-module", "Module", FACTS, _ask(VALID))
    assert not err and "[c1]" in text and "## Что не удалось" in text, err
    for bad, why in ((VALID.replace("[c1]", "[c9]"), "вне списка"),
                     (VALID.replace("## Что не удалось установить по коду", "## Другое"), "раздела"),
                     (VALID.replace("[c1]", "").replace("[c2]", ""), "нет ссылок")):
        text, err = CR.synthesize("orders", "cr-module", "Module", FACTS, _ask(bad))
        assert not text and why in err, (bad, err)


@test
def test_cleanroom_agent_uses_model_with_publish_true(tmp: Path):
    """publish: true и models: true — документ cr-module собран моделью, цитаты на месте."""
    root = _project(tmp)
    _config(root, {"orders": {"url": CF.url_of(root / "Code" / "orders"), "branch": "main",
                              "provider": "generic", "publish": True, "models": True}})
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    data = CR.build(str(root), agent=True, ask=_ask(VALID))
    assert not data["blocked"], data["blocked"]
    doc = next(d for d in data["docs"] if d["kind"] == "cr-module")
    assert doc["agent"]["ok"] and "[c1]" in doc["text"], doc
    assert "## Источники" in doc["text"] and "[c2]" in doc["text"]


@test
def test_cleanroom_agent_refuses_publish_false(tmp: Path):
    """Без publish: true модельный документ не публикуется, код выхода 1."""
    root = _project(tmp)
    _config(root, {"orders": {"url": CF.url_of(root / "Code" / "orders"), "branch": "main",
                              "provider": "generic"}})
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    res = run("clean_room.py", "--project", str(root), "--agent", "--apply", cwd=root, expect_rc=1)
    assert "publish: true" in res.stdout + res.stderr, res.stdout + res.stderr
    assert not (root / "CleanRoom/cr-module/orders.md").exists()


@test
def test_publish_doc_requires_repo_publish_true(tmp: Path):
    """ship:publish отказывает Clean Room документу без code_repos.<repo>.publish: true."""
    root = make_project(tmp)
    _config(root, {"orders": {"url": "https://example.invalid/orders.git", "branch": "main",
                              "provider": "generic"}})
    ok, why = PD.code_publish_allowed(str(root), "orders")
    assert not ok and "publish: false" in why, why
    _config(root, {"orders": {"url": "https://example.invalid/orders.git", "branch": "main",
                              "provider": "generic", "publish": True}})
    assert PD.code_publish_allowed(str(root), "orders") == (True, "")


def _project(tmp: Path) -> Path:
    root = make_project(tmp)
    repo = CF.init(root / "Code" / "orders")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00", files={
        "src/OrderController.java": (
            "package demo;\n"
            "@RestController\n"
            "@RequestMapping(\"/api\")\n"
            "public class OrderController {\n"
            "  @GetMapping(\"/orders\")\n"
            "  public String list() {\n"
            "    return \"ok\";\n"
            "  }\n"
            "}\n")})
    return root


def _config(root: Path, repos: dict) -> None:
    path = root / "aurora.config.yaml"
    path.write_text(CREPO.write_block(path.read_text(encoding="utf-8"), repos), encoding="utf-8")
