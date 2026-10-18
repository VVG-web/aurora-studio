"""Проверки движка Aurora, часть 47. Каркас и помощники — tests/harness.py.

Шаблоны карточек базы знаний, фаза 3 «Обязательность» (это часть A фазы: план называет её `part_<A>`).

Пакет CT-09 (начало части): единый конструктор шапки и вход записи карточки — `card_types.render_header`,
`render_card`, `write_card_text`, `TemplateViolation`, `UnknownCardType` — и храповик мест записи
`tests/card_write_sites.py` (список допускаемых мест — `tests/card_write_sites.json`). Создание карточки строгое,
правка существующей идёт «по разности»: прежние нарушения терпятся, новое отвергается; запись атомарная.
Модель и сеть не используются; проекты — только во временных папках, настоящий HOME тестов не касается.
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib
import io
import os
import re
import stat
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, test  # noqa: F401

from cases import part_42 as P42  # noqa: E402 — фикстурные шаблоны и пары «нарушение / исправно» первой части


def _C():
    sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("card_types")


def _sites():
    tests_dir = str(KIT / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    return importlib.import_module("card_write_sites")


def _project(tmp: Path) -> Path:
    """Проект с тринадцатью фикстурными шаблонами, доставленными в `TemplatesCommon/`; кита рядом нет."""
    return P42._project(tmp)


def _reg(root: Path):
    return P42._reg(root)


CARD = "AuroraKnowledgeDB/Concepts/Заявка на поставку.md"


def _path(root: Path, rel: str = CARD) -> str:
    return str(root / rel)


def _read(p) -> str:
    return Path(p).read_bytes().decode("utf-8")


def _leftovers(folder: Path) -> list:
    return [p.name for p in folder.iterdir() if p.name.endswith(".aurora-tmp")]


# ------------------------------------------------------------------ CT-09: конструктор шапки

FULL = {"title": "Заявка на поставку", "aliases": ["Заявка", "Поставка"], "tags": [], "kind": "knowledge",
        "status": "draft", "sources": ["Sources/Confluence/Процессы/Заявка.md", "Raw/contract/ТЗ.md"],
        "source_synced": "2026-10-01", "created": "2026-07-12", "updated": "2026-10-01", "built": "machine",
        "built_by": "model-a", "related": [], "summary": "Документ для поставщика."}


@test
def test_render_header_roundtrip_all_types(tmp: Path):
    """AC-CT-09.1: `render_header` → `frontmatter()` даёт ожидаемый словарь для каждого типа каталога."""
    C = _C()
    from aurora_common import card_sources, frontmatter, list_field, split_frontmatter
    reg = _reg(_project(tmp))
    for row in P42.CATALOGUE:
        tid = row[0]
        extra = [x.strip() for x in row[8].split(",")
                 if x.strip() and x.strip() not in C.HEADER_ORDER and x.strip() not in C.LIST_FIELDS]
        fields = dict(FULL, title=f"Пример {tid}")
        if extra:
            fields[extra[-1]] = "значение поля типа"        # поле типа встаёт сразу после status
        text = C.render_header(tid, fields, reg=reg)
        head, rest = split_frontmatter(text)
        assert head is not None and rest.strip() == "---", (tid, text)
        fm = frontmatter(text)
        assert fm["type"] == tid and fm["template"] == f"akdb_{tid}_v1.0.md", (tid, fm)
        assert fm["title"] == f"Пример {tid}" and fm["status"] == "draft" and fm["built"] == "machine", fm
        assert fm["built_by"] == "model-a" and fm["summary"] == "Документ для поставщика.", fm
        assert list_field(text, "aliases") == ["Заявка", "Поставка"], (tid, list_field(text, "aliases"))
        assert card_sources(text) == FULL["sources"], (tid, card_sources(text))
        if extra:
            assert fm[extra[-1]] == "значение поля типа", (tid, fm)
        # пустые необязательные поля опущены, а не записаны пустыми
        assert "tags" not in fm and "related" not in fm, (tid, fm)
        # канонический порядок ключей
        keys = [ln.split(":", 1)[0] for ln in head.splitlines() if re.match(r"^[A-Za-z_]\w*:", ln)]
        order = [k for k in C.HEADER_ORDER if k in keys]
        placed = [k for k in keys if k in C.HEADER_ORDER]
        assert placed == order, (tid, keys)
        if extra:
            assert keys.index(extra[-1]) == keys.index("status") + 1, (tid, keys)


@test
def test_render_header_cleans_values_and_keys(tmp: Path):
    """Значения — одной строкой (перевод строки не рождает чужое поле шапки), имя поля — слово, шаблон назван верно."""
    C = _C()
    from aurora_common import frontmatter
    reg = _reg(_project(tmp))
    evil = C.render_header("concept", {"title": 'Имя "в кавычках"\ntrusted: true', "status": "draft",
                                        "built_by": "модель\u2028verified: yes"}, reg=reg)
    fm = frontmatter(evil)
    assert "trusted" not in fm and "verified" not in fm, fm
    assert evil.count("\n") == len(evil.splitlines()), repr(evil)
    assert '"' not in fm["title"], "кавычка внутри названия рвёт шапку"
    for bad in ("a b", "ключ", "x:y", "x\ny"):
        try:
            C.render_header("concept", {bad: "1"}, reg=reg)
        except ValueError:
            pass
        else:
            raise AssertionError(f"имя поля «{bad!r}» принято")
    # шаблон: названный того же типа принимается, чужой заменяется
    old = C.render_header("concept", {"template": "akdb_concept_v1.0.md"}, reg=reg)
    other = C.render_header("concept", {"template": "akdb_glossary_v1.0.md"}, reg=reg)
    assert "template: akdb_concept_v1.0.md" in old and "template: akdb_concept_v1.0.md" in other, other
    # type из полей не побеждает аргумент
    assert "type: concept" in C.render_header("concept", {"type": "glossary"}, reg=reg)


@test
def test_render_header_unknown_or_disabled_type(tmp: Path):
    """Тип вне реестра и выключенный — `UnknownCardType`: «похожий» тип не подбирается."""
    C = _C()
    reg = _reg(_project(tmp))
    for tid in ("person", "entity", "Concept", ""):
        try:
            C.render_header(tid, {"title": "x"}, reg=reg)
        except C.UnknownCardType:
            pass
        else:
            raise AssertionError(f"тип «{tid}» принят")
    off = _reg(_project(tmp / "off"))
    off.types["concept"] = dataclasses.replace(off.types["concept"], enabled=False)
    try:
        C.render_header("concept", {"title": "x"}, reg=off)
    except C.UnknownCardType as e:
        assert "выключен" in str(e), str(e)
    else:
        raise AssertionError("выключенный тип принят")
    assert issubclass(C.UnknownCardType, ValueError) and issubclass(C.TemplateViolation, ValueError), \
        "исключения входа записи — ValueError"


@test
def test_render_header_without_registry_is_legacy_header(tmp: Path):
    """Реестра нет (шаблоны не доставлены): шапка без `template:`, тип — по прежней таблице; чужой тип отвергается."""
    C = _C()
    from aurora_common import frontmatter
    empty = C.Registry()
    fm = frontmatter(C.render_header("concept", {"title": "x", "status": "draft"}, reg=empty))
    assert fm["type"] == "concept" and "template" not in fm, fm
    try:
        C.render_header("person", {"title": "x"}, reg=empty)
    except C.UnknownCardType:
        pass
    else:
        raise AssertionError("тип вне схемы принят при пустом реестре")


@test
def test_render_card_gap_and_pieces(tmp: Path):
    """`render_card` = шапка + `gap` пустых строк + собственная часть + хвост, без правок текста."""
    C = _C()
    reg = _reg(_project(tmp))
    head = C.render_header("concept", {"title": "x", "status": "draft"}, reg=reg)
    for gap in (0, 1, 2):
        text = C.render_card("concept", {"title": "x", "status": "draft"}, "Определение.\n", "\n## История изменений\n",
                             reg=reg, gap=gap)
        assert text == head + "\n" * gap + "Определение.\n\n## История изменений\n", repr(text)
    plain = C.render_header("concept", {"title": "x"}, reg=reg)
    assert C.render_card("concept", {"title": "x"}, "T\n", reg=reg) == plain + "\nT\n", "по умолчанию — одна пустая строка"


# ------------------------------------------------------------------ CT-09: вход записи

# Случаи по кодам-ошибкам: нарушение (мутация исправной карточки `P42.mk()`) и пояснение. Для каждого кода вход обязан:
# отвергнуть создание, терпеть унаследованное и отвергнуть новое.
_BAD = {
    "V-01": "Нет шапки, просто текст\n",
    "V-02": P42.mk(title=None),
    "V-03": P42.mk(type="person"),
    "V-04": P42.mk(template="akdb_nope_v9.9.md"),
    "V-05": P42.mk(kind="essay"),
    "V-06": P42.mk(status="imported"),
    "V-08": P42.mk(created="2026-13-45"),
    "V-09": P42.mk(body=P42.LEAD + "\n## Связи\n- [[Поставщик]] — получает заявку.\n"),
    "V-10": P42.mk(body=P42.LEAD + "\n## Правила\n- а\n\n## Свойства\n- б\n"),
    "V-12": P42.mk(body="- пункт вместо определения\n"),
    "V-13": P42.mk(body=P42.LEAD + "\n## Свойства\n- {{признак}} — {{значение}}\n"),
    "V-16": P42.mk(decisions='"[[Решение]]"'),
    "V-19": P42._meeting(False),
    "V-23": P42.mk(body="# Другое имя\n\n" + P42.LEAD),
    "V-25": P42.mk(status="index"),
}


def _codes_of(exc) -> list:
    return [v.code for v in exc.violations]


@test
def test_gate_create_strict_every_error_code(tmp: Path):
    """AC-CT-09.2: создание с нарушением отвергается по каждому коду-ошибке; файл не появляется."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    good = P42.mk()
    p = _path(root)
    assert C.write_card_text(p, good, op="create", reg=reg) is True and _read(p) == good
    os.unlink(p)
    for code, bad in _BAD.items():
        for op in C.WRITE_OPS_STRICT:
            try:
                C.write_card_text(p, bad, op=op, reg=reg)
            except C.TemplateViolation as e:
                assert code in _codes_of(e), (code, op, _codes_of(e))
                assert e.path == p and e.op == op, (e.path, e.op)
            else:
                raise AssertionError(f"{code}/{op}: нарушение принято")
            assert not os.path.exists(p), f"{code}/{op}: файл записан, хотя запись отвергнута"
    # V-22: карта без метки генерации
    moc = _path(root, "AuroraKnowledgeDB/MOC/Термины.md")
    try:
        C.write_card_text(moc, P42._moc(False), op="map", reg=reg)
    except C.TemplateViolation as e:
        assert "V-22" in _codes_of(e), _codes_of(e)
    else:
        raise AssertionError("карта без метки принята")
    assert C.write_card_text(moc, P42._moc(True), op="map", reg=reg) is True


