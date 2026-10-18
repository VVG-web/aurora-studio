#!/usr/bin/env python3
"""kb_decide.py — журнал решений (Decision Record): принять, отклонить, проверить, показать (фреймворк «Аврора»).

Цикл решения — `proposed → accepted | rejected → superseded`. Раньше он жил в справке: движок не знал, кто и
когда решение принял, не проверял «не меньше двух вариантов и причина отказа у каждого отклонённого» и не
защищал принятое решение от правки. Здесь это сделано кодом.

  python3 .aurora/scripts/kb_decide.py DR-0042 --by @имя              # принять: proposed → accepted
  python3 .aurora/scripts/kb_decide.py DR-0042 --reject --reason "…"  # отклонить с причиной
  python3 .aurora/scripts/kb_decide.py --journal [--json]             # журнал решений: статусы, что просрочено
  python3 .aurora/scripts/kb_decide.py --check                        # форма каждого решения

Принимает только человек: имя в `--by` — его, имя модели или программы не принимается (по умолчанию —
`git config user.name`). Принятие проверяет форму: не меньше двух вариантов, выбран один, у каждого
отклонённого есть «Причина отказа», названы затронутые карточки и они есть в базе, нет `{{…}}` и
неразобранного раздела «Под вопросом». Не сошлось — решение остаётся `proposed`, а перечень идёт в вывод.

Принятое решение неизменяемо: при принятии в шапку ложится отпечаток тела (`accepted_fp`), и `kb:lint`
называет ошибкой решение, у которого тело с тех пор поменялось. Отпечаток не видит служебный хвост
(«История», «Под вопросом» и прочие разделы, которые ведёт движок), первый заголовок и цели ссылок `[[…]]`:
переименование карточки, на которую ссылается решение, его не меняет. Правка по существу — новое решение,
которое заменяет прежнее (`kb:supersede`).

Решения лежат в `AuroraKnowledgeDB/Decisions/`; запись идёт через единый вход записи карточек
(`card_types.write_card_text`), шаблон `akdb_decision` проверяет результат.

Панель: `kb:decide-accept` · `ops:decisions`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations
import aurora_common  # noqa: F401 — защита версии Python: на старом — понятный текст, а не traceback

import argparse
import hashlib
import json
import os
import re
import subprocess
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
from aurora_common import (KB_ROOT, LINK_RE, aliases as card_aliases, body as card_body, fold,  # noqa: E402
                           frontmatter, link_targets, lock_release, lock_take, safe_walk, utc_today,
                           with_fields)

DECISIONS = f"{KB_ROOT}/Decisions"
ARCHIVE = f"{KB_ROOT}/_archive"
STALE_DAYS = 14          # предложенное решение старше — просрочено: его принимают или отклоняют
FINAL = ("accepted", "rejected", "superseded")
# Служебный хвост карточки: его ведёт движок, на отпечаток принятого решения он не влияет.
TAIL = ("история", "история изменений", "под вопросом", "источник (перенесено дословно)",
        "исправления человеком", "слияние")
# Имя принимающего: человек. Имя модели или программы — нельзя (правило авторства Авроры). Латиница сверяется
# после `fold` (кириллическая «С» в «Сlaude» становится латинской) и по подстроке: `claude_code`, `gpt4`.
MODEL_WORDS = re.compile(r"claude|anthropic|gpt|openai|gemini|copilot|llm|assistant|codex|opus|sonnet|haiku|"
                         r"mistral|llama|deepseek|aurora[-_ ]?(?:studio|bot|engine)")
BOT_WORD = re.compile(r"(?:^|[^a-z0-9])bot(?:$|[^a-z0-9])")
RU_NOT_HUMAN = re.compile(r"(?<![а-яё])(?:клод|ассистент\w*|нейросет\w*|бот)(?![а-яё])")


def not_human(name: str) -> bool:
    low = fold(name)
    return bool(MODEL_WORDS.search(low) or BOT_WORD.search(low) or RU_NOT_HUMAN.search(name.lower()))


# ------------------------------------------------------------------ разбор тела

def split_sections(body: str) -> list:
    """[(заголовок раздела второго уровня или «», [строки])]: текст до первого «##» — раздел «»."""
    out = [("", [])]
    for line in body.splitlines():
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m:
            out.append((m.group(1), []))
        else:
            out[-1][1].append(line)
    return out


def section_text(body: str, *names: str) -> str:
    for title, lines in split_sections(body):
        if title and any(title.lower().startswith(n) for n in names):
            return "\n".join(lines)
    return ""


