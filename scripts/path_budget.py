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
import re
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


# ------------------------------------------------------------------ строки конфига

def _scalar(value: str) -> str:
    """Значение скаляра YAML: кавычки снимаются, комментарий в конце строки отбрасывается."""
    value = value.strip()
    if value[:1] in ("\"", "'"):
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else value[1:]
    return re.sub(r"\s+#.*$", "", value).strip()


def _inline_items(text: str) -> list:
    """Пункты `[a, "b, c", d]` в строку."""
    out, cur, quote = [], "", ""
    for ch in text:
        if quote:
            if ch == quote:
                quote = ""
            else:
                cur += ch
        elif ch in "\"'":
            quote = ch
        elif ch == ",":
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return [x for x in out if x]


def config_items(text: str) -> dict:
    """{путь: (значение, номер строки)} — скаляры и пункты списков `aurora.config.yaml`.

    Путь — кортеж ключей и номеров пунктов: `("paths", "knowledge_db")`, `("web", "pages", 1, "url")`.
    Разбор нужен, чтобы назвать строку конфига с её номером; значения, от которых зависит работа
    движка, он читает своими читателями, а здесь они только сверяются по номеру. Подмножество
    YAML, на котором стоит конфиг: отображения, списки в столбик и в скобках, пункты-отображения."""
    out: dict = {}
    stack: list = []            # (отступ, ключ или номер пункта, "key" | "item")
    seq: dict = {}              # путь родителя -> номер последнего пункта
    block = -1                  # отступ ключа с блочным скаляром («|», «>»): его строки не читаем
    for no, raw in enumerate(text.replace("\t", "  ").splitlines(), 1):
        body = raw.strip()
        if not body or body.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if block >= 0:
            if indent > block:
                continue
            block = -1
        if body == "-" or body.startswith("- "):
            while stack and (stack[-1][0] > indent or (stack[-1][0] == indent and stack[-1][2] != "key")):
                stack.pop()
            parent = tuple(k for _i, k, _t in stack)
            idx = seq.get(parent, -1) + 1
            seq[parent] = idx
            stack.append((indent, idx, "item"))
            rest = body[1:].lstrip(" ")
            if not rest:
                continue
            indent += len(body) - len(rest)
            body = rest
            if not re.match(r"""[\w.\-/]+\s*:(\s|$)""", body):
                out[tuple(k for _i, k, _t in stack)] = (_scalar(body), no)
                continue
        else:
            while stack and stack[-1][0] >= indent:
                stack.pop()
        m = re.match(r"""([\w.\-/]+)\s*:(?:\s+(.*))?$""", body)
        if not m:
            continue
        key, value = m.group(1), (m.group(2) or "").strip()
        stack.append((indent, key, "key"))
        path = tuple(k for _i, k, _t in stack)
        if value[:1] in ("|", ">"):
            block = indent
        elif value.startswith("["):
            for n, item in enumerate(_inline_items(value[1:value.rfind("]")] if "]" in value else value[1:])):
                out[path + (n,)] = (_scalar(item), no)
        elif value and not value.startswith("#"):
            out[path] = (_scalar(value), no)
    return out


# ------------------------------------------------------------------ что и как ложится на диск

# Сколько места нужно под самый короткий файл источника: меньше — не поместится ничего.
MIN_TAIL = {"confluence": 14, "wiki": 14, "web": 14, "jira": 8, "board": 8, "kb": 12, "free": 12}
JIRA_KEY_MAX = 10 + 1 + 7       # ключ проекта до 10 знаков, номер задачи до 7 цифр
WEB_STEM = 60                   # основа имени вложения сайта (`web_export.asset_name`)
WEB_FILES = "_files"            # `web_export.ASSET_DIR`

EXPORTERS = {"confluence_export.py": "confluence", "web_export.py": "web", "jira_export.py": "jira"}