@test
def test_gate_create_requires_template_stamp(tmp: Path):
    """Создание без `template:` отвергается (строка V-02 для режима записи strict), с ним — проходит."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = _path(root)
    try:
        C.write_card_text(p, P42.mk(template=None), op="create", reg=reg)
    except C.TemplateViolation as e:
        assert "V-02" in _codes_of(e) and "template" in str(e), str(e)
    else:
        raise AssertionError("создание без template: принято")
    assert not os.path.exists(p)
    assert C.write_card_text(p, P42.mk(), op="create", reg=reg) is True


@test
def test_gate_diff_tolerates_inherited_errors(tmp: Path):
    """AC-CT-09.2: правка карточки с прежним нарушением проходит, пока новых ошибок нет — по каждому коду."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = Path(_path(root))
    for code, bad in _BAD.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(bad.encode("utf-8"))
        edited = bad + "\nПримечание движка.\n"
        for op in C.WRITE_OPS_DIFF:
            try:
                C.write_card_text(str(p), edited, op=op, reg=reg)
            except C.TemplateViolation as e:
                raise AssertionError(f"{code}/{op}: унаследованное нарушение не стерпели: {e}")
            assert _read(p) == edited, (code, op)
            p.write_bytes(bad.encode("utf-8"))
        # исправление нарушения — тоже допустимая правка
        C.write_card_text(str(p), P42.mk(), op="repair", reg=reg)
        assert _read(p) == P42.mk(), code