OPTIONS = ("рассмотренные варианты", "варианты", "considered options", "options")
CONSEQUENCES = ("следствия", "consequences")
CHOSEN = re.compile(r"(?<![а-яёa-z])(?:выбран\w*|принят\w*|chosen|selected)(?![а-яёa-z])")
REJECTED = re.compile(r"(?<![а-яёa-z])(?:отклон[её]н\w*|отвергнут\w*|rejected)(?![а-яёa-z])")
REASON = re.compile(r"(?im)(?:причин\w*\s+отказ\w*|reason\s+for\s+(?:rejection|refusal)|rejection\s+reason)[\s*:—-]*(\S[^\n]*)$")


def body_fingerprint(text: str) -> str:
    """Отпечаток тела решения: sha256 нормализованного текста без служебного хвоста.

    Нормализация: без разделов хвоста (`TAIL`) и первого заголовка, цель ссылки `[[…]]` заменена её
    подписью (`[[Цель|подпись]]` → подпись, `[[Цель]]` → пусто; число ссылок остаётся), пробельные
    последовательности сжаты. Так переименование карточки, на которую ссылается решение, перенос строк
    или правка истории отпечаток не меняют, а слово в варианте, причине отказа или решении — меняют.
    Предел: подмена одной цели ссылки на другую того же числа и подписи отпечатком не видна —
    переименование и слияние движком от неё не отличить."""
    keep, first = [], True
    for title, lines in split_sections(card_body(text)):
        if title.lower() in TAIL:
            continue
        for l in lines:
            if first and re.match(r"^#\s", l):          # первый заголовок первого уровня — имя, его правит `kb:repair`
                first = False
                continue
            keep.append(l)
        if title:
            keep.append("## " + title)
    flat = LINK_RE.sub(lambda m: "[[" + (m.group(5) or "") + "]]", "\n".join(keep))
    return hashlib.sha256(" ".join(flat.split()).encode("utf-8")).hexdigest()


def variants(body: str) -> list:
    """Варианты из раздела «Рассмотренные варианты»: [{title, chosen, rejected, reason}]."""
    block = section_text(body, *OPTIONS)
    out = []
    for part in re.split(r"(?m)^###\s+", block)[1:]:
        head, _, rest = part.partition("\n")
        # Отметка стоит в конце заголовка после тире («1. Три дня — выбран»); нет тире — отдельное слово.
        tail = re.split(r"\s[—–-]\s", head)[-1].lower() if re.search(r"\s[—–-]\s", head) else head.lower()
        rejected = bool(REJECTED.search(tail))
        chosen = not rejected and bool(CHOSEN.search(tail))
        m = REASON.search(rest)
        out.append({"title": re.sub(r"^\d+[.)]\s*", "", head).strip(), "chosen": chosen,
                    "rejected": rejected, "reason": (m.group(1).strip() if m else "")})
    return out


def affected(body: str) -> list:
    """Карточки из строки «Затронутые карточки: [[…]], […]» раздела «Следствия»."""
    names, inside = [], False
    for line in section_text(body, *CONSEQUENCES).splitlines():
        if re.search(r"(?i)затронут|affected", line):
            inside = True
            names += link_targets(line)
        elif inside and (line.startswith((" ", "\t")) or not line.strip()) and line.strip():
            names += link_targets(line)                 # продолжение списком на следующих строках
        elif inside and line.strip():
            inside = False
    return list(dict.fromkeys(names))


def lint_decision(text: str, known: "set | None" = None) -> list:
    """Замечания к форме решения: [{id, msg}]. `known` — имена карточек базы (проверка «затронутые существуют»)."""
    body = card_body(text)
    fm = frontmatter(text)
    out = []
    vs = variants(body)
    if len(vs) < 2:
        out.append({"id": "DR-01", "msg": f"вариантов {len(vs)}: выбор из одного варианта не выбор — "
                                          "нужно не меньше двух, отклонённые остаются в теле навсегда"})
    if vs and sum(1 for v in vs if v["chosen"]) != 1:
        out.append({"id": "DR-03", "msg": "выбранный вариант должен быть один и помечен в заголовке («— выбран»), "
                                          "отклонённые — «— отклонён»"})
    bare = [v["title"] for v in vs if not v["chosen"] and not v["reason"]]
    if bare:
        out.append({"id": "DR-02", "msg": "у отклонённых вариантов нет «Причина отказа»: " + "; ".join(bare[:4])})
    names = affected(body)
    if not names:
        out.append({"id": "DR-04", "msg": "в «Следствиях» нет строки «Затронутые карточки: [[…]]»"})
    elif known is not None:
        miss = [n for n in names if n not in known]
        if miss:
            out.append({"id": "DR-05", "msg": "затронутые карточки не найдены в базе: " + ", ".join(miss[:5])})
    if "{{" in body:
        out.append({"id": "DR-06", "msg": "в тексте остались заготовки {{…}}"})
    if (fm.get("status") or "").strip() == "superseded" and not (fm.get("superseded_by") or "").strip():
        out.append({"id": "DR-07", "msg": "статус superseded без superseded_by"})
    return out


