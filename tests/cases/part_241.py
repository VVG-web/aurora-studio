"""Проверки движка Aurora, часть 241: numpy ставится в правильное окружение (1.231.7).

Каркас и помощники — tests/harness.py. Homebrew/PEP 668 не даёт ставить пакеты в
системный Python; панель не обходит запрет `--break-system-packages`, а создаёт единый
venv на той же базе и ставит пакет туда. numpy ускоряет `kb:map --insights`.
"""
from __future__ import annotations

import importlib
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch

from harness import KIT, SCRIPTS, test  # noqa: F401

for _p in (str(KIT / "cockpit"), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@test
def test_numpy_is_a_dependency_and_a_visible_install_row(_t):
    """numpy назван в зависимостях и в списке «Установки», иначе кнопки не будет."""
    req = (KIT / "requirements.txt").read_text(encoding="utf-8")
    line = next((ln.strip() for ln in req.splitlines() if ln.strip().startswith("numpy")), "")
    assert re.fullmatch(r"numpy>=\d+(\.\d+)+,<\d+", line), line
    ck = importlib.import_module("aurora_cockpit")
    rows = {pkg: (mods, what) for pkg, mods, what in ck.ENV_PY}
    assert "numpy" in rows and rows["numpy"][0] == ("numpy",), rows
    assert "kb:map" in rows["numpy"][1], rows["numpy"]


@test
def test_install_package_creates_a_venv_instead_of_breaking_system_python(_t):
    """Homebrew/PEP 668: сначала venv на той же базе, потом pip в него; `--break-system-packages`
    не появляется."""
    RT = importlib.import_module("aurora_runtime")
    calls = []
    checked = {"valid": True, "venv": False, "source": "homebrew",
               "executable": "/opt/homebrew/bin/python3", "externally_managed": True}

    def configure(**kw):
        calls.append(("configure", kw))
        return {"ok": True, "runtime": {"effective_python": "/home/u/.aurora/venv/bin/python"}}

    def pip_install(exe, specs, timeout=0):
        calls.append(("pip", exe, list(specs)))
        return {"ok": True, "output": "installed", "error": ""}

    with patch.object(RT, "validate_python", lambda path, timeout=0: dict(checked)), \
            patch.object(RT, "configure", configure), \
            patch.object(RT, "pip_install", pip_install), \
            patch.object(RT, "package_info", lambda exe, module, distribution="":
                         {"ok": True, "version": "2.3.2", "module": "numpy"}), \
            patch.object(RT, "current", lambda force=False: {"needs_reload": True}):
        got = RT.install_package("numpy", python="/opt/homebrew/bin/python3")
    assert got["ok"] and got["method"] == "venv+pip", got
    assert got["python"] == "/home/u/.aurora/venv/bin/python" and got["version"] == "2.3.2", got
    assert got["needs_reload"] is True, got
    assert calls[0][0] == "configure" and calls[0][1]["isolated"] is True, calls
    assert calls[1] == ("pip", "/home/u/.aurora/venv/bin/python", ["numpy"]), calls
    assert "break-system-packages" not in " ".join(map(str, calls)), calls


@test
def test_install_package_uses_the_existing_venv_without_making_another(_t):
    """Уже venv — новый не создаём, ставим в него."""
    RT = importlib.import_module("aurora_runtime")
    calls = []
    checked = {"valid": True, "venv": True, "source": "venv",
               "executable": "/home/u/.aurora/venv/bin/python", "externally_managed": False}

    with patch.object(RT, "validate_python", lambda path, timeout=0: dict(checked)), \
            patch.object(RT, "configure", lambda **kw: calls.append(("configure", kw)) or
                         {"ok": False, "error": "нельзя"}), \
            patch.object(RT, "pip_install", lambda exe, specs, timeout=0:
                         calls.append(("pip", exe, list(specs))) or
                         {"ok": True, "output": "", "error": ""}), \
            patch.object(RT, "package_info", lambda *a, **k:
                         {"ok": True, "version": "2.3.2", "module": "numpy"}), \
            patch.object(RT, "current", lambda force=False: {"needs_reload": False}):
        got = RT.install_package("numpy", python="/home/u/.aurora/venv/bin/python",
                                 specs=["numpy>=2.2,<3"])
    assert got["ok"] and got["method"] == "pip", got
    assert [c[0] for c in calls] == ["pip"], calls
    assert calls[0][2] == ["numpy>=2.2,<3"], calls


@test
def test_install_page_uses_the_runtime_installer(_t):
    """Страница «Установка» ставит numpy через runtime, а не самодельным `pip --user`."""
    ck = importlib.import_module("aurora_cockpit")
    seen = []

    def fake_install(name, specs=None, timeout=0):
        seen.append((name, list(specs or [])))
        return {"ok": True, "seen": True, "python": "/venv/python", "method": "pip",
                "version": "2.3.2", "output": "ok", "needs_reload": False}

    extras = SimpleNamespace(safe_spec=lambda pkg: ("numpy>=2.2,<3", ""),
                             pinned_spec=lambda pkg: "", record_install=lambda *a: "")
    real = (ck._extras, ck.environment, ck.CACHE.get("access"))
    ck._extras = lambda: extras
    ck.environment = lambda fresh=False: {"items": []}
    try:
        with patch("aurora_runtime.install_package", fake_install):
            got = ck.env_install("numpy")
    finally:
        ck._extras = real[0]
        ck.environment = real[1]
        if real[2] is None:
            ck.CACHE.pop("access", None)
        else:
            ck.CACHE["access"] = real[2]
    assert got["ok"] and got["method"] == "pip" and got["version"] == "2.3.2", got
    assert seen == [("numpy", ["numpy>=2.2,<3"])], seen


@test
def test_look_alike_uses_numpy_and_returns_pairs(_t):
    """numpy-путь `look_alike` действительно считает близость, а не остаётся мёртвой ветвью."""
    from array import array
    import types
    import kb_embed
    import kb_graph

    cards = {"a": {"row": 0}, "b": {"row": 1}, "c": {"row": 2}}
    vec = array("f", [1.0, 0.0, 0.95, 0.1, 0.0, 1.0])

    class _Spy(types.ModuleType):
        """Настоящий numpy, но помнит, какие функции позвали: иначе ветку не отличить."""

        def __init__(self, real):
            super().__init__("numpy")
            self._real, self.used = real, []

        def __getattr__(self, name):
            self.used.append(name)
            return getattr(self._real, name)

    real_np = importlib.import_module("numpy")
    spy = _Spy(real_np)
    common = (
        patch.object(kb_embed, "load_index", lambda: {"cards": cards, "dim": 2}),
        patch.object(kb_embed, "load_vectors", lambda dim, n: vec),
    )
    with common[0], common[1], patch.dict(sys.modules, {"numpy": spy}):
        got = kb_graph.look_alike(".", top=5)
    assert got and got[0][1:] == ("a", "b"), got
    assert all(s >= kb_graph.LOOK_ALIKE for s, _x, _y in got), got
    assert {"frombuffer", "triu_indices", "nonzero"} <= set(spy.used), spy.used

    warned = []
    with common[0], common[1], patch.dict(sys.modules, {"numpy": None}), \
            patch.object(kb_graph, "pair_warning", lambda *a: warned.append(a) or "нет numpy"):
        fallback = kb_graph.look_alike(".", top=5)
    assert warned, "без numpy нет честного предупреждения"
    assert fallback == got, (fallback, got)
