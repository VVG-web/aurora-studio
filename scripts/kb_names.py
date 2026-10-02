#!/usr/bin/env python3
"""kb_names.py — привести имена проекта к правилам Windows, macOS и Linux сразу.

  python3 .opencode/scripts/kb_names.py            # что не так и что будет сделано
  python3 .opencode/scripts/kb_names.py --apply    # сделать

Панель: `kb:names`

Проект — git-репозиторий, и выгружают его на всех трёх системах: имя, допустимое на
одной, на другой ломает выгрузку всего репозитория (правило — `aurora_common.portable_name`,
AGENTS.md, п. 10). Новые имена движок с 1.140.0 делает правильными сам; эта команда чинит
то, что легло раньше. Каждой беде — свой способ:

  • зеркало Confluence: страницы перекладываются по тому же правилу, что даст следующий
    синк (`confluence_export.Exporter.place`), — без сети; база переводится на новые пути
    тем же `follow_moves`, что и при синке, старые копии убираются;
  • карточки: `kb_fix --portable-names --links` — переименование и правка ссылок на
    карточку (код документа с имени здесь не снимается — это другое правило);
  • пары путей в git, различимые только регистром: на macOS и Windows это один файл, и
    лишняя запись снимается из индекса git (`git rm --cached`), файл на диске не трогается;
  • остальное (`Workspaces/`, `Raw/`, `Deliverables/`) — файлы человека: печатается список,
    переименовывать их молча движок не вправе.

Раскладка зеркала обязана совпасть с той, что сделает синк, — иначе страницы переедут
второй раз. Поэтому код один: имя уровня, место под схемы, имена схем считает `Exporter`.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import case_clashes, path_problems  # noqa: E402

SCHEMES = "## Схемы страницы"
# Имя страницы бывает со скобками («Дашборд_(Карта)»), а имя схемы в ссылке закодировано
# (`quote`), и скобок в нём нет — по этому и отделяем одно от другого.
LINK_RE = re.compile(r"^- \[(?P<name>.+?)\]\((?P<base>.+?)_assets/(?P<q>[^/()\s]+)\)$", re.M)
HASH_RE = re.compile(r"^content_hash: \S+$", re.M)


# ------------------------------------------------------------------- зеркало

def mirror_dir(root: str) -> str:
    """Папка зеркала Confluence из настройки проекта. Пусто — зеркала нет."""
    import confluence_export as CE
    here = os.getcwd()
    try:
        os.chdir(root)
        out = CE.read_config().get("out") or CE.DEFAULT_OUT
    finally:
        os.chdir(here)
    path = os.path.join(root, out)
    return path if os.path.isfile(os.path.join(path, CE.STATE)) else ""


def _state_rows(mirror: str) -> list:
    """[(номер, заголовок, путь, статус)] из состояния синка — в его порядке."""
    import confluence_export as CE
    rows = []
    row_re = re.compile(r"^\|\s*\d+\s*\|\s*(\d{4,})\s*\|\s*(.*?)\s*\|\s*([^|]+?)\s*\|\s*(\w+)\s*\|")
    for line in open(os.path.join(mirror, CE.STATE), encoding="utf-8", errors="ignore"):
        m = row_re.match(line)
        if m:
            rows.append((m.group(1), m.group(2), m.group(3).strip(), m.group(4)))
    return rows


def _split(text: str) -> tuple:
    """Страница зеркала → (шапка с заголовком, тело). Тело — то, по чему синк считает хэш."""
    end = text.find("\n---\n\n", 4)
    if not text.startswith("---\n") or end < 0:
        return "", text
    head_end = end + len("\n---\n\n")
    nl = text.find("\n\n", head_end)
    if nl < 0 or not text.startswith("# ", head_end):
        return text[:head_end], text[head_end:]
    return text[:nl + 2], text[nl + 2:]


def mirror_plan(root: str, mirror: str) -> list:
    """[{pid, old, new, assets: {старое имя: новое}}] — что изменит раскладка по правилу.

    Дерево восстанавливается по путям: родитель страницы — страница с `index.md` в той
    папке, где она лежит. Порядок обхода — как у синка: предки раньше потомков, соседи по
    (заголовку, номеру) — от него зависит, кому из различимых только регистром достанется
    имя без номера.
    """
    import confluence_export as CE
    from sources_core import WikiMirror
    exp = CE.Exporter.__new__(CE.Exporter)
    WikiMirror.__init__(exp, mirror)
    where = os.path.relpath(os.path.abspath(mirror), os.path.abspath(root)).replace("\\", "/")
    exp.prefix_len = len(where) + 1
    exp.claimed = {}

    rows = _state_rows(mirror)
    by_rel = {rel: (pid, title) for pid, title, rel, _st in rows}
    page_of_dir = {rel[:-len("/index.md")]: rel for rel in by_rel if rel.endswith("/index.md")}
    # Дерево — по `breadcrumbs` в шапке (цепочка заголовков предков), а не по путям: страницу
    # из очень глубокой ветки синк кладёт под номером в папку ближайшего предка, и по пути
    # её родителем выглядел бы этот предок. Повторная починка переложила бы её не туда.
    # Заголовок страницы в Confluence уникален в пространстве, и цепочка их — адрес.
    crumbs, own = {}, {}
    for rel in by_rel:
        try:
            head = open(os.path.join(mirror, rel), encoding="utf-8").read(4000)
        except OSError:
            continue
        c = re.search(r'^breadcrumbs: "(.*)"$', head, re.M)
        t = re.search(r'^title: "(.*)"$', head, re.M)
        if c and t:
            crumbs[rel], own[rel] = c.group(1), t.group(1)
    rel_of_crumbs = {c: rel for rel, c in crumbs.items()}

    def parent_rel(rel: str) -> str:
        c, t = crumbs.get(rel), own.get(rel)
        if c is not None and t is not None:
            if c == t:
                return ""                              # корень обхода
            if c.endswith(" / " + t) and c[:-len(t) - 3] in rel_of_crumbs:
                return rel_of_crumbs[c[:-len(t) - 3]]
        d = rel[:-len("/index.md")] if rel.endswith("/index.md") else rel[:-3]
        return page_of_dir.get(d.rsplit("/", 1)[0], "") if "/" in d else ""

    kids: dict = {}
    for rel in by_rel:
        kids.setdefault(parent_rel(rel), []).append(rel)
    new_here: dict = {}
    plan = []

    def visit(rel: str, parent_here: str) -> None:
        pid, title = by_rel[rel]
        has_children = rel.endswith("/index.md")
        text = ""
        try:
            text = open(os.path.join(mirror, rel), encoding="utf-8").read()
        except OSError:
            pass
        has_assets = f"\n{SCHEMES}\n" in text
        here = exp.place(title, pid, parent_here, has_children, has_assets)
        new = f"{here}/index.md" if has_children else f"{here}.md"
        new_here[rel] = here
        assets = {}
        if has_assets:
            folder = os.path.splitext(new)[0] + "_assets"
            for m in LINK_RE.finditer(text):
                old_name = m.group("name")
                assets[old_name] = exp.asset_name(old_name, folder)
        if new != rel or any(a != b for a, b in assets.items()):
            plan.append({"pid": pid, "title": title, "old": rel, "new": new, "assets": assets})
        for child in sorted(kids.get(rel, []), key=lambda r: (by_rel[r][1], by_rel[r][0])):
            visit(child, here)

    for top in sorted(kids.get("", []), key=lambda r: (by_rel[r][1], by_rel[r][0])):
        visit(top, "")
    return plan


def orphan_assets(mirror: str) -> list:
    """Файлы `<страница>_assets/`, чьей страницы нет в состоянии синка. → [путь в зеркале]."""
    from sources_core import ASSET_DIR_RE, WikiMirror
    state = WikiMirror.__new__(WikiMirror)
    WikiMirror.__init__(state, mirror)
    known = [rel for _pid, _title, rel, _st in _state_rows(mirror)]
    if not known:
        return []          # состояния нет — судить, чья схема, не по чему
    return [r for r in state.extra_files(known) if ASSET_DIR_RE.search(r)]


def same_knowledge(a: str, b: str) -> bool:
    """Тот же текст страницы с точностью до имён файлов схем и хэша содержания.

    Починка имён переименовывает схемы и правит ссылки на них — знание страницы при этом
    не меняется. Без этой сверки разбор такой страницы считался устаревшим, и маршрут
    разбирал её моделью заново (PRJ-A 28.09.2026: 118 страниц, повторный разбор легаси-
    справочников задвоил в них текст).
    """
    drop = lambda t: "\n".join(l for l in t.splitlines()
                               if not l.startswith("content_hash:") and not LINK_RE.match(l))
    return drop(a) == drop(b)


def apply_mirror(root: str, mirror: str, plan: list) -> dict:
    """Переложить страницы по плану и перевести на них базу. → итог `follow_moves`."""
    import confluence_export as CE
    from build_plan import file_hash, load_manifest, save_manifest
    from kb_remap import follow_moves
    from sources_core import WikiMirror, drop_empty_dirs, mirror_prefix
    moved = {p["old"]: p for p in plan}
    prefix = mirror_prefix(mirror)
    man = load_manifest()
    fresh = {}                       # новый путь → разбор был актуален для прежнего текста
    for item in plan:
        rec = (man.get("sources") or {}).get(f"{prefix}/{item['old']}") or {}
        src = os.path.join(mirror, item["old"])
        if rec.get("hash") and os.path.isfile(src) and rec["hash"] == file_hash(src):
            fresh[f"{prefix}/{item['new']}"] = open(src, encoding="utf-8").read()
    for item in plan:
        src = os.path.join(mirror, item["old"])
        dst = os.path.join(mirror, item["new"])
        try:
            text = open(src, encoding="utf-8").read()
        except OSError:
            continue
        old_base = os.path.splitext(item["old"])[0] + "_assets"
        new_base = os.path.splitext(item["new"])[0] + "_assets"
        stem = os.path.basename(os.path.splitext(item["new"])[0])
        if item["assets"]:
            os.makedirs(os.path.join(mirror, new_base), exist_ok=True)
            for old_name, new_name in item["assets"].items():
                a = os.path.join(mirror, old_base, old_name)
                b = os.path.join(mirror, new_base, new_name)
                if not os.path.isfile(a) or os.path.abspath(a) == os.path.abspath(b):
                    continue
                if item["old"] == item["new"]:
                    os.replace(a, b)            # страница на месте — схему переименовываем
                else:
                    shutil.copy2(a, b)          # старую папку уберёт follow_moves вместе со страницей

            def relink(m, item=item, stem=stem):
                name = item["assets"].get(m.group("name"), m.group("name"))
                return f"- [{name}]({stem}_assets/{urllib.parse.quote(name)})"
            head, body = _split(text)
            body = LINK_RE.sub(relink, body)
            # хэш — тем же счётом, что у синка: тело без последнего перевода строки
            digest = hashlib.md5(body[:-1].encode("utf-8") if body.endswith("\n")
                                 else body.encode("utf-8")).hexdigest()[:16]
            head = HASH_RE.sub(f"content_hash: {digest}", head, count=1)
            text = head + body
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text)
    # Состояние синка — под новые пути, тем же кодом, что пишет синк.
    state = CE.Exporter.__new__(CE.Exporter)
    WikiMirror.__init__(state, mirror)
    state.records = [(pid, moved[rel]["new"] if rel in moved else rel, title, status)
                     for pid, title, rel, status in _state_rows(mirror)]
    state.write_state()
    # Старые копии — переезды: база переводится на новые пути, копии уходят.
    st = follow_moves(mirror, apply=True, kb=os.path.join(root, "AuroraKnowledgeDB"))
    # Разбор остаётся действительным, если знание страницы то же: меняли только схемы.
    man = load_manifest()
    kept = 0
    for new, was_text in fresh.items():
        rec = (man.get("sources") or {}).get(new)
        if not rec or not os.path.isfile(new):
            continue
        now_text = open(new, encoding="utf-8").read()
        if rec.get("hash") != file_hash(new) and same_knowledge(was_text, now_text):
            rec["hash"] = file_hash(new)
            kept += 1
    if kept:
        save_manifest(man)
    st["kept_parsed"] = kept
    drop_empty_dirs(mirror)
    return st


# --------------------------------------------------------------------- git

def tracked(root: str) -> list:
    out = subprocess.run(["git", "-C", root, "-c", "core.quotepath=false", "ls-files", "-z"],
                         capture_output=True, text=True).stdout
    return [r for r in out.split("\0") if r]


def on_disk_exactly(root: str, rel: str) -> bool:
    """Лежит ли путь на диске ровно с таким регистром и формой букв каждого уровня."""
    import unicodedata
    cur = root
    for part in rel.split("/"):
        try:
            names = os.listdir(cur)
        except OSError:
            return False
        want = unicodedata.normalize("NFC", part)
        if not any(unicodedata.normalize("NFC", n) == want for n in names):
            return False
        cur = os.path.join(cur, part)
    return True


def case_plan(root: str) -> list:
    """Записи индекса git, у которых на диске другой регистр: это следы переименования."""
    drop = []
    for a, b in case_clashes(tracked(root)):
        stale = [r for r in (a, b) if not on_disk_exactly(root, r)]
        if len(stale) == 1:
            drop.append(stale[0])
    return drop


# -------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Имена проекта — по правилам Windows, macOS и Linux")
    ap.add_argument("--apply", action="store_true", help="сделать (иначе только показать)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="писать и при незакоммиченных правках в базе и зеркале")
    ap.add_argument("--root", default=".", help="корень проекта")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    # Учёт разбора и настройка читаются от корня проекта — работаем из него.
    os.chdir(root)
    print("# Имена файлов по правилам Windows, macOS и Linux\n")
    changed = False
    # Чистота дерева — один раз, до всего: починка — одна операция, и откатить её надо
    # целиком. Проверять перед каждым шагом нельзя: перекладка зеркала сама переводит
    # карточки на новые пути, и шаг карточек упирался бы в свою же работу.
    if a.apply:
        from aurora_common import git_guard
        for part in ("AuroraKnowledgeDB", "Sources"):
            if os.path.isdir(part) and not git_guard(part, a.allow_dirty, "починка имён"):
                return 2

    mirror = mirror_dir(root)
    if mirror:
        plan = mirror_plan(root, mirror)
        moves = [p for p in plan if p["old"] != p["new"]]
        renamed_assets = sum(1 for p in plan for x, y in p["assets"].items() if x != y)
        print(f"## Зеркало Confluence\n\nстраниц переложить: {len(moves)} · "
              f"схем переименовать: {renamed_assets}")
        for p in moves[:5]:
            print(f"  - {p['old']}\n    → {p['new']}")
        if len(moves) > 5:
            print(f"  … ещё {len(moves) - 5}")
        if a.apply and plan:
            st = apply_mirror(root, mirror, plan)
            print(f"сделано: переложено {st.get('moves', 0)}, карточек переведено на новые "
                  f"пути {st.get('cards', 0)}")
            changed = True
        # Схемы страниц, которых в зеркале уже нет: папка `<страница>_assets/` пережила
        # саму страницу (прежняя раскладка, переезд). Правило то же, что у чистки синка
        # (`extra_files`), но без сети — по состоянию синка. Путь такого следа doctor и
        # зовёт чинить этой командой (PRJ-B 29.09.2026: 205 знаков).
        orphans = orphan_assets(mirror)
        print(f"схем без страницы: {len(orphans)}")
        for rel in orphans[:5]:
            print(f"  - {rel}")
        if a.apply and orphans:
            from sources_core import drop_empty_dirs
            for rel in orphans:
                try:
                    os.remove(os.path.join(mirror, rel))
                except OSError as e:
                    print(f"  ! не удалить {rel}: {e}", file=sys.stderr)
            drop_empty_dirs(mirror)
            changed = True
        print()

    fix = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb_fix.py"),
           "--portable-names", "--links"] + (["--apply", "--allow-dirty"] if a.apply else [])
    cp = subprocess.run(fix, cwd=root, capture_output=True, text=True)
    lines = [l for l in cp.stdout.splitlines()
             if "имён приведено" in l or l.startswith("    ") or "переименований" in l]
    if a.apply and re.search(r"переименований: [1-9]", cp.stdout):
        changed = True
    print("## Карточки (kb_fix --portable-names --links)\n")
    print("\n".join(lines[:12]) or "  нечего")
    if cp.returncode not in (0, 1):
        print((cp.stderr or cp.stdout)[-600:])
    print()

    drop = case_plan(root)
    print(f"## Пары путей, различимые только регистром\n\nснять из индекса git: {len(drop)}")
    for rel in drop[:6]:
        print(f"  - {rel}")
    if a.apply and drop:
        subprocess.run(["git", "-C", root, "rm", "--cached", "-q", "--", *drop], check=False)
        changed = True
    print()

    rest = [(r, w[0]) for r in tracked(root)
            if not r.startswith(("Sources/Confluence/", "AuroraKnowledgeDB/"))
            for w in [path_problems(r)] if w]
    if rest:
        print(f"## Файлы человека — переименовать руками: {len(rest)}\n")
        for r, w in rest[:10]:
            print(f"  - {r} — {w}")
        print()
    if a.apply:
        # Граф базы — по новым путям: иначе сервер графа (MCP) и страница графа отвечали бы
        # по старым до следующего маршрута. Выгрузка дешёвая и ничего не переписывает зря.
        g = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                         "kb_graph.py"), "--export"],
                           cwd=root, capture_output=True, text=True)
        print("## Граф базы пересобран по новым путям\n" if g.returncode == 0 else
              f"## Граф базы не пересобран: {(g.stderr or g.stdout)[-300:]}\n")
    if not a.apply:
        print("(dry-run) Ничего не изменено. Сделать: --apply")
    elif changed:
        print("Готово. Проверьте `git status` и закоммитьте; доктор: aurora_doctor.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
