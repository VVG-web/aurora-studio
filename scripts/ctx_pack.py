#!/usr/bin/env python3
"""ctx_pack.py — сборка context pack по правилам ретрива (фреймворк «Аврора»).

Пак собирается детерминированно: отбор карточек, фильтр по статусу и релизу, шапки
доверия, преамбула и бюджет — это правила из `references/retrieval.md`, а не понимание.
Раньше их выполняла модель «по памяти»: шапка могла потеряться, `imported` — уехать в
контекст как факт, а `usage.log` (топливо очереди верификации) не вестись вовсе.

  python3 .opencode/scripts/ctx_pack.py "Заявка"                 # пак по теме
  python3 .opencode/scripts/ctx_pack.py "Заявка" --mode ask      # + история: deprecated и отклонённые DR
  python3 .opencode/scripts/ctx_pack.py "Заявка" --budget 8000   # ограничить объём (символов)
  python3 .opencode/scripts/ctx_pack.py "Заявка" --save          # + файл в Artifacts/drafts/

Режимы (`--mode`) по таблице retrieval.md:
  generate (по умолчанию), review — только knowledge (доверенный источник)
  ask                             — плюс deprecated и отклонённые/заменённые DR как история
  evaluate                        — плюс draft: знание из недоказанных источников

Bootstrap: пока verified меньше порога из `aurora.config.yaml`, непроверенные
карточки допускаются с громкой шапкой — и пак об этом честно предупреждает.

Каждая включённая карточка дописывается в `AuroraKnowledgeDB/meta/usage.log`: по этому
логу `kb_queue.py` понимает, что верифицировать первым.

Панель: `ctx:context`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

from aurora_common import TRUSTED, Card as BaseCard, body, frontmatter, link_targets, walk_md

ROOT = "AuroraKnowledgeDB"
USAGE = os.path.join(ROOT, "meta", "usage.log")
RELEASES = os.path.join(ROOT, "meta", "releases.md")
JIRA_MIRROR = os.path.join("Sources", "JIRA")
TODAY = date.today().isoformat()
# «US-4.7.2», «US 4.7.2», «4.7.2» в вопросе — номер истории; «PRJ-480» — ключ задачи
STORY_NUM = re.compile(r"(?i)\b(?:US|AC|ALG)?[\s._-]?(\d+(?:\.\d+){1,3})\b")
ISSUE_KEY = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
JIRA_ROWS = 12            # больше — это уже не ответ на вопрос, а выгрузка бэклога
MODE_STATUSES = {
    # Шкала после перехода на вычисляемое доверие: `knowledge` — знание из доверенного
    # источника, `draft` — из недоверенного либо недоказанного. Легаси-статусы
    # (`verified`, `imported`, `canonical`) читаются как прежде, пока проект не прошёл
    # пересборку: база не должна ослепнуть на время перехода.
    "generate": {"knowledge", "verified", "canonical"},
    "review": {"knowledge", "verified", "canonical"},
    "ask": {"knowledge", "verified", "canonical", "deprecated"},
    "evaluate": {"knowledge", "draft", "verified", "canonical", "in-review", "imported", ""},
}
PREAMBLE = (
    "Ниже — карточки базы знаний проекта. Класс доверия указан в шапке каждой карточки и\n"
    "вычислен движком по статусу связанных задач, а не проставлен человеком.\n"
    "knowledge — знание из доверенного источника: постановка устоялась, на это можно\n"
    "опираться. draft — источник недоверенный или связей с задачами нет: материал для\n"
    "оценки, не факт. deprecated — история, не применять.\n"
    "Противоречие двух knowledge-карточек — это ошибка базы, о которой надо сообщить.\n"
)






class Card(BaseCard):
    """Карточка в паке: общая шапка из aurora_common плюс то, что нужно ретриву."""

    def __init__(self, path: str, text: str):
        super().__init__(path, text, ROOT)
        self.title = self.fm.get("title", self.stem)
        self.aliases = re.findall(r'"([^"]+)"', self.fm.get("aliases", "")) or []
        self.applies_to = [x.strip().strip('"[]') for x in self.fm.get("applies_to", "").split(",")
                           if x.strip()]
        self.summary = (self.fm.get("summary") or "").strip().strip('"')

    @property
    def expired(self) -> bool:
        rb = (self.fm.get("review_by") or "").strip()
        return bool(rb and rb < TODAY and self.status in TRUSTED)

    def header(self) -> str:
        """Шапка доверия — инвариант 4: карточка не входит в промпт без неё."""
        st = self.status or "без статуса"
        if self.status == "deprecated":
            succ = self.fm.get("superseded_by", "—")
            return f"[deprecated | заменено: {succ} | только исторический контекст]"
        # Основание словами вместо имени владельца: доверие больше не чьё-то решение, и
        # спрашивать «кто принял» стало не у кого. Спрашивать надо «почему», а ответ на
        # это пишет `kb:trust` — статус задачи и то, чем доказана связь.
        why = (self.fm.get("trust_basis") or "").strip().strip('"')
        if self.status in TRUSTED:
            if self.expired:
                return (f"[{st} | ПЕРЕСЧИТАТЬ: {self.fm.get('review_by')} прошло — "
                        "статус задачи мог измениться]")
            return f"[{st} | доверенный источник | {why or 'основание не записано'}]"
        if self.section == "Reference":
            return "[reference | справочник домена]"
        return f"[{st} | НЕ ФАКТ | {why or 'источник не подтверждён задачей'}]"


def first_sentence(text: str) -> str:
    """Первая содержательная строка тела — заменитель `summary`, пока его нет.

    Заголовки, цитаты «История изменений» и разметку таблиц пропускаем: в оглавлении
    нужна фраза о сути, а не то, с чего начинается разметка страницы источника.
    """
    for line in body(text).splitlines():
        line = line.strip()
        if (not line or line.startswith(("#", ">", "|", "-", "*", "```", "!["))
                or line.startswith("_")):
            continue
        return " ".join(line.split())[:90]
    return ""


def load_cards() -> dict:
    cards = {}
    for dirpath, _, files in os.walk(ROOT):
        if "/meta" in dirpath.replace("\\", "/"):
            continue
        for f in files:
            if not f.endswith(".md") or f.startswith("_") or f == "index.md":
                continue
            p = os.path.join(dirpath, f)
            try:
                cards[os.path.splitext(f)[0]] = Card(p, open(p, encoding="utf-8", errors="ignore").read())
            except Exception:
                continue
    return cards


def threshold() -> int:
    cfg = "aurora.config.yaml"
    if os.path.isfile(cfg):
        m = re.search(r"verified_threshold_pct:\s*(\d+)", open(cfg, encoding="utf-8", errors="ignore").read())
        if m:
            return int(m.group(1))
    return 20


def current_release() -> str:
    if not os.path.isfile(RELEASES):
        return ""
    for line in open(RELEASES, encoding="utf-8", errors="ignore"):
        if "current" in line.lower():
            m = re.search(r"\b(R\d+[\w.\-]*)", line)
            if m:
                return m.group(1)
    return ""


# Слова, которые есть в любом запросе и ничего не отбирают.
STOP = {"и", "в", "во", "не", "на", "с", "со", "как", "что", "по", "для", "из", "за",
        "то", "же", "или", "а", "но", "у", "о", "об", "от", "до", "при", "если", "это",
        "нужно", "надо", "нужна", "нужен", "хочет", "может", "быть", "есть", "после",
        "перед", "чтобы", "когда", "где", "который", "которая", "которые", "его", "её",
        "их", "мы", "он", "она", "они", "я", "ты", "вы", "написать", "сделать"}

# Окончания русских словоформ. Полноценная морфология тянет словарь на мегабайты, а нам
# нужно ровно одно: чтобы «обеспечительного платежа» и «обеспечительный платёж» сошлись
# в одном ключе. Отрезаем хвост от длинного слова — этого хватает, а ошибки отсечения
# бьют одинаково по запросу и по карточке, поэтому пара всё равно сходится.
ENDINGS = ("ованием", "ования", "ованию", "ование", "ами", "ями", "ах", "ях", "ов", "ев",
           "ий", "ый", "ой", "ая", "яя", "ое", "ее", "ые", "ие", "ых", "их", "ому", "ему",
           "ого", "его", "ом", "ем", "ей", "ии", "ия", "ию", "ы", "и", "а", "я", "у", "ю",
           "е", "о", "ь", "й")


def norm(word: str) -> str:
    """Слово → ключ поиска: регистр, ё/е и словоформа не должны разводить пару."""
    w = word.lower().replace("ё", "е")
    if len(w) > 5:
        for end in ENDINGS:
            if w.endswith(end) and len(w) - len(end) >= 4:
                return w[:-len(end)]
    return w


def words(text: str) -> list:
    """Слова длиннее двух букв, приведённые к ключу поиска, без стоп-слов."""
    out = []
    for raw in re.findall(r"[\w\-]+", text.lower().replace("ё", "е"), re.U):
        if len(raw) < 3 or raw in STOP:
            continue
        out.append(norm(raw))
    return out


def semantic(topic: str, limit: int) -> dict:
    """{имя карточки: близость} по смыслу. Пусто — индекса нет, и это нормально.

    Слова находят то, что человек назвал; вектора — то, что он имел в виду. Вместе они
    работают лучше, чем поодиночке, поэтому это не замена словам, а добавка к ним.
    Индекса нет, сеть недоступна, модель сменилась — пак просто собирается по словам.
    """
    try:
        import agent_core as AG
        import kb_embed as E
        cfg = AG.parse_config(AG.raw_config())
        if not E.endpoints(cfg):
            return {}
        return {name: sim for name, sim in E.search(topic, cfg, cfg["embed"]["model"], limit)}
    except Exception:            # noqa: BLE001 — выборка не имеет права падать из-за сети
        return {}


RARITY: dict = {}          # {слово: во скольких карточках встречается}


def measure_rarity(cards: dict) -> None:
    """Во скольких карточках встречается каждое слово. Считается один раз на выборку.

    Без этого «статус» весит столько же, сколько «ЭСФ», хотя первое есть на каждой
    второй карточке, а второе выделяет одну. Частое слово почти не сужает поиск — значит
    и вес его должен быть меньше. Это обычный IDF, только считается по базе, которая уже
    прочитана, и потому ничего не стоит.
    """
    RARITY.clear()
    RARITY["__total__"] = max(1, len(cards))
    for c in cards.values():
        for w in set(words(c.stem) + words(c.title) + words(c.summary) + words(c.text)):
            RARITY[w] = RARITY.get(w, 0) + 1


def weight(word: str) -> float:
    """Множитель слова: редкое весит до двух раз больше частого, частое — вдвое меньше.

    Границы намеренно узкие. Полный IDF даёт разброс в десятки раз, и тогда одна опечатка
    в запросе (слово, которого нет в базе) перевешивает всё остальное.
    """
    total = RARITY.get("__total__", 0)
    if not total:
        return 1.0
    seen = RARITY.get(word, 0)
    if not seen:
        return 1.0          # слова нет в базе: судить не по чему, вес обычный
    share = seen / total
    if share > 0.30:
        return 0.5          # каждая третья карточка — слово почти ничего не сужает
    if share > 0.10:
        return 0.8
    if share < 0.01:
        return 2.0          # реже одной карточки из ста — это имя, код или термин
    return 1.2


def score(card: Card, topic: str, close: dict | None = None) -> int:
    """Насколько карточка отвечает теме: заголовок > алиас > теги > тело.

    Считаем по СЛОВАМ, а не по фразе целиком. Пока сравнивалась вся строка, живой
    запрос аналитика («вернуть обеспечительный платёж после аннулирования») не совпадал
    ни с чем и пак собирался из трёх случайных карточек: фраза целиком не встречается
    в базе никогда, а слова из неё — на каждой второй странице.
    """
    ask = words(topic)
    if not ask:
        return 0
    # Одна фраза о сути весит почти как заголовок: она написана про смысл карточки,
    # а не про то, как её назвали в источнике.
    head = set(words(card.stem) + words(card.title))
    brief = set(words(card.summary))
    alias = {w for a in card.aliases for w in words(a)}
    tag = set(words(card.tags))
    body = words(card.text)
    seen = {}
    for w in body:
        seen[w] = seen.get(w, 0) + 1

    s, hits = 0.0, 0
    for w in ask:
        got, part = False, 0
        if w in head:
            part += 10; got = True
        if w in brief:
            part += 8; got = True
        if w in alias:
            part += 6; got = True
        if w in tag:
            part += 3; got = True
        if w in seen:
            part += min(seen[w], 4); got = True
        # Редкое слово сужает поиск сильнее частого — значит и весит больше.
        s += part * weight(w)
        hits += got
    # Совпало всё, что спрашивали, — карточка про это, а не «упомянула слово».
    if hits == len(ask):
        s += 8
    elif hits * 2 < len(ask):
        s = s / 2           # половину запроса не нашли — это скорее шум
    # Близость по смыслу добавляется к словам, а не заменяет их: ниже 0.35 совпадение
    # случайно, выше — весит примерно как попадание в заголовок.
    if close:
        sim = close.get(card.stem, 0.0)
        if sim > 0.35:
            s += int((sim - 0.35) * 60)
    return int(s)


def collect(cards: dict, topic: str, statuses: set, bootstrap: bool,
            release: str, max_cards: int, close: dict | None = None) -> tuple:
    """Seed по теме → один переход по ссылкам. Возвращает (карточки, исключено по релизу)."""
    def allowed(c: Card) -> bool:
        # Заготовка проходит приёмку (утверждений в ней нет — не верить нечему), но в
        # контексте она пустое место: имя без содержания только съедает бюджет пака.
        if "заготовка" in c.tags or "_Заготовка:" in c.text:
            return False
        if c.status in statuses:
            return True
        return bootstrap and c.status in ("", "imported", "draft", "in-review")

    ranked = ((score(c, topic, close), c) for c in cards.values())
    seeds = sorted(((s, c) for s, c in ranked if s > 0), key=lambda x: (-x[0], x[1].stem))
    chosen, seen, dropped = [], set(), []
    for _, c in seeds:
        if len(chosen) >= max_cards:
            break
        if c.stem in seen or not allowed(c):
            continue
        if release and c.applies_to and release not in c.applies_to:
            dropped.append(c)
            continue
        seen.add(c.stem)
        chosen.append(c)

    # один переход по связям — так пак получает термины и соседей темы
    for c in list(chosen):
        for link in c.links():
            if len(chosen) >= max_cards:
                break
            nb = cards.get(link)
            if not nb or nb.stem in seen or not allowed(nb):
                continue
            if release and nb.applies_to and release not in nb.applies_to:
                continue
            seen.add(nb.stem)
            chosen.append(nb)

    # справочник аббревиатур — в каждый пак (retrieval.md)
    for c in cards.values():
        if c.section == "Reference" and re.search(r"(?i)аббревиатур|abbrev", c.stem + c.title):
            if c.stem not in seen:
                seen.add(c.stem)
                chosen.insert(0, c)
    return chosen, dropped


def jira_state(topic: str, limit: int = JIRA_ROWS) -> list:
    """Задачи зеркала Jira, о которых спрашивают: ключ, название, статус, эпик, дата.

    Статус задачи — не знание, а состояние. В карточке он был бы неправдой на следующий
    день после переноса задачи, поэтому по базе он не распыляется: карточки отвечают на
    «как это устроено», а «докуда дошла разработка» читается из зеркала при сборке пака.

    Без этого база честно отвечала «статуса US-4.7.2 у меня нет» — при том, что задача
    лежала в зеркале рядом, со статусом «Бэклог». Знание было в проекте, но не на пути
    к модели: пак собирается только из карточек.
    """
    if not os.path.isdir(JIRA_MIRROR):
        return []
    nums = {m.group(1) for m in STORY_NUM.finditer(topic)}
    keys = {m.group(1) for m in ISSUE_KEY.finditer(topic)}
    if not nums and not keys:
        return []
    out = []
    for path in sorted(walk_md(JIRA_MIRROR)):
        fm = frontmatter(open(path, encoding="utf-8", errors="ignore").read())
        unq = lambda v: (v or "").strip().strip('"\'')          # noqa: E731
        key, title = unq(fm.get("key")), unq(fm.get("title"))
        if not key:
            continue
        hit = key in keys or any(n in {x.group(1) for x in STORY_NUM.finditer(title)}
                                 for n in nums)
        if not hit:
            continue
        out.append({"key": key, "title": title, "status": unq(fm.get("status")) or "—",
                    "type": unq(fm.get("type")) or "—",
                    "epic": unq(fm.get("epic_title")) or "—",
                    "updated": (unq(fm.get("updated")) or "—")[:10]})
    return out[:limit]


def jira_block(rows: list) -> list:
    """Таблица состояния задач в пак. Отдельным разделом — чтобы не путать со знанием."""
    if not rows:
        return []
    out = ["## Состояние разработки (зеркало Jira, не карточки базы)", "",
           "| Задача | Название | Тип | Статус | Эпик | Обновлена |", "|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['key']} | {r['title'][:70]} | {r['type']} | **{r['status']}** "
                   f"| {r['epic'][:40]} | {r['updated']} |")
    out += ["",
            "> Это снимок внешней системы на момент последнего `sync:jira`, а не знание "
            "базы. Ссылайтесь на ключ задачи (PRJ-000), а не на карточку. Задачи нет в "
            "таблице — значит её нет и в зеркале.", ""]
    return out


def order(cards: list) -> list:
    rank = {"canonical": 1, "verified": 1}   # canonical — легаси-синоним
    return sorted(cards, key=lambda c: (c.expired, rank.get(c.status, 2), c.stem))


def log_usage(stems: list, command: str) -> None:
    os.makedirs(os.path.dirname(USAGE), exist_ok=True)
    with open(USAGE, "a", encoding="utf-8") as f:
        for s in stems:
            f.write(f"{TODAY}\t{command}\t{s}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Детерминированная сборка context pack")
    ap.add_argument("topic", help="тема запроса")
    ap.add_argument("--mode", default="generate", choices=sorted(MODE_STATUSES),
                    help="режим отбора: generate и review — только verified; "
                         "ask — плюс история; evaluate — всё")
    # Потолок в 15 карточек ставился под контекст в 8–32k токенов. Медианная карточка
    # базы — 1500 символов, сорок таких это 15k токенов: у модели со 128k остаётся место
    # и под постановку, и под шаблон, и под сам ответ. Резать выборку до пятнадцати
    # значит выбрасывать релевантное там, где места хватает.
    ap.add_argument("--max-cards", type=int, default=40,
                    help="потолок числа карточек в паке")
    ap.add_argument("--budget", type=int, default=0,
                    help="объём пака в символах; 0 — взять из объявленного окна модели")
    ap.add_argument("--release", help="релиз задачи (по умолчанию current из meta/releases.md)")
    ap.add_argument("--save", action="store_true", help="сохранить пак в Artifacts/drafts/")
    ap.add_argument("--no-log", action="store_true", help="не писать в meta/usage.log")
    ap.add_argument("--no-semantic", action="store_true",
                    help="только слова: не спрашивать семантический индекс (kb:embed)")
    ap.add_argument("--index", action="store_true",
                    help="оглавление базы вместо пака: строка на карточку "
                         "(имя · тип · статус · суть · путь) — модель выбирает сама")
    a = ap.parse_args()

    if not os.path.isdir(ROOT):
        print(f"ctx_pack: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    cards = load_cards()
    # Редкость слов считается один раз по всей базе — до ранжирования, а не в каждом
    # сравнении: иначе тот же счёт делался бы тысячу раз за одну выборку.
    measure_rarity(cards)
    if not cards:
        print("ctx_pack: в базе нет карточек", file=sys.stderr)
        return 1

    if a.index:
        # Оглавление вместо пака: вся база по строке на карточку. Выборка по словам
        # находит то, что человек назвал; оглавление решает другую задачу — показать
        # модели базу целиком, чтобы она увидела и то, чего в запросе не было.
        # Поэтому строка предельно скупа: раздел даёт группировка, путь выводится из
        # имени. Полная выкладка с путями и типами стоила бы 107k токенов — дороже,
        # чем весь остальной контекст вместе взятый.
        groups: dict = {}
        for c in cards.values():
            # Ключ словаря — имя карточки, а не путь: раздел берём у самой карточки.
            if "заготовка" in c.tags or c.status not in MODE_STATUSES[a.mode]:
                continue
            rel = os.path.relpath(c.path, ROOT).replace("\\", "/")
            section = rel.split("/")[0] if "/" in rel else "—"
            brief = c.summary or first_sentence(c.text)
            groups.setdefault(section, []).append(f"- {c.title[:60]} — {brief[:90]}")
        total = sum(len(v) for v in groups.values())
        print(f"# Оглавление базы знаний — {TODAY}\n")
        print(f"Карточек: {total}, режим {a.mode}. Строка на карточку: имя и суть.")
        print("Нашли нужное — возьмите полные тексты: `ctx:context <тема>`.\n")
        for section in sorted(groups):
            print(f"\n## {section} ({len(groups[section])})\n")
            print("\n".join(sorted(groups[section])))
        return 0

    trusted = sum(1 for c in cards.values() if c.status in TRUSTED)
    pct = trusted / len(cards) * 100
    bootstrap = pct < threshold()
    release = a.release if a.release is not None else current_release()
    close = {} if a.no_semantic else semantic(a.topic, a.max_cards * 2)
    chosen, dropped = collect(cards, a.topic, MODE_STATUSES[a.mode], bootstrap, release,
                              a.max_cards, close)
    jira = jira_state(a.topic)
    if not chosen and not jira:
        print(f"ctx_pack: по теме «{a.topic}» ничего не найдено. "
              "База не знает — не выдумывайте: заведите вопрос (kb:question) или карточку.",
              file=sys.stderr)
        return 1
    if not chosen:
        # Карточек нет, а задача в зеркале есть: «истории US-4.7.2 в базе нет, в Jira она
        # в бэклоге» — это полноценный ответ, и он лучше, чем «ничего не найдено».
        print(f"# Context pack: {a.topic}\n")
        print(f"_Собран {TODAY} · карточек по теме нет · задач в зеркале {len(jira)}_\n")
        print("\n".join(jira_block(jira)))
        print("> Карточек по теме в базе нет: знание о задаче не описано. Так и ответьте — "
              "и предложите завести карточку или вопрос (`kb:question`).")
        return 0
    chosen = order(chosen)

    out = [f"# Context pack: {a.topic}", "",
           f"_Собран {TODAY} · режим {a.mode} · карточек {len(chosen)}"
           + (" · поиск по словам и смыслу" if close else " · поиск по словам")
           + (f" · релиз {release}" if release else "")
           + f" · verified в базе {pct:.1f}%_", "", PREAMBLE]
    if bootstrap:
        out += [f"> ⚠️ **BOOTSTRAP:** проверено {pct:.1f}% базы (порог {threshold()}%). "
                "Ниже есть непроверенные карточки — они помечены и фактами не являются.\n"]
    if dropped:
        out += [f"> Исключено по релизу {release}: {len(dropped)} карточек "
                f"({', '.join(c.stem for c in dropped[:5])}…)\n"]
    out += jira_block(jira)

    # Пак собирался вслепую к окну модели: `--budget` в символах ставил человек, а если
    # не ставил — пак рос без предела и упирался в шлюз уже на стороне модели. Окно
    # объявлено — берём его; не объявлено — предела нет, как и раньше: движок не
    # придумывает чужие ограничения.
    if not a.budget:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import agent_core as AG
            cfg = AG.parse_config(AG.raw_config())
            a.budget = AG.prompt_budget(cfg, reserve_chars=len("".join(out)) + 4000)
            if a.budget:
                out.append(f"\n> Объём пака ограничен окном модели: {a.budget} символов.\n")
        except Exception:                                   # noqa: BLE001
            a.budget = 0        # агент не настроен — это не повод не собрать пак

    used, total = [], 0
    for c in chosen:
        block = f"\n---\n\n## {c.title}\n\n{c.header()}\n\n{body(c.text).strip()}\n"
        if a.budget and total + len(block) > a.budget:
            out.append(f"\n> ⚠️ Бюджет {a.budget} символов исчерпан: не вошло "
                       f"{len(chosen) - len(used)} карточек ({', '.join(x.stem for x in chosen[len(used):][:5])}…)")
            break
        out.append(block)
        used.append(c)
        total += len(block)

    unverified = [c.stem for c in used if c.status not in TRUSTED and c.status != "deprecated"]
    if unverified:
        out += ["\n---\n", f"> Эти карточки ждут верификации — примите с дефолтами после беглой "
                f"проверки: {', '.join(unverified)}"]

    text = "\n".join(out)
    print(text)
    if a.save:
        path = os.path.join("Artifacts", "drafts",
                            f"{TODAY}_context_{re.sub(r'[^0-9A-Za-zА-Яа-яёЁ]+', '-', a.topic)[:60]}.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(text + "\n")
        print(f"\n_Сохранено: {path}_", file=sys.stderr)
    if not a.no_log:
        log_usage([c.stem for c in used], a.mode)
    print(f"\n_Карточек в паке: {len(used)} · символов: {total}_", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
