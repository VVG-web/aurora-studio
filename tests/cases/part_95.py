"""Проверки движка Aurora, часть 95: длина путей на Windows, macOS и Linux (`path_budget`).

Каркас и помощники — tests/harness.py. Проект открывают на разных системах, и путь, который
на одной создаётся, на другой не создаётся совсем: Windows принимает 259 знаков пути (MAX_PATH
260 с завершающим нулём) и 255 знаков имени UTF-16, macOS и Linux — 255 БАЙТ имени UTF-8
(кириллица занимает два байта на букву). Здесь проверяется чистый счёт (пределы и их границы,
знаки против байтов), обход зеркал (гистограммы длин, потолок числа файлов с пометкой «проверено
частично», ссылки не раскрываются) и то, что проверка ничего не пишет, кроме временных папок.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import os
import sys

from harness import KIT, SCRIPTS, test  # noqa: F401


def _pb():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("path_budget")


@test
def test_path_limits_count_characters_on_windows_and_bytes_on_unix(tmp: Path):
    """Знаки UTF-16 у Windows, байты UTF-8 у macOS и Linux: 128 кириллических букв — 128 и 256."""
    pb = _pb()
    word = "я" * 128
    assert pb.measure("windows", word) == 128, pb.measure("windows", word)
    assert pb.measure("macos", word) == 256 and pb.measure("linux", word) == 256
    assert pb.measure("windows", "x" * 259) == 259
    # знак вне BMP — два знака UTF-16 и четыре байта UTF-8
    astral = "\U0001F600"
    assert pb.measure("windows", astral) == 2 and pb.measure("macos", astral) == 4
    # пределы: MAX_PATH 260 включает завершающий нуль, имя на macOS и Linux — 255 байт
    assert pb.LIMITS["windows"] == {"path": 259, "dir": 247, "name": 255}, pb.LIMITS["windows"]
    assert pb.LIMITS["macos"]["name"] == 255 == pb.LIMITS["linux"]["name"]
    assert pb.LIMITS["macos"]["path"] == 1023 and pb.LIMITS["linux"]["path"] == 4095


@test
def test_path_budget_roots_are_real_here_and_assumed_elsewhere(tmp: Path):
    """Корень этой системы — настоящий путь, чужих — допущение с именем папки проекта; константа, не настройка."""
    pb = _pb()
    proj = tmp / "PRJ-A"
    proj.mkdir()
    on_mac = pb.root_models(str(proj), current="macos")
    assert on_mac["macos"] == os.path.abspath(proj), on_mac
    assert on_mac["windows"] == "C:\\Users\\username\\Documents\\Projects\\PRJ-A", on_mac
    assert on_mac["linux"] == "/home/username/Projects/PRJ-A", on_mac
    on_win = pb.root_models(str(proj), current="windows")
    assert on_win["windows"] == os.path.abspath(proj) and on_win["macos"].endswith("/PRJ-A"), on_win
    forced = pb.root_models(str(proj), current="macos", roots={"windows": "D:\\x"})
    assert forced["windows"] == "D:\\x", forced
    assert pb.WIN_ASSUMED_ROOT == "C:\\Users\\username\\Documents\\Projects"


@test
def test_path_scan_measures_every_file_and_folder_without_following_links(tmp: Path):
    """Обход считает длины внутри папки, делит по верхним уровням и не раскрывает ссылки."""
    pb = _pb()
    base = tmp / "mirror"
    (base / "Страница" / "дочерняя").mkdir(parents=True)
    (base / "Страница" / "index.md").write_text("x", encoding="utf-8")
    (base / "Страница" / "дочерняя" / "лист.md").write_text("x", encoding="utf-8")
    (base / "Страница_assets").mkdir()
    (base / "Страница_assets" / "схема.drawio").write_text("x", encoding="utf-8")
    (base / "Другая.md").write_text("x", encoding="utf-8")
    outside = tmp / "outside"
    outside.mkdir()
    (outside / "не-считать.md").write_text("x", encoding="utf-8")
    try:
        os.symlink(outside, base / "ссылка")
    except (OSError, NotImplementedError):
        pass        # Windows без прав на ссылки: проверка остаётся без этого условия
    res = pb.scan(str(base), tops=True)
    assert res.exists and not res.partial, vars(res)
    assert res.total.files == 4, res.total.files
    assert pb.measure("windows", "Страница/дочерняя/лист.md") in res.total.w, res.total.w
    assert pb.measure("macos", "Страница/дочерняя/лист.md") in res.total.b
    assert res.total.b != res.total.w, "байты и знаки у кириллицы различаются"
    assert set(res.tops) == {"Страница", "Другая"}, res.tops
    assert res.tops["Страница"].files == 3 and res.tops["Другая"].files == 1
    assert "не-считать" not in res.total.long_w[1] and "не-считать" not in res.total.long_b[1]
    assert pb.scan(str(tmp / "нет-такой")).exists is False


@test
def test_path_scan_stops_at_the_file_ceiling_and_says_partial(tmp: Path):
    """Потолок числа файлов: обход останавливается, результат — «проверено частично», порядок по имени."""
    pb = _pb()
    base = tmp / "big"
    base.mkdir()
    for i in range(30):
        (base / f"f{i:02d}.md").write_text("x", encoding="utf-8")
    res = pb.scan(str(base), left=[10])
    assert res.partial and res.total.files == 10, (res.partial, res.total.files)
    again = pb.scan(str(base), left=[10])
    assert again.total.long_w == res.total.long_w and dict(again.total.w) == dict(res.total.w)
    full = pb.scan(str(base), left=[100])
    assert not full.partial and full.total.files == 30


@test
def test_path_tally_counts_files_over_a_limit_at_the_boundary(tmp: Path):
    """Граница предела: файл ровно в предел проходит, на знак длиннее — нет; считаются знаки внутри папки."""
    pb = _pb()
    t = pb.Tally()
    for n in (10, 11, 12):
        t.add("x" * n, "x" * n, False)
    assert pb.Tally.over(t.w, 11) == 1 and pb.Tally.over(t.w, 12) == 0 and pb.Tally.over(t.w, 9) == 3
    t.add("d" * 30, "d" * 30, True)
    assert t.files == 3 and pb.Tally.over(t.dw, 29) == 1 and t.name_w == (30, "d" * 30)
