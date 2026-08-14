#!/usr/bin/env python3
"""make_extended.py — расширенный дашборд эффективности аналитиков.

Собирает один самодостаточный HTML: недельная активность по Jira и Confluence,
переходы задач, фильтры по типу и человеку. Всё, что отличает проект от проекта —
имя, год, адреса Jira и Confluence, пути к ростеру и событиям — приходит из
`aurora.config.yaml` через `paths.py`; в самом генераторе проектных констант нет.

  python3 .opencode/reports/analyst/make_extended.py

Панель: `ops:report`
"""
import json
import csv
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

YEAR = paths.YEAR
PROJECT = paths.PROJECT_NAME


read_rows = paths.read_rows

# Годы, по которым посчитано. Дашборд собирается сразу по всем: проект живёт дольше
# года, и переключать период человек должен в самом отчёте, а не пересборкой.
YEARS = paths.years_built() or [YEAR]
if YEAR not in YEARS:
    YEAR = YEARS[-1]


def load_year(y):
    with open(paths.out("analyst_metrics.json", y), encoding="utf-8") as f:
        jira_data = json.load(f)
    conf_path = paths.out("confluence_activity.json", y)
    conf_data = (json.load(open(conf_path, encoding="utf-8"))
                 if os.path.isfile(conf_path) else {"weeks": [], "pages": [], "role_of": {}})
    return jira_data, conf_data


def week_labels_for(y, weeks):
    """Понедельник каждой ISO-недели — подпись «06 (02.02)»."""
    out = {}
    for w in weeks:
        try:
            out[w] = datetime.date.fromisocalendar(y, int(w), 1).strftime("%d.%m")
        except ValueError:
            pass        # 53-я неделя есть не в каждом году
    return out

# 3. Read Roster
# Колонка «Прежние ФИО» перечисляет старые написания имени одного и того же
# человека (смена фамилии). В выгрузках Jira они остаются «замороженными»:
# assignee_history хранит ФИО на момент назначения, поэтому один сотрудник
# распадается на две строки отчёта. Учётная запись (Email) — доказательство,
# что это один человек; склейку делаем здесь, до всех агрегаций.
roster = {}
alias_of = {}      # прежнее ФИО -> актуальное ФИО
aliases_of = {}    # актуальное ФИО -> [прежние ФИО]
account_of = {}    # актуальное ФИО -> учётная запись
for row in read_rows(paths.ROSTER_PATH):
    fio = (row.get("ФИО") or "").strip()
    if not fio:
        continue
    roster[fio] = row.get("Роль") or ""
    prev = [a.strip() for a in (row.get("Прежние ФИО") or "").split(",") if a.strip()]
    if prev:
        aliases_of[fio] = prev
        account_of[fio] = (row.get("Email") or "").strip()
        for a in prev:
            alias_of[a] = fio


def canon(name):
    """Актуальное ФИО сотрудника (прежние написания приводятся к нему).

    Пустой исполнитель в остальном конвейере называется «Не назначен» —
    иначе в таблице по сотрудникам появляется строка с именем null.
    """
    if not name:
        return "Не назначен"
    return alias_of.get(name, name)


def prepare(y):
    """Данные одного года, приведённые к актуальным ФИО и ролям."""
    jira_data, conf_data = load_year(y)

    # Приводим к актуальным ФИО всё, что дальше группируется по людям
    for t in jira_data.get("transitions_raw", []):
        t["assignee"] = canon(t.get("assignee"))

    if jira_data.get("weekly_by_person"):
        merged = {}
        for person, person_weeks in jira_data["weekly_by_person"].items():
            dst = merged.setdefault(canon(person), {})
            for week, wd in person_weeks.items():
                acc = dst.setdefault(week, {"stories": 0, "others": 0, "ba_sa": 0})
                for key in acc:
                    acc[key] += wd.get(key, 0)
        jira_data["weekly_by_person"] = merged

    if jira_data.get("persons_available"):
        seen = {}
        for p in jira_data["persons_available"]:
            seen[canon(p)] = None
        jira_data["persons_available"] = list(seen)

    if jira_data.get("role_of"):
        merged_roles = {}
        for p, r in jira_data["role_of"].items():
            c = canon(p)
            # у прежнего ФИО роли обычно нет — не даём ему затереть настоящую
            if merged_roles.get(c) in (None, "", "Не назначен"):
                merged_roles[c] = r
        jira_data["role_of"] = merged_roles

    # Merge roster into role_of for both jira and confluence if missing
    if "role_of" not in jira_data:
        jira_data["role_of"] = {}
    for person in jira_data.get("persons_available", []):
        if person not in jira_data["role_of"] or not jira_data["role_of"][person]:
            jira_data["role_of"][person] = roster.get(person, "Не назначен")

    # Страницы Confluence одни и те же во всех годах — они лежат отдельно, один раз.
    # Различаются только недели, а их дашборд размечает сам при переключении периода.
    conf_data.pop("pages", None)
    if "role_of" not in conf_data:
        conf_data["role_of"] = {}

    weeks = sorted(set(jira_data.get("weeks", [])) | set(conf_data.get("weeks", [])))
    return {"jira": jira_data, "confluence": conf_data,
            "week_labels": week_labels_for(y, weeks)}


def shared_pages():
    """Страницы Confluence — общий список на все годы.

    Раньше пакет каждого года нёс свою копию: на семилетнем проекте один и тот же
    список из 1705 страниц лежал в файле семь раз — 3,8 МБ из 4,5. Отчёт носят
    почтой, и его вес — не мелочь.
    """
    for y in YEARS:
        p = paths.out("confluence_activity.json", y)
        if not os.path.isfile(p):
            continue
        pages = json.load(open(p, encoding="utf-8")).get("pages", [])
        for page in pages:
            for field in ("author_created", "author_updated"):
                if page.get(field):
                    page[field] = canon(page[field])
            author = page.get("author_created")
            if author:
                page["role"] = page.get("role") or roster.get(author, "Не назначен")
            # разметку недель делает браузер: она зависит от выбранного года
            page.pop("_week_created", None)
            page.pop("_week_updated", None)
        return pages
    return []

# 4. Load events config
events = []
for row in read_rows(paths.EVENTS_PATH):
    if not row.get('weeks') or not row.get('caption'): continue
    # Недели в данных двузначные ('06'), а в конфиге пишут и «6» — дополняем нулём,
    # иначе событие молча не найдёт свою неделю и полоса не нарисуется.
    weeks = [w.strip().zfill(2) if w.strip().isdigit() else w.strip()
             for w in row['weeks'].split(',') if w.strip()]
    events.append({'weeks': weeks, 'name': (row.get('name') or '').strip(),
                   'caption': row['caption'].strip(),
                   'severity': (row.get('severity') or '').strip()})

YEAR_DATA = {str(y): prepare(y) for y in YEARS}

# Текущая ISO-неделя: она ещё идёт, и её столбик заведомо неполный. Тренд по ней не
# считаем — иначе последняя точка каждый раз тянет линию вниз просто потому, что
# сегодня среда.
_today = datetime.date.today()
_iso = _today.isocalendar()
partial = {"year": _iso[0], "week": f"{_iso[1]:02d}"}

