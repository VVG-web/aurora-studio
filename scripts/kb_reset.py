#!/usr/bin/env python3
"""kb_reset.py — обнулить базу знаний и собрать её заново (фреймворк «Аврора»).

Иногда база расходится с реальностью настолько, что чинить дороже, чем построить заново:
пятьсот карточек с чужой разметкой, сотни двойников, половина без типа.

  python3 .aurora/scripts/kb_reset.py            # что будет удалено (dry-run)
  python3 .aurora/scripts/kb_reset.py --apply    # обнулить базу
  python3 .aurora/scripts/kb_reset.py --drop-unknown --apply   # и карточки неизвестного происхождения

**Что удаляется:** то, что соберётся заново, — карточки с живым `source:` или `built: machine`
(в том числе зеркала записей), оглавления, архив, `meta/`. Карточки неизвестного происхождения
(ни того, ни другого) остаются и называются; `--drop-unknown` сносит и их. Пустые папки
разделов остаются: структура папок — часть движка, а не содержимое базы.

**Чего скрипт не касается:** всего, что лежит за пределами `AuroraKnowledgeDB/` —
`Sources/`, `Raw/`, `Artifacts/`, `Deliverables/`, `Workspaces/`, `Templates/`, `Prompts/`
остаются как были. Внутри базы уцелеет то, что знанием не является и из источников
не выводится:

  .obsidian/                настройки хранилища: вид, плагины, открытые вкладки
  meta/aurora_version.txt   отметка версии движка — по ней панель, `doctor` и `update`
                            понимают, что в проекте установлено
  meta/ask/                 разговоры вкладки «Спросить»: вопросы аналитика и ответы
                            базы, в любом режиме сброса

Заново из источников выведется не всё. `kb:build` читает `Raw/project`, `Raw/customer`, `Raw/brd`, `Raw/srs`,
`Raw/contract`, `Sources/Confluence`, `Sources/JIRA`; законы и контракты, которые цитируют (`Raw/laws`, помеченные
`cited: true`), нарезает `kb:cite`, и единицы в `Documents/` вернутся им. Записи человека — решения, вопросы,
рукописные справочники — лежат мастер-файлами в `Raw/decisions|questions|reference`: рукописного
в базе нет, всё, что не вернётся из источников, лежит в `Raw/` и сбросом не затрагивается.
Зеркала записей в `Decisions/`, `Questions/`, `Reference/` — карточки `built: machine`, и сброс их
сносит вместе с остальными; `kb:record --mirror` (шаг маршрута сразу после сброса) возвращает их
из мастер-файлов. Карточка без `built` и без `sources` в этих разделах — рукописная запись прежних
версий, ждущая переноса (`kb:record --migrate`): она остаётся и называется, как всякая
неопознанная. Правила базы в `meta/` (`conventions.md`, эталон, базовая линия) остаются; карточки,
оглавления и учёт извлечения (`meta/manifest.json`) уходят.

**Восстановление — только из git**: скрипт не работает по незакоммиченному дереву, после
ошибки достаточно `git checkout -- AuroraKnowledgeDB`. Проект без git обязан указать
`--backup`.

Панель: `kb:reset`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (card_sources, engine_dir, frontmatter, git_dirty,  # noqa: E402
                           git_guard, head_text, is_service, root_link_escapes,
                           utc_stamp, write_text_atomic)

ROOT = "AuroraKnowledgeDB"
from aurora_common import utc_slug  # noqa: E402
TODAY = utc_slug("%Y-%m-%d_%H%M")   # отметка копии — в UTC, как у всех записей

# Не знание, а обвязка базы: из источников не выводится, но и содержимым базы не является.
# Версию движка отсюда читают панель, `doctor` и `update`.
KEEP = ("meta/aurora_version.txt",)
# Разговоры «Спросить» — журнал работы аналитика, а не содержимое базы. Лежат в `meta/`,
# и сброс считал их служебными файлами, которые «соберутся заново»: живая пересборка
# снесла девять разговоров за месяц, и вкладка открылась пустой. Пересборка их не вернёт,
# а `--drop-unknown` просит снести карточки неизвестного происхождения, не журнал.
CONVERSATIONS = "meta/ask/"
# Правила и проверки базы: их пишет человек, и `manifest.json` к ним не относится — он
# уходит всегда, иначе `kb:build` считает источники разобранными и план выйдет пустым.
HANDMADE_META = ("conventions.md", "golden_questions.md", "lint_baseline.txt", "conform_state.json")
# Разделы, которые собираются из самой базы: `kb:moc` и `kb:index` вернут их целиком.
GENERATED = ("MOC",)


def regenerated(rel: str, status: str) -> bool:
    """Карта или оглавление: источника не имеет и не должно — собирается из базы."""
    return (status == "index" or rel.split("/")[0] in GENERATED
            or os.path.basename(rel) == "_index.md")


# ----------------------------------------------------------------------------- пересборка идёт (1.189.0)
#
# «Пересобрать базу с нуля» — часы работы, нарезанные оборотами, и до конца она доходит не всегда. До 1.189.0 об этом
# нигде не было записано, и повтор («Пересобрать» ещё раз, возврат расписания к упавшему пункту) начинал со сноса и
# стирал собранное: 21.10.2026 так потеряли недособранные базы трёх проектов. Теперь снос пишет отметку «пересборка
# идёт» и ставит тег на коммит с прежней базой. Повторный снос, пока отметку не сняли, отказывает; маршрут вместо него
# продолжает с цикла (`route_runner`); конец пересборки отмечает `--finish`; прежнюю базу возвращает `--undo`.

STATE_REL = os.path.join("state", "rebuild.json")      # в папке движка проекта: локальная отметка, не часть базы
TAG_PREFIX = "aurora/pre-rebuild-"                      # тег на коммит с базой, какой она была до сноса


def state_path(project: str = ".") -> str:
    return os.path.join(str(project), engine_dir(str(project)), STATE_REL)


def read_state(project: str = ".") -> dict:
    """Отметка пересборки: `{"phase": "building" | "finished" | "undone", "started", "tag", "commit", …}`; нет файла,
    он не читается или не словарь — пусто."""
    try:
        with open(state_path(project), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(project: str, rec: dict) -> None:
    path = state_path(project)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_text_atomic(path, json.dumps(rec, ensure_ascii=False, indent=1) + "\n")


def in_progress(project: str = ".") -> dict:
    """Отметка начатой и не законченной пересборки; пусто — такой нет."""
    rec = read_state(project)
    return rec if rec.get("phase") == "building" else {}


def route_running(project: str = ".") -> dict:
    """Живой держатель замка маршрута проекта (`.aurora/state/route.lock`, 1.188.1) или пусто. Возврат базы и осознанный
    перезапуск сноса при идущем маршруте перемешали бы руки двух писателей; сам шаг сноса внутри маршрута замок держит
    его же маршрут, поэтому проверяют только эти два режима."""
    from aurora_common import lock_holder
    try:
        return lock_holder(os.path.join(str(project), engine_dir(str(project)), "state", "route.lock"))
    except OSError:
        return {}


def _git(args: list, project: str = ".", env=None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300, env=env)


def make_tag(project: str = ".") -> tuple:
    """Тег на коммит с прежней базой. → (имя, короткий хеш, причина отказа). Нет git или ни одного коммита — отказ."""
    head = _git(["rev-parse", "--verify", "-q", "HEAD"], project)
    sha = head.stdout.strip()
    if head.returncode != 0 or not sha:
        return "", "", "в проекте нет коммита, на который можно поставить тег"
    stamp = utc_slug("%Y%m%dT%H%M%S")          # `utc_slug` сам дописывает «Z»
    for n in range(1, 20):
        name = TAG_PREFIX + stamp + ("" if n == 1 else f"-{n}")
        # Занятость имени — по ссылке, а не по тексту отказа git: тот локализован («уже существует»).
        if _git(["rev-parse", "-q", "--verify", "refs/tags/" + name], project).returncode == 0:
            continue
        r = _git(["tag", name, sha], project)
        if r.returncode == 0:
            return name, sha[:7], ""
        return "", "", (r.stderr or r.stdout or "git tag не отработал").strip()[:200]
    return "", "", "не удалось подобрать свободное имя тега"


def tags(project: str = ".") -> list:
    """Теги отката, свежие первыми."""
    r = _git(["tag", "--list", TAG_PREFIX + "*", "--sort=-refname"], project)
    return [x.strip() for x in r.stdout.splitlines() if x.strip()] if r.returncode == 0 else []


def _card_like(path: str) -> bool:
    parts = path.replace("\\", "/").split("/")
    return (path.endswith(".md") and "meta" not in parts[1:-1] and "_archive" not in parts
            and "_assets" not in parts and parts[-1] not in ("_index.md", "README.md"))


def count_cards(project: str = ".", rev: str = "") -> int:
    """Карточек в базе: на диске или (с `rev`) в коммите/теге."""
    if rev:
        r = _git(["ls-tree", "-r", "--name-only", "-z", rev, "--", ROOT], project)
        return sum(1 for p in r.stdout.split("\0") if p and _card_like(p)) if r.returncode == 0 else 0
    n = 0
    for dirpath, dirs, files in os.walk(os.path.join(str(project), ROOT)):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        n += sum(1 for f in files if _card_like(os.path.relpath(os.path.join(dirpath, f), str(project))))
    return n


def known_machine(fm: dict) -> bool:
    """Карточку писала машина и сказала об этом сама.

    Метку `built: machine` ставят все машинные пути: нарезка (`kb:build`), заготовки
    (`kb:repair --stubs`), агент. Проверено на живой пересборке: 965 карточек из 965 —
    с меткой. Метка появилась не сразу, поэтому на старых базах её может не быть — и
    тогда происхождение именно **неизвестно**, а не «рукотворное».
    """
    return (fm.get("built") or "").strip().strip('"') == "machine"


def survives(rel: str, source_alive: bool, machine: bool, status: str,
             drop_unknown: bool) -> str:
    """Почему файл остаётся; пустая строка — не остаётся.

    Решает происхождение, а не имя папки: база разложена по предметам, а авторство лежит
    поперёк предметов, и папка ошибается в обе стороны.

    Но «нет источника» тоже не значит «писал человек» — эту ошибку движок сделал ровно
    в одном релизе. Замер на живом проекте: из 425 карточек без источника 137 создал
    `kb:repair --stubs` заготовками, 64 пришли массовым коммитом, и лишь единицы —
    журнал решений, написанный человеком. Беречь заготовки вредно вдвойне: пересборка
    их не вернёт, а держать в базе пустышки незачем — их и так ловит линтер.

    Отсюда три исхода, а не два: соберётся заново (сносим), сделала машина и сама это
    подтвердила (сносим), происхождение неизвестно (оставляем и **называем**, чтобы
    человек посмотрел, а не узнал случайно).
    """
    if rel in KEEP:
        return "обвязка базы: отметка версии движка"
    if rel.startswith(CONVERSATIONS):
        return "разговоры с базой: вопросы и ответы вкладки «Спросить»"
    if drop_unknown:
        return ""      # просили снести всё — значит и правила базы тоже
    if rel.split("/")[0] == "meta" and os.path.basename(rel) in HANDMADE_META:
        return "правила и проверки базы"
    if source_alive or machine or regenerated(rel, status):
        return ""
    return "происхождение неизвестно: ни источника, ни метки машины"


def scan(drop_unknown: bool) -> tuple:
    """(что удалить, что оставить с причиной, статистика по статусам, потери по разделам).

    Потеря — карточка, которую пересборка не вернёт: нет живого `source:` и нет метки
    `built: machine`. По умолчанию такие остаются; `--drop-unknown` сносит и их.
    """
    drop, keep, statuses, orphan = [], [], {}, {}
    for dirpath, dirs, files in os.walk(ROOT):
        # `.obsidian/` — настройки редактора, а не знание; точечные файлы (`.gitkeep`)
        # держат в git пустые папки разделов, которые остаются после сброса
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in sorted(files):
            if f.startswith("."):
                continue
            path = os.path.join(dirpath, f).replace("\\", "/")
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            st, source_alive, machine = "", True, True
            if f.endswith(".md") and not is_service(path):
                fm = frontmatter(head_text(path))
                st = (fm.get("status") or "без статуса").strip()
                # Источник жив, если жив хоть один из них: карточка накапливает знание
                # из нескольких артефактов, и снос её из-за одного исчезнувшего значил бы
                # потерю остальных четырёх.
                srcs = card_sources(head_text(path))
                source_alive = any(os.path.exists(
                    os.path.join(os.path.dirname(ROOT) or ".", s)) for s in srcs)
                machine = known_machine(fm)
            why = survives(rel, source_alive, machine, st, drop_unknown)
            if why:
                keep.append((path, why))
                continue
            drop.append(path)
            if st:
                statuses[st] = statuses.get(st, 0) + 1
                if not (source_alive or machine or regenerated(rel, st)):
                    orphan[rel.split("/")[0]] = orphan.get(rel.split("/")[0], 0) + 1
    return sorted(drop), sorted(keep), statuses, orphan


def save_snapshot() -> None:
    """Снимок «карточка → источник» перед сносом. Имена карточек агент выберет заново, и `based_on:` артефактов после
    пересборки указывал бы в никуда. Артефакт — сданный документ, и терять его провенанс нельзя даже ради качественной
    базы: по снимку `kit:remap-sources` находит новую карточку по источнику, а не по имени. Пишется ПОСЛЕ охраны git:
    раньше он писался до неё, и закоммиченный снимок прошлой пересборки сам делал дерево «грязным» — охрана отказывала
    из-за файла, который сносу и положен."""
    from aurora_common import walk_md as _walk
    snap = {}
    for path in _walk(ROOT, skip_service=True, skip_archive=True):
        # Снимок держит ВСЕ источники карточки: по нему `kit:remap-sources` находит её после пересборки, и накопленная
        # карточка должна находиться по любому из своих артефактов, а не только по первому.
        srcs = card_sources(open(path, encoding="utf-8", errors="ignore").read())
        if srcs:
            snap[os.path.splitext(os.path.basename(path))[0]] = srcs
    out = os.path.join(ROOT, "meta", "trace")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "rebuild-snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    print(f"Снимок соответствий сохранён: карточек {len(snap)} "
          f"→ {ROOT}/meta/trace/rebuild-snapshot.json\n")


def cmd_status(a) -> int:
    rec = read_state(".")
    live = in_progress(".")
    if a.json:
        print(json.dumps({"active": bool(live), **rec}, ensure_ascii=False, indent=1))
        return 0
    if live:
        print(f"Пересборка идёт с {live.get('started') or '?'}: прежняя база — тег {live.get('tag') or '(без тега)'}, "
              f"карточек было {live.get('cards_before', '?')}, сейчас {count_cards('.')}.")
        print("Продолжить: маршрут «Пересобрать базу с нуля» (снос он пропустит). Вернуть прежнюю базу: "
              "`kb:reset --undo --apply`. Считать законченной: `kb:reset --finish --apply`.")
    else:
        phase = rec.get("phase")
        print("Незаконченной пересборки нет" + (f" (последняя: {phase}, {rec.get('finished') or rec.get('undone') or rec.get('started') or '?'})" if phase else "") + ".")
    return 0


def cmd_finish(a) -> int:
    live = in_progress(".")
    if not live:
        print("Незаконченной пересборки нет — отмечать нечего.")
        return 0
    if not a.apply:
        print(f"(dry-run) Пересборка с {live.get('started') or '?'} будет отмечена законченной. Записать: --apply")
        return 0
    live.update(phase="finished", finished=utc_stamp(), cards_after=count_cards("."))
    write_state(".", live)
    print(f"✅ Пересборка отмечена законченной: карточек было {live.get('cards_before', '?')}, стало {live['cards_after']}. "
          f"Тег {live.get('tag') or '—'} остаётся: вернуть прежнюю базу можно и теперь (`kb:reset --undo --apply`), "
          "а когда он станет не нужен — `git tag -d`.")
    return 0


def cmd_undo(a) -> int:
    """Вернуть базу, какой она была до сноса, из тега. Делает то, что руками делали 21.10.2026: `git restore` каталога
    базы из тега и коммит. Отметку версии движка не трогает: движок с тех пор мог обновиться."""
    rec = read_state(".")
    tag = (a.tag or rec.get("tag") or (tags(".") or [""])[0]).strip()
    if not tag:
        print("kb_reset --undo: тега отката нет (ни в отметке пересборки, ни среди aurora/pre-rebuild-*). Вернуть можно "
              "и вручную: git restore --source=<коммит> --staged --worktree -- AuroraKnowledgeDB", file=sys.stderr)
        return 2
    ok = _git(["rev-parse", "--verify", "-q", "refs/tags/" + tag + "^{commit}"])
    if ok.returncode != 0:
        print(f"kb_reset --undo: тега «{tag}» нет в этом проекте", file=sys.stderr)
        return 2
    sha = ok.stdout.strip()[:7]
    then, now = count_cards(".", tag), count_cards(".")
    print(f"# Возврат базы — {TODAY}\n")
    print(f"Откуда: тег {tag} ({sha}) · карточек там {then}, сейчас {now}.")
    print("Что будет: каталог AuroraKnowledgeDB/ станет точь-в-точь таким, каким был в теге (лишние файлы уйдут), кроме "
          "meta/aurora_version.txt; один коммит. Источники и всё за пределами базы не тронутся.")
    if not a.apply:
        print("\n(dry-run) Ничего не изменено. Вернуть: --apply")
        return 0
    busy = route_running(".")
    if busy:
        print(f"❌ в проекте идёт маршрут «{busy.get('title') or busy.get('route') or '?'}» (с {busy.get('since') or '?'}, "
              f"процесс {busy.get('pid') or '?'}): возврат базы перемешался бы с его записью. Остановите маршрут или "
              "дождитесь конца.", file=sys.stderr)
        return 2
    stamp_file = f"{ROOT}/meta/aurora_version.txt"
    dirty = [x for x in git_dirty(ROOT) if x[3:].strip().replace("\\", "/") != stamp_file]
    staged = [x for x in _git(["diff", "--cached", "--name-only"]).stdout.split() if x]
    if (dirty or staged) and not a.allow_dirty:
        print(f"❌ в {ROOT}/ есть незакоммиченные правки ({len(dirty) + len(staged)}): возврат перезаписал бы их. "
              "Сначала зафиксируйте их или осознанно продолжите: --allow-dirty", file=sys.stderr)
        return 2
    r = _git(["restore", f"--source={tag}", "--staged", "--worktree", "--", ROOT, f":(exclude){stamp_file}"])
    if r.returncode != 0:
        print(f"❌ git restore не отработал: {(r.stderr or r.stdout).strip()[:300]}", file=sys.stderr)
        return 2
    msg = (f"Возврат базы знаний до пересборки с нуля: состояние {sha} (тег {tag}, карточек {then})\n\n"
           f"Пересобранное (карточек {now}) осталось в истории git.")
    c = _git(["commit", "-m", msg], env=dict(os.environ, AURORA_SKIP_RATCHET="1"))
    out = (c.stdout + c.stderr)
    if c.returncode != 0 and "nothing to commit" not in out and "нечего коммитить" not in out:
        print(f"❌ коммит не прошёл: {out.strip()[-400:]}\nФайлы базы уже возвращены и лежат в индексе: "
              "зафиксируйте их сами.", file=sys.stderr)
        return 2
    rec.update(phase="undone", undone=utc_stamp(), restored_from=tag)
    write_state(".", rec)
    print(f"\n✅ База возвращена: карточек {count_cards('.')} (в теге {then})." + (
          " База уже совпадала с тегом — коммит не нужен." if c.returncode != 0 else ""))
    print("Начать пересборку заново можно маршрутом «Пересобрать базу с нуля».")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Обнулить базу знаний и собрать заново")
    ap.add_argument("--apply", action="store_true", help="удалить (иначе dry-run)")
    ap.add_argument("--drop-unknown", action="store_true",
                    help="снести и карточки неизвестного происхождения: пересборка их "
                         "не вернёт, только git")
    ap.add_argument("--keep-handmade", action="store_true",
                    help=argparse.SUPPRESS)   # с 1.92.5 это поведение по умолчанию
    ap.add_argument("--list-unknown", action="store_true",
                    help="перечислить карточки неизвестного происхождения и выйти")
    ap.add_argument("--backup", metavar="DIR",
                    help="сначала скопировать базу целиком в эту папку")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="работать по незакоммиченному дереву (откат станет невозможным)")
    ap.add_argument("--restart", action="store_true",
                    help="снести базу, даже если пересборка не закончена")
    ap.add_argument("--finish", action="store_true",
                    help="отметить пересборку законченной")
    ap.add_argument("--undo", action="store_true",
                    help="вернуть базу, какой она была до сноса")
    ap.add_argument("--tag", metavar="TAG", help="с --undo: из какого тега вернуть")
    ap.add_argument("--status", action="store_true", help="сказать, идёт ли пересборка")
    ap.add_argument("--json", action="store_true", help="с --status: ответ JSON")
    a = ap.parse_args()

    # База — ссылка наружу проекта: обход и удаление пошли бы по чужой папке (git проекта
    # её не видит, git_guard не защитит). Движок такой каталог не читает вовсе, и сносить в
    # нём тоже нечего. Код 2: команда не отработала.
    if root_link_escapes(ROOT):
        print(f"kb_reset: каталог базы {ROOT}/ — ссылка наружу проекта: сброс не выполняется; "
              "положите базу в проект или уберите ссылку", file=sys.stderr)
        return 2

    if a.status:
        return cmd_status(a)
    if a.finish:
        return cmd_finish(a)
    if a.undo:
        return cmd_undo(a)

    if not os.path.isdir(ROOT):
        print(f"kb_reset: нет {ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1

    drop, keep, statuses, orphan = scan(a.drop_unknown)

    if a.list_unknown:
        rows = [os.path.relpath(p, ROOT).replace("\\", "/") for p, why in keep
                if why.startswith("происхождение")]
        print(f"# Карточки неизвестного происхождения — {TODAY}\n")
        print(f"Ни живого `source:`, ни метки `built: machine`. Пересборка их не вернёт,"
              f" но это не значит, что их писал человек: на старых базах метку ещё не"
              f" ставили, и сюда же попадают заготовки `kb:repair --stubs`.\n")
        for rel in rows:
            print(f"  {rel}")
        print(f"\nВсего: {len(rows)}")
        print("Своего здесь нет — снести: `kb:reset --drop-unknown --apply`.")
        return 0
    cards = [p for p in drop if p.endswith(".md")]
    print(f"# Сброс базы знаний — {TODAY}\n")
    print(f"Режим: {'полный, включая неизвестное' if a.drop_unknown else 'всё, что соберётся заново'}")
    print(f"К удалению: {len(drop)} файлов (карточек {len(cards)}) · остаётся: {len(keep)}")
    print("Не тронутся: .obsidian/ (настройки хранилища), meta/aurora_version.txt и "
          "разговоры meta/ask/, "
          "а за пределами базы — ничего: Sources/, Raw/, Artifacts/, Deliverables/, "
          "Workspaces/, Templates/, Prompts/.\n")
    if statuses:
        print("Среди удаляемых карточек:")
        for st, n in sorted(statuses.items(), key=lambda x: -x[1]):
            mark = "  ⚠️ это работа человека" if st == "verified" else ""
            print(f"  {st}: {n}{mark}")
        print()
    if drop:
        by_dir = {}
        for path in drop:
            top = os.path.relpath(path, ROOT).replace("\\", "/").split("/")[0]
            by_dir[top] = by_dir.get(top, 0) + 1
        print("По разделам:")
        for top, n in sorted(by_dir.items(), key=lambda x: -x[1]):
            lost = orphan.get(top, 0)
            mark = (f"  ⚠️ из них {lost} не выведется заново: источника нет"
                    if lost else "")
            print(f"  {top}: {n}{mark}")
        print()
    lost = sum(orphan.values())
    if lost:
        print(f"⚠️  Идут под снос {lost} карточек неизвестного происхождения: ни живого "
              f"`source:`, ни метки `built: machine`. Пересборка их не вернёт — только "
              f"git. Вы попросили это флагом `--drop-unknown`.\n")
    if not a.drop_unknown:
        # обвязку базы уже назвали выше — здесь только рукотворное
        seen = {}
        for path, why in keep:
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            if rel not in KEEP and not rel.startswith(CONVERSATIONS):
                seen[why] = seen.get(why, 0) + 1
        if seen:
            print("Остаётся — пересборка это не вернёт:")
            for why, n in sorted(seen.items()):
                print(f"  {why}: {n}")
            unknown = sum(n for why, n in seen.items() if why.startswith("происхождение"))
            if unknown:
                print(f"\n«Происхождение неизвестно» — это НЕ обязательно ваша работа. На"
                      f" старых базах метку `built: machine` ещё не ставили, и под этот"
                      f" счёт попадают заготовки `kb:repair --stubs` и всё, что пришло"
                      f" массовыми коммитами. Посмотрите список глазами: если своего там"
                      f" нет, снесите их `--drop-unknown` — база станет чище.")
                print(f"Показать поимённо: `kb:reset --list-unknown`.")
            print()
    if not drop:
        print("✅ Удалять нечего — база уже пуста.")
        return 0

    live = in_progress(".")
    # Отказ нужен, когда снос уничтожил бы СОБРАННОЕ пересборкой (машинные карточки), а не оставшееся от прежней базы:
    # второй снос с `--drop-unknown` по недобору неопознанных карточек собранного не трогает.
    rebuilt = sum(1 for p in cards if known_machine(frontmatter(head_text(p)))) if live else 0
    if live and rebuilt:
        print(f"⚠️  Пересборка уже идёт с {live.get('started') or '?'} (прежняя база — тег {live.get('tag') or '—'}): "
              "снос сейчас сотрёт собранное. Продолжить её — маршрут «Пересобрать базу с нуля» (снос пропустит); "
              "вернуть прежнюю базу — `kb:reset --undo --apply`; всё равно начать заново — `--restart`.\n")
    if not a.apply:
        print("(dry-run) Ничего не удалено. Обнулить: --apply")
        if not a.drop_unknown:
            print("Снести вообще всё, включая неизвестное: --drop-unknown")
        print("\nПосле сброса: `kb:build` → задание ассистенту на партию → `kb:links --cards`.")
        return 0

    if live and rebuilt and not a.restart:
        print("❌ снос отказан: прежняя пересборка не закончена, и он стёр бы собранное. Продолжить её — маршрут "
              "«Пересобрать базу с нуля»; вернуть прежнюю базу — `kb:reset --undo --apply`; начать заново осознанно — "
              "`--restart`.", file=sys.stderr)
        return 2
    busy = route_running(".") if a.restart else {}
    if busy:
        print(f"❌ в проекте идёт маршрут «{busy.get('title') or busy.get('route') or '?'}»: снос с --restart "
              "перемешался бы с его записью. Остановите маршрут или дождитесь конца.", file=sys.stderr)
        return 2
    tag, tag_sha = "", ""
    if a.backup:
        dest = os.path.join(a.backup, f"AuroraKnowledgeDB_{TODAY}")
        shutil.copytree(ROOT, dest)
        print(f"Копия базы: {dest}")
    elif not git_guard(ROOT, a.allow_dirty, "сброс базы знаний"):
        print("Проект без git? Тогда нужна копия: --backup <папка>", file=sys.stderr)
        # Код 2, а не 1: команда НЕ отработала. Единица означает «нашла, что чинить», и
        # маршрут после неё идёт дальше — а дальше идти некуда, база не сброшена, и
        # следующие шаги отработают по нетронутой базе, отрапортовав успехом.
        return 2
    else:
        # Прежняя база — под тег: вернуть её одной командой (`--undo`), а не искать коммит по истории.
        if _git(["rev-parse", "--verify", "-q", "HEAD"]).returncode != 0:
            # Нет git или в нём ни одного коммита: охрана выше такое дерево пропускает, возвращать нечего и некуда.
            print("⚠️  Точки возврата нет: в проекте нет git или ни одного коммита. Вернуть прежнюю базу будет нечем — "
                  "для этого нужна копия: --backup <папка>.\n")
        else:
            tag, tag_sha, why = make_tag(".")
            if not tag:
                print(f"❌ не поставлен тег отката: {why}. Без точки возврата база не сносится; сделайте копию: "
                      "--backup <папка>", file=sys.stderr)
                return 2
            print(f"Точка возврата: тег {tag} ({tag_sha}) — вернуть прежнюю базу: `kb:reset --undo --apply`.")
    save_snapshot()

    for path in drop:
        try:
            os.remove(path)
        except OSError as e:
            print(f"  ! {path}: {e}", file=sys.stderr)
    # пустые каталоги разделов оставляем: структура папок — часть движка
    for dirpath, dirs, files in os.walk(ROOT, topdown=False):
        if not dirs and not files and os.path.relpath(dirpath, ROOT).count(os.sep) >= 1:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    write_state(".", {"schema": 1, "phase": "building", "started": utc_stamp(), "tag": tag, "commit": tag_sha,
                      "cards_before": len(cards), "files_removed": len(drop), "restart": bool(live),
                      "backup": a.backup or ""})
    print(f"\n✅ База обнулена: удалено файлов {len(drop)}. За пределами {ROOT}/ не тронуто "
          "ничего — Sources/, Raw/, Artifacts/, Deliverables/, Workspaces/, Templates/, "
          "Prompts/ на месте.")
    print("Дальше:")
    if a.drop_unknown:
        print("  0. правила базы (meta/conventions.md, meta/golden_questions.md) из источников")
        print("     не вернутся — возьмите их из git или из шаблонов kit'а")
    print("  1. `kb:build`                    — план: партии и готовое задание ассистенту")
    print("  2. кнопка «Партия 1» под консолью — задание уходит в буфер, вставьте его в чат")
    print("  3. `kb:links` с флагами --cards --apply — связи между карточками")
    print("  4. `kb:index`                    — оглавления разделов")
    return 0


if __name__ == "__main__":
    sys.exit(main())
