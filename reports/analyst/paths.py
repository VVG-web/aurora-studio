#!/usr/bin/env python3
"""paths.py — откуда отчёт берёт настройки и данные и куда кладёт результат.

Своего конфига у отчёта нет. Пакет, из которого он приехал, носил с собой
`settings/config.yaml` — второй файл настроек рядом с `aurora.config.yaml`, где имя
проекта и адрес Jira уже записаны. Два источника правды расходятся молча: в пакете
стояло имя одного проекта, а в шапке отчёта печаталось имя другого: имя
проекта в вёрстку не подставлялось вовсе. Настройки читаются из конфига проекта.

YAML читается без PyYAML — как и во всём остальном движке, лишней зависимости у
отчёта нет.

Всё, что отдаёт модуль, — абсолютные пути от корня проекта; скрипты пакета
запускаются с рабочим каталогом проекта (так их зовёт и панель, и `ops:report`).
"""
from __future__ import annotations

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def config_text(path: str) -> str:
    return open(path, encoding="utf-8", errors="ignore").read() if os.path.isfile(path) else ""


def scalar(text: str, key: str, default: str = "") -> str:
    m = re.search(rf'^\s*{re.escape(key)}\s*:\s*"?([^"\n#]+?)"?\s*$', text, re.M)
    return m.group(1).strip() if m else default


def section(text: str, key: str, indent: int = 0) -> str:
    """Вложенный блок YAML — по отступу, а не по маркеру следующей секции.

    В движке для этого есть `block()`: он режет от ключа до первой из перечисленных
    строк-ограничителей. Ограничители перечисляются руками, и как только проект
    поменяет порядок секций в конфиге или заведёт новую между ними, блок молча
    захватит чужие поля — `scalar` найдёт в нём первый попавшийся `base_url`.
    Здесь секции вложенные (`reports:` → `analyst:`), и такой разбор нужен точный.
    """
    want = " " * indent + key + ":"
    lines, out, inside = text.splitlines(), [], False
    for line in lines:
        if not inside:
            if line.rstrip() == want or line.startswith(want + " "):
                inside = True
            continue
        # пустые строки и комментарии блок не закрывают
        if not line.strip() or line.lstrip().startswith("#"):
            out.append(line)
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line)
    return "\n".join(out)

PROJECT_ROOT = os.path.abspath(os.getcwd())
CONFIG_PATH = os.path.join(PROJECT_ROOT, "aurora.config.yaml")

# Умолчания. Проект, который ничего не написал в `reports:`, всё равно должен собраться:
# ростер и события заводятся пустыми шаблонами, данные копятся в кэше движка.
DEFAULTS = {
    "roster": "Settings/report-roster.csv",
    "events": "Settings/report-events.csv",
    "data_dir": ".opencode/cache/reports/analyst",
    "output": "Artifacts/reports/{project}_analyst_extended.html",
}


def _text() -> str:
    return config_text(CONFIG_PATH)


def _analyst() -> str:
    """Блок `reports:` → `analyst:`. Секции нет — работаем на умолчаниях."""
    return section(section(_text(), "reports"), "analyst", indent=2)


def project_name() -> str:
    return scalar(section(_text(), "project"), "name", "project")


def project_slug() -> str:
    """Короткое машинное имя проекта — оно и идёт в имя файла отчёта.

    Имя проекта человеческое и бывает каким угодно («НДС в Киргизии»): в имени файла
    это пробелы и кириллица, которые потом ломают ссылки и передачу через командную
    строку. Slug для того и заведён.
    """
    p = section(_text(), "project")
    return scalar(p, "slug", "") or scalar(p, "name", "project")


def year() -> int:
    """Год, по которому режутся ISO-недели на этом прогоне шага.

    Раньше он был вписан в генератор числом `2026` в четырёх местах. Отчёт по проекту
    прошлого года собирался пустым: недели считались, данные отбрасывались фильтром
    `dt.year == 2026`, и человек видел исправный дашборд без единой строки.

    Проект живёт дольше года, и отчёт собирается по каждому году отдельно: оркестратор
    прогоняет цепочку счёта по разу на год, подставляя год сюда через окружение. В
    конфиге при этом остаётся год по умолчанию — тот, что дашборд открывает первым.
    """
    import datetime
    env = os.environ.get("AURORA_REPORT_YEAR", "")
    if env.strip().isdigit():
        return int(env)
    raw = scalar(_analyst(), "year", "")
    return int(raw) if raw.strip().isdigit() else datetime.date.today().year


