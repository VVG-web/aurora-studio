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
  python3 .aurora/scripts/kb_raptor.py --query "..." --global
  python3 .aurora/scripts/kb_raptor.py --metrics --apply  # обновить замер
  python3 .aurora/scripts/kb_raptor.py --check            # свеж ли индекс

Зависимостей нет; модель не вызывается. `--vectors` добавляет готовый
семантический индекс `kb:embed` (один запрос эмбеддинга на вопрос), без индекса
остаётся словесный поиск. Индекс хранит только имена, заголовки и короткие
тексты, а не копию базы.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aurora_common import (KB_ROOT, body, frontmatter, replace_file, section_of,
                           trust_header, untrusted, walk_md)  # noqa: E402

ROOT = KB_ROOT
INDEX = os.path.join(ROOT, "meta", "raptor.json")
MOC_DIR = os.path.join(ROOT, "MOC")
# Версия меняется, когда меняется смысл полей индекса. Иначе после правки
# правил старый raptor.json выглядит «свежим»: хеши источников те же, а
# текст узлов посчитан по старым правилам.
INDEX_VERSION = 3
LINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
STOP = {"и", "в", "во", "не", "на", "с", "со", "как", "что", "по", "для", "из",
        "за", "то", "же", "или", "а", "но", "у", "о", "об", "от", "до", "при",
        "если", "это", "the", "a", "an", "of", "to", "in", "and", "or", "is",
        "какие", "какой", "какая", "каких", "кто", "где", "куда", "откуда",
        "сколько", "входит", "входят", "относится", "относятся", "есть",
        "лежат", "лежит", "описано", "описаны", "описан", "карточки", "про",
        "чем", "этом", "эти", "этот", "описывает", "описывают", "опиши",
        "определены", "определено", "найдено", "найдены", "все", "всех",
        "какие-то", "какой-то", "назови", "перечисли"}

# Слова, которые в вопросе значат одно и то же. Приводим их к одной основе
# до стемминга: иначе «сервисы» и «Системы» не встречаются, хотя человек
# спрашивает об одном. Это не словарь русского языка, а мост между тем, как
# называют разделы в этой базе, и тем, как о них спрашивают.
SYNONYMS = {
    "эпик": "epic", "эпику": "epic", "эпика": "epic", "эпиков": "epic",
    "эпики": "epic", "эпикам": "epic", "epic": "epic",
    "пустые": "пуст", "пустой": "пуст", "пустых": "пуст", "пустая": "пуст",
    "пустышки": "пуст", "пустышк": "пуст", "пустышка": "пуст",
    "брошены": "брошен", "брошенных": "брошен", "брошенной": "брошен",
    "брошенные": "брошен", "заброшенные": "брошен",
    "цитируемые": "цитир", "цитируется": "цитир", "цитируются": "цитир",
    "цитаты": "цитир", "цитат": "цитир", "цитирование": "цитир",
    "глоссарий": "глоссари", "глоссария": "глоссари", "глоссарии": "глоссари",
    "термины": "термин", "терминов": "термин", "терминам": "термин",
    "понятия": "понят", "понятий": "понят", "понятиям": "понят",
    "документы": "документ", "документов": "документ", "документам": "документ",
    "системы": "систем", "система": "систем", "системам": "систем",
    "сервис": "систем", "сервисы": "систем", "сервисов": "систем",
    "подсистема": "систем", "подсистемы": "систем",
    "требования": "требован", "требований": "требован", "требованиям": "требован",
    "процессы": "процесс", "процессов": "процесс", "процессам": "процесс",
    "справочники": "справочник", "справочников": "справочник",
    "приложение": "приложени", "приложения": "приложени", "приложений": "приложени",
}
ROOTS = set(SYNONYMS.values())

# Формы, которые стеммер снимает до общей основы. Порядок важен: длинные
# окончания проверяются раньше коротких, иначе «требования» потеряет только
# «я» и перестанет совпадать с «требований».
SUFFIXES = ("иями", "ями", "ами", "ыми", "ими", "ения", "ений", "ением",
            "ации", "ацию", "ацией", "ость", "ости", "ия", "ии", "ию", "ий",
            "ей", "ой", "ый", "ая", "ое", "ые", "ых", "их", "ую", "юю",
            "ам", "ям", "ах", "ях", "ом", "ем", "а", "я", "ы", "и", "у", "ю",
            "е", "о")

