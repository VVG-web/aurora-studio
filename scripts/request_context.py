"""Контекст запроса человека: файлы и папки проекта по `@`, вложения, навыки по `/`, MCP по `@`.

Одно правило на движок. «Продуктивность» и всё, что дальше научится принимать запрос
человека, разбирают упоминания здесь, а не каждый по-своему:

- `@путь` или `@"путь с пробелами"` — файл или папка проекта: текст идёт в задание модели;
- вложение — внешний текстовый файл, сохранённый панелью в `.opencode/context/<день>/`;
- `/имя` — навык (SKILL.md): проект → кит → `~/.claude/skills`; его метод идёт в задание;
- `@имя` MCP-сервера — сервер подключается к прогону сразу, остальные — по требованию.

Секреты в задание не попадают никогда: `.env*`, ключи, `.git`, `local/` и настройки MCP
отсекаются по имени до чтения — даже если человек назвал их прямо.
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

CONTEXT_DIR = Path(".opencode") / "context"   # вложения запросов; закрыто .gitignore
KEEP_DAYS = 14                                 # дольше вложения не живут: это не архив
FILE_CAP = 60_000                              # знаков с одного файла
TOTAL_CAP = 160_000                            # знаков на все файлы запроса
FOLDER_FILES = 40                              # файлов из одной папки
UPLOAD_CAP = 1_000_000                         # байт на вложение

TEXT_EXT = {
    ".md", ".markdown", ".txt", ".text", ".rst", ".adoc", ".tex", ".log",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties",
    ".csv", ".tsv", ".xml", ".xsd", ".wsdl", ".html", ".htm", ".svg", ".bpmn", ".dmn",
    ".puml", ".plantuml", ".mmd", ".graphql", ".proto", ".sql",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".css",
    ".scss", ".less", ".java", ".kt", ".kts", ".scala", ".go", ".rs", ".rb", ".php",
    ".cs", ".fs", ".vb", ".c", ".h", ".cpp", ".hpp", ".cc", ".m", ".mm", ".swift",
    ".r", ".jl", ".lua", ".pl", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".gradle", ".groovy", ".tf", ".hcl", ".feature", ".gherkin", ".robot",
}
TEXT_NAMES = {"dockerfile", "makefile", "readme", "license", "changelog", "procfile"}

# Никогда — ни по ссылке, ни в папке: секреты и служебное.
NEVER_DIRS = {".git", "node_modules", "local", ".venv", "venv", "__pycache__", ".sisyphus",
              "runs", "state"}
SECRET_NAME = re.compile(r"^(\.env.*|.*\.(pem|key|p12|pfx|jks|keystore)|id_(rsa|ed25519|ecdsa)\w*|"
                         r"\.?mcp\.json|\.netrc|\.npmrc|\.pypirc|credentials(\.\w+)?)$", re.I)

SKILL_ALIASES = {"grill-me": "aurora-grill", "grilling": "aurora-grill"}
MENTION_SKILL = re.compile(r"(?<![\w/.:\-])/([A-Za-z][\w.\-]{0,63})\b")
MENTION_AT = re.compile(r'(?<![\w@])@(?:"([^"\n]{1,300})"|([^\s,;!?()«»"]{1,300}))')


def utc_day(when=None) -> str:
    """Папка дня вложений — по общему правилу времени движка (UTC, `aurora_common`)."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from aurora_common import utc_slug
    return utc_slug("%Y-%m-%d", when)


def is_secret(rel: str) -> bool:
    """Путь ведёт к секрету или служебному — в задание не читаем."""
    parts = [p for p in Path(rel).parts if p not in ("", ".")]
    if any(p in NEVER_DIRS for p in parts[:-1]):
        return True
    return bool(parts) and bool(SECRET_NAME.match(parts[-1]))


def is_text_name(name: str) -> bool:
    low = name.lower()
    return Path(low).suffix in TEXT_EXT or low in TEXT_NAMES or Path(low).stem in TEXT_NAMES


def looks_text(data: bytes) -> bool:
    """Текст, а не двоичный файл: нет NUL в начале и читается как UTF-8."""
    head = data[:8192]
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as e:
        return e.start > len(head) - 4        # оборвалось на многобайтовой букве — это текст
    return True


def inside(cwd: str, rel: str) -> str:
    """Абсолютный путь внутри проекта или пусто: ссылка не выводит за корень."""
    root = os.path.realpath(cwd)
    full = os.path.realpath(os.path.join(root, rel.strip().lstrip("/\\")))
    return full if full == root or full.startswith(root + os.sep) else ""