DATA = {
    # Активный год кладём и плоско: все блоки читают DATA.jira / DATA.confluence,
    # а переключатель просто переставляет эти ссылки на другой год.
    "jira": YEAR_DATA[str(YEAR)]["jira"],
    "confluence": YEAR_DATA[str(YEAR)]["confluence"],
    "week_labels": YEAR_DATA[str(YEAR)]["week_labels"],
    "pages": shared_pages(),
    "years": YEAR_DATA,
    "year": YEAR,
    "years_available": YEARS,
    "partial": partial,
    "roster": roster,
    "events": events,
    # Готовая команда пересборки: вставляется в терминал, когда дашборд открыт
    # из файла и кнопка не может ничего запустить сама. Называем команду так, как
    # она называется в панели и в реестре, а не путём к скрипту.
    "rebuild_cmd": "python3 .opencode/scripts/report_analyst.py --skip-fetch",
    # Файлы настроек: путь от корня проекта (по нему файл открывается в браузере,
    # если serve_dashboard.py не запущен и дашборд отдаёт обычная статика) и папка —
    # её показывает кнопка «Папка».
    "config_files": {
        name: {
            "file": os.path.basename(path),
            "path": os.path.relpath(path, paths.PROJECT_ROOT),
            "dir": os.path.relpath(os.path.dirname(path), paths.PROJECT_ROOT),
        }
        for name, path in {
            "roster": paths.ROSTER_PATH,
            "events": paths.EVENTS_PATH,
            "analyst_metrics": paths.out("analyst_metrics.json"),
        }.items()
    },
    # объединённые сотрудники: актуальное ФИО -> прежние написания + учётка
    "merged_people": {
        fio: {"aliases": prev, "account": account_of.get(fio, "")}
        for fio, prev in aliases_of.items()
    }
}
# HTML Template
html_template = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__PROJECT__ — эффективность аналитиков (расширенный)</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--txt:#e2e8f0;--mut:#94a3b8;--acc:#38bdf8;--acc2:#4ade80;--acc3:#f472b6}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font-family:'Inter',system-ui,sans-serif;padding:32px;line-height:1.5}
.wrap{max-width:1260px;margin:0 auto}
h1{font-size:25px;margin-bottom:4px}
.sub{color:var(--mut);margin-bottom:22px;font-size:14px}
.built{display:block;font-size:11.5px;opacity:.75;margin-top:2px}
.head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;flex-wrap:wrap}
.head-title{min-width:0}
.head-actions{flex:0 0 auto;max-width:340px;text-align:right}
.rebuild-btn{background:var(--acc);color:#0b1220;border:none;border-radius:8px;padding:9px 16px;font-family:inherit;font-size:13.5px;font-weight:600;cursor:pointer;white-space:nowrap}
.rebuild-btn:hover{filter:brightness(1.08)}
.rebuild-btn:disabled{opacity:.55;cursor:progress}
#rebuild-status{font-size:11px;margin-top:6px;line-height:1.5;text-align:left}
#rebuild-status.ok{color:var(--acc2)}
#rebuild-status.err{color:#fbbf24}
#rebuild-status code{font-size:10.5px;color:var(--txt);background:rgba(148,163,184,.12);padding:1px 5px;border-radius:4px;word-break:break-all;user-select:all}
@media (max-width:760px){.head-actions{max-width:100%;text-align:left}}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin-bottom:22px}
.card h2{font-size:16px;margin-bottom:12px;font-weight:600}
.canvasbox{position:relative;height:440px;width:100%}
.note{color:var(--mut);font-size:12.5px;margin-top:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500}
td:first-child,th:first-child{text-align:left}
tr:hover td{background:#243249}
.pctl-btn{padding:5px 12px;border-radius:7px;font-size:12.5px;cursor:pointer;border:1px solid var(--line);background:transparent;color:var(--mut);user-select:none;margin-right:4px}
.pctl-btn:hover{color:var(--txt);border-color:var(--acc)}
.pctl-btn.on{background:var(--acc);color:#0b1220;border-color:var(--acc)}
/* --- Панель фильтров --- */
.filters{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:22px}
.filter-group{display:flex;flex-direction:column;align-items:flex-start;gap:6px;min-width:0}
.filter-group>label{font-size:12px;font-weight:600;color:var(--txt)}
.filter-group select,.filter-group input[type=number]{background:var(--bg);color:var(--txt);border:1px solid var(--line);padding:6px 8px;border-radius:6px;font-size:13px;font-family:inherit;max-width:100%}
.filter-group select{min-width:150px}
#config-open-row{grid-column:1/-1}
.cfg-btns{display:flex;flex-wrap:wrap;gap:6px}
.cfg-btns .pctl-btn{margin-right:0}
#cfg-status{font-size:11px;margin-top:4px;line-height:1.5}
#cfg-status.ok{color:var(--acc2)}
#cfg-status.err{color:#fbbf24}
#cfg-status a{color:var(--acc)}
#cfg-status code{font-size:11px;color:var(--txt);background:rgba(148,163,184,.12);padding:1px 5px;border-radius:4px;word-break:break-all;user-select:all}
.mini{font-size:10px;padding:1px 6px;border-radius:5px;border:1px solid var(--line);background:transparent;color:var(--mut);cursor:pointer;font-family:inherit;margin-left:2px}
.mini:hover{color:var(--txt);border-color:var(--acc)}
/* --- Выпадающий список с чекбоксами --- */
.dd{position:relative;width:100%;max-width:260px}
.dd-btn{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:7px 10px;font-family:inherit;font-size:13px;text-align:left;cursor:pointer}
.dd-btn:hover{border-color:var(--acc)}
.dd.open>.dd-btn{border-color:var(--acc)}
.dd.active>.dd-btn{border-color:var(--acc);color:var(--acc)}
.dd-txt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dd-caret{color:var(--mut);font-size:11px;flex:0 0 auto;transition:transform .15s}
.dd.open .dd-caret{transform:rotate(180deg)}
.dd-menu{display:none;position:absolute;z-index:50;top:calc(100% + 4px);left:0;min-width:100%;width:max-content;max-width:min(360px,80vw);background:var(--card);border:1px solid var(--line);border-radius:9px;box-shadow:0 14px 30px rgba(0,0,0,.5);padding:8px}
.dd.open>.dd-menu{display:block}
.dd-search{width:100%;background:var(--bg);color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-family:inherit;font-size:12px;margin-bottom:6px}
.dd-empty{display:none;font-size:12px;color:var(--mut);padding:4px 7px}
.dd-empty.show{display:block}
.chkgrp-master{padding-bottom:6px;margin-bottom:6px;border-bottom:1px solid var(--line)}
.chkgrp-master .chkgrp-label{color:var(--txt);font-weight:600}
.chkgrp-val{display:flex;flex-direction:column;gap:1px;max-height:240px;overflow-y:auto;width:100%}
.chkgrp-val.chkgrp-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(74px,1fr));gap:2px}
.chkgrp-val::-webkit-scrollbar{width:8px}
.chkgrp-val::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
.chkgrp-label{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--mut);cursor:pointer;border-radius:6px;padding:4px 7px;max-width:100%}
.chkgrp-label:hover{color:var(--txt);background:rgba(148,163,184,.1)}
.chkgrp-label.hidden{display:none}
.chkgrp-label span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chkgrp-label input[type=checkbox]{width:14px;height:14px;flex:0 0 auto;cursor:pointer;accent-color:var(--acc)}
/* --- KPI --- */
.kpi-row{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:22px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;flex:1;min-width:180px}
.kpi .n{font-size:26px;font-weight:700;color:var(--acc)}
.kpi .l{font-size:12.5px;color:var(--mut);margin-top:4px}
/* --- Таблицы и текст --- */
.hl{color:var(--acc);font-weight:700}
.merged-mark{color:var(--acc);cursor:help;font-weight:700}
.alias-hint{color:var(--mut);font-style:normal;opacity:.75}
td.min-val{color:var(--acc2);font-weight:600}
td.max-val{color:#f87171;font-weight:600}
.method{font-size:13px;color:var(--mut)}
.method b{color:var(--txt)}
/* --- Переключатель периода --- */
.year-pick{display:flex;align-items:center;gap:9px;justify-content:flex-end;margin-bottom:9px}
.year-lbl{font-size:11.5px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.year-tabs{display:inline-flex;background:rgba(148,163,184,.12);border-radius:9px;padding:2px}
.year-tabs button{background:none;border:none;color:var(--mut);font-family:inherit;font-size:13px;
  font-weight:600;padding:5px 13px;border-radius:7px;cursor:pointer;font-variant-numeric:tabular-nums}
.year-tabs button:hover{color:var(--txt)}
.year-tabs button[aria-pressed="true"]{background:var(--acc);color:#0b1220}
@media (max-width:640px){body{padding:16px}h1{font-size:21px}.canvasbox{height:320px}}
</style>
</head>
<body>
<div class="wrap">
<div class="head">
  <div class="head-title">
    <h1>📅 __PROJECT__ — эффективность команды аналитиков (расширенный)</h1>
    <div class="sub">Дашборд активности и переходов · __SOURCES__ · <span id="sub-year">__YEAR__</span>
      <span class="built">собран __BUILT_AT__ · правки в файлах настроек попадают сюда только после пересборки</span>
    </div>
  </div>
  <div class="head-actions">
    <div class="year-pick" id="year-pick" hidden>
      <span class="year-lbl">Период</span>
      <div class="year-tabs" id="year-tabs"></div>
    </div>
    <button type="button" class="rebuild-btn" id="btn-rebuild">↻ Пересобрать дашборд</button>
    <div class="note" id="rebuild-status"></div>
  </div>
</div>

<div class="filters">
  <!-- Type Filter -->
  <div class="filter-group">
    <label id="lbl-type">Тип задачи (Jira)</label>
    <div class="dd" data-dd="type">
      <button type="button" class="dd-btn" aria-haspopup="true" aria-expanded="false" aria-labelledby="lbl-type">
        <span class="dd-txt">Все</span><span class="dd-caret">▾</span>
      </button>
      <div class="dd-menu">
        <div class="chkgrp-master">
          <label class="chkgrp-label">
            <input type="checkbox" data-master="type" checked>
            <span>Все</span>
          </label>
        </div>
        <div class="chkgrp-val" data-dim="type" id="chkgrp-type">
          <label class="chkgrp-label">
            <input type="checkbox" value="История">
            <span>История</span>
          </label>
          <label class="chkgrp-label">
            <input type="checkbox" value="BA-SA Task">
            <span>BA-SA Task</span>
          </label>
          <label class="chkgrp-label">
            <input type="checkbox" value="Прочие">
            <span>Прочие</span>
          </label>
        </div>
      </div>
    </div>
  </div>
  <!-- Role Filter -->
  <div class="filter-group">
    <label id="lbl-role">Роль</label>
    <div class="dd" data-dd="role">
      <button type="button" class="dd-btn" aria-haspopup="true" aria-expanded="false" aria-labelledby="lbl-role">
        <span class="dd-txt">Все</span><span class="dd-caret">▾</span>
      </button>
      <div class="dd-menu">
        <div class="chkgrp-master">
          <label class="chkgrp-label">
            <input type="checkbox" data-master="role" checked>
            <span>Все</span>
          </label>
        </div>
        <div class="chkgrp-val" data-dim="role" id="chkgrp-role"></div>
      </div>
    </div>
  </div>
  <!-- Person Filter -->
  <div class="filter-group">
    <label id="lbl-person">Сотрудник</label>
    <div class="dd" data-dd="person">
      <button type="button" class="dd-btn" aria-haspopup="true" aria-expanded="false" aria-labelledby="lbl-person">
        <span class="dd-txt">Все</span><span class="dd-caret">▾</span>
      </button>
      <div class="dd-menu">
        <input type="text" class="dd-search" data-search="person" placeholder="Поиск по ФИО…">
        <div class="chkgrp-master">
          <label class="chkgrp-label">
            <input type="checkbox" data-master="person" checked>
            <span>Все</span>
          </label>
        </div>
        <div class="chkgrp-val" data-dim="person" id="chkgrp-person"></div>
        <div class="dd-empty" data-empty="person">Ничего не найдено</div>
      </div>
    </div>
  </div>
  <!-- Week Filter -->
  <div class="filter-group">
    <label id="lbl-week">Неделя</label>
    <div class="dd" data-dd="week">
      <button type="button" class="dd-btn" aria-haspopup="true" aria-expanded="false" aria-labelledby="lbl-week">
        <span class="dd-txt">Все</span><span class="dd-caret">▾</span>
      </button>
      <div class="dd-menu">
        <input type="text" class="dd-search" data-search="week" placeholder="Поиск по неделе…">
        <div class="chkgrp-master">
          <label class="chkgrp-label">
            <input type="checkbox" data-master="week" checked>
            <span>Все</span>
          </label>
        </div>
        <div class="chkgrp-val chkgrp-grid" data-dim="week" id="chkgrp-week"></div>
        <div class="dd-empty" data-empty="week">Ничего не найдено</div>
      </div>
    </div>
  </div>
  <!-- Weight -->
  <div class="filter-group">
    <label>Вес типов (категории BA/SA/Прочие)</label>
    <input type="number" id="weight-input" min="0.1" max="2" step="0.1" value="0.5" style="width:100px">
    <div class="note" style="font-size:11px;margin-top:2px">Множитель для «Прочих» и BA/SA (1.0 = без весов)</div>
  </div>
  <!-- Scale Mode -->
  <div class="filter-group">
    <label>Масштаб Блока 1</label>
    <select id="scale-mode"><option value="total" selected>Тотал</option><option value="percapita">На 1 человека</option></select>
  </div>
  <!-- Config Buttons -->
  <div class="filter-group" id="config-open-row">
    <label>Конфигурация</label>
    <div class="cfg-btns">
      <button type="button" class="pctl-btn cfg-btn" data-cfg="roster">Лица (roster)</button>
      <button type="button" class="pctl-btn cfg-btn" data-cfg="events">События (events)</button>
      <button type="button" class="pctl-btn cfg-btn" data-cfg="analyst_metrics">Метрики (metrics)</button>
      <button type="button" class="pctl-btn" id="btn-folder" title="Показать файлы настроек в Finder">📁 Папка</button>
    </div>
    <div class="note" id="cfg-status">Открывает файл в системном редакторе (через serve_dashboard.py). Иначе предложит открыть его в браузере.</div>
  </div>
</div>

<div class="kpi-row">
  <div class="kpi"><div class="n" id="kpi-stories">—</div><div class="l">историй → «Аналитика готово»</div></div>
  <div class="kpi"><div class="n" id="kpi-others">—</div><div class="l">прочих артефактов → «Аналитика готово»</div></div>
  <div class="kpi"><div class="n" id="kpi-total">—</div><div class="l">всего переходов</div></div>
</div>

<!-- Блок 1 -->
<div class="card">
  <h2>1. Переходы в «Аналитика - готово» по неделям (стек)</h2>
  <div class="canvasbox"><canvas id="chart-weekly"></canvas></div>
  <div class="note">Каждый столбец — одна ISO-неделя __YEAR__ года. Данные агрегированы, фильтры применяются только если возможно.</div>
</div>

<!-- Блок 2 -->
<div class="card">
  <h2>2. Метрики длительности переходов историй между этапами (Jira)</h2>
  <div style="margin-bottom:10px">
    <span style="color:var(--mut);font-size:13px;margin-right:8px">Среднее по выборке, обрезанной до персентиля:</span>
    <button class="pctl-btn" data-p="100">100% (все)</button>
    <button class="pctl-btn on" data-p="95">95%</button>
    <button class="pctl-btn" data-p="90">90%</button>
    <button class="pctl-btn" data-p="80">80%</button>
    <button class="pctl-btn" data-p="70">70%</button>
  </div>
  <div class="note" id="pctl-tag"></div>
  <div style="margin-top:10px;overflow-x:auto">
    <table>
      <thead>
        <tr>
          <th>Переход</th><th>N</th><th>Среднее (персентиль <span id="pctl-show">95%</span>)</th>
          <th>Среднее (все)</th><th>Δ к среднему</th><th>Медиана</th><th>Граница персентиля</th>
        </tr>
      </thead>
      <tbody id="transBody"></tbody>
    </table>
  </div>
</div>

<!-- Блок 3 -->
<div class="card">
  <h2>3. Длительность переходов по сотрудникам (Jira)</h2>
  <div style="margin-top:10px;overflow-x:auto">
    <table>
      <thead>
        <tr id="empHead">
          <th>Сотрудник</th><th>Роль</th><th>N (всего)</th>
        </tr>
      </thead>
      <tbody id="empBody"></tbody>
    </table>
  </div>
  <div class="note">Зелёным подсвечен минимум по столбцу (лучший результат), красным — максимум. Значения — среднее при выбранном персентиле.</div>
  <div class="note" id="merged-note"></div>
</div>

<!-- Блок 4 -->
<div class="card">
  <h2>4. Созданные страницы по неделям (Confluence)</h2>
  <div class="canvasbox"><canvas id="chart-conf-pages"></canvas></div>
  <div class="note">Количество страниц, созданных на соответствующей неделе (по дате создания).</div>
</div>

<!-- Блок 5 -->
<div class="card">
  <h2>5. Создание + правки страниц по неделям (Confluence)</h2>
  <div class="canvasbox"><canvas id="chart-conf-stack"></canvas></div>
  <div class="note">Сумма созданных (created) и обновлённых (updated) страниц по неделям.</div>
</div>

  <div class="card">
    <h2>Методология и ограничения</h2>
    <div class="method">
      <b>Confluence:</b> данные получены через REST API с expand=history (createdDate, createdBy) и expand=version (updated, author). Блоки 4–5 используют реальные даты создания и правки.<br>
      <b>Jira (История):</b> длительности считаются по первому входу в статус. Событие "аналитик закончил" = переход в "Аналитика - готово".<br>
      <b>Jira (BA-SA Task):</b> событие "аналитик закончил" = переход в статус "Закрыто" (отдельный workflow без статуса "Аналитика - готово").<br>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
const DATA = __DATA__;
let currentPctl = 95;

// Chart.js по умолчанию рисует подписи тёмно-серым — на тёмном фоне их не видно
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = 'rgba(148,163,184,0.15)';
Chart.defaults.font.family = "'Inter', system-ui, sans-serif";

// ============ ПЕРИОД ============
// Отчёт несёт данные всех лет, по которым посчитано. Активный год лежит в DATA.jira /
// DATA.confluence / DATA.week_labels, и переключатель просто переставляет эти ссылки:
// так все блоки читают год, ничего про переключение не зная.
let YEAR_ACTIVE = DATA.year;

// Страницы Confluence лежат один раз на все годы: в файле повторять их незачем.
// Номер недели зависит от выбранного периода, поэтому размечаем при переключении.
// Дату берём как написано в строке («2024-11-26T…»), без перевода в UTC: иначе
// страница, созданная в понедельник в 01:00 по Москве, уезжала бы в прошлую неделю.
function isoWeekOf(raw, y) {
  if (!raw || raw.slice(0, 4) !== String(y)) return null;
  const d = new Date(raw.slice(0, 10) + 'T00:00:00Z');
  if (isNaN(d)) return null;
  const t = new Date(d);
  t.setUTCDate(t.getUTCDate() + 4 - (t.getUTCDay() || 7));   // четверг своей недели
  const jan1 = new Date(Date.UTC(t.getUTCFullYear(), 0, 1));
  const wk = Math.ceil(((t - jan1) / 86400000 + 1) / 7);
  return String(wk).padStart(2, '0');
}

function stampPages(y) {
  (DATA.pages || []).forEach(p => {
    p._week_created = isoWeekOf(p.created, y);
    p._week_updated = isoWeekOf(p.updated, y);
  });
}
DATA.confluence.pages = DATA.pages || [];
stampPages(YEAR_ACTIVE);

// ============ СПРАВОЧНИКИ (из DATA) ============
// Пересобираются при смене года: состав команды и набор недель у каждого года свои,
// и оставить прошлогодний список сотрудников — значит показать фильтр с людьми,
// которых в этом году на проекте не было.
let roles = [], persons = [], weeks = [];

function readDictionaries() {
  roles = [...new Set([
    ...Object.values(DATA.jira.role_of || {}),
    ...Object.values((DATA.confluence || {}).role_of || {}),
    ...Object.values(DATA.roster || {})
  ].filter(Boolean))];

  persons = [...new Set([
    ...(DATA.jira.persons_available || []),
    ...Object.keys(DATA.jira.weekly_by_person || {}),
    ...((DATA.confluence || {}).pages || []).map(p => p.author_created)
  ].filter(Boolean))];

  weeks = [...new Set([
    ...(DATA.jira.weeks || []),
    ...((DATA.confluence || {}).weeks || [])
  ])].sort();
}
readDictionaries();

// ============ ОБЪЕДИНЁННЫЕ СОТРУДНИКИ ============
// Смена фамилии: в выгрузке Jira старые назначения подписаны прежним ФИО.
// Строки склеены по учётной записи из roster — помечаем это явно.
const MERGED = DATA.merged_people || {};
function mergedTitle(name) {
  const m = MERGED[name];
  if (!m) return '';
  return 'Учтено вместе с прежним ФИО: ' + m.aliases.join(', ') +
         (m.account ? ' (учётная запись ' + m.account + ')' : '');
}
function mergedMark(name) {
  const t = mergedTitle(name);
  return t ? ` <span class="merged-mark" title="${t}">≡</span>` : '';
}
document.getElementById('merged-note').innerHTML = Object.keys(MERGED).length
  ? '≡ — строки объединены по учётной записи: ' + Object.entries(MERGED)
      .map(([fio, m]) => `<b>${fio}</b> = ${m.aliases.join(', ')}${m.account ? ' · ' + m.account : ''}`)
      .join('; ')
  : '';

// ============ ЭЛЕМЕНТЫ УПРАВЛЕНИЯ ============
const weightInput = document.getElementById('weight-input');
const scaleSelect = document.getElementById('scale-mode');
let othersWeight = parseFloat(weightInput.value) || 1;
let BA_SA_WEIGHT = othersWeight;
let scaleMode = scaleSelect.value;

// Целые показываем целыми, дробные (после весов) — с одним знаком
const fmtNum = v => Number.isInteger(v) ? v : (Math.round(v * 10) / 10).toFixed(1);

// ============ FILTER INITIALIZATION ============
// Populate checkbox groups for roles, persons, weeks
const chkgrpRole = document.getElementById('chkgrp-role');
const chkgrpPerson = document.getElementById('chkgrp-person');
const chkgrpWeek = document.getElementById('chkgrp-week');

function fillFilterGroups() {
  chkgrpRole.innerHTML = '';
  chkgrpPerson.innerHTML = '';
  chkgrpWeek.innerHTML = '';

  [...roles].sort().forEach(r => {
    const lbl = document.createElement('label');
    lbl.className = 'chkgrp-label';
    lbl.innerHTML = `<input type="checkbox" value="${r}"><span>${r}</span>`;
    chkgrpRole.appendChild(lbl);
  });

  [...persons].sort().forEach(p => {
    const lbl = document.createElement('label');
    lbl.className = 'chkgrp-label';
    // прежние ФИО показываем рядом: по ним ищут и по ним же узнают человека
    const alias = MERGED[p] ? ` <i class="alias-hint">(${MERGED[p].aliases.join(', ')})</i>` : '';
    if (MERGED[p]) lbl.title = mergedTitle(p);
    lbl.innerHTML = `<input type="checkbox" value="${p}"><span>${p}${alias}</span>`;
    chkgrpPerson.appendChild(lbl);
  });

  [...weeks].forEach(w => {
    const lbl = document.createElement('label');
    lbl.className = 'chkgrp-label';
    lbl.innerHTML = `<input type="checkbox" value="${w}"><span>${w}</span>`;
    chkgrpWeek.appendChild(lbl);
  });
}
fillFilterGroups();

// ============ FILTER STATE ============
let filters = {
  type: new Set(['__all']),
  role: new Set(['__all']),
  person: new Set(['__all']),
  week: new Set(['__all'])
};

// Helper for filter check
const fOk = (set, val) => set.has('__all') || set.has(val);

// ============ ВЫПАДАЮЩИЕ СПИСКИ ============
function closeAllDd(except) {
  document.querySelectorAll('.dd.open').forEach(dd => {
    if (dd === except) return;
    dd.classList.remove('open');
    dd.querySelector('.dd-btn').setAttribute('aria-expanded', 'false');
  });
}

// Подпись на кнопке: «Все» / выбранное значение / «Выбрано: N»
function refreshDdLabels() {
  document.querySelectorAll('.dd').forEach(dd => {
    const set = filters[dd.dataset.dd];
    let txt;
    if (set.has('__all')) txt = 'Все';
    else if (set.size === 1) txt = [...set][0];
    else txt = 'Выбрано: ' + set.size;
    const el = dd.querySelector('.dd-txt');
    el.textContent = txt;
    el.title = set.has('__all') ? '' : [...set].join(', ');
    dd.classList.toggle('active', !set.has('__all'));
  });
}

document.querySelectorAll('.dd-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const dd = btn.closest('.dd');
    const willOpen = !dd.classList.contains('open');
    closeAllDd(dd);
    dd.classList.toggle('open', willOpen);
    btn.setAttribute('aria-expanded', String(willOpen));
    if (willOpen) {
      const s = dd.querySelector('.dd-search');
      if (s) s.focus();
    }
  });
});

