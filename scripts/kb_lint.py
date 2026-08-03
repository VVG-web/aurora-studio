#!/usr/bin/env python3
"""kb_lint.py — механический линтер AuroraKnowledgeDB (фреймворк «Аврора»).

Проверки:
  1. Битые wiki-ссылки [[X]] (резолв по имени файла и aliases).
  2. Карточки со status, но без обязательных полей (verified => owner, review_by).
  3. deprecated без superseded_by.
  4. Бинарники в _assets/ без карточки-обёртки (нет входящих ![[...]] / [[...]]).
  5. Дубликаты aliases (один alias у двух карточек).

Легаси-карточки без status валидны (= imported) и не флагуются за отсутствие полей.
Запуск из корня репозитория: python3 .opencode/scripts/kb_lint.py [--summary]
Выход: код 0 если ошибок нет, 1 если есть (пригодно для pre-commit/CI).
"""
import os, re, sys, collections

from aurora_common import aliases, body_hash, card_body, frontmatter, link_refs

ROOT = "AuroraKnowledgeDB"


def load_releases() -> set:
    """Релизы проекта из meta/releases.md — реестр, по которому фильтруется контекст."""
    path = os.path.join(ROOT, "meta", "releases.md")
    if not os.path.isfile(path):
        return set()
    out = set()
    for line in open(path, encoding="utf-8", errors="ignore"):
        for m in re.finditer(r"\b(R\d+[\w.\-]*)", line):
            out.add(m.group(1))
    return out


