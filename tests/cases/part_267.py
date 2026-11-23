"""part_267 - «Замеры»: точный дашборд usage.d и отдельная оценка timing.d.

Проверки не поднимают сервер: форма ответа `/api/usage` задана контрактом
1.231.18, а разметка и каталоги читаются с диска. Node исполняет чистые
функции раздела так же, как их зовёт браузер.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from harness import KIT, test

VIEW = KIT / "cockpit/modules/metrics/view.js"
HTML = KIT / "cockpit/modules/metrics/view.html"
RU = KIT / "cockpit/modules/metrics/i18n/ru.json"
EN = KIT / "cockpit/modules/metrics/i18n/en.json"


def _node(script: str) -> dict:
    node = shutil.which("node")
    assert node, ("part_267 требует Node.js: исполняемый файл node не найден в PATH. "
                  "Установите Node.js и повторите тест.")
    cp = subprocess.run([node, "--input-type=module", "-e", script],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=30, check=False)
    assert cp.returncode == 0, cp.stderr
    output = cp.stdout.strip().splitlines()
    assert output, "part_267: node не вернул JSON в stdout"
    try:
        return json.loads(output[-1])
    except json.JSONDecodeError as exc:
        raise AssertionError(f"part_267: node вернул не JSON: {output[-1]!r}") from exc


def _usage() -> dict:
    return {
        "schema": 2,
        "window": {"from": "2026-11-16", "to": "2026-11-22", "days": 7},
        "totals": {
            "source": "usage.d", "approximate": False, "runs": 2, "calls": 5,
            "tokens_in": 60, "tokens_out": 40, "tokens_total": 100,
            "cached_in": None, "reasoning_out": None, "tokens_per_run": 50,
            "tokens_all": 100,
        },
        "approx": {
            "source": "timing.d", "approximate": True, "runs": 9, "calls": 99,
            "tokens_in": 900, "tokens_out": 100, "tokens_total": 1000,
        },
        "previous": {"totals": {
            "tokens_total": 80, "tokens_in": 50, "tokens_out": 30,
            "runs": 2, "cached_in": None, "tokens_per_run": 40,
        }},
        "series": [
            {"at": "2026-11-17", "tokens_total": 30, "runs": 1, "calls": 3},
            {"at": "2026-11-16", "tokens_total": 20, "runs": 1, "calls": 2},
        ],
        "by_project": [
            {"project": "B", "tokens_total": 20, "share": 0.2, "runs": 1},
            {"project": "A", "tokens_total": 80, "share": 0.8, "runs": 1},
        ],
        "by_model": [
            {"model": "m", "calls": 5, "tokens_in": 60, "tokens_out": 40,
             "tokens_total": 100, "share": 1},
        ],
        "by_provider": [
            {"provider": "go", "provider_name": "Gateway", "calls": 5,
             "tokens_total": 100, "share": 1, "key_present": None},
            {"provider": "local", "calls": 1, "tokens_total": 0,
             "share": 0, "key_present": False},
        ],
        "by_role": [{"role": "worker", "calls": 5, "tokens_total": 100, "share": 1}],
        "quality": {"records": 2, "approx_records": 2, "empty": False,
                    "truncated": False, "cached_known": False},
        "options": {"projects": ["A", "B"], "models": ["m"],
                    "providers": ["go", "local"], "roles": ["worker"]},
    }


@test
def test_metrics_cards_use_exact_totals_and_keep_the_estimate_separate(_tmp: Path):
    """Шесть карточек берут только usage.d; timing.d остаётся отдельной оценкой."""
    script = f"""
import {{exactCards, approxValues, previousValue, seriesPoints,
        keyStatus, isOldServer, sortRows}} from {json.dumps(VIEW.as_uri())};
const usage = {json.dumps(_usage(), ensure_ascii=False)};
const cards = exactCards(usage);
const empty = {{schema: 2, totals: {{tokens_total: 0, tokens_in: 0, tokens_out: 0,
  runs: 0, cached_in: null, tokens_per_run: null}}, quality: {{empty: true}}}};
const approxZero = {{approx: {{runs: 0, calls: 0, tokens_in: 0, tokens_out: 0,
  tokens_total: 0}}, quality: {{approx_records: 1}}}};
const approxNoRecords = {{approx: {{runs: 9, calls: 99, tokens_in: 900,
  tokens_out: 100, tokens_total: 1000}}, quality: {{approx_records: 0}}}};
const approxPresent = {{approx: {{runs: 1, calls: 2, tokens_in: 3, tokens_out: 4,
  tokens_total: 7}}, quality: {{approx_records: 1}}}};
