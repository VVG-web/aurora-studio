#!/usr/bin/env python3
"""ship_doc.py — поставка документа: экспорт в офисный формат и фиксация версии.

Два шага одной работы, и до 1.44.0 они жили в двух скриптах с двумя разборами
frontmatter (`export_doc.py`, `release_doc.py`):

  --export docx|pdf   заказчик принимает не markdown: конвертация pandoc'ом, wiki-ссылки
                      разрезолвливаются в текст, фирменный шаблон через `--reference`
  --release           инвариант 6, сданное неизменяемо: снапшот в `Deliverables/released/`,
                      дата передачи в рабочей копии, коммит базы на момент передачи

  python3 .aurora/scripts/ship_doc.py Deliverables/work/ОПЗ_v1.md --export docx
  python3 .aurora/scripts/ship_doc.py Deliverables/work/ОПЗ_v2.1.md --release --apply

Экспорт — производная копия: истина остаётся в markdown, в git. Перезаписать снапшот
нельзя: одна версия — один файл. Изменился документ — это новая версия, а не правка
сданного.

Панель: `ship:export` (флаги --export docx) · `ship:release` (флаги --release)
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys

from aurora_common import (GATE_CODES, KB_ROOT, LINK_RE, TRUSTED, as_list, body as md_body,
                           clean_copy, find_bin, frontmatter, git_commit, git_dirty, outward_check,
                           path_inside, release_hash, released_problems, safe_part,
                           set_field, split_frontmatter, walk_md)

WORK = "Deliverables/work"
RELEASED = "Deliverables/released"
from aurora_common import TODAY  # noqa: E402 — дата в UTC, одна на движок


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


def _tracked(path: str) -> bool:
    """Файл под версионным контролем. Самого git нет — проверять нечего, считаем «да»."""
    try:
        return subprocess.run(["git", "ls-files", "--error-unmatch", "--", path], capture_output=True,
                              timeout=30).returncode == 0
    except Exception:                                       # noqa: BLE001
        return True


def gate(text: str, allow, apply: bool, what: str) -> int:
    """Единый предохранитель выходной команды (`aurora_common.outward_check`): 0 — можно, 2 — отказ."""
    return outward_check(text, allow, apply, what, "ship_doc")


def export(source: str, fmt: str, reference: str, out: str, keep_links: bool, allow=()) -> int:
    """Документ → docx/pdf/html/odt через pandoc."""
    # Не только PATH: на Windows winget кладёт pandoc в %LOCALAPPDATA%\Pandoc, а PATH
    # процесса, запущенного до установки, о нём не знает.
    pandoc = find_bin("pandoc")
    if not pandoc:
        print("ship_doc: нужен pandoc — macOS: `brew install pandoc`, Windows: "
              "`winget install --id JohnMacFarlane.Pandoc -e`, Linux: `sudo apt install pandoc`",
              file=sys.stderr)
        return 1
    raw = open(source, encoding="utf-8").read()
    fm = frontmatter(raw)
    stuck = gate(raw, set(allow), True, "экспорт")
    if stuck:
        return stuck
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
    # --sandbox: текст приходит из базы (Confluence, модель), и pandoc не должен по нему читать файлы и ходить
    # по адресам из картинок — иначе чужая страница читает диск и сеть пользователя через экспорт.
    cmd = [pandoc, "-f", "gfm", "-o", target, "--from", "gfm", "--standalone", "--sandbox"]
    if reference and fmt == "docx":
        if not os.path.isfile(reference):
            print(f"ship_doc: нет эталона оформления {reference}", file=sys.stderr)
            return 1
        cmd += ["--reference-doc", reference]
    title = (fm.get("title") or "").strip('"') or os.path.basename(os.path.splitext(source)[0])
    cmd += ["--metadata", f"title={title}"]

    try:
        proc = subprocess.run(cmd, input=text, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=300)
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
    ap.add_argument("document", nargs="?", help="файл из Deliverables/work/")
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
    ap.add_argument("--verify", action="store_true",
                    help="проверить сданные версии в Deliverables/released/: хеш, правки и удаления после коммита")
    ap.add_argument("--allow", action="append", default=[], metavar="КОД", choices=sorted(GATE_CODES),
                    help="снять блокировку предохранителя выходных команд (КОД назван в его сообщении)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="--release: документ не закоммичен — всё равно заморозить (в снимке пометка released_dirty)")
    a = ap.parse_args()

    if a.verify:
        bad = released_problems(".")
        print("# Сданные версии\n")
        print("\n".join(f"⛔ {p}" for p in bad) if bad else "✅ все сданные версии на месте и не менялись")
        return 1 if bad else 0
    if not a.document:
        ap.error("нужен документ (или --verify)")
    if not os.path.isfile(a.document):
        print(f"ship_doc: нет файла {a.document}", file=sys.stderr)
        return 1
    if a.export:
        return export(a.document, a.export, a.reference or "", a.out or "", a.keep_links, a.allow)
    if not path_inside(a.document, WORK):
        print(f"ship_doc: замораживать можно только документы из {WORK}/", file=sys.stderr)
        return 1

    text = open(a.document, encoding="utf-8").read()
    fm = frontmatter(text)
    stem = os.path.basename(a.document)[:-3]
    # doc и version приходят из шапки документа (а шапку пишет и модель), и по ним строится имя файла:
    # разделители пути и «..» в них вывели бы снимок за пределы Deliverables/released/.
    version = safe_part(a.version or fm.get("version") or "")
    if not version:
        m = re.search(r"_v([0-9][\w.\-]*)$", stem)
        version = safe_part(m.group(1) if m else "1.0", "1.0")
    doc_name = safe_part(fm.get("doc") or re.sub(r"_v[0-9][\w.\-]*$", "", stem), "document")
    date = safe_part(a.date, TODAY)
    target = os.path.join(RELEASED, f"{doc_name}_v{version}_{date}.md")

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

    # Предохранитель: то, что фиксируем и отдаём, проверяется так же, как публикация и экспорт.
    stuck = gate(text, set(a.allow), a.apply, "фиксация")
    if stuck:
        return stuck
    # `released_commit` обещает: «эта версия была в этом коммите». Документ, которого в коммите нет (правки не
    # закоммичены или файл не под git), даёт ложное обещание — отказ, либо явный `--allow-dirty` с пометкой.
    commit = git_commit()
    dirty = ""
    if commit:
        if git_dirty(a.document):
            dirty = "в документе есть незакоммиченные правки"
        elif not _tracked(a.document):
            dirty = "документ не добавлен в git"
    if dirty:
        print(f"⚠️ {dirty}: коммит {commit} не содержит того, что сдаётся")
        if a.apply and not a.allow_dirty:
            print("ship_doc: закоммитьте документ и повторите (или --allow-dirty: в снимке будет "
                  "пометка released_dirty)", file=sys.stderr)
            return 2

    if not a.apply:
        print("\n(dry-run) Ничего не записано. Повторите с --apply.")
        return 0

    # Снимок — ТО, ЧТО ПЕРЕДАЛИ: чистовик без разделов производства (его же берёт экспорт и публикация),
    # а не полный текст рабочей копии. Рабочая копия остаётся целой. Хеш в шапке — чем доказать, что сданное
    # не менялось (`ship:release --verify`, `kit:doctor`).
    sent = clean_copy(text)
    head, rest = split_frontmatter(sent)
    fields = {"released": a.date, "released_commit": commit}
    if dirty:
        fields["released_dirty"] = "true"
    if a.binary:
        with open(a.binary, "rb") as f:
            fields["binary"] = os.path.basename(a.binary)
            fields["binary_sha256"] = hashlib.sha256(f.read()).hexdigest()
    if head is None:
        base_head = f"\ndoc: {doc_name}\nversion: \"{version}\"\ntype: deliverable"
        for k, v in fields.items():
            base_head = set_field(base_head, k, v)
        shell = "---" + base_head + "\n---\n\n"
        snapshot = shell + sent
        snap_head = base_head
    else:
        snap_head = head
        for k, v in fields.items():
            snap_head = set_field(snap_head, k, v)
        snapshot = "---" + snap_head + rest
    snapshot = "---" + set_field(snap_head, "sha256", release_hash(snapshot)) + snapshot[len("---" + snap_head):]
    work_head = split_frontmatter(text)[0]
    work_text = text if work_head is None else \
        "---" + set_field(work_head, "released", a.date) + split_frontmatter(text)[1]

    os.makedirs(RELEASED, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(snapshot)
    open(a.document, "w", encoding="utf-8").write(work_text)
    if a.binary:
        shutil.copy2(a.binary, os.path.join(RELEASED, os.path.basename(a.binary)))

    print(f"\n✅ Снапшот: {target} (неизменяем, sha256 {release_hash(snapshot)[:12]}…)")
    print("   В рабочей копии проставлена дата передачи.")
    print("   Дальше: обновить `pmi` в покрытых REQ и прогнать `ops:trace`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