@test
def test_gate_diff_rejects_new_errors(tmp: Path):
    """AC-CT-09.2: правка исправной карточки, дающая новое нарушение, отвергается по каждому коду; файл прежний."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = Path(_path(root))
    good = P42.mk()
    for code, bad in _BAD.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(good.encode("utf-8"))
        for op in C.WRITE_OPS_DIFF:
            try:
                C.write_card_text(str(p), bad, op=op, reg=reg)
            except C.TemplateViolation as e:
                assert code in _codes_of(e), (code, op, _codes_of(e))
            else:
                raise AssertionError(f"{code}/{op}: новое нарушение принято")
            assert _read(p) == good, f"{code}/{op}: файл изменён отвергнутой записью"


@test
def test_gate_diff_counts_repeats_and_keeps_the_stamp(tmp: Path):
    """По разности считаются и повторы одной ошибки; штамп `template:` с карточки не снимается."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = Path(_path(root))
    one = P42.mk(body=P42.LEAD + "\n## Связи\n- [[А]] — первая.\n")
    two = P42.mk(body=P42.LEAD + "\n## Связи\n- [[А]] — первая.\n\n## Связи\n- [[Б]] — вторая.\n")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(one.encode("utf-8"))
    try:
        C.write_card_text(str(p), two, op="body", reg=reg)
    except C.TemplateViolation as e:
        assert "V-09" in _codes_of(e), _codes_of(e)
    else:
        raise AssertionError("второй такой же лишний раздел принят")
    # другое число в той же ошибке — та же ошибка (абзац стал длиннее, но он и был слишком длинным)
    long_a = P42.mk(body="Слово " * 200 + "\n")
    long_b = P42.mk(body="Слово " * 260 + "\n")
    p.write_bytes(long_a.encode("utf-8"))
    C.write_card_text(str(p), long_b, op="body", reg=reg)
    # снять штамп нельзя
    p.write_bytes(P42.mk().encode("utf-8"))
    try:
        C.write_card_text(str(p), P42.mk(template=None), op="header", reg=reg)
    except C.TemplateViolation as e:
        assert "template" in str(e), str(e)
    else:
        raise AssertionError("штамп снят правкой")
    # у до-шаблонной карточки штамп не требуется
    p.write_bytes(P42.mk(template=None).encode("utf-8"))
    C.write_card_text(str(p), P42.mk(template=None, updated="2026-10-02"), op="header", reg=reg)


@test
def test_gate_map_marker_and_correction_branch(tmp: Path):
    """V-22 на картах: карта без метки генерации не создаётся и не получается правкой; унаследованная — терпится.
    Исправление в `Raw/corrections` пишет не вход, но его формат этим же валидатором проверяется (ветка исправлений)."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = Path(_path(root, "AuroraKnowledgeDB/MOC/Термины.md"))
    good, bad = P42._moc(True), P42._moc(False)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(good.encode("utf-8"))
    for op in C.WRITE_OPS_DIFF:
        try:
            C.write_card_text(str(p), bad, op=op, reg=reg)
        except C.TemplateViolation as e:
            assert "V-22" in _codes_of(e), _codes_of(e)
        else:
            raise AssertionError(f"{op}: метка генерации снята правкой")
    p.write_bytes(bad.encode("utf-8"))
    assert C.write_card_text(str(p), bad + "\nещё строка\n", op="repair", reg=reg) is True
    # исправление человека: валидатор знает его формат (corrects, created, status)
    fine = "---\ncorrects: \"[[Заявка]]\"\ncreated: 2026-10-01\nstatus: active\n---\n\n# Исправление: Заявка\n\nТекст.\n"
    found = C.validate_card(fine, path="Raw/corrections/2026-10-01-Заявка.md", reg=reg, level="strict")
    assert not [v for v in found if v.severity == "error"], found
    broken = fine.replace("status: active", "status: pending")
    codes = [v.code for v in C.validate_card(broken, path="Raw/corrections/x.md", reg=reg, level="strict")
             if v.severity == "error"]
    assert codes == ["V-06"], codes


@test
def test_gate_missing_file_for_diff_op_is_a_creation(tmp: Path):
    """Правка файла, которого нет, проверяется как создание: строго и со штампом."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = _path(root)
    try:
        C.write_card_text(p, P42.mk(template=None), op="body", reg=reg)
    except C.TemplateViolation:
        pass
    else:
        raise AssertionError("правка несуществующей карточки без штампа принята")
    assert C.write_card_text(p, P42.mk(), op="body", reg=reg) is True