// Клик мимо списка и Escape — закрыть
document.addEventListener('click', e => { if (!e.target.closest('.dd')) closeAllDd(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeAllDd(); });

// Поиск внутри списка
document.querySelectorAll('.dd-search').forEach(inp => {
  inp.addEventListener('input', () => {
    const dim = inp.dataset.search;
    const q = inp.value.trim().toLowerCase();
    const grp = document.getElementById('chkgrp-' + dim);
    let shown = 0;
    grp.querySelectorAll('.chkgrp-label').forEach(lbl => {
      const hit = !q || lbl.textContent.toLowerCase().includes(q);
      lbl.classList.toggle('hidden', !hit);
      if (hit) shown++;
    });
    document.querySelector(`[data-empty="${dim}"]`).classList.toggle('show', shown === 0);
  });
});

// ============ CHECKBOX EVENT HANDLERS ============
// Master "Все" checkbox handler
document.querySelectorAll('[data-master]').forEach(master => {
  master.addEventListener('change', e => {
    const dim = e.target.dataset.master;
    const chkgrp = document.querySelector(`.chkgrp-val[data-dim="${dim}"]`);
    const valueChks = chkgrp.querySelectorAll('input[type=checkbox]');
    if (e.target.checked) {
      // Reset to all
      filters[dim] = new Set(['__all']);
      valueChks.forEach(c => c.checked = false);
    } else {
      // «Все» нельзя снять вручную — снимается само при выборе значений
      const picked = [...valueChks].filter(c => c.checked).map(c => c.value);
      if (picked.length === 0) { e.target.checked = true; return; }
      filters[dim] = new Set(picked);
    }
    refreshDdLabels();
    updateAll();
  });
});

// Value checkbox handler
// Слушаем контейнер, а не каждую галочку: списки сотрудников и недель пересобираются
// при смене года, и обработчики, навешенные поимённо, остались бы на выброшенных
// элементах — фильтры молча перестали бы работать со второго года.
document.querySelectorAll('.chkgrp-val').forEach(box => {
  box.addEventListener('change', e => {
    if (e.target.type !== 'checkbox') return;
    const dim = box.dataset.dim;
    const val = e.target.value;
    const master = document.querySelector(`[data-master="${dim}"]`);
    if (e.target.checked) {
      // Первое выбранное значение отменяет режим «Все»
      if (filters[dim].has('__all')) filters[dim] = new Set();
      filters[dim].add(val);
      master.checked = false;
    } else {
      // Remove value
      filters[dim].delete(val);
    }
    // Auto back to all if no values selected
    if (filters[dim].size === 0) {
      filters[dim] = new Set(['__all']);
      master.checked = true;
    }
    refreshDdLabels();
    updateAll();
  });
});

// ============ КНОПКИ ОТКРЫТИЯ КОНФИГОВ ============
// Открыть файл в системном редакторе умеет только serve_dashboard.py (эндпоинт
// /__open/). Если дашборд отдаёт обычная статика — эндпоинта нет, и вместо
// молчания предлагаем открыть файл прямо в браузере по относительному пути.
const CFG_FILES = DATA.config_files || {};
const cfgStatus = document.getElementById('cfg-status');

function setCfgStatus(html, kind) {
  cfgStatus.innerHTML = html;
  cfgStatus.className = 'note' + (kind ? ' ' + kind : '');
}

const SERVE_CMD = 'python3 .opencode/tmp_eff/serve_dashboard.py --html';
const REBUILD_CMD = DATA.rebuild_cmd;
const noServer = e => /failed to fetch|networkerror|load failed/i.test(e.message || '');

function copyLegacy(text) {
  // file:// и обычный http — небезопасный контекст, Clipboard API там недоступен
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  try { return document.execCommand('copy'); } catch (e) { return false; } finally { ta.remove(); }
}

function copyText(text) {
  // Clipboard API отказывает, если окно не в фокусе — тогда старый способ
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text)
      .then(() => true)
      .catch(() => copyLegacy(text));
  }
  return Promise.resolve(copyLegacy(text));
}

