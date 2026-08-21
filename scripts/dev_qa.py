#!/usr/bin/env python3
"""dev_qa.py — QA-контур разработки движка (фреймворк «Аврора»).

Тест-кейсы и сценарии живут в `Development/QA/` — папке, закрытой `.gitignore`: наружу
уходит инструмент, а не то, как мы его проверяем. Скрипт отвечает за механику этого
контура, решения по смыслу остаются человеку и модели.

  python3 scripts/dev_qa.py --list                 # что есть: кейсы, сценарии, покрытие
  python3 scripts/dev_qa.py --check                # целостность: битые covers, дубли, версии
  python3 scripts/dev_qa.py --gap                  # что изменено в коде и чем это покрыто
  python3 scripts/dev_qa.py --run TS-001           # прогон: автотесты + чек-лист + журнал
  python3 scripts/dev_qa.py --run all              # все сценарии подряд
  python3 scripts/dev_qa.py --cover                # задание ассистенту: покрыть новое
  python3 scripts/dev_qa.py --new case "название"  # завести TC-NNN из шаблона
  python3 scripts/dev_qa.py --new scenario "имя"   # завести TS-NNN из шаблона

Работает только в самом ките: в проекте на основе Авроры проверять нечего — там пользуются
движком, а не разрабатывают его.

Панель: `dev:qa-list`, `dev:qa-check`, `dev:qa-gap`, `dev:qa-cover`, `dev:qa-run`, `dev:qa-new`
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
QA = KIT / "Development" / "QA"
CASES, SCEN, RUNS = QA / "cases", QA / "scenarios", QA / "runs"
TEMPLATES = KIT / "skills" / "aurora-dev" / "references"
TODAY = date.today().isoformat()


def is_kit() -> bool:
    return (KIT / "engine_manifest.txt").is_file() and not (KIT / "aurora.config.yaml").is_file()


def frontmatter(text: str) -> dict:
    """Шапка документа QA. Разбор нарочно простой: формат фиксирован шаблоном."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([\w_]+)\s*:\s*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).split("#")[0].strip().strip('"')
    return out


def docs(folder: Path) -> list:
    """[(путь, шапка)] всех документов папки, кроме README."""
    if not folder.is_dir():
        return []
    return [(p, frontmatter(p.read_text(encoding="utf-8", errors="ignore")))
            for p in sorted(folder.glob("*.md")) if p.name != "README.md"]


def as_list(raw: str) -> list:
    return [x.strip() for x in raw.strip("[] ").split(",") if x.strip()]


# ------------------------------------------------------------------ показать

def cmd_list() -> int:
    cases, scen = docs(CASES), docs(SCEN)
    auto = [f for _, f in cases if (f.get("automated", "no") != "no")]
    print(f"# QA движка — {TODAY}\n")
    print(f"Кейсов: {len(cases)} (из них закрыты автотестом: {len(auto)}) · "
          f"сценариев: {len(scen)} · прогонов: {len(list(RUNS.glob('*.md')))}\n")

    print("| ID | Что проверяет | Компонент | Приор. | Автотест |")
    print("|---|---|---|---|---|")
    for path, fm in cases:
        a = fm.get("automated", "no")
        print(f"| {fm.get('id', path.stem)} | {fm.get('title', '—')[:52]} | "
              f"{fm.get('component', '—')[:22]} | {fm.get('priority', '—')} | "
              f"{'да' if a != 'no' else '—'} |")

    print("\n| ID | Маршрут | Тип | Кейсы внутри | Время |")
    print("|---|---|---|---|---|")
    for path, fm in scen:
        print(f"| {fm.get('id', path.stem)} | {fm.get('title', '—')[:46]} | "
              f"{fm.get('type', '—')} | {fm.get('covers', '[]').strip('[]') or '—'} | "
              f"{fm.get('duration', '—')} |")

    covered = {c for _, fm in scen for c in as_list(fm.get("covers", ""))}
    orphan = [fm.get("id") for _, fm in cases
              if fm.get("id") not in covered and fm.get("automated", "no") == "no"]
    if orphan:
        print(f"\nНи в один сценарий не входят и автотестом не закрыты: {', '.join(orphan)}")
        print("Такой кейс не гоняется никогда — либо включите его в сценарий, либо закройте "
              "автотестом.")
    return 0