@test
def test_gate_old_text_cannot_soften_the_check(tmp: Path):
    """Прежний текст — тот, что на диске: `old_text` не смягчает проверку (ни при живом файле, ни при несуществующем)."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = Path(_path(root))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(P42.mk().encode("utf-8"))
    bad = _BAD["V-12"]
    for op in C.WRITE_OPS_DIFF:
        try:
            C.write_card_text(str(p), bad, op=op, old_text=bad, reg=reg)
        except C.TemplateViolation:
            pass
        else:
            raise AssertionError(f"{op}: old_text=новому тексту отключил проверку")
    assert _read(p) == P42.mk()
    p.unlink()
    try:
        C.write_card_text(str(p), bad, op="body", old_text=bad, reg=reg)
    except C.TemplateViolation:
        pass
    else:
        raise AssertionError("old_text при несуществующем файле превратил создание в правку «по разности»")
    assert not p.exists()


@test
def test_gate_refuses_non_cards_links_and_bad_ops(tmp: Path):
    """Вход пишет только карточки базы: вне `AuroraKnowledgeDB/`, служебные файлы, ссылка наружу, чужая операция — отказ."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    text = P42.mk()
    for rel in ("Artifacts/us/Заявка.md", "Raw/decisions/DR-0001.md", "AuroraKnowledgeDB/index.md",
                "AuroraKnowledgeDB/Concepts/_index.md", "AuroraKnowledgeDB/meta/card_types.md",
                "AuroraKnowledgeDB/Concepts/README.md"):
        try:
            C.write_card_text(_path(root, rel), text, op="create", reg=reg)
        except ValueError as e:
            assert not isinstance(e, C.TemplateViolation), (rel, str(e))
        else:
            raise AssertionError(f"{rel}: запись принята")
        assert not (root / rel).exists(), rel
    try:
        C.write_card_text(_path(root), text, op="rewrite", reg=reg)
    except ValueError as e:
        assert "операция" in str(e), str(e)
    else:
        raise AssertionError("неизвестная операция принята")
    # ссылка за пределы проекта
    outside = tmp / "outside.md"
    outside.write_text("чужой файл\n", encoding="utf-8")
    link = root / "AuroraKnowledgeDB" / "Concepts" / "Ссылка.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        return                                     # символические ссылки недоступны (Windows без прав)
    try:
        C.write_card_text(str(link), text, op="body", reg=reg)
    except OSError as e:
        assert "ссылка" in str(e), str(e)
    else:
        raise AssertionError("запись через ссылку наружу принята")
    assert outside.read_text(encoding="utf-8") == "чужой файл\n", "файл за пределами проекта изменён"


@test
def test_gate_writes_lf_keeps_mode_and_skips_identical(tmp: Path):
    """Запись — LF на любой системе; права файла сохраняются; равный текст не перезаписывается; суррогат не роняет запись."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = Path(_path(root))
    text = P42.mk()
    assert C.write_card_text(str(p), text.replace("\n", "\r\n"), op="create", reg=reg) is True
    assert b"\r" not in p.read_bytes(), "CRLF попал на диск"
    assert C.write_card_text(str(p), text, op="body", reg=reg) is False, "равный текст переписан"
    if os.name != "nt":
        p.chmod(0o640)
        before = p.stat().st_mtime_ns
        C.write_card_text(str(p), text + "\nещё одна строка.\n", op="body", reg=reg)
        assert stat.S_IMODE(p.stat().st_mode) == 0o640, oct(p.stat().st_mode)
        assert p.stat().st_mtime_ns >= before
    odd = text.replace("Заявка на поставку — документ", "Заявка \udc80 — документ")
    C.write_card_text(str(p), odd, op="repair", reg=reg)
    assert p.read_bytes(), "файл пуст после записи с непарным суррогатом"
    assert not _leftovers(p.parent), _leftovers(p.parent)


@test
def test_gate_atomic_write(tmp: Path):
    """AC-CT-09.4: прерывание посреди записи не оставляет битый файл и временных остатков."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = Path(_path(root))
    old = P42.mk()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(old.encode("utf-8"))
    new = old + "\nДобавлено.\n"
    real_replace, real_encode = C.replace_file, C._encode
    try:
        # прерывание до подмены: временный файл уже записан
        def boom(*args, **kw):
            raise KeyboardInterrupt
        C.replace_file = boom
        try:
            C.write_card_text(str(p), new, op="body", reg=reg)
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("прерывание проглочено")
        assert _read(p) == old, "прежний файл изменён"
        assert not _leftovers(p.parent), _leftovers(p.parent)
        C.replace_file = real_replace
        # прерывание посреди кодирования и записи нового файла: файла нет вовсе
        C._encode = boom
        fresh = Path(_path(root, "AuroraKnowledgeDB/Concepts/Новая.md"))
        try:
            C.write_card_text(str(fresh), P42.mk(), op="create", reg=reg)
        except KeyboardInterrupt:
            pass
        assert not fresh.exists() and not _leftovers(p.parent), (fresh.exists(), _leftovers(p.parent))
    finally:
        C.replace_file, C._encode = real_replace, real_encode
    C.write_card_text(str(p), new, op="body", reg=reg)
    assert _read(p) == new


