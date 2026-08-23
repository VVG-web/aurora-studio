#!/usr/bin/env python3
"""ship_doc.py — поставка документа: экспорт в офисный формат и фиксация версии.

Два шага одной работы, и до 1.44.0 они жили в двух скриптах с двумя разборами
frontmatter (`export_doc.py`, `release_doc.py`):

  --export docx|pdf   заказчик принимает не markdown: конвертация pandoc'ом, wiki-ссылки
                      разрезолвливаются в текст, фирменный шаблон через `--reference`
  --release           инвариант 6, сданное неизменяемо: снапшот в `Deliverables/released/`,
                      дата передачи в рабочей копии, коммит базы на момент передачи

  python3 .opencode/scripts/ship_doc.py Deliverables/work/ОПЗ_v1.md --export docx
  python3 .opencode/scripts/ship_doc.py Deliverables/work/ОПЗ_v2.1.md --release --apply

Экспорт — производная копия: истина остаётся в markdown, в git. Перезаписать снапшот
нельзя: одна версия — один файл. Изменился документ — это новая версия, а не правка
сданного.

Панель: `ship:export` (флаги --export docx) · `ship:release` (флаги --release)
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import date

from aurora_common import (KB_ROOT, LINK_RE, TRUSTED, as_list, body as md_body,
                           clean_copy,
                           frontmatter, set_field, split_frontmatter, walk_md)

WORK = "Deliverables/work"
RELEASED = "Deliverables/released"
TODAY = date.today().isoformat()


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=30)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def card_statuses() -> dict:
    return {os.path.basename(p)[:-3]: (frontmatter(open(p, encoding="utf-8", errors="ignore").read())
                                       .get("status") or "").strip()
            for p in walk_md(KB_ROOT, skip_service=True)}




def flatten_links(text: str) -> str:
    """[[Карточка|Показ]] → «Показ»; [[Карточка]] → «Карточка».

    Снаружи базы wiki-ссылка не кликается и читается как мусор — в поставке от неё
    остаётся только текст.
    """
    return LINK_RE.sub(lambda m: (m.group(4) or m.group(2)).replace("-", " "), text)


def export(source: str, fmt: str, reference: str, out: str, keep_links: bool) -> int:
    """Документ → docx/pdf/html/odt через pandoc."""
    if not shutil.which("pandoc"):
        print("ship_doc: нужен pandoc — `brew install pandoc` (или apt/choco)", file=sys.stderr)
        return 1
    raw = open(source, encoding="utf-8").read()
    fm = frontmatter(raw)
    # Выгружаемый документ — тоже чистовик: разделы производства в docx заказчику не идут.
    text = clean_copy(md_body(raw))
    if not keep_links:
        text = flatten_links(text)

    warnings = []
    if not (fm.get("based_on") or "").strip("[] "):
        warnings.append("нет `based_on` — непонятно, из каких карточек собран документ "
                        "(assemble обязан его заполнять)")
    if fm.get("type") != "deliverable" and "/Deliverables/" in os.path.abspath(source):
        warnings.append("frontmatter без `type: deliverable`")

    target = out or os.path.splitext(source)[0] + "." + fmt
    cmd = ["pandoc", "-f", "gfm", "-o", target, "--from", "gfm", "--standalone"]
    if reference and fmt == "docx":
        if not os.path.isfile(reference):
            print(f"ship_doc: нет эталона оформления {reference}", file=sys.stderr)
            return 1
        cmd += ["--reference-doc", reference]
    title = (fm.get("title") or "").strip('"') or os.path.basename(os.path.splitext(source)[0])
    cmd += ["--metadata", f"title={title}"]

    try:
        proc = subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        print(f"ship_doc: pandoc не запустился: {e}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"ship_doc: pandoc вернул ошибку:\n{proc.stderr.strip()[:800]}", file=sys.stderr)
        if fmt == "pdf":
            print("Для pdf нужен движок вёрстки: `brew install --cask basictex` либо "
                  "экспортируйте в docx и печатайте из Word.", file=sys.stderr)
        return 1

    print(f"✅ {target}  ({os.path.getsize(target) / 1024:.0f} КБ, из {source})")
    for w in warnings:
        print(f"⚠️  {w}")
    print("Экспорт — производная копия: правки вносите в markdown, затем экспортируйте заново.")
    print("Передали заказчику → зафиксируйте версию: `ship:release`.")
    return 0

def main() -> int:
    ap = argparse.ArgumentParser(description="Поставка документа: экспорт и фиксация версии")
    ap.add_argument("document", help="файл из Deliverables/work/")
    ap.add_argument("--export", metavar="FORMAT", choices=["docx", "pdf", "html", "odt"],
                    help="конвертировать в офисный формат (pandoc)")
    ap.add_argument("--release", action="store_true",
                    help="заморозить переданную версию (снапшот в Deliverables/released/)")
    ap.add_argument("--reference", help="эталон оформления .docx (pandoc --reference-doc)")
    ap.add_argument("--out", help="куда положить экспорт (по умолчанию рядом с исходником)")
    ap.add_argument("--keep-links", action="store_true",
                    help="не разрезолвливать wiki-ссылки при экспорте")
    ap.add_argument("--version", help="версия (по умолчанию из frontmatter или из имени файла)")
    ap.add_argument("--date", default=TODAY, help="дата передачи (по умолчанию сегодня)")
    ap.add_argument("--binary", help="переданный бинарник (docx/pdf) — копируется рядом")
    ap.add_argument("--apply", action="store_true",
                    help="записать снапшот (иначе только что будет заморожено)")
    a = ap.parse_args()

    if not os.path.isfile(a.document):
        print(f"ship_doc: нет файла {a.document}", file=sys.stderr)
        return 1
    if a.export:
        return export(a.document, a.export, a.reference or "", a.out or "", a.keep_links)
    if not os.path.abspath(a.document).startswith(os.path.abspath(WORK)):
        print(f"ship_doc: замораживать можно только документы из {WORK}/", file=sys.stderr)
        return 1

    text = open(a.document, encoding="utf-8").read()
    fm = frontmatter(text)
    stem = os.path.basename(a.document)[:-3]
    version = a.version or fm.get("version") or ""
    if not version:
        m = re.search(r"_v([0-9][\w.\-]*)$", stem)
        version = m.group(1) if m else "1.0"
    doc_name = fm.get("doc") or re.sub(r"_v[0-9][\w.\-]*$", "", stem)
    target = os.path.join(RELEASED, f"{doc_name}_v{version}_{a.date}.md")

    if os.path.exists(target):
        print(f"ship_doc: снапшот уже существует — {target}\n"
              "Сданное неизменяемо (инвариант 6): выпустите новую версию, а не правьте эту.",
              file=sys.stderr)
        return 1

    base = as_list(fm.get("based_on", ""))
    statuses = card_statuses()
    weak = [(b, statuses.get(b, "нет в базе")) for b in base if statuses.get(b) not in TRUSTED]

    print(f"# Release — {a.date}\n")
    print(f"{a.document}\n  → {target}")
    print(f"Документ: {doc_name} · версия {version} · коммит базы {git_commit() or '—'}")
    print(f"Оснований в `based_on`: {len(base)}")
    if not base:
        print("⚠️ `based_on` пуст — документ непрослеживаем: неизвестно, из какого знания собран.")
    if weak:
        print(f"⚠️ Ниже verified: {len(weak)} — {', '.join(f'{b} ({s})' for b, s in weak[:6])}")
        print("   Сдаём документ, собранный на непроверенном знании. Риск приёмки.")
    if a.binary:
        if not os.path.isfile(a.binary):
            print(f"ship_doc: нет бинарника {a.binary}", file=sys.stderr)
            return 1
        print(f"Бинарник: {a.binary} → {RELEASED}/")

    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0

    head, rest = split_frontmatter(text)
    commit = git_commit()
    if head is None:
        snapshot = (f"---\ndoc: {doc_name}\nversion: \"{version}\"\ntype: deliverable\n"
                    f"released: {a.date}\nreleased_commit: {commit}\n---\n\n" + text)
        work_text = text
    else:
        snap_head = set_field(set_field(head, "released", a.date), "released_commit", commit)
        snapshot = "---" + snap_head + rest
        work_text = "---" + set_field(head, "released", a.date) + rest

    os.makedirs(RELEASED, exist_ok=True)
    open(target, "w", encoding="utf-8").write(snapshot)
    open(a.document, "w", encoding="utf-8").write(work_text)
    if a.binary:
        shutil.copy2(a.binary, os.path.join(RELEASED, os.path.basename(a.binary)))

    print(f"\n✅ Снапшот: {target} (неизменяем)")
    print("   В рабочей копии проставлена дата передачи.")
    print("   Дальше: обновить `pmi` в покрытых REQ и прогнать `ops:trace`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
