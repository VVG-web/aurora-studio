#!/usr/bin/env python3
"""code_artifacts.py — какие артефакты можно собрать по коду, по каким шаблонам и промптам.

Основа настройки живёт в папках:

  TemplatesCode/<вид>_v1.0.md   паспорт вида и форма документа
  PromptCode/<вид>.md           правила письма для этого вида

Кит привозит преднастроенные виды. Проект переопределяет любой из них файлом с тем же
`kind` в своей `TemplatesCode/`, добавляет новые или выключает ненужные `enabled: "false"`.
Панель Code дополнительно хранит проектные переопределения `out`, `template`, `prompt` и
`enabled` в блоке `code_artifacts:` файла `aurora.config.yaml`; они применяются поверх
шаблонов и не переписывают остальной конфиг.

  python3 .aurora/scripts/code_artifacts.py --list
  python3 .aurora/scripts/code_artifacts.py --kind cr-api
  python3 .aurora/scripts/code_artifacts.py --show cr-api
  python3 .aurora/scripts/code_artifacts.py --check
  python3 .aurora/scripts/code_artifacts.py --json

Панель: `code:artifacts`.
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
from aurora_common import frontmatter  # noqa: E402

KIT = Path(__file__).resolve().parents[1]
TEMPLATES = "TemplatesCode"
PROMPTS = "PromptCode"
CONFIG = "aurora.config.yaml"
OVERRIDES = "code_artifacts"
OVERRIDE_FIELDS = ("out", "template", "prompt", "enabled")
ARTIFACT_ROOTS = ("Artifacts", "ArtifactsCode", "CleanRoom")
SCOPES = ("symbol", "route", "controller", "module", "table", "topic", "pair", "component", "service")
PURITIES = ("traceable", "strict")
DEFAULT_OUT = "ArtifactsCode"
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
        "out": str(meta.get("out") or f"{DEFAULT_OUT}/{kind}").strip() or f"{DEFAULT_OUT}/{kind}",
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


def _kit_root(root=".") -> Path:
    """Каталог кита: в репозитории — рядом со scripts, в проекте — из kit_path.txt."""
    here = Path(__file__).resolve().parents[1]
    if (here / "scaffold" / TEMPLATES).is_dir():
        return here
    hint = Path(root) / ".aurora" / "kit_path.txt"
    try:
        kit = Path(hint.read_text(encoding="utf-8").strip()).expanduser()
    except (OSError, UnicodeDecodeError):
        return here
    return kit if (kit / "scaffold" / TEMPLATES).is_dir() else here


def _prompt_path(root: Path, rec: dict, kit_root: Path | None = None) -> tuple:
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
            candidates.append((kit_root or KIT) / "scaffold" / rel)
        else:
            candidates.append((kit_root or KIT) / "scaffold" / PROMPTS / rel.name)
            candidates.append(Path(root) / PROMPTS / rel.name)
    for path in candidates:
        if path.is_file():
            return str(path), True
    return str(candidates[0]), False


_OVERRIDES_RX = re.compile(
    r"(?ms)^code_artifacts:[ \t]*(?:#.*)?\r?\n"
    r"(?:[ \t]+.*\r?\n|[ \t]*\r?\n)*(?=^\S|\Z)")
_KIND_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _unquote(raw: str) -> str:
    raw = str(raw or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return re.sub(r"\s+#.*$", "", raw).strip()


def _bool_or_none(value):
    text = _unquote(value).lower()
    if text in ("true", "yes", "on", "1", "да"):
        return True
    if text in ("false", "no", "off", "0", "нет"):
        return False
    return None


def read_overrides(root=".") -> dict:
    """{kind: {out/template/prompt/enabled}} из блока `code_artifacts:` проекта."""
    path = Path(root) / CONFIG
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if re.fullmatch(r"code_artifacts:[ \t]*(?:#.*)?", line.rstrip("\r"))), None)
    if start is None:
        return {}
    out: dict = {}
    current = ""
    for line in lines[start + 1:]:
        line = line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] not in (" ", "\t"):
            break
        indent = len(line) - len(line.lstrip())
        m = re.match(r"\s*([\w.-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if indent <= 2 and not value.strip():
            current = key
            out.setdefault(current, {})
        elif current and indent >= 4 and key in OVERRIDE_FIELDS:
            if key == "enabled":
                flag = _bool_or_none(value)
                if flag is not None:
                    out[current][key] = flag
            else:
                raw = _unquote(value)
                if raw:
                    out[current][key] = raw
    return {kind: rec for kind, rec in out.items() if rec}


def overrides_digest(root=".") -> str:
    """Слепок блока настроек: комментарии и порядок полей не считаются изменением."""
    data = json.dumps(read_overrides(root), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _relative_file(root: Path, raw: str, field: str) -> tuple:
    """(относительный путь, проблема) для файла шаблона/промпта проекта."""
    raw = str(raw or "").strip().replace("\\", "/")
    if not raw:
        return "", ""
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        return "", f"{field}: путь должен быть внутри проекта"
    path = root / path
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return "", f"{field}: путь выходит за пределы проекта"
    if path.suffix.lower() != ".md":
        return "", f"{field}: нужен .md-файл"
    if not path.is_file():
        return "", f"{field}: файл не найден"
    return path.relative_to(root).as_posix(), ""


def out_problem(out: str) -> str:
    """Пусто — папка допустима. Путь только внутри Artifacts/, ArtifactsCode/ или CleanRoom/."""
    text = str(out or "").strip().strip('"').replace("\\", "/").rstrip("/")
    if not text:
        return ""
    if os.path.isabs(text) or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return "путь абсолютный, а папка результата лежит внутри проекта"
    if ".." in text.split("/"):
        return "путь выходит за пределы проекта"
    if not any(text == root or text.startswith(root + "/") for root in ARTIFACT_ROOTS):
        return "папка результата должна быть внутри Artifacts/, ArtifactsCode/ или CleanRoom/"
    if re.search(r"\.[A-Za-z][A-Za-z0-9]{1,4}$", text.split("/")[-1]):
        return "путь похож на файл, а не на папку"
    return ""


def _apply_overrides(root: Path, rows: dict) -> tuple:
    overrides = read_overrides(root)
    problems = []
    for kind, ov in overrides.items():
        rec = rows.get(kind)
        template = ov.get("template")
        if template:
            rel, why = _relative_file(root, template, "template")
            if why:
                problems.append({"kind": kind, "field": "template", "code": "template", "message": why})
                continue
            try:
                rec = _read_template(root / rel, "project")
                rec["kind"] = kind
            except OSError as e:
                problems.append({"kind": kind, "field": "template", "code": "unreadable",
                                 "message": f"{rel}: {e}"})
                continue
        if rec is None:
            problems.append({"kind": kind, "field": "kind", "code": "unknown",
                             "message": "вид не найден и шаблон не задан"})
            continue
        rec = dict(rec)
        if ov.get("out"):
            rec["out"] = ov["out"]
        if ov.get("prompt"):
            rec["prompt"] = ov["prompt"]
        if "enabled" in ov:
            rec["enabled"] = ov["enabled"]
        rows[kind] = rec
    for rec in rows.values():
        rec["prompt_path"], rec["prompt_exists"] = _prompt_path(root, rec)
    return rows, problems


def _render_overrides(overrides: dict) -> str:
    lines = ["code_artifacts:"]
    for kind in sorted(overrides):
        lines.append(f"  {kind}:")
        for field in OVERRIDE_FIELDS:
            if field not in overrides[kind]:
                continue
            value = overrides[kind][field]
            if field == "enabled":
                value = "true" if value else "false"
            lines.append(f"    {field}: {value}")
    return "\n".join(lines) + "\n"


def save(root=".", kinds=None, expect: str = "") -> dict:
    """Дописать поля в блок `code_artifacts:`; остальной конфиг не меняется.

    Панель присылает только изменённые поля вида, поэтому прежние переопределения
    сохраняются. Пустое значение `out`/`template`/`prompt` снимает это переопределение.
    """
    root = Path(root)
    if not isinstance(kinds, dict):
        return {"ok": False, "code": "kinds", "error": "нужен объект видов"}
    digest = overrides_digest(root)
    if expect and expect != digest:
        return {"ok": False, "conflict": True, "code": "conflict", "digest": digest}
    clean = {kind: dict(rec) for kind, rec in read_overrides(root).items()}
    for kind, raw in kinds.items():
        if not _KIND_RX.fullmatch(str(kind or "")):
            return {"ok": False, "code": "kind", "error": f"негодное имя вида: {kind}"}
        rec = raw if isinstance(raw, dict) else {}
        ov = clean.setdefault(kind, {})
        if "out" in rec:
            out = str(rec.get("out") or "").strip()
            if not out:
                ov.pop("out", None)
            else:
                why = out_problem(out)
                if why:
                    return {"ok": False, "code": "out", "error": f"{kind}: {why}"}
                ov["out"] = out
        for field in ("template", "prompt"):
            if field not in rec:
                continue
            value = str(rec.get(field) or "").strip()
            if not value:
                ov.pop(field, None)
            else:
                rel, why = _relative_file(root, value, field)
                if why:
                    return {"ok": False, "code": field, "error": f"{kind}: {why}"}
                ov[field] = rel
        if "enabled" in rec:
            ov["enabled"] = bool(rec["enabled"])
        if not ov:
            clean.pop(kind, None)
    text = ""
    path = root / CONFIG
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass
    block = _render_overrides(clean)
    match = _OVERRIDES_RX.search(text)
    if match:
        text = text[:match.start()] + block + text[match.end():]
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        if text and not text.endswith("\n\n"):
            text += "\n"
        text += block
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as e:
        return {"ok": False, "code": "write", "error": str(e)}
    return {"ok": True, "digest": overrides_digest(root)}


def effective_kinds(root=".") -> tuple:
    """({kind: запись}, замечания). Сначала кит, затем проект поверх по `kind`."""
    kit_root = _kit_root(root)
    kit, problems = _read_dir(kit_root / "scaffold" / TEMPLATES, "kit")
    project, probs = _read_dir(Path(root) / TEMPLATES, "project")
    problems += probs
    merged = dict(kit)
    merged.update(project)
    merged, probs = _apply_overrides(Path(root), merged)
    problems += probs
    for rec in merged.values():
        rec["prompt_path"], rec["prompt_exists"] = _prompt_path(Path(root), rec, kit_root)
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
        why = out_problem(rec["out"])
        if why:
            problems.append({"kind": rec["kind"], "field": "out", "code": "out",
                             "message": f"папка результата «{rec['out']}»: {why}"})
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