@dataclass
class Res:
    """Строка конфига, которая называет место на диске: папку источника, папку или файл проекта."""
    key: str
    line: str
    source: str
    kind: str               # confluence | web | jira | wiki | board | kb | free | file
    path: str
    cfg_line: int = 0
    rows: list = field(default_factory=list)    # строки внутри источника: корни Confluence, адреса сайта


def abs_prefix(system: str, root: str, rel: str):
    """Путь `rel` из конфига на системе `system` при корне проекта `root`; None — к системе не относится.

    Путь с диском Windows (`D:\\…`) относится только к Windows, абсолютный POSIX — только к macOS и
    Linux: чужой системе он ничего не значит и в её модель не входит. `..` — переход в родительскую
    папку корня, `.` пропускается."""
    rel = (rel or "").strip().replace("\\", "/")
    win_abs = bool(re.match(r"^[A-Za-z]:(/|$)|^//", rel))
    if win_abs or rel.startswith("/"):
        if win_abs != (system == "windows"):
            return None
        rel = rel.rstrip("/") or "/"
        return rel.replace("/", "\\") if system == "windows" else rel
    parts = [p for p in re.split(r"[\\/]+", root) if p]
    lead = ""
    if system == "windows":
        lead = "\\\\" if root.startswith("\\\\") else ""
        if parts and re.match(r"^[A-Za-z]:$", parts[0]):
            lead, parts = parts[0] + "\\", parts[1:]
    else:
        lead = "/"
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return lead + ("\\" if system == "windows" else "/").join(parts)


