#!/usr/bin/env python3
"""run_tests.py — регрессионные тесты движка Aurora на синтетическом проекте.

Зачем: скрипты движка пишут разом в сотни файлов живой базы. Дефект «два файла
переименовались в одно имя, карточка молча затёрта» был найден только ручной сверкой
счётчиков — такие вещи должен ловить прогон, а не человек.

  python3 tests/run_tests.py           # прогнать всё
  python3 tests/run_tests.py -v        # с выводом команд

Каждый тест строит проект с нуля во временной папке (структура — из structure_dirs.txt),
кладёт заранее сломанные карточки и проверяет поведение скриптов. Живые проекты не
используются. Выход: 0 — все тесты прошли, 1 — есть падения.
"""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import traceback
for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на эмодзи и «—» (UnicodeEncodeError)
    # и портят протокол MCP; движок говорит по-русски и пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
import tempfile

import harness  # noqa: F401 — каркас; заодно кладёт tests/ в sys.path
from harness import (  # noqa: F401
    INVARIANTS,
    KIT,
    NO_INVARIANTS,
    ONLY,
    RESULTS,
    SHARD,
    SMOKE,
    select_tests,
    test,
    why,
)

# Проверки лежат в tests/cases/part_NN.py; импорт регистрирует их в harness.REGISTRY по порядку.
import importlib  # noqa: E402

for _part in ['part_01', 'part_02', 'part_03', 'part_04', 'part_05', 'part_06', 'part_07', 'part_08', 'part_09', 'part_10', 'part_11', 'part_12', 'part_13', 'part_14', 'part_15', 'part_16', 'part_17', 'part_18', 'part_19', 'part_20', 'part_21', 'part_22', 'part_23', 'part_24']:
    importlib.import_module("cases." + _part)

# ------------------------------------------------------------------- smoke-мета-тесты
# Не являются инвариантами (имена *_smoke_* исключены из рекурсии subprocess).
@test
def test_smoke_invariants_always_run_with_filter(_t):
    """Пустой фильтр: rc 1 + warning «не подошёл», но инварианты всё равно прогнаны (урок T5)."""
    cp = subprocess.run([sys.executable, __file__, "--only=zzz_no_match_zzz"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace", env={**os.environ, "AURORA_TESTS_ISOLATED": "1"})
    assert cp.returncode == 1, cp.stdout
    assert "не подошёл" in cp.stdout, cp.stdout
    for name in INVARIANTS:
        assert name in cp.stdout, (name, cp.stdout)


@test
def test_smoke_runs_only_invariants(_t):
    """--smoke гоняет только инварианты, без тяжёлых интеграционных проверок."""
    cp = subprocess.run([sys.executable, __file__, "--smoke"],
                      capture_output=True, text=True, encoding="utf-8", errors="replace", env={**os.environ, "AURORA_TESTS_ISOLATED": "1"})
    assert cp.returncode == 0, cp.stdout
    for name in INVARIANTS:
        assert name in cp.stdout, (name, cp.stdout)
    assert "aliases serial fallback without parallelism" not in cp.stdout, cp.stdout


# ---------------------------------+ import-драйвер: исполняет отобранные проверки
# (декоратор лишь регистрирует; исполнение здесь — чтобы --only/--smoke/--no-invariants
# решали состав до прогона).
selected = select_tests(only=ONLY, smoke=SMOKE, no_invariants=NO_INVARIANTS, shard=SHARD)
FILTER_MISSED = bool(ONLY) and not SMOKE and not any(not is_inv for _n, _f, is_inv in selected)
# Рабочая папка проверок снимается тихо: на Windows её держит открытым любой дочерний процесс,
# не успевший выйти, и `rmtree` падал на самой последней строке — после всех проверок, но
# до итога: красный прогон приходил без списка того, что именно красное.
td = tempfile.mkdtemp(prefix="aurora-tests-")
try:
    for _n, _fn, _is_inv in selected:
        run_td = Path(td) / f"case-{len(RESULTS)}"
        run_td.mkdir(parents=True)
        try:
            _fn(run_td)
            RESULTS.append((_n, None))
            print(f"  ✅ {_n}")
        except AssertionError as e:
            RESULTS.append((_n, why(e)))
            print(f"  ❌ {_n}\n     {why(e).splitlines()[0]}")
        except Exception as e:  # noqa: BLE001
            # Не только тип и текст: на чужой системе по одной строке `ValueError: …` не найти,
            # откуда она пришла. Три последних кадра стека — достаточно, чтобы не гадать.
            frames = " ← ".join(f"{os.path.basename(f.filename)}:{f.lineno}"
                               for f in reversed(traceback.extract_tb(e.__traceback__)[-4:]))
            RESULTS.append((_n, f"{type(e).__name__}: {e}  [{frames}]"))
            print(f"  ❌ {_n} — {type(e).__name__}: {e}  [{frames}]")
finally:
    shutil.rmtree(td, ignore_errors=True)


def main() -> int:
    print(f"Aurora engine tests — kit {(KIT / 'VERSION').read_text(encoding='utf-8').strip()}"
          + (f" · только «{ONLY}»" if ONLY else "") + "\n")
    if ONLY and not RESULTS:
        print(f"Ни одна проверка не подошла под «{ONLY}».")
    if FILTER_MISSED:
        print(f"⚠️ фильтр «{ONLY}» не подошёл ни одной проверке — инварианты прогнаны; проверьте опечатку в фильтре")
        return 1
    failed = [(n, e) for n, e in RESULTS if e]
    print(f"\nПройдено: {len(RESULTS) - len(failed)}/{len(RESULTS)}")
    if failed:
        print("\nПадения:")
        for n, e in failed:
            print(f"\n— {n}:\n{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
