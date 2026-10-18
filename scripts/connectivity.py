#!/usr/bin/env python3
"""connectivity.py — доступность соединений: профили, probe, состояние и сервис для остального движка.

Часть функций кита живёт за VPN, часть — в интернете, часть — только на этом компьютере. Раньше кит
о связи не знал ничего: оборванное соединение выглядело как «таймаут источника» или «провайдер не
ответил», и движок гнал по кольцу шлюзов, сажал их в карантин на пятнадцать минут и ждал вслепую.
Здесь связь описана явно и проверяется до работы, а не по её следам.

Понятия:
- **профиль** — имя канала (`VPN-1`). Сам по себе ничего не проверяет;
- **probe** — конкретная проверка: адрес, отвечающий только когда связь есть. TCP (порт открыт),
  HTTP/HTTPS (сервис отвечает ожидаемым статусом и телом) или ICMP (`ping`, он нередко закрыт);
- **правило профиля** — как из ответов его probe получается «доступен»: `any_healthy`, `quorum(K)`,
  `all_healthy`; профиль меняет состояние только после N неудач или M успехов подряд (антидребезг);
- **интернет** — встроенный профиль `internet`. Адреса для него задаёт только владелец: кит не ходит на
  чужие адреса без настройки; пока probe нет, интернет «не известен» (`unknown`), но не «нет».

Состояния профиля: `online`, `degraded` (правило выполнено, но не все probe отвечают), `offline`,
`unknown` (проверки не было или она старше трёх интервалов). Глобальное состояние считается по
**требуемым** зависимостям (включённые источники проекта и провайдеры моделей): `online`, `degraded`,
`partial` (часть недоступна), `no_vpn` (интернет есть, а все нужные профили лежат), `offline` (нет
интернета), `unknown`.

Хранение. Профили и probe — на машине, в `<кит>/local/connectivity.json` (в git не попадает; секретов
в файле нет). Состояние проверок — `~/.aurora/connectivity-state.json`: его пишет и фоновая проверка
панели, и любой процесс, проверивший связь перед запуском, поэтому антидребезг живёт в нём. Править
настройку руками не нужно: всё делается в панели, раздел «Проверка соединения».

Функция выключена, пока владелец её не включил (`enabled`): без настройки поведение кита прежнее.

  python3 .aurora/scripts/connectivity.py --status [--json] [--targets]   # состояние и причины
  python3 .aurora/scripts/connectivity.py --probe [--only ID|ПРОФИЛЬ]     # проверить сейчас

Панель: `net:status`, `net:probe`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations
import aurora_common  # noqa: F401 — защита версии Python: на старом — понятный текст, а не traceback

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на эмодзи и «—»; движок говорит по-русски и пишет UTF-8.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from aurora_common import lock_release, lock_take, parse_time, replace_file, utc_slug, utc_stamp  # noqa: E402

VERSION = 1
CONFIG_FILE = ("local", "connectivity.json")
STATE_NAME = "connectivity-state.json"
INTERNET = "internet"                 # встроенный профиль: интернет вообще
PROVIDER_PREFIX = "provider:"         # служебный профиль проверки ручки провайдера (в настройке не бывает)
PROBE_TYPES = ("tcp", "http", "icmp")
AGGREGATES = ("any_healthy", "quorum", "all_healthy")
POLICIES = ("fail_fast", "partial", "queue", "partial_queue")
POLICY_DEFAULT = "partial_queue"      # автоматические прогоны: сделать доступное, остальное отложить и повторить
STATES = ("online", "degraded", "offline", "unknown")
GLOBAL_STATES = ("online", "degraded", "partial", "no_vpn", "offline", "unknown")
MAX_PROBES = 10                       # на кит целиком
DEFAULTS = {"interval": 60, "timeout": 3, "fail_after": 3, "ok_after": 2}
LIMITS = {"interval": (10, 86400), "timeout": (1, 30), "fail_after": (1, 20), "ok_after": (1, 20),
          "quorum": (1, MAX_PROBES)}
UNKNOWN_AFTER = 3                     # данные старше стольких интервалов — состояние unknown
STALE_GRACE = 30                      # плюс столько секунд: раунд идёт не мгновенно (ход фоновой проверки, срок probe)
MIN_GAP = 0.5                         # раунд, пришедший раньше половины интервала после прежнего, не считается новым:
#                                       три нажатия «Проверить сейчас» подряд — это одна оценка, а не три неудачи подряд
FRESH_FOR = 1                         # перед запуском состояние моложе одного интервала берётся из кэша
PRELAUNCH_BUDGET = 3.0                # общий срок предзапускной проверки, с
HISTORY = 32                          # сколько последних оценок профиля помнит антидребезг
EVENTS_KEEP = 200                     # журнал смен состояния в файле состояния
BODY_LIMIT = 65536                    # сколько тела ответа читает HTTP-probe
STATUS_DEFAULT = ["2xx", "3xx"]
LEVEL = {"offline": 0, "degraded": 1, "online": 2}
LEVEL_NAME = {0: "offline", 1: "degraded", 2: "online"}
TOP_KEYS = ("version", "enabled", "settings", "policy", "profiles", "internet", "probes")
SERVER_KEYS = ("notes", "error", "no_kit")       # поля ответа сервера: в файл не пишутся ни при каком обороте страницы
PROFILE_KEYS = ("name", "aggregate", "quorum", "fail_after", "ok_after", "note")
PROBE_KEYS = ("id", "profile", "type", "host", "port", "url", "expect_status", "expect_body", "timeout",
              "interval", "label")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")
PROBE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")


def valid_name(name) -> bool:
    """Имя профиля — метка, а не адрес (`aurora_common.profile_name_ok`): IP и составные имена хостов не принимаются."""
    from aurora_common import profile_name_ok
    return profile_name_ok(name)


HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?$")
STATUS_RE = re.compile(r"^[1-5]xx$")


# ------------------------------------------------------------------ пути

def kit_dir(kit=None) -> Path | None:
    """Кит: переданный явно либо тот, из которого запущен движок (в проекте — по указателю `.aurora/kit_path.txt`)."""
    if kit:
        return Path(kit)
    from aurora_common import kit_root
    return kit_root()


def config_path(kit=None) -> Path | None:
    k = kit_dir(kit)
    return k.joinpath(*CONFIG_FILE) if k else None


def state_path() -> Path:
    """Состояние проверок — общее на машину, как `adapter-alarm.json`. Тесты подменяют HOME."""
    forced = os.environ.get("AURORA_CONNECTIVITY_STATE")
    if forced:
        return Path(forced)
    return Path(os.path.expanduser("~")) / ".aurora" / STATE_NAME


# ------------------------------------------------------------------ настройка

def empty() -> dict:
    """Настройка по умолчанию: выключена, профилей и probe нет."""
    return {"version": VERSION, "enabled": False, "settings": dict(DEFAULTS),
            "policy": {"auto": POLICY_DEFAULT}, "profiles": [],
            "internet": {"aggregate": "any_healthy", "quorum": 1}, "probes": []}


def _num(value, low: int, high: int):
    """Целое в пределах или None. Булево и дробь числом не считаются."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None
        value = int(value)
    if isinstance(value, str) and re.fullmatch(r"\d{1,6}", value.strip()):
        value = int(value.strip())
    if isinstance(value, int) and low <= value <= high:
        return value
    return None


def valid_host(host) -> bool:
    """Имя или адрес для TCP/ICMP: без схемы, пробелов и начального «-» (имя уходит в команду `ping`)."""
    h = str(host or "")
    if not h or len(h) > 253 or h != h.strip() or h.startswith("-"):
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    if h.endswith("."):
        h = h[:-1]
    return bool(h) and all(HOST_LABEL_RE.match(part) for part in h.split("."))


def valid_url(url) -> bool:
    """Адрес для HTTP-probe: http(s), хост, без учётных данных в адресе и пробелов."""
    u = str(url or "")
    if not u or len(u) > 1000 or u != u.strip() or re.search(r"\s", u) or any(ord(c) < 32 for c in u):
        return False
    try:
        p = urllib.parse.urlsplit(u)
        p.port        # noqa: B018 — разбор порта бросает ValueError на мусоре
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.hostname) and not p.username and not p.password


