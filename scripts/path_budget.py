#!/usr/bin/env python3
"""path_budget.py — хватит ли путей проекта на Windows, macOS и Linux (фреймворк «Аврора»).

Проект — git-репозиторий, и его открывают на разных компьютерах. Путь файла, который
на одном создаётся, на другом не создаётся совсем:

  Windows  — весь путь до 259 знаков (MAX_PATH 260 вместе с завершающим нулём), папка — до 247,
             имя — до 255 знаков UTF-16; это, пока в системе не включены длинные пути;
  macOS    — путь до 1023 байт (PATH_MAX 1024 с нулём), имя — до 255 БАЙТ UTF-8: кириллица
             занимает два байта на букву, то есть около 127 букв;
  Linux    — путь до 4095 байт, имя — до 255 байт.

Корень проекта у людей разный: на Windows у коллеги он обычно `C:\\Users\\<имя>\\Documents\\…`,
длиннее, чем на Mac владельца. Поэтому проверка считает не только эту машину, но и
консервативную модель каждой системы: корень Windows, если ОС не Windows, —
`C:\\Users\\username\\Documents\\Projects\\<имя папки проекта>` (константа `WIN_ASSUMED_ROOT`,
не настройка файла; она показана в каждом сообщении).

Проверка ничего не меняет и никого не останавливает: путь, который владелец записал в свой
конфиг, не ограничивается и не «исправляется» — о нём только сообщается, строкой конфига,
длиной, пределом, системой и тем, что из-за этого не загрузится. Без сети, чтение папок —
`os.scandir`, потолок числа файлов `SCAN_FILES_MAX`; упёрлись — результат помечен «проверено частично».

  python3 .aurora/scripts/path_budget.py            # проверка и список строк
  python3 .aurora/scripts/path_budget.py --json

Панель: `kit:paths`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations
import aurora_common  # noqa: F401 — защита версии Python: на старом — понятный текст, а не traceback

import os
import sys
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, field

# ------------------------------------------------------------------ пределы систем

SYSTEMS = ("windows", "macos", "linux")
LABEL = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}
WIN_PATH = 259          # путь файла: MAX_PATH 260 включает завершающий нуль
WIN_DIR = 247           # папку без длинных путей создают, пока путь ≤ MAX_PATH − 12 (место под имя 8.3)
WIN_NAME = 255          # одно имя, знаки UTF-16
MAC_PATH = 1023         # PATH_MAX 1024 включает завершающий нуль
LIN_PATH = 4095
NAME_BYTES = 255        # одно имя macOS и Linux, байты UTF-8
LIMITS = {"windows": {"path": WIN_PATH, "dir": WIN_DIR, "name": WIN_NAME},
          "macos": {"path": MAC_PATH, "dir": MAC_PATH, "name": NAME_BYTES},
          "linux": {"path": LIN_PATH, "dir": LIN_PATH, "name": NAME_BYTES}}

# Корень проекта, каким его считает проверка, когда система — не эта машина. Константа кода,
# а не настройка: у коллеги корень другой, и «правильного» значения нет — есть разумное допущение.
WIN_ASSUMED_ROOT = "C:\\Users\\username\\Documents\\Projects"
MAC_ASSUMED_ROOT = "/Users/username/Documents/Projects"
LIN_ASSUMED_ROOT = "/home/username/Projects"
ASSUMED_BASE = {"windows": WIN_ASSUMED_ROOT, "macos": MAC_ASSUMED_ROOT, "linux": LIN_ASSUMED_ROOT}

SERVICE_FILES = ("sync_state.md", "update_log.md")   # состояние синка — не страница
SCAN_FILES_MAX = 60000  # файлов за одну проверку, все папки вместе; дальше — «проверено частично»


def current_system() -> str:
    return "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux"


def measure(system: str, text: str) -> int:
    """Длина строки в единицах системы: знаки UTF-16 у Windows, байты UTF-8 у macOS и Linux."""
    if text.isascii():
        return len(text)
    if system == "windows":
        return len(text.encode("utf-16-le", "surrogatepass")) // 2
    return len(text.encode("utf-8", "surrogateescape"))


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def root_models(root: str, current: str | None = None, roots: dict | None = None) -> dict:
    """{система: корень проекта}: у системы этой машины — настоящий путь, у остальных — допущение.

    `roots` задаёт корни целиком (проверки и разбор чужого случая); остальное достраивается."""
    current = current or current_system()
    real = os.path.abspath(root)
    name = os.path.basename(real.rstrip("/\\")) or "project"
    out = {}
    for system in SYSTEMS:
        sep = "\\" if system == "windows" else "/"
        out[system] = (real if system == current
                       else ASSUMED_BASE[system] + sep + name)
    out.update(roots or {})
    return out


# ------------------------------------------------------------------ что нашли

@dataclass
class Finding:
    """Одна строка конфига, у которой на какой-то системе не хватает места.

    `level`: `fail` — это уже есть на диске или заведомо не поместится; `risk` — худший случай
    по правилам именования выгрузки, на диске его может ещё не быть. `kind`: `path` — путь
    файла, `dir` — путь папки (Windows создаёт папку по более строгому пределу), `name` — одно
    имя, `prefix` — папка источника настолько длинна, что в неё не поместится даже самый короткий файл.
    """
    key: str                # строка для формы: `root:<page_id>`, `web:<адрес>`, `sources:<id>`, `path:<ключ>`…
    line: str               # понятное название строки конфига
    source: str             # источник: Confluence, Web, Jira, База знаний, Папка…
    system: str             # Windows | macOS | Linux | macOS и Linux
    kind: str               # path | dir | name | prefix
    level: str              # fail | risk
    length: int             # посчитанная длина
    limit: int              # предел системы
    count: int = 0          # сколько файлов затронуто (для fail)
    sample: str = ""        # самый длинный путь или имя, укороченное
    root: str = ""          # корень проекта, от которого считали
    root_len: int = 0
    cfg_line: int = 0       # строка aurora.config.yaml; 0 — не нашлась
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class Tally:
    """Длины путей в папке: гистограммы по файлам и папкам и самые длинные имена.

    Пути считаются внутри папки (от неё, с «/»), а длина целого пути получается прибавлением
    корня и самой папки — так одну папку можно мерить для корней разных систем, не обходя заново.
    """

    def __init__(self):
        self.files = 0
        self.pages = 0                  # файлы-страницы (`.md`, кроме служебных): есть ли в зеркале что мерить
        self.w: Counter = Counter()     # длина файла внутри папки, знаки UTF-16
        self.b: Counter = Counter()     # то же, байты UTF-8
        self.dw: Counter = Counter()    # длина вложенных папок, знаки UTF-16
        self.name_w = (0, "")
        self.name_b = (0, "")
        self.long_w = (0, "")
        self.long_b = (0, "")

    def add(self, rel: str, name: str, is_dir: bool) -> None:
        w, b = measure("windows", rel), measure("macos", rel)
        nw, nb = measure("windows", name), measure("macos", name)
        if nw > self.name_w[0]:
            self.name_w = (nw, name)
        if nb > self.name_b[0]:
            self.name_b = (nb, name)
        if is_dir:
            self.dw[w] += 1
            return
        self.files += 1
        self.pages += 1 if name.endswith(".md") and not name.startswith(".") and name not in SERVICE_FILES else 0
        self.w[w] += 1
        self.b[b] += 1
        if w > self.long_w[0]:
            self.long_w = (w, rel)
        if b > self.long_b[0]:
            self.long_b = (b, rel)

    def merge(self, other: "Tally") -> None:
        """Прибавить сводку другой папки (того же корня): так считается «всё, кроме корней Confluence»."""
        self.files += other.files
        self.pages += other.pages
        for mine, theirs in ((self.w, other.w), (self.b, other.b), (self.dw, other.dw)):
            mine.update(theirs)
        for attr in ("name_w", "name_b", "long_w", "long_b"):
            if getattr(other, attr)[0] > getattr(self, attr)[0]:
                setattr(self, attr, getattr(other, attr))

    @staticmethod
    def over(hist: Counter, limit: int) -> int:
        return sum(n for length, n in hist.items() if length > limit)


class Scan:
    """Итог обхода папки: общая сводка, сводки по верхним уровням, обрыв по потолку."""

    def __init__(self):
        self.exists = False
        self.total = Tally()
        self.tops: dict = {}
        self.partial = False
        self.unreadable = 0


def owner_top(name: str) -> str:
    """Верхний уровень зеркала вики: папка страницы, её файл и её `_assets` — одна страница."""
    if name.endswith(".md"):
        name = name[:-3]
    return name[:-7] if name.endswith("_assets") else name


def scan(folder: str, tops: bool = False, left: list | None = None) -> Scan:
    """Обойти папку и посчитать длины. Ссылки не раскрываются, порядок обхода — по имени.

    `left` — общий счётчик оставшихся файлов (`[число]`) на все папки одной проверки: кончился —
    обход останавливается, результат помечен `partial`. `tops` — отдельно по первому уровню."""
    res = Scan()
    if left is None:
        left = [SCAN_FILES_MAX]
    if not os.path.isdir(folder):
        return res
    res.exists = True
    stack = [(folder, "", "")]
    while stack:
        here, inner, top = stack.pop()
        try:
            with os.scandir(here) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            res.unreadable += 1
            continue
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                is_dir = e.is_dir(follow_symlinks=False)
                if not is_dir and not e.is_file(follow_symlinks=False):
                    continue
            except OSError:
                res.unreadable += 1
                continue
            if not is_dir and left[0] <= 0:
                res.partial = True
                return res
            name = nfc(e.name)
            if is_dir and name.startswith("."):
                continue            # `.git`, `.obsidian`: не содержимое проекта
            rel = f"{inner}/{name}" if inner else name
            mine = top or (owner_top(name) if tops else "")
            res.total.add(rel, name, is_dir)
            if tops and mine:
                res.tops.setdefault(mine, Tally()).add(rel, name, is_dir)
            if is_dir:
                stack.append((e.path, rel, mine))
            else:
                left[0] -= 1
    return res