# ------------------------------------------------------------------ проверить

def cmd_check() -> int:
    cases, scen = docs(CASES), docs(SCEN)
    ids = [fm.get("id", "") for _, fm in cases + scen]
    problems = []

    dup = {i for i in ids if i and ids.count(i) > 1}
    problems += [f"номер занят дважды: {i} — номера не переиспользуются" for i in sorted(dup)]

    known = {fm.get("id") for _, fm in cases}
    for path, fm in scen:
        for c in as_list(fm.get("covers", "")):
            if c not in known:
                problems.append(f"{fm.get('id', path.stem)}: в covers указан {c}, "
                                "а такого кейса нет")

    version = (KIT / "VERSION").read_text(encoding="utf-8").strip()

    def minor(v: str) -> str:
        """1.92.0 и 1.92.1 — одно и то же поведение, под которое писался кейс."""
        return ".".join(v.split(".")[:2])

    def cycles_behind(v: str) -> int:
        """На сколько минорных выпусков кейс отстал от движка."""
        try:
            a, b = minor(v).split("."), minor(version).split(".")
            return (int(b[0]) - int(a[0])) * 1000 + (int(b[1]) - int(a[1]))
        except (ValueError, IndexError):
            return 0

    # Отставание на ОДИН минор — норма: выпуск меняет одну область движка, а кейсов
    # полсотни, и подавляющего большинства правка не касается. Требовать после каждого
    # минора поднять версию во всех файлах — значит учить менять число не глядя, ровно
    # против того, о чём говорит подсказка ниже. Два минора — уже сигнал: на кейс не
    # смотрели два цикла подряд, и совпадение его ожиданий с движком никто не проверял.
    stale = [fm.get("id") for _, fm in cases + scen
             if fm.get("version") and cycles_behind(fm["version"]) >= 2]
    if stale:
        problems.append(f"написаны под другую версию движка (сейчас {version}): "
                        f"{', '.join(sorted(x for x in stale if x))}")

    for path, fm in cases + scen:
        for field in ("id", "title", "priority", "status"):
            if not fm.get(field):
                problems.append(f"{path.name}: нет обязательного поля `{field}`")

    print(f"# Проверка QA — {TODAY}\n")
    if not problems:
        print(f"Кейсов {len(cases)}, сценариев {len(scen)} — расхождений нет.")
        return 0
    print(f"Расхождений: {len(problems)}\n")
    for p in problems:
        print(f"- {p}")
    print("\nВерсия в шапке — не формальность: по ней видно, на каком поведении писался "
          "кейс.\nОбновляйте её вместе с проверкой ожиданий, а не заменой числа.")
    return 1


# ------------------------------------------------------------------ пробел в покрытии

def changed_scripts(base: str) -> list:
    """Изменённые файлы движка относительно базы сравнения."""
    cp = subprocess.run(["git", "diff", "--name-only", base], cwd=str(KIT),
                        capture_output=True, text=True)
    files = [f for f in cp.stdout.split() if f.endswith((".py", ".html", ".txt", ".md"))]
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(KIT),
                           capture_output=True, text=True).stdout
    files += [l[3:].strip() for l in dirty.splitlines() if l[3:].strip().endswith(".py")]
    return sorted(set(files))


