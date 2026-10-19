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
import html
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


def _same_file(a: str, b: str) -> bool:
    """Один и тот же файл (на macOS и Windows `Deliverables` и `deliverables` — одна папка, а `abspath` их различает)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.abspath(a) == os.path.abspath(b)


def _pandoc_sandbox(pandoc: str) -> bool:
    """Умеет ли этот pandoc `--sandbox` (с 2.15). Не смогли спросить — считаем, что умеет: лучше отказ, чем экспорт без защиты."""
    try:
        cp = subprocess.run([pandoc, "--version"], capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=30)
        m = re.match(r"pandoc(?:\.exe)?\s+(\d+)\.(\d+)", (cp.stdout or "").strip())
        return not m or (int(m.group(1)), int(m.group(2))) >= (2, 15)
    except Exception:                                       # noqa: BLE001
        return True


IMAGE_RE = re.compile(r"!\[[^\]\n]{0,300}\]\([^)\n]{1,500}\)|<img\b", re.I)
_IMG_MD = re.compile(r"!\[([^\]\n]{0,300})\]\([^)\n]{1,500}\)")
_IMG_TAG = re.compile(r"<img\b[^>\n]{0,500}>?", re.I)
_BLOCKED = "#blocked"
_SCHEME = re.compile(r"(?:javascript|vbscript|data|file):")
_SAFE_DATA = re.compile(r"data:image/(?:png|jpe?g|gif|webp)[;,]")        # встроенная картинка — не ссылка-сценарий
_INVISIBLE = re.compile(r"[\x00-\x20\u00a0\u200b-\u200d\u2060\ufeff]")
_TARGET = re.compile(r"\]\([ \t]*(?:\n[ \t]*)?<?([^)\n]{0,80})")
_REF_TARGET = re.compile(r"(?m)^( {0,3}\[[^\]\n]{1,200}\]:\s{0,100}<?)([^\s>]{0,80})")
_AUTOLINK_OPEN = re.compile(r"(?i)<(?=(?:javascript|data|vbscript|file):)")
_ATTR = re.compile(r"""(?i)\b(href|src|poster|action|formaction|xlink:href|data)[ \t\r\n]*=[ \t\r\n]*(?:"([^"]{0,4000})"|'([^']{0,4000})')""")


def _dangerous(raw: str) -> bool:
    """Адрес со схемой-сценарием; сущности (`&Tab;`, `&#106;`) и невидимые знаки в схеме браузер выбрасывает или читает."""
    t = re.sub(r"\\(?=[!-/:-@\[-`{-~])", "", html.unescape(raw or ""))      # `javascript\:` — обратный слэш перед знаком не виден
    t = _INVISIBLE.sub("", t).casefold()
    return bool(_SCHEME.match(t)) and not _SAFE_DATA.match(t)


def neutralize_links(text: str) -> str:
    """Адреса со схемой `javascript:`, `data:`, `vbscript:`, `file:` в ссылках markdown заменяются на `#blocked`.

    Текст приходит из базы (Confluence, модель), а экспорт в html открывает заказчик: pandoc переносит адрес как есть,
    и `--sandbox` этому не мешает (он запрещает pandoc читать файлы и сеть, а не писать опасный адрес в вывод).
    Смотрится только начало адреса после `](`, подпись не ищется: разбор линеен на любом вводе и не зависит от
    вложенных скобок в подписи. Последняя защита — `scrub_html` по готовому html: она видит то, что вышло из pandoc."""
    def target(m):
        return m.group(0)[:m.start(1) - m.start(0)] + _BLOCKED if _dangerous(m.group(1)) else m.group(0)

    def ref(m):
        return m.group(1) + _BLOCKED if _dangerous(m.group(2)) else m.group(0)
    return _AUTOLINK_OPEN.sub("&lt;", _REF_TARGET.sub(ref, _TARGET.sub(target, text)))


def scrub_html(page: str) -> str:
    """Готовый html: `href`, `src` и подобные с адресом-сценарием → `#blocked`. Смотрит на вывод pandoc, а не на вход."""
    def fix(m):
        raw = m.group(2) if m.group(2) is not None else m.group(3)
        return f'{m.group(1)}="{_BLOCKED}"' if _dangerous(raw) else m.group(0)
    return _ATTR.sub(fix, page)


_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)$")
# Теги без атрибутов, которые ничего не исполняют (переносы строк и подстрочные знаки в таблицах Confluence), остаются.
_PLAIN_TAGS = "br|hr|sub|sup|kbd|b|i|u|s|em|strong|code|mark|details|summary"
_TAG = re.compile(r"<(?=[A-Za-z/!?])(?![A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*>)(?![^\s<>@]+@[^\s<>]+>)"
                  rf"(?!/?(?:{_PLAIN_TAGS})[ \t]*/?>)", re.I)
_SPAN = re.compile(r"(`{1,16})(?!`)(.{1,500}?)(?<!`)\1(?!`)")