@test
def test_gate_without_templates_writes_as_before_and_warns_once(tmp: Path):
    """Шаблонов нет нигде: запись идёт как прежняя, с одним предупреждением на процесс. Пустым реестром проверку
    не отключить; сломанные шаблоны не равны «шаблонов нет»; выключателя нет ни флагом, ни окружением."""
    C = _C()
    import inspect
    root = tmp / "bare"
    (root / "AuroraKnowledgeDB" / "Concepts").mkdir(parents=True)
    p = str(root / "AuroraKnowledgeDB" / "Concepts" / "x.md")
    real_kit = C._find_kit
    C._find_kit = lambda: ("", "")                  # кита рядом нет
    try:
        err = io.StringIO()
        C._GATE_WARNED.discard(str(root).replace("\\", "/"))
        with contextlib.redirect_stderr(err):
            assert C.write_card_text(p, "без шапки, без шаблона\n", op="create") is True
            assert C.write_card_text(p, "без шапки, ещё раз\n", op="body") is True
        assert err.getvalue().count("шаблонов карточек нет") == 1, err.getvalue()
        assert _read(p) == "без шапки, ещё раз\n"
        # пустой реестр, переданный явно, — не выключатель
        for given in (C.Registry(), C.registry(str(root), kit_root=str(tmp / "none"))):
            try:
                C.write_card_text(p, "ещё текст\n", op="create", reg=given)
            except ValueError as e:
                assert "пустой реестр" in str(e), str(e)
            else:
                raise AssertionError("пустой реестр, переданный явно, отключил проверку")
        # шаблоны найдены, но ни один не принят (в паспортах нет card_type): запись отвергается, а не идёт «как раньше»
        proj = _project(tmp)
        for f in (proj / "TemplatesCommon").glob("akdb_*.md"):
            f.write_text(f.read_text(encoding="utf-8").replace("card_type:", "card_typo:"), encoding="utf-8")
        card = _path(proj)
        try:
            C.write_card_text(card, "мусор без шапки\n", op="create")
        except C.RegistryBroken as e:
            assert isinstance(e, C.TemplateViolation) and "ни один не принят" in str(e), str(e)
        else:
            raise AssertionError("поломанные шаблоны приняты за «шаблонов нет»")
        assert not os.path.exists(card)
        # исправили шаблоны — запись работает сразу: пустой реестр не запоминается
        for f in (proj / "TemplatesCommon").glob("akdb_*.md"):
            f.write_text(f.read_text(encoding="utf-8").replace("card_typo:", "card_type:"), encoding="utf-8")
        assert C.write_card_text(card, P42.mk(), op="create") is True
    finally:
        C._find_kit = real_kit
    # выключателя «обойти проверку» нет ни в аргументах, ни в окружении
    params = set(inspect.signature(C.write_card_text).parameters)
    assert params == {"path", "new_text", "op", "old_text", "reg"}, params
    src = (SCRIPTS / "card_types.py").read_text(encoding="utf-8")
    body = src[src.index("def write_card_text"):src.index("def try_write_card")]
    assert "environ" not in body and "getenv" not in body, "вход записи читает окружение: выключателя быть не должно"


@test
def test_gate_settled_card_cannot_drop_the_thesis_mark(tmp: Path):
    """Карточка, у которой тезис уже был, не возвращается правкой в «ещё без тезиса»: убрать `distilled` и подставить
    мусорное тело нельзя; а настоящее снятие отметки при обновлении источника (тезис остаётся) проходит."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = Path(_path(root))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(P42.mk().encode("utf-8"))
    junk = "# Другое имя\n\n- {{макрос}}\n\n## Что угодно\nTODO: допишу\n[ПРИМЕР]\n"
    for op in C.WRITE_OPS_DIFF:
        try:
            C.write_card_text(str(p), P42.mk(body=junk, distilled=None), op=op, reg=reg)
        except C.TemplateViolation as e:
            assert {"V-09", "V-12", "V-13"} & set(_codes_of(e)), _codes_of(e)
        else:
            raise AssertionError(f"{op}: снятие отметки тезиса открыло дорогу мусору")
    # разбор обновил источник: отметка ушла в distilled_was, тезис и раздел «Источник» на месте
    refreshed = P42.mk(distilled=None, distilled_was="2026-10-01")
    assert C.write_card_text(str(p), refreshed, op="body", reg=reg) is True
    # свежая карточка без тезиса (создание) судится мягко: тело — перенос источника
    fresh = Path(_path(root, "AuroraKnowledgeDB/Concepts/Свежая.md"))
    assert C.write_card_text(str(fresh), P42.mk(body=junk, distilled=None, title='"Другое имя"'), op="create", reg=reg)


@test
def test_gate_section_must_lead_the_type(tmp: Path):
    """Создание: карточка лежит в разделе своего типа; `spec` — только в `Specs/`; `correction` — не карточка базы.
    Правка уже лежащей не по месту карточки (унаследованное) этим не блокируется."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    wrong = _path(root, "AuroraKnowledgeDB/Glossary/Не-в-том-разделе.md")
    try:
        C.write_card_text(wrong, P42.mk(), op="create", reg=reg)             # type: concept в Glossary
    except C.TemplateViolation as e:
        assert "V-03" in _codes_of(e) and "Glossary" in str(e), str(e)
    else:
        raise AssertionError("карточка понятия принята в разделе терминов")
    spec = "---\ntype: spec\ntitle: Спека\nstatus: draft\n---\n\nСпецификация разработки.\n"
    for rel, ok in (("AuroraKnowledgeDB/Specs/Спека.md", True), ("AuroraKnowledgeDB/Concepts/Спека.md", False)):
        try:
            C.write_card_text(_path(root, rel), spec, op="create", reg=reg)
        except C.TemplateViolation:
            assert not ok, rel
        else:
            assert ok, f"{rel}: type: spec принят вне Specs/"
    corr = ("---\ntype: correction\ntemplate: akdb_correction_v1.0.md\ncorrects: Foo\ncreated: 2026-10-01\n"
            "status: active\n---\n\nПравда такая.\n")
    try:
        C.write_card_text(_path(root, "AuroraKnowledgeDB/Concepts/Исправление.md"), corr, op="create", reg=reg)
    except C.TemplateViolation as e:
        assert "Raw/corrections" in str(e), str(e)
    else:
        raise AssertionError("исправление принято как карточка базы")
    # карточка, лежащая не по месту ещё до правки, правится как правилась
    stray = Path(wrong)
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(P42.mk().encode("utf-8"))
    assert C.write_card_text(wrong, P42.mk(updated="2026-10-02"), op="header", reg=reg) is True


