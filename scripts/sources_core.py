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
import unicodedata
import urllib.request
from datetime import date

CONFIG = "aurora.config.yaml"
ENV_LOCAL = ".env.aurora.local"
TODAY = date.today().isoformat()
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
    m = re.search(rf'^\s*{re.escape(key)}\s*:\s*"?([^"\n#]+?)"?\s*$', text, re.M)
    return m.group(1).strip() if m else default


def read_env() -> dict:
    """Окружение плюс `.env.aurora.local` (он в .gitignore). Секреты наружу не печатаем."""
    env = dict(os.environ)
    if os.path.isfile(ENV_LOCAL):
        for line in open(ENV_LOCAL, encoding="utf-8", errors="ignore"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def read_secret(prefix: str) -> tuple:
    """→ (заголовок Authorization, как назвали способ).

    Имена переменных выводятся из префикса модуля: `CONFLUENCE` → `CONFLUENCE_PAT`,
    `CONFLUENCE_PERSONAL_TOKEN`, `CONFLUENCE_USER` + `CONFLUENCE_PASSWORD`.
    """
    env = read_env()
    pat = env.get(f"{prefix}_PAT") or env.get(f"{prefix}_PERSONAL_TOKEN")
    if pat:
        return f"Bearer {pat}", "PAT"
    user, pwd = env.get(f"{prefix}_USER"), env.get(f"{prefix}_PASSWORD")
    if user and pwd:
        return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode(), "basic"
    return "", ""


# --------------------------------------------------------------------- REST

class RestApi:
    """Минимальный REST-клиент: токен в заголовке, JSON на выходе, свой User-Agent.

    Ничего на сервер источника не ставится, третьих библиотек не нужно — синк должен
    заводиться там, где есть только стандартный Python.
    """

    agent = "aurora-sync/1.0"

    def __init__(self, base: str, auth: str):
        self.base, self.auth = base.rstrip("/"), auth

    def get(self, path: str) -> dict:
        url = path if path.startswith("http") else self.base + path
        req = urllib.request.Request(url, headers={
            "Authorization": self.auth, "Accept": "application/json",
            "User-Agent": self.agent})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def fetch(self, url: str) -> bytes:
        """Сырое тело по адресу: вложения (схемы, диаграммы) — не JSON."""
        req = urllib.request.Request(url if url.startswith("http") else self.base + url,
                                     headers={"Authorization": self.auth,
                                              "User-Agent": self.agent})
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read()


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
            # чистка сносила ровно то, что синк только что скачал.
            if ASSET_DIR_RE.search(rel):
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