# Общий вопрос спрашивает про раздел или состав, а не про один документ.
# Это правило намеренно простое и объяснимое: его можно проверить глазами,
# в отличие от модели, которая «решила», что вопрос общий.
GLOBAL_RE = re.compile(
    r"\b(какие|какой|какая|каких|каково|сколько|перечисли|список|обзор|состав|"
    r"назови|что\s+(?:входит|относится|описывает|описано|есть)|"
    r"где\s+(?:лежат|лежит))\b", re.UNICODE)
DETAIL_RE = re.compile(
    r"\b(как|почему|зачем|когда|куда|откуда|что\s+делать|инструкц|настро|"
    r"исправ|созда|добав|удал|обнов|запусти|проверь)\b", re.UNICODE)
# Код карточки — признак точного вопроса. Epic сюда не входит: это имя
# раздела, и «что входит в Epic-4» как раз общий вопрос.
CODE_RE = re.compile(r"\b(?:US|AC|ALG|DR|REQ|PRJ|BR|KB|TASK)[-_ ]?\d", re.IGNORECASE)
RANK_CODE_RE = re.compile(
    r"\b(?:US|AC|ALG|DR|REQ|PRJ|BR|KB|TASK|EPIC)[-_ ]?\d+(?:[._]\d+)*",
    re.IGNORECASE)


def _fold(text: str) -> str:
    """Нижний регистр и «ё» как «е»: в базе пишут и так, и так."""
    return (text or "").lower().replace("ё", "е")


def _stem(word: str) -> str:
    """Грубая основа слова: снимает частые русские окончания, не трогая корень."""
    if word in ROOTS:
        return word
    if len(word) <= 4:
        return word
    for suffix in SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[:-len(suffix)]
    return word


def _hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=12).hexdigest()


def _words(text: str) -> list:
    out = []
    for raw in WORD_RE.findall(text or ""):
        w = _fold(raw)
        if w in STOP:
            continue
        w = SYNONYMS.get(w, w)
        out.append(w if w in ROOTS else _stem(w))
    return out


def _codes(text: str) -> set:
    """Коды карточек в одном написании: `US-4.2.11`, `US 4.2.11`, `эпик 4`."""
    folded = re.sub(r"эпик\w*", "epic", _fold(text))
    out = set()
    for raw in RANK_CODE_RE.findall(folded):
        out.add(re.sub(r"[-_ .]+", "-", raw.strip()).strip("-"))
    return out


def _read(path: str) -> str:
    try:
        return open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return ""


def _summary(text: str, limit: int = 1200, with_body: bool = True) -> str:
    """Заголовок, summary и (для карточки) начало тела.

    У MOC тело — список ссылок на детей. Если считать его слова, раздел
    «Процессы» проигрывает документу, в списке которого чаще встречается
    слово «процесс»: это совпадение имён детей, а не темы раздела.
    """
    fm = frontmatter(text)
    title = (fm.get("title") or "").strip().strip('"')
    summary = (fm.get("summary") or "").strip().strip('"')
    first = " ".join(body(text).split())[:limit] if with_body else ""
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
                      "text": _summary(text, with_body=False), "cards": children,
                      "children": []})
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
    return {"version": INDEX_VERSION, "nodes": nodes, "cards": child_of, "hashes": hashes}


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
    return (old.get("version") == now.get("version")
            and old.get("hashes") == now.get("hashes")), \
        "правила или хеши источников изменились"


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


def classify_global(query: str) -> dict:
    """Общий ли вопрос. → {global, why}; правило объяснимое, без вызова модели."""
    text = " ".join(_fold(query).split())
    if not text:
        return {"global": False, "why": "вопрос пуст"}
    if CODE_RE.search(text):
        return {"global": False, "why": "назван точный код карточки"}
    if DETAIL_RE.search(text):
        return {"global": False, "why": "вопрос о том, как что-то сделать"}
    hit = GLOBAL_RE.search(text)
    if hit:
        return {"global": True, "why": f"общий оборот «{hit.group(0)}»"}
    return {"global": False, "why": "нет общего оборота"}


def is_global_question(query: str) -> bool:
    """Короткий ответ для маршрута: добавлять ли обзор к обычному поиску."""
    return bool(classify_global(query)["global"])