def _statuses(value):
    """Список ожидаемых статусов → (список, ошибка). Элемент — число 100..599 или «2xx»."""
    if value is None or value == "" or value == []:
        return list(STATUS_DEFAULT), ""
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        value = [p for p in re.split(r"[\s,;]+", str(value)) if p]
    if not isinstance(value, list) or not value or len(value) > 10:
        return list(STATUS_DEFAULT), "bad"
    out = []
    for item in value:
        s = str(item).strip().lower()
        if STATUS_RE.match(s):
            out.append(s)
        elif re.fullmatch(r"[1-5]\d\d", s):
            out.append(int(s))
        else:
            return list(STATUS_DEFAULT), "bad"
    return out, ""


def status_matches(code: int, expect: list) -> bool:
    """Подходит ли код ответа под ожидаемые: число точно, «2xx» — по сотне, «lt500» — любой ниже 500."""
    for e in expect:
        if e == "lt500" and code < 500:
            return True
        if isinstance(e, int) and e == code:
            return True
        if isinstance(e, str) and STATUS_RE.match(e) and code // 100 == int(e[0]):
            return True
    return False


def _problem(problems: list, code: str, where: str, text: str) -> None:
    problems.append({"code": code, "where": where, "text": text})


def clean(data) -> tuple:
    """Привести настройку к схеме → (настройка, [проблемы]).

    Проблема — `{code, where, text}`: код стабилен, по нему панель берёт строку из каталога языка. При чтении файла
    проблемы только называются (негодное отброшено или заменено умолчанием), при записи из панели — отказ.
    Неизвестные поля — верхнего уровня, профилей и probe — переносятся как есть: файл может нести то,
    что знает более новый кит или рука человека.
    """
    problems: list = []
    raw = data if isinstance(data, dict) else {}
    if data is not None and not isinstance(data, dict):
        _problem(problems, "bad_config", "", "настройка соединений — не объект")
    out = {k: v for k, v in raw.items() if k not in TOP_KEYS and k not in SERVER_KEYS}
    out["version"] = VERSION
    flag = raw.get("enabled", False)
    if flag is not None and not isinstance(flag, bool):
        _problem(problems, "bad_flag", "enabled", "«включено» — true или false")
    out["enabled"] = flag is True

    src = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    if raw.get("settings") is not None and not isinstance(raw.get("settings"), dict):
        _problem(problems, "bad_settings", "settings", "общие настройки — не объект")
    st = {k: v for k, v in src.items() if k not in DEFAULTS}
    for key, default in DEFAULTS.items():
        if key not in src or src[key] in (None, ""):
            st[key] = default
            continue
        n = _num(src[key], *LIMITS[key])
        if n is None:
            _problem(problems, "bad_" + key, "settings." + key,
                     f"«{key}» — целое от {LIMITS[key][0]} до {LIMITS[key][1]}")
        st[key] = default if n is None else n
    out["settings"] = st

    pol = raw.get("policy") if isinstance(raw.get("policy"), dict) else {}
    po = {k: v for k, v in pol.items() if k != "auto"}
    auto = pol.get("auto", POLICY_DEFAULT)
    if auto in (None, ""):
        auto = POLICY_DEFAULT
    if auto not in POLICIES:
        _problem(problems, "bad_policy", "policy.auto", "политика — fail_fast, partial, queue или partial_queue")
        auto = POLICY_DEFAULT
    po["auto"] = auto
    out["policy"] = po

    names: dict = {}            # имя в нижнем регистре → имя как записано
    profiles = []
    items = raw.get("profiles")
    if items is not None and not isinstance(items, list):
        _problem(problems, "bad_profiles", "profiles", "профили — список")
        items = []
    for i, p in enumerate(items or []):
        where = f"profiles[{i}]"
        if not isinstance(p, dict):
            _problem(problems, "bad_profile", where, "профиль — не объект")
            continue
        name = str(p.get("name") or "").strip()
        if not valid_name(name):
            _problem(problems, "bad_name", where + ".name",
                     "имя профиля — короткая метка: латиница, цифры, «_», «-», не больше одной «.»; до 40 знаков, "
                     "начинается с буквы или цифры; адрес сюда не пишут")
            continue
        low = name.lower()
        if low == INTERNET or low.startswith(PROVIDER_PREFIX):
            _problem(problems, "reserved_name", where + ".name", f"имя «{name}» занято самим китом")
            continue
        if low in names:
            _problem(problems, "dup_profile", where + ".name", f"профиль «{name}» уже есть")
            continue
        names[low] = name
        profiles.append(_clean_rule(p, name, where, problems))
    out["profiles"] = profiles

    net = raw.get("internet") if isinstance(raw.get("internet"), dict) else {}
    if raw.get("internet") is not None and not isinstance(raw.get("internet"), dict):
        _problem(problems, "bad_internet", "internet", "правило интернета — не объект")
    out["internet"] = _clean_rule(net, None, "internet", problems)

    out["probes"] = _clean_probes(raw.get("probes"), names, problems)
    return out, problems


def _clean_rule(p: dict, name, where: str, problems: list) -> dict:
    """Правило профиля: как из ответов probe получается «доступен», и свои пороги антидребезга (по умолчанию общие)."""
    row = {k: v for k, v in p.items() if k not in PROFILE_KEYS}
    if name is not None:
        row["name"] = name
    agg = p.get("aggregate", "any_healthy")
    if agg in (None, ""):
        agg = "any_healthy"
    if agg not in AGGREGATES:
        _problem(problems, "bad_aggregate", where + ".aggregate", "правило — any_healthy, quorum или all_healthy")
        agg = "any_healthy"
    row["aggregate"] = agg
    if agg == "quorum" or p.get("quorum") not in (None, ""):
        q = _num(p.get("quorum", 1), *LIMITS["quorum"])
        if q is None:
            _problem(problems, "bad_quorum", where + ".quorum", f"кворум K — целое от 1 до {MAX_PROBES}")
            q = 1
        row["quorum"] = q
    elif agg != "quorum":
        row["quorum"] = 1
    for key in ("fail_after", "ok_after"):
        if p.get(key) in (None, ""):
            continue
        n = _num(p[key], *LIMITS[key])
        if n is None:
            _problem(problems, "bad_" + key, where + "." + key, f"«{key}» — целое от 1 до 20")
        else:
            row[key] = n
    note = p.get("note")
    if isinstance(note, str) and note.strip():
        row["note"] = " ".join(note.split())[:200]
    return row


def _clean_probes(items, profiles: dict, problems: list) -> list:
    if items is not None and not isinstance(items, list):
        _problem(problems, "bad_probes", "probes", "probe — список")
        items = []
    out, ids = [], set()
    for i, p in enumerate(items or []):
        where = f"probes[{i}]"
        if not isinstance(p, dict):
            _problem(problems, "bad_probe", where, "probe — не объект")
            continue
        if len(out) >= MAX_PROBES:
            _problem(problems, "probe_limit", where, f"на кит не больше {MAX_PROBES} probe")
            break
        row = {k: v for k, v in p.items() if k not in PROBE_KEYS and not str(k).startswith("_")}
        pid = str(p.get("id") or "").strip()
        if pid and not PROBE_ID_RE.match(pid):
            _problem(problems, "bad_probe_id", where + ".id", "идентификатор: a-z, 0-9, «_», «-»; до 24 знаков")
            continue
        if not pid:
            n = 1
            while f"p{n}" in ids:
                n += 1
            pid = f"p{n}"
        if pid in ids:
            _problem(problems, "dup_probe", where + ".id", f"probe «{pid}» уже есть")
            continue
        profile = str(p.get("profile") or "").strip()
        if profile.lower() == INTERNET:
            profile = INTERNET
        elif profile.lower() not in profiles:
            _problem(problems, "unknown_profile", where + ".profile", f"профиля «{profile}» нет")
            continue
        else:
            profile = profiles[profile.lower()]
        kind = p.get("type")
        if kind not in PROBE_TYPES:
            _problem(problems, "bad_type", where + ".type", "тип проверки — tcp, http или icmp")
            continue
        row.update(id=pid, profile=profile, type=kind)
        if kind in ("tcp", "icmp"):
            host = str(p.get("host") or "").strip()
            if not valid_host(host):
                _problem(problems, "bad_host", where + ".host", "адрес — имя хоста или IP без схемы и пробелов")
                continue
            row["host"] = host
            if kind == "tcp":
                port = _num(p.get("port"), 1, 65535)
                if port is None:
                    _problem(problems, "bad_port", where + ".port", "порт — целое от 1 до 65535")
                    continue
                row["port"] = port
        else:
            url = str(p.get("url") or "").strip()
            if not valid_url(url):
                _problem(problems, "bad_url", where + ".url",
                         "адрес — http:// или https:// и хост, без логина и пароля в адресе")
                continue
            row["url"] = url
            exp, bad = _statuses(p.get("expect_status"))
            if bad:
                _problem(problems, "bad_status", where + ".expect_status",
                         "ожидаемые статусы — числа 100..599 или «2xx»; до десяти")
            row["expect_status"] = exp
            body = p.get("expect_body")
            if isinstance(body, str) and body:
                if len(body) > 200 or re.search(r"[\x00-\x08\x0b-\x1f]", body):
                    _problem(problems, "bad_body", where + ".expect_body", "ожидаемый текст — до 200 знаков без управляющих")
                else:
                    row["expect_body"] = body
        for key in ("timeout", "interval"):
            if p.get(key) in (None, ""):
                continue
            n = _num(p[key], *LIMITS[key])
            if n is None:
                _problem(problems, "bad_" + key, f"{where}.{key}",
                         f"«{key}» — целое от {LIMITS[key][0]} до {LIMITS[key][1]}")
            else:
                row[key] = n
        label = p.get("label")
        if isinstance(label, str) and label.strip():
            row["label"] = " ".join(label.split())[:80]
        ids.add(pid)
        out.append(row)
    return out


