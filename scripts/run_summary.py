#!/usr/bin/env python3
"""run_summary.py — итог прогона: время, модель, документы, карточки, ошибки («Аврора»).

Человек, запустивший «Обновить базу», в конце видел «пройден: 28 шагов» и собирал остальное
из вывода шагов сам: сколько ушло времени и токенов, с какой скоростью шла генерация, что
разобрано и что нет, сколько карточек появилось, дополнено и пропало, какие были ошибки.
Итог собирает это в одном месте.

Составитель один на движок: агент печатает итог своего прогона, а панель в конце маршрута
складывает итоги шагов через этот же модуль (`/api/run/summary`). Два рендера одного итога
разошлись бы на второй же правке.

Итог агента уходит в вывод двумя формами: для человека — строками, для панели — одной
машинной строкой `AURORA-SUMMARY {...}`, которую консоль панели не показывает.

Изменения базы считаются по git, а не по отчётам шагов: так учтены и шаги-скрипты (ремонт,
слияния, переименования), и итог маршрута переживает перезапуск панели.
"""
from __future__ import annotations

import json
import os
import subprocess

MARK = "AURORA-SUMMARY "
KB = "AuroraKnowledgeDB"

# Статусы шагов агентов, где шаг — документ-источник (у шага есть `source`).
DOC_DONE = {"разобран", "разобран по абзацам", "пусто — отмечено", "знания нет"}
DOC_SKIP = {"пропущена", "человеку", "без секций — человеку", "слишком длинная", "стоп",
            "отложен"}
DOC_FAIL = {"сбой", "отклонено критиком", "отклонено проверкой", "отброшен"}

NUMBERS = ("seconds", "model_calls", "model_failed", "tokens_in", "tokens_out", "gen_seconds",
           "docs_done", "docs_skipped", "docs_failed",
           "cards_created", "cards_updated", "cards_marked", "cards_deleted", "cards_failed")


def empty() -> dict:
    """Пустой итог: все счётчики нулевые, ошибок нет, изменения базы не посчитаны."""
    s = {k: 0 for k in NUMBERS}
    s["gen_seconds"] = 0.0
    s["errors"] = {}
    s["cards_known"] = False
    return s


def _err(s: dict, kind: str, n: int = 1) -> None:
    if n:
        s["errors"][kind] = s["errors"].get(kind, 0) + n


def _short(text, n: int = 60) -> str:
    t = " ".join(str(text or "").split())
    return t[:n].rstrip(" .,;:—") + ("…" if len(t) > n else "")


def from_agent(steps, usage: dict | None, seconds: float, delta: dict | None) -> dict:
    """Итог прогона агента: его шаги, счётчик обращений к модели, изменения базы."""
    s = empty()
    s["seconds"] = round(seconds or 0, 1)
    u = usage or {}
    s["model_calls"] = int(u.get("calls") or 0)
    s["model_failed"] = int(u.get("failed") or 0)
    s["tokens_in"] = int(u.get("tokens_in") or 0)
    s["tokens_out"] = int(u.get("tokens_out") or 0)
    s["gen_seconds"] = round(float(u.get("gen_seconds") or 0.0), 2)
    # Ошибки прогона — это документы и карточки, которые не вышли. Неудачный вызов модели —
    # ещё не ошибка: его повторяют, и чаще всего следующая попытка проходит; такие вызовы
    # показаны в строке модели («неудачных N»). Складывать их с упавшими карточками значило
    # считать один сбой дважды: на PRJ-A 22.09.2026 «Ошибки: 198» при 99 упавших карточках.
    # Причины вызовов идут в ошибки, только когда шагов нет вовсе (разговор, артефакт).
    if not steps:
        for kind, n in (u.get("errors") or {}).items():
            _err(s, kind, int(n or 0))
    for st in steps or []:
        status = str(st.get("status") or "")
        if "source" in st:
            if status in DOC_DONE:
                s["docs_done"] += 1
            elif status in DOC_SKIP:
                s["docs_skipped"] += 1
            elif status in DOC_FAIL:
                s["docs_failed"] += 1
        if st.get("content_fail"):
            # Ответ модели не разобран в содержание — карточка не создана и не дополнена.
            s["cards_failed"] += 1
            _err(s, "карточка не записана: ответ модели не разобран")
        elif status in DOC_FAIL:
            why = _short(st.get("note") or st.get("why") or "")
            _err(s, status + (f": {why}" if why else ""))
    if delta is not None:
        s.update(cards_created=delta["created"], cards_updated=delta["updated"],
                 cards_marked=delta.get("marked", 0),
                 cards_deleted=delta["deleted"], cards_known=True)
    return s


