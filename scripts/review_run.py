#!/usr/bin/env python3
"""review_run.py — автоматическое ревью истории или алгоритма по закрытому чек-листу («Аврора»).

Оценка выносится без человека. Модель не ставит баллы и не выбирает тяжесть дефекта: она
отвечает «да / нет / н/п / не определить» на вопросы чек-листа из шаблона
`review_v2.0.md`, а оценку и вердикт считает этот скрипт. Разброс LLM-оценщика
сидит в открытых суждениях; закрытые вопросы и голосование по нескольким прогонам его
снимают (CheckEval, Rating Roulette).

Два режима на одном движке:

  одна страница — независимая оценка и подсказки автору, что исправить:
    python3 .opencode/scripts/review_run.py --page <адрес или номер>            # отчёт в вывод
    python3 .opencode/scripts/review_run.py --page <адрес или номер> --apply    # и в Artifacts/reviews/

  пакет — ретроспектива: сотни страниц, сводка, продолжение после обрыва:
    python3 .opencode/scripts/review_run.py --cql 'space = X and title ~ "US-*"' --name retro-2025
    python3 .opencode/scripts/review_run.py --cql '...' --name retro-2025 --as-of 2025-12-31 --apply
    python3 .opencode/scripts/review_run.py --summary Artifacts/reviews/batch_retro-2025

Без `--apply` пакет ничего не вызывает: печатает, что будет оценено и сколько это вызовов
модели. Повторный запуск с тем же `--name` продолжает с места: оценённые страницы
той же версии не оцениваются заново.

Слои:
  0 — механика кодом: рабочие пометки, удалённые страницы, словарь расплывчатых слов;
  1 — чек-лист: `--runs` прогонов (по умолчанию 3), голосование по каждому вопросу,
      эскалация до пяти прогонов у порога и на неустойчивых вопросах, влияющих на вердикт;
  2 — свободные рекомендации модели (`--free`), на оценку не влияют.

Панель: `make:review-auto`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import csv
import html as html_lib
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TEMPLATE = "review_v2.0.md"
REVIEWS = "Artifacts/reviews"
CACHE = ".opencode/state/review_cache"   # служебное: закрыто .gitignore, в историю проекта не идёт
WEIGHT = {"критичный": 5, "важный": 3, "мелкий": 1}
SEV_KEY = {"критичный": "critical", "важный": "major", "мелкий": "minor"}
ANSWERS = ("yes", "no", "na", "unknown")
ALIASES = {"да": "yes", "нет": "no", "н/п": "na", "na": "na", "n/a": "na",
           "не определить": "unknown", "не определено": "unknown", "неизвестно": "unknown"}
GATE, BORDER_HI, RESTART_BELOW, COVERAGE_MIN = 9.0, 9.2, 5.0, 0.7
MAX_RUNS = 5
LINKED_CHARS = 12000          # сколько текста связанной страницы кладём в промпт
MANDATORY_HEAD = {
    "us": r"предуслов|acceptance|критери|сценари|use.?case|экранн|форм|макет",
    "alg": r"истори|user.?stor|контракт|маппинг|mapping|вызыва|источник",
}
SAME_FAIL_LIMIT = 3


# ------------------------------------------------------------------ шаблон и чек-лист

def find_template(explicit: str = "") -> str:
    """Шаблон ищется там же, где его найдёт человек: проект, затем кит."""
    if explicit:
        return explicit
    here = Path(__file__).resolve().parent
    for cand in (Path("TemplatesCommon") / TEMPLATE, Path("Templates") / TEMPLATE,
                 here.parent / "scaffold" / "TemplatesCommon" / TEMPLATE,
                 here.parent.parent / "TemplatesCommon" / TEMPLATE):
        if cand.is_file():
            return str(cand)
    return ""


def load_checklist(path: str) -> dict:
    """Шаблон → чек-лист, словарь слоя 0, правила для модели, версия.

    Чек-лист — markdown-таблицы в самом шаблоне: правка вопроса видна человеку в том же
    месте, где её читает скрипт, и не бывает двух расходящихся копий.
    """
    text = Path(path).read_text(encoding="utf-8")
    version = (re.search(r'^version:\s*"?([\d.]+)"?', text, re.M) or [None, "?"])[1]
    profiles: dict = {"us": [], "alg": []}
    for line in text.splitlines():
        m = re.match(r"^\|\s*((US|ALG)-\d+)\s*\|", line)
        if not m:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6 or cells[1] not in WEIGHT:
            continue
        profiles["us" if m.group(2) == "US" else "alg"].append({
            "id": cells[0], "severity": cells[1], "when": cells[2], "question": cells[3],
            "fix": cells[4], "decider": cells[5]})
    # «То же условие, что у ALG-13» — удобно человеку, но модели нужен сам текст условия.
    for prof in profiles.values():
        by_id = {c["id"]: c for c in prof}
        for c in prof:
            ref = re.match(r"то же условие, что у (\S+)", c["when"])
            if ref and ref.group(1) in by_id:
                c["when"] = by_id[ref.group(1)]["when"]
    lexicon: dict = {}
    lex = re.search(r"^## Словарь слоя 0[^\n]*\n(.*?)(?=^## )", text, re.S | re.M)
    if lex:
        for line in lex.group(1).splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 2 and cells[0] != "Группа" and not set(cells[0]) <= set("-: "):
                lexicon[cells[0]] = [w.strip() for w in cells[1].split(",") if w.strip()]
    rules = re.search(r"ИНСТРУКЦИЯ ДЛЯ ИИ\s*\n\s*=+\s*\n(.*?)\n\s*=+ -->", text, re.S)
    return {"version": version, "file": os.path.basename(path), "profiles": profiles,
            "lexicon": lexicon, "rules": rules.group(1).strip() if rules else ""}


# ------------------------------------------------------------------ Confluence

def html_to_text(view_html: str) -> str:
    """Представление страницы для просмотра → markdown. Макросы уже раскрыты сервером."""
    view_html = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", "", view_html or "")
    try:
        from markdownify import markdownify
        text = markdownify(view_html, heading_style="ATX")
    except ImportError:
        text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</tr>|</h\d>", "\n", view_html)
        text = html_lib.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def links_with_sections(view_html: str, base: str) -> list:
    """[(заголовок раздела, вид, цель)] — ссылки страницы в порядке документа.

    Вид: `id` — номер страницы, `title` — пространство и заголовок, `planned` — ссылка на
    ещё не созданную страницу (Confluence рисует её как «создать»): это предусловие
    «сделать», а не битая ссылка.
    """
    out, heading = [], ""
    for m in re.finditer(r"(?is)<(h[1-6])[^>]*>(.*?)</\1>|<a\s([^>]*)>", view_html or ""):
        if m.group(1):
            heading = html_lib.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
            continue
        attrs = m.group(3)
        href = html_lib.unescape((re.search(r'href="([^"]*)"', attrs) or [None, ""])[1])
        if not href or (href.startswith("http") and not href.startswith(base)):
            continue
        # Не артефакты: версии самой страницы из макроса «История изменений» («v. 20»),
        # профили людей и личные пространства. Первый боевой прогон принял ссылки на
        # старые версии за четыре «удалённые страницы».
        if ("historical" in attrs or "viewuserprofile" in href or "/display/~" in href
                or "viewpreviousversions" in href or "diffpages" in href):
            continue
        if "createpage.action" in href or "createlink" in attrs:
            out.append((heading, "planned", href))
        elif (pid := re.search(r"pageId=(\d+)", href)):
            out.append((heading, "id", pid.group(1)))
        elif (dm := re.search(r"/display/([^/?#]+)/([^?#]+)", href)):
            title = urllib.parse.unquote(dm.group(2)).replace("+", " ")
            out.append((heading, "title", f"{dm.group(1)}/{title}"))
    return out


class Reader:
    """Чтение страниц с кэшем и срезом «по состоянию на дату».

    Кэш по номеру и версии: в ретроспективе одну и ту же экранную форму ссылают десятки
    историй, и читать её каждый раз — часы лишних запросов.
    """

    def __init__(self, api, base: str, cache_dir: str, as_of: str = ""):
        self.api, self.base, self.as_of = api, base.rstrip("/"), as_of
        self.cache = Path(cache_dir)
        self.lock = threading.Lock()
        self.titles: dict = {}

    def _get(self, path: str):
        """→ (данные, статус): ok | missing | forbidden | error."""
        try:
            return self.api.get(path), "ok"
        except urllib.error.HTTPError as e:
            return None, {404: "missing", 401: "forbidden", 403: "forbidden"}.get(e.code, "error")
        except Exception:  # noqa: BLE001 — сеть: ответ страницы не получен
            return None, "error"

    def resolve(self, kind: str, target: str) -> tuple:
        """Цель ссылки → (номер страницы, статус)."""
        if kind == "id":
            return target, "ok"
        with self.lock:
            if target in self.titles:
                return self.titles[target]
        space, _, title = target.partition("/")
        data, st = self._get(f"/rest/api/content?spaceKey={urllib.parse.quote(space)}"
                             f"&title={urllib.parse.quote(title)}&limit=1")
        hits = (data or {}).get("results", []) if st == "ok" else []
        res = (hits[0]["id"], "ok") if hits else ("", "missing" if st == "ok" else st)
        with self.lock:
            self.titles[target] = res
        return res

    def version_at(self, page_id: str) -> tuple:
        """Номер версии, действовавшей на дату `as_of`. → (номер|0 — текущая, статус)."""
        if not self.as_of:
            return 0, "ok"
        limit_ts = self.as_of + "T23:59:59"
        best, start = None, 0
        while True:
            data, st = self._get(f"/rest/experimental/content/{page_id}/version"
                                 f"?limit=100&start={start}")
            if st != "ok":
                return 0, "as_of_unavailable"
            rows = data.get("results", [])
            for r in rows:
                if (r.get("when") or "")[:19] <= limit_ts:
                    if best is None or r["number"] > best:
                        best = r["number"]
            if len(rows) < 100:
                break
            start += 100
        return (best, "ok") if best else (0, "not_existed")

    def page(self, page_id: str) -> dict:
        """Страница целиком: текст для модели, ссылки, версия, автор. Со статусом чтения."""
        number, vst = self.version_at(page_id)
        if vst == "not_existed":
            return {"id": page_id, "status": "not_existed"}
        # Кэш по номеру и версии. Текущую версию заранее не знаем — её читаем всегда;
        # историческая неизменна, и её достаточно прочитать один раз на весь пакет.
        key = self.cache / f"{page_id}_{number}.json"
        if number and key.is_file():
            return json.loads(key.read_text(encoding="utf-8"))
        expand = "body.view,version,space,history"
        path = (f"/rest/api/content/{page_id}?status=historical&version={number}&expand={expand}"
                if number else f"/rest/api/content/{page_id}?expand={expand}")
        data, st = self._get(path)
        if st != "ok":
            # 404 бывает и у удалённой, и у закрытой правами страницы — различить нельзя.
            return {"id": page_id, "status": "unavailable" if st == "missing" else st}
        view = ((data.get("body") or {}).get("view") or {}).get("value", "")
        hist = data.get("history") or {}
        ver = data.get("version") or {}
        page = {
            "id": str(data.get("id", page_id)), "status": "ok",
            "title": data.get("title", ""),
            "space": (data.get("space") or {}).get("key", ""),
            "version": ver.get("number", number or 0),
            "modified": (ver.get("when") or "")[:10],
            "version_by": ((ver.get("by") or {}).get("displayName") or ""),
            "created": (hist.get("createdDate") or "")[:10],
            "author": ((hist.get("createdBy") or {}).get("displayName") or ""),
            "url": f"{self.base}/pages/viewpage.action?pageId={data.get('id', page_id)}",
            "as_of_status": vst,
            "text": html_to_text(view),
            "links": links_with_sections(view, self.base),
        }
        self.cache.mkdir(parents=True, exist_ok=True)
        key = self.cache / f"{page['id']}_{page['version']}.json"
        key.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
        return page


def gather(reader: Reader, root: dict, profile: str, depth: int, max_pages: int) -> list:
    """Связанные страницы: до `depth` переходов, не больше `max_pages`.

    Обязательные (предусловия, критерии, сценарии, формы для истории; история, контракт,
    маппинг для алгоритма) читаются первыми и помечаются: не прочитав их, ревью не имеет
    права пропустить постановку.
    """
    head_re = re.compile(MANDATORY_HEAD.get(profile, "$^"), re.I)
    seen, out = {root["id"]}, []
    frontier = [(1, h, k, t) for h, k, t in root.get("links", [])]
    frontier.sort(key=lambda x: (x[0], not head_re.search(x[1] or "")))
    while frontier and len(out) < max_pages:
        hop, heading, kind, target = frontier.pop(0)
        mandatory = hop == 1 and bool(head_re.search(heading or ""))
        if kind == "planned":
            out.append({"hop": hop, "section": heading, "target": target, "mandatory": False,
                        "status": "planned", "title": urllib.parse.unquote(
                            (re.search(r"title=([^&]+)", target) or [None, target])[1]).replace("+", " ")})
            continue
        pid, st = reader.resolve(kind, target)
        if st != "ok":
            out.append({"hop": hop, "section": heading, "target": target, "mandatory": mandatory,
                        "status": "planned" if kind == "title" and st == "missing" else st,
                        "title": target.partition("/")[2] or target})
            continue
        if pid in seen:
            continue
        seen.add(pid)
        page = reader.page(pid)
        page.update(hop=hop, section=heading, mandatory=mandatory, target=target)
        out.append(page)
        if page.get("status") == "ok" and hop < depth:
            for h, k, t in page.get("links", []):
                frontier.append((hop + 1, h, k, t))
    return out


# ------------------------------------------------------------------ слой 0

def layer0(text: str, lexicon: dict, linked: list) -> dict:
    """Механика кодом: кандидаты в неоднозначность, рабочие пометки, удалённые страницы."""
    cands, marks = [], []
    for group, words in lexicon.items():
        for w in words:
            if w in ("{{", "}}", "???"):
                rx = re.compile(re.escape(w))
            else:
                rx = re.compile(r"(?<![\w])" + re.escape(w) + r"(?![\w])", re.I)
            for m in rx.finditer(text):
                snip = text[max(0, m.start() - 60): m.end() + 60].replace("\n", " ")
                (marks if group == "Рабочие пометки" else cands).append(
                    {"group": group, "word": w, "snippet": snip.strip()})
    return {"candidates": cands[:40], "marks": marks[:10]}


# ------------------------------------------------------------------ модель

def build_prompt(cl: dict, profile: str, page: dict, linked: list, l0: dict) -> tuple:
    """→ (системный текст, тело для пользователя). Тело режется под окно бэкенда."""
    rows = "\n".join(f"- {c['id']} [{c['severity']}] применимо, если: {c['when']}. "
                     f"Вопрос: {c['question']}"
                     for c in cl["profiles"][profile] if c["decider"] != "код")
    system = (f"{cl['rules']}\n\nПРОФИЛЬ: {profile}\nВОПРОСЫ ЧЕК-ЛИСТА:\n{rows}\n\n"
              "Ответь строго одним JSON-объектом по форме из раздела 3. Никакого текста вне JSON.")
    parts = []
    if l0["candidates"]:
        parts.append("СЛОЙ 0 — кандидаты в неоднозначность (решай сам, меняют ли они поведение):\n"
                     + "\n".join(f"- «{c['word']}»: …{c['snippet']}…" for c in l0["candidates"]))
    parts.append(f"ПРОВЕРЯЕМЫЙ АРТЕФАКТ: {page.get('title')} ({page.get('url')}), "
                 f"версия {page.get('version')}\n\n{page.get('text', '')}")
    for ln in linked:
        head = (f"СВЯЗАННАЯ СТРАНИЦА (переход {ln['hop']}, раздел «{ln.get('section', '')}», "
                f"{'обязательная' if ln.get('mandatory') else 'контекст'}): {ln.get('title', '')}")
        if ln.get("status") == "ok":
            room = (LINKED_CHARS if ln.get("mandatory") else
                    LINKED_CHARS // 2 if ln["hop"] == 1 else LINKED_CHARS // 4)
            parts.append(f"{head}\n\n{ln.get('text', '')[:room]}")
        elif ln.get("status") == "planned":
            parts.append(f"{head}\n\n[страница ещё не создана — предусловие «сделать», не дефект]")
        else:
            parts.append(f"{head}\n\n[не прочитана: страница недоступна — удалена или закрыта "
                         f"правами, по API их не отличить. Не считай это дефектом автора]")
    return system, "\n\n---\n\n".join(parts)


def parse_answer(text: str, ids: list) -> dict | None:
    """Ответ модели → {checks: {id: {v, evidence, fix}}, split}. Мусор → None."""
    if not text:
        return None
    body = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        return None
    raw = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    checks = {}
    for cid in ids:
        item = raw.get(cid) or {}
        if isinstance(item, str):
            item = {"v": item}
        v = str(item.get("v") or item.get("verdict") or "unknown").strip().lower()
        v = ALIASES.get(v, v)
        if v not in ANSWERS:
            v = "unknown"
        ev = str(item.get("evidence") or "").strip()
        if v == "no" and not ev:
            v = "unknown"          # «нет» без доказательства не принимается
        checks[cid] = {"v": v, "evidence": ev, "fix": str(item.get("fix") or "").strip()}
    return {"checks": checks, "split": str(data.get("split") or "").strip()}


def one_run(cfg: dict, cl: dict, profile: str, system: str, body: str, call, prefer: int,
            deadline: float) -> dict:
    ids = [c["id"] for c in cl["profiles"][profile] if c["decider"] != "код"]

    def build(part: str) -> list:
        return [{"role": "system", "content": system}, {"role": "user", "content": part}]

    r = call(cfg, "qa", build(body), trim=(body, build), prefer=prefer, deadline=deadline)
    if not r.get("ok"):
        return {"ok": False, "why": "; ".join((r.get("log") or [])[-2:])[:300]}
    parsed = parse_answer(r.get("text", ""), ids)
    if parsed is None:
        return {"ok": False, "why": "ответ модели не разобран как JSON"}
    parsed.update(ok=True, model=r.get("model", ""), seen=r.get("seen", 0), cut=r.get("cut", 0))
    return parsed


# ------------------------------------------------------------------ голосование и счёт

def vote(cl: dict, profile: str, runs: list, l0: dict) -> dict:
    """Ответы прогонов → итоговый ответ на каждый вопрос, с голосами и устойчивостью.

    Голосуют «лагери», а не буквы ответа: `no` — дефект; `yes` и `na` — дефекта нет (оба
    ответа значат одно для оценки, и их расхождение не повод звать лишний прогон);
    `unknown` — воздержался. Решают только определившиеся: воздержание не перевешивает
    двух согласных. Первый боевой прогон показал, зачем это нужно: при чётном числе
    годных прогонов ничья 2:2 превращалась в «не определено» на восьми вопросах из 23.
    """
    good = [r for r in runs if r.get("ok")]
    out = {}
    for c in cl["profiles"][profile]:
        cid = c["id"]
        if c["decider"] == "код":
            hit = l0["marks"]
            ev = "; ".join(f"«{m['word']}»: …{m['snippet'][:80]}…" for m in hit[:3])
            out[cid] = {"v": "no" if hit else "yes", "evidence": ev if hit else "",
                        "fix": c["fix"], "votes": "код", "stable": True, "tie": False, "all": []}
            continue
        answers = [r["checks"].get(cid, {"v": "unknown"}) for r in good]
        seq = [a["v"] for a in answers]
        fail = sum(1 for v in seq if v == "no")
        ok_n = sum(1 for v in seq if v in ("yes", "na"))
        tie = False
        if fail + ok_n < 2:
            v = "unknown"
        elif fail > ok_n:
            v = "no"
        elif ok_n > fail:
            y, n = seq.count("yes"), seq.count("na")
            v = "na" if n > y else "yes"
        else:
            v, tie = "unknown", True
        if v == "na" and c["when"].strip().lower() == "всегда":
            v = "yes" if seq.count("yes") else "unknown"
        agree = [a for a in answers if a["v"] == v]
        ev = next((a["evidence"] for a in agree if a["evidence"]), "")
        fix = next((a["fix"] for a in agree if a["fix"]), "") or (c["fix"] if v == "no" else "")
        blocs = {"no" if x == "no" else "pass" if x in ("yes", "na") else "?" for x in seq}
        side = fail if v == "no" else ok_n
        out[cid] = {"v": v, "evidence": ev, "fix": fix,
                    "votes": f"{side}/{len(seq)}" if seq else "0/0",
                    "stable": len(blocs) <= 1, "tie": tie, "all": seq}
    return out


def score(cl: dict, profile: str, answers: dict, incomplete: bool, read_ok: bool,
          good_runs: int) -> dict:
    """Ответы → итог, покрытие, вердикт. Арифметика кодом, по правилам шаблона."""
    checks = {c["id"]: c for c in cl["profiles"][profile]}
    w = lambda cid: WEIGHT[checks[cid]["severity"]]  # noqa: E731
    yes = sum(w(i) for i, a in answers.items() if a["v"] == "yes")
    det = sum(w(i) for i, a in answers.items() if a["v"] in ("yes", "no"))
    app = sum(w(i) for i, a in answers.items() if a["v"] in ("yes", "no", "unknown"))
    value = math.floor(10 * yes / det * 10 + 1e-9) / 10 if det else 0.0
    coverage = det / app if app else 0.0
    failed = {"critical": 0, "major": 0, "minor": 0}
    unknown_gate = []
    for i, a in answers.items():
        sev = checks[i]["severity"]
        if a["v"] == "no":
            failed[SEV_KEY[sev]] += 1
        if a["v"] == "unknown" and sev in ("критичный", "важный"):
            unknown_gate.append(i)
    reasons = []
    if not read_ok or not good_runs or coverage < COVERAGE_MIN:
        verdict = "не оценено"
        reasons.append("страница не прочитана" if not read_ok else
                       "нет годного прогона модели" if not good_runs else
                       f"покрытие {coverage:.0%} ниже {COVERAGE_MIN:.0%}")
    elif value < RESTART_BELOW:
        verdict = "переписать"
    elif failed["critical"] or failed["major"]:
        verdict = "доработать"
        reasons.append(f"не пройдено: критичных {failed['critical']}, важных {failed['major']}")
    elif unknown_gate or incomplete:
        verdict = "доработать"
        if unknown_gate:
            reasons.append("не определено: " + ", ".join(unknown_gate))
        if incomplete:
            reasons.append("не прочитана обязательная связанная страница")
    elif value < GATE:
        verdict = "доработать"
    else:
        verdict = "готова к передаче"
    return {"score": value, "coverage": round(coverage, 3), "verdict": verdict,
            "reasons": reasons, "failed": failed,
            "unknown": sum(1 for a in answers.values() if a["v"] == "unknown"),
            "unstable": sum(1 for a in answers.values() if not a["stable"])}


def needs_more(cl: dict, profile: str, answers: dict, res: dict) -> bool:
    """Эскалация: исход держится на спорном вопросе или итог у самого порога.

    Спорный — критичный или важный вопрос, где определившиеся разошлись между «дефект» и
    «дефекта нет», либо ничья. Воздержания сами по себе прогон не зовут.
    """
    if res["verdict"] == "готова к передаче" and res["score"] <= BORDER_HI:
        return True
    sev = {c["id"]: c["severity"] for c in cl["profiles"][profile]}
    for i, a in answers.items():
        if sev[i] not in ("критичный", "важный") or a["votes"] == "код":
            continue
        split = "no" in a["all"] and any(x in ("yes", "na") for x in a["all"])
        if split or a.get("tie"):
            return True
    return False


# ------------------------------------------------------------------ одна страница

def free_recs(cfg: dict, call, system_rules: str, body: str, prefer: int, deadline: float) -> list:
    """Слой 2: рекомендации вне чек-листа. На оценку не влияют."""
    system = (system_rules + "\n\nЗАДАЧА: дай до 5 рекомендаций по ПРОВЕРЯЕМОМУ АРТЕФАКТУ, "
              "которых нет в вопросах чек-листа. Правила: только пробелы в том, что артефакт уже "
              "заявляет, — не предлагай новых требований и функций, которых нет в постановке; "
              "не давай рекомендаций по связанным страницам; не выдумывай коды ошибок, числа, "
              "сроки и значения — если нужны, пиши «указать ...»; стиль не трогай. Нечего "
              "сказать — пустой массив. Ответ — JSON-массив "
              '[{"where": "...", "what": "...", "fix": "..."}], без текста вне JSON.')

    def build(part: str) -> list:
        return [{"role": "system", "content": system}, {"role": "user", "content": part}]

    r = call(cfg, "qa", build(body), trim=(body, build), prefer=prefer, deadline=deadline)
    if not r.get("ok"):
        return []
    text = re.sub(r"^```(?:json)?|```$", "", (r.get("text") or "").strip(), flags=re.M)
    s, e = text.find("["), text.rfind("]")
    try:
        items = json.loads(text[s:e + 1]) if s >= 0 < e else []
    except json.JSONDecodeError:
        return []
    return [i for i in items if isinstance(i, dict)][:5]


def detect_profile(title: str, forced: str) -> str:
    if forced and forced != "auto":
        return forced
    t = (title or "").strip().upper()
    if re.match(r"^\[?US[-\s.]", t):
        return "us"
    if re.match(r"^\[?ALG[-\s.]", t) or "АЛГОРИТМ" in t:
        return "alg"
    return ""


def code_of(page: dict) -> str:
    m = re.match(r"^\[?((?:US|ALG)[-\s.]?[\d.]*\d)", (page.get("title") or "").strip(), re.I)
    return re.sub(r"\s", "-", m.group(1).upper()) if m else f"page-{page.get('id')}"


def review_page(cfg: dict, cl: dict, reader: Reader, page_id: str, *, profile: str = "auto",
                runs: int = 3, depth: int = 2, max_pages: int = 15, free: bool = False,
                call=None, prefer: int = 0, request_timeout: float = 0,
                slots: list | None = None) -> dict:
    """Полное ревью одной страницы → запись результата (всё, что нужно отчёту и сводке)."""
    call = call or _default_call()
    started = time.time()
    page = reader.page(page_id)
    rec = {"page_id": page_id, "template": cl["file"], "template_version": cl["version"],
           "as_of": reader.as_of or "", "reviewed": _now()}
    if page.get("status") != "ok":
        rec.update(status=page.get("status"), verdict="не оценено" if page.get("status") !=
                   "not_existed" else "не существовала", reasons=[f"страница: {page.get('status')}"])
        return rec
    prof = detect_profile(page["title"], profile)
    rec.update({k: page.get(k, "") for k in ("title", "url", "version", "author", "version_by",
                                              "created", "modified", "space")})
    rec.update(code=code_of(page), profile=prof)
    if not prof:
        rec.update(status="skipped", verdict="не оценено",
                   reasons=["профиль не определён по заголовку — задайте --profile"])
        return rec
    linked = gather(reader, page, prof, depth, max_pages)
    incomplete = any(ln.get("mandatory") and ln.get("status") not in ("ok", "planned")
                     for ln in linked)
    l0 = layer0(page["text"], cl["lexicon"], linked)
    system, body = build_prompt(cl, prof, page, linked, l0)
    deadline = lambda: time.time() + (request_timeout or cfg["request_timeout"])  # noqa: E731
    main_len = len(body.split("ПРОВЕРЯЕМЫЙ АРТЕФАКТ", 1)[0]) + len(page["text"]) + 200
    def batch_of(n: int) -> list:
        if slots and len(slots) > 1 and n > 1:
            # Одиночное ревью: прогоны независимы, и человек не должен ждать их по очереди.
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(n, len(slots))) as ex:
                return list(ex.map(lambda i: one_run(cfg, cl, prof, system, body, call,
                                                     slots[i % len(slots)], deadline()),
                                   range(n)))
        return [one_run(cfg, cl, prof, system, body, call, prefer, deadline()) for _ in range(n)]

    # Нужны именно годные прогоны: сломавшийся не голосует, и его место занимает новый.
    # Попыток — не больше двух на прогон, иначе молчащий шлюз съест весь бюджет.
    results: list = []
    attempts = 0
    while sum(1 for r in results if r.get("ok")) < runs and attempts < runs * 2:
        need = runs - sum(1 for r in results if r.get("ok"))
        results += batch_of(need)
        attempts += need
    why = [r.get("why", "") for r in results if not r.get("ok")]
    answers = vote(cl, prof, results, l0)
    good = [r for r in results if r.get("ok")]
    cut_main = any(r.get("cut") and r.get("seen", 0) < main_len for r in good)
    res = score(cl, prof, answers, incomplete or cut_main, True, len(good))
    while (good and len(good) < MAX_RUNS and attempts < MAX_RUNS * 2
           and needs_more(cl, prof, answers, res)):
        results += batch_of(1)
        attempts += 1
        good = [x for x in results if x.get("ok")]
        answers = vote(cl, prof, results, l0)
        res = score(cl, prof, answers, incomplete or cut_main, True, len(good))
    rec.update(res)
    rec.update(status="ok", runs=len(results), good_runs=len(good), run_errors=why[:3],
               model=next((r.get("model") for r in good if r.get("model")), ""),
               incomplete=bool(incomplete or cut_main), context_cut=bool(cut_main),
               answers=answers,
               split=(next((r["split"] for r in good if r.get("split")), "")
                      if any(answers.get(k, {}).get("v") == "no" for k in ("US-18", "ALG-19"))
                      else ""),
               linked=[{k: ln.get(k) for k in ("hop", "title", "url", "section", "mandatory",
                                                 "status", "target")} for ln in linked],
               marks=l0["marks"], seconds=round(time.time() - started, 1))
    if free and good:
        rec["free"] = free_recs(cfg, call, cl["rules"], body, prefer, deadline())
    return rec


# ------------------------------------------------------------------ отчёт

def _now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")


def fmt(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def render(rec: dict, cl: dict) -> str:
    """Запись результата → отчёт по форме шаблона."""
    checks = {c["id"]: c for c in cl["profiles"].get(rec.get("profile") or "us", [])}
    f = rec.get("failed") or {"critical": 0, "major": 0, "minor": 0}
    head = ["---", "type: review", f"profile: {rec.get('profile', '')}",
            f"object: {json.dumps(rec.get('title', ''), ensure_ascii=False)}",
            f"object_link: \"{rec.get('url', '')}\"", f"object_version: {rec.get('version', '')}",
            f"as_of: {rec.get('as_of') or 'текущая'}",
            f"author: {json.dumps(rec.get('author', ''), ensure_ascii=False)}",
            f"reviewer: {json.dumps(rec.get('model', ''), ensure_ascii=False)}",
            f"reviewed: {rec.get('reviewed', '')}", f"template: {cl['file']}",
            f"runs: {rec.get('runs', 0)}", f"score: {rec.get('score', 0)}",
            f"coverage: {rec.get('coverage', 0)}",
            f"verdict: \"{rec.get('verdict', '')}\"",
            f"failed: {{critical: {f['critical']}, major: {f['major']}, minor: {f['minor']}}}",
            f"unstable: {rec.get('unstable', 0)}",
            f"incomplete: {str(bool(rec.get('incomplete'))).lower()}", "---", ""]
    L = head + [f"# Ревью {rec.get('title') or rec.get('page_id')}", "", "## Итог", "", "| | |",
                "|---|---|", f"| **Вердикт** | {rec.get('verdict', '').capitalize()} |"]
    if rec.get("status") != "ok":
        L += [f"| Причина | {'; '.join(rec.get('reasons') or [])} |",
              f"| Версия шаблона ревью | {cl['version']} (`{cl['file']}`) |", ""]
        return "\n".join(L)
    total = len(rec.get("answers") or {})
    L += [f"| **Итоговая оценка** | **{fmt(rec['score'])} из 10** |",
          f"| Не пройдено | критичных {f['critical']}, важных {f['major']}, мелких {f['minor']} |",
          f"| Не определено | {rec.get('unknown', 0)} |",
          f"| Неустойчивых вопросов | {rec.get('unstable', 0)} из {total} |",
          f"| Покрытие | {rec.get('coverage', 0):.0%} |",
          f"| Прогонов | {rec.get('runs', 0)} (годных {rec.get('good_runs', 0)}) |",
          f"| Версия шаблона ревью | {cl['version']} (`{cl['file']}`) |",
          f"| Версия страницы | {rec.get('version')}"
          + (f" (по состоянию на {rec['as_of']})" if rec.get("as_of") else "") + " |"]
    if rec.get("reasons"):
        L.append(f"| Почему такой вердикт | {'; '.join(rec['reasons'])} |")
    L.append("")
    order = {"критичный": 0, "важный": 1, "мелкий": 2}
    fails = sorted((i for i, a in rec["answers"].items() if a["v"] == "no"),
                   key=lambda i: (order[checks[i]["severity"]], i))
    L += ["## Что исправить", ""]
    if fails:
        L += ["| Вопрос | Критичность | Где | Что исправить |", "|---|---|---|---|"]
        for i in fails:
            a = rec["answers"][i]
            L.append(f"| {i}. {_cell(checks[i]['question'])} | {checks[i]['severity']} | "
                     f"{_cell(a['evidence'])} | {_cell(a['fix'] or checks[i]['fix'])} |")
    else:
        L.append("Все применимые вопросы пройдены.")
    if rec.get("split"):
        L += ["", f"**Предложение по разделению:** {rec['split']}"]
    unk = [i for i, a in rec["answers"].items() if a["v"] == "unknown"]
    L += ["", "## Не определено", ""]
    L += [f"* {i}. {checks[i]['question']}" for i in unk] or ["Нет."]
    L += ["", "## Все вопросы", "", "| Вопрос | Критичность | Ответ | Голоса |", "|---|---|---|---|"]
    for i, a in rec["answers"].items():
        mark = "" if a["stable"] else " ⚠"
        L.append(f"| {i} | {checks[i]['severity']} | {a['v']}{mark} | {a['votes']} |")
    L += ["", "⚠ — прогоны разошлись; ответ принят по большинству.", "",
          "## Что прочитано", "", "| Переход | Страница | Обязательная | Статус |", "|---|---|---|---|",
          f"| 0 | {_cell(rec.get('title', ''))} | да | прочитана |"]
    for ln in rec.get("linked") or []:
        L.append(f"| {ln['hop']} | {_cell(ln.get('title') or ln.get('target') or '')} | "
                 f"{'да' if ln.get('mandatory') else 'нет'} | {ln.get('status')} |")
    if rec.get("free"):
        L += ["", "## Рекомендации вне чек-листа", "", "На оценку не влияют.", ""]
        L += [f"* **{_cell(x.get('where', ''))}** — {_cell(x.get('what', ''))}"
              + (f" → {_cell(x['fix'])}" if x.get("fix") else "") for x in rec["free"]]
    return "\n".join(L) + "\n"


def _cell(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).replace("|", "/").strip()


# ------------------------------------------------------------------ пакет

def select(api, cql: str, pages_file: str, pages: list, limit: int) -> list:
    """→ [(номер, заголовок)] — что оценивать."""
    out = [(p, "") for p in pages]
    if pages_file:
        out += [(ln.strip(), "") for ln in Path(pages_file).read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.startswith("#")]
    if cql:
        start = 0
        while True:
            data = api.get(f"/rest/api/content/search?cql={urllib.parse.quote(cql)}"
                           f"&limit=100&start={start}")
            rows = data.get("results", [])
            out += [(r["id"], r.get("title", "")) for r in rows if r.get("type", "page") == "page"]
            if len(rows) < 100 or (limit and len(out) >= limit):
                break
            start += 100
    seen, uniq = set(), []
    for pid, title in out:
        if pid not in seen:
            seen.add(pid)
            uniq.append((pid, title))
    return uniq[:limit] if limit else uniq


def done_keys(results_path: Path) -> dict:
    """Что уже оценено: номер страницы → (версия, версия шаблона, as_of)."""
    keys = {}
    if results_path.is_file():
        for line in results_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("status") in ("ok", "skipped", "not_existed"):
                keys[r["page_id"]] = (r.get("version"), r.get("template_version"), r.get("as_of"))
    return keys


def summarize(batch_dir: Path, by_author: bool = False) -> str:
    """results.jsonl → summary.csv и summary.md. Пересчитывается целиком: переживает обрыв."""
    rows = {}
    rp = batch_dir / "results.jsonl"
    for line in rp.read_text(encoding="utf-8").splitlines() if rp.is_file() else []:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows[r["page_id"]] = r          # последняя запись по странице — действующая
    recs = list(rows.values())
    cols = ["page_id", "code", "title", "profile", "url", "version", "as_of", "author", "created",
            "modified", "score", "coverage", "verdict", "failed_critical", "failed_major",
            "failed_minor", "unknown", "unstable", "runs", "incomplete", "failed_ids", "report"]
    with open(batch_dir / "summary.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(cols)
        for r in recs:
            f = r.get("failed") or {}
            w.writerow([r.get("page_id"), r.get("code", ""), r.get("title", ""), r.get("profile", ""),
                        r.get("url", ""), r.get("version", ""), r.get("as_of", ""), r.get("author", ""),
                        r.get("created", ""), r.get("modified", ""),
                        str(r.get("score", "")).replace(".", ","), r.get("coverage", ""),
                        r.get("verdict", ""), f.get("critical", ""), f.get("major", ""),
                        f.get("minor", ""), r.get("unknown", ""), r.get("unstable", ""),
                        r.get("runs", ""), r.get("incomplete", ""),
                        " ".join(i for i, a in (r.get("answers") or {}).items() if a["v"] == "no"),
                        r.get("report", "")])
    ok = [r for r in recs if r.get("status") == "ok" and r.get("verdict") != "не оценено"]
    by_v: dict = {}
    for r in recs:
        by_v[r.get("verdict", "?")] = by_v.get(r.get("verdict", "?"), 0) + 1
    scores = sorted(r["score"] for r in ok)
    L = [f"# Сводка ревью: {batch_dir.name}", "",
         f"Страниц: {len(recs)} · оценено: {len(ok)} · шаблон: "
         f"{next((r.get('template') for r in recs if r.get('template')), '')}", ""]
    if scores:
        med = scores[len(scores) // 2]
        passed = by_v.get("готова к передаче", 0)
        L += ["## Итог", "", "| | |", "|---|---|",
              f"| Средняя оценка | {fmt(sum(scores) / len(scores))} |", f"| Медиана | {fmt(med)} |",
              f"| Прошли порог | {passed} из {len(ok)} ({passed / len(ok):.0%}) |", ""]
    L += ["## Вердикты", "", "| Вердикт | Страниц |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in sorted(by_v.items(), key=lambda kv: -kv[1])]
    buckets = [("0–4,9", 0, 5), ("5–6,9", 5, 7), ("7–8,9", 7, 9), ("9–10", 9, 10.01)]
    L += ["", "## Распределение оценок", "", "| Оценка | Страниц |", "|---|---|"]
    L += [f"| {n} | {sum(1 for s in scores if lo <= s < hi)} |" for n, lo, hi in buckets]
    for key, label in (("profile", "По профилю"), ("quarter", "По кварталу последней правки")):
        groups: dict = {}
        for r in ok:
            g = r.get("profile") if key == "profile" else _quarter(r.get("modified", ""))
            groups.setdefault(g or "—", []).append(r)
        L += ["", f"## {label}", "", "| Группа | Страниц | Средняя | Прошли |", "|---|---|---|---|"]
        for g, rs in sorted(groups.items()):
            L.append(f"| {g} | {len(rs)} | {fmt(sum(x['score'] for x in rs) / len(rs))} | "
                     f"{sum(1 for x in rs if x['verdict'] == 'готова к передаче')} |")
    fail_n, app_n, unst_n = {}, {}, {}
    for r in ok:
        for i, a in (r.get("answers") or {}).items():
            if a["v"] in ("yes", "no"):
                app_n[i] = app_n.get(i, 0) + 1
            if a["v"] == "no":
                fail_n[i] = fail_n.get(i, 0) + 1
            if not a.get("stable", True):
                unst_n[i] = unst_n.get(i, 0) + 1
    L += ["", "## Частые дефекты", "", "| Вопрос | Не пройден | Из применимых |", "|---|---|---|"]
    for i, n in sorted(fail_n.items(), key=lambda kv: -kv[1])[:15]:
        L.append(f"| {i} | {n} | {n / app_n[i]:.0%} |")
    L += ["", "## Устойчивость чек-листа", "",
          "Доля страниц, где прогоны разошлись по вопросу. Высокая доля — вопрос "
          "сформулирован нечётко и кандидат на переформулировку в следующей версии шаблона.", "",
          "| Вопрос | Разошлись | Доля |", "|---|---|---|"]
    for i, n in sorted(unst_n.items(), key=lambda kv: -kv[1])[:15]:
        L.append(f"| {i} | {n} | {n / len(ok):.0%} |")
    if not unst_n:
        L.append("| — | 0 | 0% |")
    if by_author:
        groups = {}
        for r in ok:
            groups.setdefault(r.get("author") or "—", []).append(r)
        L += ["", "## По авторам", "", "| Автор | Страниц | Средняя | Прошли |", "|---|---|---|---|"]
        for g, rs in sorted(groups.items()):
            L.append(f"| {g} | {len(rs)} | {fmt(sum(x['score'] for x in rs) / len(rs))} | "
                     f"{sum(1 for x in rs if x['verdict'] == 'готова к передаче')} |")
    miss = [r for r in recs if r.get("verdict") in ("не оценено",)]
    if miss:
        L += ["", "## Не оценено", ""]
        L += [f"* {r.get('title') or r['page_id']} — {'; '.join(r.get('reasons') or [])}"
              for r in miss[:50]]
    L += ["", "Детали по странице — в её отчёте; вся таблица — `summary.csv`."]
    text = "\n".join(L) + "\n"
    (batch_dir / "summary.md").write_text(text, encoding="utf-8")
    return text


def _quarter(day: str) -> str:
    return f"{day[:4]}-Q{(int(day[5:7]) - 1) // 3 + 1}" if re.match(r"\d{4}-\d{2}", day or "") else ""


# ------------------------------------------------------------------ запуск

def _default_call():
    import agent_core as AG
    return AG.call_role


def _connect():
    from confluence_export import Api, read_config, read_secret
    cfg = read_config()
    auth, _how = read_secret()
    if not cfg.get("base_url"):
        sys.exit("нет confluence.base_url в aurora.config.yaml")
    if not auth:
        sys.exit("нет доступа к Confluence: CONFLUENCE_PAT в .env.aurora.local")
    return Api(cfg["base_url"], auth), cfg


def main() -> int:
    ap = argparse.ArgumentParser(description="Автоматическое ревью истории или алгоритма")
    ap.add_argument("--page", action="append", default=[],
                    help="адрес или номер страницы (можно несколько)")
    ap.add_argument("--cql", default="", help="отбор страниц запросом CQL — пакетный режим")
    ap.add_argument("--pages-file", default="", help="файл со списком страниц — пакетный режим")
    ap.add_argument("--name", default="", help="имя пакета: папка batch_<имя>, продолжение с места")
    ap.add_argument("--profile", default="auto", choices=["auto", "us", "alg"])
    ap.add_argument("--as-of", default="", help="оценивать версию страниц на дату ГГГГ-ММ-ДД")
    ap.add_argument("--runs", type=int, default=3, help="независимых прогонов чек-листа (1–5)")
    ap.add_argument("--depth", type=int, default=2, help="переходов по связанным страницам")
    ap.add_argument("--max-pages", type=int, default=15, help="потолок связанных страниц")
    ap.add_argument("--limit", type=int, default=0, help="не больше N страниц пакета")
    ap.add_argument("--free", action="store_true",
                    help="рекомендации вне чек-листа (для одной страницы включены всегда)")
    ap.add_argument("--by-author", action="store_true", help="раздел сводки по авторам")
    ap.add_argument("--budget-min", type=int, default=0,
                    help="бюджет пакета в минутах (0 — из настройки агента)")
    ap.add_argument("--template", default="", help="путь к шаблону ревью")
    ap.add_argument("--summary", default="", help="пересобрать сводку папки пакета и выйти")
    ap.add_argument("--apply", action="store_true", help="записать отчёты в Artifacts/reviews/")
    args = ap.parse_args()

    if args.summary:
        print(summarize(Path(args.summary), args.by_author))
        return 0
    tpl = find_template(args.template)
    if not tpl:
        sys.exit(f"не найден шаблон {TEMPLATE} (TemplatesCommon/ проекта или кит)")
    cl = load_checklist(tpl)
    runs = max(1, min(MAX_RUNS, args.runs))
    api, ccfg = _connect()
    reader = Reader(api, ccfg["base_url"], CACHE, args.as_of)
    import agent_core as AG
    cfg = AG.parse_config(AG.raw_config())
    batch = bool(args.cql or args.pages_file or len(args.page) > 1)

    if not batch:
        if not args.page:
            ap.error("нужна --page, --cql или --pages-file")
        from confluence_export import resolve_ref
        pid, _t, err = resolve_ref(api, args.page[0], ccfg.get("space", ""))
        if err:
            sys.exit(err)
        print(f"Ревью страницы {pid} · шаблон {cl['file']} · прогонов {runs}", file=sys.stderr)
        rec = review_page(cfg, cl, reader, pid, profile=args.profile, runs=runs,
                          depth=args.depth, max_pages=args.max_pages, free=True,
                          slots=AG.pool(cfg))
        report = render(rec, cl)
        if args.apply:
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
            out = Path(REVIEWS) / f"{stamp}_review_{rec.get('code') or pid}.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(report, encoding="utf-8")
            print(f"Отчёт: {out}", file=sys.stderr)
        print(report)
        return 0

    todo = select(api, args.cql, args.pages_file,
                  [resolve_or_self(api, p, ccfg) for p in args.page], args.limit)
    # Запрос CQL по заголовку ловит и служебные страницы («Чек-лист к US», «Матрица
    # согласований»). Профиль по заголовку им не определить — отсеиваем до оценки, чтобы
    # они не засоряли сводку строками «не оценено» и не искажали счёт вызовов.
    if args.profile == "auto":
        dropped = [(p, t) for p, t in todo if t and not detect_profile(t, "auto")]
        todo = [(p, t) for p, t in todo if not t or detect_profile(t, "auto")]
        if dropped:
            print(f"Отсеяно по заголовку (не история и не алгоритм): {len(dropped)} — "
                  + "; ".join(t for _p, t in dropped[:5]) + (" …" if len(dropped) > 5 else ""))
    name = args.name or datetime.now().strftime("%Y-%m-%d_%H%M")
    bdir = Path(REVIEWS) / f"batch_{name}"
    known = done_keys(bdir / "results.jsonl")
    # Пропускаем только то, что оценено той же версией шаблона и на ту же дату: сменили
    # чек-лист — прежние ответы к новым вопросам не относятся.
    same = lambda k: k[1] == cl["version"] and (k[2] or "") == (args.as_of or "")  # noqa: E731
    fresh = [(p, t) for p, t in todo if not (p in known and same(known[p]))]
    print(f"Пакет «{name}»: страниц {len(todo)}, уже оценено {len(todo) - len(fresh)}, "
          f"к оценке {len(fresh)} · прогонов {runs} (до {MAX_RUNS} при эскалации) · "
          f"≈ {len(fresh) * runs}–{len(fresh) * (MAX_RUNS + (1 if args.free else 0))} "
          f"вызовов модели · шаблон {cl['file']}"
          + (f" · по состоянию на {args.as_of}" if args.as_of else ""))
    if not args.apply:
        for p, t in fresh[:30]:
            print(f"  {p}  {t}")
        if len(fresh) > 30:
            print(f"  … и ещё {len(fresh) - 30}")
        print("(предпросмотр) Модель не вызывалась. Повторите с --apply.")
        return 0
    bdir.mkdir(parents=True, exist_ok=True)
    budget_min = args.budget_min or cfg["budget_min"]
    deadline = time.time() + budget_min * 60
    slots = AG.pool(cfg)
    lock, stop = threading.Lock(), threading.Event()
    state = {"done": 0, "fail_streak": 0, "why": ""}
    started = time.time()

    def work(job):
        idx, (pid, _title) = job
        if stop.is_set() or time.time() > deadline:
            return
        rec = review_page(cfg, cl, reader, pid, profile=args.profile, runs=runs,
                          depth=args.depth, max_pages=args.max_pages, free=args.free,
                          prefer=slots[idx % len(slots)])
        if rec.get("status") == "ok" or rec.get("status") in ("skipped", "not_existed"):
            fname = f"{rec.get('code') or 'page'}_{pid}.md"
            (bdir / fname).write_text(render(rec, cl), encoding="utf-8")
            rec["report"] = fname
        with lock:
            with open(bdir / "results.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            state["done"] += 1
            bad = rec.get("status") == "ok" and not rec.get("good_runs")
            state["fail_streak"] = state["fail_streak"] + 1 if bad else 0
            if state["fail_streak"] >= SAME_FAIL_LIMIT:
                state["why"] = f"{SAME_FAIL_LIMIT} страницы подряд без ответа модели: " \
                               f"{'; '.join(rec.get('run_errors') or [])[:160]}"
                stop.set()
            print(f"  [{state['done']}/{len(fresh)}] {rec.get('code') or pid}: "
                  f"{rec.get('verdict')}"
                  + (f" · {fmt(rec['score'])}" if rec.get("status") == "ok" and rec.get("good_runs")
                     else "") + f" · {int(time.time() - started)} с", file=sys.stderr, flush=True)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, len(slots))) as ex:
        list(ex.map(work, list(enumerate(fresh))))
    left = len(fresh) - state["done"]
    if state["why"]:
        print(f"Остановлено: {state['why']}", file=sys.stderr)
    elif left:
        print(f"Бюджет {budget_min} мин исчерпан, осталось {left}. "
              f"Продолжение — тот же запуск с --name {name}.", file=sys.stderr)
    summarize(bdir, args.by_author)
    print(f"Сводка: {bdir / 'summary.md'} · таблица: {bdir / 'summary.csv'}")
    return 0


def resolve_or_self(api, ref: str, ccfg: dict) -> str:
    from confluence_export import resolve_ref
    pid, _t, _err = resolve_ref(api, ref, ccfg.get("space", ""))
    return pid or ref


if __name__ == "__main__":
    sys.exit(main())