def digest(data) -> str:
    """Слепок настройки как есть: сравнивается при записи, чтобы не затереть чужую правку."""
    blob = json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def file_stamp(kit=None) -> str:
    """Слепок файла настройки (`expect` панели): нет файла — пусто."""
    path = config_path(kit)
    if path is None:
        return ""
    from aurora_common import personal_kit_file
    if personal_kit_file(path):
        return ""                   # прогон тестов: личная настройка машины не читается даже для слепка
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:32]
    except OSError:
        return ""


def load(kit=None) -> dict:
    """Настройка кита. Нет файла — выключенная; битый файл не переписывается, а называется в `error`."""
    path = config_path(kit)
    if path is None:
        out = empty()
        out["no_kit"] = True        # копия движка без указателя на кит: настройки машины ей не видны
        return out
    from aurora_common import personal_kit_file
    if personal_kit_file(path):
        return empty()              # прогон тестов: личная настройка машины в него не попадает
    if not path.is_file():
        return empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out = empty()
        out["error"] = f"{path}: не разобран ({e}) — исправьте файл или удалите его"
        return out
    try:
        out, problems = clean(data)
    except Exception as e:  # noqa: BLE001 — рука человека не должна ронять панель: файл называется негодным
        out = empty()
        out["error"] = f"{path}: настройка не разобрана ({type(e).__name__}) — исправьте файл или удалите его"
        return out
    if problems:
        out["notes"] = [p["text"] for p in problems]
    return out


