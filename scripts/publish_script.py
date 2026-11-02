#!/usr/bin/env python3
"""publish_script.py — один раз собрать скрипт страницы по промпту публикации вида.

Промпт публикации описывает, как оформить готовый документ в страницу Confluence. Держать модель на каждой публикации
дорого; держать только промпт — страница расходится с ним. Поэтому модель читает промпт и заготовку ОДИН раз и пишет
скрипт, который затем повторяет оформление без токенов. Скрипт лежит в проекте (`Publish/<вид>.py`), рядом — отпечатки
входов (`.meta.json`): промпт или заготовку поправили — публикация скажет, что скрипт устарел.

  python3 .aurora/scripts/publish_script.py --kind ac --project .            # проверить и показать, что будет
  python3 .aurora/scripts/publish_script.py --kind ac --project . --apply    # записать скрипт и отпечатки

Контракт скрипта — в `publish_llm.py`: stdin JSON, stdout JSON {"title", "storage"}. Генератор проверяет скрипт на
последнем документе вида: запускает дважды, сверяет страницы и обязательные разделы. Не прошёл — не записывается.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_kinds as MK
import publish_llm as LLM
from aurora_common import split_frontmatter

META_VERSION = 1


def _root(project: str) -> Path:
    return Path(project or ".").resolve()


def _rel(project: str, rel: str) -> str:
    """Путь настройки → путь относительно проекта. Абсолютный внутри проекта — относительно; наружу — отказ."""
    root = _root(project)
    raw = str(rel or "").strip().replace("\\", "/")
    if not raw:
        return ""
    path = Path(raw) if os.path.isabs(raw) else root / raw
    try:
        relpath = path.resolve().relative_to(root)
    except ValueError:
        raise ValueError(f"путь вне проекта: {rel}")
    return relpath.as_posix()


def default_script(kind: str) -> str:
    return f"Publish/{kind}.py"


def script_rel(project: str, kind: str, rec: dict) -> str:
    """Где лежит скрипт вида: поле `publish_script`, иначе `Publish/<вид>.py`."""
    return _rel(project, rec.get("publish_script") or default_script(kind))


def meta_rel(project: str, kind: str, rec: dict) -> str:
    """Отпечатки рядом со скриптом: `Publish/ac.py` → `Publish/ac.meta.json`."""
    s = script_rel(project, kind, rec)
    return (s[:-3] if s.endswith(".py") else s) + ".meta.json"


def _sha(text: str) -> str:
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()


def _file_text(project: str, rel: str) -> str:
    if not str(rel or "").strip():
        return ""
    path = _root(project) / rel
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def sample_document(project: str, kind: str, rec: dict) -> tuple[str, str]:
    """Последний готовый документ вида: на нём проверяем скрипт. → (путь относительно проекта, текст)."""
    out = str(rec.get("out") or "").strip()
    if not out:
        raise RuntimeError(f"у вида {kind} не задана папка документов (out) — не на чем проверить скрипт")
    folder = _root(project) / _rel(project, out)
    if not folder.is_dir():
        raise RuntimeError(f"папки документов вида {kind} нет: {out}")
    files = [p for p in folder.rglob("*.md") if p.is_file() and not p.name.startswith(".")]
    if not files:
        raise RuntimeError(f"в {out} нет документов вида {kind} — скрипт не на чем проверить")
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return newest.resolve().relative_to(_root(project)).as_posix(), newest.read_text(encoding="utf-8", errors="replace")


def _sample_title(project: str, kind: str, rec: dict, path: str, text: str) -> str:
    """Заголовок образца тем же кодом, что публикация: поле publish_title шапки или шаблон вида."""
    try:
        sample = str((_root(project) / path).resolve())
        ctx = MK.publish_context(text, sample, kind, MK.project_name(project), rec.get("out") or "", str(_root(project)))
        target = MK.publish_target(rec, ctx)
        title = str(target.get("title") or "").strip()
        if title:
            return title
    except Exception:  # noqa: BLE001 — образец не обязан проходить все подстановки
        pass
    _front, fm = split_frontmatter(text)
    for key in ("publish_title", "title"):
        if str((fm or {}).get(key) or "").strip():
            return str(fm[key]).strip()
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else Path(path).stem


def generate(kind: str, project: str = ".", script_path: str = "", apply: bool = False,
             attempts: int = 2, role: str = LLM.ROLE, max_tokens: int = LLM.MAX_TOKENS) -> dict:
    """Собрать скрипт вида по промпту публикации и проверить его на последнем документе.

    `apply=False` — только проверка: ничего не пишется. Возвращает машинный отчёт; `ok` — скрипт проверен.
    """
    root = _root(project)
    kinds = MK.read_kinds(str(root)) or {}
    rec = kinds.get(kind) or {}
    if not rec:
        return {"ok": False, "kind": kind, "why": f"вида {kind} нет в artifacts проекта"}
    prompt_rel = _rel(project, rec.get("publish_prompt") or "")
    if not prompt_rel or not (_root(project) / prompt_rel).is_file():
        return {"ok": False, "kind": kind, "why": f"у вида {kind} не задан существующий publish_prompt"}
    template_rel = _rel(project, rec.get("publish_template") or "") if rec.get("publish_template") else ""
    if template_rel and not (_root(project) / template_rel).is_file():
        return {"ok": False, "kind": kind, "why": f"заготовка вида {kind} не найдена: {template_rel}"}
    try:
        sample_path, sample_text = sample_document(project, kind, rec)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "kind": kind, "why": str(e)}
    title = _sample_title(project, kind, rec, sample_path, sample_text)
    sample = LLM.document_data(sample_path, sample_text, title, kind, MK.project_name(project))
    messages = LLM.build_script_messages(kind, rec, project, sample_path, sample_text)
    errors = []
    code = ""
    last = {}
    for attempt in range(1, max(1, attempts) + 1):
        r = LLM._call(messages, role=role, thinking=True, max_tokens=max_tokens)
        if not r.get("ok"):
            errors.append(f"попытка {attempt}: {r.get('error') or 'модель не ответила'}")
            break
        code = LLM.strip_code(r["text"])
        check = LLM.validate_script(project, kind, code, sample)
        last = {"attempt": attempt, "model": r.get("model") or "", "validation": check}
        if check.get("ok"):
            break
        errors.append(f"попытка {attempt}: {check.get('why') or 'проверка не прошла'}")
        messages = messages + [
            {"role": "assistant", "content": r["text"]},
            {"role": "user", "content": "Проверка скрипта не прошла: " + str(check.get("why") or "неизвестно") +
             ". Исправь скрипт и верни только код, без ``` и пояснений."},
        ]
    check = (last.get("validation") or {})
    if not check.get("ok"):
        return {"ok": False, "kind": kind, "why": "; ".join(errors) or "скрипт не проверен",
                "attempts": last.get("attempt", 0), "model": last.get("model", ""), "errors": errors}
    target_rel = _rel(project, script_path) if script_path else script_rel(project, kind, rec)
    meta_target = (target_rel[:-3] if target_rel.endswith(".py") else target_rel) + ".meta.json"
    prompt_text = _file_text(project, prompt_rel)
    template_text = _file_text(project, template_rel) if template_rel else ""
    meta = {
        "version": META_VERSION,
        "kind": kind,
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                          .isoformat(timespec="seconds").replace("+00:00", "Z"),
        "role": role,
        "model": last.get("model") or "",
        "prompt": prompt_rel,
        "prompt_sha": _sha(prompt_text),
        "template": template_rel,
        "template_sha": _sha(template_text) if template_rel else "",
        "script": target_rel,
        "script_sha": _sha(code),
        "sample": sample_path,
        "sample_sha": _sha(sample_text),
        "validation": {k: v for k, v in check.items() if k != "storage"},
        "engine": str(rec.get("publish_engine") or "auto").strip() or "auto",
    }
    summary = {"ok": True, "kind": kind, "script": target_rel, "meta": meta_target,
               "title": check.get("title") or "", "storage_len": check.get("storage_len") or 0,
               "model": last.get("model") or "", "checks": check.get("checks") or [], "applied": bool(apply)}
    if apply:
        s_path = _root(project) / target_rel
        s_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = s_path.with_suffix(s_path.suffix + ".tmp")
        tmp.write_text(code, encoding="utf-8")
        os.chmod(tmp, 0o755)
        tmp.replace(s_path)
        m_path = _root(project) / meta_target
        m_path.parent.mkdir(parents=True, exist_ok=True)
        m_tmp = m_path.with_suffix(m_path.suffix + ".tmp")
        m_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        m_tmp.replace(m_path)
        summary["written"] = [target_rel, meta_target]
    return summary


def read_meta(project: str, kind: str, rec: dict) -> dict:
    """Отпечатки скрипта, если они есть. Пустой словарь — не создавали."""
    path = _root(project) / meta_rel(project, kind, rec)
    if not path.is_file():
        return {}
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return got if isinstance(got, dict) else {}


def status(project: str, kind: str, rec: dict) -> dict:
    """Состояние скрипта для формы и публикации: есть ли, совпадают ли входы, когда создан.

    `state`: `missing` — скрипта нет; `ready` — есть и входы те же; `stale` — промпт/заготовка/скрипт изменились после
    создания. `why` называет, что именно изменилось, чтобы человек понимал, зачем жать «Создать скрипт» снова."""
    script = script_rel(project, kind, rec)
    s_path = _root(project) / script
    meta = read_meta(project, kind, rec)
    present = s_path.is_file()
    if not present:
        return {"state": "missing", "script": script, "meta": "", "why": "скрипта нет", "why_codes": ["missing"],
                "generated_at": ""}
    why = []
    codes = []
    if not meta:
        why.append("отпечатков нет: скрипт положили вручную или перенесли")
        codes.append("no-meta")
    else:
        try:
            prompt_now = _rel(project, rec.get("publish_prompt") or "") if rec.get("publish_prompt") else ""
        except ValueError:
            prompt_now = str(rec.get("publish_prompt") or "")
        try:
            template_now = _rel(project, rec.get("publish_template") or "") if rec.get("publish_template") else ""
        except ValueError:
            template_now = str(rec.get("publish_template") or "")
        checks = (("prompt", prompt_now, str(meta.get("prompt") or ""), "prompt_sha", "промпт публикации"),
                  ("template", template_now, str(meta.get("template") or ""), "template_sha", "заготовка страницы"),
                  ("script", script, str(meta.get("script") or script), "script_sha", "сам скрипт"))
        for what, now_rel, was_rel, sha_key, label in checks:
            if now_rel != was_rel:
                why.append(f"{label} заменён после создания скрипта")
                codes.append({"prompt": "prompt-changed", "template": "template-changed",
                              "script": "script-changed"}[what])
                continue
            want = str(meta.get(sha_key) or "")
            text = _file_text(project, now_rel) if now_rel else ""
            if want and _sha(text) != want:
                why.append(f"{label} изменился после создания скрипта")
                codes.append({"prompt": "prompt-changed", "template": "template-changed",
                              "script": "script-changed"}[what])
        if not str(meta.get("script_sha") or "").strip():
            why.append("в отпечатках нет хеша скрипта")
            codes.append("no-script-sha")
    return {"state": "stale" if why else "ready", "script": script,
            "meta": meta_rel(project, kind, rec) if meta else "", "why": "; ".join(why), "why_codes": codes,
            "generated_at": str(meta.get("generated_at") or "")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Собрать скрипт страницы вида по промпту публикации")
    ap.add_argument("--kind", required=True, help="вид артефакта из artifacts проекта")
    ap.add_argument("--project", default=".", help="корень проекта (по умолчанию текущий)")
    ap.add_argument("--script", default="", help="куда писать скрипт (по умолчанию publish_script вида или Publish/<вид>.py)")
    ap.add_argument("--apply", action="store_true", help="записать скрипт и отпечатки (без флага — только проверка)")
    ap.add_argument("--attempts", type=int, default=2, help="сколько раз просить модель исправить скрипт")
    ap.add_argument("--json", action="store_true", help="машинный отчёт в stdout")
    a = ap.parse_args(argv)
    try:
        res = generate(a.kind, a.project, a.script, apply=a.apply, attempts=a.attempts)
    except Exception as e:  # noqa: BLE001 — CLI отдаёт причину, а не трассу
        res = {"ok": False, "kind": a.kind, "why": f"{type(e).__name__}: {e}"}
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, sort_keys=True))
    elif res.get("ok"):
        print(f"# скрипт страницы {res['kind']}: {res['script']}")
        print(f"Заголовок образца: {res.get('title') or '—'} · storage {res.get('storage_len') or 0} символов")
        print("Проверки: " + ", ".join(res.get("checks") or []))
        print("Записан." if a.apply else "Не записан: без --apply это только проверка.")
    else:
        print(f"publish_script: {res.get('why')}", file=sys.stderr)
        for line in res.get("errors") or []:
            print(f"  {line}", file=sys.stderr)
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