// Пути показываем всегда: без сервера открыть файл из браузера нельзя, но
// скопировать путь и открыть его самому — можно везде.
function cfgPaths(c) {
  return `Файл: <code>${c.path}</code> <button type="button" class="mini" data-copy="${c.path}">копировать</button>` +
         `<br>Папка: <code>${c.dir}</code> <button type="button" class="mini" data-copy="${c.dir}">копировать</button>`;
}

function cfgFallback(name, why) {
  const c = CFG_FILES[name] || {};
  setCfgStatus(`Открыть файл может только serve_dashboard.py (${why}).<br>` + cfgPaths(c) +
               `<br>Открывать и править прямо отсюда — если запустить: <code>${SERVE_CMD}</code>`, 'err');
}

// Кнопки «копировать» живут внутри перерисовываемого блока — вешаем делегат
cfgStatus.addEventListener('click', e => {
  const btn = e.target.closest('[data-copy]');
  if (!btn) return;
  const was = btn.textContent;
  copyText(btn.dataset.copy).then(ok => {
    // Если скопировать не дали — выделяем путь, чтобы забрать вручную
    btn.textContent = ok ? 'скопировано' : 'выделите путь ↑';
    if (!ok) getSelection().selectAllChildren(btn.previousElementSibling);
    setTimeout(() => { btn.textContent = was; }, 2000);
  });
});

