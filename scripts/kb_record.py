#!/usr/bin/env python3
"""kb_record.py — записи в `Raw/`: решения, вопросы, рукописные справочники (фреймворк «Аврора»).

Зачем. База знаний — производная: сброс и пересборка возвращают её из `Sources/` и `Raw/`. Всё, что пишет человек и
чего нет ни в одном источнике, — решение с вариантами, вопрос заказчику, справочник, который ведут руками, — раньше жило
в самой базе (`Decisions/`, `Questions/`, `Reference/`) и после сброса не возвращалось ниоткуда. Теперь эти записи лежат
физически в `Raw/` (`Raw/decisions/`, `Raw/questions/`, `Raw/reference/`; исправления — по-прежнему `Raw/corrections/`),
а в базе остаётся дословное зеркало, которое строит движок. Мастер-файл — единственное место правды: зеркало
пересоздаваемо, правят мастер.

  python3 .aurora/scripts/kb_record.py --new decision "Срок подтверждения заявки"   # заготовка DR-NNNN-….md в Raw/decisions/
  python3 .aurora/scripts/kb_record.py --new question "Максимум вложений" --blocks "[[Заявка на поставку]]"
  python3 .aurora/scripts/kb_record.py --new reference "Коды причин отказа"          # Raw/reference/<имя>.md
  python3 .aurora/scripts/kb_record.py --list                    # сколько записей по типам и состояниям
  python3 .aurora/scripts/kb_record.py --check                   # мастер-файлы и зеркала: шапка, папка, номера, поля цикла
  python3 .aurora/scripts/kb_record.py --mirror                  # зеркала: что будет создано, обновлено, снято в архив
  python3 .aurora/scripts/kb_record.py --mirror --apply          # построить и обновить зеркала в AuroraKnowledgeDB/
  python3 .aurora/scripts/kb_record.py --migrate                 # рукописные записи из Decisions/Questions/Reference: план переноса
  python3 .aurora/scripts/kb_record.py --migrate --apply         # перенести в Raw/ (`git mv`) и построить зеркала

Номер новой записи выдаёт реестр идентификаторов (`id_registry`): максимум среди мастер-файлов, зеркал и архива, плюс один.
Имя файла — `DR-NNNN-<суть>.md` и `Q-NNN-<суть>.md` по правилам нормализации имён (кириллица сохраняется).

Зеркало. Тип берётся из поля `type` мастера, а без него — из папки (`Raw/decisions` → `decision`); папка, не совпавшая с
домом типа, — ошибка записи (V-17). Шапка зеркала собирается конструктором шапки (`card_types.render_header`) из полей,
которые копируются из мастера, и полей движка (`kind`, `sources`, `source_synced`, `built: machine`; у решения и вопроса
статус и вид заданы типом, у справочника статус пишет `kb:trust`). Остальные поля мастера в зеркало не попадают. Тело
зеркала — тело мастера побайтно, пропущенное через `quote_safe` (служебные заголовки хвоста экранируются). Имя файла
зеркала равно имени мастера; если в базе под этим именем уже лежит не зеркало, зеркало не создаётся — находка V-REC-1.
Мастер, в котором остались заготовки `{{…}}`, зеркалом не становится: он ещё не готов. Исчезнувший мастер: зеркало уходит
в `_archive/` с записью в «История изменений». Запись идёт через единый вход записи карточек (`op=mirror`): не прошедшее
шаблон зеркало не пишется. Повторный прогон ничего не меняет (дата сверки с источником в сравнении не участвует).

Перенос рукописного (`--migrate`). Кандидаты — карточки `Decisions/`, `Questions/`, `Reference/` без `built: machine` и без
живого `sources`. Шапка мастера собирается из прежней теми же правилами, что и зеркало (поля движка отбрасываются), тело
переносится побайтно, файл — `git mv` (история сохраняется), затем строятся зеркала. Занятый целевой путь — отказ по этой
карточке, остальные идут дальше. Без git перенос не выполняется.

Сухой прогон по умолчанию; запись — с `--apply`; по незакоммиченному дереву — только с `--allow-dirty`. Команда не делает
коммитов и не правит существующие мастер-файлы: она создаёт новые файлы по просьбе человека и строит производные.

Коды находок `--check` и отчёта `--mirror`: V-01 шапка не разбирается, V-13 в мастере остались заготовки `{{…}}`, V-17 нет
полей цикла, запись лежит в чужой папке, имя не по образцу или номер занят дважды, V-REC-1 имя занято не зеркалом (или `DR-…`/`Q-…`
носит карточка другого типа), V-REC-2 зеркала нет, оно отстало от мастера или осталось без мастера — лечится `--mirror --apply`;
остальные — коды шаблона карточки (`kb:cards`), которые зеркало не прошло бы.

Код возврата: 0 — ошибок нет и делать нечего, 1 — есть что делать или найдены ошибки, 2 — сбой.

Панель: `kb:record`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import (  # noqa: E402
    KB_ROOT, NAME_BYTES, OWN_LIFECYCLE, TODAY, body, card_filename, card_sources, frontmatter, git_guard, head_blank,
    head_cut_comment, is_service, list_field, quote_safe, read_card_text, safe_walk, split_frontmatter, utc_slug,
)
import aurora_fs  # noqa: E402 — атомарная запись: перевод строки нового файла — `\n`, прежний `\r\n` сохраняется
import card_types as CT  # noqa: E402 — реестр, шаблоны, конструктор шапки, единый вход записи

RAW = "Raw"
RECORD_TYPES = ("decision", "question", "reference")      # типы, у которых в `Raw/` мастер-файл, а в базе зеркало
CORRECTIONS_HOME = "Raw/corrections"
# Прежние значения, если типа нет в реестре проекта (повреждённая установка): дом, раздел базы, префикс номера.
FALLBACK = {"decision": ("Raw/decisions", "Decisions", "DR-"),
            "question": ("Raw/questions", "Questions", "Q-"),
            "reference": ("Raw/reference", "Reference", "")}
SPACE = {"decision": "DR", "question": "Q"}                # пространства реестра идентификаторов (`id_registry`)

# Поля мастера, которые копируются в зеркало (§11.4 плана), по типам; порядок в шапке задаёт конструктор. К решению добавлен
# `accepted_fp` — отпечаток тела принятого решения (`kb:decide-accept`): без него линтер перестал бы видеть правку тела зеркала.
COPY = {
    "decision": ("title", "aliases", "tags", "template", "status", "date", "owner", "supersedes", "superseded_by",
                 "applies_to", "accepted_fp", "created", "updated"),
    "question": ("title", "aliases", "tags", "template", "q_id", "q_status", "owner", "asked_to", "channel", "asked", "due",
                 "answered", "answer_source", "blocks", "raised_by", "fingerprint", "created", "updated"),
    "reference": ("title", "aliases", "tags", "template", "kind", "summary", "owner", "created", "updated"),
}
QUOTE_ALWAYS = frozenset(("owner", "asked_to", "channel", "answer_source", "raised_by"))   # значения со словами и «@»: в кавычках
# Поля зеркала, принадлежащие другим проходам: при пересборке зеркала они переносятся из прежнего.
CARRY = ("related", "trust", "trust_basis", "trust_checked", "corrected_by", "correction_retired")
# Что сравнивается при вопросе «изменилось ли зеркало»: даты сверки и правки в сравнении не участвуют.
SKIP_COMPARE = frozenset(("source_synced", "updated"))

PLACEHOLDER_RE = re.compile(r"\{\{[^{}\n]{1,300}\}\}")
COMMENT_RE = re.compile(r"<!--.*?-->[ \t]*\n?", re.S)
NUMBERED_RE = {"decision": re.compile(r"^DR-(\d{1,9})(?![\d])", re.I), "question": re.compile(r"^Q-(\d{1,9})(?![\d])", re.I)}
RESERVED_RE = re.compile(r"^(?:DR|Q)-\d{1,9}(?!\d)(?!\.\d)(?!-\d\d-\d\d)", re.I)    # имена, занятые под записи (V-REC-1)
SUFFIX_MAX = 60                                              # знаков «сути» в имени и заголовке записи

# Уровни находок.
ERR, WARN, INFO = "error", "warn", "info"


class RecordError(Exception):
    """Запись не создана или не перенесена по понятной причине (сообщение — человеку)."""


# ----------------------------------------------------------------------------- типы и папки

@dataclass(frozen=True)
class Spec:
    """Тип записи: где лежит мастер, в каком разделе базы зеркало, с какого префикса идёт номер."""
    tid: str
    home: str
    section: str
    prefix: str
    ct: object = None

    @property
    def mirror_dir(self) -> str:
        return f"{KB_ROOT}/{self.section}"


def specs(reg) -> dict:
    """{тип: Spec} для типов записей. Берётся из паспорта шаблона; нет шаблона — прежние значения."""
    out = {}
    for tid, (home, section, prefix) in FALLBACK.items():
        ct = reg.get(tid)
        out[tid] = Spec(tid, (ct.home if ct and ct.home else home).strip("/"), (ct.section if ct and ct.section else section),
                        (ct.id_prefix if ct and ct.id_prefix else prefix), ct)
    return out


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_text(path: str) -> "str | None":
    text = read_card_text(path)
    return None if text is None else _norm(text)


def rel_of(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/")


def list_masters(root: str, spec: Spec) -> list:
    """Мастер-файлы типа: пути от корня проекта, по порядку. Описание папки и служебные файлы не в счёт."""
    base = os.path.join(root, spec.home)
    out = []
    if not os.path.isdir(base):
        return out
    for dirpath, dirs, files in safe_walk(base):
        dirs.sort()
        for f in sorted(files):
            if not f.endswith(".md") or f == "README.md" or f.startswith("_"):
                continue
            out.append(rel_of(root, os.path.join(dirpath, f)))
    return sorted(out)


def number_of(spec: Spec, name: str) -> int:
    """Номер записи из имени файла (`DR-0007-…` → 7); -1 — имя не по образцу или у типа нет номеров."""
    rx = NUMBERED_RE.get(spec.tid)
    m = rx.match(name) if rx else None
    return int(m.group(1)) if m else -1


def h1_of(text: str) -> str:
    for line in body(text).split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
        if line.strip() and not line.startswith("<!--"):
            return ""
    return ""


# ----------------------------------------------------------------------------- зеркало: сборка

def _quoted(value: str) -> str:
    return '"' + value.replace('"', "'") + '"'


SIMPLE_FIELDS = frozenset(("status", "q_status", "q_id", "date", "created", "updated", "asked", "due", "answered", "kind"))


def field_raw(fm: dict, key: str) -> str:
    """Значение поля мастера. Однословные поля (статус, даты, вид) читаются без хвоста-комментария `# …`: человек,
    скопировавший шапку шаблона вместе с пояснениями, не должен получить зеркало со словами пояснения в статусе."""
    raw = (fm.get(key) or "").strip()
    return head_cut_comment(raw).strip() if key in SIMPLE_FIELDS else raw


def _field_value(key: str, raw: str) -> str:
    raw = raw.strip()
    if key in QUOTE_ALWAYS and not raw.startswith("["):
        return _quoted(raw)
    return raw


def proj(text: str, tid: str) -> dict:
    """Что считается содержимым зеркала при сравнении: поля шапки без дат, источники и тело."""
    fm = frontmatter(text)
    out = {k: (v or "").strip() for k, v in fm.items() if k not in SKIP_COMPARE}
    out["sources"] = card_sources(text)
    out["@body"] = body(text)
    return out


@dataclass
class Plan:
    """Что делать с одним мастер-файлом (или с зеркалом, у которого мастера уже нет)."""
    action: str                 # create | update | same | archive | skip
    master: str = ""            # путь мастера от корня проекта
    mirror: str = ""            # путь зеркала от корня проекта
    tid: str = ""
    text: str = ""              # новый текст зеркала
    why: str = ""               # причина пропуска или отказа
    code: str = ""              # V-17, V-REC-1, V-13 …
    problems: list = field(default_factory=list)


def build_fields(spec: Spec, fm: dict, text: str, stem: str, master_rel: str, old: "str | None") -> dict:
    """Поля шапки зеркала: копии из мастера плюс поля движка. `old` — прежний текст зеркала (его даты и поля других проходов)."""
    old_fm = frontmatter(old) if old else {}
    f: dict = {}
    for key in COPY[spec.tid]:
        raw = field_raw(fm, key)
        if key == "title":
            f["title"] = raw or h1_of(text) or stem
        elif raw and not head_blank(raw):
            f[key] = _field_value(key, raw)
    created = f.get("created") or (old_fm.get("created") or "").strip() or (f.get("date") or "") or TODAY
    f["created"] = created
    f["updated"] = f.get("updated") or (old_fm.get("updated") or "").strip() or created
    f["type"] = spec.tid
    f["sources"] = [master_rel]
    f["built"] = "machine"
    f["source_synced"] = (old_fm.get("source_synced") or "").strip() or TODAY
    if spec.tid == "decision":
        f["kind"] = "document"
    elif spec.tid == "question":
        f["kind"] = "document"
        f["status"] = "draft"
        # Пока расхождение-источник вопроса не разбирается (пакет расхождений), состояние считается по ходу самого вопроса.
        f["q_effective"] = "resolved" if f.get("q_status") in ("answered", "closed-no-answer") else "open"
    else:
        kinds = spec.ct.kinds if spec.ct is not None else ("dictionary", "document")
        f["kind"] = f["kind"] if f.get("kind") in kinds else (kinds[0] if kinds else "dictionary")
        f["status"] = (old_fm.get("status") or "").strip() or "knowledge"      # дальше статус ведёт `kb:trust`
    if old:
        for key in CARRY:
            if key == "related":
                items = list_field(old, "related")
                if items:
                    f["related"] = items
            elif (old_fm.get(key) or "").strip():
                f[key] = _field_value(key, old_fm[key])
    return f


def plan_one(root: str, reg, spec: Spec, master_rel: str, text: str, mirror_taken: "dict | None" = None) -> Plan:
    """Зеркало одного мастера: действие и новый текст. Ничего не пишет."""
    name = os.path.basename(master_rel)
    stem = name[:-3]
    mirror_rel = f"{spec.mirror_dir}/{name}"
    plan = Plan("skip", master_rel, mirror_rel, spec.tid)
    head, _rest = split_frontmatter(text)
    if head is None:
        plan.code, plan.why = "V-01", "шапка не найдена: файл должен начинаться со строки «---» и закрываться такой же"
        return plan
    fm = frontmatter(text)
    declared = (fm.get("type") or "").strip()
    if declared and declared != spec.tid:
        plan.code = "V-17"
        plan.why = f"запись типа «{declared}» лежит в {spec.home}/, а тут место типа «{spec.tid}»"
        return plan
    if PLACEHOLDER_RE.search(text):
        plan.code, plan.why = "V-13", "в тексте остались заготовки {{…}}: заполните или удалите их — пока запись не готова, зеркала нет"
        return plan
    taken = (mirror_taken or {}).get(mirror_rel.lower())
    if taken and taken != master_rel:
        plan.code, plan.why = "V-REC-1", f"имя {name} уже занято мастер-файлом {taken}: зеркала с одним именем не бывает"
        return plan
    target = os.path.join(root, mirror_rel)
    old = read_text(target) if os.path.isfile(target) else None
    if old is not None:
        mine = (frontmatter(old).get("built") or "").strip() == "machine" and master_rel in card_sources(old)
        if not mine:
            plan.code = "V-REC-1"
            plan.why = (f"в базе под именем {name} уже лежит не зеркало этой записи ({mirror_rel}) — зеркало не создаётся; "
                        "переименуйте мастер или ту карточку")
            return plan
    fields = build_fields(spec, fm, text, stem, master_rel, old)
    fresh = CT.render_header(spec.tid, fields, reg=reg) + quote_safe(body(text))
    if old is not None and proj(fresh, spec.tid) == proj(old, spec.tid):
        plan.action, plan.text = "same", old
        return plan
    if old is not None:                      # изменилось по существу: дата сверки — сегодня
        fields["source_synced"] = TODAY
        fresh = CT.render_header(spec.tid, fields, reg=reg) + quote_safe(body(text))
    plan.action = "create" if old is None else "update"
    plan.text = fresh
    return plan


def validate_plan(root: str, reg, plan: Plan) -> list:
    """Нарушения шаблона у будущего зеркала (строго): [(код, сообщение)]. Пусто — запись примут."""
    if plan.action not in ("create", "update") or not plan.text:
        return []
    try:
        found = CT.check_card_text(os.path.join(root, plan.mirror), plan.text, op="mirror", reg=reg)
    except CT.TemplateViolation as e:
        return [("V-REG-1", str(e))]
    except (ValueError, OSError) as e:
        return [("V-01", str(e))]
    return [(v.code, v.message) for v in found]


def mirror_names(root: str, reg, by_spec: dict) -> dict:
    """{имя зеркала в нижнем регистре: путь мастера}: два мастера с одним именем не получают общего зеркала."""
    taken: dict = {}
    for spec in by_spec.values():
        for m in list_masters(root, spec):
            key = f"{spec.mirror_dir}/{os.path.basename(m)}".lower()
            taken.setdefault(key, m)
    return taken


# ----------------------------------------------------------------------------- обход

def scan(root: str, reg, only=None) -> list:
    """[(Spec, путь мастера, текст, Plan)] по всем мастер-файлам записей."""
    by_spec = specs(reg)
    taken = mirror_names(root, reg, by_spec)
    out = []
    for tid, spec in by_spec.items():
        if only and tid not in only:
            continue
        for rel in list_masters(root, spec):
            text = read_text(os.path.join(root, rel))
            if text is None:
                p = Plan("skip", rel, f"{spec.mirror_dir}/{os.path.basename(rel)}", tid, why="файл не читается", code="V-01")
                out.append((spec, rel, "", p))
                continue
            out.append((spec, rel, text, plan_one(root, reg, spec, rel, text, taken)))
    return out


def orphans(root: str, reg) -> list:
    """Зеркала, чьего мастера больше нет: [Plan(archive)]. Зеркалом считается машинная карточка раздела записей, все
    источники которой лежат в доме типа и ни одного из них на диске нет."""
    out = []
    for tid, spec in specs(reg).items():
        base = os.path.join(root, spec.mirror_dir)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            if not name.endswith(".md") or name.startswith("_") or name == "README.md":
                continue
            rel = f"{spec.mirror_dir}/{name}"
            text = read_text(os.path.join(root, rel))
            if text is None or (frontmatter(text).get("built") or "").strip() != "machine":
                continue
            srcs = card_sources(text)
            if not srcs or not all(s.replace("\\", "/").startswith(spec.home + "/") for s in srcs):
                continue
            if any(os.path.exists(os.path.join(root, s)) for s in srcs):
                continue
            out.append(Plan("archive", srcs[0], rel, tid, why=f"мастер-файл {srcs[0]} исчез"))
    return out


def master_spec(root: str, reg, rel: str) -> "Spec | None":
    """Тип записи по пути мастер-файла (`Raw/decisions/DR-0001-….md`); не мастер — None."""
    low = rel.replace("\\", "/")
    for spec in specs(reg).values():
        if low.startswith(spec.home + "/") and low.endswith(".md"):
            return spec
    return None


def find_master(root: str, reg, stem: str, tid: str = "") -> str:
    """Мастер-файл записи по имени без `.md` (так имя пишут в ссылках `[[…]]`): путь от корня проекта или пусто."""
    for spec in specs(reg).values():
        if tid and spec.tid != tid:
            continue
        for rel in list_masters(root, spec):
            if os.path.basename(rel)[:-3] == stem:
                return rel
    return ""


def why_master_refused(root: str, reg, rel: str, text: str) -> str:
    """Почему мастер-файл с этим текстом нельзя записать: его зеркало не прошло бы единый вход записи. Пусто — можно.
    Не мастер — пусто (суд не наш)."""
    spec = master_spec(root, reg, rel)
    if spec is None:
        return ""
    plan = plan_one(root, reg, spec, rel, _norm(text), mirror_names(root, reg, specs(reg)))
    if plan.action == "skip":
        return f"{plan.code}: {plan.why}"
    return "; ".join(f"{code}: {message}" for code, message in validate_plan(root, reg, plan)[:3])


def write_master(root: str, reg, rel: str, text: str) -> str:
    """Записать мастер-файл (правка существующего по команде человека: принятие решения, замена). → причина отказа или пусто.
    Записи не кладёт в базу ничего, кроме себя: зеркало строит `--mirror`."""
    why = why_master_refused(root, reg, rel, text)
    if why:
        return why
    aurora_fs.write_atomic(os.path.join(root, rel), text, newline="keep")
    return ""


# ----------------------------------------------------------------------------- проверка

def _numbers(root: str, spec: Spec, masters: list) -> dict:
    nums: dict = {}
    for rel in masters:
        n = number_of(spec, os.path.basename(rel))
        if n >= 0:
            nums.setdefault(n, []).append(rel)
    return nums


def reserved_names(root: str, reg) -> list:
    """Карточки базы с именами `DR-…`/`Q-…`, которые не записи: V-REC-1."""
    found = []
    for path in sorted(_walk_base(root)):
        name = os.path.basename(path)[:-3]
        if not RESERVED_RE.match(name):
            continue
        text = read_text(path)
        fm = frontmatter(text or "")
        tid = (fm.get("type") or "").strip()
        if tid in ("decision", "question"):
            continue
        found.append((rel_of(root, path), tid))
    return found


def _walk_base(root: str):
    base = os.path.join(root, KB_ROOT)
    for dirpath, dirs, files in safe_walk(base):
        dirs[:] = sorted(d for d in dirs if d not in ("_archive", "meta", "_assets"))
        for f in sorted(files):
            if f.endswith(".md") and not is_service(os.path.join(dirpath, f)):
                yield os.path.join(dirpath, f)


def check_all(root: str, reg) -> list:
    """Находки проверки: [(уровень, код, путь, сообщение)]. Читает, ничего не пишет."""
    out = []
    by_spec = specs(reg)
    results = scan(root, reg)
    for tid, spec in by_spec.items():
        if spec.ct is None:
            out.append((WARN, "V-REG-1", spec.home, f"в реестре нет типа «{tid}» — записи этого типа проверяются по прежним правилам"))
        elif not spec.ct.enabled:
            out.append((INFO, "V-03", spec.home, f"тип «{tid}» выключен в настройках проекта — зеркала не строятся"))
    for spec, rel, text, plan in results:
        if spec.ct is not None and not spec.ct.enabled:
            continue
        name = os.path.basename(rel)
        if spec.prefix and number_of(spec, name) < 0:
            out.append((ERR, "V-17", rel, f"имя файла записи типа «{spec.tid}» — {spec.prefix}<номер>-<суть>.md (номер выдаёт kb:record --new)"))
        if plan.action == "skip":
            out.append((ERR if plan.code in ("V-17", "V-13", "V-01", "V-REC-1") else WARN, plan.code or "V-17", rel, plan.why))
            continue
        tpl = (frontmatter(text).get("template") or "").strip()
        if tpl:
            known = reg.files.get(tpl)
            if known is None or known.id != spec.tid:
                out.append((WARN, "V-04", rel, f"template «{tpl[:60]}» не называет шаблон типа «{spec.tid}» — зеркало получит "
                                               f"{spec.ct.template_file if spec.ct else 'шаблон типа'}"))
        for code, message in validate_plan(root, reg, plan):
            out.append((ERR, code, rel, message))
        if plan.action == "create":
            out.append((WARN, "V-REC-2", rel, "зеркала в базе ещё нет: `kb:record --mirror --apply`"))
        elif plan.action == "update":
            out.append((WARN, "V-REC-2", rel, "зеркало отстаёт от мастера: `kb:record --mirror --apply`"))
    for spec in by_spec.values():
        nums = _numbers(root, spec, list_masters(root, spec))
        for n, where in sorted(nums.items()):
            if len(where) > 1:
                import id_registry as IDR
                out.append((ERR, "V-17", where[1], f"номер {IDR.format_id(SPACE[spec.tid], n)} занят дважды: " + ", ".join(where)))
    for plan in orphans(root, reg):
        out.append((INFO, "V-REC-2", plan.mirror, plan.why + ": `kb:record --mirror --apply` снимет зеркало в архив"))
    for rel, tid in reserved_names(root, reg):
        out.append((ERR, "V-REC-1", rel, f"имя занято под записи (DR-…, Q-…), а карточка типа «{tid or 'без типа'}» — не запись"))
    out.extend(check_corrections(root, reg))
    out.extend(check_pending(root, reg))
    return out


def check_corrections(root: str, reg) -> list:
    out = []
    base = os.path.join(root, CORRECTIONS_HOME)
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        if not name.endswith(".md") or name.startswith("_") or name == "README.md":
            continue
        rel = f"{CORRECTIONS_HOME}/{name}"
        text = read_text(os.path.join(base, name))
        if text is None:
            out.append((WARN, "V-01", rel, "файл не читается"))
            continue
        for v in CT.validate_card(text, path=rel, reg=reg, level="strict"):
            if v.severity == "info":
                out.append((INFO, v.code, rel, v.message))
            elif v.severity == "error":
                out.append((ERR, v.code, rel, v.message))
    return out


def check_pending(root: str, reg) -> list:
    """Рукописные записи, оставшиеся в базе: их переносит `--migrate`."""
    return [(WARN, "V-20", rel, f"рукописная карточка в базе: перенесите в {home}/ (`kb:record --migrate`)")
            for rel, home in pending(root, reg)]


def pending(root: str, reg) -> list:
    """[(путь, дом)] рукописных записей в разделах базы — кандидатов на перенос (на любой глубине раздела; в мастер-файле
    путь плоский: под своим именем)."""
    out = []
    for spec in specs(reg).values():
        base = os.path.join(root, spec.mirror_dir)
        if not os.path.isdir(base):
            continue
        for dirpath, dirs, files in safe_walk(base):
            dirs[:] = sorted(d for d in dirs if not d.startswith(("_", ".")))
            for name in sorted(files):
                if not name.endswith(".md") or name.startswith("_") or name == "README.md":
                    continue
                rel = rel_of(root, os.path.join(dirpath, name))
                text = read_text(os.path.join(root, rel))
                if text is None or not CT.pending_record(root, rel, text, reg):
                    continue
                if (frontmatter(text).get("kind") or "").strip() in CT.LEGACY_KINDS:
                    continue
                out.append((rel, spec.home))
    return sorted(out)


# ----------------------------------------------------------------------------- --new

def human_owner(given: str) -> str:
    """Владелец записи: названный или `git config user.name`. Имя модели или программы не принимается."""
    import kb_decide as KD
    who, err = KD.human_name(given)
    if err:
        err = err.replace("--by", "--owner").replace("решение принимает человек", "запись ведёт человек")
        raise RecordError(err if "--owner" in err else err + " (укажите --owner @имя)")
    return who


def clean_suffix(title: str) -> str:
    """Суть записи для имени и заголовка: одна строка, без скобок-заготовок, не длиннее `SUFFIX_MAX`."""
    s = " ".join(str(title or "").replace("{", "").replace("}", "").split())
    if len(s) > SUFFIX_MAX:
        s = s[:SUFFIX_MAX].rsplit(" ", 1)[0] if " " in s[:SUFFIX_MAX] else s[:SUFFIX_MAX]
    return s.strip(" .:-")


def fill_body(skeleton: str, values: dict, drop_notes: bool = False) -> str:
    """Скелет тела шаблона с подставленными номером, заголовком, датами и владельцем; прочие `{{…}}` остаются."""
    text = COMMENT_RE.sub("", skeleton)
    if drop_notes:                       # необязательный раздел справочника: в заготовке не нужен
        text = re.sub(r"(?ms)^## Примечания\s*\n.*\Z", "", text)
    for token, value in values.items():
        text = text.replace("{{" + token + "}}", value)
    return text.rstrip("\n") + "\n"


def new_record(root: str, reg, tid: str, title: str, *, status: str = "", owner: str = "", blocks: str = "") -> str:
    """Заготовка записи в `Raw/<дом>/`. → путь от корня проекта."""
    spec = specs(reg).get(tid)
    if spec is None:
        raise RecordError(f"тип «{tid}» не запись: есть {', '.join(RECORD_TYPES)}")
    ct = spec.ct
    if ct is None or not CT.registry_for(root).types:
        raise RecordError(f"в реестре нет шаблона типа «{tid}» — обновите проект: `kit:update`")
    if not ct.enabled:
        raise RecordError(f"тип «{tid}» выключен в настройках проекта")
    suffix = clean_suffix(title)
    if not suffix:
        raise RecordError("нужна суть записи словами: kb:record --new " + tid + ' "суть"')
    tpl = CT.load_template(ct.path)
    today = TODAY
    who = human_owner(owner)
    if tid == "decision":
        status = (status or "proposed").strip()
        if status not in ("proposed", "accepted"):
            raise RecordError("новое решение — proposed или accepted (rejected и superseded наступают позже)")
    ident = ""
    if tid in SPACE:
        import id_registry as IDR
        try:
            ident = IDR.allocate(SPACE[tid], root)
        except (OSError, RuntimeError, ValueError) as e:
            raise RecordError(f"номер не выдан: {e}") from e
        number = ident.split("-", 1)[1]
        full_title = f"{ident}: {suffix}"
    else:
        number = ""
        full_title = suffix
    name = card_filename(full_title) + ".md"
    if len(name.encode("utf-8")) > NAME_BYTES:
        raise RecordError("имя файла длиннее допустимого — сократите суть")
    rel = f"{spec.home}/{name}"
    path = os.path.join(root, rel)
    if os.path.exists(path):
        raise RecordError(f"{rel} уже есть")
    if os.path.exists(os.path.join(root, spec.mirror_dir, name)):
        raise RecordError(f"в базе уже есть карточка {spec.mirror_dir}/{name} — зеркало с этим именем не создалось бы (V-REC-1); "
                          "выберите другую суть")
    fields: dict = {"title": full_title, "tags": [tid], "created": today, "updated": today}
    if tid == "decision":
        fields.update(aliases=[ident], status=status, date=today, owner=_quoted(who))
    elif tid == "question":
        fields.update(aliases=[ident], q_id=ident, q_status="open", owner=_quoted(who))
        if blocks.strip():
            fields["blocks"] = [blocks.strip()]
    else:
        fields.update(tags=[], kind="dictionary", owner=_quoted(who))
    header = CT.render_header(tid, fields, reg=reg)
    values = {"NNNN": number, "NNN": number, "краткая формулировка решения": suffix, "краткая суть вопроса": suffix,
              "Название справочника": suffix, "ГГГГ-ММ-ДД": today, "@автор": who, "кем": who}
    text = header + "\n" + fill_body(tpl.body_skeleton, values, drop_notes=(tid == "reference"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    aurora_fs.write_atomic(path, text, newline="keep")
    return rel


# ----------------------------------------------------------------------------- --list

def status_key(spec: Spec, fm: dict) -> str:
    if spec.tid == "decision":
        return (fm.get("status") or "").strip() or "—"
    if spec.tid == "question":
        return (fm.get("q_status") or "").strip() or "—"
    return (fm.get("kind") or "").strip() or "dictionary"


def counts(root: str, reg) -> dict:
    out = {}
    waiting: dict = {}
    for _rel, home in pending(root, reg):
        waiting[home] = waiting.get(home, 0) + 1
    for tid, spec in specs(reg).items():
        by = {}
        masters = list_masters(root, spec)
        for rel in masters:
            text = read_text(os.path.join(root, rel))
            if text is None:
                continue
            k = status_key(spec, frontmatter(text))
            by[k] = by.get(k, 0) + 1
        mirrors = 0
        base = os.path.join(root, spec.mirror_dir)
        for name in (sorted(os.listdir(base)) if os.path.isdir(base) else []):
            if name.endswith(".md") and not name.startswith("_") and name != "README.md":
                text = read_text(os.path.join(base, name))
                if text is not None and (frontmatter(text).get("built") or "").strip() == "machine" \
                        and any(s.startswith(spec.home + "/") for s in card_sources(text)):
                    mirrors += 1
        out[tid] = {"home": spec.home, "masters": len(masters), "by_status": dict(sorted(by.items())), "mirrors": mirrors,
                    "pending": waiting.get(spec.home, 0)}
    base = os.path.join(root, CORRECTIONS_HOME)
    files = [n for n in (os.listdir(base) if os.path.isdir(base) else []) if n.endswith(".md") and not n.startswith("_") and n != "README.md"]
    out["correction"] = {"home": CORRECTIONS_HOME, "masters": len(files)}
    return out


def print_list(data: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return
    names = {"decision": "Решения", "question": "Вопросы", "reference": "Справочники", "correction": "Исправления"}
    print("# Записи в Raw\n")
    for tid in ("decision", "question", "reference", "correction"):
        d = data[tid]
        line = f"- {names[tid]} (`{d['home']}`): {d['masters']}"
        if d.get("by_status"):
            line += " — " + ", ".join(f"{k} {v}" for k, v in d["by_status"].items())
        if tid != "correction":
            line += f" · зеркал в базе: {d['mirrors']}"
            if d.get("pending"):
                line += f" · рукописных в базе, ждут переноса: {d['pending']}"
        print(line)


# ----------------------------------------------------------------------------- зеркала: запись

def _free_archive(dst: str) -> str:
    if not os.path.exists(dst):
        return dst
    folder, name = os.path.split(dst)
    stem, ext = os.path.splitext(name)
    for suffix in ["-" + utc_slug("%Y%m%d-%H%M"), "-" + utc_slug("%Y%m%d-%H%M%S")] + [f"-{n}" for n in range(2, 60)]:
        room = NAME_BYTES - len((suffix + ext).encode("utf-8"))
        cut = stem.encode("utf-8")[:max(1, room)].decode("utf-8", "ignore")
        probe = os.path.join(folder, cut + suffix + ext)
        if not os.path.exists(probe):
            return probe
    return dst


def archive_mirror(root: str, reg, plan: Plan) -> str:
    """Зеркало исчезнувшего мастера → `_archive/` с записью в «История изменений». → причина отказа или пусто."""
    src = os.path.join(root, plan.mirror)
    text = read_text(src)
    if text is None:
        return "файл не читается"
    line = f"- {TODAY}: мастер-файл `{plan.master}` исчез — зеркало снято в архив"
    note = (text.rstrip() + "\n" + line + "\n") if "\n## " + CT.HEAD_FOOTER in text else \
        (text.rstrip() + f"\n\n## {CT.HEAD_FOOTER}\n\n{line}\n")
    CT.put_text(src, note, op="repair", reg=reg)             # отказ шаблона запись в историю не отменяет: перенос важнее заметки
    dst_dir = os.path.join(root, KB_ROOT, "_archive")
    if CT.leaves_base(src, dst_dir):
        return "папка архива лежит вне базы знаний (ссылка)"
    os.makedirs(dst_dir, exist_ok=True)
    shutil.move(src, _free_archive(os.path.join(dst_dir, os.path.basename(src))))
    return ""


def write_mirror(root: str, reg, plan: Plan) -> str:
    """Записать зеркало через единый вход. → причина отказа или пусто."""
    path = os.path.join(root, plan.mirror)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    done, why = CT.try_write_card(path, plan.text, op="mirror", reg=reg)
    return why if not done and why else ""


def in_git() -> bool:
    try:
        cp = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30)
        return cp.returncode == 0 and cp.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def run_mirror(root: str, reg, apply: bool, only=None) -> dict:
    """Построить зеркала. → итог: счётчики и списки. Без `apply` ничего не пишет."""
    res = {"create": [], "update": [], "same": [], "archive": [], "skip": [], "refused": [], "written": 0}
    by_spec = specs(reg)
    plans = [p for _s, _r, _t, p in scan(root, reg, only)]
    plans += [] if only else orphans(root, reg)
    for plan in plans:
        spec = by_spec.get(plan.tid)
        if spec is None or (spec.ct is not None and not spec.ct.enabled):
            res["skip"].append((plan.master, f"тип «{plan.tid}» выключен в настройках проекта"))
            continue
        if plan.action == "skip":
            res["skip"].append((plan.master, f"{plan.code}: {plan.why}"))
            continue
        if plan.action == "same":
            res["same"].append(plan.mirror)
            continue
        if plan.action == "archive":
            res["archive"].append(plan.mirror)
            if apply:
                why = archive_mirror(root, reg, plan)
                if why:
                    res["refused"].append((plan.mirror, why))
                else:
                    res["written"] += 1
            continue
        bad = validate_plan(root, reg, plan)
        if bad:
            res["refused"].append((plan.master, "; ".join(f"{c}: {m}" for c, m in bad[:3])))
            continue
        res[plan.action].append(plan.mirror)
        if apply:
            why = write_mirror(root, reg, plan)
            if why:
                res["refused"].append((plan.master, why))
                res[plan.action].remove(plan.mirror)
            else:
                res["written"] += 1
    return res


def print_mirror(res: dict, apply: bool) -> None:
    print(f"# Зеркала записей — {TODAY}\n")
    print(f"- создать: {len(res['create'])} · обновить: {len(res['update'])} · без изменений: {len(res['same'])} · "
          f"снять в архив: {len(res['archive'])} · пропущено: {len(res['skip'])} · отказано шаблоном: {len(res['refused'])}")
    for title, key in (("Создаются", "create"), ("Обновляются", "update"), ("Уходят в архив", "archive")):
        for rel in res[key][:8]:
            print(f"  - {title.lower()}: {rel}")
        if len(res[key]) > 8:
            print(f"  … и ещё {len(res[key]) - 8} ({title.lower()})")
    for master, why in res["skip"][:10]:
        print(f"  ! пропущено {master}: {why}")
    for master, why in res["refused"][:10]:
        print(f"  ! не записано {master}: {why}")
    if not apply:
        todo = res["create"] or res["update"] or res["archive"]
        if todo:
            print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        elif not (res["skip"] or res["refused"]):
            print("\nЗеркала в порядке — делать нечего.")
    else:
        print(f"\nЗаписано: {res['written']}")


# ----------------------------------------------------------------------------- --migrate

def master_header(spec: Spec, text: str, rel: str) -> "tuple[str, list]":
    """Шапка мастера из шапки рукописной карточки базы (§14.4): копируются поля мастера, поля движка отбрасываются.
    → (шапка, замечания для отчёта)."""
    fm = frontmatter(text)
    notes = []
    f: dict = {}
    for key in COPY[spec.tid]:
        raw = field_raw(fm, key)
        if key == "title":
            f["title"] = raw or h1_of(text) or os.path.basename(rel)[:-3]
        elif key == "template":
            continue
        elif raw and not head_blank(raw):
            f[key] = _field_value(key, raw)
    f["type"] = spec.tid
    if spec.tid == "decision":
        status = f.get("status", "")
        if status and status not in OWN_LIFECYCLE["decision"]:
            cycle = ", ".join(OWN_LIFECYCLE["decision"])
            notes.append(f"status «{status}» вне цикла решения ({cycle}) оставлен как есть")
    if spec.tid == "reference":
        f["kind"] = "dictionary"
        if not f.get("owner"):
            notes.append("владелец справочника неизвестен — поле owner пусто")
    return CT.render_header(spec.tid, f), notes


def run_migrate(root: str, reg, apply: bool) -> dict:
    res = {"moved": [], "skipped": [], "notes": [], "mirrors": None}
    by_spec = specs(reg)
    for rel, home in pending(root, reg):
        spec = next(s for s in by_spec.values() if s.home == home)
        if spec.ct is not None and not spec.ct.enabled:
            res["skipped"].append((rel, f"тип «{spec.tid}» выключен в настройках проекта"))
            continue
        text = read_text(os.path.join(root, rel))
        target = f"{spec.home}/{os.path.basename(rel)}"
        if os.path.exists(os.path.join(root, target)):
            res["skipped"].append((rel, f"{target} уже есть — перенос отказан, ничего не потеряно"))
            continue
        header, notes = master_header(spec, text, rel)
        res["moved"].append((rel, target))
        res["notes"] += [(rel, n) for n in notes]
        if not apply:
            continue
        src, dst = os.path.join(root, rel), os.path.join(root, target)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        cp = subprocess.run(["git", "mv", "--", rel, target], cwd=root, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120)
        if cp.returncode != 0:
            res["moved"].pop()
            res["skipped"].append((rel, "git mv не удался: " + (cp.stderr.strip().splitlines() or ["?"])[-1][:200]))
            continue
        aurora_fs.write_atomic(dst, header + body(text), newline="keep")
        if os.path.exists(src):
            res["skipped"].append((rel, "прежний файл остался на месте"))
    if apply and res["moved"]:
        res["mirrors"] = run_mirror(root, reg, True)
    return res


def print_migrate(res: dict, apply: bool) -> None:
    print(f"# Перенос рукописных записей в Raw — {TODAY}\n")
    print(f"- переносится: {len(res['moved'])} · пропущено: {len(res['skipped'])}")
    for src, dst in res["moved"][:20]:
        print(f"  - {src} → {dst}")
    for src, why in res["skipped"][:20]:
        print(f"  ! пропущено {src}: {why}")
    for src, note in res["notes"][:20]:
        print(f"  · {src}: {note}")
    if res.get("mirrors") is not None:
        print_mirror(res["mirrors"], True)
    elif not res["moved"] and not res["skipped"]:
        print("\nРукописных записей в базе нет — делать нечего.")
    elif not apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")


# ----------------------------------------------------------------------------- команда

def print_check(found: list, as_json: bool, title: str = "Записи в Raw — проверка") -> int:
    errors = sum(1 for f in found if f[0] == ERR)
    warns = sum(1 for f in found if f[0] == WARN)
    infos = sum(1 for f in found if f[0] == INFO)
    if as_json:
        print(json.dumps({"errors": errors, "warnings": warns, "info": infos,
                          "findings": [dict(zip(("level", "code", "path", "message"), f)) for f in found]},
                         ensure_ascii=False, indent=1))
        return 1 if errors else 0
    print(f"# {title}\n")
    marks = {ERR: "ОШИБКА", WARN: "предупреждение", INFO: "сведение"}
    for level, code, rel, message in found[:200]:
        print(f"- {marks[level]} {code} {rel}: {message}")
    if len(found) > 200:
        print(f"… ещё {len(found) - 200}")
    print(f"\nНайдено: ошибок {errors}, предупреждений {warns}, сведений {infos}")
    return 1 if errors else 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(description="Записи в Raw/: решения, вопросы, рукописные справочники и их зеркала в базе")
    ap.add_argument("title", nargs="?", default="", metavar="СУТЬ", help="суть новой записи словами, в кавычках (с --new)")
    ap.add_argument("--new", metavar="ТИП", choices=RECORD_TYPES,
                    help="завести запись: decision, question или reference (файл в Raw/…, номер выдаёт реестр)")
    ap.add_argument("--status", default="", metavar="СТАТУС", help="с --new decision: proposed (по умолчанию) или accepted")
    ap.add_argument("--owner", default="", metavar="@ИМЯ", help="с --new: кто ведёт запись (человек; по умолчанию git user.name)")
    ap.add_argument("--blocks", default="", metavar="КАРТОЧКА", help="с --new question: ссылка [[Карточка]] на то, что блокирует вопрос")
    ap.add_argument("--list", action="store_true", help="сколько записей по типам и состояниям")
    ap.add_argument("--check", action="store_true", help="проверить мастер-файлы и зеркала (код 1 — есть ошибки)")
    ap.add_argument("--mirror", action="store_true", help="построить и обновить зеркала записей в базе (без --apply — план)")
    ap.add_argument("--migrate", action="store_true",
                    help="перенести рукописные записи из Decisions/Questions/Reference в Raw/ и построить зеркала (без --apply — план)")
    ap.add_argument("--apply", action="store_true", help="записать изменения (иначе сухой прогон)")
    ap.add_argument("--allow-dirty", action="store_true", help="писать по незакоммиченному дереву (откат станет неполным)")
    ap.add_argument("--json", action="store_true", help="машинный вывод для --list и --check")
    ap.add_argument("--root", metavar="PATH", default=".", help="корень проекта (по умолчанию — текущая папка)")
    args = ap.parse_args(argv)
    modes = [bool(args.new), args.list, args.check, args.mirror, args.migrate]
    if sum(modes) != 1:
        ap.error("нужен ровно один режим: --new ТИП «суть», --list, --check, --mirror или --migrate")
    if not hasattr(CT, "record_cycle_problems"):
        print("kb_record: копия card_types.py старше этого скрипта — обновите проект: `kit:update`", file=sys.stderr)
        return 2
    if args.root != ".":
        os.chdir(args.root)
    root = "."
    if not os.path.isdir(KB_ROOT):
        print(f"kb_record: нет {KB_ROOT}/ — запускайте из корня проекта", file=sys.stderr)
        return 1
    reg = CT.registry_for(root)
    if not reg.types:
        why = "; ".join(reg.problems[:3])
        print("kb_record: реестр типов пуст — шаблонов akdb_*_v*.md нет ни в проекте, ни в ките"
              + (f" ({why})" if why else "") + ". Обновите проект: `kit:update`", file=sys.stderr)
        return 2

    if args.new:
        try:
            rel = new_record(root, reg, args.new, args.title, status=args.status, owner=args.owner, blocks=args.blocks)
        except RecordError as e:
            print(f"kb_record: {e}", file=sys.stderr)
            return 1
        print(f"# Запись заведена\n\n`{rel}`\n")
        print("Дальше: заполните заготовки `{{…}}` в файле. Зеркало в базе появится, когда запись будет готова: "
              "`kb:record --mirror --apply` (маршруты «Обновить» и «Починить» делают это сами).")
        return 0
    if args.list:
        print_list(counts(root, reg), args.json)
        return 0
    if args.check:
        return print_check(check_all(root, reg), args.json)

    if args.apply:
        if not args.allow_dirty and not in_git():
            print("kb_record: проект не под git — откат невозможен. Закоммитьте работу (`git init`) или осознанно "
                  "продолжите с --allow-dirty.", file=sys.stderr)
            return 2
        if not git_guard(KB_ROOT, args.allow_dirty, "зеркала записей" if args.mirror else "перенос записей"):
            return 2
        if args.migrate and not git_guard(RAW, args.allow_dirty, "перенос записей"):
            return 2
    if args.mirror:
        res = run_mirror(root, reg, args.apply)
        print_mirror(res, args.apply)
        todo = res["create"] or res["update"] or res["archive"]
        if args.apply:
            return 1 if (res["refused"] or res["skip"]) else 0
        return 1 if (todo or res["refused"] or res["skip"]) else 0
    if args.migrate:
        if args.apply and not in_git():
            print("kb_record: перенос идёт командой `git mv`, а проект не под git — перенос не выполнен. Заведите git "
                  "(`git init`) и закоммитьте работу.", file=sys.stderr)
            return 2
        res = run_migrate(root, reg, args.apply)
        print_migrate(res, args.apply)
        if args.apply:
            mirrors = res.get("mirrors") or {}
            return 1 if (res["skipped"] or mirrors.get("refused")) else 0
        return 1 if (res["moved"] or res["skipped"]) else 0
    return 0


if __name__ == "__main__":
    sys.exit(CT.run_script(main))