def main():
    summary = "--summary" in sys.argv   # только итоговая строка: карточек и ошибок
    releases = load_releases()
    names, alias_owner, cards = set(), {}, {}
    dup_aliases, errors = [], []

    if not os.path.isdir(ROOT):
        print(f"kb_lint: нет папки {ROOT}/ — запускайте из корня репозитория проекта", file=sys.stderr)
        return 1

    for dirpath, _, files in os.walk(ROOT):
        for f in files:
            if not f.endswith(".md"):
                continue
            path = os.path.join(dirpath, f)
            rel = path.replace("\\", "/")
            stem = os.path.splitext(f)[0]
            names.add(stem)
            try:
                text = open(path, encoding="utf-8").read()
            except Exception as e:
                errors.append(f"{rel}: не читается ({e})")
                continue
            fm = frontmatter(text)
            cards[rel] = (fm, text)
            for a in aliases(text):
                if a in alias_owner and alias_owner[a] != rel:
                    dup_aliases.append((a, alias_owner[a], rel))
                else:
                    alias_owner[a] = rel

    resolvable = names | set(alias_owner.keys())

    for rel, (fm, text) in cards.items():
        status = fm.get("status", "")
        if status == "verified":
            if not fm.get("owner"):
                errors.append(f"{rel}: verified без owner")
            if not fm.get("review_by"):
                errors.append(f"{rel}: verified без review_by")
            # Карточку правят и после приёмки — картотека живёт, это нормально. Но
            # `verified` относится к тексту, который человек читал: если текст с тех пор
            # другой, статус говорит неправду, и это надо увидеть, а не запретить правку.
            stamp = (fm.get("verified_hash") or "").strip().strip('"')
            if stamp and stamp != body_hash(card_body(text)):
                errors.append(f"{rel}: правили после приёмки — тело не то, что подтверждали. "
                              "Подтвердить заново: kb:verify --refresh; "
                              "или понизить статус до draft")
        if status == "deprecated" and not fm.get("superseded_by"):
            errors.append(f"{rel}: deprecated без superseded_by")

        # applies_to без реестра релизов — мёртвая разметка: фильтр по релизу в
        # context pack молча выключается, и карточка чужого релиза уходит как факт
        applies = (fm.get("applies_to") or "").strip("[] ")
        if applies:
            if not releases:
                errors.append(f"{rel}: applies_to задан, но нет "
                              f"AuroraKnowledgeDB/meta/releases.md — фильтр по релизу не работает")
            else:
                unknown = [r.strip().strip('"\'') for r in applies.split(",")
                           if r.strip().strip('"\'') and r.strip().strip('"\'').rstrip("+") not in releases]
                if unknown:
                    errors.append(f"{rel}: applies_to ссылается на релизы вне реестра: "
                                  f"{', '.join(unknown[:3])}")

        # карточки вопросов: ответ обязан иметь дату и источник, открытый — срок и адресата
        if fm.get("type") == "question" or fm.get("q_id"):
            qs = fm.get("q_status", "")
            if not qs:
                errors.append(f"{rel}: карточка вопроса без q_status")
            if qs == "answered" and not (fm.get("answered") and fm.get("answer_source")):
                errors.append(f"{rel}: q_status answered без answered/answer_source "
                              f"(ответ «на словах» не доказательство)")
            if qs in ("open", "asked") and not fm.get("blocks", "").strip("[] "):
                errors.append(f"{rel}: открытый вопрос без blocks — непонятно, что он держит")
            if qs == "asked" and not fm.get("asked"):
                errors.append(f"{rel}: q_status asked без даты asked")

        for target in link_refs(text):
            # ссылки с путём внутри (Concepts/_index) исторически не проверяются:
            # они указывают на служебные индексы, а не на карточки
            if target.startswith("http") or "/" in target:
                continue
            base = target.split("#")[0].strip()
            if base and base not in resolvable:
                errors.append(f"{rel}: битая ссылка [[{target}]]")

    for a, a1, a2 in dup_aliases:
        errors.append(f"дубликат alias «{a}»: {a1} и {a2}")

    # orphan binaries in _assets
    assets = os.path.join(ROOT, "_assets")
    if os.path.isdir(assets):
        referenced = set()
        for _, (_, text) in cards.items():
            for target in link_refs(text):
                referenced.add(os.path.basename(target.split("#")[0].strip()))
        for f in os.listdir(assets):
            if f.startswith(".") or f.endswith(".md"):
                continue
            if f not in referenced and os.path.splitext(f)[0] not in referenced:
                errors.append(f"{assets}/{f}: бинарник без карточки-обёртки")

    # secrets accidentally committed (WARN → treated as errors for CI)
    secret_re = [
        re.compile(r"(?i)ATLASSIAN_API_TOKEN\s*=\s*\S+"),
        re.compile(r"(?i)password\s*[:=]\s*[^\s#]+"),
    ]
    for scan_root in (".opencode/skills", ".cursor/rules"):
        if not os.path.isdir(scan_root):
            continue
        for dirpath, _, files in os.walk(scan_root):
            for f in files:
                if not f.endswith((".md", ".json", ".mdc", ".yml", ".yaml")):
                    continue
                rel = os.path.join(dirpath, f).replace("\\", "/")
                try:
                    t = open(rel, encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue
                for rx in secret_re:
                    if rx.search(t):
                        errors.append(f"{rel}: возможный секрет в git (уберите токен/пароль)")
                        break
    if os.path.isfile("aurora.config.yaml"):
        t = open("aurora.config.yaml", encoding="utf-8", errors="ignore").read()
        for rx in secret_re:
            if rx.search(t):
                errors.append("aurora.config.yaml: возможный секрет (токены только в .env.aurora.local)")
                break

    n = len(cards)
    print(f"kb_lint: карточек {n}, ошибок {len(errors)}")
    if summary or not errors:
        return 1 if errors else 0

    # Список из сотен строк подряд читать нечем: одинаковые беды идут вперемешку, и
    # непонятно, это одна причина или двести разных. Группируем по виду и говорим,
    # чем каждый вид лечится.
    kinds = [
        ("битая ссылка", "битые ссылки",
         "цели нет в базе. `kb:repair --links` чинит то, что находится по алиасу или "
         "регистру; остальное — либо завести карточку на термин, либо снять ссылку"),
        ("дубликат alias", "одинаковые alias у разных карточек",
         "ссылка по такому имени неоднозначна. `kb:repair --aliases` оставит alias там, "
         "где он совпадает с названием, и снимет у остальных"),
        ("правили после приёмки", "правка после приёмки",
         "текст изменился после verified: `kb:verify --refresh` или понизить до draft"),
        ("verified без", "verified без обязательных полей",
         "приёмка без владельца или срока годности — запустите `kb:verify` заново"),
        ("нет frontmatter", "карточки без шапки",
         "`kb:repair --frontmatter` проставит статус и дату"),
    ]
    shown, rest = set(), []
    for needle, title, cure in kinds:
        hits = [e for e in errors if needle in e]
        if not hits:
            continue
        shown.update(id(e) for e in hits)
        print(f"\n## {title}: {len(hits)}")
        print(f"   {cure}")
        for e in hits[:8]:
            print(f"   - {e}")
        if len(hits) > 8:
            print(f"   … ещё {len(hits) - 8}")
    rest = [e for e in errors if id(e) not in shown]
    if rest:
        print(f"\n## прочее: {len(rest)}")
        for e in rest[:20]:
            print(f"   - {e}")
        if len(rest) > 20:
            print(f"   … ещё {len(rest) - 20}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
