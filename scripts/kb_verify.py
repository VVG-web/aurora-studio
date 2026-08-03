#!/usr/bin/env python3
"""kb_verify.py — пакетный verify-гейт (фреймворк «Аврора»).

Решение «этой карточке верю» принимает человек. Запись решения — механика: проставить
`status`, `owner`, `verified`, `review_by` и проверить, что карточка вообще готова к
верификации. Раньше это делала модель по одному файлу — дорого и с ошибками, поэтому
в живой базе 516 карточек из очереди так и остались непроверенными.

  python3 .opencode/scripts/kb_verify.py Glossary --owner @vadim          # что будет сделано
  python3 .opencode/scripts/kb_verify.py Glossary --owner @vadim --apply
  python3 .opencode/scripts/kb_verify.py AuroraKnowledgeDB/Systems/ГП-3.md --owner @sa --months 6 --apply

Предпроверки (карточка не верифицируется, если):
  • нет frontmatter или нет `source` — нечем подтвердить происхождение;
  • есть битые wiki-ссылки — верифицировать сломанное нельзя;
  • статус уже `verified` (повторно — только с `--refresh`);
  • статус `deprecated` — это история.

Пакетная приёмка по возрасту источника (`--source-older-than 24`) отбирает карточки,
чья страница в источнике не менялась дольше N месяцев: устоявшееся знание не обязано
перечитываться по одной карточке. Возраст — не доказательство правильности, поэтому
основание записывается в саму карточку (`verified_basis`), и отвечает за решение тот, кто
запустил команду. Ошиблись — `git revert`, основание видно в каждой карточке.

Приёмка по статусу задачи (`--by-jira`) опирается на то, как устроена работа: страница
истории привязана к задачам Jira макросом или номером в заголовке, а задача, дошедшая до
разработки и тестирования, уже прошла разбор аналитика и приёмку постановки.
`trust_statuses` из конфига дают `verified`, `assumption_statuses` — `draft` с пометкой
«это ещё предположение».

Задач у истории может быть несколько, и это не помеха: решение выносится, когда все они
говорят одно и то же. Спор — единственное, что останавливает: одна задача закрыта, другая
лежит в бэклоге, и какая из них описывает состояние знания, машине неизвестно. Задача со
статусом вне обоих списков голоса не имеет — она и не «за», и не «против».

`verified` — верхний статус базы. Ступени «canonical со вторым человеком» больше нет:
она была размечена в схеме, но за всё время не использована ни разу ни в одном проекте
(1.10.0). Кто проверил и когда — видно из `owner` и `verified`.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date, timedelta

from aurora_common import (TRUSTED, body_hash, card_body, frontmatter, git_guard,
                           link_targets, set_field, split_frontmatter)

ROOT = "AuroraKnowledgeDB"
TODAY = date.today()


def all_names(root: str) -> set:
    names = set()
    for dirpath, _, files in os.walk(root):
        for f in files:
            if f.endswith(".md"):
                names.add(os.path.splitext(f)[0])
                try:
                    text = open(os.path.join(dirpath, f), encoding="utf-8", errors="ignore").read(2000)
                except Exception:
                    continue
                for a in re.findall(r'"([^"]+)"', re.search(r"aliases:.*", text).group(0)) \
                        if re.search(r"aliases:.*", text) else []:
                    names.add(a)
    return names


def default_owner() -> str:
    """Кто принимает решение — тот, кто запустил команду.

    Требовать `--owner` у каждого запуска незачем: имя уже записано в git проекта, тем же
    подписаны коммиты рядом. Флаг остаётся для случая «принимаю за коллегу».
    """
    try:
        p = subprocess.run(["git", "config", "user.name"], capture_output=True,
                           text=True, timeout=5)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("USER") or ""


def config_statuses(key: str) -> set:
    """Списки статусов из `aurora.config.yaml` (`atlassian.jira.<key>`)."""
    cfg = "aurora.config.yaml"
    if not os.path.isfile(cfg):
        return set()
    m = re.search(rf"^\s*{key}\s*:\s*\[([^\]]*)\]",
                  open(cfg, encoding="utf-8", errors="ignore").read(), re.M)
    return {x.strip().strip("\"'").casefold() for x in m.group(1).split(",")
            if x.strip()} if m else set()


def jira_verdicts(conf_root: str, jira_root: str) -> dict:
    """{путь страницы Confluence: (вердикт, [ключи с этим вердиктом], [статусы], [без голоса])}.

    Задач у истории бывает несколько. Пока они говорят одно и то же, число их значения не
    имеет: три закрытые задачи — то же основание, что одна. Останавливает только спор —
    одна закрыта, другая в бэклоге: какая описывает состояние знания, машине неизвестно.
    Задача со статусом вне обоих списков голоса не имеет и решению не мешает.
    """
    trust = config_statuses("trust_statuses")
    guess = config_statuses("assumption_statuses")
    if not trust and not guess:
        return {}
    import kb_graph as G
    g = G.Graph()
    g.read_confluence(conf_root)
    g.read_jira(jira_root)
    out = {}
    for _num, hub in g.stories().items():
        if not hub["issues"] or not hub["us"]:
            continue
        votes: dict = {}
        mute = []
        for key in hub["issues"]:
            status = (g.issues[key]["status"] or "").strip()
            low = status.casefold()
            verdict = "verified" if low in trust else "draft" if low in guess else ""
            if verdict:
                votes.setdefault(verdict, []).append((key, status))
            else:
                mute.append(f"{key} ({status or 'статус неизвестен'})")
        if len(votes) != 1:
            continue                     # ноль голосов или спор — решает человек
        verdict, pairs = next(iter(votes.items()))
        for rel in hub["us"]:
            out[f"{conf_root}/{rel}"] = (verdict, [k for k, _s in pairs],
                                         [s for _k, s in pairs], mute)
    return out


def source_updated(src: str) -> date | None:
    """Дата последней правки страницы-источника — из шапки зеркала (`updated:`).

    Для Confluence это дата версии страницы в самом Confluence, а не дата выгрузки:
    выгрузка говорит, когда мы посмотрели, источник — когда его меняли в последний раз.
    """
    if not src or not os.path.isfile(src):
        return None
    fm = frontmatter(open(src, encoding="utf-8", errors="ignore").read(4000))
    raw = (fm.get("updated") or "").strip().strip('"')
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def targets(selector: str) -> list:
    """Файл, папка или раздел базы → список карточек.

    Отбор не выходит за пределы базы знаний: `.` — это вся база, а не весь проект.
    Иначе в выборку попадают README из node_modules движка, и отчёт тонет в мусоре.
    """
    if os.path.isfile(selector):
        return [selector]
    if selector in (".", "", ROOT, ROOT + "/"):
        selector = ROOT
    for base in (selector, os.path.join(ROOT, selector)):
        if os.path.isdir(base) and os.path.relpath(base, ROOT).startswith(".."):
            print(f"kb_verify: «{selector}» вне {ROOT}/ — верифицируются только карточки базы",
                  file=sys.stderr)
            return []
        if os.path.isdir(base):
            return sorted(os.path.join(dp, f).replace("\\", "/")
                          for dp, _, fs in os.walk(base) for f in fs
                          if f.endswith(".md") and not f.startswith("_") and f != "index.md")
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="Пакетная верификация карточек базы знаний")
    ap.add_argument("selector", nargs="?", default=ROOT,
                    help="файл, папка или раздел базы (по умолчанию вся база)")
    ap.add_argument("--owner", default="", help="владелец карточек (@имя); по умолчанию — "
                                                "имя из git этого проекта")
    ap.add_argument("--months", type=int, default=3, help="срок годности, месяцев (по умолчанию 3)")
    ap.add_argument("--status", default="verified", choices=["verified"],
                    help="верхний статус базы; других ступеней нет")
    ap.add_argument("--by-jira", action="store_true",
                    help="решение по статусу связанной задачи (списки — в конфиге проекта)")
    ap.add_argument("--source-older-than", type=int, metavar="MONTHS", dest="older",
                    help="только карточки, чей источник не менялся дольше N месяцев")
    ap.add_argument("--refresh", action="store_true", help="обновить уже проверенные (продлить срок)")
    ap.add_argument("--apply", action="store_true",
                    help="записать изменения (иначе dry-run)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="писать по незакоммиченному дереву (откат станет невозможным)")
    a = ap.parse_args()

    if not os.path.isdir(ROOT):
        print(f"kb_verify: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    owner = a.owner.strip() or default_owner()
    if not owner:
        print("kb_verify: не удалось определить владельца. Укажите: --owner @имя",
              file=sys.stderr)
        return 2
    a.owner = owner if owner.startswith("@") else "@" + owner

    files = targets(a.selector)
    if not files:
        print(f"kb_verify: по «{a.selector}» карточек не найдено", file=sys.stderr)
        return 1
    names = all_names(ROOT)
    review_by = (TODAY + timedelta(days=30 * a.months)).isoformat()

    verdicts = {}
    if a.by_jira:
        verdicts = jira_verdicts("Sources/Confluence", "Sources/JIRA")
        if not verdicts:
            print("kb_verify: нечего решать по Jira. Проверьте, что в aurora.config.yaml "
                  "заданы atlassian.jira.trust_statuses / assumption_statuses и что зеркала "
                  "выгружены.", file=sys.stderr)
            return 1
        print(f"Страниц историй, где задачи говорят одно и то же: {len(verdicts)}\n")

    basis = ""
    if a.older:
        cutoff = TODAY - timedelta(days=30 * a.older)
        basis = f"источник не менялся дольше {a.older} мес (пакетная приёмка)"
        print(f"Отбор по возрасту источника: страницы, не менявшиеся с {cutoff.isoformat()}\n")

    ready, skipped = [], []
    for path in files:
        try:
            text = open(path, encoding="utf-8").read()
        except Exception as e:  # noqa: BLE001
            skipped.append((path, f"не читается: {e}"))
            continue
        head, rest = split_frontmatter(text)
        fm = frontmatter(text)
        if head is None:
            skipped.append((path, "нет frontmatter"))
            continue
        status = (fm.get("status") or "").strip()
        if status == "deprecated":
            skipped.append((path, "deprecated — это история"))
            continue
        if status in TRUSTED and not a.refresh and not a.by_jira:
            skipped.append((path, f"уже {status} (продлить: --refresh)"))
            continue
        if not (fm.get("source") or "").strip():
            skipped.append((path, "нет source — происхождение не подтверждено"))
            continue
        broken = [t for t in link_targets(text) if t not in names]
        if broken:
            skipped.append((path, f"битые ссылки: {', '.join(broken[:3])}"))
            continue
        verdict = ""
        if a.by_jira:
            src = (fm.get("source") or "").strip().strip('"')
            hit = verdicts.get(src.replace("\\", "/"))
            if not hit:
                skipped.append((path, "за источником нет истории, чьи задачи говорят "
                                      "одно и то же"))
                continue
            verdict, jkeys, jstatuses, jmute = hit
            said = ", ".join(f"{k} ({s})" for k, s in zip(jkeys, jstatuses))
            aside = f"; без голоса: {', '.join(jmute)}" if jmute else ""
            if verdict == "draft":
                # Не знание, а предположение: работа по задаче ещё не начиналась.
                # Понижаем доверие явно, чтобы карточка не выглядела проверенной.
                new_head = set_field(head, "status", "draft")
                new_head = set_field(new_head, "verified_basis",
                                     f'"{said} — это ещё предположение, а не знание{aside}"')
                new_head = set_field(new_head, "updated", TODAY.isoformat())
                ready.append((path, "---" + new_head + rest))
                continue

        src_date = None
        if a.older:
            src_date = source_updated((fm.get("source") or "").strip().strip('"'))
            if not src_date:
                skipped.append((path, "дата источника неизвестна — возраст не проверить"))
                continue
            if src_date > cutoff:
                skipped.append((path, f"источник менялся {src_date.isoformat()} — свежее порога"))
                continue

        new_head = set_field(head, "status", a.status)
        new_head = set_field(new_head, "owner", f'"{a.owner}"')
        new_head = set_field(new_head, "verified", TODAY.isoformat())
        new_head = set_field(new_head, "review_by", review_by)
        new_head = set_field(new_head, "updated", TODAY.isoformat())
        # Отпечаток тела на момент приёмки. Карточку правят и после неё — это Zettelkasten,
        # а не архив, — но тогда `verified` относится уже не к тому тексту. По отпечатку
        # линтер отличает «дописали» от «подтверждено как есть».
        new_head = set_field(new_head, "verified_hash", body_hash(card_body(text)))
        if verdict == "verified":
            new_head = set_field(new_head, "verified_basis",
                                 f'"{said}: постановка прошла разбор и приёмку{aside}"')
        if basis:
            # основание доверия записывается в карточку: через полгода никто не вспомнит,
            # почему полторы тысячи карточек стали verified в один день
            new_head = set_field(new_head, "verified_basis",
                                 f'"{basis}; правка источника {src_date.isoformat()}"')
        ready.append((path, "---" + new_head + rest))

    print(f"# Verify — {TODAY.isoformat()}\n")
    print(f"Отобрано: {len(files)} · к верификации: {len(ready)} · пропущено: {len(skipped)}")
    print(f"Статус: {a.status} · владелец {a.owner} · годно до {review_by}"
)
    if skipped:
        print("\n## Пропущены (нужен человек)\n")
        for path, why in skipped[:40]:
            print(f"- {path}: {why}")
        if len(skipped) > 40:
            print(f"- … ещё {len(skipped) - 40}")
    if ready:
        print("\n## К верификации\n")
        for path, _ in ready[:40]:
            print(f"- {path}")
        if len(ready) > 40:
            print(f"- … ещё {len(ready) - 40}")

    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0
    if not git_guard(ROOT, a.allow_dirty, "верификация"):
        return 2
    for path, text in ready:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    print(f"\n✅ Верифицировано: {len(ready)}. Проверьте: aurora_stats.py и git diff --stat")
    return 0


if __name__ == "__main__":
    sys.exit(main())
