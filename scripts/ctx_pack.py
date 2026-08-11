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
  generate (по умолчанию), review — только verified
  ask                             — плюс deprecated и отклонённые/заменённые DR как история
  evaluate                        — плюс draft/in-review/imported (оценка кандидатов)

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
TODAY = date.today().isoformat()
MODE_STATUSES = {
    # canonical — легаси-синоним verified (убран из схемы в 1.10.0): старые базы
    # не должны разом потерять доверие к карточкам
    "generate": {"canonical", "verified"},
    "review": {"canonical", "verified"},
    "ask": {"canonical", "verified", "deprecated"},
    "evaluate": {"canonical", "verified", "draft", "in-review", "imported", ""},
}
PREAMBLE = (
    "Ниже — карточки базы знаний проекта. Уровень доверия указан в шапке каждой карточки.\n"
    "verified — факты; imported/draft — материал для оценки, не факты;\n"
    "deprecated — история, не применять. При противоречии верь карточке с более высоким\n"
    "статусом и более свежей датой verified; противоречие verified-карточек — это ошибка,\n"
    "о которой надо сообщить.\n"
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
        st, owner = self.status or "без статуса", self.fm.get("owner", "—")
        if self.status == "deprecated":
            succ = self.fm.get("superseded_by", "—")
            return f"[deprecated | заменено: {succ} | только исторический контекст]"
        if self.status in TRUSTED:
            if self.expired:
                return (f"[{st} | ПРОСРОЧЕНО: review_by {self.fm.get('review_by')} — "
                        "возможно устарело, перепроверь]")
            return (f"[{st} | проверено {self.fm.get('verified', '—')} | владелец {owner} | "
                    f"годно до {self.fm.get('review_by', '—')}]")
        if self.section == "Reference":
            return "[reference | справочник домена]"
        return f"[{st} | НЕ ПРОВЕРЕНО ЧЕЛОВЕКОМ | не считать фактом]"


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
        if not cfg["backends"]:
            return {}
        model = AG.raw_config().get("AURORA_AGENT_EMBED_MODEL") or "bge-m3"
        return {name: sim for name, sim in E.search(topic, cfg, model, limit)}
    except Exception:            # noqa: BLE001 — выборка не имеет права падать из-за сети
        return {}


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

    s, hits = 0, 0
    for w in ask:
        got = False
        if w in head:
            s += 10; got = True
        if w in brief:
            s += 8; got = True
        if w in alias:
            s += 6; got = True
        if w in tag:
            s += 3; got = True
        if w in seen:
            s += min(seen[w], 4); got = True
        hits += got
    # Совпало всё, что спрашивали, — карточка про это, а не «упомянула слово».
    if hits == len(ask):
        s += 8
    elif hits * 2 < len(ask):
        s = s // 2          # половину запроса не нашли — это скорее шум
    # Близость по смыслу добавляется к словам, а не заменяет их: ниже 0.35 совпадение
    # случайно, выше — весит примерно как попадание в заголовок.
    if close:
        sim = close.get(card.stem, 0.0)
        if sim > 0.35:
            s += int((sim - 0.35) * 60)
    return s


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
    ap.add_argument("--budget", type=int, default=0, help="ограничение объёма пака в символах")
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
    if not chosen:
        print(f"ctx_pack: по теме «{a.topic}» ничего не найдено. "
              "База не знает — не выдумывайте: заведите вопрос (kb:question) или карточку.",
              file=sys.stderr)
        return 1
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