def _read(full: str, cap: int) -> tuple:
    with open(full, "rb") as f:
        data = f.read(cap * 4 + 1)
    if not looks_text(data):
        return "", "двоичный файл — пропущен"
    text = data.decode("utf-8", "replace")
    return (text[:cap], f"обрезан до {cap} знаков") if len(text) > cap else (text, "")


def read_context(cwd: str, paths: list) -> tuple:
    """(блок для задания модели, пояснения) по ссылкам и вложениям человека."""
    parts, notes, used = [], [], 0
    for rel in dict.fromkeys(p for p in paths if p):
        full = inside(cwd, rel)
        if not full or not os.path.exists(full):
            notes.append(f"«{rel}» — нет в проекте, пропущен")
            continue
        if is_secret(os.path.relpath(full, os.path.realpath(cwd))):
            notes.append(f"«{rel}» — секреты и служебное в задание не идут")
            continue
        files = [full] if os.path.isfile(full) else []
        if os.path.isdir(full):
            for base, dirs, names in os.walk(full):
                dirs[:] = sorted(d for d in dirs if d not in NEVER_DIRS and not d.startswith("."))
                files += [os.path.join(base, n) for n in sorted(names) if is_text_name(n)]
            if len(files) > FOLDER_FILES:
                notes.append(f"«{rel}» — в папке {len(files)} текстовых файлов, взяты первые "
                             f"{FOLDER_FILES}")
                files = files[:FOLDER_FILES]
        for f in files:
            frel = os.path.relpath(f, os.path.realpath(cwd))
            if is_secret(frel):
                continue
            if used >= TOTAL_CAP:
                notes.append(f"«{frel}» — не вошёл: общий предел {TOTAL_CAP} знаков")
                continue
            text, why = _read(f, min(FILE_CAP, TOTAL_CAP - used))
            if why:
                notes.append(f"«{frel}» — {why}")
            if text:
                used += len(text)
                parts.append(f"### {frel}\n\n```\n{text.rstrip()}\n```")
    if not parts:
        return "", notes
    return ("Материалы, которые аналитик приложил к задаче или назвал в ней. Это такие же "
            "исходные данные, как сама задача: опирайся на них наравне с базой и называй "
            "файл, когда берёшь из него.\n\n" + "\n\n".join(parts) + "\n\n"), notes


