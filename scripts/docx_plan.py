#!/usr/bin/env python3
"""docx_plan.py — декларативный план Word без исполняемого кода.

План W5 отвечает только на вопрос «какие разделы, разрывы и оглавление»,
а не «какой XML вставлять». Поэтому здесь нет eval/exec, импорта модельного
кода и чтения файлов из плана. Модель один раз возвращает JSON, движок
проверяет схему v1 и сам пишет план и паспорт.

  python3 scripts/docx_plan.py --project . --kind opz --plan plan.json \\
      --source Sources/opz.md --checks Prompts/opz_checks.json \\
      --docx Scripts/Generated/opz.docx --pandoc-version 3.13

Модуль намеренно не импортирует W1/W3: валидатор и runner работают на одном
JSON, а интеграция идёт через `validate_plan`, `collect_hashes`,
`stale_reasons` и `run_plan`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aurora_common import real_rel, write_text_atomic  # noqa: E402

PLAN_VERSION = 1
META_VERSION = 1
CHECKS_VERSION = 1
PLAN_SUFFIX = ".docx.json"
META_SUFFIX = ".docx.meta.json"
DEFAULT_TOC_DEPTH = 3
MAX_PLAN_BYTES = 256 * 1024
MAX_CHECKS_BYTES = 256 * 1024

HEX64 = re.compile(r"^[0-9a-f]{64}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")

PLAN_KEYS = frozenset({"version", "kind", "source_sha256", "sections",
                       "toc_depth", "notes"})
SECTION_KEYS = frozenset({"heading", "level", "page_break_before"})
CHECKS_KEYS = frozenset({
    "version", "kind", "required_sections", "title_fields",
    "toc", "page", "fonts", "images",
})
TOC_DEFAULT = {"enabled": True, "depth": DEFAULT_TOC_DEPTH, "title": "Содержание"}
PAGE_DEFAULT = {
    "size": "A4",
    "orientation": "portrait",
    "margins_mm": {"top": 20, "right": 15, "bottom": 20, "left": 25},
    "header_mm": 10,
    "footer_mm": 10,
}
FONTS_DEFAULT = {"body": "Times New Roman", "heading": "Times New Roman"}
IMAGES_DEFAULT = {"enabled": True, "max_count": 50, "max_bytes": 5242880,
                  "formats": ["png", "jpeg"]}
CHECKS_TOC_KEYS = frozenset({"enabled", "depth", "title"})
CHECKS_PAGE_KEYS = frozenset({"size", "orientation", "margins_mm",
                              "header_mm", "footer_mm"})
CHECKS_MARGIN_KEYS = frozenset({"top", "right", "bottom", "left"})
CHECKS_FONTS_KEYS = frozenset({"body", "heading"})
CHECKS_IMAGES_KEYS = frozenset({"enabled", "max_count", "max_bytes",
                                "formats", "logo_sha256"})
HASH_FIELDS = (
    ("source", "source_sha256"),
    ("docx_checks", "docx_checks_sha256"),
    ("prompt", "prompt_sha256"),
    ("template", "template_sha256"),
    ("example", "example_sha256"),
    ("reference", "reference_sha256"),
    ("plan", "plan_sha256"),
    ("logo", "logo_sha256"),
)
HASH_KEYS = tuple(key for key, _field in HASH_FIELDS)
INPUT_KEYS = tuple(key for key in HASH_KEYS if key != "plan")
META_FIELDS = frozenset({
    "version", "kind", "source_sha256", "docx_sha256",
    "docx_checks_sha256", "prompt_sha256", "template_sha256",
    "example_sha256", "reference_sha256", "plan_sha256", "logo_sha256",
    "logo_source", "toc_depth", "date", "source_date_epoch",
    "pandoc_version", "margins_mm", "margins_twips",
    "header_mm", "header_twips", "footer_mm", "footer_twips",
})


class PlanError(ValueError):
    """Ошибка плана с машинным кодом для отчёта и панели."""

    def __init__(self, code: str, why: str):
        super().__init__(why)
        self.code = code
        self.why = why

    def as_dict(self) -> Dict[str, str]:
        return {"ok": False, "code": self.code, "error": self.why}


def _fail(code: str, why: str) -> None:
    raise PlanError(code, why)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(value: Any, field: str, low: int, high: int,
          *, multiline: bool = False, code: str = "docx-plan-schema") -> str:
    if not isinstance(value, str):
        _fail(code, f"{field}: ожидается строка")
    if not low <= len(value) <= high:
        _fail(code, f"{field}: длина {len(value)}, нужно {low}..{high}")
    for ch in value:
        if ord(ch) < 32 and not (multiline and ch in "\n\t"):
            _fail(code, f"{field}: управляющий символ")
    return value


def _hash_value(value: Any, field: str, *, optional: bool = False,
                code: str = "docx-plan-schema") -> Optional[str]:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not HEX64.match(value):
        _fail(code, f"{field}: нужен sha256 из 64 строчных hex")
    return value


def _unknown_keys(data: Mapping[str, Any], allowed: Iterable[str], where: str,
                  *, code: str = "docx-plan-schema") -> None:
    extra = sorted(set(data) - set(allowed))
    if extra:
        _fail(code, f"{where}: неизвестные поля: {', '.join(extra)}")


def _number(value: Any, field: str, low: float, high: float,
            *, code: str = "docx-checks-schema") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(code, f"{field}: ожидается число")
    if not low <= float(value) <= high:
        _fail(code, f"{field}: {value}, нужно {low}..{high}")
    return float(value)


def _object(value: Any, field: str, allowed: Iterable[str],
            *, code: str = "docx-checks-schema") -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail(code, f"{field}: ожидается объект")
    _unknown_keys(value, allowed, field, code=code)
    return value


def _mm_to_twips(value: float) -> int:
    return int(value * 1440 / 25.4 + 0.5)


def validate_checks(data: Any, *, kind: Optional[str] = None) -> Dict[str, Any]:
    """Проверить `docx_checks` v1 без импорта W1/W3."""
    code = "docx-checks-schema"
    if not isinstance(data, dict):
        _fail(code, "docx_checks должен быть JSON-объектом")
    _unknown_keys(data, CHECKS_KEYS, "docx_checks", code=code)
    required = {"version", "kind", "required_sections", "title_fields"}
    missing = sorted(required - set(data))
    if missing:
        _fail(code, f"нет полей docx_checks: {', '.join(missing)}")
    if not _is_int(data.get("version")) or data.get("version") != CHECKS_VERSION:
        _fail("docx-checks-version",
              f"version docx_checks должен быть {CHECKS_VERSION}")
    checks_kind = data.get("kind")
    if not isinstance(checks_kind, str) or not KIND_RE.match(checks_kind):
        _fail(code, "kind docx_checks")
    if kind is not None and checks_kind != kind:
        _fail("docx-checks-kind", f"kind {checks_kind!r} не совпадает с {kind!r}")

    sections = data.get("required_sections")
    if not isinstance(sections, list) or len(sections) > 100:
        _fail(code, "required_sections: массив 0..100")
    clean_sections = []
    for index, row in enumerate(sections, 1):
        where = f"required_sections[{index}]"
        row = _object(row, where, {"level", "text"}, code=code)
        if set(row) != {"level", "text"}:
            _fail(code, f"{where}: нужны level и text")
        level = row.get("level")
        if not _is_int(level) or not 1 <= level <= 6:
            _fail(code, f"{where}.level: 1..6")
        clean_sections.append({
            "level": level,
            "text": _text(row.get("text"), f"{where}.text", 1, 200, code=code),
        })

    title_fields = data.get("title_fields")
    if not isinstance(title_fields, dict) or len(title_fields) > 100:
        _fail(code, "title_fields: объект 0..100")
    clean_fields = {}
    for key, value in title_fields.items():
        if not isinstance(key, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", key):
            _fail(code, f"title_fields: недопустимый ключ {key!r}")
        clean_fields[key] = _text(value, f"title_fields.{key}", 0, 1000,
                                  multiline=True, code=code)

    toc = dict(TOC_DEFAULT)
    if "toc" in data:
        raw = _object(data["toc"], "toc", CHECKS_TOC_KEYS, code=code)
        if set(raw) != CHECKS_TOC_KEYS:
            _fail(code, "toc: нужны enabled, depth, title")
        if not isinstance(raw.get("enabled"), bool):
            _fail(code, "toc.enabled: bool")
        if not _is_int(raw.get("depth")) or not 1 <= raw["depth"] <= 3:
            _fail(code, "toc.depth: 1..3")
        toc = {
            "enabled": raw["enabled"],
            "depth": raw["depth"],
            "title": _text(raw.get("title"), "toc.title", 1, 100, code=code),
        }

    page = {
        "size": PAGE_DEFAULT["size"],
        "orientation": PAGE_DEFAULT["orientation"],
        "margins_mm": dict(PAGE_DEFAULT["margins_mm"]),
        "header_mm": PAGE_DEFAULT["header_mm"],
        "footer_mm": PAGE_DEFAULT["footer_mm"],
    }
    if "page" in data:
        raw = _object(data["page"], "page", CHECKS_PAGE_KEYS, code=code)
        if set(raw) != CHECKS_PAGE_KEYS:
            _fail(code, "page: нужны size, orientation, margins_mm, header_mm, footer_mm")
        if raw.get("size") not in ("A4", "Letter"):
            _fail(code, "page.size: A4 или Letter")
        if raw.get("orientation") not in ("portrait", "landscape"):
            _fail(code, "page.orientation: portrait или landscape")
        margins = _object(raw.get("margins_mm"), "page.margins_mm",
                          CHECKS_MARGIN_KEYS, code=code)
        if set(margins) != CHECKS_MARGIN_KEYS:
            _fail(code, "page.margins_mm: top/right/bottom/left")
        page = {
            "size": raw["size"],
            "orientation": raw["orientation"],
            "margins_mm": {
                side: _number(margins.get(side), f"page.margins_mm.{side}", 5, 50)
                for side in ("top", "right", "bottom", "left")
            },
            "header_mm": _number(raw.get("header_mm"), "page.header_mm", 5, 30),
            "footer_mm": _number(raw.get("footer_mm"), "page.footer_mm", 5, 30),
        }

    fonts = dict(FONTS_DEFAULT)
    if "fonts" in data:
        raw = _object(data["fonts"], "fonts", CHECKS_FONTS_KEYS, code=code)
        if set(raw) != CHECKS_FONTS_KEYS:
            _fail(code, "fonts: нужны body и heading")
        fonts = {
            "body": _text(raw.get("body"), "fonts.body", 1, 100, code=code),
            "heading": _text(raw.get("heading"), "fonts.heading", 1, 100, code=code),
        }

    images = dict(IMAGES_DEFAULT)
    if "images" in data:
        raw = _object(data["images"], "images", CHECKS_IMAGES_KEYS, code=code)
        required_images = CHECKS_IMAGES_KEYS - {"logo_sha256"}
        if not required_images <= set(raw):
            _fail(code, "images: нужны enabled, max_count, max_bytes, formats")
        if not isinstance(raw.get("enabled"), bool):
            _fail(code, "images.enabled: bool")
        if not _is_int(raw.get("max_count")) or not 0 <= raw["max_count"] <= 50:
            _fail(code, "images.max_count: 0..50")
        if not _is_int(raw.get("max_bytes")) or not 1024 <= raw["max_bytes"] <= 5242880:
            _fail(code, "images.max_bytes: 1024..5242880")
        formats = raw.get("formats")
        if (not isinstance(formats, list) or not formats
                or len(set(formats)) != len(formats)
                or any(fmt not in ("png", "jpeg") for fmt in formats)):
            _fail(code, "images.formats: уникальные png/jpeg")
        images = {
            "enabled": raw["enabled"],
            "max_count": raw["max_count"],
            "max_bytes": raw["max_bytes"],
            "formats": list(formats),
            "logo_sha256": _hash_value(raw.get("logo_sha256"), "images.logo_sha256",
                                       optional=True, code=code),
        }

    return {
        "version": CHECKS_VERSION,
        "kind": checks_kind,
        "required_sections": clean_sections,
        "title_fields": clean_fields,
        "toc": toc,
        "page": page,
        "fonts": fonts,
        "images": images,
    }


def load_checks(path: str | os.PathLike, *, kind: Optional[str] = None) -> Dict[str, Any]:
    """Прочитать `docx_checks` и проверить его схему v1."""
    full = Path(path)
    try:
        raw = full.read_bytes()
    except OSError as exc:
        _fail("docx-checks-missing", f"docx_checks не прочитан: {exc}")
    if len(raw) > MAX_CHECKS_BYTES:
        _fail("docx-checks-size", f"docx_checks больше {MAX_CHECKS_BYTES} байт")
    try:
        text = raw.decode("utf-8")
        data = json.loads(
            text,
            object_pairs_hook=lambda pairs: _duplicate_hook(
                pairs, code="docx-checks-json"),
            parse_constant=lambda value: _json_constant(
                value, code="docx-checks-json"))
    except PlanError:
        raise
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        _fail("docx-checks-json", f"docx_checks не разобран: {exc}")
    return validate_checks(data, kind=kind)


def _duplicate_hook(pairs: Sequence[Tuple[str, Any]],
                    *, code: str = "docx-plan-json") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            _fail(code, f"дубликат ключа: {key}")
        out[key] = value
    return out


def _json_constant(value: str, *, code: str = "docx-plan-json") -> None:
    _fail(code, f"недопустимое число JSON: {value}")


def loads_plan(text: str, *, kind: Optional[str] = None,
               toc_enabled: bool = True, toc_depth: Optional[int] = None,
               source_sha256: Optional[str] = None) -> Dict[str, Any]:
    """Разобрать и проверить JSON-план. Ничего не исполняет и не читает."""
    if not isinstance(text, str):
        _fail("docx-plan-json", "план должен быть текстом JSON")
    if len(text.encode("utf-8")) > MAX_PLAN_BYTES:
        _fail("docx-plan-size", f"план больше {MAX_PLAN_BYTES} байт")
    try:
        data = json.loads(text, object_pairs_hook=_duplicate_hook,
                          parse_constant=_json_constant)
    except PlanError:
        raise
    except (TypeError, ValueError) as exc:
        _fail("docx-plan-json", f"не разобрать JSON: {exc}")
    return validate_plan(data, kind=kind, toc_enabled=toc_enabled,
                         toc_depth=toc_depth, source_sha256=source_sha256)


def validate_plan(data: Any, *, kind: Optional[str] = None,
                  toc_enabled: bool = True, toc_depth: Optional[int] = None,
                  source_sha256: Optional[str] = None) -> Dict[str, Any]:
    """Проверить план v1. → нормализованный dict, иначе PlanError."""
    if not isinstance(data, dict):
        _fail("docx-plan-schema", "план должен быть JSON-объектом")
    _unknown_keys(data, PLAN_KEYS, "план")
    missing = sorted(PLAN_KEYS - {"toc_depth", "notes"} - set(data))
    if missing:
        _fail("docx-plan-schema", f"нет полей: {', '.join(missing)}")

    if not _is_int(data.get("version")) or data.get("version") != PLAN_VERSION:
        _fail("docx-plan-schema", f"version должен быть {PLAN_VERSION}")
    plan_kind = data.get("kind")
    if not isinstance(plan_kind, str) or not KIND_RE.match(plan_kind):
        _fail("docx-plan-schema", "kind: строчные латиница, цифры, точка, дефис")
    if kind is not None and plan_kind != kind:
        _fail("docx-plan-kind", f"kind {plan_kind!r} не совпадает с {kind!r}")

    source = _hash_value(data.get("source_sha256"), "source_sha256")
    if source_sha256 is not None and source != source_sha256:
        _fail("docx-plan-source", "source_sha256 не совпадает с исходником")

    sections = data.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= 500:
        _fail("docx-plan-schema", "sections: массив 1..500")
    clean_sections: List[Dict[str, Any]] = []
    for index, section in enumerate(sections, 1):
        where = f"sections[{index}]"
        if not isinstance(section, dict):
            _fail("docx-plan-schema", f"{where}: ожидается объект")
        _unknown_keys(section, SECTION_KEYS, where)
        if set(section) != SECTION_KEYS:
            _fail("docx-plan-schema", f"{where}: нужны heading, level, page_break_before")
        heading = _text(section.get("heading"), f"{where}.heading", 1, 200)
        level = section.get("level")
        if not _is_int(level) or not 1 <= level <= 6:
            _fail("docx-plan-schema", f"{where}.level: 1..6")
        page_break = section.get("page_break_before")
        if not isinstance(page_break, bool):
            _fail("docx-plan-schema", f"{where}.page_break_before: bool")
        clean_sections.append({"heading": heading, "level": level,
                               "page_break_before": page_break})

    clean: Dict[str, Any] = {
        "version": PLAN_VERSION,
        "kind": plan_kind,
        "source_sha256": source,
        "sections": clean_sections,
    }

    if "toc_depth" in data:
        depth = data["toc_depth"]
        if not _is_int(depth) or not 1 <= depth <= 3:
            _fail("docx-plan-schema", "toc_depth: 1..3")
        if toc_enabled and toc_depth is not None and depth != toc_depth:
            _fail("docx-plan-toc-depth",
                  f"toc_depth {depth} не совпадает с docx_checks {toc_depth}")
        clean["toc_depth"] = depth

    if "notes" in data:
        notes = data["notes"]
        if not isinstance(notes, list) or len(notes) > 100:
            _fail("docx-plan-schema", "notes: массив 0..100")
        clean_notes = [_text(note, f"notes[{i}]", 0, 500, multiline=True)
                       for i, note in enumerate(notes, 1)]
        clean["notes"] = clean_notes

    if toc_depth is not None and (not _is_int(toc_depth) or not 1 <= toc_depth <= 3):
        _fail("docx-plan-schema", "toc_depth ожидаемого TOC: 1..3")
    return clean


def load_plan(path: str | os.PathLike, **kwargs: Any) -> Dict[str, Any]:
    """Прочитать план из файла и проверить."""
    full = Path(path)
    try:
        raw = full.read_bytes()
    except OSError as exc:
        _fail("docx-plan-missing", f"план не прочитан: {exc}")
    if len(raw) > MAX_PLAN_BYTES:
        _fail("docx-plan-size", f"план больше {MAX_PLAN_BYTES} байт")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail("docx-plan-json", f"план не UTF-8: {exc}")
    return loads_plan(text, **kwargs)


def dump_plan(plan: Mapping[str, Any]) -> str:
    """Канонический JSON плана: одинаковый вход даёт одинаковые байты."""
    return json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def plan_digest(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(dump_plan(plan).encode("utf-8")).hexdigest()


def effective_toc_depth(plan: Mapping[str, Any], *, toc_enabled: bool = True,
                        checks_depth: Optional[int] = None) -> Optional[int]:
    """Глубина TOC: план, иначе docx_checks, иначе 3; при выключенном TOC None."""
    if not toc_enabled:
        return None
    value = plan.get("toc_depth")
    if value is not None:
        return int(value)
    if checks_depth is not None:
        return int(checks_depth)
    return DEFAULT_TOC_DEPTH


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
    except OSError as exc:
        _fail("docx-plan-missing-input", f"не прочитан {path}: {exc}")
    return digest.hexdigest()


def _resolve_input(root: Path, rel: Any, field: str, *, required: bool = True) -> Optional[Path]:
    if rel is None:
        return None
    if not isinstance(rel, (str, os.PathLike)):
        _fail("docx-plan-path", f"{field}: ожидается путь")
    text = str(rel).strip()
    if not text:
        return None
    if os.path.isabs(text):
        _fail("docx-plan-path", f"{field}: абсолютный путь запрещён")
    full = root / text
    inside = real_rel(str(root), str(full))
    if not inside:
        _fail("docx-plan-path", f"{field}: путь вне проекта")
    path = root / inside
    if not path.is_file():
        if required:
            _fail("docx-plan-missing-input", f"{field}: файл не найден")
        return None
    return path


def collect_hashes(project: str | os.PathLike, paths: Mapping[str, Any],
                   *, plan: Optional[Mapping[str, Any]] = None) -> Dict[str, Optional[str]]:
    """sha256 всех входов плана. Отсутствующий вход это None, а не выдуманный хеш."""
    root = Path(project).resolve()
    if not root.is_dir():
        _fail("docx-plan-path", "корень проекта не найден")
    _unknown_keys(paths, INPUT_KEYS, "inputs")
    out: Dict[str, Optional[str]] = {}
    for key in INPUT_KEYS:
        rel = paths.get(key)
        path = _resolve_input(root, rel, key, required=rel is not None)
        out[key] = _sha_file(path) if path is not None else None
    if plan is not None:
        out["plan"] = plan_digest(plan)
    return out


def parse_meta_date(value: str) -> Tuple[str, int]:
    """RFC3339 или YYYY-MM-DD → (UTC RFC3339, epoch seconds)."""
    text = str(value or "").strip()
    if not text:
        _fail("docx-plan-date", "date пуст")
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            when = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            when = when.astimezone(timezone.utc)
    except ValueError as exc:
        _fail("docx-plan-date", f"date не разобран: {exc}")
    return when.strftime("%Y-%m-%dT%H:%M:%SZ"), int(when.timestamp())


def date_from_source(path: Optional[Path]) -> str:
    """Дата из шапки исходника: строка `date:` между первыми `---`."""
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = re.match(r"^\s*date\s*:\s*(.*?)\s*$", line)
        if match:
            return match.group(1).strip().strip("\"'")
    return ""


def make_meta(kind: str, hashes: Mapping[str, Optional[str]], *,
              toc_depth: Optional[int], docx_sha256: str, date: str,
              source_date_epoch: int, pandoc_version: str,
              margins_mm: Mapping[str, Any], margins_twips: Mapping[str, Any],
              header_mm: float, header_twips: int, footer_mm: float,
              footer_twips: int, logo_source: Optional[str] = None) -> Dict[str, Any]:
    """Паспорт плана. Все поля R5 3.10 заполнены, кроме допустимых null."""
    if logo_source not in (None, "docx_logo", "docx_template"):
        _fail("docx-plan-schema", "logo_source: docx_logo, docx_template или null")
    if toc_depth is not None and (not _is_int(toc_depth) or not 1 <= toc_depth <= 3):
        _fail("docx-plan-schema", "toc_depth meta: 1..3 или null")
    _hash_value(docx_sha256, "docx_sha256")
    _text(date, "date", 1, 40)
    if not _is_int(source_date_epoch) or source_date_epoch < 0:
        _fail("docx-plan-schema", "source_date_epoch: целые секунды")
    _text(pandoc_version, "pandoc_version", 1, 100)
    for field, value in (("margins_mm", margins_mm), ("margins_twips", margins_twips)):
        if not isinstance(value, Mapping) or set(value) != {"top", "right", "bottom", "left"}:
            _fail("docx-plan-schema", f"{field}: top/right/bottom/left")
    for side, value in margins_mm.items():
        _number(value, f"margins_mm.{side}", 5, 50, code="docx-plan-schema")
    for side, value in margins_twips.items():
        if not _is_int(value) or value < 0:
            _fail("docx-plan-schema", f"margins_twips.{side}: целые twips")
    for field, value in (("header_mm", header_mm), ("footer_mm", footer_mm)):
        _number(value, field, 5, 30, code="docx-plan-schema")
    for field, value in (("header_twips", header_twips), ("footer_twips", footer_twips)):
        if not _is_int(value) or value < 0:
            _fail("docx-plan-schema", f"{field}: целые twips")
    meta: Dict[str, Any] = {
        "version": META_VERSION,
        "kind": kind,
        "docx_sha256": docx_sha256,
        "logo_source": logo_source,
        "toc_depth": toc_depth,
        "date": date,
        "source_date_epoch": source_date_epoch,
        "pandoc_version": pandoc_version,
        "margins_mm": dict(margins_mm),
        "margins_twips": dict(margins_twips),
        "header_mm": header_mm,
        "header_twips": header_twips,
        "footer_mm": footer_mm,
        "footer_twips": footer_twips,
    }
    for key, field in HASH_FIELDS:
        meta[field] = hashes.get(key)
    return meta


def validate_meta(meta: Any) -> Dict[str, Any]:
    """Строгая проверка паспорта v1 по R5 3.10."""
    if not isinstance(meta, dict):
        _fail("docx-meta-schema", "meta должен быть JSON-объектом")
    _unknown_keys(meta, META_FIELDS, "meta")
    missing = sorted(META_FIELDS - set(meta))
    if missing:
        _fail("docx-meta-schema", f"нет полей meta: {', '.join(missing)}")
    if not _is_int(meta.get("version")) or meta.get("version") != META_VERSION:
        _fail("docx-meta-schema", f"version meta должен быть {META_VERSION}")
    if not isinstance(meta.get("kind"), str) or not KIND_RE.match(meta["kind"]):
        _fail("docx-meta-schema", "kind meta")
    for key, field in HASH_FIELDS:
        optional = key in ("prompt", "template", "example", "reference", "logo")
        _hash_value(meta.get(field), field, optional=optional)
    _hash_value(meta.get("docx_sha256"), "docx_sha256")
    if meta.get("logo_source") not in (None, "docx_logo", "docx_template"):
        _fail("docx-meta-schema", "logo_source meta")
    if meta.get("logo_source") and not meta.get("logo_sha256"):
        _fail("docx-meta-schema", "logo_source без logo_sha256")
    if meta.get("toc_depth") is not None and (
            not _is_int(meta["toc_depth"]) or not 1 <= meta["toc_depth"] <= 3):
        _fail("docx-meta-schema", "toc_depth meta: 1..3 или null")
    _text(meta.get("date"), "date", 1, 40, code="docx-meta-schema")
    if not _is_int(meta.get("source_date_epoch")) or meta["source_date_epoch"] < 0:
        _fail("docx-meta-schema", "source_date_epoch: целые секунды")
    try:
        _date_iso, date_epoch = parse_meta_date(meta["date"])
    except PlanError:
        _fail("docx-meta-schema", "date: RFC3339 UTC")
    if date_epoch != meta["source_date_epoch"]:
        _fail("docx-meta-schema", "source_date_epoch не совпадает с date")
    _text(meta.get("pandoc_version"), "pandoc_version", 1, 100, code="docx-meta-schema")
    for field in ("margins_mm", "margins_twips"):
        value = meta.get(field)
        if not isinstance(value, dict) or set(value) != {"top", "right", "bottom", "left"}:
            _fail("docx-meta-schema", f"{field}: top/right/bottom/left")
    for side in ("top", "right", "bottom", "left"):
        _number(meta["margins_mm"][side], f"margins_mm.{side}", 5, 50,
                code="docx-meta-schema")
        if not _is_int(meta["margins_twips"][side]) or meta["margins_twips"][side] < 0:
            _fail("docx-meta-schema", f"margins_twips.{side}: целые twips")
    for field in ("header_mm", "footer_mm"):
        _number(meta.get(field), field, 5, 30, code="docx-meta-schema")
    for field in ("header_twips", "footer_twips"):
        if not _is_int(meta.get(field)) or meta[field] < 0:
            _fail("docx-meta-schema", f"{field}: целые twips")
    return meta


def stale_reasons(meta: Mapping[str, Any], current: Mapping[str, Any]) -> List[str]:
    """Почему паспорт устарел. Сравниваются все хеши и эффективный TOC/логотип."""
    out: List[str] = []
    for key, field in HASH_FIELDS:
        if meta.get(field) != current.get(key):
            out.append(f"{key}: hash changed")
    if meta.get("toc_depth") != current.get("toc_depth"):
        out.append("toc_depth: changed")
    if meta.get("logo_source") != current.get("logo_source"):
        out.append("logo_source: changed")
    if meta.get("date") != current.get("date"):
        out.append("date: changed")
    if meta.get("pandoc_version") != current.get("pandoc_version"):
        out.append("pandoc_version: changed")
    return out


def is_stale(meta: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    return bool(stale_reasons(meta, current))


def _out_dir(project: Path, rel: str) -> Path:
    if not isinstance(rel, str) or not rel.strip():
        _fail("docx-plan-path", "out-dir пуст")
    if os.path.isabs(rel):
        _fail("docx-plan-path", "out-dir: абсолютный путь запрещён")
    inside = real_rel(str(project), str(project / rel))
    if not inside:
        _fail("docx-plan-path", "out-dir вне проекта")
    path = project / inside
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _fail("docx-plan-path", f"out-dir не создан: {exc}")
    if not path.is_dir():
        _fail("docx-plan-path", "out-dir не папка")
    return path


def _write_json(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        write_text_atomic(str(path), text)
    except OSError as exc:
        _fail("docx-plan-write", f"не записан {path}: {exc}")


def run_plan(project: str | os.PathLike, kind: str, plan_path: str | os.PathLike,
             *, checks: str | os.PathLike, docx_path: Optional[str | os.PathLike] = None,
             docx_sha256: Optional[str] = None,
             inputs: Optional[Mapping[str, Any]] = None,
             out_dir: str = "Scripts/Generated",
             toc_depth: Optional[int] = None, date: str = "",
             pandoc_version: str = "",
             logo_source: Optional[str] = None) -> Dict[str, Any]:
    """Проверить план и docx_checks, заполнить полный meta R5 3.10."""
    root = Path(project).resolve()
    if not root.is_dir():
        _fail("docx-plan-path", "корень проекта не найден")
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        _fail("docx-plan-schema", "kind: недопустимое имя вида")
    if toc_depth is not None and (not _is_int(toc_depth) or not 1 <= toc_depth <= 3):
        _fail("docx-plan-schema", "toc_depth: 1..3")
    plan_file = _resolve_input(root, plan_path, "plan", required=True)
    checks_file = _resolve_input(root, checks, "docx_checks", required=True)
    checks_data = load_checks(checks_file, kind=kind)
    toc_enabled = checks_data["toc"]["enabled"]
    checks_depth = checks_data["toc"]["depth"]
    if toc_depth is not None and toc_enabled and toc_depth != checks_depth:
        _fail("docx-plan-toc-depth",
              f"toc_depth {toc_depth} не совпадает с docx_checks {checks_depth}")
    plan = load_plan(plan_file, kind=kind, toc_enabled=toc_enabled,
                     toc_depth=checks_depth if toc_enabled else None)
    paths = dict(inputs or {})
    paths["docx_checks"] = real_rel(str(root), str(checks_file))
    hashes = collect_hashes(root, paths, plan=plan)
    if paths.get("source"):
        if hashes.get("source") != plan["source_sha256"]:
            _fail("docx-plan-source", "source_sha256 не совпадает с исходником")
    else:
        hashes["source"] = plan["source_sha256"]
    source_path = _resolve_input(root, paths.get("source"), "source",
                                 required=bool(paths.get("source")))
    date_text = date or date_from_source(source_path)
    if not date_text:
        _fail("docx-plan-date", "нет --date и date в шапке исходника")
    date_iso, date_epoch = parse_meta_date(date_text)
    if not str(pandoc_version or "").strip():
        _fail("docx-plan-pandoc", "pandoc_version обязателен")
    if docx_path is None and docx_sha256 is None:
        _fail("docx-plan-docx", "нужен docx_path или docx_sha256")
    if docx_path is not None:
        docx_file = _resolve_input(root, docx_path, "docx", required=True)
        actual_docx = _sha_file(docx_file)
        if docx_sha256 is not None and docx_sha256 != actual_docx:
            _fail("docx-plan-docx", "docx_sha256 не совпадает с файлом")
        docx_sha256 = actual_docx
    else:
        docx_sha256 = _hash_value(docx_sha256, "docx_sha256")

    effective = effective_toc_depth(plan, toc_enabled=toc_enabled,
                                    checks_depth=checks_depth)
    hashes["plan"] = plan_digest(plan)
    allowed_logo = checks_data["images"].get("logo_sha256")
    actual_logo = hashes.get("logo")
    if actual_logo and not allowed_logo:
        _fail("docx-plan-logo",
              "логотип не разрешён: нет docx_checks.images.logo_sha256")
    if logo_source and not allowed_logo:
        _fail("docx-plan-logo",
              "logo_source без docx_checks.images.logo_sha256")
    if logo_source and not actual_logo:
        _fail("docx-plan-logo", "logo_source задан, но файл логотипа не найден")
    if allowed_logo and actual_logo and actual_logo != allowed_logo:
        _fail("docx-plan-logo",
              "logo_sha256 не совпадает с файлом логотипа")
    page = checks_data["page"]
    margins_mm = page["margins_mm"]
    margins_twips = {side: _mm_to_twips(value) for side, value in margins_mm.items()}
    meta = make_meta(
        kind, hashes, toc_depth=effective, docx_sha256=docx_sha256,
        date=date_iso, source_date_epoch=date_epoch,
        pandoc_version=str(pandoc_version).strip(),
        margins_mm=margins_mm, margins_twips=margins_twips,
        header_mm=page["header_mm"],
        header_twips=_mm_to_twips(page["header_mm"]),
        footer_mm=page["footer_mm"],
        footer_twips=_mm_to_twips(page["footer_mm"]),
        logo_source=logo_source,
    )
    validate_meta(meta)
    target = _out_dir(root, out_dir)
    plan_file = target / f"{kind}{PLAN_SUFFIX}"
    meta_file = target / f"{kind}{META_SUFFIX}"
    _write_json(plan_file, plan)
    _write_json(meta_file, meta)
    return {
        "ok": True,
        "plan": str(plan_file),
        "meta": str(meta_file),
        "toc_depth": effective,
        "hashes": hashes,
        "meta_data": meta,
    }


def _cli() -> int:
    parser = argparse.ArgumentParser(
        description="Проверить декларативный Word-план и записать meta")
    parser.add_argument("--project", default=".", help="корень проекта")
    parser.add_argument("--kind", required=True, help="вид артефакта")
    parser.add_argument("--plan", required=True, help="входной JSON-план")
    parser.add_argument("--checks", required=True, help="docx_checks v1")
    parser.add_argument("--docx", required=True, help="собранный docx для meta")
    parser.add_argument("--pandoc-version", required=True)
    parser.add_argument("--date", default="")
    parser.add_argument("--out-dir", default="Scripts/Generated")
    parser.add_argument("--source", default="")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--template", default="")
    parser.add_argument("--example", default="")
    parser.add_argument("--reference", default="")
    parser.add_argument("--logo", default="")
    parser.add_argument("--logo-source", choices=("docx_logo", "docx_template"),
                        default=None)
    parser.add_argument("--toc-depth", type=int, default=None)
    args = parser.parse_args()
    paths = {
        "source": args.source,
        "prompt": args.prompt,
        "template": args.template,
        "example": args.example,
        "reference": args.reference,
        "logo": args.logo,
    }
    paths = {key: value for key, value in paths.items() if value}
    try:
        result = run_plan(args.project, args.kind, args.plan, inputs=paths,
                          checks=args.checks, docx_path=args.docx,
                          out_dir=args.out_dir, toc_depth=args.toc_depth,
                          date=args.date, pandoc_version=args.pandoc_version,
                          logo_source=args.logo_source)
    except PlanError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({
        "ok": True,
        "plan": result["plan"],
        "meta": result["meta"],
        "toc_depth": result["toc_depth"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
