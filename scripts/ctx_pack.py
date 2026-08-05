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
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date

from aurora_common import TRUSTED, body, frontmatter, link_targets, walk_md

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






class Card:
    def __init__(self, path: str, text: str):
        self.path = path.replace("\\", "/")
        self.stem = os.path.splitext(os.path.basename(path))[0]
        self.fm = frontmatter(text)
        self.text = text
        self.section = os.path.relpath(os.path.dirname(self.path), ROOT).split(os.sep)[0]
        self.status = (self.fm.get("status") or "").strip()
        self.title = self.fm.get("title", self.stem)
        self.aliases = re.findall(r'"([^"]+)"', self.fm.get("aliases", "")) or []
        self.tags = self.fm.get("tags", "")
        self.links = link_targets(text)
        self.applies_to = [x.strip().strip('"[]') for x in self.fm.get("applies_to", "").split(",") if x.strip()]

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


def score(card: Card, topic: str) -> int:
    """Насколько карточка отвечает теме: заголовок > алиас > теги > тело."""
    t = topic.lower()
    s = 0
    if t in card.stem.lower() or t in card.title.lower():
        s += 10
    if any(t in a.lower() for a in card.aliases):
        s += 6
    if t in card.tags.lower():
        s += 3
    s += min(card.text.lower().count(t), 5)
    return s


def collect(cards: dict, topic: str, statuses: set, bootstrap: bool,
            release: str, max_cards: int) -> tuple:
    """Seed по теме → один переход по ссылкам. Возвращает (карточки, исключено по релизу)."""
    def allowed(c: Card) -> bool:
        # Заготовка проходит приёмку (утверждений в ней нет — не верить нечему), но в
        # контексте она пустое место: имя без содержания только съедает бюджет пака.
        if "заготовка" in c.tags or "_Заготовка:" in c.text:
            return False
        if c.status in statuses:
            return True
        return bootstrap and c.status in ("", "imported", "draft", "in-review")

    seeds = sorted(((score(c, topic), c) for c in cards.values() if score(c, topic) > 0),
                   key=lambda x: (-x[0], x[1].stem))
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
        for link in c.links:
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
    ap.add_argument("--max-cards", type=int, default=15,
                    help="потолок числа карточек в паке")
    ap.add_argument("--budget", type=int, default=0, help="ограничение объёма пака в символах")
    ap.add_argument("--release", help="релиз задачи (по умолчанию current из meta/releases.md)")
    ap.add_argument("--save", action="store_true", help="сохранить пак в Artifacts/drafts/")
    ap.add_argument("--no-log", action="store_true", help="не писать в meta/usage.log")
    a = ap.parse_args()

    if not os.path.isdir(ROOT):
        print(f"ctx_pack: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    cards = load_cards()
    if not cards:
        print("ctx_pack: в базе нет карточек", file=sys.stderr)
        return 1

    trusted = sum(1 for c in cards.values() if c.status in TRUSTED)
    pct = trusted / len(cards) * 100
    bootstrap = pct < threshold()
    release = a.release if a.release is not None else current_release()
    chosen, dropped = collect(cards, a.topic, MODE_STATUSES[a.mode], bootstrap, release, a.max_cards)
    if not chosen:
        print(f"ctx_pack: по теме «{a.topic}» ничего не найдено. "
              "База не знает — не выдумывайте: заведите вопрос (kb:question) или карточку.",
              file=sys.stderr)
        return 1
    chosen = order(chosen)

    out = [f"# Context pack: {a.topic}", "",
           f"_Собран {TODAY} · режим {a.mode} · карточек {len(chosen)}"
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