document.querySelectorAll('.cfg-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const name = btn.dataset.cfg;
    const c = CFG_FILES[name] || {};

    // Страница открыта из файла — сервера нет и быть не может, сразу к путям.
    if (location.protocol === 'file:') {
      cfgFallback(name, 'дашборд открыт из файла, а не по http');
      return;
    }

    setCfgStatus('Открываю ' + (c.file || name) + '…');
    fetch('/__open/' + name)
      .then(r => r.ok ? r.json() : Promise.reject(new Error('сервер ответил HTTP ' + r.status)))
      .then(d => {
        if (!d.ok) throw new Error(d.error || 'неизвестная ошибка');
        setCfgStatus(`Открыт в редакторе (${d.app || 'системный'}).<br>` + cfgPaths(c), 'ok');
      })
      .catch(e => cfgFallback(name, noServer(e) ? 'страница открыта не через него' : e.message));
  });
});

// Показать файл настроек в Finder
document.getElementById('btn-folder').addEventListener('click', () => {
  const c = CFG_FILES.roster || {};
  if (location.protocol === 'file:') {
    setCfgStatus('Папку открывает только serve_dashboard.py.<br>' + cfgPaths(c), 'err');
    return;
  }
  setCfgStatus('Открываю папку…');
  fetch('/__reveal/roster')
    .then(r => r.ok ? r.json() : Promise.reject(new Error('сервер ответил HTTP ' + r.status)))
    .then(d => {
      if (!d.ok) throw new Error(d.error || 'неизвестная ошибка');
      setCfgStatus(`Папка открыта в Finder: <code>${d.dir}</code>`, 'ok');
    })
    .catch(e => setCfgStatus(
      `Открыть папку может только serve_dashboard.py (${noServer(e) ? 'страница открыта не через него' : e.message}).<br>` +
      cfgPaths(c), 'err'));
});

// ============ ПЕРЕСБОРКА ============
// С сервером — один клик. Без сервера запустить процесс из браузера нельзя,
// поэтому кнопка кладёт готовую команду в буфер: остаётся вставить в терминал.
const rebuildBtn = document.getElementById('btn-rebuild');
const rebuildStatus = document.getElementById('rebuild-status');

function setRebuildStatus(html, kind) {
  rebuildStatus.innerHTML = html;
  rebuildStatus.className = 'note' + (kind ? ' ' + kind : '');
}

function offerRebuildCommand(why) {
  copyText(REBUILD_CMD).then(copied => {
    setRebuildStatus(
      `${why}. Команда ${copied ? '<b>скопирована</b> — вставьте' : 'для запуска'} в терминал:` +
      `<br><code>${REBUILD_CMD}</code>`, 'err');
    if (!copied) getSelection().selectAllChildren(rebuildStatus.querySelector('code'));
  });
}

rebuildBtn.addEventListener('click', () => {
  if (location.protocol === 'file:') {
    offerRebuildCommand('Дашборд открыт из файла, сам себя пересобрать он не может');
    return;
  }
  rebuildBtn.disabled = true;
  setRebuildStatus('Пересобираю… это несколько секунд');
  fetch('/__rebuild')
    .then(r => r.json().catch(() => Promise.reject(new Error('сервер ответил HTTP ' + r.status))))
    .then(d => {
      if (!d.ok) throw new Error(d.error);
      setRebuildStatus('Готово, обновляю страницу…', 'ok');
      location.reload();
    })
    .catch(e => {
      rebuildBtn.disabled = false;
      if (noServer(e)) offerRebuildCommand('Страница открыта не через serve_dashboard.py');
      else setRebuildStatus('Пересборка не удалась — ' + e.message, 'err');
    });
});