def _vector_scores(query: str, cfg: dict | None, limit: int) -> dict:
    """{карточка: близость} из готового семантического индекса, если он есть.

    Вектора не строятся здесь и модель не вызывается на каждую карточку:
    `kb_embed` уже хранит нормированный индекс. Если индекса нет или модель
    другая, возвращается пустой словарь и остаётся словесный поиск.
    """
    if not cfg:
        return {}
    try:
        import kb_embed as E
        model = str((cfg.get("embed") or {}).get("model") or "")
        if not model:
            return {}
        rows = E.search(query, cfg, model, limit=max(20, limit * 4))
    except Exception:  # noqa: BLE001 — индекс/шлюз не должны ломать обзор
        return {}
    out = {}
    for name, score in rows or []:
        owner = str(name).split("¶", 1)[0]
        out[owner] = max(out.get(owner, 0.0), float(score))
    return out


def rank(query: str, limit: int = 10, cfg: dict | None = None,
         vectors: bool = False) -> list:
    """Узлы по вопросу: словесные совпадения, при возможности — с векторами.

    Словесный счёт остаётся главным: 70 % порядка. Вектора добавляют 30 % и
    помогают пересказам, где общих слов нет. Такой же принцип у реранкера,
    чтобы два механизма не тянули выдачу в разные стороны.
    """
    data = load()
    if not data:
        return []
    ask = set(_words(query))
    # «Есть в системе» — это место, а не тема: иначе «какие процессы есть в
    # системе» находит раздел «Системы» вместо «Процессы».
    if re.search(r"\bв\s+системе\b", _fold(query)):
        ask.discard("систем")
    codes = _codes(query)
    if not ask:
        return []
    scored, raw = [], {}
    for node in data.get("nodes") or []:
        words = _words(" ".join((node.get("text") or "").split()))
        hit = sum(words.count(w) for w in ask)
        card_exact = any(set(_words(card)) == ask for card in (node.get("cards") or []))
        if not hit and not card_exact:
            continue
        title = set(_words(node.get("title") or ""))
        # Верхний узел важнее листовой при равном совпадении: обзор отвечает
        # на глобальный вопрос, а не тонет в одной карточке.
        value = (float(hit) + 5.0 * len(ask & title)
                 + 0.25 * int(node.get("level") or 0))
        asked = " ".join(_fold(query).split())
        named = " ".join(_fold(node.get("title") or "").split())
        if named and named in asked:
            # «Что входит в Требования» называет раздел целиком. Документ,
            # в заголовке которого тоже есть «требования», не должен
            # выигрывать только потому, что он длиннее.
            value += 8.0
        if card_exact:
            # Человек назвал саму карточку, а не раздел: обзор её родителя
            # всё равно правильный, даже если имени раздела в вопросе нет.
            value += 20.0
        if codes and codes & _codes(node.get("title") or ""):
            # Названный код — сильнее любого совпадения слов: это уже не
            # пересказ, а точное имя карточки или раздела.
            value += 100.0
        raw[id(node)] = value
        scored.append((value, node))
    vmap = _vector_scores(query, cfg, limit) if vectors else {}
    if not scored and not vmap:
        return []
    base = max(raw.values(), default=0.0) or 1.0
    rows = []
    for value, node in scored:
        vs = max((vmap.get(c, 0.0) for c in node.get("cards") or []), default=0.0)
        score = 0.7 * value / base + 0.3 * vs if vmap else value
        rows.append((score, node))
    seen = {id(node) for _v, node in rows}
    for node in data.get("nodes") or []:
        if id(node) in seen:
            continue
        vs = max((vmap.get(c, 0.0) for c in node.get("cards") or []), default=0.0)
        if vs > 0:
            rows.append((0.3 * vs, node))
    rows.sort(key=lambda x: (-x[0], x[1].get("title") or ""))
    return rows[:max(1, limit)]


def _card_index() -> dict:
    """{stem: (путь, текст)} без MOC и служебных файлов."""
    out = {}
    for path in walk_md(ROOT, skip_service=True, skip_archive=True):
        norm = path.replace("\\", "/")
        if "/MOC/" in norm or norm.endswith("/MOC"):
            continue
        text = _read(path)
        if text:
            out[os.path.basename(path)[:-3]] = (path, text)
    return out