def body_changed(text: str) -> bool:
    """Тело принятого (отклонённого, заменённого) решения не то, что было при принятии."""
    fm = frontmatter(text)
    stamp = (fm.get("accepted_fp") or "").strip().strip("\"'")
    return bool(stamp) and (fm.get("status") or "").strip() in FINAL and stamp != body_fingerprint(text)


# ------------------------------------------------------------------ поиск

def dr_number(name: str) -> int:
    m = re.match(r"(?i)^DR-([0-9]+)", name)
    return int(m.group(1)) if m else -1


def decision_files(root: str = ".") -> list:
    """[(путь, архив ли)] карточек-решений: `Decisions/` и архив базы (файлы `DR-…`)."""
    out = []
    for rel, archived in ((DECISIONS, False), (ARCHIVE, True)):
        base = os.path.join(root, rel)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in safe_walk(base):
            for f in sorted(files):
                if f.endswith(".md") and dr_number(f) >= 0:
                    out.append((os.path.join(dirpath, f), archived))
    return sorted(out)


def find_dr(root: str, ident: str) -> tuple:
    """→ (путь, ошибка): решение по номеру (`DR-0042`, `DR-42`, `42`) или по имени файла."""
    ident = ident.strip()
    if ident.lower().endswith(".md"):
        ident = ident[:-3]
    num = int(ident) if ident.isascii() and ident.isdigit() else dr_number(ident)
    hits = [p for p, _a in decision_files(root)
            if os.path.splitext(os.path.basename(p))[0] == ident or (num >= 0 and dr_number(os.path.basename(p)) == num)]
    exact = [p for p in hits if os.path.splitext(os.path.basename(p))[0] == ident]
    live = [p for p in (exact or hits) if "/_archive/" not in p.replace("\\", "/")]
    hits = live or exact or hits
    if not hits:
        return "", f"решения «{ident}» нет в {DECISIONS}/"
    if len(hits) > 1:
        return "", "номеру отвечает несколько файлов: " + ", ".join(sorted(hits)) + " — повтор номера, разберите его"
    return hits[0], ""


def known_names(root: str = ".") -> set:
    """Имена карточек базы и их синонимы — то, на что решение вправе ссылаться."""
    names = set()
    base = os.path.join(root, KB_ROOT)
    for dirpath, _dirs, files in safe_walk(base):
        if "/_archive" in dirpath.replace("\\", "/"):
            continue
        for f in files:
            if f.endswith(".md"):
                names.add(os.path.splitext(f)[0])
                try:
                    with open(os.path.join(dirpath, f), encoding="utf-8", errors="ignore") as fh:
                        names.update(card_aliases(fh.read(4000)))
                except OSError:
                    continue
    return names


# ------------------------------------------------------------------ принять и отклонить

def human_name(by: str) -> tuple:
    """→ (имя «@имя», ошибка). Пусто — из `git config user.name`. Имя модели или программы не принимается."""
    by = (by or "").strip()
    given = bool(by)
    if not by:
        try:
            by = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            by = ""
    if not by:
        return "", "не назван принявший: укажите --by @имя (решение принимает человек)"
    by = by.lstrip("@").strip()
    # Имя попадает в шапку и в историю: управляющие знаки, переводы строк (в том числе U+2028 и `\r`, по которым
    # рвёт `splitlines`), кавычки и обратная косая не годятся — иначе имя дописало бы в шапку чужие поля.
    if not by or len(by) > 60 or any(ord(ch) < 32 or ch in '"\\\u2028\u2029\u0085\u007f' for ch in by):
        src = "" if given else " (взято из git config user.name — задайте --by)"
        return "", "имя принявшего не годится: одна короткая строка без кавычек и управляющих знаков" + src
    if not_human(by):
        return "", "решение принимает человек: имя модели или программы не принимается"
    return "@" + by, ""


def add_history(text: str, line: str) -> str:
    """Строка в «## История» (сверху, как у `kb:supersede`); раздела нет — он создаётся в конце."""
    head, sep, body = text.partition("\n---")
    if re.search(r"(?m)^## История\s*$", body):
        body = re.sub(r"(?m)^(## История\s*\n)", lambda m: m.group(1) + "\n" + line + "\n", body, count=1)
        return head + sep + body
    return text.rstrip("\n") + f"\n\n## История\n\n{line}\n"


