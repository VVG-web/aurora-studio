#!/usr/bin/env python3
"""agent_runner.py — агентский цикл: задача, шаги, оракул, журнал.

Фаза 2 встроенного агента. Транспорт и цепочку моделей даёт `agent_core`, здесь — то,
ради чего всё затевалось: агент доводит задачу до конца сам, а достижение цели проверяет
не он, а команда движка.

  python3 .opencode/scripts/agent_runner.py --task aliases          # что будет сделано
  python3 .opencode/scripts/agent_runner.py --task aliases --apply  # с записью в базу
  python3 .opencode/scripts/agent_runner.py --task aliases --apply --critic

Устройство цикла — три роли и никакой веры в самооценку модели:

    worker   предлагает решение по одному конфликту
    critic   (по флагу) проверяет предложение до записи — глитч в базе знаний бьёт
             по всей последующей разработке, поэтому в проде критик обязателен
    оракул   команда движка проверяет ФАКТ: kb:lint пересчитывает конфликты

Агент пишет в базу только через команды движка (белый список в `agent_core`): у них
dry-run, git-guard и журнал. Прямая правка файлов моделью запрещена конструкцией — LLM
умеет только перегенерировать файл целиком и вместе с одной строкой переписывает шапку,
теги и тело.

Перед прогоном — git-чекпойнт: текущая работа человека фиксируется отдельным коммитом,
и всё, что сделает агент, откатывается одной строкой. Без этого правки агента смешались
бы с незакоммиченной работой (в живом проекте её бывают сотни файлов).

Панель: `agent:aliases`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_core as AG  # noqa: E402

RUNS_DIR = Path("AuroraKnowledgeDB") / "meta" / "agent-runs"
SAME_FAIL_LIMIT = 3      # одна и та же команда с теми же аргументами: долбёжка в стену


# ------------------------------------------------------------------ git-чекпойнт

def git(*args: str, cwd: str = ".") -> tuple:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def checkpoint(cwd: str, task: str, enabled: bool) -> dict:
    """Зафиксировать работу человека до прогона агента.

    `git_guard` в командах движка просто отказывается писать по грязному дереву — для
    аналитика, который правит базу весь день, это значит «сначала закоммить сотню файлов».
    Здесь иначе: коммитим его работу как есть, отдельным коммитом, и дальше всё, что
    сделает агент, лежит поверх и снимается одной строкой.
    """
    rc, _out, _err = git("rev-parse", "--is-inside-work-tree", cwd=cwd)
    if rc != 0:
        return {"ok": False, "why": "проект не под git — отката не будет", "sha": ""}
    dirty = git("status", "--porcelain", cwd=cwd)[1]
    if not enabled:
        sha = git("rev-parse", "HEAD", cwd=cwd)[1]
        return {"ok": True, "sha": sha, "committed": 0,
                "why": "чекпойнт выключен" + (" — дерево грязное, откат будет ручным"
                                              if dirty else "")}
    if dirty:
        git("add", "-A", cwd=cwd)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        rc, _o, err = git("commit", "--no-verify", "-m",
                          f"checkpoint: перед агентом {task} · {stamp}", cwd=cwd)
        if rc != 0:
            return {"ok": False, "why": f"чекпойнт не создан: {err[:200]}", "sha": ""}
    sha = git("rev-parse", "HEAD", cwd=cwd)[1]
    return {"ok": True, "sha": sha, "committed": len(dirty.splitlines()),
            "why": "работа человека зафиксирована" if dirty else "дерево было чистым"}


# ------------------------------------------------------------------ вызов команд

def run_command(cwd: str, script: str, args: list, timeout: int = 300) -> dict:
    """Выполнить команду движка, если она разрешена агенту. → {ok, rc, out, refused}."""
    allowed, why = AG.write_allowed(script, args)
    if not allowed:
        return {"ok": False, "refused": why, "rc": None, "out": ""}
    path = os.path.join(cwd, ".opencode", "scripts", script)
    if not os.path.isfile(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script)
    p = subprocess.run([sys.executable, path, *args], cwd=cwd,
                       capture_output=True, text=True, timeout=timeout)
    return {"ok": p.returncode == 0, "rc": p.returncode,
            "out": ((p.stdout or "") + (p.stderr or "")).strip(), "refused": ""}


# ------------------------------------------------------------------ задача: синонимы

CONFLICT_RE = re.compile(r"^- «([^»]+)» → (.+)$", re.M)


def read_conflicts(cwd: str) -> list:
    """[(синоним, [карточки])] — из отчёта kb_fix --aliases. Читает движок, не модель."""
    r = run_command(cwd, "kb_fix.py", ["--aliases"])
    out = []
    for alias, cards in CONFLICT_RE.findall(r["out"]):
        out.append((alias, [c.strip() for c in cards.split(",") if c.strip()]))
    return out


def lint_conflicts(cwd: str) -> int:
    """Оракул: сколько конфликтов синонимов видит линтер. Проверяет факт, а не мнение."""
    r = run_command(cwd, "kb_lint.py", [])
    m = re.search(r"одинаковые alias у разных карточек:\s*(\d+)", r["out"])
    return int(m.group(1)) if m else 0


def lint_errors(cwd: str) -> int:
    r = run_command(cwd, "kb_lint.py", ["--summary"])
    m = re.search(r"ошибок (\d+)", r["out"])
    return int(m.group(1)) if m else -1


PROMPT_WORKER = """Ты разбираешь конфликт синонимов в базе знаний проекта.