def escape_raw_html(text: str) -> str:
    """Начало тега (`<script`, `</a`, `<!--`) вне кода → `&lt;`: html-экспорт не получает чужой сырой HTML.

    `--sandbox` pandoc не запрещает ему попасть в вывод, а расширение `raw_html` блоки HTML не отключает. Блоки и
    строки кода не трогаются (ограждение — по правилам CommonMark: закрывает такое же и не короче), автоссылки
    `<https://…>`, `<имя@хост>` и безобидные теги без атрибутов (`<br>`, `<sub>`, `<kbd>`) остаются. Тег внутри многострочного кода-в-строке экранируется лишним: это только
    вид, не безопасность."""
    out, fence = [], None
    for line in text.split("\n"):
        if fence:
            m = re.fullmatch(r" {0,3}([`~]{3,})[ \t]*", line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            out.append(line)
            continue
        m = _FENCE.fullmatch(line)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = m.group(1)
            out.append(line)
            continue
        if len(line) > 2000:            # длинную строку не разбирают на код-в-строке: теги в ней экранируются все
            out.append(_TAG.sub("&lt;", line))
            continue
        parts, last = [], 0
        for span in _SPAN.finditer(line):
            parts.append(_TAG.sub("&lt;", line[last:span.start()]))
            parts.append(span.group(0))
            last = span.end()
        parts.append(_TAG.sub("&lt;", line[last:]))
        out.append("".join(parts))
    return "\n".join(out)


def strip_images(text: str) -> str:
    """Картинки → подпись: у pandoc без `--sandbox` адрес картинки из текста — это чтение файла или обращение по сети."""
    return _IMG_TAG.sub("", _IMG_MD.sub(lambda m: m.group(1), text))


def export(source: str, fmt: str, reference: str, out: str, keep_links: bool, allow=(),
           embed_images: bool = False) -> int:
    """Документ → docx/pdf/html/odt через pandoc."""
    # Не только PATH: на Windows winget кладёт pandoc в %LOCALAPPDATA%\Pandoc, а PATH
    # процесса, запущенного до установки, о нём не знает.
    pandoc = find_bin("pandoc")
    if not pandoc:
        print("ship_doc: нужен pandoc — macOS: `brew install pandoc`, Windows: "
              "`winget install --id JohnMacFarlane.Pandoc -e`, Linux: `sudo apt install pandoc`",
              file=sys.stderr)
        return 1
    raw = open(source, encoding="utf-8-sig").read()      # BOM: без него шапка не читалась и draft не был виден
    fm = frontmatter(raw)
    stuck = gate(raw, set(allow), True, "экспорт")
    if stuck:
        return stuck
    # Выгружаемый документ — тоже чистовик: разделы производства в docx заказчику не идут.
    text = clean_copy(md_body(raw))
    if not keep_links:
        text = flatten_links(text)
    text = neutralize_links(text)
    if fmt == "html":       # в docx/odt/pdf сырой HTML pandoc отбрасывает, в html-экспорт он попал бы как есть
        text = escape_raw_html(text)

    warnings = []
    if not (fm.get("based_on") or "").strip("[] "):
        warnings.append("нет `based_on` — непонятно, из каких карточек собран документ "
                        "(assemble обязан его заполнять)")
    if fm.get("type") != "deliverable" and "/Deliverables/" in os.path.abspath(source):
        warnings.append("frontmatter без `type: deliverable`")

    target = out or os.path.splitext(source)[0] + "." + fmt
    # --sandbox: текст приходит из базы (Confluence, модель), и pandoc не должен по нему читать файлы и ходить
    # по адресам из картинок — иначе чужая страница читает диск и сеть пользователя через экспорт. Цена —
    # картинки в документ не встраиваются (pandoc молча пишет warning): об этом сказано в выводе, а документу,
    # которому доверяют, картинки возвращает `--embed-images`.
    sandbox = not embed_images and _pandoc_sandbox(pandoc)
    cmd = [pandoc, "-f", "gfm", "-o", target, "--from", "gfm", "--standalone"] + (["--sandbox"] if sandbox else [])
    if not embed_images and not sandbox:
        text = strip_images(text)
        warnings.append("pandoc старше 2.15 не знает --sandbox: картинки заменены подписью, чтобы текст не читал файлы и "
                        "адреса, — обновите pandoc")
    images = len(IMAGE_RE.findall(text))
    if images and sandbox:
        warnings.append(f"в документе картинок: {images} — в экспорт они не встроены (песочница pandoc); "
                        "документу, которому доверяете, — `--embed-images`")
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

    if fmt == "html":           # что бы ни вышло из pandoc, опасного адреса в готовом html не остаётся
        try:
            with open(target, encoding="utf-8") as f:
                page = f.read()
            clean = scrub_html(page)
            if clean != page:
                with open(target, "w", encoding="utf-8", newline="") as f:
                    f.write(clean)
        except (OSError, ValueError):
            pass
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
    ap.add_argument("--embed-images", action="store_true",
                    help="--export: встроить картинки в документ (pandoc без песочницы; только для документов, которым доверяете)")
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
        return export(a.document, a.export, a.reference or "", a.out or "", a.keep_links, a.allow, a.embed_images)
    if not path_inside(a.document, WORK):
        print(f"ship_doc: замораживать можно только документы из {WORK}/", file=sys.stderr)
        return 1

    text = open(a.document, encoding="utf-8-sig").read()
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
        kept = os.path.join(RELEASED, os.path.basename(a.binary))
        if os.path.exists(kept) and not _same_file(kept, a.binary):
            with open(kept, "rb") as f1, open(a.binary, "rb") as f2:
                same = hashlib.sha256(f1.read()).digest() == hashlib.sha256(f2.read()).digest()
            if not same:
                print(f"ship_doc: {kept} уже сдан с другим содержимым. Сданное неизменяемо: дайте новой версии "
                      "файла другое имя.", file=sys.stderr)
                return 1

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
    if a.binary and not _same_file(a.binary, os.path.join(RELEASED, os.path.basename(a.binary))):
        shutil.copy2(a.binary, os.path.join(RELEASED, os.path.basename(a.binary)))

    print(f"\n✅ Снапшот: {target} (неизменяем, sha256 {release_hash(snapshot)[:12]}…)")
    print("   В рабочей копии проставлена дата передачи.")
    print("   Дальше: обновить `pmi` в покрытых REQ и прогнать `ops:trace`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