def decide(root: str, ident: str, by: str, reject: bool, reason: str) -> tuple:
    """Принять или отклонить решение. → (код возврата, строки вывода)."""
    path, err = find_dr(root, ident)
    if err:
        return 1, [f"kb_decide: {err}"]
    # Принять и отклонить одновременно нельзя: статус читается и пишется под замком, и второй видит уже
    # изменённое решение.
    lock = os.path.join(root, ".aurora", "state", "ids", f"decide-{dr_number(os.path.basename(path)):04d}.lock")
    got = False
    for _ in range(100):
        got, _held = lock_take(lock, {"task": "decide", "at": time.time()}, stale_s=60)
        if got:
            break
        time.sleep(0.05)
    if not got:
        return 1, ["kb_decide: решение сейчас меняет другой процесс — повторите"]
    try:
        return _decide_locked(root, path, by, reject, reason)
    finally:
        lock_release(lock)


def _decide_locked(root: str, path: str, by: str, reject: bool, reason: str) -> tuple:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    fm = frontmatter(text)
    status = (fm.get("status") or "").strip()
    name = os.path.splitext(os.path.basename(path))[0]
    if status != "proposed":
        tail = (" Заменить принятое — новое решение и `kb:supersede`." if status in FINAL else "")
        return 1, [f"kb_decide: {name} в состоянии «{status or 'без статуса'}»: принимают и отклоняют только "
                   f"proposed.{tail}"]
    who, err = human_name(by)
    if err:
        return 1, [f"kb_decide: {err}"]
    if reject and not reason.strip():
        return 1, ["kb_decide: отклонение без причины не принимается — укажите --reason «…»"]
    today = utc_today()
    if not reject:
        problems = lint_decision(text, known_names(root))
        if re.sub(r"\s+", "", section_text(card_body(text), "под вопросом")):
            problems.append({"id": "DR-08", "msg": "остался раздел «Под вопросом»: перенесите проверенное в "
                                                   "варианты и решение, остальное удалите"})
        if problems:
            return 1, [f"kb_decide: {name} не принято — форма решения не сходится:"] + \
                   [f"  {p['id']}: {p['msg']}" for p in problems] + \
                   ["Поправьте карточку и повторите; пока она остаётся proposed."]
    new_status = "rejected" if reject else "accepted"
    event = (f"- {today}: отклонено ({who}) — причина: {' '.join(reason.split())}" if reject
             else f"- {today}: принято ({who})")
    fields = {"status": new_status, "date": today, "updated": today,
              "accepted_fp": body_fingerprint(text)}
    import card_types as CT
    try:
        new = add_history(with_fields(text, fields), event)
        CT.write_card_text(path, new, op="body")
    except CT.TemplateViolation as e:
        return 1, [f"kb_decide: шаблон решения не принял запись: {e}"]
    return 0, [f"{name}: {status} → {new_status} ({who}, {today}); отпечаток тела записан — дальше решение "
               "меняется только заменой (`kb:supersede`)."]


def reseal(was: str, now: str, why: str) -> str:
    """Текст `now` (правка движка, не смысла: обезличивание) с отпечатком, пересчитанным по новому телу.

    Только если принятое решение до правки было целым: отпечаток сходился. Уже изменённое тело не «отмывается»."""
    stamp = (frontmatter(was).get("accepted_fp") or "").strip().strip("\"'")
    if not stamp or body_changed(was) or not frontmatter(now).get("accepted_fp"):
        return now
    out = with_fields(now, {"accepted_fp": body_fingerprint(now)})
    return add_history(out, f"- {utc_today()}: {why}, отпечаток тела пересчитан")


# ------------------------------------------------------------------ журнал