def cmd_gap(base: str) -> int:
    """Что изменено в коде и чем это покрыто — вход для решения «дописать тест или кейс»."""
    files = changed_scripts(base)
    if not files:
        print(f"Со времени {base} изменений в движке нет — покрывать нечего.")
        return 0
    cases, scen = docs(CASES), docs(SCEN)
    tests = (KIT / "tests" / "run_tests.py").read_text(encoding="utf-8", errors="ignore")

    print(f"# Покрытие изменений — {TODAY}\n")
    print(f"База сравнения: {base} · изменённых файлов: {len(files)}\n")
    print("| Файл | Автотесты упоминают | Кейсы QA |")
    print("|---|---|---|")
    for f in files:
        stem = Path(f).stem
        in_tests = tests.count(Path(f).name)
        hits = [fm.get("id") for _, fm in cases + scen
                if stem.replace("_", ":") in fm.get("component", "")
                or Path(f).name in fm.get("component", "")]
        print(f"| {f} | {in_tests or '—'} | {', '.join(x for x in hits if x) or '—'} |")

    print("""
Как читать таблицу. Прочерк в обеих колонках — изменение никем не проверяется: либо
допишите автотест (дёшево, падает при каждом коммите), либо заведите кейс QA, если
проверка требует живого контура, браузера или большой базы.

Автотест предпочтительнее всегда, когда поведение воспроизводится за секунды. Кейс —
это признание, что автоматизировать нельзя; такое признание должно быть осознанным.

Завести кейс:      python3 scripts/dev_qa.py --new case "что проверяем"
Завести сценарий:  python3 scripts/dev_qa.py --new scenario "какой маршрут"
""")
    return 0


# ------------------------------------------------------------------ прогон

def cmd_cover(base: str) -> int:
    """Задание ассистенту: дополнить QA под то, что разработано.

    Отдельная точка входа, а не флаг у `--gap`: её копируют в **другой диалог**, где модель
    только что писала код и знает, что именно делала. Таблица покрытия там же — чтобы не
    заставлять её собирать контекст заново.
    """
    rc = cmd_gap(base)
    cases, scen = docs(CASES), docs(SCEN)
    ids = ", ".join(fm.get("id", "") for _, fm in scen)
    print(f"""
─────────────────────────────────────────────────────────────────────
ЗАДАНИЕ АССИСТЕНТУ · ПОКРЫТЬ НОВОЕ — скопируйте блок целиком в чат
─────────────────────────────────────────────────────────────────────
/aurora-dev dev:qa-cover

Ты только что дорабатывал движок Авроры. Дополни QA-контур под сделанное.

Порядок — строго такой:

1. Перечисли, что изменилось по существу: не файлы, а поведение. «Команда X теперь
   отказывается делать Y, если Z» — это проверяемое утверждение, «поправил build_plan» нет.

2. По каждому утверждению реши, чем оно закрывается:
   • **автотест** — если поведение воспроизводится за секунды на временной папке.
     Это предпочтительный вариант ВСЕГДА. Допиши тест в tests/run_tests.py рядом с
     соседями по теме, прогони весь набор, добейся зелёного.
   • **тест-кейс QA** — только если автотестом нельзя: нужен живой Confluence или Jira,
     браузер, база в тысячу карточек, замер времени, оценка читаемости вывода.
     Заводится командой: python3 scripts/dev_qa.py --new case "что проверяем"
   • **сценарий** — если проверяется стык между командами, а не одна команда.
     Заводится командой: python3 scripts/dev_qa.py --new scenario "какой маршрут"

3. Заполни заведённые документы по-настоящему: шаблон копируется целиком, включая
   пояснения — их надо ЗАМЕНИТЬ содержанием. Кейс с текстом из шаблона хуже отсутствующего:
   он создаёт видимость покрытия. Требования — в skills/aurora-dev/references/.

4. Новый кейс включи в подходящий сценарий (поле covers): {ids}.
   Кейс вне сценария и без автотеста не гоняется никогда.

5. Если правка сделала существующий кейс неверным — не удаляй его: поправь ожидания и
   обнови version. Если проверять стало нечего — status: deprecated с причиной, номер
   остаётся за ним навсегда.

6. Проверь себя: python3 scripts/dev_qa.py --check  (должно быть «расхождений нет»)
                 python3 scripts/dev_qa.py --list   (не должно остаться кейсов-сирот)

7. Отчитайся: что закрыл автотестом, что кейсом и почему кейсом, а не тестом.

Сейчас в контуре: кейсов {len(cases)}, сценариев {len(scen)}, автотестов — см. вывод
tests/run_tests.py.""")
    return rc