def save_attachment(cwd: str, name: str, data: bytes) -> dict:
    """Сохранить вложение в `.opencode/context/<день>/`. → {path} или {error}."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = re.sub(r"[^\w.\- ]+", "_", base)[:120].strip(" .") or "вложение.txt"
    if not is_text_name(base):
        return {"error": f"«{base}» — вложения только текстовые (.md, .txt, .json, .py и т. п.)"}
    if SECRET_NAME.match(base):
        return {"error": f"«{base}» — похоже на файл с секретами: такое не вкладываем"}
    if len(data) > UPLOAD_CAP:
        return {"error": f"«{base}» больше {UPLOAD_CAP // 1_000_000} МБ — это уже не контекст"}
    if not looks_text(data):
        return {"error": f"«{base}» — не текст: вложения только текстовые"}
    folder = Path(cwd) / CONTEXT_DIR / utc_day()
    folder.mkdir(parents=True, exist_ok=True)
    target, n = folder / base, 1
    while target.exists():
        target = folder / f"{Path(base).stem}-{n}{Path(base).suffix}"
        n += 1
    target.write_bytes(data)
    trim_context(cwd)
    return {"path": str(target.relative_to(cwd)), "name": target.name, "bytes": len(data)}


def trim_context(cwd: str, keep_days: int = KEEP_DAYS) -> int:
    """Убрать папки вложений старше `keep_days` дней. → сколько убрано."""
    root = Path(cwd) / CONTEXT_DIR
    if not root.is_dir():
        return 0
    edge = utc_day(time.time() - keep_days * 86400)
    gone = 0
    for d in root.iterdir():
        if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}Z?", d.name) and d.name < edge:
            shutil.rmtree(d, ignore_errors=True)
            gone += 1
    return gone


def skill_dirs(cwd: str, kit: str = "") -> list:
    kit = kit or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [os.path.join(cwd, ".opencode", "skills"), os.path.join(kit, "skills"),
            os.path.join(os.path.expanduser("~"), ".claude", "skills")]


def find_skill(name: str, cwd: str, kit: str = "") -> tuple:
    """(путь к SKILL.md, тело без шапки) или ("", "") — навык по имени или псевдониму."""
    for want in (name, SKILL_ALIASES.get(name.lower(), "")):
        if not want or not re.fullmatch(r"[\w.\-]{1,64}", want):
            continue
        for base in skill_dirs(cwd, kit):
            path = os.path.join(base, want, "SKILL.md")
            if os.path.isfile(path):
                text = open(path, encoding="utf-8", errors="replace").read(24_000)
                body = text.split("---", 2)[-1].strip() if text.startswith("---") else text
                return path, body
    return "", ""


def skill_names(cwd: str, kit: str = "") -> list:
    """Все навыки, доступные запросу: для подсказок по `/`."""
    seen = {}
    for base in skill_dirs(cwd, kit):
        if os.path.isdir(base):
            for d in sorted(os.listdir(base)):
                if os.path.isfile(os.path.join(base, d, "SKILL.md")):
                    seen.setdefault(d, os.path.join(base, d, "SKILL.md"))
    for alias, to in SKILL_ALIASES.items():
        if to in seen:
            seen.setdefault(alias, seen[to])
    return sorted(seen)


def mentions(text: str, cwd: str, mcp_names=(), kit: str = "") -> dict:
    """Что человек упомянул в запросе. → {skills: [(имя, путь, тело)], mcp: [...], files: [...]}.

    Навык, упомянутый внутри навыка (`grill-me` → `/grilling`), подтягивается на один
    уровень: без этого личный навык-обёртка приносил бы одну строку «запусти другой».
    """
    text = text or ""
    skills, seen = [], set()

    def add_skills(src: str, depth: int) -> None:
        for m in MENTION_SKILL.finditer(src):
            name = m.group(1).rstrip(".")
            if name.lower() in seen:
                continue
            path, body = find_skill(name, cwd, kit)
            if path:
                seen.add(name.lower())
                skills.append((name, path, body))
                if depth < 1:
                    add_skills(body, depth + 1)

    add_skills(text, 0)
    names = {n.lower(): n for n in mcp_names}
    mcp, files = [], []
    for m in MENTION_AT.finditer(text):
        token = (m.group(1) or m.group(2) or "").rstrip(".:")
        if token.lower() in names:
            mcp.append(names[token.lower()])
            continue
        full = inside(cwd, token)
        if full and os.path.exists(full):
            files.append(os.path.relpath(full, os.path.realpath(cwd)))
    return {"skills": skills, "mcp": list(dict.fromkeys(mcp)), "files": list(dict.fromkeys(files))}


def skills_block(skills: list) -> str:
    if not skills:
        return ""
    return ("Аналитик позвал навыки — следуй их методу там, где он применим к этой задаче; "
            "то, что требует инструментов, которых у тебя нет, пропусти:\n\n"
            + "\n\n".join(f"### /{name}\n\n{body.strip()}" for name, _p, body in skills)
            + "\n\n")


def suggest(cwd: str, query: str, mcp_names=(), kit: str = "", limit: int = 20) -> list:
    """Подсказки для поля запроса: файлы и папки проекта, MCP-серверы, навыки.

    → [{kind: file|dir|mcp|skill, value, label}]. `/…` — навыки, `@…` — всё остальное.
    """
    q = (query or "").strip()
    out = []
    if q.startswith("/"):
        low = q[1:].lower()
        return [{"kind": "skill", "value": "/" + n, "label": n}
                for n in skill_names(cwd, kit) if low in n.lower()][:limit]
    low = q.lstrip("@").lower()
    for n in mcp_names:
        if low in n.lower():
            out.append({"kind": "mcp", "value": "@" + n, "label": n})
    root = os.path.realpath(cwd)
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in NEVER_DIRS and not d.startswith("."))
        rel_base = os.path.relpath(base, root)
        for d in dirs:
            rel = os.path.normpath(os.path.join(rel_base, d))
            if low in rel.lower():
                out.append({"kind": "dir", "value": rel, "label": rel + "/"})
        for n in sorted(names):
            rel = os.path.normpath(os.path.join(rel_base, n))
            if is_text_name(n) and not is_secret(rel) and low in rel.lower():
                out.append({"kind": "file", "value": rel, "label": rel})
        if len(out) >= limit * 3:
            break
    # Короткие пути и совпадения в имени — выше: человек обычно помнит имя, а не глубину.
    def rank(x):
        name = os.path.basename(x["value"]).lower()
        return (x["kind"] != "mcp", low not in name, x["value"].count(os.sep), len(x["value"]))
    return sorted(out, key=rank)[:limit]
