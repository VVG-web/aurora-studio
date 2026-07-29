#!/usr/bin/env python3
"""export_doc.py — поставляемый документ → docx/pdf/html (фреймворк «Аврора»).

Заказчик принимает не markdown. Документы собираются в `Deliverables/work/` из базы
(`assemble`), а отдаются в офисном формате — до сих пор это делалось руками.

  python3 .opencode/scripts/export_doc.py Deliverables/work/ОПЗ_v1.md
  python3 .opencode/scripts/export_doc.py Deliverables/work/ОПЗ_v1.md --format pdf
  python3 .opencode/scripts/export_doc.py Deliverables/work/ОПЗ_v1.md --reference Templates/docx/gost.docx

Что делает:
  1. Проверяет провенанс: у документа должен быть `based_on` (иначе непонятно, из какого
     знания он собран) — предупреждает, но не блокирует.
  2. Убирает служебное: frontmatter в экспорт не идёт; wiki-ссылки `[[Карточка]]`
     разрезолвливаются в текст (снаружи базы они не кликаются) — как в spec-pack.
  3. Конвертирует pandoc'ом; `--reference` подставляет фирменный шаблон оформления .docx.
  4. Кладёт результат рядом с исходником и печатает путь.

Требует pandoc (`brew install pandoc`); для pdf — ещё и движок LaTeX/typst.
Экспорт — производная копия: истина остаётся в markdown, в git.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

LINK_RE = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")


def strip_frontmatter(text: str) -> tuple:
    if not text.startswith("---"):
        return text, {}
    end = text.find("\n---", 3)
    if end == -1:
        return text, {}
    fm = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([\w_]+)\s*:(.*)$", line)
        if m:
            fm[m.group(1)] = m.group(2).strip()
    nl = text.find("\n", end + 1)
    return (text[nl + 1:] if nl != -1 else ""), fm


def flatten_links(text: str) -> str:
    """[[Карточка|Показ]] → «Показ»; [[Карточка]] → «Карточка» (без обратных ссылок)."""
    return LINK_RE.sub(lambda m: (m.group(2) or m.group(1)).replace("-", " "), text)


def main() -> int:
    ap = argparse.ArgumentParser(description="Экспорт поставляемого документа в офисный формат")
    ap.add_argument("source", help="markdown-файл (обычно из Deliverables/work/)")
    ap.add_argument("--format", default="docx", choices=["docx", "pdf", "html", "odt"],
                    help="формат: docx или pdf")
    ap.add_argument("--reference", help="эталон оформления .docx (pandoc --reference-doc)")
    ap.add_argument("--out", help="куда положить (по умолчанию рядом с исходником)")
    ap.add_argument("--keep-links", action="store_true", help="не разрезолвливать wiki-ссылки")
    a = ap.parse_args()

    if not os.path.isfile(a.source):
        print(f"export_doc: нет файла {a.source}", file=sys.stderr)
        return 1
    if not shutil.which("pandoc"):
        print("export_doc: нужен pandoc — `brew install pandoc` (или apt/choco)", file=sys.stderr)
        return 1

    raw = open(a.source, encoding="utf-8").read()
    body, fm = strip_frontmatter(raw)
    if not a.keep_links:
        body = flatten_links(body)

    warnings = []
    if not fm.get("based_on", "").strip("[] "):
        warnings.append("нет `based_on` — непонятно, из каких карточек собран документ "
                        "(assemble обязан его заполнять)")
    if fm.get("type") != "deliverable" and "/Deliverables/" in os.path.abspath(a.source):
        warnings.append("frontmatter без `type: deliverable`")

    out = a.out or os.path.splitext(a.source)[0] + "." + a.format
    cmd = ["pandoc", "-f", "gfm", "-o", out, "--from", "gfm", "--standalone"]
    if a.reference and a.format == "docx":
        if not os.path.isfile(a.reference):
            print(f"export_doc: нет эталона оформления {a.reference}", file=sys.stderr)
            return 1
        cmd += ["--reference-doc", a.reference]
    title = fm.get("title", "").strip('"') or os.path.basename(os.path.splitext(a.source)[0])
    cmd += ["--metadata", f"title={title}"]

    try:
        proc = subprocess.run(cmd, input=body, text=True, capture_output=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        print(f"export_doc: pandoc не запустился: {e}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"export_doc: pandoc вернул ошибку:\n{proc.stderr.strip()[:800]}", file=sys.stderr)
        if a.format == "pdf":
            print("Для pdf нужен движок вёрстки: `brew install --cask basictex` либо "
                  "экспортируйте в docx и печатайте из Word.", file=sys.stderr)
        return 1

    size = os.path.getsize(out) / 1024
    print(f"✅ {out}  ({size:.0f} КБ, из {a.source})")
    for w in warnings:
        print(f"⚠️  {w}")
    print("Экспорт — производная копия: правки вносите в markdown, затем экспортируйте заново.")
    print("Передали заказчику → зафиксируйте версию командой `ship:release`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