// Вес типов и масштаб Блока 1
weightInput.addEventListener('input', () => {
  const v = parseFloat(weightInput.value);
  othersWeight = BA_SA_WEIGHT = (isFinite(v) && v > 0) ? v : 1;
  updateBlock1();
});
scaleSelect.addEventListener('change', () => {
  scaleMode = scaleSelect.value;
  updateBlock1();
});

// Только кнопки персентилей: у кнопок конфигурации тот же класс, но нет data-p
document.querySelectorAll('.pctl-btn[data-p]').forEach(btn => {
  btn.addEventListener('click', e => {
    document.querySelectorAll('.pctl-btn[data-p]').forEach(b => b.classList.remove('on'));
    e.target.classList.add('on');
    currentPctl = parseInt(e.target.dataset.p);
    document.getElementById('pctl-show').textContent = currentPctl === 100 ? '—' : currentPctl + '%';
    document.getElementById('pctl-tag').textContent = currentPctl === 100 ? 'выборка целиком (с выбросами)' : `обрезка до ${currentPctl}% персентиля (отброшены верхние ${100-currentPctl}% значений-выбросов)`;
    updateBlock2();
    updateBlock3();
  });
});

// Charts instances
let chart1, chart4, chart5;

// ============ Event Highlight Plugin ============ 
const weekEvents = (() => {
  const eventMap = {};
  (DATA.events || []).forEach(ev => {
    ev.weeks.forEach(w => {
      if (!eventMap[w]) eventMap[w] = { captions: [], hasRed: false, hasYellow: false };
      if (!eventMap[w].captions.includes(ev.caption)) eventMap[w].captions.push(ev.caption);
      if (ev.severity === 'red') eventMap[w].hasRed = true;
      if (ev.severity === 'yellow') eventMap[w].hasYellow = true;
    });
  });
  const resolved = {};
  Object.entries(eventMap).forEach(([week, data]) => {
    resolved[week] = { captions: data.captions.join(', '), severity: data.hasRed ? 'red' : (data.hasYellow ? 'yellow' : null) };
  });
  return resolved;
})();
function eventPlugin(rawWeeks){
  return {
    id: 'weekEvents',
    beforeDatasetsDraw(chart, args, plugin){
      const ctx = chart.ctx;
      ctx.save();
      rawWeeks.forEach((w, i) => {
        if (!weekEvents[w]) return;
        const severity = weekEvents[w].severity;
        if (!severity) return;
        const x = chart.scales.x.getPixelForValue(i);
        const nextX = i < rawWeeks.length - 1 ? chart.scales.x.getPixelForValue(i + 1) : x + (chart.chartArea.width / rawWeeks.length);
        const bandWidth = nextX - x;
        const half = bandWidth / 2;
        ctx.fillStyle = severity === 'red' ? 'rgba(239, 68, 68, 0.22)' : 'rgba(250, 204, 21, 0.22)';
        ctx.fillRect(x - half, chart.chartArea.top, bandWidth, chart.chartArea.height);
      });
      ctx.restore();
    },
    afterDatasetsDraw(chart, args, plugin){
      const ctx = chart.ctx;
      ctx.save();
      rawWeeks.forEach((w, i) => {
        if (!weekEvents[w]) return;
        const cap = weekEvents[w].captions;
        if (!cap) return;
        // Многонедельное событие подписываем один раз, у начала полосы
        const prev = rawWeeks[i - 1];
        if (prev && weekEvents[prev] && weekEvents[prev].captions === cap) return;
        const x = chart.scales.x.getPixelForValue(i);
        ctx.font = '10px Inter, sans-serif';
        const textLen = ctx.measureText(cap).width;
        const bandTop = chart.chartArea.top;
        const y = bandTop + textLen - 5;
        ctx.translate(x + 8, y + 5);
        ctx.rotate(-Math.PI / 2);
        ctx.fillStyle = '#e2e8f0';
        ctx.textAlign = 'left';
        ctx.fillText(cap, 0, 0);
        ctx.restore();
        ctx.save();
      });
      ctx.restore();
    }
  };
}

function getPercentileMean(arr, pctl) {
  if (!arr || arr.length === 0) return null;
  if (pctl >= 100) return arr.reduce((a,b)=>a+b,0)/arr.length;
  const sorted = [...arr].sort((a,b)=>a-b);
  const cut = Math.max(1, Math.round(sorted.length * pctl / 100));
  const sliced = sorted.slice(0, cut);
  return sliced.reduce((a,b)=>a+b,0)/sliced.length;
}

