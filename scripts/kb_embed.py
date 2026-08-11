#!/usr/bin/env python3
"""kb_embed.py — семантический индекс базы знаний (фреймворк «Аврора»).

Поиск по словам находит то, что человек назвал. Он не найдёт «как вернуть деньги за
несостоявшуюся поставку», если в карточке написано «возврат обеспечительного платежа при
аннулировании»: общих слов нет, а тема одна. Это не недостаток словесного поиска — это
его граница, и закрывается она эмбеддингами.

  python3 .opencode/scripts/kb_embed.py --status     # что в индексе, что устарело
  python3 .opencode/scripts/kb_embed.py --apply      # досчитать недостающее
  python3 .opencode/scripts/kb_embed.py --apply --all # пересчитать всё заново

Зависимостей не добавляет: вектора считает та же модель, к которой уже ходит агент
(`AURORA_AGENT_EMBED_MODEL`, по умолчанию `bge-m3`), скалярное произведение — stdlib.
Вектора хранятся нормированными, поэтому близость — это одно умножение на пару чисел.

Индекс лежит в `AuroraKnowledgeDB/meta/embeddings.bin` и **не идёт в git**: это
производная от карточек, она пересобирается за минуты и меняется целиком при смене
модели. Потеряли — соберите заново.

Что уходит наружу: заголовок, синонимы и начало тела каждой карточки — на тот самый
шлюз, куда агент и так носит их целиком. Если контур это запрещает, не включайте
семантику: словесный поиск работает без неё.

Панель: `kb:embed`
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_core as AG  # noqa: E402
from aurora_common import KB_ROOT, body, frontmatter  # noqa: E402

META = os.path.join(KB_ROOT, "meta")
VECTORS = os.path.join(META, "embeddings.bin")
INDEX = os.path.join(META, "embeddings.json")
TODAY = date.today().isoformat()
BATCH = 32                 # столько текстов за один запрос: шлюз отвечает ~секунду
PIECE = 1500               # символов карточки в вектор: дальше идёт хвост, а не суть


def card_texts(root: str = KB_ROOT) -> dict:
    """{имя карточки: текст для вектора}. Заголовок и синонимы весомее хвоста тела."""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
        if "/meta" in dirpath.replace("\\", "/"):
            continue
        for f in sorted(files):
            if not f.endswith(".md") or f.startswith("_") or f == "index.md":
                continue
            path = os.path.join(dirpath, f).replace("\\", "/")
            text = open(path, encoding="utf-8", errors="ignore").read()
            fm = frontmatter(text)
            head = " · ".join(x for x in (
                (fm.get("title") or f[:-3]).strip('"'),
                (fm.get("summary") or "").strip('"'),
                (fm.get("aliases") or "").strip("[]"),
            ) if x)
            out[f[:-3]] = (head + "\n" + body(text))[:PIECE]
    return out


def digest(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


def load_index() -> dict:
    """{имя: {hash, row}} плюс размерность и модель. Битый индекс — просто пустой."""
    try:
        with open(INDEX, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "cards" in data:
            return data
    except (OSError, ValueError):
        pass
    return {"model": "", "dim": 0, "built": "", "cards": {}}


def load_vectors(dim: int, rows: int) -> array.array:
    v = array.array("f")
    try:
        with open(VECTORS, "rb") as f:
            v.fromfile(f, dim * rows)
    except (OSError, EOFError):
        return array.array("f")
    return v


def normalize(vec: list) -> list:
    s = sum(x * x for x in vec) ** 0.5
    return [x / s for x in vec] if s else vec


def endpoints(cfg: dict) -> list:
    """Куда ходить за векторами: свой сервис эмбеддингов либо кольцо бэкендов агента.

    Отдельный адрес нужен там, где эмбеддинги подняты своим сервисом — он и модель знает
    одну, и ключа может не требовать. Не задан — берём то же кольцо, что и чат: в
    инфраструктуре с одним шлюзом настраивать нечего.
    """
    own = (cfg.get("embed") or {}).get("url")
    if own:
        return [{"url": own, "key": (cfg["embed"] or {}).get("key", ""), "n": 0}]
    return cfg["backends"]


def embed(texts: list, cfg: dict, model: str) -> list:
    """Вектора для списка текстов. Идём по кольцу бэкендов, как остальной агент."""
    out = []
    for i in range(0, len(texts), BATCH):
        chunk = texts[i:i + BATCH]
        got = None
        for backend in endpoints(cfg):
            st, data, err, _dt = AG.http_json(backend["url"] + "/embeddings",
                                              {"model": model, "input": chunk},
                                              backend["key"], cfg["request_timeout"])
            if st == 200 and (data.get("data") or []):
                got = [normalize(d["embedding"]) for d in
                       sorted(data["data"], key=lambda d: d.get("index", 0))]
                break
            print(f"  бэкенд {backend['url']}: {err or 'пустой ответ'}", file=sys.stderr)
        if got is None or len(got) != len(chunk):
            return []
        out += got
        print(f"  посчитано {len(out)} из {len(texts)}", flush=True)
    return out


def search(query: str, cfg: dict, model: str, limit: int = 40) -> list:
    """[(имя карточки, близость)] по убыванию. Пустой список — индекса нет или он чужой."""
    idx = load_index()
    if not idx["cards"] or idx.get("model") != model:
        return []
    dim, cards = idx["dim"], idx["cards"]
    vectors = load_vectors(dim, len(cards))
    if len(vectors) < dim * len(cards):
        return []
    q = embed([query], cfg, model)
    if not q:
        return []
    qv = q[0]
    scored = []
    for name, rec in cards.items():
        off = rec["row"] * dim
        scored.append((sum(qv[k] * vectors[off + k] for k in range(dim)), name))
    scored.sort(reverse=True)
    return [(n, round(s, 4)) for s, n in scored[:limit]]


def main() -> int:
    ap = argparse.ArgumentParser(description="Семантический индекс базы знаний")
    ap.add_argument("--status", action="store_true", help="что в индексе и что устарело")
    ap.add_argument("--apply", action="store_true", help="досчитать недостающие вектора")
    ap.add_argument("--all", action="store_true", help="пересчитать всё заново")
    ap.add_argument("--query", metavar="ТЕКСТ", help="проверить поиск по индексу")
    a = ap.parse_args()

    if not os.path.isdir(KB_ROOT):
        print(f"kb_embed: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    cfg = AG.parse_config(AG.raw_config())
    model = cfg["embed"]["model"]
    texts = card_texts()
    idx = load_index()
    stale = [n for n, t in texts.items()
             if a.all or idx["cards"].get(n, {}).get("hash") != digest(t)]
    gone = [n for n in idx["cards"] if n not in texts]

    print(f"# Семантический индекс — {TODAY}\n")
    where = cfg["embed"]["url"] or (cfg["backends"][0]["url"] if cfg["backends"] else "—")
    print(f"Карточек в базе: {len(texts)} · в индексе: {len(idx['cards'])} "
          f"· модель: {idx.get('model') or '—'}")
    print(f"Считает: {model} на {where}"
          + ("" if cfg["embed"]["url"] else " (кольцо агента — своего адреса не задано)"))
    print(f"Пересчитать: {len(stale)} · выбыло: {len(gone)}")

    if a.query:
        hits = search(a.query, cfg, model)
        if not hits:
            print("\nИндекса нет или он собран другой моделью — соберите: `kb:embed --apply`")
            return 1
        print(f"\n## Ближайшие по смыслу — «{a.query}»\n")
        for name, score in hits[:15]:
            print(f"- {score:.3f} · {name}")
        return 0

    if not a.apply:
        print("\n(dry-run) Ничего не записано. Досчитать: `kb:embed --apply`")
        return 0
    if not endpoints(cfg):
        print("kb_embed: некуда идти за векторами. Задайте AURORA_EMBED_URL или настройте "
              "бэкенды агента: панель «Настройка» → «Агент»", file=sys.stderr)
        return 1
    if not stale and not gone:
        print("\n✅ Индекс актуален.")
        return 0

    # Пересборка целиком: индекс — производная, и держать его частями сложнее, чем
    # пересчитать. Считаем только устаревшие, но пишем файл заново, чтобы номера строк
    # не разъезжались с содержимым.
    keep = {n: r for n, r in idx["cards"].items() if n in texts and n not in stale}
    old_dim = idx.get("dim") or 0
    old = load_vectors(old_dim, len(idx["cards"])) if keep else array.array("f")
    fresh = embed([texts[n] for n in stale], cfg, model) if stale else []
    if stale and not fresh:
        print("kb_embed: вектора не получены — индекс не тронут", file=sys.stderr)
        return 2

    dim = len(fresh[0]) if fresh else old_dim
    out, cards = array.array("f"), {}
    for name in sorted(texts):
        if name in keep and old_dim == dim:
            off = keep[name]["row"] * dim
            vec = old[off:off + dim]
        else:
            vec = array.array("f", fresh[stale.index(name)])
        cards[name] = {"hash": digest(texts[name]), "row": len(out) // dim}
        out.extend(vec)

    os.makedirs(META, exist_ok=True)
    with open(VECTORS, "wb") as f:
        out.tofile(f)
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump({"model": model, "dim": dim, "built": TODAY, "cards": cards},
                  f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"\n✅ Индекс собран: карточек {len(cards)}, размерность {dim}, модель {model}.")
    print(f"   Файлы: {VECTORS} и {INDEX} (в git не идут — это производная).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