def cmd_run(what: str, apply_record: bool) -> int:
    scen = docs(SCEN)
    chosen = [(p, fm) for p, fm in scen
              if what == "all" or fm.get("id", "").upper() == what.upper()]
    if not chosen:
        print(f"dev_qa: сценария {what} нет. Доступные: "
              f"{', '.join(fm.get('id', '?') for _, fm in scen)}", file=sys.stderr)
        return 1

    print(f"# Прогон QA — {TODAY}\n")
    print("## Шаг 0 · автотесты (общий для всех сценариев)\n")
    # Прогон запускает автотесты, а автотест может запустить прогон — и это бесконечность.
    # Метка в окружении разрывает круг: вложенный вызов пропускает шаг, а не повторяет его.
    if os.environ.get("AURORA_QA_RUNNING"):
        print("    пропущены: прогон уже идёт внутри автотестов\n")
        cp = None
    else:
        cp = subprocess.run([sys.executable, str(KIT / "tests/run_tests.py")],
                            cwd=str(KIT), capture_output=True, text=True,
                            env={**os.environ, "AURORA_QA_RUNNING": "1"})
    if cp is not None:
        tail = [l for l in cp.stdout.splitlines() if l.strip()][-1:]
        print(f"    {tail[0] if tail else 'нет вывода'}")
        green = cp.returncode == 0
        print(f"    результат: {'зелёные' if green else 'ЕСТЬ ПАДЕНИЯ — сценарии не гоняем'}\n")
        if not green:
            for line in cp.stdout.splitlines():
                if line.startswith("—") or "❌" in line:
                    print(f"    {line}")
            return 1

    for path, fm in chosen:
        print(f"\n## {fm.get('id')} · {fm.get('title')}\n")
        print(f"Файл: `{path.relative_to(KIT)}` · время: {fm.get('duration', '—')} · "
              f"кейсы: {fm.get('covers', '—')}\n")
        body = path.read_text(encoding="utf-8", errors="ignore")
        for line in body.splitlines():
            if line.startswith("### Шаг"):
                print(f"  [ ] {line[4:].strip()}")
        if apply_record:
            rec = RUNS / f"{TODAY}_{fm.get('id')}.md"
            if not rec.exists():
                RUNS.mkdir(parents=True, exist_ok=True)
                rec.write_text(run_template(fm, path), encoding="utf-8")
                print(f"\n  Журнал заведён: {rec.relative_to(KIT)} — заполните по ходу.")
            else:
                print(f"\n  Журнал за сегодня уже есть: {rec.relative_to(KIT)}")

    print("""
─────────────────────────────────────────────────────────────────────
ЗАДАНИЕ АССИСТЕНТУ · ПРОГОН СЦЕНАРИЯ — скопируйте блок целиком в чат
─────────────────────────────────────────────────────────────────────
/aurora-dev dev:qa-run

Выполни шаги сценария по файлу выше, по порядку и без пропусков.

- сверяй **точные** строки вывода и коды возврата, а не смысл;
- после каждого шага проверяй заявленное «состояние после», а не только rc=0;
- ничего не чини молча: обход руками — находка, её надо записать;
- команды с `--apply` в живом проекте выполняй только там, где это прямо сказано
  в шаге; в сомнении — спроси;
- в конце заполни журнал прогона в `Development/QA/runs/` по шаблону: результат
  по каждому шагу, точные числа и что найдено.""")
    return 0


def run_template(fm: dict, path: Path) -> str:
    version = (KIT / "VERSION").read_text(encoding="utf-8").strip()
    steps = [l[4:].strip() for l in path.read_text(encoding="utf-8").splitlines()
             if l.startswith("### Шаг")]
    rows = "\n".join(f"| {s} | |" for s in steps)
    return f"""# Прогон {fm.get('id')} · {TODAY}

| | |
|---|---|
| версия движка | {version} |
| что правили | <заполнить> |
| кто гонял | <заполнить> |
| итог | <пройден / найдено N дефектов> |

## По шагам

| Шаг | Результат |
|---|---|
{rows}

## Находки

<Каждая находка: что ожидалось, что получилось, точные строки вывода.
Если исправлено — версия исправления и имя автотеста, которым закрыто.>

## Числа

<Замеры, по которым видно деградацию: время старта панели, число ошибок линтера,
число карточек до и после.>
"""