def read_decision(path: str, archived: bool, known: set, today: str) -> dict:
    with open(path, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    fm = frontmatter(text)
    name = os.path.splitext(os.path.basename(path))[0]
    status = (fm.get("status") or "").strip()
    since = (fm.get("date") or fm.get("created") or "").strip().strip("\"'")[:10]
    age = None
    if re.match(r"^\d{4}-\d\d-\d\d$", since) and re.match(r"^\d{4}-\d\d-\d\d$", today):
        from datetime import date
        try:
            age = (date.fromisoformat(today) - date.fromisoformat(since)).days
        except ValueError:
            age = None
    return {"id": f"DR-{dr_number(name):04d}", "name": name, "status": status, "date": since,
            "owner": (fm.get("owner") or "").strip().strip("\"'"), "archived": archived,
            "superseded_by": (fm.get("superseded_by") or "").strip().strip("\"'[]"),
            "affects": affected(card_body(text)), "age_days": age,
            "body_changed": body_changed(text),
            "form": ([p["id"] for p in lint_decision(text, known)]
                     if status in ("proposed", "accepted") and not archived else [])}


def journal(root: str = ".", today: str = "", stale_days: int = STALE_DAYS) -> dict:
    today = today or utc_today()
    known = known_names(root)
    rows = [read_decision(p, a, known, today) for p, a in decision_files(root)]
    rows.sort(key=lambda r: (dr_number(r["name"]), r["name"]))
    by_status: dict = {}
    for r in rows:
        by_status[r["status"] or "—"] = by_status.get(r["status"] or "—", 0) + 1
    return {"decisions": rows, "by_status": dict(sorted(by_status.items())),
            "stale": [r["id"] for r in rows if r["status"] == "proposed" and (r["age_days"] or 0) > stale_days],
            "body_changed": [r["id"] for r in rows if r["body_changed"]],
            "form_problems": [r["id"] for r in rows if r["form"]],
            "stale_days": stale_days}


def render_journal(j: dict) -> str:
    rows = j["decisions"]
    L = [f"# Журнал решений — {len(rows)}", ""]
    if not rows:
        return "\n".join(L + ["Решений пока нет. Первое предлагает `agent:decide` или карточка по "
                              "`Templates/decision_record_template.md`.", ""])
    L += ["Состояния: " + ", ".join(f"{k} {v}" for k, v in j["by_status"].items()), "",
          "| Решение | Состояние | Дата | Владелец | Затрагивает | Замечания |", "|---|---|---|---|---|---|"]
    for r in rows:
        mark = " (архив)" if r["archived"] else ""
        note = ", ".join(filter(None, [("тело изменено после принятия" if r["body_changed"] else ""),
                                       ("форма: " + " ".join(r["form"]) if r["form"] else "")]))
        L.append(f"| [[{r['name']}]]{mark} | {r['status'] or '—'} | {r['date'] or '—'} | {r['owner'] or '—'} | "
                 f"{len(r['affects'])} | {note or '—'} |")
    L.append("")
    if j["stale"]:
        L += [f"Предложены больше {j['stale_days']} дней назад и не приняты: " + ", ".join(j["stale"]) +
              " — принять (`kb:decide-accept`) или отклонить с причиной.", ""]
    if j["body_changed"]:
        L += ["Тело изменено после принятия: " + ", ".join(j["body_changed"]) +
              " — верните прежний текст или заведите новое решение, заменяющее это (`kb:supersede`).", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Журнал решений: принять, отклонить, проверить, показать")
    ap.add_argument("decision", nargs="?", default="", metavar="DR",
                    help="решение (DR-0042, 42 или имя файла): принять или отклонить")
    ap.add_argument("--by", default="", metavar="@ИМЯ", help="кто принял (человек; по умолчанию git user.name)")
    ap.add_argument("--reject", action="store_true", help="отклонить вместо принятия (нужен --reason)")
    ap.add_argument("--reason", default="", metavar="ТЕКСТ", help="причина отклонения")
    ap.add_argument("--journal", action="store_true", help="журнал решений: состояния, просроченные, изменённые")
    ap.add_argument("--check", action="store_true", help="форма каждого решения (код 1 — есть замечания)")
    ap.add_argument("--stale-days", type=int, default=STALE_DAYS, metavar="N",
                    help=f"сколько дней предложенное решение ждёт принятия (по умолчанию {STALE_DAYS})")
    ap.add_argument("--json", action="store_true", help="машинный вывод журнала")
    a = ap.parse_args()
    a.root = "."                      # скрипт запускают из корня проекта, как остальные

    if a.journal or a.check:
        j = journal(a.root, stale_days=a.stale_days)
        if a.json:
            print(json.dumps(j, ensure_ascii=False, indent=1))
        elif a.check:
            bad = [r for r in j["decisions"] if r["form"] or r["body_changed"]]
            print(f"Решений {len(j['decisions'])}, с замечаниями {len(bad)}")
            for r in bad:
                print(f"- {r['name']} ({r['status']}): " + ", ".join(
                    r["form"] + (["тело изменено после принятия"] if r["body_changed"] else [])))
        else:
            print(render_journal(j))
        if a.check:
            return 1 if any(r["form"] or r["body_changed"] for r in j["decisions"]) else 0
        return 0
    if not a.decision:
        ap.error("нужно решение (DR-0042), --journal или --check")
    code, lines = decide(a.root, a.decision, a.by, a.reject, a.reason)
    print("\n".join(lines), file=sys.stdout if code == 0 else sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