Синоним «{alias}» принадлежит сразу нескольким карточкам, поэтому ссылка по нему
неоднозначна — движок не может выбрать, какая карточка имелась в виду.

Карточки (в скобках — точное имя, которым её надо называть в ответе):
{cards}

Твоё решение — одно из двух, и это главный выбор:

1. РАЗНЫЕ сущности (например, алгоритм и система, требование и экранная форма). Тогда
   уточни синоним у каждой карточки так, чтобы он отражал именно её. Уточнение должно
   быть осмысленным: «Курс валют ЦБ (сервис)» — годится, «SPR-001 (Statuses)» — нет,
   это маскировка названием папки, а не смысл.

2. ОДНА И ТА ЖЕ сущность, записанная дважды (одно определение, один справочник, один
   процесс — просто в разных разделах или на разных языках). Тогда НЕ выдумывай
   различий: это дубль, его должен слить человек командой kb:dedupe. Твоя работа —
   честно это назвать.

Ответь строго одним JSON-объектом, без пояснений вокруг:

{{"verdict": "distinct", "renames": [{{"card": "<имя карточки>", "new": "<новый синоним>"}}]}}
или
{{"verdict": "duplicate", "reason": "<чем карточки совпадают, одна фраза>"}}

Правила для renames: по одной записи на карточку, которой синоним нужен уточнённый;
в поле "card" копируй имя из скобок ДОСЛОВНО, ничего не сокращая и не переставляя;
новый синоним не должен совпадать с существующими у других карточек."""

PROMPT_CRITIC = """Ты проверяешь решение по конфликту синонимов в базе знаний, ДО записи.

Синоним: «{alias}»
Карточки:
{cards}

Предложенное решение:
{proposal}

Проверь три вещи:
1. Если предложено «distinct» — точно ли это разные сущности? Если карточки описывают
   одно и то же, решение неверно: это дубль.
2. Если предложены уточнения — осмысленны ли они? Суффикс раздела или технический
   префикс вместо смысла — плохое уточнение.
3. Если предложено «duplicate» — точно ли карточки об одном и том же?

