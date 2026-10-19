"""Проверки движка Aurora, часть 97: сервер панели отдаёт находки по длине путей вместе с записью.

Каркас и помощники — tests/harness.py. Запись «Настройки проекта» (форма `/api/setup`, текст
конфига `/api/config`, модули источников `/api/sources`) отвечает как раньше и добавляет
`path_findings`: строки конфига, которым на Windows, macOS или Linux не хватит места под путь.
Находка не блокирует запись — конфиг записан, а ответ называет строку; слова подписей страница
берёт из каталога языка, поэтому в ответе нет русских подписей. Сети и настоящего домашнего
каталога проверки не касаются.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import json
import os
import re
import sys

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401

LONG_OUT = "/".join(["папка"] * 40)          # 239 знаков: вместе с корнем-допущением Windows не создаётся


def _ck():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("aurora_cockpit")


def _long_project(tmp: Path) -> Path:
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + f"artifacts:\n  ac:\n    out: {LONG_OUT}\n", encoding="utf-8")
    return root


@test
def test_saving_the_project_form_answers_with_the_path_findings_and_still_saves(tmp: Path):
    """Форма записала настройки и вернула находки: строка названа, запись не заблокирована, подписей по-русски нет."""
    restore = set_home(tmp / "home")
    try:
        ck = _ck()
        root = _long_project(tmp)
        r = ck.Handler._run_setup(None, str(root), {"name": "Другое имя"})
        assert r.get("ok") and "error" not in r, r
        assert 'name: "Другое имя"' in (root / "aurora.config.yaml").read_text(encoding="utf-8"), "запись выполнена"
        found = r["path_findings"]
        assert found["lines"] == 1, found
        want = os.path.abspath(root) if os.name == "nt" else "C:\\Users\\username\\Documents\\Projects\\project"
        assert found["models"]["windows"] == want, found["models"]
        f = found["findings"][0]
        assert f["key"] == "artifacts:ac" and f["where"] == ["artifacts", "ac", "out"], f
        assert f["system"] == "windows" and f["limit"] == 247 and f["kind"] in ("dir", "prefix"), f
        assert f["length"] > 247 and f["cfg_line"] > 0 and f["root_len"] > 0, f
        assert "line" not in f and "source" not in f, "русские подписи страница берёт из каталога"
        assert not re.search("[А-Яа-яЁё]", json.dumps({k: v for k, v in f.items() if k not in ("sample", "root")},
                                                     ensure_ascii=False)), f
        (tmp / "второй").mkdir()
        clean = ck.Handler._run_setup(None, str(make_project(tmp / "второй")), {"name": "Чистый"})
        assert clean["ok"] and clean["path_findings"]["findings"] == [], clean
    finally:
        restore()


@test
def test_saving_the_config_text_answers_with_the_path_findings(tmp: Path):
    """Текст конфига записан как был, ответ несёт находки; повторное сохранение без правок — тоже."""
    restore = set_home(tmp / "home")
    try:
        ck = _ck()
        root = _long_project(tmp)
        cfg = root / "aurora.config.yaml"
        text = cfg.read_text(encoding="utf-8")
        first = ck.Handler._write_config(None, str(root), text + "\n# правка\n")
        assert first["ok"] and first["path_findings"]["lines"] == 1, first
        again = ck.Handler._write_config(None, str(root), text + "\n# правка\n")
        assert again.get("unchanged") and again["path_findings"]["lines"] == 1, again
        assert "# правка" in cfg.read_text(encoding="utf-8")
        short = ck.Handler._write_config(None, str(root), text.replace(LONG_OUT, "Artifacts/ac"))
        assert short["ok"] and short["path_findings"]["findings"] == [] and short["path_findings"]["lines"] == 0, short
    finally:
        restore()


@test
def test_a_failing_path_check_never_breaks_the_save(tmp: Path):
    """Проверка длины путей упала — запись всё равно ответила «ok», находок нет, отметка о сбое проверки есть."""
    restore = set_home(tmp / "home")
    ck = _ck()
    real = ck.PB.assess
    try:
        root = make_project(tmp)

        def boom(project):
            raise RuntimeError("сбой проверки")
        ck.PB.assess = boom
        r = ck.Handler._write_config(None, str(root), (root / "aurora.config.yaml").read_text(encoding="utf-8") + "# x\n")
        assert r["ok"] and r["path_findings"]["findings"] == [] and r["path_findings"]["failed"] is True, r
    finally:
        ck.PB.assess = real
        restore()


@test
def test_the_panel_has_a_read_only_route_for_the_path_check(tmp: Path):
    """`GET /api/paths` отдаёт проверку проекта без записи; страница обходит его в английском режиме."""
    src = (KIT / "cockpit" / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'u.path == "/api/paths"' in src and "path_findings(project)" in src
    crawl = (KIT / "tests" / "cases" / "part_16.py").read_text(encoding="utf-8")
    assert '"/api/paths"' in crawl, "новый путь обязан быть в обходе английского режима"
    restore = set_home(tmp / "home")
    try:
        ck = _ck()
        root = _long_project(tmp)
        before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
        got = ck.path_findings(str(root))
        assert got["findings"] and got["lines"] == 1
        assert before == sorted(str(p.relative_to(root)) for p in root.rglob("*")), "проверка ничего не пишет"
    finally:
        restore()