def short(path: str, width: int = 96) -> str:
    """Путь для сообщения: начало и хвост, середина — многоточие."""
    return path if len(path) <= width else path[:width // 2 - 1] + "…" + path[-(width // 2):]


def web_names(url: str) -> list:
    """Самые длинные имена файлов страницы сайта по правилу `web_export.slug`: с заголовком в 60
    букв и без заголовка (имя берётся от хоста). Правило — в самом экспортёре, не копия."""
    try:
        import web_export as W
        return [W.slug(url, "я" * W.NAME_CHARS), W.slug(url, "")]
    except Exception:  # noqa: BLE001 — нет экспортёра или он не импортируется: имена не проверяем
        return []


def resources(root: str, text: str, cfg: dict) -> list:
    """Строки конфига, которые называют место на диске → [Res]. Порядок — как в конфиге."""
    out: list = []

    def get(*path):
        return cfg.get(tuple(path), ("", 0))

    try:
        import sources_registry as R
        inst, declared = R.instances(root), R.has_section(text)
        order = [k for k, (m, _i, _p, _r) in enumerate(R.declared_chunks(text)) if m]
    except Exception:  # noqa: BLE001 — нет реестра или он не разобрал конфиг: остальные строки проверяются
        inst, declared, order = [], False, []
    seen: dict = {}
    for n, it in enumerate(inst):
        kind = EXPORTERS.get(it.get("script") or "") or it.get("kind") or "free"
        if not it.get("path"):
            continue
        if declared and n < len(order):
            line, label = get("sources", order[n], "path")[1], f"sources: {it['id']} → path"
        else:
            legacy = ((it.get("manifest") or {}).get("mirror") or {}).get("legacy_path_key") or "sources_confluence"
            line, label = get("paths", legacy)[1], f"paths → {legacy}"
        res = Res(f"sources:{it['id']}", label, it["id"], kind, it["path"], line)
        out.append(res)
        seen.setdefault((kind, it["path"].strip("/").casefold()), res)
    # Пути, которые читают сами выгрузчики (`paths`), могут расходиться с реестром `sources:`.
    for kind, key, name in (("confluence", "sources_confluence", "Confluence"), ("jira", "sources_jira", "Jira")):
        value, line = get("paths", key)
        if value and (kind, value.strip("/").casefold()) not in seen:
            out.append(Res(f"path:{key}", f"paths → {key}", name, kind, value, line))
    mirrors = {r.kind: r for r in out if r.kind in ("confluence", "web")}
    if "confluence" in mirrors:
        for path, (page_id, line) in cfg.items():
            if len(path) >= 3 and path[-1] == "page_id" and path[-3] == "sync_roots" and page_id.isdigit():
                title = cfg.get(path[:-1] + ("title",), ("", 0))[0]
                mirrors["confluence"].rows.append({
                    "key": f"root:{page_id}", "id": page_id, "title": title, "cfg_line": line,
                    "line": f"atlassian → confluence → sync_roots → строка {path[-2] + 1}"})
    if "web" in mirrors:
        for path, (url, line) in cfg.items():
            if path[:2] == ("web", "pages") and path[-1] == "url" and len(path) == 4:
                mirrors["web"].rows.append({
                    "key": f"web:{url}", "id": url, "title": "", "cfg_line": line,
                    "line": f"web → pages → строка {path[2] + 1}"})
    value, line = get("paths", "knowledge_db")
    out.append(Res("path:knowledge_db", "paths → knowledge_db", "База знаний", "kb",
                   value or "AuroraKnowledgeDB", line))
    slug = get("project", "slug")[0]
    for path, (value, line) in cfg.items():
        if not value:
            continue
        if path[:2] == ("paths", "extra_structure_dirs") and len(path) == 3:
            out.append(Res(f"extra:{value}", f"paths → extra_structure_dirs → строка {path[2] + 1}",
                           "Папка проекта", "free", value, line))
        elif len(path) == 3 and path[0] == "artifacts" and path[2] == "out":
            out.append(Res(f"artifacts:{path[1]}", f"artifacts → {path[1]} → out", "Артефакты", "free", value, line))
        elif len(path) == 3 and path[0] == "reports" and path[2] in ("output", "data_dir"):
            file_out = path[2] == "output"
            out.append(Res(f"reports:{path[1]}:{path[2]}", f"reports → {path[1]} → {path[2]}", "Отчёты",
                           "file" if file_out else "free", value.replace("{project}", slug or "project"), line))
    return out


# ------------------------------------------------------------------ проверка

@dataclass
class Report:
    findings: list
    models: dict            # система -> корень проекта, от которого считали
    resources: int = 0      # сколько строк конфига проверено
    files: int = 0          # сколько файлов просмотрено в зеркалах и папках
    partial: bool = False   # обход упёрся в потолок: проверено частично

    def keys(self) -> list:
        """Строки конфига с находками — без повторов, в порядке находок."""
        return list(dict.fromkeys(f.key for f in self.findings))

    def as_dict(self) -> dict:
        return {"findings": [f.as_dict() for f in self.findings], "lines": len(self.keys()),
                "models": self.models, "resources": self.resources, "files": self.files,
                "partial": self.partial}


class _Owner:
    """Чья находка: строка конфига (или строка внутри источника) — ключ, подпись, номер строки."""

    def __init__(self, res: Res, row: dict | None = None):
        self.key = row["key"] if row else res.key
        self.line = row["line"] if row else res.line
        self.source = res.source
        self.cfg_line = row["cfg_line"] if row else res.cfg_line


def _find(owner, system: str, models: dict, **kw) -> Finding:
    extra = kw.pop("extra", {})
    return Finding(key=owner.key, line=owner.line, source=owner.source, system=LABEL[system],
                   root=models[system], root_len=measure(system, models[system]),
                   cfg_line=owner.cfg_line, extra=extra, **kw)


def _facts(owner, tally: Tally, system: str, models: dict, base: int, rel: str, partial: bool) -> list:
    """Файлы и папки, которые уже лежат в зеркале и не поместятся на этой системе при этом корне."""
    out = []
    lim = LIMITS[system]
    hist, longest = (tally.w, tally.long_w) if system == "windows" else (tally.b, tally.long_b)
    over = Tally.over(hist, lim["path"] - base - 1)
    if over:
        out.append(_find(owner, system, models, kind="path", level="fail", count=over,
                         length=base + 1 + longest[0], limit=lim["path"],
                         sample=short(f"{rel}/{longest[1]}" if rel else longest[1]),
                         extra={"effect": "files", "partial": partial}))
    if system == "windows" and tally.dw and not over:      # папка глубже предела при файлах в пределе — редкость
        deep = Tally.over(tally.dw, lim["dir"] - base - 1)
        if deep:
            out.append(_find(owner, system, models, kind="dir", level="fail", count=deep,
                             length=base + 1 + max(tally.dw), limit=lim["dir"],
                             extra={"effect": "files", "partial": partial}))
    return out


def _names(owner, name_w: tuple, name_b: tuple, models: dict, level: str = "fail", effect: str = "files") -> list:
    """Имена длиннее предела: Windows — знаки UTF-16, macOS и Linux — байты UTF-8."""
    out = []
    for system, (size, name) in (("windows", name_w), ("macos", name_b)):
        if size > LIMITS[system]["name"]:
            f = _find(owner, system, models, kind="name", level=level, length=size,
                      limit=LIMITS[system]["name"], sample=short(name, 60), extra={"effect": effect})
            if system == "macos":
                f.system = "macOS и Linux"
            out.append(f)
    return out


def _state_tops(folder: str) -> dict:
    """{номер страницы: верхний уровень зеркала} по состоянию синка: к какому корню относятся файлы."""
    try:
        from sources_core import WikiMirror
        cells = WikiMirror(folder).state_cells()
    except Exception:  # noqa: BLE001 — нет состояния: корни без отдельных находок
        return {}
    return {c[1]: owner_top(nfc(c[3]).split("/")[0]) for c in cells if len(c) >= 4 and c[1].isdigit() and c[3]}


def _check_folder(root: str, res: Res, models: dict, scans: dict, left: list, report: Report) -> list:
    rel = res.path.strip().replace("\\", "/")
    out: list = []
    folder = os.path.normpath(os.path.join(root, rel)) if rel else ""
    real, here = os.path.realpath(folder), os.path.realpath(root)
    scannable = (res.kind != "file" and rel and here != real and not here.startswith(real.rstrip(os.sep) + os.sep))
    tree = res.kind in ("confluence", "wiki")
    cache_key = (folder, tree)
    if scannable and cache_key not in scans:
        scans[cache_key] = scan(folder, tops=tree, left=left)
        report.files += scans[cache_key].total.files
        report.partial = report.partial or scans[cache_key].partial
    sc = scans.get(cache_key) if scannable else None
    owner = _Owner(res)
    # Тали: строка внутри источника и её доля зеркала. Корни Confluence — по состоянию синка.
    tallies = []
    if sc and sc.exists and tree and res.rows:
        tops, mapped = _state_tops(folder), set()
        for row in res.rows:
            top = tops.get(row["id"])
            if top and top in sc.tops:
                tallies.append((_Owner(res, row), sc.tops[top]))
                mapped.add(top)
        rest = Tally()
        for top, t in sc.tops.items():
            if top not in mapped:
                rest.merge(t)
        tallies.append((owner, rest if mapped else sc.total))
    elif sc and sc.exists:
        tallies.append((owner, sc.total))
    for who, tally in tallies:
        out += _names(who, tally.name_w, tally.name_b, models)
    parts = [p for p in re.split(r"[\\/]+", rel) if p not in ("", ".", "..")]
    longest = {s: max(parts, key=lambda p: measure(s, p), default="") for s in ("windows", "macos")}
    out += _names(owner, *[(measure(s, longest[s]), longest[s]) for s in ("windows", "macos")], models)
    for system in SYSTEMS:
        pre = abs_prefix(system, models[system], rel)
        if pre is None:
            continue
        base, lim = measure(system, pre), LIMITS[system]
        if res.kind == "file":
            if base > lim["path"]:
                out.append(_find(owner, system, models, kind="path", level="fail", length=base,
                                 limit=lim["path"], sample=short(pre), extra={"effect": "file"}))
            continue
        if system == "windows" and base > lim["dir"]:
            out.append(_find(owner, system, models, kind="dir", level="fail", length=base, limit=lim["dir"],
                             sample=short(pre), extra={"effect": "none_fits"}))
            continue
        if base + 1 + MIN_TAIL[res.kind] > lim["path"]:
            out.append(_find(owner, system, models, kind="prefix", level="fail", length=base,
                             limit=lim["path"], extra={"effect": "none_fits", "need": MIN_TAIL[res.kind]}))
            continue
        for who, tally in tallies:
            out += _facts(who, tally, system, models, base, rel, bool(sc and sc.partial))
        out += _risks(res, system, models, base, tallies)
    return out


def _risks(res: Res, system: str, models: dict, base: int, tallies: list) -> list:
    """Худший случай по правилам именования выгрузки — там, где на диске ещё нечего измерить."""
    lim = LIMITS[system]
    owner = _Owner(res)
    seen_pages = any(t.pages for _w, t in tallies)
    if res.kind == "confluence" and not seen_pages:
        # движок держит путь страницы от корня проекта в `PATH_CHARS`; корень длиннее — запас кончается
        worst = measure(system, models[system]) + 1 + aurora_common.PATH_CHARS
        if worst > lim["path"]:
            return [_find(owner, system, models, kind="path", level="risk", length=worst, limit=lim["path"],
                          extra={"effect": "tree", "engine": aurora_common.PATH_CHARS})]
    if res.kind == "jira" and not seen_pages:
        worst = base + 1 + measure(system, "K" * JIRA_KEY_MAX + ".md")
        if worst > lim["path"]:
            return [_find(owner, system, models, kind="path", level="risk", length=worst, limit=lim["path"],
                          extra={"effect": "issue"})]
    if res.kind == "web" and not seen_pages:
        out = []
        for row in res.rows:
            worst = max([base + 1 + measure(system, n) for n in web_names(row["id"])] or [0])
            if worst > lim["path"]:
                out.append(_find(_Owner(res, row), system, models, kind="path", level="risk", length=worst,
                                 limit=lim["path"], extra={"effect": "page"}))
        if out:
            return out
        stem = "я" * WEB_STEM + "-" + "0" * 8 + ".converted.md"
        worst = base + 1 + len(WEB_FILES) + 1 + measure(system, stem)
        if worst > lim["path"]:
            return [_find(owner, system, models, kind="path", level="risk", length=worst, limit=lim["path"],
                          extra={"effect": "assets"})]
    return []


def assess(root: str, roots: dict | None = None, current: str | None = None) -> Report:
    """Проверка проекта `root`: каждая строка конфига, которая называет место на диске.

    Ничего не пишет и не ходит в сеть. `roots` и `current` — для проверок: корни систем и система
    «этой машины». Потолок обхода — `SCAN_FILES_MAX` файлов на всё сразу."""
    try:
        with open(os.path.join(root, "aurora.config.yaml"), encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return Report([], root_models(root, current, roots))
    models = root_models(root, current, roots)
    res = resources(root, text, config_items(text))
    report = Report([], models, resources=len(res))
    left, scans = [SCAN_FILES_MAX], {}
    for r in res:
        report.findings += _check_folder(root, r, models, scans, left, report)
    here = current or current_system()
    for f in report.findings:
        # корень чужой системы — допущение, и в сообщении это сказано
        f.extra["assumed"] = f.system.split()[0].lower() != here
    return report


# ------------------------------------------------------------------ слова

def _plural(n: int, forms: tuple) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return forms[0]
    return forms[1] if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else forms[2]


EFFECT = {
    "files": "эти файлы не создадутся при клонировании репозитория (git сообщит «Filename too long»), "
             "а синк не сможет их обновить",
    "tree": "страницы глубже запаса не будут записаны: синк назовёт их строкой в итоге, а при клонировании "
            "такие файлы не создадутся",
    "page": "страница сайта не будет сохранена",
    "assets": "вложения сайта (`_files/`) не будут сохранены",
    "issue": "задача не будет записана",
    "none_fits": "в эту папку не поместится ни один файл",
    "file": "файл не создастся",
}
HINT_WINDOWS = ("сократите имя папки проекта или перенесите проект ближе к корню диска; либо включите длинные "
                "пути Windows (LongPathsEnabled) и `git config core.longpaths true`")


def describe(f: Finding) -> str:
    """Находка одной строкой по-русски: строка конфига, что и насколько превышено, что из-за этого будет."""
    win = f.system == "Windows"
    u = "знаков" if win else "байт"
    where = f"корень проекта {f.root}, {f.root_len} {u}" + (" — допущение" if f.extra.get("assumed") else "")
    if f.kind == "name":
        what = f"имя «{f.sample}» — {f.length} {u} при пределе {f.limit} на {f.system}"
    elif f.kind == "prefix":
        what = (f"путь папки занимает {f.length} из {f.limit} {u} на {f.system}, а самому короткому файлу нужно "
                f"ещё {f.extra.get('need', 0) + 1} ({where})")
    elif f.kind == "dir":
        what = f"путь папки — {f.length} {u} при пределе {f.limit} на {f.system} ({where})"
    elif f.level == "risk":
        what = f"в худшем случае путь достигнет {f.length} {u} при пределе {f.limit} на {f.system} ({where})"
    elif f.count:
        what = (f"не поместится {f.count} {_plural(f.count, ('файл', 'файла', 'файлов'))}: самый длинный путь — "
                f"{f.length} {u} при пределе {f.limit} на {f.system} ({where})")
    else:
        what = f"путь — {f.length} {u} при пределе {f.limit} на {f.system} ({where})"
    if win and f.kind != "name":
        what += ", если длинные пути Windows не включены"
    effect = EFFECT.get(f.extra.get("effect", ""), "")
    advice = ("Сократите имя." if f.kind == "name"
              else HINT_WINDOWS[0].upper() + HINT_WINDOWS[1:] + "." if win
              else "Сократите путь папки в конфиге или перенесите проект ближе к корню диска.")
    out = f"{f.line}: {what}" + (f" — {effect}" if effect else "") + "."
    if f.sample and f.kind != "name":
        out += f" Пример: {f.sample}."
    return f"{out} {advice}" + (" Проверено частично." if f.extra.get("partial") else "")


def hint(report: Report, width: int = 3) -> str:
    """Подсказка для старта прогона и итога: что не загрузится и какие строки тому виной. Пусто — всё в порядке."""
    names = []
    for f in report.findings:
        if f.line not in names:
            names.append(f.line)
    if not names:
        return ""
    shown = "; ".join(names[:width]) + (f"; ещё {len(names) - width}" if len(names) > width else "")
    return f"длинные пути: не всё будет загружено — {shown}"


def render(report: Report) -> str:
    """Отчёт для терминала: корни по системам, находки по строкам, подсказка."""
    lines = [f"Длинные пути: проверено строк конфига {report.resources}, файлов просмотрено {report.files}"
             + (" — проверено частично (потолок обхода)" if report.partial else "")]
    for system in SYSTEMS:
        mark = " (допущение)" if system != current_system() else ""
        lines.append(f"  {LABEL[system]}: корень проекта {report.models[system]}{mark}")
    if not report.findings:
        return "\n".join(lines + ["Всё помещается: ни одна строка конфига не даст ошибки длины пути."])
    lines.append("")
    lines += [f"✗ {describe(f)}" for f in report.findings]
    lines.append("")
    lines.append("Запуск возможен, но не всё будет загружено: " + hint(report).split(" — ", 1)[1] + ".")
    return "\n".join(lines)


def main() -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Длина путей проекта на Windows, macOS и Linux (только чтение)")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    a = ap.parse_args()
    report = assess(os.getcwd())
    if a.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(report))
    return 1 if report.findings else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    sys.exit(main())
