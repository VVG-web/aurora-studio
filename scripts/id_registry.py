#!/usr/bin/env python3
"""id_registry.py — реестр идентификаторов проекта: выдача номеров под замком и проверка повторов.

Номера REQ, SPEC, Q и DR назначались «следующим свободным по _index.md» — моделью или рукой.
Два аналитика в двух ветках или два параллельных прогона ассистента получали один номер, а
линтер уникальность не проверял. У артефактов, которые производит агент (`agent:make`),
идентификатора не было вовсе: связь «история → критерии → тест» держалась на имени файла.

Здесь номер выдаёт код:

  python3 .aurora/scripts/id_registry.py DR            # выдать следующий номер: DR-0012
  python3 .aurora/scripts/id_registry.py DR --peek     # показать, не занимая
  python3 .aurora/scripts/id_registry.py --check       # повторы идентификаторов в проекте
  python3 .aurora/scripts/id_registry.py --list        # пространства имён

Пространства: `REQ` и `SPEC` (три цифры), `Q` (три), `DR` (четыре) — карточки базы, номер стоит в
имени файла; `ART` (четыре) — артефакт агента, номер в поле `artifact_id` его шапки. Номер выдаётся
под замком `.aurora/state/ids/<пространство>.lock`: максимум из имён и шапок проекта (карточки в
`_archive` тоже занимают номер) и записанной отметки `.aurora/state/ids/<пространство>.json`. Отметка
нужна, чтобы выданный, но ещё не записанный номер не достался второму, а номер удалённой карточки
не вернулся. Живёт она вне git: в чистом клоне номер считается по файлам проекта. Два клона одного
репозитория могут выдать один номер — такой повтор находит `--check` (и `kb:lint`), а не замок.

Выданный номер считается занятым, даже если им не воспользовались: пропуск в нумерации — не ошибка.

Панель: `kb:next-id`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations
import aurora_common  # noqa: F401 — защита версии Python: на старом — понятный текст, а не traceback

import argparse
import json
import os
import re
import sys
import time
for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на эмодзи и «—» (UnicodeEncodeError)
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from aurora_common import KB_ROOT, lock_release, lock_take, safe_walk, utc_stamp  # noqa: E402

STATE = ".aurora/state/ids"
LOCK_WAIT_S = 15         # сколько ждать чужую выдачу: она длится миллисекунды
LOCK_STALE_S = 60        # замок старше — держатель завис, замок снимается (и если pid жив)
HEAD_CHARS = 6000        # сколько знаков шапки артефакта читаем в поисках артефактного номера

# Пространство → префикс, число цифр, подпись и где искать занятые номера. У карточек номер стоит в
# имени файла (`DR-0007-выбор-шины.md`), у артефакта — в поле шапки (`header`).
SPACES = {
    "REQ": {"prefix": "REQ", "width": 3, "title": "требование",
            "dirs": (f"{KB_ROOT}/Requirements",)},
    "SPEC": {"prefix": "SPEC", "width": 3, "title": "спецификация",
             "dirs": (f"{KB_ROOT}/Specs",)},
    "Q": {"prefix": "Q", "width": 3, "title": "вопрос",
          "dirs": (f"{KB_ROOT}/Questions", "Raw/questions")},
    "DR": {"prefix": "DR", "width": 4, "title": "решение",
           "dirs": (f"{KB_ROOT}/Decisions", "Raw/decisions")},
    "ART": {"prefix": "ART", "width": 4, "title": "артефакт агента", "header": "artifact_id",
            "dirs": ("Artifacts", "Deliverables")},
}
ARCHIVE = f"{KB_ROOT}/_archive"      # архив всей базы: в нём лежат и прежние REQ, Q, DR
# Снимки и архив поставок хранят копию шапки сданного документа: номер они занимают (чужой артефакт не
# получит его), но «вторым файлом с тем же номером» не считаются.
SNAPSHOT_DIRS = ("Deliverables/released/", "Deliverables/_archive/")


class IdBusy(RuntimeError):
    """Замок выдачи занят дольше `LOCK_WAIT_S`: чужая выдача не закончилась."""


def _ns(name: str) -> str:
    """Каноничное имя пространства: без пробелов, в верхнем регистре."""
    return (name or "").strip().upper()


def space_of(name: str) -> dict:
    """Описание пространства по имени (`dr`, `DR`); неизвестное — ValueError со списком."""
    key = _ns(name)
    if key not in SPACES:
        raise ValueError(f"пространства «{name}» нет; есть: {', '.join(SPACES)}")
    return SPACES[key]


def format_id(ns: str, number: int) -> str:
    spec = space_of(ns)
    return f"{spec['prefix']}-{number:0{spec['width']}d}"


def _md_files(root: str, rel: str):
    base = os.path.join(root, rel)
    if not os.path.isdir(base):
        return
    for dirpath, dirs, files in safe_walk(base):
        dirs.sort()
        for f in sorted(files):
            if f.endswith(".md") and f != "README.md" and not f.startswith("_index"):
                yield os.path.join(dirpath, f)


def _kind_dirs(root: str) -> list:
    """Папки результата объявленных видов артефактов: вид может класть файлы и вне `Artifacts/`."""
    try:
        import make_kinds as MK
        return [rec.get("out") for rec in (MK.read_kinds(root) or {}).values()
                if rec.get("out") and not MK.out_problem(rec["out"])]
    except Exception:  # noqa: BLE001 — реестр видов необязателен: без него ищем в умолчаниях
        return []


def _session_ids(root: str, key: str) -> list:
    """Номера, которые занимают сессии производства: файл артефакта в них ещё может не лежать."""
    out = []
    base = os.path.join(root, "Workspaces")
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        path = os.path.join(base, name, "session.json")
        try:
            with open(path, encoding="utf-8") as f:
                value = (json.load(f) or {}).get(key)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(value, str) and value:
            out.append((value, os.path.relpath(path, root).replace("\\", "/")))
    return out


def _head_value(head: str, key: str) -> str:
    """Значение поля шапки по первым знакам файла. Шапку, не закрытую в окне чтения (сотни ссылок в
    `based_on`), это не мешает: закрывающая черта не нужна; BOM в начале терпится."""
    head = head.lstrip("\ufeff")
    if not head.startswith("---"):
        return ""
    end = head.find("\n---", 3)
    m = re.search(rf"(?m)^{re.escape(key)}:[ \t]*[\"']?([^\s\"']+)", head[3:end] if end != -1 else head[3:])
    return m.group(1) if m else ""


def occupied(root: str, ns: str, sessions: bool = True) -> dict:
    """{номер: [опоры]} — где пространство уже занято. Опора — путь файла без расширения (карточки)
    или путь файла с полем (артефакты): по числу разных опор у номера находят повтор. `sessions` —
    считать ли номера сессий производства (нужно выдаче, поиску повторов не нужно)."""
    spec = space_of(ns)
    # Номер — до девяти цифр и не начало даты или дробного номера (`DR-2026-10-19`, `Q-1.1`): такие имена
    # не выдача этого реестра и не должны ни сдвигать нумерацию, ни давать ложный повтор.
    pat = re.compile(rf"^{re.escape(spec['prefix'])}-(\d{{1,9}})(?!\d)(?!\.\d)(?!-\d\d-\d\d)", re.I)
    found: dict = {}
    if "header" in spec:
        key = spec["header"]
        id_re = re.compile(rf"^{re.escape(spec['prefix'])}-(\d{{1,9}})$", re.I)
        folders = list(spec["dirs"]) + [d for d in _kind_dirs(root) if d not in spec["dirs"]]
        seen = set()
        for rel in folders:
            for path in _md_files(root, rel):
                real = os.path.realpath(path)
                if real in seen:
                    continue
                seen.add(real)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        head = f.read(HEAD_CHARS)
                except OSError:
                    continue
                m = id_re.match(_head_value(head, key))
                if m:
                    shown = os.path.relpath(path, root).replace("\\", "/")
                    found.setdefault(int(m.group(1)), [])
                    if not shown.startswith(SNAPSHOT_DIRS):
                        found[int(m.group(1))].append(shown)
        for value, rel in (_session_ids(root, key) if sessions else []):
            m = id_re.match(value.strip())
            if m:
                found.setdefault(int(m.group(1)), [])        # занято, но файла ещё нет — не повтор
        return found
    for rel in list(spec["dirs"]) + [ARCHIVE]:
        for path in _md_files(root, rel):
            m = pat.match(os.path.basename(path))
            if m:
                stem = os.path.splitext(os.path.relpath(path, root))[0].replace("\\", "/")
                found.setdefault(int(m.group(1)), []).append(stem)
    return found


def _ledger_path(root: str, ns: str) -> str:
    return os.path.join(root, STATE, f"{_ns(ns)}.json")


def _ledger_last(root: str, ns: str) -> int:
    try:
        with open(_ledger_path(root, ns), encoding="utf-8") as f:
            return max(0, int((json.load(f) or {}).get("last") or 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def _ledger_write(root: str, ns: str, number: int) -> None:
    path = _ledger_path(root, ns)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last": number, "at": utc_stamp()}, f)
    for attempt in range(6):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # Windows: отметку в эту минуту читает чужой `--peek`
            if attempt == 5:
                raise
            time.sleep(0.02)


def next_number(root: str, ns: str) -> int:
    """Следующий свободный номер, ничего не занимая: максимум из файлов, сессий и отметки, плюс один."""
    return max(max(occupied(root, ns), default=0), _ledger_last(root, ns)) + 1


def peek(ns: str, root: str = ".") -> str:
    return format_id(ns, next_number(root, ns))


def allocate(ns: str, root: str = ".") -> str:
    """Выдать следующий номер пространства: он записывается в отметку до отпускания замка, поэтому
    второй вызов (другой процесс, другой поток) получит следующий."""
    space_of(ns)
    ns = _ns(ns)
    lock = os.path.join(root, STATE, f"{ns}.lock")
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        got, _held = lock_take(lock, {"task": f"id {ns}", "at": time.time()}, stale_s=LOCK_STALE_S)
        if got:
            break
        if time.monotonic() > deadline:
            raise IdBusy(f"выдача номеров {ns} занята другим процессом дольше {LOCK_WAIT_S} с")
        time.sleep(0.03)
    try:
        number = next_number(root, ns)
        _ledger_write(root, ns, number)
        return format_id(ns, number)
    finally:
        lock_release(lock)


def duplicates(root: str = ".", spaces=None) -> dict:
    """{идентификатор: [опоры]} — номера, которые носят два разных файла. Одна и та же карточка в
    папке и в архиве (то же имя файла) повтором не считается."""
    out: dict = {}
    for ns in (spaces or SPACES):
        by_name = "header" not in SPACES[ns]
        for number, where in sorted(occupied(root, ns, sessions=False).items()):
            unique = sorted(dict.fromkeys(where))
            # Карточка: тот же файл в папке раздела и в архиве — одна карточка (то же имя), а не повтор.
            # Артефакт: два пути — два файла, даже если имена совпадают.
            if len({os.path.basename(w) for w in unique} if by_name else unique) > 1:
                out[format_id(ns, number)] = unique
    return out


# ------------------------------------------------------------------ ключ Requirement Yogi: маска вида (BL-KBA-17)
#
# Реестр идентификаторов и связей аналитика — Requirement Yogi: ключ `RU.PRJ.US-1.1.1` заводит он, не Аврора.
# Аврора ключ не выдаёт и не разбирает формат «внутри»: она хранит его непрозрачным значением (`ref_key` в
# паспорте артефакта, `ref_scheme: ry`) и проверяет только одно — что ключ написан по маске вида, объявленной в
# настройках проекта (`artifacts: <вид>: ref_mask`). Смена системы реестра меняет схему, а не карточки.
#
# Язык маски: `<имя>` — любой сегмент из букв, цифр и «_» (страна, проект); одиночная `x` — число; остальное —
# буквально. Пример: `<СТРАНА>.<ПРОЕКТ>.US-x.x.x` читает `RU.PRJ.US-1.1.1`, но не `RU.PRJ.US-1.1`.

MASK_TOKEN = re.compile(r"(<[^<>\s]+>|(?<![A-Za-z0-9_])x(?![A-Za-z0-9_]))")
KEY_BEFORE = r"(?<![A-Za-z0-9_.\-])"
KEY_AFTER = r"(?![A-Za-z0-9_]|[.\-][0-9A-Za-z])"


def mask_problem(mask: str) -> str:
    """Что не так с маской; пусто — годится (или маски нет вовсе)."""
    mask = (mask or "").strip()
    if not mask:
        return ""
    if len(mask) > 100 or re.search(r"\s", mask) or '"' in mask:
        return "маска — одна строка до 100 знаков без пробелов и кавычек"
    parts = MASK_TOKEN.split(mask)
    if not any(p == "x" or p.startswith("<") for p in parts):
        return "в маске нет ни числа (x), ни именованной части (<имя>): ей отвечал бы один-единственный ключ"
    if not re.search(r"[A-Za-z0-9]", "".join(p for p in parts if not p.startswith("<") and p != "x")):
        return "в маске нет постоянной части (например, «US-»): по ней нельзя отличить ключи вида"
    return ""


def _mask_pattern(mask: str) -> str:
    out = []
    for part in MASK_TOKEN.split(mask.strip()):
        if part == "x":
            out.append(r"[0-9]+")
        elif part.startswith("<"):
            out.append(r"[A-Za-z0-9_]+")
        else:
            out.append(re.escape(part))
    return "".join(out)


def mask_regex(mask: str):
    """Регулярное выражение маски (ключ целиком, не часть длиннее); None — маски нет или она негодна."""
    if not (mask or "").strip() or mask_problem(mask):
        return None
    return re.compile(KEY_BEFORE + "(?:" + _mask_pattern(mask) + ")" + KEY_AFTER)


def similar_regex(mask: str):
    """Ключи того же вида в любом написании: постоянная часть перед первым числом маски (`US-`), любые
    префиксы и номер из цифр и точек. Нужна, чтобы назвать ключ, похожий на ключ вида, но не по маске."""
    m = re.search(r"([A-Za-z]+)-x(?![A-Za-z0-9_])", (mask or "").strip())
    if not m or mask_problem(mask):
        return None
    return re.compile(KEY_BEFORE + r"(?:[A-Za-z0-9_]+\.)*" + re.escape(m.group(1)) + r"-[0-9]+(?:\.[0-9]+)*"
                      + r"(?![A-Za-z0-9_]|[.\-][0-9A-Za-z])")


def find_keys(text: str, rx) -> list:
    """Ключи по выражению в порядке появления, без повторов."""
    return list(dict.fromkeys(m.group(0) for m in rx.finditer(text or ""))) if rx else []


def own_key(text: str, mask: str) -> str:
    """Ключ самого документа по маске вида: на строке с «Код» (таблица свойств истории), иначе первый."""
    rx = mask_regex(mask)
    if not rx:
        return ""
    for line in (text or "").splitlines():
        if re.search(r"(?i)\bкод\b|\bkey\b", line):
            found = find_keys(line, rx)
            if found:
                return found[0]
    found = find_keys(text, rx)
    return found[0] if found else ""


def kinds_with_masks(root: str = ".") -> dict:
    """{вид: {mask, out}} — виды артефактов с годной маской."""
    try:
        import make_kinds as MK
        kinds = MK.read_kinds(root) or {}
    except Exception:  # noqa: BLE001 — реестр видов необязателен
        return {}
    return {k: {"mask": r["ref_mask"].strip(), "out": r.get("out") or ""} for k, r in kinds.items()
            if (r.get("ref_mask") or "").strip() and not mask_problem(r["ref_mask"])}


def ref_findings(root: str = ".") -> dict:
    """{путь: [замечания]} — ключи, похожие на ключи вида, но не по его маске: в артефактах вида и на страницах
    зеркала, объявляющих ключ (`ry_defines`). Замечание — {id, msg}. Нет масок — пусто."""
    from aurora_common import clean_copy, body as md_body, frontmatter
    out: dict = {}
    masks = kinds_with_masks(root)
    if not masks:
        return out
    for kind, spec in sorted(masks.items()):
        rx, like = mask_regex(spec["mask"]), similar_regex(spec["mask"])
        if not like:
            continue
        for path in _md_files(root, spec["out"]) if spec["out"] else []:
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    text = clean_copy(md_body(f.read()))
            except OSError:
                continue
            bad = [k for k in find_keys(text, like) if not rx.fullmatch(k)]
            if bad:
                out.setdefault(os.path.relpath(path, root).replace("\\", "/"), []).append(
                    {"id": "REF-02", "msg": f"ключ «{bad[0]}»" + (f" и ещё {len(bad) - 1}" if len(bad) > 1 else "")
                     + f" похож на ключ вида «{kind}», но не по маске «{spec['mask']}»"})
    mirror = os.path.join(root, "Sources", "Confluence")
    if os.path.isdir(mirror):
        for dirpath, _dirs, files in os.walk(mirror):
            for f in files:
                if not f.endswith(".md"):
                    continue
                path = os.path.join(dirpath, f)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as fh:
                        head = fh.read(3000)
                except OSError:
                    continue
                defined = [k.strip().strip("\"'") for k in (frontmatter(head).get("ry_defines") or "").strip("[] ").split(",")
                           if k.strip()]
                for kind, spec in sorted(masks.items()):
                    rx, like = mask_regex(spec["mask"]), similar_regex(spec["mask"])
                    bad = [k for k in defined if like and like.fullmatch(k) and not rx.fullmatch(k)]
                    if bad:
                        out.setdefault(os.path.relpath(path, root).replace("\\", "/"), []).append(
                            {"id": "REF-03", "msg": f"страница объявляет ключ «{bad[0]}» не по маске вида «{kind}» («{spec['mask']}»)"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Реестр идентификаторов: выдача номера под замком, повторы")
    ap.add_argument("space", nargs="?", default="", metavar="ПРОСТРАНСТВО",
                    help="REQ, SPEC, Q, DR или ART: выдать следующий номер")
    ap.add_argument("--peek", action="store_true", help="показать следующий номер, не занимая его")
    ap.add_argument("--check", action="store_true",
                    help="найти идентификаторы, которые носят два разных файла (код 1 — повторы есть)")
    ap.add_argument("--list", action="store_true", help="пространства имён и где они ищутся")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    ap.add_argument("--root", default=".", help="корень проекта")
    a = ap.parse_args()

    if a.list:
        rows = [{"space": k, "prefix": v["prefix"], "digits": v["width"], "title": v["title"],
                 "where": v.get("header") or ", ".join(v["dirs"]), "next": peek(k, a.root)}
                for k, v in SPACES.items()]
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            print("| Пространство | Пример | Что это | Где ищется занятое | Следующий |")
            print("|---|---|---|---|---|")
            for r in rows:
                print(f"| `{r['space']}` | {format_id(r['space'], 1)} | {r['title']} | "
                      f"{r['where']} | {r['next']} |")
        return 0

    if a.check:
        dups = duplicates(a.root)
        if a.json:
            print(json.dumps(dups, ensure_ascii=False, indent=1))
        elif dups:
            print(f"# Повторы идентификаторов: {len(dups)}\n")
            for ident, where in dups.items():
                print(f"- `{ident}` носят {len(where)} файла: " + "; ".join(f"`{w}`" for w in where))
            print("\nЗамок не спасает от двух веток git, где выдан один номер: повтор называется "
                  "при слиянии. Один из файлов переименуйте и поправьте ссылки на него.")
        else:
            print("Повторов идентификаторов нет.")
        return 1 if dups else 0

    if not a.space:
        ap.error("нужно пространство (REQ, SPEC, Q, DR, ART), --check или --list")
    try:
        space_of(a.space)
        ident = peek(a.space, a.root) if a.peek else allocate(a.space, a.root)
    except (ValueError, OSError, IdBusy) as e:
        print(f"id_registry: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"id": ident, "reserved": not a.peek}, ensure_ascii=False))
    else:
        print(ident)
    return 0


if __name__ == "__main__":
    sys.exit(main())