def save(kit, data, expect=None, replace_broken: bool = False) -> dict:
    """Записать настройку неделимо. → {ok, stamp} либо {ok: False, error, code, errors[, conflict]}.

    `expect` — слепок, с которым страница открыла настройку: файл за это время менялся — отказ (`conflict`).
    Неизвестные разделы файла и неизвестные поля профилей и probe, которых страница не прислала, переносятся
    из прежнего файла как есть: форма не стирает то, чем не управляет. Прежний файл, который не разобрать,
    заменяется только по явной просьбе (`replace_broken`): возможно, в нём правка человека.
    """
    path = config_path(kit)
    if path is None:
        return {"ok": False, "code": "no_kit", "error": "кит не найден", "errors": []}
    from aurora_common import personal_kit_file
    if personal_kit_file(path):
        return {"ok": False, "code": "tests", "errors": [],
                "error": "прогон тестов не пишет личную настройку машины"}
    try:
        new, problems = clean(data)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": "bad_config", "errors": [], "error": f"настройка не разобрана ({type(e).__name__})"}
    if problems:
        return {"ok": False, "code": problems[0]["code"], "error": problems[0]["text"], "errors": problems}
    root = Path(kit)
    if (root / "aurora.config.yaml").is_file() and not (root / "scripts" / "connectivity.py").is_file():
        return {"ok": False, "code": "not_kit", "errors": [],
                "error": f"{root} — папка проекта, а не кита: настройка пишется только в кит"}
    lock = str(path) + ".lock"
    for _ in range(100):
        got, _held = lock_take(lock, stale_s=60)
        if got:
            break
        time.sleep(0.05)
    else:
        return {"ok": False, "code": "busy", "errors": [], "error": "файл настройки занят другим процессом"}
    try:
        if expect is not None and expect != file_stamp(kit):
            return {"ok": False, "conflict": True, "code": "conflict", "errors": [],
                    "error": "настройка изменилась на диске с момента открытия страницы"}
        old = {}
        if path.is_file():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                if not replace_broken:
                    return {"ok": False, "code": "broken_file", "errors": [],
                            "error": "прежний файл настройки не разобран; сохранение заменит его целиком"}
                try:                       # рядом остаётся копия: в файле могла быть правка руками
                    path.with_name(f"{path.name}.broken-{utc_slug()}").write_bytes(path.read_bytes())
                except OSError:
                    pass
                old = {}
        _carry(old if isinstance(old, dict) else {}, new, data if isinstance(data, dict) else {})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            replace_file(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        stamp = file_stamp(kit)
    except (OSError, ValueError) as e:
        return {"ok": False, "code": "write", "errors": [], "failed": True,
                "error": f"не удалось записать настройку: {getattr(e, 'strerror', None) or type(e).__name__}"}
    finally:
        lock_release(lock)
    return {"ok": True, "stamp": stamp}


def _carry(old: dict, new: dict, sent: dict) -> None:
    """Перенести из прежнего файла то, чего страница не знает и не прислала: разделы верхнего уровня и
    лишние поля профилей и probe (по имени и идентификатору)."""
    for key, value in old.items():
        if key not in TOP_KEYS and key not in sent:
            new[key] = value
    for coll, ident, known in (("profiles", "name", PROFILE_KEYS), ("probes", "id", PROBE_KEYS)):
        was = {str(x.get(ident)): x for x in old.get(coll) or [] if isinstance(x, dict)}
        for row in new.get(coll, []):
            prev = was.get(str(row.get(ident))) or {}
            for key, value in prev.items():
                if key not in known and key not in row:
                    row[key] = value


# ------------------------------------------------------------------ правила и пороги

def rule_of(cfg: dict, profile: str) -> dict:
    """{aggregate, quorum, fail_after, ok_after} профиля с общими умолчаниями. Служебный профиль провайдера —
    «хоть один ответ»."""
    st = cfg.get("settings") or DEFAULTS
    if profile == INTERNET:
        row = cfg.get("internet") or {}
    else:
        row = next((p for p in cfg.get("profiles") or [] if p.get("name") == profile), {})
    return {"aggregate": row.get("aggregate", "any_healthy"), "quorum": row.get("quorum", 1),
            "fail_after": row.get("fail_after") or st.get("fail_after", DEFAULTS["fail_after"]),
            "ok_after": row.get("ok_after") or st.get("ok_after", DEFAULTS["ok_after"])}


def probe_timeout(cfg: dict, p: dict) -> int:
    return p.get("timeout") or (cfg.get("settings") or {}).get("timeout") or DEFAULTS["timeout"]


def probe_interval(cfg: dict, p: dict) -> int:
    return p.get("interval") or (cfg.get("settings") or {}).get("interval") or DEFAULTS["interval"]


def profile_interval(cfg: dict, probes: list) -> int:
    """Как часто проверять профиль: чаще всех его probe."""
    return min([probe_interval(cfg, p) for p in probes] or [(cfg.get("settings") or {}).get("interval")
                                                             or DEFAULTS["interval"]])


def probes_of(cfg: dict, profile: str) -> list:
    return [p for p in cfg.get("probes") or [] if p.get("profile") == profile]


def rule_text(rule: dict) -> str:
    if rule["aggregate"] == "quorum":
        return f"quorum({rule['quorum']})"
    return rule["aggregate"]


def level_of(rule: dict, healthy: int, total: int) -> int:
    """Оценка раунда: 2 — все отвечают, 1 — правило выполнено, но не все, 0 — правило не выполнено."""
    if total <= 0:
        return 0
    agg = rule["aggregate"]
    need = 1 if agg == "any_healthy" else total if agg == "all_healthy" else min(max(1, rule["quorum"]), total)
    if healthy < need:
        return 0
    return 2 if healthy == total else 1


def advance(entry, level: int, rule: dict, now: float, interval: int, sig: str = "", replace_last: bool = False):
    """Один раунд антидребезга → (новая запись профиля, прежнее состояние или None, если не менялось).

    Состояние держится, пока N раундов подряд не подтвердили худшее (`fail_after`) или M раундов подряд лучшее
    (`ok_after`): падаем на самую высокую из оценок последних N раундов, поднимаемся на самую низкую из
    последних M. Неизвестное состояние (проверок не было, данные старше трёх интервалов, изменилась настройка
    профиля) снимает первый же раунд: сравнивать не с чем.
    """
    prev = None
    known = (isinstance(entry, dict) and entry.get("state") in LEVEL and entry.get("sig", "") == sig
             and isinstance(entry.get("hist"), list) and entry["hist"]
             and _age(entry.get("round_at"), now) <= UNKNOWN_AFTER * interval + STALE_GRACE)
    stamp = utc_stamp(now)
    if not known:
        row = {"state": LEVEL_NAME[level], "since": stamp, "hist": [level]}
        prev = "unknown"
    else:
        row = dict(entry)
        hist = [int(x) for x in row["hist"] if x in (0, 1, 2)]
        hist = (hist[:-1] if replace_last and hist else hist) + [level]
        row["hist"] = hist[-HISTORY:]
        cur = LEVEL[row["state"]]
        last_n = row["hist"][-rule["fail_after"]:]
        last_m = row["hist"][-rule["ok_after"]:]
        new = cur
        if len(last_n) >= rule["fail_after"] and max(last_n) < cur:
            new = max(last_n)
        elif len(last_m) >= rule["ok_after"] and min(last_m) > cur:
            new = min(last_m)
        if new != cur:
            prev = row["state"]
            row["state"], row["since"] = LEVEL_NAME[new], stamp
    row.update(sig=sig, round_at=stamp, raw=LEVEL_NAME[level])
    return row, prev


def _age(stamp, now: float) -> float:
    t = parse_time(stamp)
    return float("inf") if t is None else max(0.0, now - t.timestamp())


def trend(entry) -> dict:
    """Что копится к смене состояния: сколько раундов подряд оценка хуже или лучше текущего состояния."""
    if not isinstance(entry, dict) or entry.get("state") not in LEVEL:
        return {}
    cur, down, up = LEVEL[entry["state"]], 0, 0
    for x in reversed(entry.get("hist") or []):
        if x < cur:
            down += 1
        else:
            break
    for x in reversed(entry.get("hist") or []):
        if x > cur:
            up += 1
        else:
            break
    return {"worse": down, "better": up}


# ------------------------------------------------------------------ проверки (probe)

_ERRNO_UNREACHABLE = {101, 113, 50, 51, 64, 65, 10050, 10051, 10064, 10065, 100, 112}   # данные ОС
_ERRNO_REFUSED = {111, 61, 10061}
_ERRNO_TIMEOUT = {110, 60, 10060}


def classify_error(exc) -> tuple:
    """Исключение сети → (код, подробность). Коды: dns, refused, timeout, unreachable, tls, other. Подробность —
    только то, что не переводится (текст самой библиотеки); объяснение по коду даёт тот, кто показывает."""
    import ssl
    reason = getattr(exc, "reason", None)
    if isinstance(exc, urllib.error.URLError) and reason is not None and not isinstance(exc, urllib.error.HTTPError):
        if isinstance(reason, str):
            return "other", "URLError"
        return classify_error(reason)
    if isinstance(exc, ssl.SSLError) or (isinstance(exc, ssl.CertificateError)):
        return "tls", str(getattr(exc, "reason", None) or exc)[:80]
    if isinstance(exc, socket.gaierror):
        return "dns", ""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout", ""
    if isinstance(exc, ConnectionRefusedError):
        return "refused", ""
    code = getattr(exc, "errno", None)
    win = getattr(exc, "winerror", None)
    for n in (code, win):
        if n in _ERRNO_REFUSED:
            return "refused", ""
        if n in _ERRNO_TIMEOUT:
            return "timeout", ""
        if n in _ERRNO_UNREACHABLE:
            return "unreachable", ""
    return "other", type(exc).__name__


def _result(ok: bool, started: float, code: str, detail: str = "") -> dict:
    return {"ok": ok, "code": code, "ms": int((time.monotonic() - started) * 1000), "detail": detail}


def _check_tcp(p: dict, timeout: float) -> dict:
    started = time.monotonic()
    try:
        with socket.create_connection((p["host"], int(p["port"])), timeout=timeout):
            pass
    except OSError as e:
        code, detail = classify_error(e)
        return _result(False, started, code, detail)
    return _result(True, started, "ok")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Переход по перенаправлению — это уже другой адрес: проверка отвечает за тот, что назван."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _check_http(p: dict, timeout: float) -> dict:
    started = time.monotonic()
    headers = {"User-Agent": "aurora-connectivity/1", "Accept": "*/*"}
    headers.update(p.get("_headers") or {})
    req = urllib.request.Request(p["url"], headers=headers, method="GET")
    handlers = [_NoRedirect()]
    if p.get("_headers") and p["url"].lower().startswith("http://"):
        # Проверка с ключом по открытому каналу идёт напрямую: прокси из окружения увидел бы ключ, и проверка мерила бы
        # прокси, а не провайдера. Без ключа (и по https, где прокси видит только туннель) прокси окружения соблюдается.
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    want = p.get("expect_status") or STATUS_DEFAULT
    body = b""
    try:
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:      # 3xx без перехода, 4xx, 5xx — тоже ответ
            resp = e
        try:
            status = int(getattr(resp, "status", None) or resp.getcode())
            if p.get("expect_body"):
                body = resp.read(BODY_LIMIT)
        finally:
            resp.close()
    except (OSError, ValueError) as e:
        code, detail = classify_error(e)
        for secret in (p.get("_headers") or {}).values():
            if secret and len(str(secret)) > 3:
                detail = detail.replace(str(secret), "***")        # ключ в тексте чужой ошибки не остаётся нигде
        return _result(False, started, code, detail)
    if not status_matches(status, want):
        return _result(False, started, "status", f"HTTP {status}")
    if p.get("expect_body") and p["expect_body"] not in body.decode("utf-8", "replace"):
        return _result(False, started, "body", f"HTTP {status}")
    return _result(True, started, "ok", f"HTTP {status}")


def icmp_argv(host: str, timeout: float, system: str | None = None) -> list:
    """Команда `ping` одним пакетом: Windows — `-n 1 -w мс`, macOS — `-c 1 -W мс`, Linux — `-c 1 -W с`."""
    system = system or sys.platform
    if system.startswith("win"):
        return ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    if system == "darwin":
        return ["ping", "-c", "1", "-W", str(int(timeout * 1000)), host]
    return ["ping", "-c", "1", "-W", str(max(1, int(timeout + 0.999))), host]


def _check_icmp(p: dict, timeout: float) -> dict:
    started = time.monotonic()
    extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if hasattr(subprocess, "CREATE_NO_WINDOW") else {}
    try:
        cp = subprocess.run(icmp_argv(p["host"], timeout), capture_output=True, text=True, errors="replace",
                            timeout=timeout + 2, **extra)
    except FileNotFoundError:
        return dict(_result(False, started, "no_ping"), inconclusive=True)         # нет программы ping — не «нет ответа»
    except subprocess.TimeoutExpired:
        return _result(False, started, "timeout")
    except OSError as e:
        return dict(_result(False, started, "other", type(e).__name__), inconclusive=True)
    text = (cp.stdout or "") + (cp.stderr or "")
    if cp.returncode == 0:
        # Windows отвечает кодом 0 и на «Destination host unreachable» от шлюза: настоящий ответ несёт TTL.
        if sys.platform.startswith("win") and "ttl=" not in text.lower():
            return _result(False, started, "unreachable")
        return _result(True, started, "ok")
    if re.search(r"unknown host|could not find host|cannot resolve|name or service not known|nodename nor", text, re.I):
        return _result(False, started, "dns")
    if cp.returncode >= 2 and not sys.platform.startswith("win"):
        # нет прав или ping не смог — не «нет ответа»: раунд не засчитывается
        return dict(_result(False, started, "other", f"ping: код {cp.returncode}"), inconclusive=True)
    return _result(False, started, "timeout")


CHECKERS = {"tcp": _check_tcp, "http": _check_http, "icmp": _check_icmp}


def probe_target(p: dict) -> str:
    """Куда смотрит probe — для показа: без пути и параметров адреса (в запросе бывают токены)."""
    if p.get("type") == "http":
        try:
            u = urllib.parse.urlsplit(p.get("url") or "")
            return f"{u.scheme}://{u.netloc}" + (u.path if u.path not in ("", "/") else "/")
        except ValueError:
            return ""
    host = p.get("host") or ""
    return f"{host}:{p['port']}" if p.get("type") == "tcp" and p.get("port") else host


def run_probe(p: dict, timeout: float, checkers=None) -> dict:
    fn = (checkers or CHECKERS).get(p.get("type"))
    if fn is None:
        return {"ok": False, "code": "other", "ms": 0, "detail": "unknown probe type"}
    try:
        res = fn(p, timeout)
    except Exception as e:  # noqa: BLE001 — проверка не должна ронять вызывающего
        # Сама проверка сломалась — это не свидетельство против адреса: раунд по ней не засчитывается (`inconclusive`).
        res = {"ok": False, "code": "other", "ms": 0, "detail": (type(e).__name__ + ": " + str(e))[:120], "inconclusive": True}
    return res


def try_probe(probe, settings=None, checkers=None) -> dict:
    """Одна проверка без записи в состояние — кнопка «Проверить» у probe, который ещё не сохранён.

    → {ok, code, ms, detail, target} либо {ok: False, problems: [...]}, если probe не годится. Профиль не нужен.
    """
    raw = dict(probe) if isinstance(probe, dict) else {}
    raw["profile"], raw["id"] = "draft", "draft"
    cfg, problems = clean({"settings": settings or {}, "profiles": [{"name": "draft"}], "probes": [raw]})
    if not cfg["probes"]:
        return {"ok": False, "problems": [x for x in problems if x["where"].startswith("probes")]}
    p = cfg["probes"][0]
    res = run_probe(p, probe_timeout(cfg, p), checkers)
    res["target"] = probe_target(p)
    return res


def run_probes(probes: list, cfg: dict, budget: float | None = None, checkers=None) -> dict:
    """Все probe разом, каждая в своём потоке → {id: результат}.

    С `budget` срок каждой probe урезается до 80 % бюджета: молчащий адрес успевает честно ответить «нет ответа» до конца
    общего срока. Не уложившаяся всё же (зависшее разрешение имени) — результат `late`: раунд профиля по ней не пишется,
    состояние остаётся прежним (не «известно, что лежит», а «не знаем»). Потоки служебные: выход процесса они не держат.
    """
    results: dict = {}
    lock = threading.Lock()
    cap = None if budget is None else max(0.5, budget * 0.8)

    def one(p: dict) -> None:
        timeout = probe_timeout(cfg, p)
        res = run_probe(p, timeout if cap is None else min(timeout, cap), checkers)
        with lock:
            results.setdefault(p["id"], res)

    threads = []
    for p in probes:
        t = threading.Thread(target=one, args=(p,), daemon=True)
        t.start()
        threads.append(t)
    end = None if budget is None else time.monotonic() + budget
    for t in threads:
        t.join(None if end is None else max(0.0, end - time.monotonic()))
    with lock:
        out = dict(results)
    for p in probes:
        out.setdefault(p["id"], {"ok": False, "code": "late", "ms": int((budget or 0) * 1000),
                                 "detail": "", "late": True})
    return out


# ------------------------------------------------------------------ состояние на диске

def _blank_state() -> dict:
    return {"version": VERSION, "probes": {}, "profiles": {}, "events": []}


def read_state(path=None) -> dict:
    try:
        data = json.loads(Path(path or state_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _blank_state()
    if not isinstance(data, dict):
        return _blank_state()
    out = _blank_state()
    for key in ("probes", "profiles"):
        if isinstance(data.get(key), dict):
            out[key] = {k: v for k, v in data[key].items() if isinstance(v, dict)}
    if isinstance(data.get("events"), list):
        out["events"] = [e for e in data["events"] if isinstance(e, dict)][-EVENTS_KEEP:]
    return out


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        replace_file(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def update_state(fn, path=None):
    """Прочитать состояние под замком, дать `fn` изменить, записать. → результат `fn`. Замок взять не вышло
    за несколько секунд — работаем без него: потерянная запись раунда лучше зависшей проверки."""
    path = Path(path or state_path())
    lock = str(path) + ".lock"
    held = False
    for _ in range(80):
        try:
            held, _info = lock_take(lock, stale_s=60)
        except OSError:
            break
        if held:
            break
        time.sleep(0.05)
    try:
        state = read_state(path)
        res = fn(state)
        state["events"] = state["events"][-EVENTS_KEEP:]
        try:
            _write_state(path, state)
        except OSError:
            pass            # состояние — подсказка, а не работа: не записалось, вызов от этого не падает
        return res
    finally:
        if held:
            lock_release(lock)


def provider_profile(pid: str) -> str:
    return PROVIDER_PREFIX + pid


def _sig(cfg: dict, profile: str, probes: list) -> str:
    """Подпись профиля: правило и список probe с их целями. Изменились — прежние оценки не годятся."""
    rule = rule_of(cfg, profile)
    parts = [rule["aggregate"], str(rule["quorum"]), str(rule["fail_after"]), str(rule["ok_after"])]
    for p in sorted(probes, key=lambda x: x["id"]):
        parts.append("|".join([p["id"], p.get("type", ""), probe_target(p), str(p.get("timeout") or ""),
                               ",".join(str(x) for x in p.get("expect_status") or []),
                               p.get("expect_body", "")]))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def apply_round(state: dict, cfg: dict, profile: str, probes: list, results: dict, now: float) -> list:
    """Записать раунд профиля в состояние → список событий (смены состояния).

    Если хоть одна probe не успела за общий срок (`late`) или не смогла выполниться (`inconclusive`: нет программы ping,
    сбой самой проверки), раунд профиля не пишется: оценка по обрывку — не оценка, а «нет программы» — не «нет связи».
    Раунд, пришедший раньше половины интервала после прежнего, заменяет прежнюю оценку, а не добавляется к ней."""
    stamp = utc_stamp(now)
    healthy, late = 0, False
    for p in probes:
        res = results.get(p["id"]) or {"ok": False, "code": "other", "ms": 0, "detail": ""}
        late = late or bool(res.get("late")) or bool(res.get("inconclusive"))
        state["probes"][p["id"]] = {"ok": None if (res.get("late") or res.get("inconclusive")) else bool(res.get("ok")),
                                    "code": res.get("code", ""),
                                    "ms": res.get("ms", 0), "detail": str(res.get("detail") or "")[:160], "at": stamp,
                                    "profile": profile}
        healthy += 1 if res.get("ok") else 0
    if late:
        return []
    rule = rule_of(cfg, profile)
    level = level_of(rule, healthy, len(probes))
    interval = profile_interval(cfg, probes)
    prev_entry = state["profiles"].get(profile)
    quick = isinstance(prev_entry, dict) and _age(prev_entry.get("round_at"), now) < MIN_GAP * interval
    row, prev = advance(prev_entry, level, rule, now, interval, _sig(cfg, profile, probes), replace_last=quick)
    row.update(healthy=healthy, total=len(probes))
    state["profiles"][profile] = row
    events = []
    if prev is not None and row["state"] != prev:
        events.append({"at": stamp, "scope": "profile", "name": profile, "from": prev, "to": row["state"],
                       "healthy": healthy, "total": len(probes)})
    state["events"].extend(events)
    return events


def prune_state(state: dict, cfg: dict, extra_profiles=()) -> None:
    """Убрать из состояния профили и probe, которых в настройке больше нет."""
    live = {p["name"] for p in cfg.get("profiles") or []} | {INTERNET} | set(extra_profiles)
    for name in [n for n in state["profiles"] if n not in live]:
        del state["profiles"][name]
    for pid in [i for i, r in state["probes"].items() if (r or {}).get("profile") not in live]:
        del state["probes"][pid]


def profile_state(entry, now: float, interval: int) -> str:
    """Состояние для показа: нет записи или данные старше трёх интервалов — unknown."""
    if not isinstance(entry, dict) or entry.get("state") not in LEVEL:
        return "unknown"
    return entry["state"] if _age(entry.get("round_at"), now) <= UNKNOWN_AFTER * interval + STALE_GRACE else "unknown"


# ------------------------------------------------------------------ провайдеры моделей

def is_local_host(url: str) -> bool:
    """Адрес на этом же компьютере: такому провайдеру не нужны ни интернет, ни VPN."""
    from aurora_common import is_loopback_host
    try:
        host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return False
    return bool(host) and is_loopback_host(host)


def providers_of(kit=None, models=None) -> list:
    """Провайдеры моделей кита для проверки доступности: [{id, name, url, key, profile, check, path, local}].

    Ключ нужен только проверке с авторизацией и дальше провайдера не уходит; в состояние и ответы панели он не
    попадает. В прогоне тестов личная настройка машины не читается (`model_config.load`).
    """
    if models is None:
        try:
            import model_config as MC
            k = kit_dir(kit)
            models = MC.load(k) if k else {}
        except Exception:  # noqa: BLE001 — нет настройки моделей: провайдеров нет
            models = {}
    out = []
    for p in (models or {}).get("providers") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        health = p.get("health") if isinstance(p.get("health"), dict) else {}
        out.append({"id": p["id"], "name": p.get("name") or p["id"], "url": p.get("url") or "",
                    "key": p.get("key") or "", "profile": str(p.get("vpn") or "").strip(),
                    "check": health.get("check") if health.get("check") in ("reach", "auth") else "off",
                    "path": str(health.get("path") or "/models"), "local": is_local_host(p.get("url") or "")})
    return out


def provider_probe(p: dict) -> dict | None:
    """Проверка ручки провайдера как внутренний HTTP-probe; ключ уходит только на хост самого провайдера и только
    по защищённому каналу (https, свой компьютер или явное разрешение владельца)."""
    if p.get("check") not in ("reach", "auth") or not p.get("url"):
        return None
    base = p["url"].rstrip("/")
    path = p.get("path") or "/models"
    url = base + (path if path.startswith("/") else "/" + path)
    if not valid_url(url):
        return None
    probe = {"id": provider_profile(p["id"]), "profile": provider_profile(p["id"]), "type": "http", "url": url,
             "expect_status": ["lt500"] if p["check"] == "reach" else ["2xx"]}
    key = str(p.get("key") or "").strip()
    if p["check"] == "auth" and not key:
        return None                       # проверка «с ключом» без ключа не проверка: неизвестно, а не «не прошла»
    if p["check"] == "auth" and key:
        # Ключ с управляющими знаками или не из latin-1 не годится в заголовок: библиотека назвала бы его в тексте ошибки.
        if any(ord(c) < 32 or ord(c) == 127 for c in key) or not key.isascii():
            return None
        p = dict(p, key=key)
        from aurora_common import allow_cleartext, cleartext_secret_problem
        if cleartext_secret_problem(url, allow_cleartext()):
            return None
        probe["_headers"] = {"Authorization": "Bearer " + p["key"]}
    return probe


# ------------------------------------------------------------------ сервис доступности

def global_state(profiles: dict, internet: str, monitored: bool, required: list, providers_down: int,
                 providers_unknown: int = 0) -> str:
    """Глобальное состояние по требуемым зависимостям.

    `profiles` — {имя: состояние}; `required` — имена нужных профилей; `internet` — состояние интернета,
    `monitored` — задан ли у него хоть один probe (нет — интернет в расчёт не входит).
    """
    if monitored and internet == "offline":
        return "offline"
    states = [profiles.get(n, "unknown") for n in required]
    down = [s for s in states if s == "offline"]
    if states and len(down) == len(states):
        # «Интернет есть, а VPN лежат» — утверждение об интернете: без подтверждения это просто неизвестно.
        return "no_vpn" if (not monitored or internet in ("online", "degraded")) else "unknown"
    if down or providers_down:
        return "partial"
    unknown = [s for s in states if s == "unknown"]
    if unknown or providers_unknown or (monitored and internet == "unknown"):
        return "unknown"
    if any(s == "degraded" for s in states) or (monitored and internet == "degraded"):
        return "degraded"
    return "online"


class AvailabilityService:
    """Единая точка ответа на вопрос «что сейчас доступно»: сводка, проверка на месте, подписка на смены.

    Состояние лежит в файле на машине (`state_path`), поэтому сервис фоновой проверки в панели и любой
    процесс, проверивший связь перед запуском, видят одно и то же и не проверяют лишний раз.
    """

    def __init__(self, kit=None, *, clock=time.time, checkers=None, providers_fn=None, state_file=None):
        self.kit = kit
        self.clock = clock
        self.checkers = checkers
        self.providers_fn = providers_fn
        self.state_file = state_file
        self._cfg = None
        self._cfg_key = None
        self._subs: list = []
        self._lock = threading.Lock()
        self._monitor = None
        self._stop = threading.Event()
        self._last_global: dict = {}
        self.required_fn = None         # () → {profiles, providers, internet}: что нужно текущему делу панели

    # ---- настройка
    def config(self) -> dict:
        path = config_path(self.kit)
        try:
            st = path.stat() if path else None
            key = (st.st_mtime_ns, st.st_size) if st else None
        except OSError:
            key = None
        with self._lock:
            if self._cfg is None or key != self._cfg_key:
                self._cfg, self._cfg_key = load(self.kit), key
            return self._cfg

    @property
    def enabled(self) -> bool:
        return bool(self.config().get("enabled"))

    def providers(self) -> list:
        return (self.providers_fn or (lambda: providers_of(self.kit)))()

    # ---- подписка
    def subscribe(self, fn) -> None:
        with self._lock:
            if fn not in self._subs:
                self._subs.append(fn)

    def unsubscribe(self, fn) -> None:
        with self._lock:
            if fn in self._subs:
                self._subs.remove(fn)

    def _emit(self, events: list) -> None:
        with self._lock:
            subs = list(self._subs)
        for ev in events:
            for fn in subs:
                try:
                    fn(ev)
                except Exception:  # noqa: BLE001 — сбой подписчика не мешает остальным
                    pass

    # ---- что проверять
    def _units(self, cfg: dict, only=None) -> dict:
        """{профиль: [probe]} всего, что проверяется: профили настройки, интернет и — у включённой проверки — ручки
        провайдеров (ключ уходит на их хосты только когда владелец включил функцию)."""
        units: dict = {}
        for name in [p["name"] for p in cfg.get("profiles") or []] + [INTERNET]:
            pr = probes_of(cfg, name)
            if pr:
                units[name] = pr
        for prov in (self.providers() if cfg.get("enabled") else []):
            probe = provider_probe(prov)
            if probe:
                units[probe["profile"]] = [probe]
        if only:
            keep = {only} if isinstance(only, str) else set(only)
            found = {n: pr for n, pr in units.items() if n in keep or any(p["id"] in keep for p in pr)}
            return found
        return units

    def _due(self, units: dict, state: dict, now: float, within: float) -> list:
        """Профили, чей последний раунд старше `within` интервалов (нет раунда — тоже)."""
        cfg = self.config()
        out = []
        for name, probes in units.items():
            iv = profile_interval(cfg, probes)
            entry = state["profiles"].get(name)
            if (not isinstance(entry, dict) or entry.get("sig") != _sig(cfg, name, probes)
                    or _age(entry.get("round_at"), now) > within * iv):
                out.append(name)
        return out

    # ---- проверка
    def check(self, only=None, budget: float | None = None, force: bool = False) -> dict:
        """Проверить сейчас (все профили или названные) и вернуть сводку. Выключено — просто сводка."""
        cfg = self.config()
        if not cfg.get("enabled") and not force:
            return self.snapshot()
        units = self._units(cfg, only)
        self._run(units, cfg, budget)
        return self.snapshot()

    def ensure(self, profiles=None, budget: float = PRELAUNCH_BUDGET) -> dict:
        """Предзапускная проверка: свежее состояние из кэша берётся как есть, устаревшее перепроверяется на месте.

        `profiles` — какие профили нужны (None — все); интернет и ручки провайдеров включаются сами, если заданы.
        """
        cfg = self.config()
        if not cfg.get("enabled"):
            return self.snapshot()
        units = self._units(cfg)
        if profiles is not None:
            want = set(profiles) | {INTERNET}
            units = {n: p for n, p in units.items() if n in want or n.startswith(PROVIDER_PREFIX)}
        state = read_state(self.state_file)
        due = self._due(units, state, self.clock(), FRESH_FOR)
        if due:
            self._run({n: units[n] for n in due}, cfg, budget)
        return self.snapshot()

    def _run(self, units: dict, cfg: dict, budget: float | None) -> None:
        if not units:
            return
        flat = [p for probes in units.values() for p in probes]
        if budget is None:                      # без общего срока зависшее разрешение имени держало бы вызов бесконечно
            budget = max([probe_timeout(cfg, p) for p in flat] or [DEFAULTS["timeout"]]) + 2
        results = run_probes(flat, cfg, budget, self.checkers)
        now = self.clock()
        events: list = []

        def write(state: dict) -> None:
            for name, probes in units.items():
                events.extend(apply_round(state, cfg, name, probes, results, now))
            prune_state(state, cfg, [provider_profile(p["id"]) for p in self.providers()])

        update_state(write, self.state_file)
        self._emit(events)

    def due_now(self) -> list:
        """Профили, которым пора на очередную проверку по их интервалу (фоновая проверка панели)."""
        cfg = self.config()
        if not cfg.get("enabled"):
            return []
        return self._due(self._units(cfg), read_state(self.state_file), self.clock(), 1)

    def tick(self, budget: float | None = None) -> list:
        """Один ход фоновой проверки: перепроверить то, что подошло по интервалу → имена проверенных профилей."""
        cfg = self.config()
        due = self.due_now()
        if due:
            units = self._units(cfg)
            self._run({n: units[n] for n in due if n in units}, cfg, budget)
        if cfg.get("enabled"):
            self.note_global(self.snapshot(self.required_fn() if self.required_fn else None))
        return due

    def note_global(self, snap: dict) -> None:
        """Сменилось глобальное состояние (по требуемым зависимостям) — событие в журнал и подписчикам."""
        new, old = snap.get("state"), self._last_global.get("state")
        self._last_global["state"] = new
        if old is not None and old != new:
            ev = {"at": snap["at"], "scope": "global", "name": "kit", "from": old, "to": new}
            update_state(lambda st: st["events"].append(ev), self.state_file)
            self._emit([ev])

    # ---- сводка
    def snapshot(self, required: dict | None = None, hold: bool = False) -> dict:
        """Состояние целиком (JSON): профили, probe, интернет, провайдеры, глобальное состояние и причины.

        `required` — {profiles: [...], providers: [id...]|None, internet: bool|None}: что нужно текущему делу.
        Не задано — нужны профили, на которые ссылаются провайдеры моделей.

        `hold` — для ожидания восстановления: профиль, чьё последнее известное состояние «недоступен», а данные состарились
        (проверка не может завершиться, например зависло разрешение имени), остаётся недоступным, пока не придёт
        положительный раунд. Обычная сводка считает такой профиль неизвестным и ничего не блокирует.
        """
        cfg = self.config()
        now = self.clock()
        state = read_state(self.state_file)
        out = {"enabled": bool(cfg.get("enabled")), "at": utc_stamp(now), "error": cfg.get("error", ""),
               "policy": (cfg.get("policy") or {}).get("auto", POLICY_DEFAULT)}
        provs = self.providers()
        by_id = {p["id"]: p for p in provs}
        req = dict(required or {})
        req_profiles = list(req.get("profiles") if req.get("profiles") is not None
                            else sorted({p["profile"] for p in provs if p["profile"]}))
        req_providers = [i for i in (req.get("providers") if req.get("providers") is not None else list(by_id))
                         if i in by_id]

        def entry_view(name: str, probes: list, title: str = "") -> dict:
            iv = profile_interval(cfg, probes) if probes else cfg["settings"]["interval"]
            ent = state["profiles"].get(name) if isinstance(state["profiles"].get(name), dict) else None
            known = ent is not None and ent.get("sig") == _sig(cfg, name, probes)
            st = profile_state(ent, now, iv) if known else "unknown"
            if hold and st == "unknown" and known and ent.get("state") == "offline":
                st = "offline"
            rule = rule_of(cfg, name)
            row = {"name": name, "state": st, "rule": rule_text(rule), "interval": iv,
                   "fail_after": rule["fail_after"], "ok_after": rule["ok_after"],
                   "since": (ent or {}).get("since", "") if known else "",
                   "checked": (ent or {}).get("round_at", "") if known else "",
                   "age_s": int(_age((ent or {}).get("round_at"), now)) if known else None,
                   "healthy": (ent or {}).get("healthy") if known else None, "total": len(probes),
                   "trend": trend(ent) if known else {}, "probes": []}
            for p in probes:
                rec = state["probes"].get(p["id"]) if isinstance(state["probes"].get(p["id"]), dict) else {}
                row["probes"].append({"id": p["id"], "type": p.get("type", ""), "target": probe_target(p),
                                      "label": p.get("label", ""), "ok": rec.get("ok"), "code": rec.get("code", ""),
                                      "ms": rec.get("ms"), "detail": rec.get("detail", ""), "at": rec.get("at", "")})
            return row

        net_probes = probes_of(cfg, INTERNET)
        net = entry_view(INTERNET, net_probes)
        net["monitored"] = bool(net_probes)
        out["internet"] = net
        prof_rows = []
        for p in cfg.get("profiles") or []:
            row = entry_view(p["name"], probes_of(cfg, p["name"]))
            row["required"] = p["name"] in req_profiles
            if p.get("note"):
                row["note"] = p["note"]
            prof_rows.append(row)
        out["profiles"] = prof_rows
        states = {r["name"]: r["state"] for r in prof_rows}

        prov_rows = []
        for pid in [p["id"] for p in provs]:
            prov = by_id[pid]
            st, why = "online", ""
            if prov["profile"]:
                if prov["profile"] not in states:
                    st, why = "unknown", "no_profile"          # привязан к профилю, которого в настройке нет
                elif states[prov["profile"]] == "offline":
                    st, why = "offline", "profile_offline"
                elif states[prov["profile"]] == "unknown":
                    st, why = "unknown", "profile_unknown"
            elif not prov["local"] and net["monitored"] and net["state"] == "offline":
                st, why = "offline", "internet_offline"
            if st == "online" and prov["check"] != "off":
                probe = provider_probe(prov)
                if probe is None:
                    st, why = "unknown", "check_unusable"     # проверку собрать нельзя (адрес, ключ, открытый http)
                else:
                    hp = entry_view(provider_profile(pid), [probe])
                    if hp["state"] == "offline":
                        st, why = "offline", "provider_check"
                    elif hp["state"] == "unknown":
                        st, why = "unknown", "provider_unknown"
            prov_rows.append({"id": pid, "name": prov["name"], "profile": prov["profile"], "check": prov["check"],
                              "local": prov["local"], "state": st, "why": why,
                              "required": pid in req_providers})
        out["providers"] = prov_rows
        req_rows = [r for r in prov_rows if r["required"]]
        down = sum(1 for r in req_rows if r["state"] == "offline")
        unknown = sum(1 for r in req_rows if r["state"] == "unknown" and r["why"] not in ("no_profile", "check_unusable"))
        want_net = req.get("internet")
        monitored = bool(net["monitored"]) and (True if want_net is None else bool(want_net))
        out["state"] = (global_state(states, net["state"], monitored, req_profiles, down, unknown)
                        if out["enabled"] else "unknown")
        out["required"] = {"profiles": req_profiles, "providers": req_providers, "internet": monitored}
        out["reasons"] = reasons_of(out)
        out["events"] = state["events"][-20:]
        return out

    # ---- фоновая проверка
    def start_monitor(self, tick: float = 5.0, on_tick=None) -> None:
        """Фоновая проверка (панель): раз в `tick` секунд проверяет профили, подошедшие по интервалу."""
        with self._lock:
            if self._monitor is not None and self._monitor.is_alive():
                return
            self._stop.clear()

            def loop() -> None:
                wait = tick
                while not self._stop.wait(wait):
                    try:
                        self.tick()
                        if on_tick:
                            on_tick()
                        wait = tick
                    except Exception:  # noqa: BLE001 — сторож не падает от одной неудачной проверки
                        wait = max(tick, 30.0)          # а упорная ошибка не гонит проверки каждые пять секунд

            self._monitor = threading.Thread(target=loop, daemon=True, name="aurora-connectivity")
            self._monitor.start()

    def stop_monitor(self) -> None:
        self._stop.set()
        m = self._monitor
        if m is not None:
            m.join(2)
        self._monitor = None


def reasons_of(snap: dict) -> list:
    """Причины, из-за которых состояние не `online`, — кодами: [{code, name}]. Строку из кода делает тот, кто показывает."""
    out = []
    if not snap.get("enabled"):
        return out
    net = snap.get("internet") or {}
    req = snap.get("required") or {}
    if not req.get("profiles") and not req.get("internet"):
        out.append({"code": "nothing_required", "name": ""})     # справка: проверять пока нечего
    if net.get("monitored") and net.get("state") == "offline" and req.get("internet"):
        out.append({"code": "internet_offline", "name": INTERNET})
    for r in snap.get("profiles") or []:
        if not r.get("required"):
            continue
        if r["state"] == "offline":
            out.append({"code": "profile_offline", "name": r["name"]})
        elif r["state"] == "unknown":
            out.append({"code": "profile_unknown", "name": r["name"]})
        elif r["state"] == "degraded":
            out.append({"code": "profile_degraded", "name": r["name"]})
    for r in snap.get("providers") or []:
        if not r.get("required"):
            continue
        if r["why"] == "check_unusable":
            out.append({"code": "check_unusable", "name": r["id"]})
        elif r["state"] == "offline" and r["why"] == "provider_check":
            out.append({"code": "provider_check", "name": r["id"]})
        elif r["state"] == "offline" and r["why"] == "profile_offline":
            out.append({"code": "provider_vpn", "name": r["id"], "profile": r["profile"]})
        elif r["state"] == "offline":
            out.append({"code": "provider_internet", "name": r["id"]})
    return out


_SERVICES: dict = {}
_SERVICES_LOCK = threading.Lock()


def service(kit=None) -> AvailabilityService:
    """Общий сервис процесса для кита (по одному на путь)."""
    key = str(kit_dir(kit) or "")
    with _SERVICES_LOCK:
        if key not in _SERVICES:
            _SERVICES[key] = AvailabilityService(kit)
        return _SERVICES[key]


def reset_services() -> None:
    with _SERVICES_LOCK:
        for s in _SERVICES.values():
            s.stop_monitor()
        _SERVICES.clear()


# ------------------------------------------------------------------ вывод для человека

GLOBAL_TEXT = {
    "online": "связь в порядке: все нужные зависимости доступны",
    "degraded": "доступно всё нужное, но часть probe не отвечает",
    "partial": "часть зависимостей недоступна: работает то, что их не требует",
    "no_vpn": "интернет есть, но нужные VPN-профили недоступны",
    "offline": "нет интернета: доступны только функции без подключения",
    "unknown": "состояние не определено: проверки ещё не было или она устарела",
}
STATE_TEXT = {"online": "доступен", "degraded": "доступен частично", "offline": "недоступен", "unknown": "не известно"}
CODE_TEXT = {"ok": "ответил", "late": "не успел за общий срок", "timeout": "нет ответа", "refused": "отказал в соединении", "dns": "имя не находится",
             "unreachable": "недостижим", "tls": "ошибка TLS", "status": "неожиданный статус", "body": "нет ожидаемого текста",
             "no_ping": "нет команды ping", "other": "ошибка"}
REASON_TEXT = {
    "nothing_required": "ни один источник или провайдер не привязан к профилю, интернет не проверяется: проверять нечего "
                        "(привязка — в «Настройках проекта» и в «Модели»)",
    "internet_offline": "интернет недоступен",
    "profile_offline": "{name} недоступен",
    "profile_unknown": "{name}: состояние не известно",
    "profile_degraded": "{name} доступен частично",
    "provider_check": "провайдер {name} не прошёл проверку",
    "check_unusable": "проверку ручки провайдера {name} собрать нельзя: адрес, ключ или открытый http",
    "provider_vpn": "провайдер {name} недоступен: {profile} не отвечает",
    "provider_internet": "провайдер {name} недоступен: нет интернета",
}


def reason_text(r: dict) -> str:
    return REASON_TEXT.get(r.get("code"), r.get("code", "")).format(
        name=r.get("name", ""), profile=r.get("profile", ""))


def mode_line(snap: dict) -> str:
    """Строка режима работы (§7 требований): что доступно, а что нет."""
    st = snap.get("state")
    if not snap.get("enabled"):
        return "проверка соединений выключена: включается в панели, раздел «Проверка соединения»"
    if st == "offline":
        return "Режим работы — offline, доступны только функции без подключения"
    down = [r["name"] for r in snap.get("profiles") or [] if r.get("required") and r["state"] == "offline"]
    if st in ("no_vpn", "partial") and down:
        return f"{', '.join(down)} недоступен; доступны функции без {', '.join(down)} и web-функции"
    return GLOBAL_TEXT.get(st, "")


def render(snap: dict, targets: bool = False) -> str:
    L = [f"Связь: {snap.get('state')} — {mode_line(snap)}"]
    if snap.get("error"):
        L.append("ОШИБКА настройки: " + snap["error"])
    if not snap.get("enabled"):
        return "\n".join(L)
    net = snap.get("internet") or {}
    if net.get("monitored"):
        L.append(f"  интернет: {STATE_TEXT.get(net['state'], net['state'])}")
        L += _probe_lines(net, targets)
    else:
        L.append("  интернет: не проверяется (в разделе «Проверка соединения» не задан ни один адрес)")
    for r in snap.get("profiles") or []:
        age = f", проверено {r['age_s']} с назад" if r.get("age_s") is not None else ""
        mark = "" if r.get("required") else " (не требуется текущему делу)"
        L.append(f"  {r['name']}: {STATE_TEXT.get(r['state'], r['state'])} · правило {r['rule']}{age}{mark}")
        L += _probe_lines(r, targets)
    for r in snap.get("providers") or []:
        why = {"profile_offline": f"профиль {r['profile']} недоступен", "internet_offline": "нет интернета",
               "check_unusable": "проверку ручки собрать нельзя",
               "provider_check": "проверка ручки не прошла", "profile_unknown": f"профиль {r['profile']}: не известно",
               "provider_unknown": "проверка ручки не выполнялась", "no_profile": f"профиль {r['profile']} не заведён"}.get(r["why"], "")
        if r["state"] != "online" or r["profile"] or r["check"] != "off":
            L.append(f"  провайдер {r['id']}: {STATE_TEXT.get(r['state'], r['state'])}"
                     + (f" — {why}" if why else ""))
    for r in snap.get("reasons") or []:
        L.append("  причина: " + reason_text(r))
    return "\n".join(L)


def without_targets(snap: dict) -> dict:
    """Копия сводки без адресов probe: машинный вывод команды по умолчанию, как и текстовый, адресов не показывает."""
    out = json.loads(json.dumps(snap))
    for row in [out.get("internet") or {}] + list(out.get("profiles") or []):
        for p in row.get("probes") or []:
            p.pop("target", None)
    return out


def _probe_lines(row: dict, targets: bool) -> list:
    out = []
    for p in row.get("probes") or []:
        ok = "ок" if p["ok"] else ("—" if p["ok"] is None else "нет")
        what = f"{p['type']} {p['target']}" if targets else p["type"]
        extra = f" · {p['ms']} мс" if p.get("ms") is not None and p["ok"] else ""
        extra += f" · {CODE_TEXT.get(p['code'], p['code'])}" if p.get("code") and not p["ok"] else ""
        out.append(f"      {p['id']} ({what}): {ok}{extra}")
    return out


# ------------------------------------------------------------------ команды

def _required_for_cwd(svc: AvailabilityService):
    """Что нужно делу текущей папки (проект — его источники и провайдеры), иначе None."""
    if not os.path.isfile("aurora.config.yaml"):
        return None
    try:
        import conn_gate as GATE
        return GATE.required_for(os.getcwd(), svc)
    except Exception:  # noqa: BLE001 — сводка важнее разбора проекта
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Доступность соединений: состояние и проверка")
    ap.add_argument("--status", action="store_true", help="состояние профилей, probe, провайдеров и причины")
    ap.add_argument("--probe", action="store_true", help="проверить сейчас (все probe или --only)")
    ap.add_argument("--only", metavar="ID", help="только этот профиль или probe")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    ap.add_argument("--targets", action="store_true", help="показать адреса probe (по умолчанию — только тип и идентификатор)")
    a = ap.parse_args(argv)
    svc = service()
    if svc.config().get("error"):
        print("ОШИБКА: " + svc.config()["error"], file=sys.stderr)
        return 2
    if a.probe:
        if not svc.enabled:
            print("Проверка соединений выключена: включается в панели, раздел «Проверка соединения».")
            return 0
        svc.check(only=a.only, budget=None)
    required = _required_for_cwd(svc)
    snap = svc.snapshot(required)
    if a.json:
        print(json.dumps(snap if a.targets else without_targets(snap), ensure_ascii=False, indent=2))
    else:
        print(render(snap, a.targets))
    if a.probe and snap["enabled"] and snap["state"] in ("offline", "no_vpn", "partial"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
