#!/usr/bin/env python3
"""part_229 — Clean Room: содержательные документы, Swift, снимок исходников и структура."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, run, test

sys.path.insert(0, str(SCRIPTS))
import code_fixtures as CF  # noqa: E402
import code_repos as CR  # noqa: E402
import clean_room as CLEAN  # noqa: E402
import code_artifacts as CA  # noqa: E402


def _config(root: Path, repos: dict) -> None:
    path = root / "aurora.config.yaml"
    path.write_text(CR.write_block(path.read_text(encoding="utf-8"), repos), encoding="utf-8")


def _swift_project(tmp: Path) -> Path:
    root = make_project(tmp)
    repo = CF.init(root / "Code" / "orders")
    CF.commit(repo, "init", "2026-01-01T00:00:00+00:00", files={
        "Package.swift": "// swift-tools-version: 5.9\n",
        "Sources/Orders/Order.swift": (
            "import Foundation\n\n"
            "public struct Order: Codable {\n"
            "    public let id: String\n"
            "    public let amount: Double\n"
            "    public init(id: String, amount: Double) { self.id = id; self.amount = amount }\n"
            "    public func total() -> Double { amount }\n"
            "}\n"),
        "Sources/Orders/OrderView.swift": (
            "import SwiftUI\n\n"
            "public struct OrderView: View {\n"
            "    @State public var query = \"\"\n"
            "    public var body: some View { TextField(\"id\", text: $query) }\n"
            "}\n"),
        "README.md": "# Orders\n\nOrder service for the app.\n",
    })
    _config(root, {"orders": {"url": CF.url_of(repo), "branch": "main", "provider": "generic"}})
    return root


@test
def test_clean_room_uses_swift_and_writes_full_source(tmp: Path):
    root = _swift_project(tmp)
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    run("clean_room.py", "--project", str(root), "--apply", cwd=root)
    module = (root / "CleanRoom/cr-module/orders.md").read_text(encoding="utf-8")
    assert "`Order`" in module and "public struct Order" in module, module[:3000]
    assert "OrderView" in module and "Индекс не дал фактов" not in module, module[:2000]
    source = root / "CleanRoom/cr-source/orders/Sources-Orders.md"
    assert source.is_file(), list((root / "CleanRoom/cr-source/orders").glob("*"))
    text = source.read_text(encoding="utf-8")
    manifest = root / "CleanRoom/cr-source/orders/manifest.md"
    assert manifest.is_file(), "нет манифеста полноты снимка"
    assert "В снимке" in manifest.read_text(encoding="utf-8")
    assert "public struct Order" in text and "public let amount: Double" in text, text[:2000]
    assert "## Сборка и запуск" in module and "swift build" in module, module[-3000:]
    assert "README.md" in module, "документация проекта не попала в состав"


@test
def test_clean_room_without_api_explains_why_and_keeps_build(tmp: Path):
    root = _swift_project(tmp)
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    run("clean_room.py", "--project", str(root), "--apply", cwd=root)
    api = (root / "CleanRoom/cr-api/orders.md").read_text(encoding="utf-8")
    assert "HTTP-маршрутов в индексе нет" in api, api[:2500]
    assert "swift build" in api, "в API-документе нет команд сборки"


@test
def test_clean_room_writes_handoff_artifacts_and_honors_enabled(tmp: Path):
    """Набор для передачи команде: продукт, US/UC/AC, UI/GUI, JSON, словарь, NFR,
    план тестов, релиз и трассировка. Выключенный вид не попадает в паспорт."""
    root = _swift_project(tmp)
    run("code_index.py", "--project", str(root), "--index", "--apply", cwd=root)
    run("clean_room.py", "--project", str(root), "--apply", cwd=root)
    passport = json.loads((root / "CleanRoom/meta/passport.json").read_text(encoding="utf-8"))
    kinds = {doc["kind"] for doc in passport["docs"]}
    expected = {"cr-product", "cr-us", "cr-uc", "cr-ac", "cr-ui", "cr-gui", "cr-json",
                "cr-dictionary", "cr-nfr", "cr-test-plan", "cr-release", "cr-trace"}
    assert expected <= kinds, sorted(expected - kinds)
    for kind in sorted(expected):
        doc = root / "CleanRoom" / kind / "orders.md"
        assert doc.is_file(), kind
        text = doc.read_text(encoding="utf-8")
        assert "Что не удалось установить по коду" in text, kind
    disabled = CA.save(root, {"cr-us": {"enabled": False}}, CA.overrides_digest(root))
    assert disabled.get("ok") is True, disabled
    run("clean_room.py", "--project", str(root), "--apply", cwd=root)
    passport = json.loads((root / "CleanRoom/meta/passport.json").read_text(encoding="utf-8"))
    kinds = {doc["kind"] for doc in passport["docs"]}
    assert "cr-us" not in kinds, "выключенный cr-us всё ещё в паспорте"


@test
def test_clean_room_folders_are_allowed_by_structure_check(tmp: Path):
    root = _swift_project(tmp)
    (root / ".aurora" / "kit_path.txt").write_text(str(KIT), encoding="utf-8")
    _config(root, {"orders": {"url": CF.url_of(root / "Code" / "orders"), "branch": "main",
                              "provider": "generic"}})
    (root / "CleanRoom" / "cr-api").mkdir(parents=True, exist_ok=True)
    (root / "CleanRoom" / "meta").mkdir(parents=True, exist_ok=True)
    (root / "CleanRoom" / "cr-api" / "orders.md").write_text("x", encoding="utf-8")
    (root / "CleanRoom" / "meta" / "passport.json").write_text("{}", encoding="utf-8")
    res = run("aurora_doctor.py", "--structure", cwd=root)
    assert "структурные папки вне схемы" not in res.stdout, res.stdout
    assert "CleanRoom/meta" in (KIT / "structure_dirs.txt").read_text(encoding="utf-8")
