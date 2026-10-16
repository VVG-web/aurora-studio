#!/usr/bin/env python3
"""sources_core.py — общая часть синка внешних источников (фреймворк «Аврора»).

Зеркало в `Sources/` устроено одинаково независимо от того, откуда его наливают:
файлы markdown, рядом файл состояния, инкрементальность по версии записи, `--prune`
для того, чего в источнике больше нет, и гейт детерминизма. Различается только то,
как ходить в продукт и как превращать его разметку в markdown, — это дело модуля
(`connectors/<id>/`), а не движка.

Два вида хранилищ, которые Аврора умеет зеркалить:

  wiki  — дерево страниц со стабильными номерами: папки повторяют иерархию,
          страница с детьми даёт папку и `index.md`, состояние — `sync_state.md`;
  board — плоский список задач со стабильными ключами: файл на задачу,
          состояние — `update_log.md`, копится между прогонами по узкому запросу.

Модуль реализует загрузку и рендер, всё остальное берёт отсюда. Сам по себе файл
ничего не делает: это библиотека, её импортируют скрипты модулей и `sync_audit.py`.
"""
from __future__ import annotations

import base64
import filecmp
import json
import os
import re
import sys
import tempfile

from aurora_common import (allow_cleartext, cleartext_secret_problem, head_scalar, root_link_escapes,
                           is_loopback_host, safe_urlopen, same_origin, split_frontmatter, url_origin,
                           yaml_scalar)
import unicodedata
import urllib.parse
import urllib.request

CONFIG = "aurora.config.yaml"
ENV_LOCAL = ".env.aurora.local"
from aurora_common import TODAY  # noqa: E402 — дата в UTC, одна на движок
KB_ROOT = "AuroraKnowledgeDB"

# Служебные файлы зеркала — не страницы и не задачи: промпты, правила, шаблоны и
# отчёты прежних синк-скиллов. Список общий для выгрузки и для аудита: разойдись они,
# аудит числил бы служебный файл сиротой, а `--prune` его не трогал — и расхождение
# не сходилось бы никогда.
SERVICE_RE = re.compile(
    r"(sync_state|update_log|sync_paths|sync_report|_prompt|_template|_example|"
    r"-rules|_rules|SYNC_|FINAL_SYNC|README)", re.I)


# ------------------------------------------------------------------ конфиг

def config_text(path: str = CONFIG) -> str:
    return open(path, encoding="utf-8", errors="ignore").read() if os.path.isfile(path) else ""


def block(text: str, key: str, *until: str) -> str:
    """Кусок конфига от `key:` до следующего из `until` — YAML читаем без зависимостей."""
    if key not in text:
        return ""
    tail = text.split(key, 1)[-1]
    for stop in until:
        tail = tail.split(stop, 1)[0]
    return tail


def scalar(text: str, key: str, default: str = "") -> str:
    """Скаляр конфига — общим правилом движка (`aurora_common.yaml_scalar`)."""
    return yaml_scalar(text, key, default)