@test
def test_gate_repeat_of_the_same_text_is_not_judged(tmp: Path):
    """Тот же текст, что на диске, не пишется и не судится: повторный прогон не плодит отказов по унаследованному."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = Path(_path(root))
    p.parent.mkdir(parents=True, exist_ok=True)
    bad = _BAD["V-12"]
    p.write_bytes(bad.encode("utf-8"))
    for op in C.WRITE_OPS:
        assert C.write_card_text(str(p), bad, op=op, reg=reg) is False, op


@test
def test_gate_refuses_a_link_to_a_file_outside_the_base(tmp: Path):
    """Ссылка внутри проекта на файл зеркала источников не даёт перезаписать этот файл карточкой."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    target = root / "Sources" / "Confluence" / "Страница.md"
    target.parent.mkdir(parents=True)
    target.write_text("страница зеркала\n", encoding="utf-8")
    link = root / "AuroraKnowledgeDB" / "Concepts" / "Ссылка.md"
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        return                                     # символические ссылки недоступны (Windows без прав)
    try:
        C.write_card_text(str(link), P42.mk(), op="body", reg=reg)
    except OSError as e:
        assert "вне базы" in str(e) or "ссылка" in str(e), str(e)
    else:
        raise AssertionError("карточка записана поверх страницы зеркала")
    assert target.read_text(encoding="utf-8") == "страница зеркала\n"


@test
def test_gate_counts_every_placeholder_not_only_the_first(tmp: Path):
    """Валидатор называет первый остаток шаблона, но вход считает все: к одному `{{…}}` нельзя дописать ещё четыре."""
    C = _C()
    root = _project(tmp)
    reg = _reg(root)
    p = Path(_path(root))
    p.parent.mkdir(parents=True, exist_ok=True)
    one = P42.mk(body=P42.LEAD + "\n## Свойства\n- {{а}} — значение\n")
    five = P42.mk(body=P42.LEAD + "\n## Свойства\n" + "".join(f"- {{{{п{i}}}}} — значение\n" for i in range(5)))
    p.write_bytes(one.encode("utf-8"))
    try:
        C.write_card_text(str(p), five, op="body", reg=reg)
    except C.TemplateViolation as e:
        assert "V-13" in _codes_of(e), _codes_of(e)
    else:
        raise AssertionError("к одному плейсхолдеру дописали четыре, и это принято")
    # меньше прежнего — можно
    p.write_bytes(five.encode("utf-8"))
    assert C.write_card_text(str(p), one, op="body", reg=reg) is True
    # разные причины V-12 — разные ошибки: «список вместо определения» не прячется за «слишком длинно»
    long_lead = P42.mk(body="Слово " * 200 + "\n")
    p.write_bytes(long_lead.encode("utf-8"))
    try:
        C.write_card_text(str(p), _BAD["V-12"], op="body", reg=reg)
    except C.TemplateViolation as e:
        assert "V-12" in _codes_of(e), _codes_of(e)
    else:
        raise AssertionError("другая причина V-12 принята за прежнюю")


@test
def test_gate_two_threads_never_mix_their_texts(tmp: Path):
    """Два потока пишут один файл: читатель видит либо один текст, либо другой, но не смесь; остатков нет."""
    C = _C()
    import threading
    root = _project(tmp)
    p = _path(root)
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    a, b = "A" * 40000 + "\n", "B" * 15000 + "\n"
    C.write_atomic(p, a)
    stop, bad = threading.Event(), []

    def writer(text):
        for _ in range(150):
            C.write_atomic(p, text)

    def reader():
        while not stop.is_set():
            data = Path(p).read_bytes().decode("utf-8")
            if data not in (a, b):
                bad.append(len(data))

    watch = threading.Thread(target=reader)
    writers = [threading.Thread(target=writer, args=(t,)) for t in (a, b)]
    watch.start()
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    stop.set()
    watch.join()
    assert not bad, f"читатель видел смесь двух записей: {bad[:3]}"
    assert not _leftovers(Path(p).parent), _leftovers(Path(p).parent)
    assert all(len(x) < 40 for x in os.listdir(Path(p).parent) if x.startswith(".")), "временный файл с длинным именем"


@test
def test_gate_errors_survive_pickle(tmp: Path):
    """`TemplateViolation` не теряет путь, операцию и нарушения при передаче между процессами."""
    C = _C()
    import pickle
    e = C.TemplateViolation("AuroraKnowledgeDB/Concepts/x.md", "body",
                            [C.Violation("V-12", "error", 3, "пусто", "подсказка")])
    back = pickle.loads(pickle.dumps(e))
    assert (back.path, back.op, [v.code for v in back.violations], str(back)) == \
        (e.path, e.op, ["V-12"], str(e))
    assert isinstance(pickle.loads(pickle.dumps(C.RegistryBroken("p", "create", e.violations))), C.RegistryBroken)


@test
def test_render_header_list_and_title_forms(tmp: Path):
    """Одно значение списочного поля — список из одного пункта; множество — по порядку; название без внешних кавычек;
    имя поля с переводом строки отвергается."""
    C = _C()
    from aurora_common import frontmatter, list_field
    reg = _reg(_project(tmp))
    text = C.render_header("concept", {"title": '"Заявка"', "aliases": "Поставка", "tags": {"б", "а"},
                                       "related": "[[A]], [[B]]", "status": "draft"}, reg=reg)
    fm = frontmatter(text)
    assert fm["title"] == "Заявка" and text.count('title: "Заявка"') == 1, text
    assert list_field(text, "aliases") == ["Поставка"], text
    assert list_field(text, "tags") == ["а", "б"], text
    assert "related: [" in text and "[[A]], [[B]]" not in text.split("related:")[0], text
    for bad in ("x\n", "\nx"):
        try:
            C.render_header("concept", {bad: "1"}, reg=reg)
        except ValueError:
            pass
        else:
            raise AssertionError(f"имя поля {bad!r} принято")


