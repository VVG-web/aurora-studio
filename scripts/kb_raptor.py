#!/usr/bin/env python3
"""kb_raptor.py — дерево обзора базы из готовых тезисов и MOC.

Панель: `kb:raptor`

RAPTOR обычно строит иерархию summary-узлов вызовами модели. В Авроре уже есть
готовые summaries: `summary:` карточек, их тезисы и карты `MOC/`. Поэтому первый
слой дерева можно собрать без нового вызова модели: MOC становится узлом верхнего
уровня, связанные карточки — его детьми, а карточки вне MOC — узлами «Разное».

Дерево — производный индекс: оно не заменяет карточки, MOC или `ctx_pack`. Оно
отвечает на глобальные вопросы («какие блоки», «что входит в направление») и
служит входом для поиска, когда обычная выдача тонет в отдельных карточках.

  python3 .aurora/scripts/kb_raptor.py --build --apply   # записать meta/raptor.json
  python3 .aurora/scripts/kb_raptor.py --build            # показать, что изменится
  python3 .aurora/scripts/kb_raptor.py --query "какие блоки входят в ядро"
  python3 .aurora/scripts/kb_raptor.py --check            # свеж ли индекс

Зависимостей нет; модель не вызывается. Индекс хранит только имена, заголовки и
короткие тексты, а не копию базы.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aurora_common import KB_ROOT, body, frontmatter, replace_file, walk_md  # noqa: E402

ROOT = KB_ROOT
INDEX = os.path.join(ROOT, "meta", "raptor.json")
MOC_DIR = os.path.join(ROOT, "MOC")
LINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
WORD_RE = re.compile(r"[\w\-]+", re.UNICODE)
STOP = {"и", "в", "во", "не", "на", "с", "со", "как", "что", "по", "для", "из",
        "за", "то", "же", "или", "а", "но", "у", "о", "об", "от", "до", "при",
        "если", "это", "the", "a", "an", "of", "to", "in", "and", "or", "is",
        "какие", "какой", "какая", "каких", "кто", "где", "куда", "откуда",
        "сколько", "входит", "входят", "относится", "относятся", "есть",
        "лежат", "лежит", "описано", "описаны", "описан", "карточки", "про",
        "чем", "этом", "эти", "этот"}
SYNONYMS = {"эпик": "epic", "эпику": "epic", "эпика": "epic", "эпиков": "epic",
            "пустые": "пустышки", "пустой": "пустышки", "пустых": "пустышки",
            "брошены": "брошенные", "брошенных": "брошенные",
            "брошенной": "брошенные"}


def _hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=12).hexdigest()


def _words(text: str) -> list:
    out = []
    for raw in WORD_RE.findall(text or ""):
        w = raw.lower()
        if w in STOP:
            continue
        w = SYNONYMS.get(w, w)
        if len(w) > 5 and w.endswith(("ыми", "ами", "ях", "ах", "ов", "ев",
                                      "ые", "ий", "ый", "ая", "ое")):
            w = w[:-3] if w.endswith(("ыми", "ами")) else w[:-2]
        out.append(w)
    return out


def _read(path: str) -> str:
    try:
        return open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return ""


def _summary(text: str, limit: int = 1200) -> str:
    fm = frontmatter(text)
    title = (fm.get("title") or "").strip().strip('"')
    summary = (fm.get("summary") or "").strip().strip('"')
    first = " ".join(body(text).split())[:limit]
    return "\n".join(x for x in (title, summary, first) if x)


def _cards() -> dict:
    """Карточки знания без MOC и служебных файлов: {stem: текст}."""
    out = {}
    for path in walk_md(ROOT, skip_service=True, skip_archive=True):
        norm = path.replace("\\", "/")
        if "/MOC/" in norm or norm.endswith("/MOC"):
            continue
        text = _read(path)
        if not text:
            continue
        out[os.path.basename(path)[:-3]] = text
    return out


def _moc() -> list:
    """[(stem, текст)] — карты содержания."""
    out = []
    if not os.path.isdir(MOC_DIR):
        return out
    for name in sorted(os.listdir(MOC_DIR)):
        if not name.endswith(".md") or name.startswith(("_", ".")):
            continue
        path = os.path.join(MOC_DIR, name)
        text = _read(path)
        if text:
            out.append((name[:-3], text))
    return out


def scan() -> dict:
    """Собрать узлы и хеши. Модель не вызывается."""
    cards = _cards()
    nodes, child_of = [], {}
    linked = set()
    for stem, text in _moc():
        title = frontmatter(text).get("title", stem).strip().strip('"')
        children = [x for x in LINK_RE.findall(body(text)) if x in cards]
        children = list(dict.fromkeys(children))
        linked.update(children)
        nodes.append({"id": "moc:" + stem, "title": title, "level": 1,
                      "text": _summary(text), "cards": children, "children": []})
        for child in children:
            child_of.setdefault(child, "moc:" + stem)
    for stem, text in cards.items():
        if stem in linked:
            continue
        title = frontmatter(text).get("title", stem).strip().strip('"')
        nodes.append({"id": "card:" + stem, "title": title, "level": 0,
                      "text": _summary(text), "cards": [stem], "children": []})
        child_of.setdefault(stem, "card:" + stem)
    # У каждой карточки ровно один родитель; если MOC ссылается на карточку,
    # она уже вошла в children выше и не создаётся отдельным узлом.
    hashes = {stem: _hash(text) for stem, text in cards.items()}
    hashes.update({"MOC/" + stem: _hash(text) for stem, text in _moc()})
    return {"version": 1, "nodes": nodes, "cards": child_of, "hashes": hashes}


def load() -> dict | None:
    try:
        return json.loads(open(INDEX, encoding="utf-8").read())
    except (OSError, ValueError):
        return None


def check(fresh: dict | None = None) -> tuple[bool, str]:
    old = load()
    if old is None:
        return False, "индекс не собран"
    now = scan() if fresh is None else fresh
    return old.get("hashes") == now.get("hashes"), "хеши источников изменились"


def build(apply: bool = False) -> dict:
    now = scan()
    ok, why = check(now)
    if ok:
        print("дерево RAPTOR свежее: источники не менялись")
        return now
    if not apply:
        print("дерево RAPTOR устарело: " + why + "; записать: --build --apply")
        return now
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(now, f, ensure_ascii=False, indent=1)
    replace_file(tmp, INDEX)
    print(f"дерево RAPTOR: {len(now['nodes'])} узлов, "
          f"{len(now['cards'])} карточек → {INDEX}")
    return now


def rank(query: str, limit: int = 10) -> list:
    data = load()
    if not data:
        return []
    ask = _words(query)
    if not ask:
        return []
    rows = []
    for node in data.get("nodes") or []:
        text = " ".join((node.get("text") or "").split())
        words = _words(text)
        if not words:
            continue
        seen = {w: words.count(w) for w in set(ask)}
        hit = sum(seen.values())
        if not hit:
            continue
        title = [w.lower() for w in WORD_RE.findall(node.get("title") or "")]
        score = hit + 3 * sum(1 for w in ask if w in title)
        # Верхний узел важнее листовой при равном совпадении: обзор отвечает
        # на глобальный вопрос, а не тонет в одной карточке.
        score += 0.25 * int(node.get("level") or 0)
        rows.append((score, node))
    rows.sort(key=lambda x: (-x[0], x[1].get("title") or ""))
    return rows[:max(1, limit)]


def main() -> int:
    ap = argparse.ArgumentParser(description="RAPTOR-дерево из тезисов и MOC")
    ap.add_argument("--build", action="store_true", help="собрать/проверить индекс")
    ap.add_argument("--apply", action="store_true", help="записать meta/raptor.json")
    ap.add_argument("--check", action="store_true", help="только проверить свежесть")
    ap.add_argument("--query", metavar="ТЕКСТ", default="", help="найти узлы по вопросу")
    ap.add_argument("--limit", type=int, default=10, metavar="N")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    a = ap.parse_args()
    if a.check:
        ok, why = check()
        print(("свежо" if ok else "устарело: " + why))
        return 0 if ok else 1
    if a.build or not a.query:
        build(apply=a.apply)
    if a.query:
        rows = rank(a.query, a.limit)
        if a.json:
            print(json.dumps([{"title": n.get("title"), "id": n.get("id"),
                               "level": n.get("level"), "cards": n.get("cards") or [],
                               "score": round(s, 3)} for s, n in rows],
                             ensure_ascii=False))
        else:
            if not rows:
                print("Ничего не найдено.")
            for score, n in rows:
                level = "обзор" if int(n.get("level") or 0) else "карточка"
                print(f"{score:5.2f} · {level} · {n.get('title')} "
                      f"([{len(n.get('cards') or [])} карточек])")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
