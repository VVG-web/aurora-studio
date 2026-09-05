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
Поиск не перебирает базу целиком: в тот же файл индекса упакованы оси главных
компонент и проекции карточек на них, точное скалярное — только по кандидатам
(см. `search`). Старый индекс без проекций — обычный полный перебор.

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
import random
import struct
import sys
from datetime import date
from itertools import repeat
from operator import add, mul, sub
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_core as AG  # noqa: E402
from aurora_common import KB_ROOT, body, frontmatter, is_placeholder  # noqa: E402

META = os.path.join(KB_ROOT, "meta")
VECTORS = os.path.join(META, "embeddings.bin")
INDEX = os.path.join(META, "embeddings.json")
TODAY = date.today().isoformat()
BATCH = 32                 # столько текстов за один запрос: шлюз отвечает ~секунду
PIECE = 1500               # символов в одном куске карточки
PIECE_OVERLAP = 200        # нахлёст: мысль, разрезанная границей, найдётся хотя бы раз
MAX_PIECES = 8             # потолок кусков на карточку: свалка не должна съедать индекс
PIECE_SEP = "¶"            # «Карточка¶2» — второй кусок; в именах карточек знака нет
TAIL_DISCOUNT = 0.90       # скидка хвостовому куску: у длинной карточки иначе больше бросков

BIN_MAGIC = b"AVEM"        # заголовок embeddings.bin v2: вектора + предфильтр
BIN_VER = 2
PF_K = 8                    # сколько осей предфильтру: главные компоненты данных
PF_ITERS = 12               # шагов итераций мощности на ось
PF_SAMPLE = 512             # строк в подвыборке итераций: больше — те же оси, дольше
PF_EPS = 1e-6               # запас на округление: связка консервативна, хиты не теряем


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
            # Пустышка — занятое имя без знания. В индексе она вредна вдвойне: всплывает
            # в выдаче как термин, у которого есть определение, и оттесняет карточку, где
            # определение действительно написано. На живом проекте таких имён тысячи.
            if is_placeholder(fm, text):
                continue
            head = " · ".join(x for x in (
                (fm.get("title") or f[:-3]).strip('"'),
                (fm.get("summary") or "").strip('"'),
                (fm.get("aliases") or "").strip("[]"),
            ) if x)
            whole = body(text)
            for i, part in enumerate(pieces(whole)):
                # Ключ индекса — КУСОК карточки, а не карточка. Так вся длинная карточка
                # оказывается в индексе, а не первые полторы тысячи знаков: на живой базе
                # 56% карточек длиннее куска, а девятый дециль — впятеро длиннее, то есть
                # больше половины базы искалось по началу и молчало об остальном.
                #
                # Заголовок и синонимы приписываются к КАЖДОМУ куску: иначе второй кусок
                # теряет, о чём он вообще, и находится как безымянный абзац.
                out[f[:-3] if i == 0 else f"{f[:-3]}{PIECE_SEP}{i}"] = \
                    (head + "\n" + part)[:PIECE]
    return out


def piece_owner(key: str) -> str:
    """Имя карточки по ключу индекса: «Карточка¶2» → «Карточка»."""
    return key.split(PIECE_SEP, 1)[0]


def pieces(text: str) -> list:
    """Тело карточки кусками с нахлёстом. Один кусок — карточка короче окна.

    Нахлёст нужен, чтобы мысль, разрезанная границей, всё-таки нашлась целиком хотя бы
    в одном куске. Число кусков ограничено: карточка на двести тысяч знаков — это не
    сущность, а свалка, и заводить под неё полсотни векторов значит платить за чужую
    ошибку разбором всей базы.
    """
    text = text or ""
    step = max(1, PIECE - PIECE_OVERLAP)
    out = [text[i:i + PIECE] for i in range(0, max(1, len(text)), step)]
    return [p for p in out[:MAX_PIECES] if p.strip()] or [""]