def context(query: str, cfg: dict | None = None, max_cards: int = 12,
            skip=(), vectors: bool = True) -> dict:
    """Пак обзора для общего вопроса. → {used, title, cards, pack, why}.

    Это дополнение к обычному паку, а не замена: узел даёт состав раздела,
    карточки — имена для ссылок. Уже найденные карточки не повторяются.
    """
    info = classify_global(query)
    if not info["global"]:
        return {"used": False, "why": info["why"], "pack": "", "cards": []}
    rows = rank(query, limit=5, cfg=cfg, vectors=vectors)
    if not rows:
        return {"used": False, "why": "обзор не нашёл узел", "pack": "", "cards": []}
    score, node = rows[0]
    skip = set(skip or ())
    names = [c for c in (node.get("cards") or []) if c not in skip][:max(1, max_cards)]
    index = _card_index()
    out = [f"# Обзор базы: {node.get('title')}", ""]
    summary = " ".join((node.get("text") or "").split())[:900]
    if summary:
        out += ["> " + summary, ""]
    out += [f"Узел обзора: {len(node.get('cards') or [])} карточек; "
            f"в пак добавлено: {len(names)}.",
            ""]
    used = []
    for stem in names:
        pair = index.get(stem)
        if not pair:
            continue
        path, text = pair
        fm = frontmatter(text)
        title = (fm.get("title") or stem).strip().strip('"')
        part = " ".join(body(text).split())[:3000]
        out += ["", "---", "", f"## {stem}", "",
                trust_header(fm, section_of(path, ROOT), text), "",
                untrusted((f"Название: {title}\n\n" if title and title != stem else "")
                          + part, "карточка " + stem), ""]
        used.append(stem)
    return {"used": bool(used), "title": node.get("title"), "id": node.get("id"),
            "score": round(score, 3), "cards": used,
            "pack": "\n".join(out).strip() + "\n", "why": info["why"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="RAPTOR-дерево из тезисов и MOC")
    ap.add_argument("--build", action="store_true", help="собрать/проверить индекс")
    ap.add_argument("--apply", action="store_true", help="записать meta/raptor.json")
    ap.add_argument("--check", action="store_true", help="только проверить свежесть")
    ap.add_argument("--query", metavar="ТЕКСТ", default="", help="найти узлы по вопросу")
    ap.add_argument("--global", dest="only_global", action="store_true",
                    help="показать, считается ли вопрос общим")
    ap.add_argument("--vectors", action="store_true",
                    help="добавить к словесному поиску готовый семантический индекс")
    ap.add_argument("--metrics", action="store_true",
                    help="после сборки обновить meta/raptor_metrics.json")
    ap.add_argument("--questions", default="", metavar="ПУТЬ",
                    help="файл вопросов для --metrics")
    ap.add_argument("--paraphrase-from", type=int, default=0, metavar="N",
                    help="для --metrics: считать вопросы с N-го пересказами")
    ap.add_argument("--limit", type=int, default=10, metavar="N")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    a = ap.parse_args()
    if a.check:
        ok, why = check()
        print(("свежо" if ok else "устарело: " + why))
        return 0 if ok else 1
    if a.build or not a.query:
        build(apply=a.apply)
    if a.only_global and a.query:
        info = classify_global(a.query)
        if a.json:
            print(json.dumps(info, ensure_ascii=False))
        else:
            print(("общий" if info["global"] else "точный") + ": " + info["why"])
    if a.metrics:
        import kb_raptor_eval as E
        questions = a.questions or E.default_questions()
        if not os.path.isfile(questions):
            # Замер — необязательная часть: без файла вопросов маршрут
            # «Починить базу» не должен краснеть из-за отсутствия метрик.
            print("обзор: файла вопросов нет — замер пропущен")
            print("RAPTOR_METRICS_SKIPPED")
        else:
            args = ["--apply", "--quiet", "--questions", questions]
            if a.paraphrase_from:
                args += ["--paraphrase-from", str(a.paraphrase_from)]
            if a.vectors:
                args.append("--vectors")
            rc = E.main(args)
            if rc:
                return rc
    if a.query:
        cfg = None
        if a.vectors:
            try:
                import agent_core as AG
                cfg = AG.config()
            except Exception:  # noqa: BLE001 — без настройки остаётся словесный поиск
                cfg = None
        rows = rank(a.query, a.limit, cfg=cfg, vectors=a.vectors)
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