def configured_years() -> list:
    """Годы, которые велено собрать: `years: [2024, 2025]`.

    Одиночный `year:` сюда не входит намеренно — он задаёт год, который дашборд
    открывает первым, а не единственный собираемый. Иначе проект, честно указавший
    свой основной год, лишался бы переключателя периодов: собран был бы один год,
    и переключать оказалось бы нечего.

    Пусто — значит, ограничения нет и годы определяются по самим данным.
    """
    m = re.search(r"^\s*years\s*:\s*\[([^\]]*)\]", _analyst(), re.M)
    return sorted({int(x) for x in re.findall(r"\d{4}", m.group(1))}) if m else []


def setting(key: str) -> str:
    """Путь из секции `reports:` (или умолчание), приведённый к абсолютному."""
    rel = scalar(_analyst(), key, "") or DEFAULTS[key]
    rel = rel.replace("{project}", "_".join(project_slug().split()))
    return os.path.join(PROJECT_ROOT, rel)


def jira() -> dict:
    j = section(section(_text(), "atlassian"), "jira", indent=2)
    return {"base_url": scalar(j, "base_url").rstrip("/"),
            "project_key": scalar(j, "project_key")}


def confluence() -> dict:
    c = section(section(_text(), "atlassian"), "confluence", indent=2)
    return {"base_url": scalar(c, "base_url").rstrip("/"),
            "space": scalar(c, "space")}


def sources_jira() -> str:
    """Зеркало задач Jira (`sync:jira`). Оно свежее выгрузки, из которой считаются
    метрики, и по нему уточняется исполнитель на момент события."""
    rel = scalar(section(_text(), "paths"), "sources_jira", "Sources/JIRA")
    return os.path.join(PROJECT_ROOT, rel)


ROSTER_PATH = setting("roster")
EVENTS_PATH = setting("events")
DATA_DIR = setting("data_dir")
OUTPUT_PATH = setting("output")
PROJECT_NAME = project_name()
YEAR = year()

# Обратная совместимость с пакетом, из которого приехали скрипты: там эти имена
# указывали внутрь папки пакета.
SETTINGS_DIR = os.path.dirname(ROSTER_PATH)
REPORT_DIR = os.path.dirname(OUTPUT_PATH)
PACKAGE_ROOT = PROJECT_ROOT


def get_config() -> dict:
    """Совместимость: скрипты пакета звали `get_config()` и лезли в словарь сами."""
    return {
        "project": {"name": PROJECT_NAME, "jira_key": jira()["project_key"],
                    "confluence_space": confluence()["space"], "year": YEAR},
        "jira": {"base_url": jira()["base_url"]},
        "confluence": {"base_url": confluence()["base_url"]},
        "output": {"html_name": os.path.basename(OUTPUT_PATH)},
    }


def load_config() -> dict:
    return get_config()


def ensure_dirs() -> None:
    for d in (DATA_DIR, YEARS_DIR, REPORT_DIR, os.path.dirname(ROSTER_PATH)):
        os.makedirs(d, exist_ok=True)


def data(name: str) -> str:
    """Сырьё выгрузок — общее для всех лет.

    Ходить в Jira и Confluence один раз, а резать по годам сколько угодно: смена
    отчётного года не должна стоить нового похода в корпоративные системы.
    """
    return os.path.join(DATA_DIR, name)


YEARS_DIR = os.path.join(DATA_DIR, "by-year")


def out(name: str, y: int | None = None) -> str:
    """Посчитанное по одному году — в своей папке.

    Папку заводим здесь же: шагов счёта четыре, и каждый писал бы свой `makedirs`
    перед записью — а забытый в одном месте роняет прогон на пятой минуте.
    """
    d = os.path.join(YEARS_DIR, str(y or YEAR))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


def years_built() -> list:
    """Годы, по которым посчитанное уже лежит в кэше, — их и собирает дашборд."""
    if not os.path.isdir(YEARS_DIR):
        return []
    return sorted(int(d) for d in os.listdir(YEARS_DIR)
                  if d.isdigit() and os.path.isfile(
                      os.path.join(YEARS_DIR, d, "analyst_metrics.json")))


def read_rows(path: str) -> list:
    """Строки CSV независимо от того, запятая в файле или точка с запятой.

    Ростер и события заполняет человек в Excel: русская локаль сохраняет CSV через
    точку с запятой, английская — через запятую. Пакет читал файл `DictReader`ом с
    разделителем по умолчанию, и файл из русского Excel разбирался в одну колонку:
    ростер приезжал пустым, все исполнители получали роль «не в ростере», и отчёт
    молча собирался без единой строки по людям.
    """
    import csv
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        head = f.readline()
        f.seek(0)
        delim = ";" if head.count(";") > head.count(",") else ","
        return list(csv.DictReader(f, delimiter=delim))


def roster() -> dict:
    """ФИО → роль. Один разбор ростера на всех, кто по нему раскладывает людей."""
    out = {}
    for row in read_rows(ROSTER_PATH):
        fio = (row.get("ФИО") or "").strip()
        role = (row.get("Роль") or "").strip()
        if fio and role:
            out[fio] = role
    return out