def git_head(cwd: str) -> str:
    """Коммит, от которого считаются изменения базы; пусто — проект не в git."""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def _card(path: str) -> bool:
    p = path.replace("\\", "/")
    if not (p.startswith(KB + "/") and p.endswith(".md")):
        return False
    return not ("/meta/" in p or "/MOC/" in p or os.path.basename(p).startswith("_"))


def _archived(path: str) -> bool:
    return "/_archive/" in path.replace("\\", "/")


def kb_delta(cwd: str, since: str) -> dict | None:
    """Карточки с коммита `since` до нынешнего дерева: создано, изменено, удалено.

    Карточка, уехавшая в `_archive/`, — удалённая: из базы её больше не достать. Служебные
    файлы (карты содержания, оглавления, meta) — не карточки и не считаются.
    """
    if not since:
        return None
    git = ["git", "-c", "core.quotepath=false"]
    try:
        d = subprocess.run(git + ["diff", "--name-status", "-M", since, "--", KB], cwd=cwd,
                           capture_output=True, text=True, timeout=120)
        u = subprocess.run(git + ["ls-files", "--others", "--exclude-standard", "--", KB],
                           cwd=cwd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if d.returncode != 0:
        return None
    out = {"created": 0, "updated": 0, "marked": 0, "deleted": 0}
    changed = []                   # изменённые на месте: правка знания или только шапки?
    for line in d.stdout.splitlines():
        parts = line.split("\t")
        code = parts[0][:1]
        if code == "R" and len(parts) == 3:
            old, new = parts[1], parts[2]
            if _card(old) and not _archived(old):
                out["deleted" if _archived(new) else "updated"] += 1
        elif len(parts) >= 2 and _card(parts[1]) and not _archived(parts[1]):
            key = {"A": "created", "M": "updated", "D": "deleted"}.get(code)
            if key == "updated":
                changed.append(parts[1])
            elif key:
                out[key] += 1
    for p in u.stdout.splitlines():
        if _card(p) and not _archived(p):
            out["created"] += 1
    # Правка одной шапки — отметка движка («осмотрено», «связано», доверие), а не знание.
    # Вынос на PRJ-A 22.09.2026 объявил «обновлено/дополнено 464», из которых знание
    # изменилось у десятков: остальные получили поле `extracted:`.
    before = _bodies_at(cwd, since, changed)
    for p in changed:
        try:
            now = open(os.path.join(cwd, p), encoding="utf-8", errors="ignore").read()
        except OSError:
            out["updated"] += 1
            continue
        out["marked" if before.get(p) is not None and _body(before[p]) == _body(now)
            else "updated"] += 1
    return out


def _body(text: str) -> str:
    """Тело карточки без шапки — то, что человек называет знанием."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return " ".join(text.split())


def _bodies_at(cwd: str, rev: str, paths: list) -> dict:
    """{путь: текст файла в коммите `rev`} — одним `git cat-file --batch`, а не git show на файл."""
    if not paths:
        return {}
    try:
        p = subprocess.run(["git", "cat-file", "--batch"], cwd=cwd, timeout=300,
                           input="".join(f"{rev}:{x}\n" for x in paths).encode("utf-8"),
                           capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return {}
    data, out, pos = p.stdout, {}, 0
    for path in paths:
        nl = data.find(b"\n", pos)
        if nl == -1:
            break
        head = data[pos:nl].split()
        pos = nl + 1
        if len(head) < 3 or head[1] == b"missing":
            continue
        size = int(head[2])
        out[path] = data[pos:pos + size].decode("utf-8", errors="ignore")
        pos += size + 1
    return out


def merge(items) -> dict:
    """Сумма итогов шагов."""
    s = empty()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        for k in NUMBERS:
            s[k] += it.get(k) or 0
        for kind, n in (it.get("errors") or {}).items():
            _err(s, kind, int(n or 0))
        s["cards_known"] = s["cards_known"] or bool(it.get("cards_known"))
    return s


def route(cwd: str, since: str, seconds: float, steps: list) -> dict:
    """Итог маршрута: итоги шагов плюс изменения базы за весь маршрут по git."""
    items = []
    for st in steps or []:
        got = st.get("summary") or []
        items += got if isinstance(got, list) else [got]
    s = merge(items)
    s["seconds"] = round(seconds or 0, 1)
    for st in steps or []:
        rc = int(st.get("rc") or 0)
        if rc >= 2:
            _err(s, f"шаг {st.get('cmd')} не отработал (код {rc})")
    delta = kb_delta(cwd, since)
    # Карточки маршрута — только по git: у шагов они посчитаны каждым от своего коммита, и
    # сумма таких счётов выдала бы одну карточку за несколько.
    s.update(cards_created=(delta or {}).get("created", 0),
             cards_updated=(delta or {}).get("updated", 0),
             cards_marked=(delta or {}).get("marked", 0),
             cards_deleted=(delta or {}).get("deleted", 0), cards_known=delta is not None)
    return {"lines": render(s, "Итог прогона"), "data": s}


def human_time(sec) -> str:
    sec = int(round(sec or 0))
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h} ч {m} мин"
    if m:
        return f"{m} мин {s} с"
    return f"{s} с"


def _n(x) -> str:
    return f"{int(x or 0):,}".replace(",", " ")


def render(s: dict, title: str = "Итог прогона") -> list:
    """Итог словами — одинаково в терминале, в журнале агента и в консоли панели."""
    L = [f"■ {title}", f"  Время: {human_time(s.get('seconds'))}"]
    calls = s.get("model_calls") or 0
    if calls:
        t_in, t_out = s.get("tokens_in") or 0, s.get("tokens_out") or 0
        gen = s.get("gen_seconds") or 0
        tps = f"{t_out / gen:.1f}" if gen else "—"
        L.append(f"  Модель: токенов {_n(t_in + t_out)} (вход {_n(t_in)} · выход {_n(t_out)})"
                 f" · в среднем {tps} ток/с · вызовов {_n(calls)}"
                 + (f", неудачных {_n(s.get('model_failed'))}" if s.get("model_failed") else ""))
    else:
        L.append("  Модель: не вызывалась")
    L.append(f"  Документы: обработано {_n(s.get('docs_done'))} · пропущено "
             f"{_n(s.get('docs_skipped'))} · не удалось разобрать {_n(s.get('docs_failed'))}")
    failed = f"не удалось создать/обновить {_n(s.get('cards_failed'))}"
    if s.get("cards_known"):
        L.append(f"  Карточки: создано {_n(s.get('cards_created'))} · обновлено/дополнено "
                 f"{_n(s.get('cards_updated'))} · удалено {_n(s.get('cards_deleted'))} · {failed}"
                 + (f" · служебных отметок {_n(s.get('cards_marked'))}" if s.get("cards_marked")
                    else ""))
    else:
        L.append(f"  Карточки: изменения базы не посчитаны (предпросмотр или проект не в git)"
                 f" · {failed}")
    errs = s.get("errors") or {}
    total = sum(errs.values())
    if total:
        L.append(f"  Ошибки: {_n(total)}")
        kinds = sorted(errs.items(), key=lambda kv: (-kv[1], kv[0]))
        L += [f"    · {kind} — {_n(n)}" for kind, n in kinds[:12]]
        if len(kinds) > 12:
            L.append(f"    · … и ещё видов: {len(kinds) - 12}")
    else:
        L.append("  Ошибки: не было")
    return L


def emit(s: dict) -> str:
    """Машинная строка итога для панели."""
    return MARK + json.dumps(s, ensure_ascii=False)


def parse(lines) -> list:
    """Итоги из вывода команды — все машинные строки по порядку."""
    out = []
    for line in lines or []:
        if str(line).startswith(MARK):
            try:
                out.append(json.loads(str(line)[len(MARK):]))
            except ValueError:
                pass
    return out