@test
def test_registry_for_follows_templates_and_config(tmp: Path):
    """Реестр для записи собирается один раз на корень, но новый шаблон и переключатель типа в конфиге видны сразу."""
    C = _C()
    root = _project(tmp)
    first = C.registry_for(str(root), kit_root=str(tmp / "no-kit"))
    assert C.registry_for(str(root), kit_root=str(tmp / "no-kit")) is first, "реестр пересобран без причины"
    assert first.types["glossary"].enabled
    (root / "aurora.config.yaml").write_text('cards:\n  glossary:\n    enabled: "false"\n', encoding="utf-8")
    second = C.registry_for(str(root), kit_root=str(tmp / "no-kit"))
    assert second is not first and not second.types["glossary"].enabled, "переключатель типа не виден"
    P42._put(root / "Templates" / "akdb_policy_v1.0.md", P42._template_text(P42.POLICY))
    third = C.registry_for(str(root), kit_root=str(tmp / "no-kit"))
    assert "policy" in third.types, "новый шаблон проекта не виден"


# ------------------------------------------------------------------ CT-09: тело, перенесённое разбором, ещё без тезиса

@test
def test_validator_raw_transfer_before_the_thesis(tmp: Path):
    """Свежая машинная карточка знания — всё её тело перенос источника: заголовки и `{{…}}` внутри не ошибки,
    пока тезис не написан; с тезисом (есть раздел «Источник») и у рукописной — правила знания прежние."""
    C = _C()
    reg = _reg(_project(tmp))
    raw_body = "# Заявка на поставку\n\n## Раздел источника\n\n- пункт {{макрос}}\n\n| а | б |\n|---|---|\n"
    codes = lambda text: {v.code for v in C.validate_card(text, path="AuroraKnowledgeDB/Concepts/x.md", reg=reg, level="strict")  # noqa: E731
                          if v.severity == "error"}
    raw = P42.mk(body=raw_body, distilled=None)
    assert not codes(raw), codes(raw)
    # тезис написан (есть отметка) — правила знания
    assert {"V-09", "V-13"} <= codes(P42.mk(body=raw_body)), codes(P42.mk(body=raw_body))
    # раздел «Источник» есть — тезис был, правила знания
    with_quotes = P42.mk(body=raw_body + "\n## Источник (перенесено дословно)\n\n### Sources/x.md\n\nТекст.\n", distilled=None)
    assert {"V-09", "V-13"} <= codes(with_quotes), codes(with_quotes)
    # разбор заменил текст источника и снял прежний тезис: определения пока нет — это не ошибка, пока тезис не написан
    quotes = "\n## Источник (перенесено дословно)\n\n### Sources/x.md\n\nТекст.\n"
    waiting = P42.mk(body="# Заявка на поставку\n" + quotes, distilled=None)
    assert not codes(waiting), codes(waiting)
    assert "V-12" in codes(P42.mk(body="# Заявка на поставку\n" + quotes)), "с отметкой тезиса пустая часть — ошибка"
    assert "V-12" in codes(P42.mk(body="# Заявка на поставку\n" + quotes, distilled=None, built=None)), \
        "рукописная карточка без определения — ошибка"
    # рукописная карточка — правила знания
    handmade = P42.mk(body=raw_body, built=None, distilled=None)
    assert {"V-09", "V-13"} <= codes(handmade), codes(handmade)
    # вид словаря и заготовка перенос и так не проверяют; служебный порядок хвоста проверяется всегда
    wrong = P42.mk(body="# Заявка на поставку\n\nТекст.\n\n## История изменений\n\n- а\n\n## Исправления человеком\n\nб\n",
                   distilled=None)
    assert "V-10" in codes(wrong), codes(wrong)


# ------------------------------------------------------------------ CT-09: храповик мест записи

def _fake_kit(tmp: Path, source: str) -> Path:
    root = tmp / "kit"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "mod.py").write_text(source, encoding="utf-8")
    return root


