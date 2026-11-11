#!/usr/bin/env python3
"""part_206 — настройки артефактов Code в проекте.

Проверяем проектный слой `code_artifacts:`: путь по умолчанию ArtifactsCode/<вид>,
переопределение пути/шаблона/включённости, сохранение соседних секций конфига и 409
на устаревший слепок.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from harness import KIT, make_project, test

sys.path.insert(0, str(KIT / "scripts"))
import code_artifacts as CA  # noqa: E402
sys.path.insert(0, str(KIT / "cockpit"))
import aurora_cockpit as CK  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _template(kind: str, out: str = "") -> str:
    out_line = f"out: {out}\n" if out else ""
    return (f"---\nkind: {kind}\ntitle: \"{kind}\"\nversion: \"1.0\"\n"
            f"enabled: true\nscope: module\n{out_line}prompt: PromptCode/{kind}.md\n"
            "purity: traceable\norder: 200\n---\n\n"
            f"# {kind}\n\n## Что не удалось установить по коду\n")


@test
def test_code_artifact_overrides_round_trip_and_default(_t):
    root = make_project(_t)
    _write(root / "PromptCode" / "my-doc.md", "Свой промпт.\n")
    _write(root / "TemplatesCode" / "my-doc_v1.0.md", _template("my-doc"))
    _write(root / "aurora.config.yaml", (root / "aurora.config.yaml").read_text(encoding="utf-8")
           + "\n# соседняя секция\nother: keep\n")

    rows, problems = CA.check(root)
    assert rows["my-doc"]["out"] == "ArtifactsCode/my-doc", rows["my-doc"]
    assert rows["my-doc"]["enabled"] is True, rows["my-doc"]
    assert not [p for p in problems if p["kind"] == "my-doc"], problems

    digest = CA.overrides_digest(root)
    res = CA.save(root, {"my-doc": {"out": "ArtifactsCode/custom",
                                    "template": "TemplatesCode/my-doc_v1.0.md",
                                    "enabled": False}}, digest)
    assert res.get("ok") is True, res
    raw = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    assert "other: keep" in raw and "atlassian:" in raw and "project:" in raw, raw
    assert CA.read_overrides(root)["my-doc"]["enabled"] is False
    rows, problems = CA.check(root)
    assert rows["my-doc"]["out"] == "ArtifactsCode/custom", rows["my-doc"]
    assert rows["my-doc"]["enabled"] is False, rows["my-doc"]
    assert not [p for p in problems if p["kind"] == "my-doc"], problems


@test
def test_code_artifact_overrides_refuse_outside_paths_and_stale_digest(_t):
    root = make_project(_t)
    _write(root / "PromptCode" / "my-doc.md", "Свой промпт.\n")
    _write(root / "TemplatesCode" / "my-doc_v1.0.md", _template("my-doc"))
    bad = CA.save(root, {"my-doc": {"out": "Outside/x"}}, "")
    assert bad.get("ok") is False and bad.get("code") == "out", bad
    bad = CA.save(root, {"my-doc": {"template": "../outside.md"}}, "")
    assert bad.get("ok") is False and bad.get("code") == "template", bad
    stale = CA.save(root, {"my-doc": {"out": "ArtifactsCode/x"}}, "stale")
    assert stale.get("ok") is False and stale.get("conflict") is True, stale
    assert CA.read_overrides(root) == {}, CA.read_overrides(root)


@test
def test_code_artifact_partial_save_keeps_other_overrides(_t):
    root = make_project(_t)
    assert CA.out_problem("ArtifactsCode") == "" and CA.out_problem("ArtifactsCode/") == ""
    assert "внутри" in CA.out_problem("Other/x")
    _write(root / "PromptCode" / "my-doc.md", "Свой промпт.\n")
    _write(root / "TemplatesCode" / "my-doc_v1.0.md", _template("my-doc"))
    first = CA.save(root, {"my-doc": {"out": "ArtifactsCode/custom", "enabled": False}},
                    CA.overrides_digest(root))
    assert first.get("ok") is True, first
    second = CA.save(root, {"my-doc": {"enabled": True}}, CA.overrides_digest(root))
    assert second.get("ok") is True, second
    assert CA.read_overrides(root)["my-doc"] == {"enabled": True,
                                                 "out": "ArtifactsCode/custom"}, CA.read_overrides(root)
    third = CA.save(root, {"my-doc": {"out": ""}}, CA.overrides_digest(root))
    assert third.get("ok") is True, third
    assert "out" not in CA.read_overrides(root)["my-doc"], CA.read_overrides(root)


@test
def test_code_state_fallback_stops_polling_on_full_failure(tmp: Path):
    """Сбой фонового расчёта не оставляет панель в вечном опросе.

    `_code_state_bg` раньше глотала исключение: кэш оставался пустым, `code_state`
    снова отдавал `loading: true`, и панель опрашивала сломанный проект без конца.
    Теперь ошибка кэшируется как готовое состояние с `loading: false` и причиной.
    """
    root = make_project(tmp)
    key = str(root)
    with CK._CODE_STATE_LOCK:
        CK._CODE_STATE_CACHE.pop(key, None)
        CK._CODE_STATE_BUSY.pop(key, None)
    saved = CK._code_state_full

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    CK._code_state_full = boom
    try:
        CK._code_state_bg(key)
    finally:
        CK._code_state_full = saved
    entry = CK._CODE_STATE_CACHE.get(key)
    assert entry and entry["state"]["loading"] is False, entry
    assert "RuntimeError" in entry["state"]["failed"], entry
    st = CK.code_state(key)
    assert st["loading"] is False and "RuntimeError" in st["failed"], st


@test
def test_code_registry_save_reloads_async_state(_t):
    """Сохранение реестра не оставляет карточки Code пустыми.

    С 1.212.2 `code_state` отдаёт форму сразу с `loading: true`, а artifacts/index/profiles
    считает в фоне. `save()` брала этот быстрый ответ и не запускала опрос, поэтому после
    сохранения репозитория карточки артефактов и индекса оставались пустыми до ручного
    обновления. Повторный `load()` возобновляет опрос.
    """
    source = (KIT / "cockpit/modules/code/view.js").read_text(encoding="utf-8")
    save = source.split("async function save(ctx, force){", 1)[1].split("\n}\n", 1)[0]
    assert "r.state.loading" in save and "load(ctx, true)" in save, save


@test
def test_code_artifact_ui_uses_the_settings_endpoint(_t):
    source = (KIT / "cockpit/modules/code/view.js").read_text(encoding="utf-8")
    for token in ("/api/code/artifacts", "saveArtifacts", "code.artifacts.save",
                  "code.artifacts.conflict", "r.conflict"):
        assert token in source, token
    for lang in ("ru", "en"):
        data = (KIT / "cockpit/modules/code/i18n" / f"{lang}.json").read_text(encoding="utf-8")
        assert '"code.artifacts.save"' in data and '"code.artifacts.conflict"' in data, lang


@test
def test_code_artifact_save_invalidates_the_full_state_cache(_t):
    """Сохранение галочки не должно возвращать старый кэш раздела Code.

    Раньше `code_artifacts_save` писала конфигурацию, но не сбрасывала
    `_CODE_STATE_CACHE`: следующая отрисовка брала старые `kinds`, и галочка
    выглядела «не сохранённой». Проверяем и сброс, и смену поколения, чтобы
    старый фоновый расчёт не вернул прежнее состояние.
    """
    root = make_project(_t)
    key = str(root)
    _write(root / "PromptCode" / "my-doc.md", "Свой промпт.\n")
    _write(root / "TemplatesCode" / "my-doc_v1.0.md", _template("my-doc"))
    state = CK.code_artifacts_state(key, "ru")
    with CK._CODE_STATE_LOCK:
        CK._CODE_STATE_CACHE[key] = {"state": {"artifacts": state}, "at": time.time()}
        CK._CODE_STATE_GEN[key] = 5
    res, status = CK.code_artifacts_save(
        key, {"my-doc": {"enabled": False}}, state["digest"], "ru")
    assert status == 200 and res.get("ok") is True, (status, res)
    with CK._CODE_STATE_LOCK:
        assert key not in CK._CODE_STATE_CACHE, "старый кэш Code пережил сохранение"
        assert CK._CODE_STATE_GEN[key] == 6, "поколение кэша не сменилось"
    kinds = {row["kind"]: row for row in res["state"]["kinds"]}
    assert kinds["my-doc"]["enabled"] is False, kinds["my-doc"]


@test
def test_code_artifacts_api_returns_state_and_stale_conflict(_t):
    root = make_project(_t)
    _write(root / "PromptCode" / "my-doc.md", "Свой промпт.\n")
    _write(root / "TemplatesCode" / "my-doc_v1.0.md", _template("my-doc"))
    state = CK.code_artifacts_state(str(root), "ru")
    assert state.get("digest") and state.get("kinds"), state
    res, status = CK.code_artifacts_save(
        str(root), {"my-doc": {"out": "ArtifactsCode/api"}}, state["digest"], "ru")
    assert status == 200 and res.get("ok") is True, (status, res)
    assert res["state"]["kinds"][-1]["out"] == "ArtifactsCode/api" or any(
        r["kind"] == "my-doc" and r["out"] == "ArtifactsCode/api"
        for r in res["state"]["kinds"]), res["state"]["kinds"]
    stale, status = CK.code_artifacts_save(
        str(root), {"my-doc": {"out": "ArtifactsCode/other"}}, "stale", "ru")
    assert status == 409 and stale.get("conflict") is True, (status, stale)


@test
def test_code_artifact_folder_is_in_the_project_schema_with_a_guide(_t):
    schema = (KIT / "structure_dirs.txt").read_text(encoding="utf-8").splitlines()
    assert "ArtifactsCode" in [line.strip() for line in schema if line.strip()], "нет ArtifactsCode"
    guide = (KIT / "templates/folder_guides.md").read_text(encoding="utf-8")
    assert "## ArtifactsCode" in guide, "нет описания ArtifactsCode"
    block = guide.split("## ArtifactsCode", 1)[1].split("\n## ", 1)[0]
    for field in ("title:", "what:", "put:", "not:", "who:", "nested:"):
        assert field in block, field
