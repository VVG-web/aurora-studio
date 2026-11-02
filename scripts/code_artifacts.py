#!/usr/bin/env python3
"""code_artifacts.py — какие артефакты можно собрать по коду, по каким шаблонам и промптам.

Настройка живёт в папках, а не в `aurora.config.yaml`:

  TemplatesCode/<вид>_v1.0.md   паспорт вида и форма документа
  PromptCode/<вид>.md           правила письма для этого вида

Кит привозит преднастроенные виды. Проект переопределяет любой из них файлом с тем же
`kind` в своей `TemplatesCode/`, добавляет новые или выключает ненужные `enabled: "false"`.
Правка настроек проекта для этого не нужна.

  python3 .aurora/scripts/code_artifacts.py --list
  python3 .aurora/scripts/code_artifacts.py --kind cr-api
  python3 .aurora/scripts/code_artifacts.py --show cr-api
  python3 .aurora/scripts/code_artifacts.py --check
  python3 .aurora/scripts/code_artifacts.py --json

Панель: `code:artifacts`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import frontmatter  # noqa: E402

KIT = Path(__file__).resolve().parents[1]
TEMPLATES = "TemplatesCode"
PROMPTS = "PromptCode"
SCOPES = ("symbol", "route", "controller", "module", "table", "topic", "pair", "component", "service")
PURITIES = ("traceable", "strict")
DEFAULT_OUT = "Artifacts/drafts/code"
TITLES_EN = {
    "cr-api": "API specification from code",
    "cr-data": "Data schema from code",
    "cr-events": "Events and topics from code",
    "cr-config": "Configuration from code",
    "cr-module": "Module card from code",
    "cr-algorithm": "Algorithm from code",
    "cr-rules": "Business rules from code",
    "cr-crud": "CRUD matrix from code",
    "cr-tests": "Test map from code",
    "cr-screen": "Screen form from code",
    "cr-mapping": "Data mapping from code",
    "cr-sequence": "Call sequence from code",
    "cr-adr": "Decision reconstruction from code",
}


def _bool(value, default=False) -> bool:
    text = str(value if value is not None else "").strip().strip("\"'").lower()
    if text in ("true", "yes", "on", "1", "да"):
        return True
    if text in ("false", "no", "off", "0", "нет", ""):
        return False if text else default
    return default


def _int(value, default=0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _kind_of(path: Path, meta: dict) -> str:
    kind = str(meta.get("kind") or "").strip()
    if kind:
        return kind
    name = path.stem
    return name.split("_v", 1)[0]


def _read_template(path: Path, source: str) -> dict:
    text = path.read_text(encoding="utf-8")
    meta = frontmatter(text)
    kind = _kind_of(path, meta)
    return {
        "kind": kind,
        "title": str(meta.get("title") or kind).strip(),
        "title_en": str(meta.get("title_en") or TITLES_EN.get(kind) or kind).strip(),
        "version": str(meta.get("version") or "1.0").strip(),
        "enabled": _bool(meta.get("enabled"), True),
        "scope": str(meta.get("scope") or "module").strip() or "module",
        "out": str(meta.get("out") or DEFAULT_OUT).strip() or DEFAULT_OUT,
        "prompt": str(meta.get("prompt") or f"{PROMPTS}/{kind}.md").strip(),
        "purity": str(meta.get("purity") or "traceable").strip() or "traceable",
        "order": _int(meta.get("order"), 100),
        "depth": _int(meta.get("depth"), 2),
        "source": source,
        "template_path": str(path),
        "body": text,
    }


def _read_dir(path: Path, source: str):
    rows, problems = {}, []
    if not path.is_dir():
        return rows, problems
    for file in sorted(path.glob("*.md")):
        if file.name.lower() == "readme.md":
            continue
        try:
            rec = _read_template(file, source)
        except OSError as e:
            problems.append({"kind": file.stem, "field": "template", "code": "unreadable",
                             "message": f"{file}: {e}"})
            continue
        if rec["kind"] in rows:
            problems.append({"kind": rec["kind"], "field": "kind", "code": "duplicate",
                             "message": f"в {source} два шаблона вида {rec['kind']}: {file.name}"})
            continue
        rows[rec["kind"]] = rec
    return rows, problems


def _prompt_path(root: Path, rec: dict) -> tuple:
    """(путь к промпту, есть ли файл). Проект переопределяет китовый промпт тем же именем."""
    raw = str(rec.get("prompt") or "").strip()
    if not raw:
        raw = f"{PROMPTS}/{rec['kind']}.md"
    rel = Path(raw)
    candidates = []
    if rel.is_absolute():
        candidates.append(rel)
    else:
        candidates.append(Path(root) / rel)
        if rel.parts and rel.parts[0] == PROMPTS:
            candidates.append(KIT / "scaffold" / rel)
        else:
            candidates.append(KIT / "scaffold" / PROMPTS / rel.name)
            candidates.append(Path(root) / PROMPTS / rel.name)
    for path in candidates:
        if path.is_file():
            return str(path), True
    return str(candidates[0]), False


def effective_kinds(root=".") -> tuple:
    """({kind: запись}, замечания). Сначала кит, затем проект поверх по `kind`."""
    kit, problems = _read_dir(KIT / "scaffold" / TEMPLATES, "kit")
    project, probs = _read_dir(Path(root) / TEMPLATES, "project")
    problems += probs
    merged = dict(kit)
    merged.update(project)
    for rec in merged.values():
        rec["prompt_path"], rec["prompt_exists"] = _prompt_path(Path(root), rec)
    return dict(sorted(merged.items(), key=lambda kv: (kv[1]["order"], kv[0]))), problems


def check(root=".") -> tuple:
    """(записи, замечания). Пустой список замечаний — настройка годна."""
    rows, problems = effective_kinds(root)
    for rec in rows.values():
        if rec["scope"] not in SCOPES:
            problems.append({"kind": rec["kind"], "field": "scope", "code": "scope",
                             "message": f"неизвестный scope «{rec['scope']}»; допустимы: {', '.join(SCOPES)}"})
        if rec["purity"] not in PURITIES:
            problems.append({"kind": rec["kind"], "field": "purity", "code": "purity",
                             "message": f"неизвестная purity «{rec['purity']}»; допустимы: {', '.join(PURITIES)}"})
        out = Path(rec["out"])
        if out.is_absolute() or ".." in out.parts or not str(rec["out"]).replace("\\", "/").startswith(
                ("Artifacts/", "CleanRoom/")):
            problems.append({"kind": rec["kind"], "field": "out", "code": "out",
                             "message": f"папка результата «{rec['out']}» должна быть внутри Artifacts/ или CleanRoom/"})
        if not rec["prompt_exists"]:
            problems.append({"kind": rec["kind"], "field": "prompt", "code": "no_prompt",
                             "message": f"нет промпта {rec['prompt_path']}"})
        if rec["enabled"] and "Что не удалось установить по коду" not in rec["body"]:
            problems.append({"kind": rec["kind"], "field": "template", "code": "no_gap_section",
                             "message": "в шаблоне нет обязательного раздела «Что не удалось установить по коду»"})
    return rows, problems


def _line(rec: dict) -> str:
    state = "вкл" if rec["enabled"] else "выкл"
    return (f"{rec['kind']:<14} {state:<4} {rec['scope']:<11} {rec['out']:<34} "
            f"{Path(rec['template_path']).name} → {Path(rec['prompt_path']).name}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Артефакты по коду: шаблоны и промпты")
    p.add_argument("--project", default=".", help="корень проекта (по умолчанию текущая папка)")
    p.add_argument("--kind", default="", help="только один вид")
    p.add_argument("--show", default="", help="показать шаблон и пути вида")
    p.add_argument("--check", action="store_true", help="проверить шаблоны, промпты, папки и поля")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.add_argument("--list", action="store_true", help="таблица видов (по умолчанию)")
    args = p.parse_args(argv)
    root = Path(args.project).resolve()
    rows, problems = check(root)
    if args.show:
        rec = rows.get(args.show)
        if not rec:
            print(f"Вида «{args.show}» нет. Есть: {', '.join(rows)}", file=sys.stderr)
            return 1
        print(f"# {rec['kind']}: {rec['title']}")
        print(f"Шаблон: {rec['template_path']}")
        print(f"Промпт: {rec['prompt_path']} ({'есть' if rec['prompt_exists'] else 'НЕТ'})")
        print(f"Папка: {rec['out']}; scope: {rec['scope']}; purity: {rec['purity']}; "
              f"enabled: {'да' if rec['enabled'] else 'нет'}")
        print("\n" + rec["body"])
        return 0
    if args.kind:
        rec = rows.get(args.kind)
        if not rec:
            print(f"Вида «{args.kind}» нет. Есть: {', '.join(rows)}", file=sys.stderr)
            return 1
        rows = {args.kind: rec}
    if args.json:
        out = {k: {f: v for f, v in rec.items() if f != "body"} for k, rec in rows.items()}
        print(json.dumps({"kinds": out, "problems": problems}, ensure_ascii=False, indent=1))
        return 1 if (args.check and problems) else 0
    if args.check:
        for row in problems:
            print(f"✗ {row['kind']}: {row['field']} ({row['code']}): {row['message']}")
        print(f"code:artifacts --check: видов {len(rows)}, замечаний {len(problems)}")
        return 1 if problems else 0
    for rec in rows.values():
        print(_line(rec))
        for problem in problems:
            if problem["kind"] == rec["kind"]:
                print(f"  ✗ {problem['field']}: {problem['message']}")
    print(f"Видов: {len(rows)}; включено: {sum(1 for r in rows.values() if r['enabled'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