# ------------------------------------------------------------------ завести документ

def next_id(folder: Path, prefix: str) -> str:
    used = [int(m.group(1)) for _, fm in docs(folder)
            if (m := re.match(rf"{prefix}-(\d+)", fm.get("id", "")))]
    return f"{prefix}-{max(used, default=0) + 1:03d}"


def cmd_new(kind: str, title: str) -> int:
    folder, prefix, tpl = ((CASES, "TC", "test-case.md") if kind == "case"
                           else (SCEN, "TS", "test-scenario.md"))
    template = TEMPLATES / tpl
    if not template.is_file():
        print(f"dev_qa: нет шаблона {template}", file=sys.stderr)
        return 1
    new = next_id(folder, prefix)
    slug = re.sub(r"[^\w]+", "-", title.lower()).strip("-")[:60]
    path = folder / f"{new}-{slug}.md"
    version = (KIT / "VERSION").read_text(encoding="utf-8").strip()

    body = template.read_text(encoding="utf-8")
    body = re.sub(rf"^id: {prefix}-000.*$", f"id: {new}", body, count=1, flags=re.M)
    body = re.sub(r'^title: ".*"$', f'title: "{title}"', body, count=1, flags=re.M)
    body = re.sub(r"^version: .*$", f"version: {version}", body, count=1, flags=re.M)
    body = re.sub(r"^updated: .*$", f"updated: {TODAY}", body, count=1, flags=re.M)
    body = body.replace(f"# {prefix}-000 · ", f"# {new} · ", 1)

    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    print(f"✅ {path.relative_to(KIT)}\n"
          f"   Шаблон скопирован целиком: замените пояснения своим содержанием, "
          f"пустых разделов оставлять нельзя.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="QA-контур разработки движка")
    ap.add_argument("--list", action="store_true", help="кейсы, сценарии и покрытие")
    ap.add_argument("--check", action="store_true", help="целостность реестра QA")
    ap.add_argument("--gap", action="store_true", help="что изменено в коде и чем покрыто")
    ap.add_argument("--cover", action="store_true",
                    help="то же плюс готовое задание ассистенту: покрыть сделанное")
    ap.add_argument("--base", default="HEAD", metavar="REF",
                    help="база сравнения для --gap (по умолчанию HEAD)")
    # Значение необязательно: «прогнать» без уточнения означает «прогнать всё». Панель
    # запускает команду без аргумента, и требовать его значило бы падать кодом 2 на
    # первом же нажатии кнопки.
    ap.add_argument("--run", nargs="?", const="all", metavar="ID",
                    help="прогон сценария; без значения — все подряд")
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (иначе только показ)")
    ap.add_argument("--record", action="store_true",
                    help="завести журнал прогона в runs/ (для --run)")
    ap.add_argument("--new", nargs=2, metavar=("KIND", "TITLE"),
                    help="завести документ: case|scenario и название")
    a = ap.parse_args()

    if not is_kit():
        print("dev_qa: это не кит. QA-контур нужен там, где движок разрабатывают, "
              "а не там, где им пользуются.", file=sys.stderr)
        return 1
    if not QA.is_dir() and not a.new:
        print(f"dev_qa: нет {QA.relative_to(KIT)} — кухня разработки не заведена.\n"
              f"Создайте: python3 scripts/dev_qa.py --new case \"первая проверка\"",
              file=sys.stderr)
        return 1

    if a.new:
        kind = a.new[0].lower()
        if kind not in ("case", "scenario"):
            print("dev_qa: вид документа — case или scenario", file=sys.stderr)
            return 1
        return cmd_new(kind, a.new[1])
    if a.check:
        return cmd_check()
    if a.cover:
        return cmd_cover(a.base)
    if a.gap:
        return cmd_gap(a.base)
    if a.run:
        return cmd_run(a.run, a.record)
    return cmd_list()


if __name__ == "__main__":
    sys.exit(main())