def digest(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


# Индекс, вектора и предфильтр перечитывались на КАЖДЫЙ запрос, а сборка контекста
# делает их пачками: один вопрос — десятки поисков, и каждый заново читал мегабайты с
# диска и распаковывал оси. Держим разобранное в процессе, а свежесть проверяем по
# отпечатку файла (размер и время правки) — пересборка индекса отпечаток меняет, и
# кеш сам себя выбрасывает. Ключ на файл: в одном процессе может жить не один проект.
_FILE_CACHE: dict = {}


def _stamp(path: str):
    """Отпечаток файла: (размер, время правки). Нет файла — None."""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _cached(path: str, kind: str, make):
    """Разобранное содержимое файла, пока файл не изменился."""
    key, stamp = (path, kind), _stamp(path)
    hit = _FILE_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    value = make()
    _FILE_CACHE[key] = (stamp, value)
    return value


def load_index() -> dict:
    """{имя: {hash, row}} плюс размерность и модель. Битый индекс — просто пустой."""
    return _cached(INDEX, "index", _read_index)


def _read_index() -> dict:
    try:
        with open(INDEX, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "cards" in data:
            return data
    except (OSError, ValueError):
        pass
    return {"model": "", "dim": 0, "built": "", "cards": {}}


def load_vectors(dim: int, rows: int) -> array.array:
    """Вектора по карточкам, из кеша по отпечатку файла."""
    return _cached(VECTORS, f"vectors-{dim}-{rows}", lambda: _read_vectors(dim, rows))


def _read_vectors(dim: int, rows: int) -> array.array:
    """Файл v2 начинается с заголовка (там же предфильтр); старый v1 — вектора сразу."""
    v = array.array("f")
    try:
        with open(VECTORS, "rb") as f:
            head = f.read(20)
        off = 0
        if head[:4] == BIN_MAGIC:
            ver, d, k, r = struct.unpack("<IIII", head[4:20])
            if ver != BIN_VER or d != dim or r != rows:
                return array.array("f")
            off = 20 + k * d * 8
        with open(VECTORS, "rb") as f:
            f.seek(off)
            v.fromfile(f, dim * rows)
    except (OSError, EOFError, struct.error):
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
        # Прогресс — про сборку индекса, где батчей сотни. Один вопрос поиска это
        # один батч, и строка «посчитано 1 из 1» на каждый запрос забивает вывод
        # тому, кто спрашивает базу: замер качества печатал её двести раз подряд.
        if len(texts) > BATCH:
            print(f"  посчитано {len(out)} из {len(texts)}", flush=True)
    return out


LAST_SEARCH = {"prefilter": False, "candidates": 0, "total": 0}


def load_prefilter(dim: int, rows: int):
    """(k, оси, по_строкам) или None, из кеша по отпечатку файла."""
    return _cached(VECTORS, f"prefilter-{dim}-{rows}", lambda: _read_prefilter(dim, rows))


def _read_prefilter(dim: int, rows: int):
    """(k, оси, по_строкам) или None. Предфильтр — производный слой: его отсутствие
    или рассинхрон с json — не ошибка, поиск просто делает полный перебор.
    по_строкам: для каждой строки (порядка row) k проекций на оси и норма остатка."""
    try:
        with open(VECTORS, "rb") as f:
            head = f.read(20)
            if head[:4] != BIN_MAGIC:
                return None
            ver, d, k, r = struct.unpack("<IIII", head[4:20])
            if ver != BIN_VER or d != dim or r != rows or k < 1:
                return None
            axes = list(struct.unpack(f"<{k * d}d", f.read(k * d * 8)))
            f.seek(20 + k * d * 8 + d * rows * 4)
            per_row = array.array("f")
            per_row.fromfile(f, rows * (k + 1))
            if len(per_row) < rows * (k + 1):
                return None
    except (OSError, EOFError, struct.error):
        return None
    return k, axes, per_row


def _power_axis(out, dim: int, sample: list, prev: list, rng: random.Random) -> list:
    """Одна ось методом итераций мощности на подвыборке строк. None — направление
    исчерпано (ранг данных меньше числа запрошенных осей)."""
    v = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    v = [x / sum(x * x for x in v) ** 0.5 for x in v]
    for _ in range(PF_ITERS):
        w = [sum(map(mul, v, out[i * dim:(i + 1) * dim])) for i in sample]
        v2 = [0.0] * dim
        for i, wi in zip(sample, w):
            if wi:
                v2 = list(map(add, v2, map(mul, repeat(wi), out[i * dim:(i + 1) * dim])))
        for a in prev:  # дефляция: новая ось ортогональна прежним
            d = sum(map(mul, v2, a))
            if d:
                v2 = list(map(sub, v2, map(mul, repeat(d), a)))
        n = sum(map(mul, v2, v2)) ** 0.5
        if n < 1e-12:
            return None
        v2 = [x / n for x in v2]
        if abs(abs(sum(map(mul, v2, v))) - 1.0) < 1e-9:
            return v2
        v = v2
    return v


def build_prefilter(out: array.array, dim: int, rows: int):
    """Оси главных компонент (метод итераций, stdlib) и построчные проекции.
    Возвращает (оси, по_строкам) или None, если база слишком мала для осей.
    Итерации идут по подвыборке строк: ось — статистическая характеристика базы,
    и пятьсот строк дают те же оси, что десять тысяч, за долю времени. Всё
    детерминировано: фиксированные старты, порядок и число шагов — пересборка
    индекса воспроизводима."""
    k = min(PF_K, dim, rows)
    if k < 2:
        return None
    step = max(1, rows // PF_SAMPLE)
    sample = list(range(0, rows, step))
    axes = []
    for t in range(k):
        a = _power_axis(out, dim, sample, axes, random.Random(41 + t))
        if a is None:
            break
        axes.append(a)
    if not axes:
        return None
    per_row = array.array("f")
    for r in range(rows):
        row = out[r * dim:(r + 1) * dim]
        proj = [sum(map(mul, a, row)) for a in axes]
        norm2 = sum(map(mul, row, row))
        slack = max(1e-6, (dim * 5e-16) ** 0.5) * norm2 ** 0.5
        perp = max(0.0, norm2 - sum(map(mul, proj, proj))) ** 0.5 + slack + 1e-12
        per_row.extend(proj)
        per_row.append(perp)
    return axes, per_row


PF_USEFUL_AT = 0.70        # доля базы, которую предфильтр обязан отсечь, чтобы остаться


def prefilter_pays_off(out: array.array, dim: int, rows: int, pf) -> tuple:
    """Отсекает ли предфильтр хоть что-нибудь на ЭТИХ векторах. → (стоит ли, доля, пояснение).

    Связка карточки — «проекция ± |остаток запроса| × |остаток карточки|». Если оси
    забирают малую часть нормы, остатки велики, связка шире всего разброса близостей — и
    кандидатами остаются все. Такой предфильтр не ускоряет, а замедляет: к полному
    перебору он добавляет свою арифметику и лишнее чтение с диска.

    Именно это и вышло на живой базе: bge-m3 раскладывается по восьми осям всего на 63 %
    нормы, кандидатов оставалось 3156 из 3156, замер дал 0.97×. Заявленные 3.23× меряли
    на синтетике, где данные ложатся по осям куда охотнее.

    Поэтому предфильтр обязан доказать пользу на своих же векторах, а не на чужих. Проба
    берёт вектора самих карточек как запросы: сети для этого не нужно.
    """
    if not pf or rows < 50:
        return False, 1.0, "база мала — перебор дешевле любой подготовки"
    axes, per_row = pf
    k = len(axes)
    probes = [i * max(1, rows // 24) for i in range(min(24, rows))]
    limit = min(40, max(1, rows // 4))
    seen = []
    for r in probes:
        qv = out[r * dim:(r + 1) * dim]
        qproj = [sum(a[j] * qv[j] for j in range(dim)) for a in axes]
        qperp = max(0.0, sum(x * x for x in qv) - sum(x * x for x in qproj)) ** 0.5
        lower = []
        for i in range(rows):
            base = i * (k + 1)
            pr = sum(qproj[j] * per_row[base + j] for j in range(k))
            lower.append((pr - qperp * per_row[base + k], pr + qperp * per_row[base + k]))
        thr = sorted((lo for lo, _ in lower), reverse=True)[limit - 1]
        seen.append(sum(1 for _, hi in lower if hi >= thr) / rows)
    share = sum(seen) / len(seen)
    if share <= PF_USEFUL_AT:
        return True, share, f"кандидатов {share*100:.0f} % базы"
    return False, share, (f"кандидатов {share*100:.0f} % базы — связки шире разброса "
                          f"близостей, отсекать нечего")


def save_index(model: str, dim: int, cards: dict, out: "array.array", pf) -> None:
    """Индекс на диск: файл v2 (заголовок, оси, вектора, проекции) и json-карта.
    Файлы производные: битые считываются как пустые, пересобираются --apply."""
    os.makedirs(META, exist_ok=True)
    axes, per_row = pf if pf else ([], array.array("f"))
    flat = [x for a in axes for x in a]
    with open(VECTORS, "wb") as f:
        f.write(struct.pack("<4sIIII", BIN_MAGIC, BIN_VER, dim, len(axes), len(cards)))
        if flat:
            f.write(struct.pack(f"<{len(flat)}d", *flat))
        out.tofile(f)
        per_row.tofile(f)
    with open(INDEX, "w", encoding="utf-8") as f:
        json.dump({"model": model, "dim": dim, "built": TODAY,
                   "cards": cards, "pf": len(axes)},
                  f, ensure_ascii=False, indent=1, sort_keys=True)

def search(query: str, cfg: dict, model: str, limit: int = 40) -> list:
    """[(имя карточки, близость)] по убыванию. Пустой список — индекса нет или он чужой.

    Полный перебор O(карточки × dim) заменён предфильтром: каждая карточка даёт
    нижнюю и верхнюю связки «проекция ± наихудший остаток»; кандидаты — лидеры по
    нижней связке, и набор растёт, пока связка не докажет, что вне него нет карточки,
    способной попасть в топ-`limit`. Точное скалярное считается только по кандидатам,
    и результат совпадает с полным перебором до места в выдаче. Лимит больше числа
    карточек или индекс без предфильтра (старый файл) — обычный полный перебор."""
    idx = load_index()
    if not idx["cards"] or idx.get("model") != model:
        return []
    dim, cards = idx["dim"], idx["cards"]
    vectors = load_vectors(dim, len(cards))
    if len(vectors) < dim * len(cards):
        return []
    if limit < 1:
        return []
    q = embed([query], cfg, model)
    if not q:
        return []
    qv = q[0]
    n = len(cards)
    # Внутри ищем по кускам, наружу отдаём карточки: у длинной карточки кусков несколько,
    # и близость её — лучшая из них. Просить у машины ровно `limit` кусков нельзя — они
    # могут оказаться кусками одной карточки; берём с запасом и режем после склейки.
    #
    # Запас берём ТОЛЬКО когда в индексе есть куски. Индекс без них (собран до 1.100.39)
    # ведёт себя ровно как прежде: лишний запас там ничего не находит, зато обесценивает
    # предфильтр — он перестаёт сужать, и поиск скатывается в полный перебор.
    want = min(n, max(limit * 3, limit + 8)) if any(
        PIECE_SEP in k for k in cards) else limit

    def by_card(scored: list) -> list:
        """[(кусок, близость)] → [(карточка, лучшая близость)], без повторов.

        Хвостовому куску — скидка. Без неё у длинной карточки просто больше попыток
        совпасть случайно: восемь кусков — восемь бросков против одного у короткой, и
        выдача кренится в сторону длинных. Голова карточки (заголовок, синонимы, начало
        тела) отвечает за то, о чём карточка вообще, поэтому она без скидки.
        """
        best: dict = {}
        for value, key in scored:
            owner = piece_owner(key)
            if key != owner:
                value *= TAIL_DISCOUNT
            if value > best.get(owner, -2.0):
                best[owner] = value
        rows = sorted(((v, nm) for nm, v in best.items()), reverse=True)
        return [(nm, round(v, 4)) for v, nm in rows[:limit]]

    def full_scan() -> list:
        scored = []
        for name, rec in cards.items():
            off = rec["row"] * dim
            scored.append((sum(qv[k] * vectors[off + k] for k in range(dim)), name))
        scored.sort(reverse=True)
        return by_card(scored)

    pf = load_prefilter(dim, n) if idx.get("pf") else None
    if pf is None or want >= n:
        LAST_SEARCH.update(prefilter=False, candidates=n, total=n)
        return full_scan()
    k, axes, per_row = pf
    qproj = [sum(map(mul, qv, axes[a * dim:(a + 1) * dim])) for a in range(k)]
    qperp = max(0.0, sum(map(mul, qv, qv)) - sum(map(mul, qproj, qproj))) ** 0.5 \
              + max(1e-6, (dim * 5e-16) ** 0.5)
    lower, upper, names = [], [], []
    for name, rec in cards.items():
        base = rec["row"] * (k + 1)
        p = sum(map(mul, qproj, per_row[base:base + k]))
        m = qperp * per_row[base + k] + PF_EPS
        lower.append(p - m)
        upper.append(p + m)
        names.append(name)
    order = sorted(range(n), key=lower.__getitem__, reverse=True)
    cands = [names[i] for i in order[:want]]
    rest = sorted(((upper[i], names[i]) for i in order[want:]), reverse=True)
    exact = {}

    def exact_of(name: str) -> float:
        v = exact.get(name)
        if v is None:
            off = cards[name]["row"] * dim
            v = sum(qv[j] * vectors[off + j] for j in range(dim))
            exact[name] = v
        return v

    pos, rounds = 0, 0
    while len(cands) < n:
        rounds += 1
        thr = sorted((exact_of(nm) for nm in cands), reverse=True)[want - 1]
        grew = 0
        while pos < len(rest) and rest[pos][0] >= thr:
            cands.append(rest[pos][1])
            pos += 1
            grew += 1
        if not grew:
            break
        if rounds >= 64:  # связка на этом запросе слишком широка — берём всех
            cands = names[:]
            break
    scored = sorted(((exact_of(nm), nm) for nm in cands), reverse=True)
    LAST_SEARCH.update(prefilter=True, candidates=len(exact), total=n)
    return by_card(scored)


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
    owners = {piece_owner(k) for k in texts}
    in_idx = {piece_owner(k) for k in idx["cards"]}
    print(f"Карточек в базе: {len(owners)} (кусков {len(texts)}) · в индексе: "
          f"{len(in_idx)} (кусков {len(idx['cards'])}) · модель: {idx.get('model') or '—'}")
    print(f"Считает: {model} на {where}"
          + ("" if cfg["embed"]["url"] else " (кольцо агента — своего адреса не задано)"))
    print(f"Пересчитать: {len(stale)} · выбыло: {len(gone)}")
    pf = load_prefilter(idx["dim"], len(idx["cards"])) if idx["cards"] else None
    print(f"Предфильтр: {pf[0]} осей" if pf else "Предфильтр: — (полный перебор)")

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

    pf = build_prefilter(out, dim, len(cards))
    # Записываем предфильтр, только если он себя оправдал на этих самых векторах:
    # бесполезный слой не нейтрален — он стоит арифметики и чтения с диска на каждый поиск.
    pays, share, why = prefilter_pays_off(out, dim, len(cards), pf)
    if not pays:
        pf = None
    save_index(model, dim, cards, out, pf)
    print(f"\n✅ Индекс собран: карточек {len({piece_owner(k) for k in cards})} "
          f"(кусков {len(cards)}), размерность {dim}, модель {model}"
          + (f" · предфильтр {len(pf[0])} осей ({why})" if pf
             else f" · предфильтр не пригодился: {why}") + ".")
    print(f"   Файлы: {VECTORS} и {INDEX} (в git не идут — это производная).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
