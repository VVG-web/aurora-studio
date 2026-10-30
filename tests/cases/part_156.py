"""Проверки движка Aurora, часть 156. Каркас и помощники — tests/harness.py.

Раздел панели «Код» (CR-06): папка `cockpit/modules/code/` по контракту разделов — манифест, разметка, скрипт с
`lifecycle = 1`, надписи только в каталогах; раздел зовёт только существующие команды и эндпоинты сервера, секрет
показывает маской, а ядро о разделе по имени не узнаёт больше прежнего (`ext_footprint`). Модель и сеть не используются.
"""
from __future__ import annotations

import importlib
import json
import re
import sys

from harness import KIT, test

MOD = KIT / "cockpit/modules/code"


def _cockpit():
    sys.path.insert(0, str(KIT / "cockpit"))
    return importlib.import_module("aurora_cockpit")


@test
def test_code_section_is_a_module_folder_next_to_git_with_its_commands(_t):
    """Манифест: группа проекта рядом с «Git», нужен проект, команды — из реестра, версия ядра — как у соседей; раздел
    виден в меню (не `dev`): репозитории подключаются только формой, а форма в скрытом разделе — правка файла руками."""
    ck = _cockpit()
    m = next((x for x in ck.modules() if x["id"] == "code"), None)
    assert m and not m.get("error"), m
    git = next(x for x in ck.modules() if x["id"] == "gitsync")
    assert m["group"] == "project" and m["order"] == git["order"] + 1 and m["needs"] == ["project"], m
    assert m["commands"] == ["code:sync", "code:status"], m["commands"]
    assert m["for"] == git["for"], "версия ядра раздела разошлась с соседями"
    raw = json.loads((MOD / "module.json").read_text(encoding="utf-8"))
    assert not raw.get("dev"), "раздел спрятан в «Разработку»: реестр кода нечем править, кроме файла"
    rows = {r["cmd"]: r for r in ck.registry()}
    for flag in ("--apply", "--repo", "--reset"):
        assert flag in rows["code:sync"]["flags"], f"кнопки раздела шлют {flag}, а команда его не объявила"


@test
def test_code_section_calls_only_server_endpoints_that_exist_and_hides_secrets(_t):
    """Скрипт раздела ходит только в эндпоинты, которые сервер панели обслуживает; ленивый (`lifecycle = 1`); поле
    токена — `password`, сохранённый токен приходит маской и маской же уходит, если его не трогали."""
    js = (MOD / "view.js").read_text(encoding="utf-8")
    ck_src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    called = set(re.findall(r'"(/api/[\w/]+)', js))
    assert called == {"/api/code", "/api/code/parse", "/api/code/check", "/api/code/repos"}, called
    for path in called:
        assert f'"{path}"' in ck_src, f"сервер панели не обслуживает {path}"
    assert re.search(r"^export const lifecycle = 1;", js, re.M), "раздел без ленивого обновления"
    assert 'type: "password"' in js and 'const MASK = "••••••"' in js
    assert 'expect: force ? "" : (D && D.digest)' in js, "запись реестра уходит без слепка"
    html = (MOD / "view.html").read_text(encoding="utf-8")
    assert 'id="codeBody"' in html and 'data-i18n="code.title"' in html


@test
def test_code_section_does_not_grow_the_core_footprint(_t):
    """Новый раздел — папка: число файлов ядра, называющих разделы по имени, не выросло."""
    import subprocess
    cp = subprocess.run([sys.executable, str(KIT / "tests/ext_footprint.py"), "--json"], capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    got = json.loads(cp.stdout)
    files = got["files"]["panel_module"]
    assert got["counts"]["panel_module"] <= got["baseline"]["panel_module"], f"ядро знает разделы в новых файлах: {files}"
    assert "scripts/code_repos.py" not in files and "scripts/code_sync.py" not in files, files