function getMedian(arr) {
  if (!arr || arr.length === 0) return null;
  const sorted = [...arr].sort((a,b)=>a-b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 !== 0 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function getPctlValue(arr, pctl) {
  if (!arr || arr.length === 0) return null;
  const sorted = [...arr].sort((a,b)=>a-b);
  if (pctl >= 100) return sorted[sorted.length-1];
  const idx = Math.min(sorted.length-1, Math.max(0, Math.round(sorted.length * pctl / 100) - 1));
  return sorted[idx];
}

  // ============ ТРЕНД ============
  // Линия считается по рабочему окну, а не по всей оси. Два края портили её молча.
  //
  // Начало года до старта проекта. Проект, поехавший в июле, приносил на ось
  // полгода нулей — они тянут наклон вниз тем сильнее, чем позже начался проект,
  // и два одинаково работающих проекта получали разный «тренд» только потому, что
  // один стартовал в январе, а другой в сентябре.
  //
  // Конец года, который ещё не наступил. Пока год идёт, оставшиеся недели — не
  // спад, а будущее. В прежней сборке отчёта на живом проекте линия блока 5 уходила
  // из 83 в −5: девятнадцать ненаступивших недель считались нулевой работой.
  //
  // Плюс текущая неделя: она идёт прямо сейчас и заведомо неполная.
  function trendWindow(arr) {
    let lo = 0, hi = arr.length - 1;
    while (lo <= hi && !arr[lo]) lo++;          // нули до первой активности
    while (hi >= lo && !arr[hi]) hi--;          // нули после последней
    return [lo, hi];
  }

  // Индекс недели, которая ещё идёт (или -1). Считаем только по активному году:
  // в отчёте за прошлый год незавершённых недель нет.
  function partialIndex(weeks) {
    const p = DATA.partial || {};
    if (!weeks || p.year !== YEAR_ACTIVE) return -1;
    return weeks.indexOf(p.week);
  }

  function computeTrendLine(arr, weeks) {
    let [lo, hi] = trendWindow(arr);
    const cut = partialIndex(weeks);
    if (cut === hi && hi > lo) hi--;            // неполную неделю в подгонку не берём
    const n = hi - lo + 1;
    if (n < 2) return null;
    let sx = 0, sy = 0, sxy = 0, sxx = 0;
    for (let i = lo; i <= hi; i++) {
      const x = i - lo + 1, v = arr[i];
      sx += x; sy += v; sxy += x * v; sxx += x * x;
    }
    const denom = n * sxx - sx * sx; if (denom === 0) return null;
    const slope = (n * sxy - sx * sy) / denom;
    const intercept = (sy - slope * sx) / n;
    // Вне окна линии нет: продлевать её на ненаступившие недели — то же враньё,
    // только нарисованное пунктиром.
    return arr.map((_, i) =>
      (i < lo || i > hi) ? null : slope * (i - lo + 1) + intercept);
  }


function updateBlock1() {
  const formattedLabels = DATA.jira.weeks.map(w => (DATA.week_labels && DATA.week_labels[w] ? w + ' (' + DATA.week_labels[w] + ')' : w));
  let stories = [], others = [], ba_sa = [];
  let rawSumS = 0, rawSumO = 0, rawSumBA = 0;
  
  // Build filtered weekly data from weekly_by_person or weekly
  const weeklyByPerson = DATA.jira.weekly_by_person || {};
  const weekly = DATA.jira.weekly || {};
  
  // First pass: compute activePerWeek (count analysts with >0 output in each week)
  const activePerWeek = {};
  Object.values(weeklyByPerson).forEach(personWeeks => {
    Object.entries(personWeeks).forEach(([week, data]) => {
      const total = (data.stories || 0) + (data.others || 0) + (data.ba_sa || 0);
      if (total > 0) {
        activePerWeek[week] = (activePerWeek[week] || 0) + 1;
      }
    });
  });
  
  DATA.jira.weeks.forEach(w => {
    // Week filter (fOk for multi-select)
    if (!fOk(filters.week, w)) {
      stories.push(0); others.push(0); ba_sa.push(0);
      return;
    }
    
    // Determine which data source to use
    let data = { stories: 0, others: 0, ba_sa: 0 };
    
    if (!filters.person.has('__all')) {
      // Use weekly_by_person for specific person(s) - sum across all matching persons
      Object.entries(weeklyByPerson).forEach(([person, personWeeks]) => {
        if (fOk(filters.person, person)) {
          const weekData = personWeeks[w] || {};
          data.stories += weekData.stories || 0;
          data.others += weekData.others || 0;
          data.ba_sa += weekData.ba_sa || 0;
        }
      });
    } else if (!filters.role.has('__all')) {
      // Sum by role from weekly_by_person
      Object.entries(weeklyByPerson).forEach(([person, personWeeks]) => {
        const role = DATA.jira.role_of[person] || DATA.roster[person] || 'Не назначен';
        if (fOk(filters.role, role)) {
          const weekData = personWeeks[w] || {};
          data.stories += weekData.stories || 0;
          data.others += weekData.others || 0;
          data.ba_sa += weekData.ba_sa || 0;
        }
      });
    } else {
      // Use aggregated weekly data
      const weekData = weekly[w] || {};
      data.stories = weekData.stories || 0;
      data.others = weekData.others || 0;
      data.ba_sa = weekData.ba_sa || 0;
    }
    
    // Apply type filter (multi-select: zero categories not in set)
    if (!fOk(filters.type, 'История')) data.stories = 0;
    if (!fOk(filters.type, 'BA-SA Task')) data.ba_sa = 0;
    if (!fOk(filters.type, 'Прочие')) data.others = 0;

    // Apply weights AFTER type filter, BEFORE pushing
    const weightedOthers = data.others * othersWeight;
    const weightedBaS = data.ba_sa * BA_SA_WEIGHT;

    stories.push(data.stories);
    others.push(weightedOthers);
    ba_sa.push(weightedBaS);
    rawSumS += data.stories;
    rawSumO += weightedOthers;
    rawSumBA += weightedBaS;
  });
  
  // Apply per-capita division if in per-capita mode
  let sumS = rawSumS, sumO = rawSumO, sumBA = rawSumBA;
  let accActive = 0;
  if (scaleMode === 'percapita') {
    // Divide each week's values by active analysts in that week
    for (let i = 0; i < DATA.jira.weeks.length; i++) {
      const w = DATA.jira.weeks[i];
      const activeCount = activePerWeek[w] || 0;
      if (activeCount > 0) {
        stories[i] = stories[i] / activeCount;
        others[i] = others[i] / activeCount;
        ba_sa[i] = ba_sa[i] / activeCount;
        accActive += activeCount;
      }
    }
    // Recalculate KPI sums using per-capita values
    sumS = stories.reduce((a, b) => a + b, 0);
    sumO = others.reduce((a, b) => a + b, 0);
    sumBA = ba_sa.reduce((a, b) => a + b, 0);
  } else {
    // Total mode: count all weeks as active for consistency
    accActive = DATA.jira.weeks.length;
  }
  
  // Compute trend line data (uses per-capita-adjusted values when in per-capita mode)
  const totalData = formattedLabels.map((_, i) => (stories[i] || 0) + (others[i] || 0) + (ba_sa[i] || 0));
  const trendData = computeTrendLine(totalData, DATA.jira.weeks);
  
  // Render KPIs (branch on scaleMode)
  if (scaleMode === 'percapita') {
    const safeAccActive = accActive > 0 ? accActive : 1;
    document.getElementById('kpi-stories').textContent = (rawSumS / safeAccActive).toFixed(1);
    document.getElementById('kpi-others').textContent = (rawSumO / safeAccActive).toFixed(1);
    document.getElementById('kpi-total').textContent = ((rawSumS + rawSumO + rawSumBA) / safeAccActive).toFixed(1);
  } else {
    document.getElementById('kpi-stories').textContent = fmtNum(sumS);
    document.getElementById('kpi-others').textContent = fmtNum(sumO);
    document.getElementById('kpi-total').textContent = fmtNum(sumS + sumO + sumBA);
  }
  
  const datasets = [
    {label: 'Истории', data: stories, backgroundColor: '#4ade80', stack: 's'},
    {label: 'Прочие', data: others, backgroundColor: '#38bdf8', stack: 's'}
  ];
  if (ba_sa.some(v => v > 0)) {
    datasets.push({label: 'BA-SA', data: ba_sa, backgroundColor: '#f472b6', stack: 's'});
  }
  
  // Add trend line dataset
  if (trendData) {
    datasets.push({
      label: 'Тренд',
      type: 'line',
      data: trendData,
      borderColor: '#f59e0b',
      borderWidth: 2,
      borderDash: [4, 4],
      tension: 0,
      pointRadius: 0,
      fill: false,
      spanGaps: false,
      order: 0
    });
  }
  
  if (chart1) chart1.destroy();
  chart1 = new Chart(document.getElementById('chart-weekly'), {
    type: 'bar',
    data: { labels: formattedLabels, datasets },
    options: { responsive: true, maintainAspectRatio: false, scales: { x: { stacked: true, offset: true }, y: { stacked: true } } },
    plugins: [eventPlugin(DATA.jira.weeks)]
  });
}

const TRANSITIONS_ORDER = [
  "Запланировано → Анализ",
  "Анализ → Анализ Готово",
  "Анализ Готово → Разработка",
  "Разработка → Разработка готово",
  "Разработка готово → Тестирование",
  "Тестирование → Тестирование готово"
];

function filterTransitions() {
  return DATA.jira.transitions_raw.filter(t => {
    if (!fOk(filters.type, t.issue_type)) return false;
    if (!fOk(filters.week, t.week)) return false;
    if (!fOk(filters.person, t.assignee)) return false;
    
    const role = DATA.jira.role_of[t.assignee] || DATA.roster[t.assignee] || 'Не назначен';
    if (!fOk(filters.role, role)) return false;
    
    return true;
  });
}

function updateBlock2() {
  const trans = filterTransitions();
  const grouped = {};
  TRANSITIONS_ORDER.forEach(k => grouped[k] = []);
  
  trans.forEach(t => {
    const key = `${t.from} → ${t.to}`;
    if (grouped[key]) grouped[key].push(t.days);
  });
  
  const tbody = document.getElementById('transBody');
  tbody.innerHTML = TRANSITIONS_ORDER.map(k => {
    const arr = grouped[k];
    const n = arr.length;
    const mean100 = getPercentileMean(arr, 100);
    const meanPctl = getPercentileMean(arr, currentPctl);
    const median = getMedian(arr);
    const pctlVal = getPctlValue(arr, currentPctl);
    const delta = (mean100 !== null && n > 0) ? (100 * (meanPctl - mean100) / mean100).toFixed(1) : '—';
    
    return `<tr>
      <td><b>${k}</b></td>
      <td>${n}</td>
      <td class="hl">${meanPctl === null ? '—' : meanPctl.toFixed(1)}</td>
      <td>${mean100 === null ? '—' : mean100.toFixed(1)}</td>
      <td>${delta === '—' ? '—' : delta + '%'}</td>
      <td>${median === null ? '—' : median.toFixed(1)}</td>
      <td>${pctlVal === null ? '—' : pctlVal.toFixed(1)}</td>
    </tr>`;
  }).join('');
}

function updateBlock3() {
  const trans = filterTransitions();
  const empData = {};
  
  trans.forEach(t => {
    if (!empData[t.assignee]) {
      empData[t.assignee] = { role: DATA.jira.role_of[t.assignee] || DATA.roster[t.assignee] || 'Не назначен', totalN: 0, trans: {} };
      TRANSITIONS_ORDER.forEach(k => empData[t.assignee].trans[k] = []);
    }
    const key = `${t.from} → ${t.to}`;
    if (empData[t.assignee].trans[key]) {
      empData[t.assignee].trans[key].push(t.days);
      empData[t.assignee].totalN++;
    }
  });
  
  const thead = document.getElementById('empHead');
  thead.innerHTML = `<th>Сотрудник</th><th>Роль</th><th>N (всего)</th>` + TRANSITIONS_ORDER.map(k => `<th title="${k}">${k.split(' → ')[0]} →</th>`).join('');
  
  // Calculate mins and maxes for highlighting
  const mins = {}, maxes = {};
  TRANSITIONS_ORDER.forEach(k => {
    let min = Infinity, max = -Infinity;
    Object.values(empData).forEach(emp => {
      const mean = getPercentileMean(emp.trans[k], currentPctl);
      if (mean !== null) {
        if (mean < min) min = mean;
        if (mean > max) max = mean;
      }
    });
    mins[k] = min; maxes[k] = max;
  });
  
  const tbody = document.getElementById('empBody');
  tbody.innerHTML = Object.keys(empData).sort().map(emp => {
    const d = empData[emp];
    const cells = TRANSITIONS_ORDER.map(k => {
      const mean = getPercentileMean(d.trans[k], currentPctl);
      if (mean === null) return `<td>—</td>`;
      let cls = '';
      if (mean === mins[k] && mins[k] !== maxes[k]) cls = 'min-val';
      else if (mean === maxes[k] && mins[k] !== maxes[k]) cls = 'max-val';
      return `<td class="${cls}">${mean.toFixed(1)}</td>`;
    }).join('');
    return `<tr><td><b>${emp}</b>${mergedMark(emp)}</td><td>${d.role}</td><td>${d.totalN}</td>${cells}</tr>`;
  }).join('');
}

function filterConfluence() {
  return DATA.confluence.pages.filter(p => {
    if (!fOk(filters.week, p._week_created)) return false;
    if (!fOk(filters.person, p.author_created)) return false;
    
    const role = p.role || DATA.roster[p.author_created] || 'Не назначен';
    if (!fOk(filters.role, role)) return false;
    
    return true;
  });
}

function updateBlock4() {
  const pages = filterConfluence();
  const weeksCount = {};
  DATA.confluence.weeks.forEach(w => weeksCount[w] = 0);
  
  pages.forEach(p => {
    if (weeksCount[p._week_created] !== undefined) weeksCount[p._week_created]++;
  });
  
  const labels = DATA.confluence.weeks.map(w => (DATA.week_labels && DATA.week_labels[w] ? w + ' (' + DATA.week_labels[w] + ')' : w));
  const data = DATA.confluence.weeks.map(w => weeksCount[w]);
  
  // Compute trend line data
  const trendData4 = computeTrendLine(data, DATA.confluence.weeks);

  if (chart4) chart4.destroy();
  const datasets4 = [{ label: 'Создано страниц', data, backgroundColor: '#4ade80' }];
  if (trendData4) {
    datasets4.push({
      label: 'Тренд',
      type: 'line',
      data: trendData4,
      borderColor: '#f59e0b',
      borderWidth: 2,
      borderDash: [4, 4],
      tension: 0,
      pointRadius: 0,
      fill: false,
      spanGaps: false,
      order: 0
    });
  }
  chart4 = new Chart(document.getElementById('chart-conf-pages'), {
    type: 'bar',
    data: {
      labels,
      datasets: datasets4
    },
    options: { responsive: true, maintainAspectRatio: false },
    plugins: [eventPlugin(DATA.confluence.weeks)]
  });
}

function updateBlock5() {
  // Use filterConfluence() to apply filters
  const pages = filterConfluence();
  const createdByWeek = {};
  const updatedByWeek = {};
  DATA.confluence.weeks.forEach(w => { createdByWeek[w] = 0; updatedByWeek[w] = 0; });
  
  // Count created and updated by week from filtered pages
  pages.forEach(p => {
    if (p._week_created && createdByWeek[p._week_created] !== undefined) createdByWeek[p._week_created]++;
    if (p._week_updated && updatedByWeek[p._week_updated] !== undefined) updatedByWeek[p._week_updated]++;
  });
  
  const labels = DATA.confluence.weeks.map(w => (DATA.week_labels && DATA.week_labels[w] ? w + ' (' + DATA.week_labels[w] + ')' : w));
  const createdData = DATA.confluence.weeks.map(w => createdByWeek[w]);
  const updatedData = DATA.confluence.weeks.map(w => updatedByWeek[w]);
  
  // Compute trend line data
  const totalData5 = labels.map((_, i) => createdData[i] + updatedData[i]);
  const trendData5 = computeTrendLine(totalData5, DATA.confluence.weeks);

  if (chart5) chart5.destroy();
  const datasets5 = [
    { label: 'Создано', data: createdData, backgroundColor: '#4ade80', stack: 's' },
    { label: 'Обновлено', data: updatedData, backgroundColor: '#38bdf8', stack: 's' }
  ];
  if (trendData5) {
    datasets5.push({
      label: 'Тренд',
      type: 'line',
      data: trendData5,
      borderColor: '#f59e0b',
      borderWidth: 2,
      borderDash: [4, 4],
      tension: 0,
      pointRadius: 0,
      fill: false,
      spanGaps: false,
      order: 0
    });
  }
  chart5 = new Chart(document.getElementById('chart-conf-stack'), {
    type: 'bar',
    data: {
      labels,
      datasets: datasets5
    },
    options: { responsive: true, maintainAspectRatio: false, scales: { x: { stacked: true }, y: { stacked: true } }, plugins: { legend: { display: true } } },
    plugins: [eventPlugin(DATA.confluence.weeks)]
  });
}

function updateAll() {
  updateBlock1();
  updateBlock2();
  updateBlock3();
  updateBlock4();
  updateBlock5();
}

// ============ ПЕРЕКЛЮЧАТЕЛЬ ПЕРИОДА ============
// Годов один — переключать нечего, и лишний элемент в шапке только мешает.
function useYear(y) {
  const pack = (DATA.years || {})[String(y)];
  if (!pack) return;
  YEAR_ACTIVE = Number(y);
  DATA.jira = pack.jira;
  DATA.confluence = pack.confluence;
  DATA.confluence.pages = DATA.pages || [];
  DATA.week_labels = pack.week_labels;
  stampPages(YEAR_ACTIVE);

  // Фильтры сбрасываем: выбранный сотрудник или неделя прошлого года в этом году
  // могут не существовать вовсе, и отчёт молча показал бы пустые графики.
  filters = { type: new Set(['__all']), role: new Set(['__all']),
              person: new Set(['__all']), week: new Set(['__all']) };
  document.querySelectorAll('[data-master]').forEach(m => { m.checked = true; });

  readDictionaries();
  fillFilterGroups();
  refreshDdLabels();

  document.getElementById('sub-year').textContent = String(y);
  document.querySelectorAll('#year-tabs button').forEach(b =>
    b.setAttribute('aria-pressed', String(Number(b.dataset.year) === YEAR_ACTIVE)));
  updateAll();
}

(function buildYearPicker() {
  const list = DATA.years_available || [];
  if (list.length < 2) return;
  const tabs = document.getElementById('year-tabs');
  list.forEach(y => {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.year = y;
    b.textContent = y;
    b.setAttribute('aria-pressed', String(y === YEAR_ACTIVE));
    b.addEventListener('click', () => useYear(y));
    tabs.appendChild(b);
  });
  document.getElementById('year-pick').hidden = false;
})();

refreshDdLabels();

// Стартовая подпись к выбранному персентилю (по умолчанию 95%)
document.getElementById('pctl-tag').textContent = `обрезка до ${currentPctl}% персентиля (отброшены верхние ${100-currentPctl}% значений-выбросов)`;

updateAll();
</script>
</body>
</html>
"""

def host(url):
    """Хост без схемы — в подзаголовке нужен адрес, а не ссылка."""
    return url.split("//", 1)[-1].rstrip("/") if url else ""


sources = " & ".join(x for x in (host(paths.jira()["base_url"]),
                                 host(paths.confluence()["base_url"])) if x)

html = html_template
for mark, value in (("__PROJECT__", PROJECT),
                    ("__YEAR__", str(YEAR)),
                    ("__SOURCES__", sources or "Jira &amp; Confluence"),
                    ("__BUILT_AT__", datetime.datetime.now().strftime("%d.%m.%Y %H:%M"))):
    html = html.replace(mark, value)
html = html.replace("__DATA__", json.dumps(DATA, ensure_ascii=False).replace("</", "<\\/"))

os.makedirs(os.path.dirname(paths.OUTPUT_PATH), exist_ok=True)
with open(paths.OUTPUT_PATH, "w", encoding="utf-8") as f:
    f.write(html)

print(f"Собран отчёт: {os.path.relpath(paths.OUTPUT_PATH, paths.PROJECT_ROOT)} "
      f"({os.path.getsize(paths.OUTPUT_PATH) // 1024} КБ)")
