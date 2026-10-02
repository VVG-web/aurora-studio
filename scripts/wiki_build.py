#!/usr/bin/env python3
"""wiki_build.py — wiki проекта: проверка страниц и сборка для GitHub Wiki (фреймворк «Аврора»).

База знаний об Авроре живёт в папке `wiki/` репозитория: английские страницы в корне, русские —
в `wiki/ru/`, набор один и тот же. Читать её можно прямо в просмотре репозитория на GitHub —
ссылки между страницами и на документы в `docs/` относительные, mermaid рисуется. Тем же
страницам можно дать вторую жизнь во вкладке Wiki: GitHub Wiki — отдельный репозиторий с плоской
раскладкой, и этот скрипт собирает для него каталог (`--out`), а workflow `wiki-sync`
выкладывает.

  python3 scripts/wiki_build.py --check                       # ссылки, пары, достижимость
  python3 scripts/wiki_build.py --out DIR --repo ВЛАДЕЛЕЦ/ИМЯ  # плоский каталог для GitHub Wiki

Что проверяет `--check` (код 1, если нашлось хоть что-то):
  * каждая ссылка ведёт на существующий файл или папку, а `#якорь` — на настоящий заголовок;
  * у каждой английской страницы есть русская пара и наоборот;
  * каждая страница достижима от главной (`README.md`) по ссылкам, а боковая панель
    (`_Sidebar.md`) называет их все;
  * блоки кода закрыты (незакрытый ``` съедает остаток страницы).

Что делает сборка: `README.md` → `Home`, `ru/X` → `RU-X`; ссылки на страницы становятся ссылками
на имена страниц Wiki, ссылки на файлы репозитория — абсолютными (`/blob/` или `/tree/`);
боковая панель — английская и русская вместе; добавляется подвал «страницы генерируются из
репозитория, правьте там». Внутри `--out` затираются только `*.md`; `.git` не трогается.

Скрипт движком не распространяется: он нужен ките и его CI, а не проекту аналитика.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import deque
from pathlib import Path

for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на «—» (UnicodeEncodeError);
    # движок говорит по-русски и пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

KIT = Path(__file__).resolve().parent.parent
WIKI = KIT / "wiki"
HOME = "README"
SIDEBAR = "_Sidebar"
RU = "ru/"

_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_LINK = re.compile(r"(!?\[(?:[^\[\]]|\[[^\]]*\])*\]\()([^()\s]+)((?:\s+\"[^\"]*\")?\))")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.I)


# ── разбор Markdown ──────────────────────────────────────────────────────────────

def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def prose_lines(text: str):
    """(номер строки, строка) вне блоков кода: в них нет ни ссылок, ни заголовков."""
    fence = None
    for n, line in enumerate(text.split("\n"), 1):
        m = _FENCE.match(line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        if fence is None:
            yield n, line


def unclosed_fence(text: str) -> bool:
    fence = None
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
    return fence is not None


def links(text: str) -> list[tuple[int, str]]:
    """Адреса всех ссылок и картинок страницы, кроме тех, что внутри кода."""
    out = []
    for n, line in prose_lines(text):
        bare = _CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)
        out += [(n, m.group(2)) for m in _LINK.finditer(bare)]
    return out


def slug(title: str) -> str:
    """Якорь заголовка так, как его строит GitHub: без разметки, строчные, пробелы в дефисы."""
    title = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", title)
    title = re.sub(r"<[^>]+>", "", title).replace("`", "").replace("*", "")
    return re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")


def anchors(text: str) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    for _, line in prose_lines(text):
        m = _HEADING.match(line)
        if not m:
            continue
        base = slug(m.group(2))
        k = seen.get(base, 0)
        seen[base] = k + 1
        out.add(base if k == 0 else f"{base}-{k}")
    return out


# ── страницы ─────────────────────────────────────────────────────────────────────

def page_ids() -> dict[str, Path]:
    """id → файл: `The-Card`, `ru/The-Card`, `README`, `ru/README`, `_Sidebar`, `ru/_Sidebar`."""
    out = {}
    for p in sorted(WIKI.rglob("*.md")):
        out[p.relative_to(WIKI).with_suffix("").as_posix()] = p
    return out


def flat_name(pid: str) -> str:
    """Имя страницы в плоской GitHub Wiki."""
    if pid == HOME:
        return "Home"
    if pid == RU + HOME:
        return "RU-Home"
    return "RU-" + pid[len(RU):] if pid.startswith(RU) else pid


def classify(page: Path, target: str):
    """Куда ведёт ссылка: ('page', id, якорь) · ('repo', путь, якорь) · ('anchor'|'external'|'missing', …)."""
    if _EXTERNAL.match(target):
        return "external", target, ""
    path, _, frag = target.partition("#")
    if not path:
        return "anchor", "", frag
    abspath = Path(os.path.normpath(page.parent / path))
    try:
        rel = abspath.relative_to(KIT)
    except ValueError:
        return "missing", target, frag
    if not abspath.exists():
        return "missing", target, frag
    try:
        inside = abspath.relative_to(WIKI)
    except ValueError:
        return "repo", rel.as_posix(), frag
    if abspath.is_file() and abspath.suffix == ".md":
        return "page", inside.with_suffix("").as_posix(), frag
    return "repo", rel.as_posix(), frag


# ── проверка ─────────────────────────────────────────────────────────────────────

def check() -> list[str]:
    ids = page_ids()
    problems: list[str] = []
    if HOME not in ids or RU + HOME not in ids:
        return ["нет главных страниц: wiki/README.md и wiki/ru/README.md"]

    en = {i for i in ids if not i.startswith(RU) and i != SIDEBAR}
    ru = {i[len(RU):] for i in ids if i.startswith(RU) and i != RU + SIDEBAR}
    for i in sorted(en - ru):
        problems.append(f"wiki/{i}.md: нет русской пары wiki/ru/{i}.md")
    for i in sorted(ru - en):
        problems.append(f"wiki/ru/{i}.md: нет английской пары wiki/{i}.md")
    for need in (SIDEBAR, RU + SIDEBAR):
        if need not in ids:
            problems.append(f"нет боковой панели wiki/{need}.md")

    texts = {i: read(p) for i, p in ids.items()}
    graph: dict[str, set[str]] = {i: set() for i in ids}
    anchor_cache: dict[str, set[str]] = {}

    def anchors_of(path: Path) -> set[str]:
        key = str(path)
        if key not in anchor_cache:
            anchor_cache[key] = anchors(read(path))
        return anchor_cache[key]

    for pid, path in ids.items():
        text = texts[pid]
        shown = path.relative_to(KIT).as_posix()
        if not text.lstrip().startswith("# ") and pid not in (SIDEBAR, RU + SIDEBAR):
            problems.append(f"{shown}: страница не начинается с заголовка первого уровня")
        if unclosed_fence(text):
            problems.append(f"{shown}: блок кода не закрыт — остаток страницы съест разметка")
        for n, target in links(text):
            kind, dest, frag = classify(path, target)
            where = f"{shown}:{n}"
            if kind == "missing":
                problems.append(f"{where}: ссылка «{target}» никуда не ведёт")
            elif kind == "anchor":
                if frag and frag not in anchors_of(path):
                    problems.append(f"{where}: в этой странице нет заголовка «#{frag}»")
            elif kind == "page":
                graph[pid].add(dest)
                if frag and frag not in anchors_of(ids[dest]):
                    problems.append(f"{where}: в {dest}.md нет заголовка «#{frag}»")
            elif kind == "repo" and frag:
                target_path = KIT / dest
                if target_path.is_file() and target_path.suffix == ".md" \
                        and frag not in anchors_of(target_path):
                    problems.append(f"{where}: в {dest} нет заголовка «#{frag}»")

    for root, side, own in ((HOME, SIDEBAR, en), (RU + HOME, RU + SIDEBAR, {RU + i for i in ru})):
        if side not in ids:
            continue
        seen = {root}
        queue = deque([root])
        while queue:
            for nxt in graph[queue.popleft()]:
                if nxt in ids and nxt not in seen and not nxt.endswith(SIDEBAR):
                    seen.add(nxt)
                    queue.append(nxt)
        for i in sorted(own - seen):
            problems.append(f"wiki/{i}.md: от главной страницы не достать по ссылкам")
        for i in sorted((own - {i for i in graph[side]}) - {side}):
            problems.append(f"wiki/{side}.md: боковая панель не называет страницу «{i}»")
    return problems


# ── сборка ───────────────────────────────────────────────────────────────────────

def _hide_code(line: str) -> tuple:
    """(строка с метками вместо код-спанов, сами спаны)."""
    spans: list[str] = []

    def hide(m: re.Match) -> str:
        spans.append(m.group(0))
        return f"\x00{len(spans) - 1}\x00"
    return _CODE_SPAN.sub(hide, line), spans


def _show_code(text: str, spans: list) -> str:
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)


def rewrite(text: str, page: Path, repo: str, branch: str) -> str:
    """Ссылки на страницы → имена страниц Wiki; ссылки на файлы репозитория → абсолютные."""
    def one(m: re.Match) -> str:
        head, target, tail = m.group(1), m.group(2), m.group(3)
        kind, dest, frag = classify(page, target)
        suffix = f"#{frag}" if frag else ""
        if kind == "page":
            return f"{head}{flat_name(dest)}{suffix}{tail}"
        if kind == "repo":
            mode = "tree" if (KIT / dest).is_dir() else "blob"
            return f"{head}https://github.com/{repo}/{mode}/{branch}/{dest}{suffix}{tail}"
        return m.group(0)

    out = []
    fence = None
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            out.append(line)
            continue
        if fence is not None:
            out.append(line)
            continue
        # код-спаны прячем под метки: ссылка может содержать код в подписи — `[`docs/`](…)`
        hidden, spans = _hide_code(line)
        out.append(_show_code(_LINK.sub(one, hidden), spans))
    return "\n".join(out)


FOOTER = {
    "en": "_This wiki is generated from the [`wiki/`](https://github.com/{repo}/tree/{branch}/wiki) folder of the "
          "repository — edit the pages there, changes made here are overwritten._ · [RU-Home](RU-Home)",
    "ru": "_Эта wiki собирается из папки [`wiki/`](https://github.com/{repo}/tree/{branch}/wiki) репозитория — "
          "правьте страницы там, изменения, сделанные здесь, будут перезаписаны._ · [Home](Home)",
}


def _write(path: Path, text: str) -> None:
    """Страница с концами строк `\\n` на любой системе: `Path.write_text(newline=…)` — только с Python 3.10."""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def build(out: Path, repo: str, branch: str) -> list[str]:
    ids = page_ids()
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.md"):
        old.unlink()
    written = []
    for pid, path in ids.items():
        if pid in (SIDEBAR, RU + SIDEBAR):
            continue
        body = rewrite(read(path), path, repo, branch).rstrip("\n")
        lang = "ru" if pid.startswith(RU) else "en"
        body += "\n\n---\n" + FOOTER[lang].format(repo=repo, branch=branch) + "\n"
        name = flat_name(pid) + ".md"
        _write(out / name, body)
        written.append(name)
    en_side = rewrite(read(ids[SIDEBAR]), ids[SIDEBAR], repo, branch).rstrip("\n")
    ru_side = rewrite(read(ids[RU + SIDEBAR]), ids[RU + SIDEBAR], repo, branch).rstrip("\n")
    _write(out / "_Sidebar.md", f"{en_side}\n\n---\n\n{ru_side}\n")
    written.append("_Sidebar.md")
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description="wiki проекта: проверка и сборка для GitHub Wiki")
    ap.add_argument("--check", action="store_true", help="проверить ссылки, пары, достижимость страниц")
    ap.add_argument("--out", metavar="DIR", help="собрать плоский каталог для GitHub Wiki в DIR")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                    help="ВЛАДЕЛЕЦ/ИМЯ репозитория для абсолютных ссылок (по умолчанию GITHUB_REPOSITORY)")
    ap.add_argument("--branch", default="master", help="ветка, на которую ведут ссылки на файлы (master)")
    args = ap.parse_args()
    if not args.check and not args.out:
        ap.error("нужен --check или --out")
    rc = 0
    if args.check or args.out:
        problems = check()
        if problems:
            print(f"wiki: найдено {len(problems)}:")
            for p in problems:
                print(f"  ✗ {p}")
            rc = 1
        else:
            n = len([i for i in page_ids() if not i.endswith(SIDEBAR)])
            print(f"wiki: {n} страниц, ссылки, пары и достижимость в порядке")
    if args.out:
        if rc:
            print("Сборка не начата: сначала поправьте найденное.")
            return rc
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
            ap.error("нужен --repo ВЛАДЕЛЕЦ/ИМЯ (или переменная GITHUB_REPOSITORY)")
        files = build(Path(args.out), args.repo, args.branch)
        print(f"Собрано {len(files)} файлов в {args.out}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