console.log(JSON.stringify({{
  ids: cards.map(card => card.id),
  values: cards.map(card => card.value),
  exact_total: cards.find(card => card.id === "total").value,
  approx: approxValues(usage),
  previous: previousValue(usage, "total"),
  points: seriesPoints(usage).map(point => point.label),
  keys: [keyStatus(true), keyStatus(false), keyStatus(null)],
  old: [isOldServer(null), isOldServer({{schema: 1}}), isOldServer({{schema: 2}})],
  sorted: sortRows(usage.by_project, "tokens_total", -1).map(row => row.project),
  empty_cards: exactCards(empty).map(card => card.value),
  approx_empty: approxValues(approxZero),
  approx_no_records: approxValues(approxNoRecords),
  approx_present: approxValues(approxPresent),
}}));
"""
    got = _node(script)
    assert got["ids"] == ["total", "in", "out", "cache", "runs", "per_run"], got
    assert got["values"] == [100, 60, 40, None, 2, 50], got
    assert got["exact_total"] == 100, "приблизительный блок попал в точный итог"
    assert got["approx"]["tokens_total"] == 1000, got
    assert got["approx"]["source"] == "timing.d" and got["approx"]["approximate"] is True, got
    assert got["previous"] == 80, got
    assert got["points"] == ["2026-11-16", "2026-11-17"], got
    assert got["keys"] == ["present", "absent", "unknown"], got
    assert got["old"] == [True, True, False], got
    assert got["sorted"] == ["A", "B"], got
    assert got["empty_cards"] == [None, None, None, None, None, None], got
    assert got["approx_empty"] is None, "нулевая оценка должна показывать пустой блок"
    assert got["approx_no_records"] is None, "approx_records=0 должен опустошать блок"
    assert got["approx_present"]["tokens_total"] == 7, got


@test
def test_metrics_filters_reset_and_localstorage_contract(_tmp: Path):
    """Фильтры уходят в запрос, сброс возвращает 7 дней, память — один ключ."""
    script = f"""
import {{parsePrefs, serializePrefs, resetPrefs, buildUsageQuery,
        compareAllowed}} from {json.dumps(VIEW.as_uri())};
const saved = parsePrefs({{days: 30, compare: true,
  filters: {{project: ["aurora"], model: ["m"], provider: ["go"], role: ["worker", "qa"]}}}});
const bad = parsePrefs("not json");
const long = parsePrefs({{days: 90, compare: true}});
console.log(JSON.stringify({{
  saved, query: buildUsageQuery(saved), bad, long,
  reset: resetPrefs(), keys: JSON.parse(serializePrefs(saved)),
  compare: [compareAllowed(1), compareAllowed(7), compareAllowed(30),
            compareAllowed(90), compareAllowed(0)],
}}));
"""
    got = _node(script)
    assert got["saved"]["days"] == 30 and got["saved"]["compare"] is True, got
    assert got["saved"]["filters"]["role"] == ["worker", "qa"], got
    assert got["query"].startswith("/api/usage?days=30&compare=1"), got["query"]
    for part in ("project=aurora", "model=m", "provider=go", "role=worker", "role=qa"):
        assert part in got["query"], (part, got["query"])
    assert got["bad"]["days"] == 7 and got["bad"]["compare"] is True, got["bad"]
    assert got["long"]["compare"] is False, got["long"]
    assert got["reset"]["days"] == 7 and got["reset"]["compare"] is True, got["reset"]
    assert got["reset"]["filters"] == {"project": [], "model": [], "provider": [], "role": []}, got
    assert got["keys"]["days"] == 30 and "filters" in got["keys"], got
    assert got["compare"] == [True, True, True, False, False], got

    source = VIEW.read_text(encoding="utf-8")
    assert 'const STORE_KEY = "aurora-metrics-v1"' in source
    assert "localStorage.getItem(STORE_KEY)" in source
    assert "localStorage.setItem(STORE_KEY" in source
    assert "AbortController" in source and "STATE.abort.abort()" in source
    assert "seq !== STATE.seq" in source, "ответ старого запроса не отбрасывается"
    assert "STATE.days = fresh.days" in source, "сброс не возвращает 7 дней"


@test
def test_metrics_markup_is_accessible_and_keeps_logs_on_old_server(_tmp: Path):
    """График, таблицы и details доступны; старый сервер прячет только верх."""
    html = HTML.read_text(encoding="utf-8")
    source = VIEW.read_text(encoding="utf-8")
    css = (KIT / "cockpit/ui/panel.css").read_text(encoding="utf-8")
    for node_id in ("metricsControls", "metricsMessage", "metricsDash",
                    "metricsOld", "metricsLogs"):
        assert f'id="{node_id}"' in html, node_id
    for token in ('role: "img"', 'tabindex: "0"', 'aria-sort', 'aria-pressed',
                  'caption', 'scope: "col"', 'details', 'data-approx', "aria-busy",
                  "host.hidden = STATE.oldServer", "metrics.old_server"):
        assert token in source, token
    assert 'class: "scroll-x", tabindex: "0", role: "region"' in source, \
        "прокручиваемая таблица должна быть доступна с клавиатуры"
    assert source.count("scrollBox(ctx") >= 5, "таблицы должны прокручиваться внутри себя"
    assert 'class: "grid metrics"' in source
    assert ".metrics{grid-template-columns:repeat(auto-fill,minmax(178px,1fr))}" in css, \
        "карточки должны сами перестраиваться на узкой ширине"
    assert "details" in source and "metrics.chart.table" in source, \
        "таблица значений должна быть под details"
    assert "oldServerCard" in source and "renderLogs(ctx, STATE.logs" in source, \
        "старый сервер не должен скрывать нижние логи"
    assert "keyStatus(row.key_present)" in source
    assert "metrics.key." in source
    assert not re.search(r"key_(?:mask|seq|prefix|tail|hash)", source), \
        "раздел не должен читать или показывать части ключа"
    assert not re.search(r"(?:color|background|border-color)\s*:\s*(?:#|rgba?\(|hsla?\()",
                         source), "цвет должен идти из токенов скина"
    for forbidden in ("createElementNS", "chart.js", "Chart.js", "from \"d3", "cost"):
        assert forbidden not in source, forbidden


@test
def test_metrics_i18n_has_both_catalogs_and_no_key_or_cost_words(_tmp: Path):
    """Оба каталога полны, различают оценку и точный итог, не говорят о деньгах."""
    ru = json.loads(RU.read_text(encoding="utf-8"))
    en = json.loads(EN.read_text(encoding="utf-8"))
    ru_keys = {key for key in ru if not key.startswith("_")}
    en_keys = {key for key in en if not key.startswith("_")}
    assert ru_keys == en_keys, (sorted(ru_keys - en_keys), sorted(en_keys - ru_keys))
    assert len(ru_keys) >= 150, len(ru_keys)
    assert "оценка по timing.d" in ru["metrics.approx_note"]
    assert "estimate from timing.d" in en["metrics.approx_note"]
    assert ru["metrics.key.present"] == "задан"
    assert ru["metrics.key.absent"] == "не задан"
    assert ru["metrics.key.unknown"] == "неизвестно"
    assert ru["metrics.old_server"] and en["metrics.old_server"]
    for catalog in (ru, en):
        text = " ".join(catalog.values()).lower()
        for word in ("cost", "стоимост", "валюта", "currency", "price"):
            assert word not in text, word


@test
def test_metrics_services_filter_is_keyboard_ready_and_persisted(_tmp: Path):
    """Фильтр сервисов: чистка строк, подпись из каталога и сохранение выбора."""
    script = f"""
