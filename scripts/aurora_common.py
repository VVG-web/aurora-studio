#!/usr/bin/env python3
"""aurora_common.py — общие примитивы движка Аврора.

До этого модуля парсер frontmatter жил в восьми скриптах, регулярка wiki-ссылок — в
восьми, карта гомоглифов — в двух. Любая правка требовала повторить её везде, и однажды
кто-то бы забыл. Здесь — единственная реализация того, что нужно всем.

Модуль лежит рядом со скриптами (`.opencode/scripts/`), поэтому обычный `import
aurora_common` работает: Python кладёт папку запускаемого скрипта первой в `sys.path`.
Внешних зависимостей нет.
"""
from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from datetime import date

TODAY = date.today().isoformat()
KB_ROOT = "AuroraKnowledgeDB"
# `canonical` убран из схемы в 1.10.0 (ступень не использовалась ни в одном
# проекте). Читаем его как синоним `verified`: старые базы не должны разом
# потерять доверие к карточкам. Новое знание пишется только как `verified`.
TRUSTED = ("verified", "canonical")

# Поля и статусы, выведенные из схемы. Живут здесь, а не в одном скрипте: их должны
# одинаково понимать и ремонт (`kb:retire`), и проверка готовности (`kit:doctor`).
# `trust` выведено в 1.35.0: за всё время его писали шесть скриптов и не читал
# ни один — доверие в базе выражает `status`, второе поле только путало.
RETIRED_FIELDS = ("audience", "confirmed_by", "trust")
RETIRED_STATUS = {"canonical": "verified"}

# Ссылка Obsidian: [[цель#якорь|подпись]], возможно с ! для встраивания.
LINK_RE = re.compile(r"(!?)\[\[([^\]|#]+)((?:#[^\]|]*)?)(?:\|([^\]]*))?\]\]")

# Служебные файлы, которые не являются карточками знаний.
SERVICE_NAMES = {"index.md", "_index.md", "manifest.json", "README.md"}

# Визуально неразличимые буквы: латиница ↔ кириллица.
LAT2CYR = {
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
}
CYR2LAT = {v: k for k, v in LAT2CYR.items()}


# ------------------------------------------------------------------ frontmatter

def split_frontmatter(text: str):
    """→ (head, rest) без разделителей, либо (None, None), если шапки нет."""
    if not text.startswith("---"):
        return None, None
    end = text.find("\n---", 3)
    if end == -1:
        return None, None
    return text[3:end], text[end:]