Ответь строго одним JSON-объектом:
{{"ok": true}}
или
{{"ok": false, "why": "<что не так, одна фраза>", "better": "distinct|duplicate"}}"""


def parse_json(text: str) -> dict | None:
    """Достать JSON из ответа модели: она любит обрамлять его текстом или ```-оградой."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def solve_conflict(cfg: dict, cwd: str, alias: str, cards: list, apply: bool,
                   use_critic: bool, call=None, deadline: float | None = None) -> dict:
    """Разобрать один конфликт. → {status, note, backend, model, degraded}."""
    call = call or AG.call_role
    # Модель называет карточку так, как увидела её в списке, — поэтому точное имя даём
    # отдельно и просим копировать дословно: «Получение-курсов-валют» вместо
    # «ALG-309-Получение-курсов-валют» стоило одного молча несделанного шага.
    listing = "\n".join(f"- {c}  (точное имя: {c.rsplit('/', 1)[-1]})" for c in cards)
    step = {"alias": alias, "cards": cards, "status": "", "note": "",
            "backends": [], "degraded": False}

    r = call(cfg, "worker", [{"role": "user", "content":
                              PROMPT_WORKER.format(alias=alias, cards=listing)}],
             deadline=deadline)
    if not r["ok"]:
        step.update(status="сбой", note="; ".join(r["log"][-2:]))
        return step
    step["backends"].append((r["backend"], r["model"]))
    step["degraded"] = r["backend"] != 1
    proposal = parse_json(r["text"])
    if not proposal or "verdict" not in proposal:
        step.update(status="сбой", note="ответ модели не разобран как JSON")
        return step

    if use_critic:
        c = call(cfg, "critic", [{"role": "user", "content": PROMPT_CRITIC.format(
            alias=alias, cards=listing, proposal=json.dumps(proposal, ensure_ascii=False))}],
            deadline=deadline)
        if c["ok"]:
            step["backends"].append((c["backend"], c["model"]))
            step["degraded"] = step["degraded"] or c["backend"] != 1
            verdict = parse_json(c["text"]) or {}
            if verdict.get("ok") is False:
                step.update(status="отклонено критиком",
                            note=(verdict.get("why") or "критик не согласен")[:160])
                return step

    if proposal["verdict"] == "duplicate":
        step.update(status="дубль — человеку",
                    note=(proposal.get("reason") or "карточки об одном и том же")[:160])
        return step

    renames = [x for x in (proposal.get("renames") or [])
               if x.get("card") and x.get("new")]
    if not renames:
        step.update(status="сбой", note="verdict=distinct, но уточнений не предложено")
        return step

    done = []
    for item in renames:
        args = ["--set-alias", item["card"], "--old", alias, "--new", item["new"]]
        if apply:
            args.append("--apply")
            args.append("--allow-dirty")   # чекпойнт уже зафиксировал состояние
        res = run_command(cwd, "kb_fix.py", args)
        if res.get("refused"):
            step.update(status="сбой", note="команда отклонена: " + res["refused"])
            return step
        if not res["ok"]:
            step.update(status="сбой",
                        note=f"команда не выполнила правку: {res['out'][-160:]}")
            return step
        done.append(f"{item['card']} → «{item['new']}»")
    step.update(status="уточнено" if apply else "уточнил бы", note="; ".join(done)[:200])
    return step


# ------------------------------------------------------------------ прогон

def run_aliases(cfg: dict, cwd: str, apply: bool, use_critic: bool, limit: int,
                call=None) -> dict:
    started = time.time()
    budget = started + cfg["budget_min"] * 60
    before_conflicts, before_errors = lint_conflicts(cwd), lint_errors(cwd)
    conflicts = read_conflicts(cwd)
    if limit:
        conflicts = conflicts[:limit]

    steps, fails = [], {}
    for alias, cards in conflicts:
        if time.time() > budget:
            steps.append({"alias": alias, "status": "не начат", "note": "бюджет исчерпан",
                          "backends": [], "degraded": False})
            break
        if len(steps) >= cfg["max_steps"]:
            steps.append({"alias": alias, "status": "не начат",
                          "note": f"лимит шагов {cfg['max_steps']}", "backends": [],
                          "degraded": False})
            break
        step = solve_conflict(cfg, cwd, alias, cards, apply, use_critic, call=call,
                              deadline=min(budget, time.time() + cfg["request_timeout"]))
        steps.append(step)
        if step["status"] == "сбой":
            key = step["note"][:60]
            fails[key] = fails.get(key, 0) + 1
            if fails[key] >= SAME_FAIL_LIMIT:
                steps.append({"alias": "—", "status": "стоп",
                              "note": f"одна и та же ошибка {SAME_FAIL_LIMIT} раза подряд: {key}",
                              "backends": [], "degraded": False})
                break
            if cfg["debug"]:
                steps.append({"alias": "—", "status": "стоп",
                              "note": "AURORA_AGENT_DEBUG=1: стоп на первой ошибке",
                              "backends": [], "degraded": False})
                break

    after_conflicts = lint_conflicts(cwd) if apply else before_conflicts
    after_errors = lint_errors(cwd) if apply else before_errors
    return {"steps": steps, "seconds": round(time.time() - started, 1),
            "before": {"conflicts": before_conflicts, "errors": before_errors},
            "after": {"conflicts": after_conflicts, "errors": after_errors},
            "total_conflicts": len(conflicts)}


def verdict(res: dict, apply: bool) -> tuple:
    """Оракул прогона: разобран ли каждый конфликт и не выросли ли ошибки базы.

    Ноль конфликтов любой ценой — вредная цель: часть из них дубли, и агент, добиваясь
    нуля, начал бы выдумывать различия там, где карточки надо сливать. Поэтому успех —
    «каждый конфликт разобран»: уточнён или честно отложен человеку.
    """
    done = [s for s in res["steps"] if s["status"] in ("уточнено", "уточнил бы")]
    dup = [s for s in res["steps"] if s["status"] == "дубль — человеку"]
    bad = [s for s in res["steps"] if s["status"] in ("сбой", "отклонено критиком", "стоп",
                                                      "не начат")]
    grew = apply and res["after"]["errors"] > res["before"]["errors"]
    ok = not bad and not grew and (len(done) + len(dup)) == res["total_conflicts"]
    why = []
    if bad:
        why.append(f"не разобрано: {len(bad)}")
    if grew:
        why.append(f"ошибок в базе стало больше: {res['before']['errors']} → "
                   f"{res['after']['errors']}")
    return ok, "; ".join(why) or "каждый конфликт разобран, ошибок не прибавилось"


def report(res: dict, cp: dict, apply: bool, use_critic: bool, cfg: dict) -> str:
    ok, why = verdict(res, apply)
    degraded = [s for s in res["steps"] if s.get("degraded")]
    L = [f"# Агент · синонимы — {datetime.now():%Y-%m-%d %H:%M}", "",
         f"Режим: {'запись' if apply else 'предпросмотр'} · критик: "
         f"{'да' if use_critic else 'нет'} · адаптер: {cfg['adapter']}",
         f"Конфликтов в работе: {res['total_conflicts']} · время: {res['seconds']} с", ""]
    if cp.get("sha"):
        L += [f"Чекпойнт: `{cp['sha'][:8]}` — {cp['why']}"
              + (f", зафиксировано файлов: {cp['committed']}" if cp.get("committed") else ""),
              f"Откат всей работы агента: `git reset --hard {cp['sha'][:8]}`", ""]
    else:
        L += [f"⚠️ Чекпойнта нет: {cp.get('why', 'причина неизвестна')}", ""]

    L += ["| Синоним | Итог | Подробности |", "|---|---|---|"]
    for s in res["steps"]:
        L.append(f"| {s['alias'][:40]} | {s['status']} | {s['note'][:90]} |")

    L += ["", f"**Оракул:** {'✅ ' if ok else '✗ '}{why}",
          f"Конфликтов по линтеру: {res['before']['conflicts']} → {res['after']['conflicts']} · "
          f"ошибок базы: {res['before']['errors']} → {res['after']['errors']}"]
    if AG.ADAPTER.get("fallback_why"):
        L += ["", f"⚠️ Адаптер `{cfg['adapter']}` не сработал ({AG.ADAPTER['fallback_why']}) — "
              "работали на stdlib-транспорте. Проверьте venv: «Настройка» → «Агент»."]
    if degraded:
        L += ["", f"⚠️ **Частично на резервных моделях**: шагов {len(degraded)}. "
              "Их результат стоит перепроверить глазами — качество резервной модели ниже.",
              *[f"  - {s['alias']}: " + ", ".join(f"№{n} {m}" for n, m in s["backends"])
                for s in degraded]]
    dup = [s for s in res["steps"] if s["status"] == "дубль — человеку"]
    if dup:
        L += ["", "## Отложено человеку: дубли карточек", "",
              "Это не провал прогона: агент не имеет права сливать карточки — знание можно "
              "потерять. Разберите командой `kb:dedupe`.", ""]
        L += [f"- «{s['alias']}»: {s['note']}" for s in dup]
    if not apply:
        L += ["", "(предпросмотр) В базу ничего не записано. Повторите с `--apply`."]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Агентский цикл: задача, оракул, журнал")
    ap.add_argument("--task", default="aliases", choices=["aliases"],
                    help="что делать (пока одна задача — пилот)")
    ap.add_argument("--apply", action="store_true", help="записывать в базу (иначе предпросмотр)")
    ap.add_argument("--critic", action="store_true",
                    help="проверять решение второй моделью до записи (для прода — обязательно)")
    ap.add_argument("--limit", type=int, default=0, metavar="N",
                    help="взять только первые N конфликтов (для пробы)")
    ap.add_argument("--no-checkpoint", action="store_true",
                    help="не делать git-коммит перед прогоном (откат станет ручным)")
    a = ap.parse_args()

    cwd = os.getcwd()
    if not os.path.isdir(os.path.join(cwd, "AuroraKnowledgeDB")):
        print("agent_runner: нет AuroraKnowledgeDB/ — запускайте из корня проекта",
              file=sys.stderr)
        return 1
    cfg = AG.parse_config(AG.raw_config())
    if not cfg["backends"]:
        print("agent_runner: агент не настроен — панель «Настройка» → «Агент», "
              "проверка: agent:ping", file=sys.stderr)
        return 1

    cp = checkpoint(cwd, f"agent:{a.task}", a.apply and not a.no_checkpoint)
    if a.apply and not cp["ok"]:
        print(f"agent_runner: {cp['why']}. Записывать без отката нельзя — "
              "закоммитьте работу или запустите без --apply.", file=sys.stderr)
        return 1

    res = run_aliases(cfg, cwd, a.apply, a.critic, a.limit)
    text = report(res, cp, a.apply, a.critic, cfg)
    print(text)

    runs = Path(cwd) / RUNS_DIR
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    (runs / f"{stamp}_{a.task}.md").write_text(text + "\n", encoding="utf-8")
    print(f"\nЖурнал прогона: {RUNS_DIR}/{stamp}_{a.task}.md")
    return 0 if verdict(res, a.apply)[0] else 1


if __name__ == "__main__":
    sys.exit(main())