def read_env(root: str = "") -> dict:
    """Окружение плюс `.env.aurora.local` (он в .gitignore). Секреты наружу не печатаем.

    `root` — корень проекта; без него файл ищется от текущей папки процесса."""
    env = dict(os.environ)
    env_file = os.path.join(root, ENV_LOCAL) if root else ENV_LOCAL
    if os.path.isfile(env_file):
        for line in open(env_file, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def read_secret(prefix: str, root: str = "") -> tuple:
    """→ (заголовок Authorization, как назвали способ).

    Имена переменных выводятся из префикса модуля: `CONFLUENCE` → `CONFLUENCE_PAT`,
    `CONFLUENCE_PERSONAL_TOKEN`, `CONFLUENCE_USER` + `CONFLUENCE_PASSWORD`.
    """
    env = read_env(root)
    pat = env.get(f"{prefix}_PAT") or env.get(f"{prefix}_PERSONAL_TOKEN")
    if pat:
        return f"Bearer {pat}", "PAT"
    user, pwd = env.get(f"{prefix}_USER"), env.get(f"{prefix}_PASSWORD")
    if user and pwd:
        return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode(), "basic"
    return "", ""


# --------------------------------------------------------------------- REST

class UnsafeTarget(ValueError):
    """Ключ на этот адрес не уходит: чужой хост, не тот сервер или открытый http."""


def server_pin(prefix: str, env: dict) -> str:
    """Привязка машины: `AURORA_<PREFIX>_SERVER=https://хост` в файле машины. Имя с
    префиксом `AURORA_`, чтобы не спутаться с `JIRA_SERVER` других инструментов Jira."""
    return str(env.get(f"AURORA_{prefix}_SERVER", "") or "").strip() if prefix else ""


def _trusted_path(root: str) -> str:
    return os.path.join(root or ".", ".aurora", "state", "trusted-hosts.json")


def trusted_origin(prefix: str, root: str = "") -> str:
    """Сервер, на котором ключ этого префикса уже сработал (вне git: состояние машины)."""
    try:
        with open(_trusted_path(root), encoding="utf-8") as f:
            got = json.load(f).get(prefix, "")
    except (OSError, ValueError, AttributeError):
        return ""
    return got if isinstance(got, str) else ""


def _store_trusted(prefix: str, value, root: str = "") -> bool:
    """Записать `value` под ключом `prefix` в файл доверенных серверов машины (вне git).
    True — записалось."""
    path = _trusted_path(root)
    try:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[prefix] = value
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True
    except OSError:
        return False    # не записалось — защиты чуть меньше, синк от этого не страдает


def origin_text(o: tuple) -> str:
    """`схема://хост[:порт]` по `url_origin(...)`: стандартный порт схемы не пишется, адрес
    IPv6 берётся в квадратные скобки (иначе `url_origin` записанную строку не разберёт)."""
    host = f"[{o[1]}]" if ":" in o[1] else o[1]
    return f"{o[0]}://{host}" + ("" if o[2] == {"http": 80, "https": 443}.get(o[0]) else f":{o[2]}")


def remember_origin(prefix: str, base: str, root: str = "") -> None:
    """Первый удачный ответ сервера: запомнить его адрес для ключа этого префикса, чтобы
    подмена `base_url` в общем конфиге потом не увела ключ. Без диалога: смену адреса
    человек принимает строкой `AURORA_<PREFIX>_SERVER` (ADR-026). Свой компьютер и проект
    без `.aurora/` не запоминаются."""
    o = url_origin(base)
    if not prefix or not o or is_loopback_host(o[1]) \
            or not os.path.isdir(os.path.join(root or ".", ".aurora")) \
            or trusted_origin(prefix, root):
        return
    _store_trusted(prefix, origin_text(o), root)


def trusted_hosts(prefix: str, root: str = "") -> list:
    """Хосты, где ключ этого префикса уже сработал (токен обхода сайтов: несколько хостов)."""
    try:
        with open(_trusted_path(root), encoding="utf-8") as f:
            got = json.load(f).get(prefix, [])
    except (OSError, ValueError, AttributeError):
        return []
    return [h for h in got if isinstance(h, str)] if isinstance(got, list) else []


def remember_hosts(prefix: str, hosts, root: str = "") -> bool:
    """Источники (`схема://хост[:порт]`), на которых ключ впервые сработал, — для токена обхода
    сайтов: их может быть несколько, и они запоминаются один раз, целиком. Страница, добавленная
    в общий список позже, на другом хосте или порту токен не получит, пока человек не впишет её
    строкой `AURORA_WEB_TOKEN_HOSTS`. Проект без `.aurora/` не запоминается. True — привязка
    записана этим вызовом."""
    hosts = sorted({str(h).lower() for h in hosts if h})
    if not prefix or not hosts or trusted_hosts(prefix, root) \
            or not os.path.isdir(os.path.join(root or ".", ".aurora")):
        return False
    return _store_trusted(prefix, hosts, root)


def run_main(main) -> int:
    """Запуск `main()` командного скрипта: отказ `UnsafeTarget` — ожидаемая настройка (чужой
    адрес, открытый http), а не сбой: одна строка причины в stderr и код 1, без трейсбека."""
    try:
        return main()
    except UnsafeTarget as e:
        print(f"! {e}", file=sys.stderr)
        return 1


def secret_target(url: str, base: str, prefix: str = "", root: str = "",
                  env: dict | None = None) -> str:
    """Адрес запроса с ключом — или `UnsafeTarget`, если ключ на него отправлять нельзя:
    `base` не совпал с привязкой машины или с сервером, где ключ уже сработал; адрес чужого
    хоста; открытый http. Общая проверка клиента источников и шагов отчётов."""
    env = read_env(root) if env is None else env
    base = base.rstrip("/")
    pin = server_pin(prefix, env)
    if pin and not same_origin(base, pin):
        raise UnsafeTarget(f"адрес сервера в aurora.config.yaml ({base}) не совпадает с "
                           f"AURORA_{prefix}_SERVER из файла машины ({pin}): "
                           "ключ на него не отправляется")
    if prefix and not pin:
        was, o = trusted_origin(prefix, root), url_origin(base)
        if was and o and not is_loopback_host(o[1]) and not same_origin(base, was):
            raise UnsafeTarget(f"адрес сервера изменился: был {was}, в aurora.config.yaml стал "
                               f"{base} — ключ на него не отправляется. Если переезд "
                               f"настоящий, поставьте AURORA_{prefix}_SERVER={base} в файл "
                               "машины (.env.aurora.local)")
    url = url if url.startswith("http") else base + url
    if not same_origin(url, base):
        raise UnsafeTarget(f"адрес {url.split('?', 1)[0]} вне сервера {base}: "
                           "ключ на чужой хост не отправляется")
    why = cleartext_secret_problem(url, allow_cleartext(env))
    if why:
        raise UnsafeTarget(why)
    return url


class FileTooLarge(Exception):
    """Тело ответа больше заданного предела (`RestApi.fetch(max_bytes=…)`)."""


class RestApi:
    """Минимальный REST-клиент: токен в заголовке, JSON на выходе, свой User-Agent.

    Ничего на сервер источника не ставится, третьих библиотек не нужно — синк должен
    заводиться там, где есть только стандартный Python.

    Токен принадлежит серверу из `base`: абсолютный адрес от сервера (`_links.next`,
    вложение) принимается только с того же хоста, редирект на другой хост уходит без
    `Authorization`, а по открытому `http://` ключ не шлётся (кроме своего компьютера и
    `AURORA_ALLOW_HTTP=1`). Адрес `base` живёт в общем `aurora.config.yaml`, поэтому его
    подмена в общем файле не должна уводить ключ: первый удачный ответ запоминает сервер
    (`.aurora/state/trusted-hosts.json`), и другой `base_url` отклоняется, пока человек не
    принял его строкой `AURORA_<PREFIX>_SERVER=https://хост` в файле машины (она же —
    привязка заранее, до первого запуска).
    """

    agent = "aurora-sync/1.0"
    secret_prefix = ""      # CONFLUENCE / JIRA: по нему ищется привязка и доверенный сервер

    def __init__(self, base: str, auth: str, root: str = ""):
        self.base, self.auth, self.root = base.rstrip("/"), auth, root
        self._env = None
        self._seen = False

    def _url(self, path: str) -> str:
        """Полный адрес запроса — или `UnsafeTarget`, если ключ на него отправлять нельзя."""
        if not self.auth:
            return path if path.startswith("http") else self.base + path
        if self._env is None:       # файл читаем один раз: запросов в синке тысячи
            self._env = read_env(self.root)
        return secret_target(path, self.base, self.secret_prefix, self.root, self._env)

    def _ok(self) -> None:
        """Сервер ответил на запрос с ключом: запомнить его (один раз за жизнь клиента)."""
        if self.auth and not self._seen:
            self._seen = True
            if not server_pin(self.secret_prefix, self._env or {}):
                remember_origin(self.secret_prefix, self.base, self.root)

    def get(self, path: str) -> dict:
        req = urllib.request.Request(self._url(path), headers={
            "Authorization": self.auth, "Accept": "application/json",
            "User-Agent": self.agent})
        with safe_urlopen(req, timeout=60) as r:
            self._ok()
            return json.load(r)

    def fetch(self, url: str, max_bytes: int | None = None) -> bytes:
        """Сырое тело по адресу: вложения (схемы, диаграммы) — не JSON.

        `max_bytes` — предел размера: сервер объявил больше — отказ до чтения тела, иначе
        читается на байт больше предела, и превышение даёт `FileTooLarge`. Без аргумента
        читается всё, как раньше."""
        req = urllib.request.Request(self._url(url), headers={"Authorization": self.auth,
                                                              "User-Agent": self.agent})
        with safe_urlopen(req, timeout=120) as r:
            self._ok()
            if max_bytes is None:
                return r.read()
            try:
                declared = int(r.headers.get("Content-Length") or 0)
            except (TypeError, ValueError, AttributeError):
                declared = 0
            if declared > max_bytes:
                raise FileTooLarge("файл больше предела")
            data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FileTooLarge("файл больше предела")
            return data

    def send(self, method: str, path: str, body=None, files: dict | None = None,
             timeout: float = 30, tries: int = 3):
        """Запись в источник: POST/PUT с JSON или файлом (multipart). → ответ JSON или {}.

        Повторяем только то, что лечится ожиданием: обрыв сети, 5xx и 429 — до трёх раз с
        растущей паузой. Внятный отказ сервера (400, 403, 404) повтором не лечится и
        уходит вызывающему как есть, с телом ответа: в нём причина.
        """
        import time
        import urllib.error
        url = self._url(path)
        headers = {"Authorization": self.auth, "Accept": "application/json",
                   "User-Agent": self.agent}
        if files:
            # Jira Server принимает вложение только с этим заголовком (защита от XSRF).
            boundary = "aurora" + os.urandom(12).hex()
            parts = []
            for name, (filename, content) in files.items():
                parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                             f"filename*=UTF-8''{urllib.parse.quote(filename)}\r\n"
                             f"Content-Type: application/octet-stream\r\n\r\n".encode("utf-8")
                             + content + b"\r\n")
            data = b"".join(parts) + f"--{boundary}--\r\n".encode()
            headers.update({"Content-Type": f"multipart/form-data; boundary={boundary}",
                            "X-Atlassian-Token": "no-check"})
        else:
            data = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        last = None
        for attempt in range(tries):
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with safe_urlopen(req, timeout=timeout) as r:
                    self._ok()
                    raw = r.read()
                return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504) or attempt == tries - 1:
                    raise
                last = e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt == tries - 1:
                    raise
                last = e
            time.sleep(2 ** attempt)
        raise last  # pragma: no cover — цикл выше всегда возвращает или бросает