def frontmatter(text: str) -> dict:
    """Плоские поля шапки. Значения очищены от кавычек; списки остаются строкой."""
    head, _ = split_frontmatter(text)
    if head is None:
        return {}
    fm = {}
    for line in head.splitlines():
        m = re.match(r"^([\w_]+)\s*:(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return fm


def aliases(text: str) -> list:
    """Алиасы карточки: поддерживаются и inline-список, и блочный."""
    head, _ = split_frontmatter(text)
    if head is None:
        return []
    m = re.search(r"^aliases:\s*\[(.*)\]", head, re.M)
    if m:
        return [a.strip().strip('"').strip("'") for a in m.group(1).split(",") if a.strip()]
    out, inside = [], False
    for line in head.splitlines():
        if line.startswith("aliases:"):
            inside = True
            continue
        if inside:
            am = re.match(r'^\s+-\s*["\']?(.+?)["\']?\s*$', line)
            if am:
                out.append(am.group(1))
            else:
                inside = False
    return out


def body(text: str) -> str:
    """Тело карточки без frontmatter."""
    head, rest = split_frontmatter(text)
    if head is None:
        return text
    nl = rest.find("\n", 1)
    return rest[nl + 1:] if nl != -1 else ""


def set_field(head: str, key: str, value: str) -> str:
    """Проставить/заменить поле в шапке (head — без разделителей)."""
    if re.search(rf"^{key}:", head, re.M):
        return re.sub(rf"^{key}:.*$", f"{key}: {value}", head, flags=re.M)
    return head.rstrip("\n") + f"\n{key}: {value}"


def as_list(value: str) -> list:
    """`based_on: ["[[A]]", "[[B]]"]` → ['A', 'B'] (без скобок, кавычек и путей)."""
    out = []
    for x in (value or "").strip("[] ").split(","):
        x = re.sub(r"[\[\]\"']", "", x).strip()
        if x:
            out.append(os.path.splitext(os.path.basename(x))[0])
    return out


# ----------------------------------------------------------------------- имена

def fold(name: str) -> str:
    """Каноничный ключ сравнения имён: гомоглифы → латиница, нижний регистр."""
    return "".join(CYR2LAT.get(ch, ch) for ch in name).lower()


def fold_hard(name: str) -> str:
    """Ключ сравнения имён без разделителей: «ALG-014. Подготовка» == «ALG-014-Подготовка».

    Одно и то же понятие в источниках пишут по-разному: точка после кода, пробелы вместо
    дефисов, подчёркивания из экспорта. Ссылка на такое имя не битая — она просто набрана
    иначе, и заводить под неё пустую карточку значит расколоть знание надвое.
    """
    return re.sub(r"[\s\-_.,·:;]+", "", fold(name))


def fix_mixed_script(name: str) -> str:
    """Починить буквенные группы со смешанной кириллицей/латиницей.

    Направление определяется по «уликам» — буквам, которые есть только в одном алфавите:
    «АLG» → латинские L,G → «ALG»; «AИС» → кириллическая И → «АИС»; «PRОJ» → латинские P,R,J → «PROJ».
    Улик нет или они противоречат — группа не трогается: движок не угадывает.
    """
    def is_cyr(ch: str) -> bool:
        return "Ѐ" <= ch <= "ӿ"

    def is_lat(ch: str) -> bool:
        return ("A" <= ch <= "Z") or ("a" <= ch <= "z")

    out, i, n = [], 0, len(name)
    while i < n:
        if not unicodedata.category(name[i]).startswith("L"):
            out.append(name[i])
            i += 1
            continue
        j = i
        while j < n and unicodedata.category(name[j]).startswith("L"):
            j += 1
        group = name[i:j]
        has_cyr = any(is_cyr(ch) for ch in group)
        has_lat = any(is_lat(ch) for ch in group)
        if has_cyr and has_lat:
            lat_only = any(is_lat(ch) and ch not in LAT2CYR for ch in group)
            cyr_only = any(is_cyr(ch) and ch not in CYR2LAT for ch in group)
            if lat_only and not cyr_only:
                group = "".join(CYR2LAT.get(ch, ch) for ch in group)
            elif cyr_only and not lat_only:
                group = "".join(LAT2CYR.get(ch, ch) for ch in group)
        out.append(group)
        i = j
    return "".join(out)


def is_service(path: str) -> bool:
    """Служебный файл базы (индексы, манифесты, meta) — не карточка знаний."""
    base = os.path.basename(path)
    p = path.replace("\\", "/")
    return base in SERVICE_NAMES or base.startswith("_") or "/meta/" in p or "/_meta/" in p


# ------------------------------------------------------------------ обход и ссылки

def walk_md(root: str, skip_service: bool = False, skip_archive: bool = False):
    """Все markdown-файлы под корнем (пути в posix-виде)."""
    for dirpath, _, files in os.walk(root):
        p = dirpath.replace("\\", "/")
        if skip_archive and "/_archive" in p:
            continue
        for f in files:
            if not f.endswith(".md"):
                continue
            full = os.path.join(dirpath, f).replace("\\", "/")
            if skip_service and is_service(full):
                continue
            yield full


# Расширения, которые в имени карточки или вложения действительно расширения. Всё
# остальное после точки — часть названия: «ALG-3.14 Учёт операции», «Спецификация 1.2».
KNOWN_EXT = (".md", ".png", ".jpg", ".jpeg", ".svg", ".pdf", ".drawio", ".xml", ".mmd",
             ".puml", ".json", ".txt", ".docx", ".xlsx", ".pptx", ".csv")


def leaf_name(target: str) -> str:
    """Имя цели ссылки без пути, якоря и НАСТОЯЩЕГО расширения.

    `os.path.splitext` считал расширением всё после последней точки, и ссылка
    `[[ALG-3.14 Учёт операции]]` разрешалась в карточку `ALG-3` — совсем другое знание.
    """
    base = os.path.basename(target.split("#")[0].strip())
    root, ext = os.path.splitext(base)
    return root if ext.lower() in KNOWN_EXT else base


def link_targets(text: str) -> list:
    """Имена целей всех wiki-ссылок в тексте (без якорей, подписей и путей)."""
    out = []
    for m in LINK_RE.finditer(text):
        target = m.group(2).strip()
        if target.startswith("http"):
            continue
        leaf = leaf_name(target)
        if leaf:
            out.append(leaf)
    return out


def link_refs(text: str) -> list:
    """Цели ссылок «как написано» — с путями и якорями (когда важен исходный вид)."""
    return [m.group(2).strip() for m in LINK_RE.finditer(text)]


def rewrite_links(text: str, mapping: dict) -> str:
    """Переписать цели ссылок по карте {старая: новая}, сохранив якоря и подписи."""
    def sub(m):
        new = mapping.get(m.group(2).strip())
        if not new:
            return m.group(0)
        tail = f"|{m.group(4)}" if m.group(4) is not None else ""
        return f"{m.group(1)}[[{new}{m.group(3) or ''}{tail}]]"
    return LINK_RE.sub(sub, text)


# --------------------------------------------------------------------- git

def git_dirty(path: str = ".") -> list:
    """Отслеживаемые файлы с незакоммиченными правками (неотслеживаемые не мешают)."""
    try:
        out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", path],
                             capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [l for l in out.stdout.splitlines() if l.strip()]


def git_guard(path: str, allow_dirty: bool, what: str = "операция") -> bool:
    """Массовая запись по грязному дереву делает откат невозможным. → можно ли писать."""
    import sys
    dirty = git_dirty(path)
    if not dirty or allow_dirty:
        if dirty:
            print(f"⚠️  git-guard отключён: в {path}/ {len(dirty)} незакоммиченных файлов — "
                  f"правки смешаются с вашими.\n")
        return True
    print(f"❌ git-guard: в {path}/ {len(dirty)} незакоммиченных файлов.", file=sys.stderr)
    print(f"   {what.capitalize()} пишет разом во много файлов — по грязному дереву откат "
          "станет невозможным.", file=sys.stderr)
    print("   Сначала: git add -A && git commit -m 'WIP'", file=sys.stderr)
    print("   Осознанно продолжить: --allow-dirty", file=sys.stderr)
    return False


# ------------------------------------------------------------------- конфиг

def config_value(key: str, default: str = "") -> str:
    """Значение простого поля из aurora.config.yaml (без PyYAML)."""
    cfg = "aurora.config.yaml"
    if not os.path.isfile(cfg):
        return default
    m = re.search(rf'^\s*{key}\s*:\s*"?([^"\n#]+?)"?\s*$',
                  open(cfg, encoding="utf-8", errors="ignore").read(), re.M)
    return m.group(1).strip() if m else default



def config_list(key: str) -> list:
    """Список из `aurora.config.yaml` (`ключ: [a, b, "c d"]`) — без PyYAML.

    Списков в конфиге ровно четыре вида (доверенные статусы, источники, разделы), и до
    1.44.0 каждый скрипт разбирал их своим regex — четыре почти одинаковые функции.
    """
    cfg = "aurora.config.yaml"
    if not os.path.isfile(cfg):
        return []
    m = re.search(rf"^\s*{key}\s*:\s*\[([^\]]*)\]",
                  open(cfg, encoding="utf-8", errors="ignore").read(), re.M)
    return [x.strip().strip("\"'") for x in m.group(1).split(",") if x.strip()] if m else []


def inbound_counts(root: str) -> dict:
    """{имя карточки: сколько на неё ссылок из базы}.

    Считаем по ВСЕМ файлам, включая навигационные (`_index.md`, MOC): присутствие в
    индексе — тоже связность, иначе «сиротами» станет вся база. Один счёт на всех, кто
    спрашивает про сирот и про вес карточки.
    """
    stems = {os.path.splitext(os.path.basename(p))[0] for p in walk_md(root)}
    counts: dict = {}
    for path in walk_md(root):
        self_stem = os.path.splitext(os.path.basename(path))[0]
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except Exception:  # noqa: BLE001
            continue
        for leaf in link_targets(text):
            if leaf in stems and leaf != self_stem:
                counts[leaf] = counts.get(leaf, 0) + 1
    return counts


class Card:
    """Карточка базы: путь, имя, шапка, тело, раздел.

    Одна на всех, кто читает базу целиком. Раньше у `ctx_pack` и `kb_fix` были свои
    классы с одинаковой шапкой, а `kb_queue` и `aurora_stats` собирали то же самое
    словарями — четыре способа назвать одно и то же.
    """

    def __init__(self, path: str, text: str, root: str = KB_ROOT):
        self.path = path.replace("\\", "/")
        self.text = text
        self.stem = os.path.splitext(os.path.basename(self.path))[0]
        self.fm = frontmatter(text)
        self.section = os.path.relpath(os.path.dirname(self.path), root).split(os.sep)[0]

    @property
    def status(self) -> str:
        return (self.fm.get("status") or "").strip()

    @property
    def source(self) -> str:
        return (self.fm.get("source") or "").strip().strip('"').replace("\\", "/")

    @property
    def tags(self) -> str:
        return self.fm.get("tags") or ""

    @property
    def is_stub(self) -> bool:
        """Заготовка: имя есть, знания пока нет (`kb:repair --stubs`)."""
        return "заготовка" in self.tags or "_Заготовка:" in self.text

    def links(self) -> list:
        return link_targets(self.text)


def load_cards(root: str = KB_ROOT, skip_service: bool = True,
               skip_archive: bool = True) -> dict:
    """{путь: Card} — вся база одним вызовом."""
    out = {}
    for path in walk_md(root, skip_service=skip_service, skip_archive=skip_archive):
        try:
            out[path] = Card(path, open(path, encoding="utf-8", errors="ignore").read(), root)
        except Exception:  # noqa: BLE001
            continue
    return out


def card_body(text: str) -> str:
    """Тело карточки без шапки. Один разбор на всех: приёмка ставит отпечаток, линтер
    его сверяет, и расходиться в том, что считать телом, им нельзя."""
    head, rest = split_frontmatter(text)
    body = text if head is None else rest
    return body.lstrip("-\n") if head is not None else body


def body_hash(body: str) -> str:
    """Отпечаток тела карточки — без шапки и без пустых строк по краям.

    Нужен там, где важно «текст тот же или уже другой»: приёмка относится к конкретному
    тексту, а не к имени файла. Пробелы в конце строк и переносы не считаем: они меняются
    от редактора и о содержании ничего не говорят.
    """
    import hashlib
    norm = "\n".join(line.rstrip() for line in (body or "").strip().splitlines())
    return hashlib.md5(norm.encode("utf-8")).hexdigest()[:12]