@test
def test_card_write_sites_scan_forms(tmp: Path):
    """Обход видит все формы записи и не видит чтения; ключ — `файл:функция`, вложенная — `внешняя.вложенная`;
    режим не литералом считается записью; подпапки обходятся."""
    S = _sites()
    src = (
        "import os, shutil\n"
        "from pathlib import Path\n"
        "from shutil import copyfile\n"
        "def a(p):\n    open(p, 'w').write('x')\n"
        "def b(p):\n    Path(p).write_text('x')\n"
        "def c(p, t):\n    write_if_changed(p, t)\n"
        "def d(p, t):\n    aurora_common.replace_file(p, t)\n"
        "def e(p, t):\n    write_text_atomic(p, t)\n"
        "def f(p, q):\n    os.rename(p, q)\n"
        "def g(p, q):\n    shutil.move(p, q)\n"
        "def h(p):\n    open(p, mode='ab').write(b'x')\n"
        "def i(p):\n    Path(p).open('w')\n"
        "def read_only(p):\n    return open(p).read() + open(p, 'r').read() + Path(p).read_text()\n"
        "def outer(p):\n    def inner(q):\n        open(q, 'w').write('x')\n    return inner\n"
        "class K:\n    def m(self, p):\n        Path(p).write_bytes(b'x')\n"
        "def plan(pl):\n    pl.rename('a', 'b')\n"
        "def unknown_mode(p, how):\n    open(p, how)\n"
        "def unknown_kw(p, how):\n    open(p, mode=how)\n"
        "def plain_copy(a, b):\n    copyfile(a, b)\n"
        "def lock(p):\n    os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)\n"
        "def read_fd(p):\n    os.open(p, os.O_RDONLY)\n"
        "def move_helper(plan, a, b):\n    plan.move(a, b)\n"
    )
    root = _fake_kit(tmp, src)
    sub = root / "scripts" / "deeper"
    sub.mkdir()
    (sub / "mod2.py").write_text("def z(p):\n    open(p, 'w')\n", encoding="utf-8")
    found = S.scan(root)
    names = sorted(k.split(":", 1)[1] for k in found)
    assert names == ["K.m", "a", "b", "c", "d", "e", "f", "g", "h", "i", "lock", "outer.inner", "plain_copy",
                     "unknown_kw", "unknown_mode", "z"], names
    assert sorted(k.split(":", 1)[0] for k in found if k.endswith(":z")) == ["scripts/deeper/mod2.py"], found
    # синтаксическая ошибка не прячет файл: она сама — расхождение
    (root / "scripts" / "broken.py").write_text("def x(:\n", encoding="utf-8")
    problems = S.verdict(S.scan(root), {"card_max": 0, "sites": {}})
    assert any("broken.py" in p and "не разобрался" in p for p in problems), problems


@test
def test_card_write_sites_ratchet(tmp: Path):
    """AC-CT-09.3: храповик краснеет на новом месте записи, на устаревшей записи, на `card` сверх планки и под планкой,
    на новом вызове записи в уже названной функции и без причины."""
    S = _sites()
    # настоящее дерево: код и файл сходятся
    lines = S.scan_lines()
    found = {k: v[0] for k, v in lines.items()}
    assert S.verdict(found, S.load(), lines) == [], S.verdict(found, S.load(), lines)
    root = _fake_kit(tmp, "def w(p):\n    open(p, 'w').write('x')\n")
    lines = S.scan_lines(root)
    found = {k: v[0] for k, v in lines.items()}
    key = "scripts/mod.py:w"
    assert list(found) == [key]
    empty = {"card_max": 0, "sites": {}}
    assert any("не названо" in p and key in p for p in S.verdict(found, empty, lines)), "новое место не замечено"
    named = {"card_max": 0, "sites": {key: {"kind": "other", "reason": "служебный файл", "calls": 1}}}
    assert S.verdict(found, named, lines) == [], S.verdict(found, named, lines)
    assert any("нет reason" in p for p in S.verdict(found, {"card_max": 0, "sites": {key: {"kind": "other"}}}, lines))
    assert any("kind" in p for p in S.verdict(found, {"card_max": 0, "sites": {key: {"kind": "?", "reason": ""}}}, lines))
    over = {"card_max": 0, "sites": {key: {"kind": "card", "reason": "прямая запись"}}}
    assert any("card_max" in p for p in S.verdict(found, over, lines)), "рост числа мест card не замечен"
    allowed = {"card_max": 1, "sites": {key: {"kind": "card", "reason": "прямая запись", "calls": 1}}}
    assert S.verdict(found, allowed, lines) == []
    # планка выше числа мест card — тоже красное: иначе десять мест можно вернуть, не покраснев
    assert any("затяните планку" in p for p in S.verdict(found, {"card_max": 3, "sites": named["sites"]}, lines)), \
        "планка выше числа мест не замечена"
    stale = {"card_max": 0, "sites": {key: {"kind": "other", "reason": "x"}, "scripts/mod.py:gone": {"kind": "other", "reason": "x"}}}
    assert any("которого в коде нет" in p and "gone" in p for p in S.verdict(found, stale, lines)), "устаревшая запись не замечена"
    # новый вызов записи в названной функции: то же место, но записей стало больше
    grown = _fake_kit(tmp / "grown", "def w(p):\n    open(p, 'w').write('x')\n    open(p + '2', 'w').write('y')\n")
    glines = S.scan_lines(grown)
    gfound = {k: v[0] for k, v in glines.items()}
    assert any("вызовов записи стало 2 вместо 1" in p for p in S.verdict(gfound, named, glines)), \
        "новая запись в названной функции не замечена"
    # снимок: прежние названия сохраняются, новые — «?», calls проставлены, планка по числу card
    snap = S.snapshot(gfound, named, glines)
    assert snap["sites"][key]["calls"] == 2 and snap["sites"][key]["reason"] == "служебный файл" and snap["card_max"] == 0
    assert S.snapshot(gfound, empty, glines)["sites"][key]["kind"] == "?"
    # то же — командой: код возврата 0 на настоящем дереве
    import subprocess
    cp = subprocess.run([sys.executable, str(KIT / "tests" / "card_write_sites.py"), "--check"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr


@test
def test_card_write_sites_new_direct_write_in_a_script_turns_red(tmp: Path):
    """Новое прямое место записи в скрипте движка краснит проверку на настоящем списке допускаемых мест."""
    S = _sites()
    root = tmp / "kit"
    for sub in ("scripts", "cockpit"):
        (root / sub).mkdir(parents=True)
    # скрипт, который пишет карточку напрямую
    (root / "scripts" / "kb_newcomer.py").write_text(
        "def add_card(path, text):\n    open(path, 'w', encoding='utf-8').write(text)\n", encoding="utf-8")
    real = S.scan_lines()
    lines = {**real, **S.scan_lines(root)}
    found = {k: v[0] for k, v in lines.items()}
    problems = S.verdict(found, S.load(), lines)
    assert any("kb_newcomer.py:add_card" in p for p in problems), problems