# ------------------------------------------------------------------ зеркало

# Папка со схемами страницы: `<имя страницы>_assets/…` — содержимое зеркала, но не запись
# состояния. Совпадает и в корне, и на любой глубине.
ASSET_DIR_RE = re.compile(r"(^|/)[^/]+_assets/")


def nfc(path: str) -> str:
    """Пути в единую нормализацию Unicode.

    macOS отдаёт имена файлов в NFD («и» + диакритика раздельно), а состояние синка
    писалось откуда придётся — одна и та же страница выглядит и как пропавшая, и как
    лишняя одновременно. Сравнивать пути без нормализации на macOS нельзя.
    """
    return unicodedata.normalize("NFC", path)


class Mirror:
    """Папка зеркала: где лежит, как пишется состояние, что в ней лишнее.

    Наследники (`WikiMirror`, `BoardMirror`) задают схему состояния и раскладку
    файлов. Как наливается содержимое — дело модуля источника.
    """

    state_name = ""      # имя файла состояния рядом с зеркалом
    banner = ""          # комментарий в шапке файла состояния
    count_label = ""     # что считаем: Pages, Issues…
    columns: tuple = ()  # колонки таблицы состояния
    flat = False         # плоское зеркало (файл на запись) или дерево

    def __init__(self, out: str):
        self.out = out.rstrip("/")

    @property
    def state_path(self) -> str:
        return os.path.join(self.out, self.state_name)

    def state_rows(self) -> list:
        """Строки таблицы состояния (без шапки). Реализует наследник."""
        raise NotImplementedError

    def write_state(self) -> None:
        rows = self.state_rows()
        head = [f"<!-- {self.banner} -->",
                f"**Sync Date:** {TODAY}",
                f"**{self.count_label}:** {len(rows)}",
                "",
                "| " + " | ".join(self.columns) + " |",
                "|" + "---|" * len(self.columns)]
        os.makedirs(self.out, exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("\n".join(head + rows) + "\n")

    def state_cells(self) -> list:
        """Разобранные строки состояния: список списков ячеек (шапка и разделитель отброшены)."""
        out = []
        if not os.path.isfile(self.state_path):
            return out
        for line in open(self.state_path, encoding="utf-8", errors="ignore"):
            line = line.rstrip("\n")
            if not line.startswith("|") or line.startswith("|---"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and cells != [c.strip() for c in self.columns]:
                out.append(cells)
        return out

    def disk_rels(self, only_md: bool = True) -> list:
        """Файлы зеркала (кроме служебных) относительными путями.

        `only_md=False` возвращает и всё прочее: `.md_COLLISION`, `.bak`, копии от прежних
        синк-скиллов. Зеркало — машинная выгрузка, и файл в нём, за которым не стоит
        страница, — мусор по определению. Пока чистка смотрела только на `.md`, такой
        мусор был невидим и для `--prune`, и для аудита: папка с шестью `.md_COLLISION`
        пережила и `--force`, и `--prune`, и читалась человеком как дубль каталога."""
        out = []
        if not os.path.isdir(self.out):
            return out
        if self.flat:
            names = [f for f in os.listdir(self.out) if os.path.isfile(os.path.join(self.out, f))]
            pairs = [(f, f) for f in names]
        else:
            pairs = []
            for dirpath, _, files in os.walk(self.out):
                for f in files:
                    rel = os.path.relpath(os.path.join(dirpath, f), self.out).replace("\\", "/")
                    pairs.append((f, rel))
        for name, rel in pairs:
            if name.startswith(".") or name == self.state_name or SERVICE_RE.search(name):
                continue      # служебное синка и точечные файлы ОС трогать не наше дело
            if name.endswith(".md") or not only_md:
                out.append(rel)
        return sorted(out)

    def extra_files(self, known) -> list:
        """Файлы зеркала, за которыми в состоянии синка ничего не стоит.

        Так в зеркале остаются следы прежних выгрузок: та же задача под старым именем
        читается как живая, хотя давно не обновляется. Различие только в регистре или
        в нормализации Unicode — не повод: это тот же файл, и удалять его нельзя.
        """
        known_n = {nfc(k) for k in known}
        known_ci = {k.casefold() for k in known_n}
        out = []
        for rel in self.disk_rels(only_md=False):
            # Схемы страницы лежат рядом с ней в `<страница>_assets/`. В состоянии синка
            # их нет — там страницы, — но зеркалу они принадлежат: без этого правила
            # чистка сносила ровно то, что синк только что скачал. Принадлежат, пока есть
            # сама страница: схемы страницы, которой в состоянии нет, — след прежней
            # раскладки. PRJ-B 29.09.2026: папка прежней раскладки от 12.08 держала три
            # `_assets/` без единой страницы, и путь одного из них в 205 знаков doctor
            # звал на починку, а ни синк, ни аудит, ни `kb_names` его не видели.
            m = ASSET_DIR_RE.search(rel)
            if m:
                page = rel[:m.end() - len("_assets/")]
                owners = {nfc(page + ".md"), nfc(page + "/index.md")}
                if owners & known_n or {o.casefold() for o in owners} & known_ci:
                    continue
                out.append(rel)
                continue
            n = nfc(rel)
            if n not in known_n and n.casefold() not in known_ci:
                out.append(rel)
        return sorted(out)

    def prune(self, rels, keep=()) -> int:
        """Удалить перечисленное, кроме `keep`. → сколько удалено."""
        gone = 0
        for rel in rels:
            if rel in keep:
                continue
            try:
                os.remove(os.path.join(self.out, rel))
                gone += 1
            except OSError as e:
                print(f"  ! не удалить {rel}: {e}", file=sys.stderr)
        return gone


class WikiMirror(Mirror):
    """Дерево страниц: папки по иерархии, `index.md` у страницы с детьми."""

    state_name = "sync_state.md"
    count_label = "Pages"
    columns = ("#", "Page ID", "Title", "Local Path", "Status")

    def __init__(self, out: str):
        super().__init__(out)
        self.records: list = []   # (page_id, rel, title, статус)
        self.recased: list = []   # папки, которым выправили регистр

    def state_rows(self) -> list:
        return [f"| {i} | {pid} | {title.replace('|', '/')} | {rel} | {status} |"
                for i, (pid, rel, title, status)
                in enumerate(sorted(self.records, key=lambda r: r[1]), 1)]

    def keep_unvisited(self) -> int:
        """Страницы прежнего состояния, до которых обход не дошёл, — остаются. → сколько.

        Обход с ошибками связи доходит не до всех веток: у PRJ-A 28.09.2026 из 1068 страниц
        обошлось 229 (таймауты сервера). Состояние же писалось по обойдённым, и 839 страниц
        выпадали из него — база считала их чужими зеркалу, а маршрут закоммитил бы это.
        Страница, чей файл на месте и до которой просто не дошли, остаётся прежней записью.
        """
        seen = {str(r[0]) for r in self.records}
        kept = 0
        for cells in self.state_cells():
            if len(cells) < 5 or not cells[1].isdigit() or cells[1] in seen:
                continue
            pid, title, rel, status = cells[1], cells[2], cells[3], cells[4]
            if os.path.isfile(os.path.join(self.out, rel)):
                self.records.append((pid, rel, title, status))
                seen.add(pid)
                kept += 1
        return kept

    def align_case(self, rel: str) -> None:
        """Привести регистр папок зеркала к тому, что сейчас в заголовках страниц.

        Страницу переименовали «Core_аналитический» → «Core_Аналитический». На macOS и
        Windows файловая система к регистру нечувствительна: запись по новому пути молча
        попадает в старую папку. Дальше состояние синка говорит одно, диск показывает
        другое, аудит считает это потерей страницы, а `--prune` норовит удалить только что
        записанный файл. Чиним в одном месте: перед записью выравниваем регистр
        каталогов — переименование сработает и там, где регистр не различают.
        """
        cur = self.out
        for part in os.path.dirname(rel).split("/"):
            if not part:
                continue
            want = os.path.join(cur, part)
            if not os.path.isdir(cur):
                return
            same = next((n for n in os.listdir(cur)
                         if n != part and n.casefold() == part.casefold()), None)
            if same:
                try:
                    os.rename(os.path.join(cur, same), want)
                    self.recased.append(f"{same} → {part}")
                except OSError as e:
                    print(f"  ! регистр папки не поправить: {same} → {part}: {e}", file=sys.stderr)
            cur = want


class BoardMirror(Mirror):
    """Плоская доска: файл на задачу, состояние копится между прогонами."""

    state_name = "update_log.md"
    count_label = "Issues"
    columns = ("Issue Key", "Updated", "Status", "Local Path")
    flat = True
    key_re = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

    def __init__(self, out: str):
        super().__init__(out)
        self.rows: list = []      # (ключ, updated, статус, путь) — то, что выгрузили сейчас
        self.merged: dict = {}    # ключ → строка состояния после слияния с прошлым

    def previous(self) -> dict:
        """{ключ: (ключ, updated, статус, путь)} из прошлого прогона."""
        prev = {}
        for cells in self.state_cells():
            if len(cells) >= 4 and self.key_re.match(cells[0]):
                prev[cells[0]] = (cells[0], cells[1], cells[2], cells[3])
        return prev

    def state_rows(self) -> list:
        return [f"| {key} | {updated} | {status} | {path} |"
                for key, updated, status, path in sorted(self.merged.values())]

    def write_state(self) -> dict:
        """Слить с прежним состоянием: прогон по узкому запросу не должен терять остальное."""
        merged = self.previous()
        for key, updated, status, rel in self.rows:
            merged[key] = (key, updated, status, rel)
        # осиротевшие записи (файла нет) в состоянии не держим
        self.merged = {k: v for k, v in merged.items()
                       if os.path.isfile(os.path.join(self.out, v[3]))}
        super().write_state()
        return self.merged


# ------------------------------------------------------------ связь с базой

def mirror_prefix(mirror_dir: str) -> str:
    """Как зеркало называется в карточках: путь от корня проекта (`Sources/JIRA`)."""
    p = mirror_dir.replace("\\", "/").rstrip("/")
    i = p.find("Sources/")
    return p[i:] if i >= 0 else os.path.basename(p)


def cited_by_cards(mirror_dir: str, rels) -> set:
    """Какие файлы зеркала упоминаются карточками базы через `source:`.

    `source:` — единственная нить от знания к доказательству. Удалить файл, на который
    ссылается карточка, значит оборвать её провенанс: такие файлы `--prune` не трогает,
    а называет — сначала перенацелить ссылки, потом убирать.
    """
    rels = list(rels)
    if not rels:
        return set()
    kb = os.path.normpath(os.path.join(os.path.dirname(os.path.dirname(
        mirror_dir.rstrip("/"))) or ".", KB_ROOT))
    if not os.path.isdir(kb):
        kb = KB_ROOT
    if not os.path.isdir(kb):
        return set()
    prefix = mirror_prefix(mirror_dir)
    # Сверяем ровно путь, а не подстроку: карточка, где просто упомянут номер задачи,
    # ссылкой на файл не является — иначе защита не даёт удалить вообще ничего.
    want = {rel: f"{prefix}/{rel}" for rel in rels}
    hit = set()
    for dirpath, _, files in os.walk(kb):
        if os.path.basename(dirpath) == "meta":
            continue
        for f in files:
            if not f.endswith(".md"):
                continue
            try:
                text = open(os.path.join(dirpath, f), encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if prefix not in text:
                continue
            for rel, ref in want.items():
                if rel not in hit and ref in text:
                    hit.add(rel)
    return hit


# ---------------------------------------------------------- гейт детерминизма

def verify(run, skip=()) -> int:
    """Выгрузить дважды во временные папки и сверить побайтово. → код возврата.

    Смысл гейта: пока markdown писала модель, один и тот же текст выгружался
    по-разному и git показывал правку там, где её нет. Проверяем это машинно.
    """
    with tempfile.TemporaryDirectory() as td:
        one, two = os.path.join(td, "a"), os.path.join(td, "b")
        run(one)
        run(two)
        diff, total = [], 0
        for dirpath, _, files in os.walk(one):
            for f in sorted(files):
                if f in skip:
                    continue
                p1 = os.path.join(dirpath, f)
                rel = os.path.relpath(p1, one)
                total += 1
                p2 = os.path.join(two, rel)
                if not os.path.isfile(p2) or not filecmp.cmp(p1, p2, shallow=False):
                    diff.append(rel)
        if diff:
            print(f"❌ Детерминизм нарушен: {len(diff)} из {total} файлов различаются между прогонами")
            for d in sorted(diff)[:10]:
                print("   ", d)
            return 1
        print(f"✅ Детерминизм подтверждён: {total} файлов, два прогона совпали побайтово")
        return 0


# ------------------------------------------------------------------ вывод

def drop_empty_dirs(root: str) -> int:
    """Убрать каталоги, в которых после чистки не осталось ничего, кроме мусора ОС.

    Пустая папка от переименованной страницы выглядит дублем каталога и читается человеком
    как «синк развалился». Удаляем снизу вверх; `.DS_Store` и подобное — не содержимое.
    """
    gone = 0
    if root_link_escapes(root):       # зеркало — ссылка наружу проекта: чужое не чистим
        return gone
    for dirpath, _dirs, _files in os.walk(root, topdown=False):
        if os.path.abspath(dirpath) == os.path.abspath(root):
            continue
        # список из os.walk снят до удаления вложенных: спрашиваем файловую систему
        try:
            here = os.listdir(dirpath)
        except OSError:
            continue
        if [n for n in here if not n.startswith(".")]:
            continue
        for junk in here:
            try:
                os.remove(os.path.join(dirpath, junk))
            except OSError:
                pass
        try:
            os.rmdir(dirpath)
            gone += 1
        except OSError:
            pass
    return gone


def is_promoted_document(path: str) -> bool:
    """Файл зеркала — расшифровка скачанного документа, поднятая из вложений, а не страница.

    У такой расшифровки нет строки в состоянии синка: её не выгружали, а подняли. Считать
    её лишней значило звать закон и приказ мусором, а `--prune` снёс бы каждый, на который
    ещё не сослалась карточка. Признак ставит сам подъём — `source_kind: web-document`.
    """
    if not path.endswith(".md"):
        return False
    try:
        head = open(path, encoding="utf-8", errors="ignore").read(1500)
    except OSError:
        return False
    # Признак — поле шапки, а не подстрока: заголовок страницы с этими словами не должен
    # делать её «расшифровкой документа», защищённой от `--prune`.
    return head_scalar(split_frontmatter(head)[0] or "", "source_kind") == "web-document"


def report_stale(kind: str, extra: list, out_dir: str) -> None:
    """Одинаковый рассказ про лишние файлы: их всегда сначала показывают, потом удаляют."""
    print(f"\nЛишние файлы в зеркале ({len(extra)}) — {kind}:")
    for s in extra[:20]:
        print(f"  - {s}")
    if len(extra) > 20:
        print(f"  … ещё {len(extra) - 20}")


def no_access(script: str, prefix: str) -> str:
    return (f"{script}: нет доступа. Положите в {ENV_LOCAL} (он в .gitignore):\n"
            f"  {prefix}_PAT=<персональный токен>\n"
            f"либо {prefix}_USER= и {prefix}_PASSWORD=")