import {{filterServices, parsePrefs, serializePrefs, resetPrefs}}
  from {json.dumps(VIEW.as_uri())};
const services = {{llm: {{calls: 3}}, web: {{calls: 2}}, custom: {{calls: 1}}}};
const saved = parsePrefs({{service: "web"}});
const legacy = parsePrefs({{serviceFilter: "llm"}});
console.log(JSON.stringify({{
  all: Object.keys(filterServices(services, "")),
  one: Object.keys(filterServices(services, "web")),
  none: Object.keys(filterServices(services, "missing")),
  saved: saved.serviceFilter,
  serialized: JSON.parse(serializePrefs(saved)).service,
  legacy: legacy.serviceFilter,
  reset: resetPrefs().serviceFilter,
}}));
"""
    got = _node(script)
    assert got["all"] == ["llm", "web", "custom"], got
    assert got["one"] == ["web"], got
    assert got["none"] == [], got
    assert got["saved"] == "web", got
    assert got["serialized"] == "web", got
    assert got["legacy"] == "llm", got
    assert got["reset"] == "", got

    source = VIEW.read_text(encoding="utf-8")
    for token in ("export function filterServices", 'id: "metricsServiceFilter"',
                  '"aria-label": label', "STATE.serviceFilter = select.value",
                  'ctx.t("metrics.filter_service")', "filterServices(all, selected)"):
        assert token in source, token
    ru = json.loads(RU.read_text(encoding="utf-8"))
    en = json.loads(EN.read_text(encoding="utf-8"))
    assert ru["metrics.filter_service"] == "Сервис", ru.get("metrics.filter_service")
    assert en["metrics.filter_service"] == "Service", en.get("metrics.filter_service")


@test
def test_metrics_log_helpers_are_exported_for_the_panel_tests(_tmp: Path):
    """Чистые помощники логов остаются доступными тестам панели."""
    script = f"""
import * as view from {json.dumps(VIEW.as_uri())};
const names = ["statCells", "serviceKeys", "filterRuns", "runNote"];
console.log(JSON.stringify(Object.fromEntries(
  names.map(name => [name, typeof view[name]]))));
"""
    got = _node(script)
    assert got == {"statCells": "function", "serviceKeys": "function",
                   "filterRuns": "function", "runNote": "function"}, got
