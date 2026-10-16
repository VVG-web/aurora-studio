"""Проверки движка Aurora, часть 9 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys
import time

from harness import (  # noqa: F401
    KIT,
    SCRIPTS,
    _cockpit_on,
    _embed_search,
    _write_embed_index,
    _write_minimal_docx,
    card,
    card_srcs,
    make_project,
    panel_sources,
    run,
    test,
    why,
)


@test
def test_update_removes_retired_engine_files(tmp: Path):
    """Слитые скрипты уезжают из проекта, а не остаются рядом работать по-своему.

    После слияния команда исполняется другим файлом, но прежняя копия в `.aurora/scripts`
    продолжала запускаться руками и расходиться с kit'ом. Список выведенных ведётся в
    манифесте (строки `- путь`) — угадывать «наш файл или проектный» обновление не вправе.
    """
    root = make_project(tmp)
    scripts = root / ".aurora/scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "kb_queue.py").write_text("# старая копия\n", encoding="utf-8")
    (scripts / "мой_скрипт.py").write_text("# проектный\n", encoding="utf-8")
    (root / "aurora.config.yaml").write_text('project:\n  name: "T"\n  slug: "T"\n',
                                             encoding="utf-8")

    dry = subprocess.run([sys.executable, str(KIT / "scripts/aurora_update.py"), str(root)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "kb_queue.py" in dry.stdout and "Выведены из движка" in dry.stdout, dry.stdout[:800]
    assert (scripts / "kb_queue.py").is_file(), "dry-run удалил файл"

    subprocess.run([sys.executable, str(KIT / "scripts/aurora_update.py"), str(root), "--apply"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert not (scripts / "kb_queue.py").exists(), "выведенный скрипт остался в проекте"
    assert (scripts / "мой_скрипт.py").is_file(), "обновление удалило чужой файл"
    assert (scripts / "kb_trace.py").is_file(), "новый скрипт не разложен"


@test
def test_remap_jira_moves_sources_to_issue_keys(tmp: Path):
    """Ссылки карточек переезжают со старых имён файлов Jira на ключи задач.

    Пока карточка ссылается на копию под старым именем, `--prune` не имеет права её
    удалить — это оборвало бы провенанс. Значит, сначала переезд ссылок, потом чистка.
    """
    root = make_project(tmp)
    mirror = root / "Sources/JIRA"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "update_log.md").write_text("| Issue Key | Updated | Status | Local Path |\n",
                                          encoding="utf-8")
    (mirror / "PRJ-327.md").write_text(
        "# PRJ-327: US-3.1.1. Приём\n\n| **Key** | PRJ-327 |\n", encoding="utf-8")
    (mirror / "US-3.1.1.md").write_text(
        "# PRJ-327: US-3.1.1. Приём\n\n| **Key** | PRJ-327 |\n", encoding="utf-8")
    card = root / "AuroraKnowledgeDB/Concepts/Приём.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("---\nsource: Sources/JIRA/US-3.1.1.md\n---\n\nтекст\n", encoding="utf-8")
    gone = root / "AuroraKnowledgeDB/Concepts/Потеряшка.md"
    gone.write_text("---\nsource: Sources/JIRA/3-1-9.md\n---\n\nтекст\n", encoding="utf-8")

    dry = run("kb_remap.py", "--mirror", "Sources/JIRA", cwd=root)
    assert "`US-3.1.1.md` → `PRJ-327.md`" in dry.stdout, dry.stdout[:600]
    assert "Ссылки в никуда (1)" in dry.stdout, "ссылка на несуществующий файл не отмечена"
    assert "Sources/JIRA/US-3.1.1.md" in card_srcs(card.read_text(encoding="utf-8")), \
        "dry-run не должен писать в карточки"

    run("kb_remap.py", "--mirror", "Sources/JIRA", "--apply", cwd=root)
    assert "Sources/JIRA/PRJ-327.md" in card_srcs(card.read_text(encoding="utf-8")), \
        "ссылка не перенацелена на ключ задачи"

    sys.path.insert(0, str(KIT / "scripts"))
    import jira_export as je
    assert je.cited(str(mirror), ["US-3.1.1.md"]) == set(), \
        "после переезда копия свободна и prune может её убрать"


@test
def test_kb_graph_builds_links_by_ry_and_story_number(tmp: Path):
    """Связи собираются по объявленным правилам, а не по догадкам.

    Ключ RY объявлен один раз — он и есть адрес; номер истории связывает критерии,
    задачу и саму историю. Всё, на что история ссылается по ключам, — её дети.
    """
    root = make_project(tmp)
    conf = root / "Sources/Confluence"
    jira = root / "Sources/JIRA"
    (conf / "Истории").mkdir(parents=True, exist_ok=True)
    (jira).mkdir(parents=True, exist_ok=True)

    def page(rel, title, defines=(), links=()):
        f = conf / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        head = [f'title: "{title}"', "page_id: 1", "space: SP"]
        if defines:
            head.append("ry_defines: [" + ", ".join(defines) + "]")
        if links:
            head.append("ry_links: [" + ", ".join(links) + "]")
        f.write_text("---\n" + "\n".join(head) + "\n---\n\nтекст\n", encoding="utf-8")

    page("Алгоритмы/ALG-026.md", "ALG-026 Сохранение данных", defines=["RU.PRJ.ALG-026"])
    page("Справочники/SPR-032.md", "SPR-032 Типы корректировок", defines=["RU.PRJ.SPR-032"])
    page("Истории/US-4.4.2.md", "US-4.4.2. Приём корректировки",
         links=["RU.PRJ.ALG-026", "RU.PRJ.SPR-032", "RU.PRJ.NO-001"])
    page("Критерии/AC-4.4.2.md", "AC-4.4.2. Приём корректировки")
    (jira / "PRJ-1895.md").write_text(
        "# PRJ-1895: US-4.4.2. Приём корректировки\n\n"
        "| Field | Value |\n| --- | --- |\n| **Key** | PRJ-1895 |\n"
        "| **Summary** | US-4.4.2. Приём корректировки |\n", encoding="utf-8")

    cp = run("kb_graph.py", cwd=root)
    assert "ключей RY объявлено: **2**" in cp.stdout, cp.stdout[:500]
    assert "связей по ключам: **2**" in cp.stdout, "ребро строится только на объявленный ключ"
    assert "висячих ключей (ссылка есть, объявления нет): **1**" in cp.stdout, cp.stdout[:600]

    one = run("kb_graph.py", "--story", "4.4.2", cwd=root).stdout
    assert "AC-4.4.2" in one and "PRJ-1895" in one, f"предки истории не собраны:\n{one}"
    assert "ALG · `RU.PRJ.ALG-026`" in one and "SPR · `RU.PRJ.SPR-032`" in one, \
        f"дети истории не собраны или тип не распознан:\n{one}"

    # MOC генерируется целиком: ручных правок в нём не держат
    run("kb_graph.py", "--write", cwd=root)
    moc = (root / "AuroraKnowledgeDB/MOC/Связи.md").read_text(encoding="utf-8")
    assert "ФАЙЛ ГЕНЕРИРУЕТСЯ" in moc and "type: moc" in moc, moc[:300]


@test
def test_drawio_asset_gets_extension(tmp: Path):
    """Вложение draw.io называется именем диаграммы — без расширения его нечем открыть.

    Confluence хранит схему под именем «Диаграмма без названия-1779…», и файл с таким
    именем не откроется ни редактором, ни просмотрщиком. Расширение подставляем по виду
    схемы, а файл под прежним именем убираем: `--prune` внутрь папок со схемами не ходит.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce

    class FakeApi:
        def attachments(self, page_id):
            # второе имя — с точками внутри: «.Кол» не расширение, а часть названия
            return {"Диаграмма-1": "/download/1", "Стр4.Свод.Итог": "/download/2",
                    "Готовая.drawio": "/download/3"}
        def fetch(self, url, max_bytes=None):
            return b"<mxfile/>"

    out = tmp / "mirror"
    (out / "Раздел").mkdir(parents=True)
    exp = ce.Exporter.__new__(ce.Exporter)
    exp.api, exp.out = FakeApi(), str(out)
    exp.assets_saved = exp.assets_dropped = 0
    stale = out / "Раздел/Стр_assets"
    stale.mkdir()
    (stale / "Диаграмма-1").write_text("старое имя без расширения", encoding="utf-8")

    md = exp.save_assets("1", "Раздел/Стр.md",
                         [("drawio", "Диаграмма-1"), ("drawio", "Стр4.Свод.Итог"),
                          ("drawio", "Готовая.drawio")])
    assert (stale / "Диаграмма-1.drawio").is_file(), "схема сохранена без расширения"
    assert (stale / "Стр4.Свод.Итог.drawio").is_file(), \
        "точка в названии принята за расширение — файл остался нечитаемым"
    assert (stale / "Готовая.drawio").is_file() and not (stale / "Готовая.drawio.drawio").exists(), \
        "расширение задвоилось у вложения, где оно уже было"
    assert not (stale / "Диаграмма-1").exists(), "файл под прежним именем остался"
    assert "Диаграмма-1.drawio" in md, f"ссылка ведёт на старое имя:\n{md}"
    assert exp.assets_dropped == 1


@test
def test_confluence_export_keeps_macro_content(tmp: Path):
    """Данные из макросов доезжают до зеркала: дата, автор, задача, врезка.

    Все четыре лежат не в тексте, а в атрибутах и параметрах, и общая ветка выбрасывала
    их вместе с макросом. На живой странице это выглядело как «поля пустые»: дата
    изменения, автор, ссылка на задачу и целый раздел Acceptance criteria.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce
    storage = (
        '<table><tbody>'
        '<tr><th><p>Дата</p></th><td><p><time datetime="2026-01-15"/></p></td></tr>'
        '<tr><th><p>Автор</p></th><td><p>'
        '<ac:link><ri:user ri:username="v.petrov"/></ac:link></p></td></tr>'
        '<tr><th><p>Задача</p></th><td><p>'
        '<ac:structured-macro ac:name="jira"><ac:parameter ac:name="key">PRJ-1895</ac:parameter>'
        '</ac:structured-macro></p></td></tr></tbody></table>'
        '<h1>Acceptance criteria</h1>'
        '<ac:structured-macro ac:name="excerpt-include"><ac:parameter ac:name="">'
        '<ac:link><ri:page ri:content-title="AC-1.1 Приём"/></ac:link>'
        '</ac:parameter></ac:structured-macro>'
        '<p><ac:structured-macro ac:name="status-handy">'
        '<ac:parameter ac:name="Status">Утверждена</ac:parameter></ac:structured-macro></p>')
    md = ce.to_markdown(storage, "https://c.example.com", "SP", "https://jira.example.com")
    assert "2026-01-15" in md, f"дата из макроса потеряна:\n{md}"
    assert "@v.petrov" in md, f"упоминание пользователя потеряно:\n{md}"
    assert "PRJ-1895" in md, f"ссылка на задачу потеряна:\n{md}"
    assert "AC-1.1 Приём" in md and "Включено со страницы" in md, \
        f"врезка чужой страницы потеряна — раздел остаётся пустым:\n{md}"
    assert "[Статус: Утверждена]" in md, f"статус потерян:\n{md}"
    assert md == ce.to_markdown(storage, "https://c.example.com", "SP",
                                "https://jira.example.com"), "конвертация недетерминирована"
    # пробел в заголовке страницы кодируется плюсом, а не %2B: иначе ссылка ведёт в никуда
    assert "%2B" not in md, f"ссылка на страницу перекодирована:\n{md}"


@test
def test_force_rereads_but_writes_only_changes(tmp: Path):
    """`--force` перечитывает источник, но переписывает только изменившееся.

    Иначе счётчик «записано» означает объём выгрузки, а не объём изменений: 877 записей
    там, где не поменялось ничего, — и понять по нему, что произошло, нельзя. У зеркала
    задач сверка стояла всегда, у зеркала страниц её отменял `--force`.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce
    src = ("scripts/confluence_export.py", "scripts/jira_export.py")
    for rel in src:
        code = (KIT / rel).read_text(encoding="utf-8")
        assert "if exists else None" in code or "if os.path.isfile(full) else None" in code, \
            f"{rel}: содержимое на диске не читается для сверки"
        assert "not self.force else None" not in code, \
            f"{rel}: --force снова отменяет сверку с диском"
    # обе ветки счётчиков живы: записанное и пропущенное считаются раздельно
    code = (KIT / "scripts/confluence_export.py").read_text(encoding="utf-8")
    assert "self.written += 1" in code and "self.skipped += 1" in code


@test
def test_tables_stay_markdown_unless_impossible(tmp: Path):
    """HTML в зеркале — только там, где markdown не выражает содержимое.

    Прежнее правило отправляло в HTML таблицу с любым списком или вторым абзацем в
    ячейке — на живой базе это 388 таблиц из 807, то есть почти половина знания приезжала
    разметкой, которую ни прочитать глазами, ни разобрать поиском.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce
    ok = ('<table><tbody>'
          '<tr><th>Поле</th><th>Значение</th></tr>'
          '<tr><td><ul><li>раз</li><li>два</li></ul></td>'
          '<td><p>абзац</p><p>ещё абзац</p></td></tr>'
          '<tr><td><a href="https://jira.example.com/browse/PRJ-1">PRJ-1</a></td>'
          '<td><strong>жирным</strong></td></tr></tbody></table>')
    md = ce.to_markdown(ok, "https://c.example.com", "SP")
    assert "<table" not in md, f"таблица со списком ушла в HTML:\n{md}"
    assert "- раз<br>- два" in md, f"список в ячейке потерян:\n{md}"
    assert "абзац<br>ещё абзац" in md, f"второй абзац потерян:\n{md}"
    assert "[PRJ-1](https://jira.example.com/browse/PRJ-1)" in md, \
        f"ссылка в ячейке потеряна — раньше ячейка бралась голым текстом:\n{md}"
    assert "**жирным**" in md, "выделение в ячейке потеряно"
    assert md == ce.to_markdown(ok, "https://c.example.com", "SP"), "конвертация недетерминирована"

    # объединённые ячейки markdown не выражает — остаётся HTML, и признак не теряется
    merged = ('<table class="wrapped"><tbody><tr><td colspan="2" class="x">на две</td></tr>'
              "<tr><td>а</td><td>б</td></tr></tbody></table>")
    out = ce.to_markdown(merged, "https://c.example.com", "SP")
    assert "<table" in out and 'colspan="2"' in out, f"объединение потеряно:\n{out}"
    assert 'class=' not in out, "в HTML остались шумовые атрибуты"

    # вложенная таблица и многострочный код — тоже не выражаются
    nested = "<table><tbody><tr><td><table><tbody><tr><td>вложено</td></tr></tbody></table></td></tr></tbody></table>"
    assert "<table" in ce.to_markdown(nested, "https://c.example.com", "SP")
    code = "<table><tbody><tr><td><pre>строка1\nстрока2</pre></td></tr></tbody></table>"
    assert "<table" in ce.to_markdown(code, "https://c.example.com", "SP")


@test
def test_confluence_export_keeps_plugin_diagrams(tmp: Path):
    """Диаграммы плагинов доезжают: mermaid — текстом, draw.io — исходником во вложении."""
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce
    st = ('<ac:structured-macro ac:name="mermaid"><ac:plain-text-body>'
          'graph TD; A--&gt;B;</ac:plain-text-body></ac:structured-macro>'
          '<ac:structured-macro ac:name="drawio">'
          '<ac:parameter ac:name="diagramName">Схема-входа</ac:parameter>'
          '</ac:structured-macro>'
          '<ac:structured-macro ac:name="excerpt"><ac:rich-text-body>'
          '<p>Печатная форма</p></ac:rich-text-body></ac:structured-macro>')
    assets = []
    md = ce.to_markdown(st, "https://c.example.com", "SP", "", assets)
    assert "```mermaid" in md and "graph TD" in md, f"диаграмма mermaid потеряна:\n{md}"
    assert md.count("```") == 2, f"ограда кода задвоилась:\n{md}"
    assert assets == [("drawio", "Схема-входа")], f"исходник draw.io не запрошен: {assets}"
    assert "Врезка (excerpt)" in md and "Печатная форма" in md, \
        f"врезка потеряна или не помечена:\n{md}"

    # врезка таблицы, внешний макет и история правок — три макроса из живых страниц
    more = ('<ac:structured-macro ac:name="table-excerpt-include">'
            '<ac:parameter ac:name="name">RU_ALL</ac:parameter>'
            '<ac:parameter ac:name="page"><ac:link>'
            '<ri:page ri:content-title="MAP-037 Маппинг"/></ac:link></ac:parameter>'
            '</ac:structured-macro>'
            '<ac:structured-macro ac:name="widget"><ac:parameter ac:name="url">'
            '<ri:url ri:value="https://figma.example.com/design/x"/></ac:parameter>'
            '</ac:structured-macro>'
            '<ac:structured-macro ac:name="change-history"/>')
    md2 = ce.to_markdown(more, "https://c.example.com", "SP")
    assert "MAP-037 Маппинг" in md2 and "RU\\_ALL" in md2, f"врезка таблицы потеряна:\n{md2}"
    assert "figma.example.com" in md2, f"ссылка на внешний макет потеряна:\n{md2}"

    # ограда внутри цитаты: expand/info становятся blockquote, и «> » ломает диаграмму
    quoted = ('<ac:structured-macro ac:name="expand"><ac:rich-text-body>'
              '<ac:structured-macro ac:name="mermaid"><ac:plain-text-body>'
              'flowchart TD\n  A --&gt; B</ac:plain-text-body></ac:structured-macro>'
              '</ac:rich-text-body></ac:structured-macro>')
    md3 = ce.to_markdown(quoted, "https://c.example.com", "SP")
    body = md3.split("```")[1]
    assert ">" not in body.replace("-->", ""), f"строки диаграммы в цитате:\n{md3}"

    # блок кода не должен обрастать второй оградой — это ломало подсветку в зеркале
    code = ('<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">sql'
            '</ac:parameter><ac:plain-text-body>SELECT 1;</ac:plain-text-body>'
            '</ac:structured-macro>')
    assert ce.to_markdown(code, "https://c.example.com", "SP").strip() == \
        "```sql\nSELECT 1;\n```"


@test
def test_mirror_cleanup_sees_foreign_files(tmp: Path):
    """Чистка зеркала видит не только `.md`.

    Файлы вроде `Имя.md_COLLISION` от прежних синк-скиллов пережили и `--force`, и
    `--prune`, потому что чистка смотрела на расширение. Папка с ними читалась человеком
    как дубль каталога, а аудит молчал: зеркало «чистое».
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import sources_core as sc
    import sync_audit as sa
    root = tmp / "mirror"
    (root / "Раздел").mkdir(parents=True)
    (root / "Раздел/Стр.md").write_text("---\npage_id: 1\n---\nтекст", encoding="utf-8")
    (root / "Раздел/Стр.md_COLLISION").write_text("мусор", encoding="utf-8")
    (root / "Раздел/.DS_Store").write_text("", encoding="utf-8")
    (root / "sync_state.md").write_text("состояние", encoding="utf-8")

    # схемы страницы лежат рядом с ней и принадлежат зеркалу, хотя в состоянии их нет
    (root / "Раздел/Стр_assets").mkdir()
    (root / "Раздел/Стр_assets/Схема.drawio").write_text("<mxfile/>", encoding="utf-8")

    m = sc.WikiMirror(str(root))
    extra = m.extra_files(["Раздел/Стр.md"])
    assert extra == ["Раздел/Стр.md_COLLISION"], \
        f"чистка сносит схемы страницы или не видит мусор: {extra}"
    assert sa.foreign_files(str(root)) == ["Раздел/Стр.md_COLLISION"], "аудит его не показывает"

    # после чистки опустевший каталог уходит: пустая папка читается как дубль
    (root / "Пусто/Вложено").mkdir(parents=True)
    (root / "Пусто/Вложено/.DS_Store").write_text("", encoding="utf-8")
    assert sc.drop_empty_dirs(str(root)) == 2, "опустевшие каталоги остались"
    assert not (root / "Пусто").exists() and (root / "Раздел").exists()


@test
def test_confluence_export_keeps_requirement_yogi_keys(tmp: Path):
    """Ключи Requirement Yogi и ссылки на них остаются в зеркале.

    Макрос RY не имеет тела, и общая ветка выбрасывала его вместе с ключом: в проекте,
    где трассировка между документами идёт через RY, зеркало теряло все связи разом.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import confluence_export as ce
    storage = (
        '<table><tbody><tr><th>ID</th><td><p>'
        '<ac:structured-macro ac:name="requirement" ac:schema-version="1" ac:macro-id="a">'
        '<ac:parameter ac:name="type">DEFINITION</ac:parameter>'
        '<ac:parameter ac:name="key">KB.ENS.URZ-251</ac:parameter>'
        '<ac:parameter ac:name="" /></ac:structured-macro></p></td></tr>'
        '<tr><th>Связано</th><td><p>'
        '<ac:structured-macro ac:name="requirement" ac:schema-version="1" ac:macro-id="b">'
        '<ac:parameter ac:name="freetext">Link</ac:parameter>'
        '<ac:parameter ac:name="type">LINK</ac:parameter>'
        '<ac:parameter ac:name="key">RU.PRJ.UI-012</ac:parameter></ac:structured-macro>'
        '</p></td></tr></tbody></table>')
    md = ce.to_markdown(storage, "https://c.example.com", "SP")
    assert "**RYk:KB.ENS.URZ-251**" in md, f"объявление ключа потеряно:\n{md}"
    assert "RYl:RU.PRJ.UI-012" in md, f"ссылка на ключ потеряна:\n{md}"
    assert "RYk:RU.PRJ.UI-012" not in md, "ссылка помечена как объявление ключа"
    assert md == ce.to_markdown(storage, "https://c.example.com", "SP"), "конвертация недетерминирована"

    # свойство требования и отчёт: свои метки, чтобы вид связи читался прямо в тексте
    extra = ('<p><ac:structured-macro ac:name="requirement-property">'
             '<ac:parameter ac:name="title">true</ac:parameter></ac:structured-macro>'
             '<ac:structured-macro ac:name="requirement-report">'
             '<ac:parameter ac:name="query">x</ac:parameter></ac:structured-macro></p>')
    md2 = ce.to_markdown(extra, "https://c.example.com", "SP")
    assert "RYo:title" in md2 and "RYr" in md2, f"свойство или отчёт потеряны:\n{md2}"

    defines, links = ce.ry_keys(storage)
    assert defines == ["KB.ENS.URZ-251"] and links == ["RU.PRJ.UI-012"], (defines, links)
    head = ce.render_front_matter({"id": "1", "title": "Стр", "space": "SP", "version": 1,
                                   "updated": "2026-07-30", "url": "https://c.example.com/x",
                                   "breadcrumbs": "Стр", "hash": "0" * 16,
                                   "ry_defines": defines, "ry_links": links})
    assert "ry_defines: [KB.ENS.URZ-251]" in head and "ry_links: [RU.PRJ.UI-012]" in head, head
    # ключ, объявленный на странице, не дублируется в списке ссылок
    both = storage + storage.replace("DEFINITION", "LINK")
    assert ce.ry_keys(both)[1] == ["RU.PRJ.UI-012"], "объявленный ключ попал в ссылки"


@test
def test_sync_audit_case_only_paths_are_not_a_loss(tmp: Path):
    """Регистр папки разошёлся с состоянием — это переименование, а не потеря страницы.

    Файловая система macOS и Windows к регистру нечувствительна: страницу переименовали в
    источнике, папка осталась под старым именем. Считать это одновременно MISSING и ORPHAN
    значит поднимать тревогу там, где ничего не пропало.
    """
    root = make_project(tmp)
    conf = root / "Sources/Confluence"
    (conf / "Раздел/Основной_баланс").mkdir(parents=True, exist_ok=True)
    (conf / "Раздел/Основной_баланс/index.md").write_text(
        "---\npage_id: 111111\n---\n\nтекст", encoding="utf-8")
    (conf / "sync_state.md").write_text(
        "**Sync Date:** 2026-07-25\n\n| # | Page ID | Title | Local Path | Status |\n"
        "|---|---|---|---|---|\n"
        "| 1 | 111111 | Баланс | Раздел/Основной_Баланс/index.md | SYNCED |\n", encoding="utf-8")
    cp = run("sync_audit.py", cwd=root, expect_rc=1)
    assert "CASE: **1**" in cp.stdout, f"расхождение регистра не выделено: {cp.stdout[:500]}"
    assert "MISSING: **0**" in cp.stdout, "страница объявлена потерянной, хотя она на месте"
    assert "ORPHAN: **0**" in cp.stdout, "тот же файл посчитан лишним"


@test
def test_registry_drives_mirrors_and_audit(tmp: Path):
    """Зеркала объявляют модули: движок не должен знать про Confluence и Jira по именам.

    Проверяем всю цепочку на выдуманном модуле: реестр видит его, аудит выбирает правила
    по объявленному виду хранилища, а `--source` сужает проверку до одного зеркала.
    """
    root = make_project(tmp, git=True)   # doctor считает проект без git ошибкой: откатывать нечем
    (root / ".aurora/connectors/demo-board.json").write_text(json.dumps({
        "id": "demo-board", "title": "Демо-доска", "kind": "board",
        "what": "выдуманный источник для теста",
        "mirror": {"default_path": "Sources/Demo", "state": "update_log.md"},
        "run": {"script": "demo_export.py", "command": "sync:demo", "skill": "demo-sync"},
        "auth": {"env_prefix": "DEMO"},
    }, ensure_ascii=False), encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") +
                   "\nsources:\n  - id: Demo\n    module: demo-board\n    path: Sources/Demo\n",
                   encoding="utf-8")

    out = run("sources_registry.py", cwd=root).stdout
    assert "demo-board" in out and "Sources/Demo" in out, f"модуль не в реестре:\n{out}"

    # зеркала нет на диске — doctor зовёт завести папку, но схему это не ломает
    doc = run("aurora_doctor.py", "--structure", cwd=root)
    assert "Sources/Demo" in doc.stdout, "doctor молчит про заявленное зеркало"
    assert doc.returncode == 0, "заявленное зеркало не должно быть ошибкой схемы"

    mirror = root / "Sources/Demo"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "DEMO-1.md").write_text("задача", encoding="utf-8")
    (mirror / "update_log.md").write_text(
        "**Sync Date:** 2026-07-30\n\n| Issue Key | Updated | Status | Local Path |\n"
        "|---|---|---|---|\n| DEMO-2 | 2026-07-30 10:00 | Готово | DEMO-2.md |\n",
        encoding="utf-8")
    cp = run("sync_audit.py", cwd=root, expect_rc=1)
    assert "## Demo (Sources/Demo)" in cp.stdout, f"зеркало модуля не проверено:\n{cp.stdout}"
    assert "MISSING: **1**" in cp.stdout and "ORPHAN: **1**" in cp.stdout, \
        f"правила board-зеркала не применились:\n{cp.stdout}"
    assert "Confluence" not in cp.stdout, \
        "проверено зеркало, которого нет в sources: — список берётся не из реестра"

    one = run("sync_audit.py", "--source", "Demo", cwd=root, expect_rc=1).stdout
    assert "## Demo" in one, "--source отсеял то, что просили"

    js = json.loads(run("sync_audit.py", "--json", cwd=root, expect_rc=1).stdout)
    assert js["mirrors"]["Demo"]["kind"] == "board", f"машинный итог без вида зеркала: {js}"
    assert js["mirrors"]["Demo"]["missing"] == 1, f"числа не сошлись: {js}"

    # зеркало без состояния сверять не с чем — но молчать о нём нельзя: панель покажет
    # «пусто» там, где на диске лежат данные, которые никто не проверяет
    (mirror / "update_log.md").unlink()
    js = json.loads(run("sync_audit.py", "--json", cwd=root, expect_rc=1).stdout)
    assert js["mirrors"]["Demo"]["no_state"] and js["mirrors"]["Demo"]["files"] == 1, \
        f"зеркало без состояния пропало из машинного итога: {js}"

    # папка без модуля — замечание, а не блокер: данные удалять нельзя
    (root / "Sources/Ничья").mkdir(parents=True, exist_ok=True)
    doc = run("aurora_doctor.py", "--structure", cwd=root)
    assert "зеркала без модуля" in doc.stdout and "Sources/Ничья" in doc.stdout, \
        f"ничья папка в Sources/ не названа:\n{doc.stdout}"
    assert doc.returncode == 0, "ничья папка не должна валить проверку структуры"


@test
def test_jira_prune_removes_only_unknown_files(tmp: Path):
    """`--prune` убирает следы прежних выгрузок, но не служебные файлы и не свежие задачи."""
    sys.path.insert(0, str(KIT / "scripts"))
    import jira_export as je
    root = make_project(tmp)
    mirror = root / "Sources/JIRA"
    mirror.mkdir(parents=True, exist_ok=True)
    for name in ("PRJ-1.md", "US-3.1.1.md", "JIRA_prompt.md", "JIRA_issue_template.md"):
        (mirror / name).write_text("текст", encoding="utf-8")
    state = je.write_state(str(mirror), [("PRJ-1", "2026-07-30 10:00", "Готово", "PRJ-1.md")])
    (mirror / "sync_report_2026-01-01.md").write_text("отчёт", encoding="utf-8")
    extra = je.stale(str(mirror), state)
    assert extra == ["US-3.1.1.md"], f"лишним должен быть только след старой выгрузки: {extra}"

    # упоминание номера в тексте — не ссылка на файл: иначе защита не даст удалить ничего
    (root / "AuroraKnowledgeDB/Concepts/Упоминание.md").write_text(
        "---\nsource: Sources/JIRA/PRJ-1.md\n---\n\nСделано в US-3.1.1\n", encoding="utf-8")
    assert je.cited(str(mirror), extra) == set(), \
        "совпадение по подстроке принято за ссылку на файл"

    # файл, на который ссылается карточка, удалять нельзя: это обрыв провенанса
    card = root / "AuroraKnowledgeDB/Concepts/Приём.md"
    card.parent.mkdir(parents=True, exist_ok=True)
    card.write_text("---\nsource: Sources/JIRA/US-3.1.1.md\n---\n\nтекст", encoding="utf-8")
    assert je.cited(str(mirror), extra) == {"US-3.1.1.md"}, \
        "файл, на который ссылается карточка, не защищён от удаления"


@test
def test_office_ingest_converts_and_is_idempotent(tmp: Path):
    root = make_project(tmp)
    docx = root / "Raw/customer/Документ.docx"
    _write_minimal_docx(docx)
    cp = run("office_ingest.py", cwd=root)
    md = root / "Raw/customer/Документ.md"
    assert md.exists(), f"транскрипт не создан: {cp.stdout}"
    text = md.read_text(encoding="utf-8")
    assert "converted_from:" in text and "Машинная конвертация" in text, "нет шапки провенанса"
    assert "Тестовый абзац" in text, "текст документа не извлечён"
    assert docx.exists(), "оригинал пропал (он же доказательство)"
    cp2 = run("office_ingest.py", cwd=root)
    assert "пропущено (не изменились): 1" in cp2.stdout, "повторный запуск переделывает работу"


@test
def test_a_hostile_docx_does_not_take_the_converter_down(tmp: Path):
    """Чужой docx: объявленные сущности и битый XML — «не смогли», а не падение разбора.

    Документ приходит от заказчика. XML с `<!ENTITY>` раздувается в гигабайты при
    разборе, а битый document.xml бросал `ParseError` мимо `try`, и весь прогон
    `office_ingest` падал на одном файле. Настоящий docx не объявляет ни того ни другого.
    """
    import zipfile
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    OI = importlib.import_module("office_ingest")
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def make(name: str, xml: str) -> str:
        path = tmp / name
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("word/document.xml", xml)
        return str(path)

    good = (f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>'
            '<w:p><w:r><w:t>Абзац</w:t></w:r></w:p></w:body></w:document>')
    assert "Абзац" in (OI.conv_docx_builtin(make("good.docx", good)) or ""), "обычный docx не разобран"
    bomb = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
            f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>')
    assert OI.conv_docx_builtin(make("bomb.docx", bomb)) is None, "XML с сущностями разобран"
    assert OI.conv_docx_builtin(make("broken.docx", "<w:document")) is None, \
        "битый XML уронил конвертер вместо отказа"


@test
def test_an_update_archive_cannot_write_outside_the_kit(tmp: Path):
    """Путь из архива обновления с обратной косой чертой не выводит за пределы кита.

    Проверка искала `..` только среди частей, разделённых «/». На Windows «..\\x» —
    тоже выход вверх, и архив с таким именем записал бы файл рядом с китом.
    """
    import io
    import zipfile
    kit = tmp / "aurora-studio"
    (kit).mkdir(parents=True)
    (kit / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("aurora-studio-master/VERSION", "1.1.0\n")
        z.writestr("aurora-studio-master/scripts\\..\\..\\evil.txt", "x")
    ck, restore = _cockpit_on(kit)
    ck._http_get = lambda url, limit, timeout=20: (
        b"1.1.0\n" if url.endswith("/VERSION") else buf.getvalue())
    try:
        r = ck.kit_update()
    finally:
        restore()
    assert "подозрительный путь" in r.get("error", ""), f"архив с обратной косой принят: {r}"
    assert not (tmp / "evil.txt").exists() and (kit / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


@test
def test_scan_without_text_layer_is_not_passed_off_as_converted(tmp: Path):
    """Скан без текстового слоя — это НЕ разобранный файл, и он должен дойти до распознавания.

    Регрессия с живого проекта: PDF на 2,8 МБ (16 страниц сканов) давал транскрипт на
    54 знака. Встроенный конвертер собирал строку из маркеров `<!-- стр. N -->`, проверял
    её через `text.strip()` — маркеры непустые! — и объявлял конвертацию удавшейся. Файл
    получал отметку в шапке, переставал быть кандидатом на распознавание, а в базу шёл
    пустой документ. Судить надо по тексту СТРАНИЦ, а не по склейке с разметкой.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    OI = importlib.import_module("office_ingest")

    assert OI._pages_or_none(["", "   ", "\n"]) is None, \
        "страницы без текста склеились в «транскрипт» — скан снова выдаётся за разобранный"
    got = OI._pages_or_none(["", "Статья 1. Текст.", ""])
    assert got and "Статья 1" in got and "<!-- стр. 2 -->" in got, \
        f"страница с текстом потерялась или потеряла нумерацию: {got!r}"

    names = [n for n, _fn in OI.CONVERTERS]
    assert names.index("ocr") > names.index("builtin-pdf"), \
        "распознавание стоит раньше парсеров — вызовы модели там, где текст лежит в файле"
    assert names.index("ocr") < names.index("plain"), "распознавание должно успевать до plain"

    # `--no-ocr` и оффлайн: каскад обязан честно отказать, а не выдумать пустой транскрипт
    scan = tmp / "скан.pdf"
    scan.write_bytes(b"%PDF-1.4\n% not a real pdf\n")
    text, conv = OI.convert(str(scan), "auto", use_ocr=False)
    assert text is None and conv is None, f"нечитаемый PDF выдан за разобранный: {conv!r}"


@test
def test_ocr_ring_is_separate_and_never_falls_back_to_chat(tmp: Path):
    """Кольцо распознавания — третье, независимое: не чат и не вектора.

    Зрячая модель, чат-модель и модель векторов живут на разных шлюзах. Главное отличие
    от эмбеддингов: в кольцо чата распознавание НЕ проваливается. Текстовая модель,
    получив картинку, не отказывается — она отвечает связной выдумкой, и выдумка в
    транскрипте первоисточника хуже пустого файла.
    """
    sys.path.insert(0, str(SCRIPTS))
    from agent_core import embed_ring, ocr_ring, parse_config, pool

    cfg = parse_config({
        "AURORA_AGENT_BACKEND_1_URL": "http://chat/v1", "AURORA_AGENT_BACKEND_1_MODEL": "qwen",
        "AURORA_AGENT_BACKEND_4_URL": "http://vision/v1",
        "AURORA_AGENT_BACKEND_4_OCR_MODEL": "glm-ocr",
        "AURORA_AGENT_BACKEND_5_URL": "http://vec/v1",
        "AURORA_AGENT_BACKEND_5_EMBED_MODEL": "bge-m3",
        "AURORA_OCR_MODEL": "glm-ocr", "AURORA_EMBED_MODEL": "bge-m3",
    })
    assert pool(cfg) == [1], f"зрячий и векторный шлюзы попали в чатовое кольцо: {pool(cfg)}"
    assert [r["n"] for r in ocr_ring(cfg)] == [4], \
        f"кольцо распознавания собралось не по объявлению OCR_MODEL: {ocr_ring(cfg)}"
    assert [r["n"] for r in embed_ring(cfg)] == [5], "кольца векторов и распознавания смешались"
    assert next(b for b in cfg["backends"] if b["n"] == 4)["chat"] is False, \
        "шлюз, объявивший только зрение, считается чатовым — вызов уйдёт на сервер без чат-модели"

    # Модель не названа — путь выключен. Умолчания тут быть не может: угаданное имя даёт
    # не отказ, а выдумку в транскрипте.
    off = parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://chat/v1",
                        "AURORA_AGENT_BACKEND_1_MODEL": "qwen"})
    assert ocr_ring(off) == [], "распознавание включилось само и пошло в чатовый шлюз"

    # Панель показывает и пишет ключи распознавания. Выпуск 1.104.0 ушёл без этого: кольцо
    # существовало только в файле настроек, панель о нём не знала и честно писала «отстала
    # от ядра». Человек не видит, что путь выключен, и ищет, почему скан остался пустым.
    # С 1.153.0 распознавание — возможность раздела «Модели»: роль `document`, цепочка
    # «провайдер + модель», и у каждого запасного своя зрячая модель.
    view = (KIT / "cockpit/modules/models/view.js").read_text(encoding="utf-8")
    assert '"ocr"' in view and 'num("dpi"' in view and 'num("max_pages"' in view, \
        "в панели нечем настроить распознавание"
    ui = panel_sources()
    assert "скан честно остаётся неразобранным" in ui, \
        "форма не говорит, что без бэкенда распознавание выключено"
    import importlib
    MC = importlib.import_module("model_config")
    data, _ = MC.normalize({"providers": [{"id": "v", "url": "http://vision/v1"},
                                          {"id": "w", "url": "http://work/v1"}],
        "capabilities": {"ocr": {"roles": [{"id": "document", "backends": [
            {"provider": "v", "model": "glm-ocr"}, {"provider": "w", "model": "qwen-vl"}]}]}}})
    assert [(r["url"], r["model"]) for r in ocr_ring(MC.to_config(data))] == \
        [("http://vision/v1", "glm-ocr"), ("http://work/v1", "qwen-vl")], \
        "запасной распознавания потерял свою модель"
    assert ocr_ring(MC.to_config(MC.normalize({})[0])) == [], \
        "распознавание включилось без бэкенда"


@test
def test_page_wrapped_in_form_is_not_erased(tmp: Path):
    """Страница внутри `<form>` — это документ, а не форма ввода.

    Регрессия с nalog.gov.ru: на ASP.NET WebForms весь документ лежит внутри одного
    `<form runat="server">`. Правило «выбросить form» стирало страницу до нуля, и движок
    сообщал «нет beautifulsoup4/markdownify» — то есть врал о причине: библиотеки стояли,
    а человек шёл ставить их заново. Органы управления убираем, текст оставляем.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    W = importlib.import_module("web_export")

    html = ("<html><head><title>Закон</title></head><body>"
            "<form id='MainForm' action='./'>"
            "<input type='text' name='q'><button>Найти</button>"
            "<h1>Национальная система</h1><p>Обеспечительный платёж вносится заранее.</p>"
            "<a href='https://x.example/doc'>Документ</a>"
            "</form></body></html>")
    title, body, links, _assets = W.to_markdown(html, "https://x.example/page")
    assert title == "Закон", f"заголовок потерян: {title!r}"
    assert "Обеспечительный платёж вносится заранее" in body, \
        f"текст внутри формы стёрт — страница приходит пустой: {body!r}"
    assert "Найти" not in body, "кнопка формы попала в документ как текст"
    assert "https://x.example/doc" in links, "ссылки внутри формы потеряны для обхода"

    assert hasattr(W, "HAVE_PARSER"), "нет признака «библиотеки разбора на месте»"
    src = (SCRIPTS / "web_export.py").read_text(encoding="utf-8")
    assert "страница пришла без текста" in src, \
        "пустая страница и отсутствующая библиотека снова описываются одним сообщением"


@test
def test_build_parallel_executes_concurrently(tmp: Path):
    """T4: run_build при «одновременно» = 2 и пуле из 2 — два источника сразу.

    Регрессия: build был сериальным for-циклом и пул читал только distill. Проверяем
    перекрытие интервалов, а не настенное время: два заглушенных solve_source «думают
    по 0.1 с, и сериальная обработка не может перекрыть эти интервалы, а пул на два
    потока — не может не перекрыть.
    """
    import threading
    from unittest.mock import patch

    sys.path.insert(0, str(KIT / "scripts"))
    from agent_core import parse_config
    from agent_runner import run_build

    root = make_project(tmp)
    cfg = parse_config({
        'AURORA_AGENT_BACKEND_1_URL': 'http://test',
        'AURORA_AGENT_BACKEND_1_MODEL': 'test',
        'AURORA_AGENT_BACKEND_1_WIDTH': '2',
        'AURORA_AGENT_PARALLEL': '2',
        'AURORA_AGENT_BUDGET_MIN': '20',
        'AURORA_AGENT_MAX_STEPS': '10',
        'AURORA_AGENT_REQUEST_TIMEOUT': '300',
    })

    marks, lock = {}, threading.Lock()

    def mock_solve(cfg_, *a, **k):
        src = a[2]
        t0 = time.monotonic()
        with lock:
            marks[src] = [t0, None]
        time.sleep(0.1)
        with lock:
            marks[src][1] = time.monotonic()
        return {'alias': 't', 'status': 'разобран', 'backends': [], 'degraded': False, 'note': ''}

    sources = [('Confluence', 'f1.md', 1), ('Confluence', 'f2.md', 1)]
    with patch('agent_runner.read_partition', return_value=sources), patch('agent_runner.solve_source', side_effect=mock_solve):
        res = run_build(cfg, str(root), False, True, 0)

    assert len(marks) == 2, f"обработаны не оба источника: {list(marks)}"
    (a0, a1), (b0, b1) = sorted(marks.values(), key=lambda p: p[0])
    assert a0 < b1 and b0 < a1, (
        f"интервалы solve_source ({a0:.3f}–{a1:.3f}, {b0:.3f}–{b1:.3f}) не пересекаются — "
        "обработка по очереди, а не пулом на два потока")
    assert len(res["steps"]) == 2 and all(s["status"] == "разобран" for s in res["steps"]), f"run_build потерял источник: {res['steps']}"


@test
def test_build_width_calculation_respects_cap(tmp: Path):
    """T4: width = min(слоты, источники) — потолок режет пул до числа источников.

    Пул из 2 слотов с одним источником — это сериальный путь в один слот, а не
    двухпоточный исполнитель, а «одновременно» незаданное / = 1 — всегда по очереди,
    какая бы широкая ни была ширина шлюза. Детерминизм: сериальный путь гоняет
    solve_source в главном потоке и строго один за другим.
    """
    import threading
    from unittest.mock import patch

    sys.path.insert(0, str(KIT / "scripts"))
    from agent_core import parse_config
    from agent_runner import run_build

    root = make_project(tmp)
    main_thread = threading.get_ident()

    def make_cfg(parallel=None):
        env = {'AURORA_AGENT_BACKEND_1_URL': 'http://test',
               'AURORA_AGENT_BACKEND_1_MODEL': 'test',
               'AURORA_AGENT_BACKEND_1_WIDTH': '2',
               'AURORA_AGENT_BUDGET_MIN': '20',
               'AURORA_AGENT_MAX_STEPS': '10',
               'AURORA_AGENT_REQUEST_TIMEOUT': '300'}
        if parallel is not None:
            env['AURORA_AGENT_PARALLEL'] = parallel
        return parse_config(env)

    def run_once(cfg_, n_sources):
        marks, lock = {}, threading.Lock()

        def mock_solve(cfg2, *a, **k):
            src = a[2]
            t0 = time.monotonic()
            with lock:
                marks[src] = [t0, None, threading.get_ident()]
            time.sleep(0.05)
            with lock:
                marks[src][1] = time.monotonic()
            return {'alias': 't', 'status': 'разобран', 'backends': [], 'degraded': False,
                    'note': ''}

        sources = [('Confluence', f's{i}.md', 1) for i in range(n_sources)]
        with patch('agent_runner.read_partition', return_value=sources), patch('agent_runner.solve_source', side_effect=mock_solve):
            res = run_build(cfg_, str(root), False, True, 0)
        return marks, res

    def assert_serial(marks, why):
        for s1 in marks:
            for s2 in marks:
                if s1 >= s2:
                    continue
                i1, i2 = marks[s1], marks[s2]
                assert i1[2] == main_thread and i2[2] == main_thread, f"{why}: solve_source ушёл в поток исполнителя — это параллельность"
                assert not (i1[0] < i2[1] and i2[0] < i1[1]), f"{why}: интервалы источников пересекаются — сериальный режим стал параллельным"

    # два слота пула, один источник: width = min(2, 1) = 1
    marks, res = run_once(make_cfg(parallel="2"), 1)
    assert len(marks) == 1 and len(res["steps"]) == 1, f"один источник должен дать один шаг: marks={list(marks)} steps={res['steps']}"
    assert next(iter(marks.values()))[2] == main_thread, \
        "один источник пошёл через исполнителя: пул из 2 слотов стартанул с 1 источника"

    # «одновременно» незаданное / = 1: два источника строго один за другим, в главном потоке
    for parallel in (None, "1"):
        why = f"PARALLEL={'не задан' if parallel is None else parallel}"
        marks, res = run_once(make_cfg(parallel=parallel), 2)
        assert len(marks) == 2 and len(res["steps"]) == 2
        assert_serial(marks, why)
        assert all(s["status"] == "разобран" for s in res["steps"]), res["steps"]


@test
def test_build_plan_inprocess_no_popen_per_card(tmp: Path):
    """T5: solve_source пишет карточки в-процессе — ноль Popen build_plan.py на карточку.

    Регрессия: каждая карточка и каждый --done поднимали subprocess build_plan.py (128–311 мс
    холодный запуск, N×M за прогон). In-process-путь (run_build_plan: те же build_card/
    mark_done под единым локом) обязан не оставить ни одного запуска с --card/--done за весь
    solve_source, а побочные эффекты те же: карточки в базе, отметка в манифесте.
    """
    from unittest.mock import patch

    sys.path.insert(0, str(KIT / "scripts"))
    from agent_core import parse_config
    from agent_runner import solve_source

    root = make_project(tmp)
    src_rel = "Sources/Confluence/Страница.md"
    src = root / src_rel
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        "# Страница\n\n"
        "## Первая тема\n\n" + "Текст первой темы. " * 20 +
        "\n\n## Вторая тема\n\n" + "Текст второй темы. " * 20 + "\n",
        encoding="utf-8")
    cfg = parse_config({'AURORA_AGENT_BACKEND_1_URL': 'http://test',
                        'AURORA_AGENT_BACKEND_1_MODEL': 'test'})

    def fake_call(cfg_, role, messages, **k):
        return {'ok': True,
                'text': '{"cards": [{"title": "Тема-два", "sections": "2", "to": "Concepts"},'
                        ' {"title": "Тема-одна", "sections": "1", "to": "Concepts"}]}',
                'backend': 1, 'model': 'm', 'log': []}

    sections = [(1, "Первая тема", 340, "превью"), (2, "Вторая тема", 340, "превью")]
    spawned = []

    class TracingPopen(subprocess.Popen):
        """Popen, записывающий запуски: оракул «были ли холодные subprocess»."""
        def __init__(self, args, *a, **k):
            spawned.append([str(x) for x in args])
            super().__init__(args, *a, **k)

    with patch('agent_runner.read_sections', return_value=sections), patch('subprocess.Popen', TracingPopen):
        step = solve_source(cfg, str(root), "Confluence", src_rel, True, False, call=fake_call)

    assert step["status"] == "разобран", f"solve_source сломался: {step}"
    bp = [a for a in spawned if "build_plan.py" in " ".join(a)
          and ("--card" in a or "--done" in a)]
    assert bp == [], f"холодные запуски build_plan.py на карточки: {bp}"

    made = sorted(p.name for p in (root / "AuroraKnowledgeDB" / "Concepts").glob("Тема-*.md"))
    assert made == ["Тема-два.md", "Тема-одна.md"], f"карточки не собраны: {made}"
    man = json.loads((root / "AuroraKnowledgeDB" / "meta" / "manifest.json")
                     .read_text(encoding="utf-8"))
    assert man["sources"][src_rel]["cards"] == 2, f"отметка не на 2 карточки: {man['sources']}"


@test
def test_semantic_search_prefilter_keeps_exact_top_n_and_ties(tmp: Path):
    """T7: предфильтр держит точный топ-N и не теряет ничью на границе выдачи.

    Связка консервативна: карточка, чья верхняя связь дотягивается до порога, обязана
    попасть в кандидаты, даже если нижняя связь поставила её за топ-N. Фикстура: шесть
    единичных векторов в R², две пары совпадают — ничья ровно на границе топ-3. Точные
    оценки известны (запрос лежит на оси X), а между ничейными код выбирает по имени —
    сверяемся с оракулом полного точного перебора.
    """
    import math

    root = make_project(tmp)
    names = ["Один-альфа", "Два-бета", "Три-гамма", "Три-гамма-дубль",
             "Пять-дельта", "Шесть-эпсилон"]
    vecs = [[math.cos(math.radians(a)), math.sin(math.radians(a))]
            for a in (0.0, 20.0, 40.0, 40.0, 80.0, 100.0)]
    qv = [1.0, 0.0]
    out = _write_embed_index(root, names, vecs, with_pf=True)

    # фикстура обязана содержать ничью на границе топ-3 — иначе тест ничего не доказывает
    s = [out[i * 2] for i in range(len(names))]      # qv = (1, 0): оценка — первая координата
    order = sorted(range(len(s)), key=lambda i: s[i], reverse=True)
    assert s[order[2]] == s[order[3]] and s[order[3]] > s[order[4]] + 0.1, f"фикстура сломана: ничья не на границе топ-3: {sorted(s, reverse=True)}"

    E, res = _embed_search(root, qv, 3)
    assert E.LAST_SEARCH["prefilter"] is True, "поиск обошёл предфильтр, хотя он в индексе"
    assert E.LAST_SEARCH["candidates"] < len(names), f"предфильтр не сузил: {E.LAST_SEARCH}"

    expected = sorted(((s[i], names[i]) for i in range(len(names))), reverse=True)[:3]
    expected = [(nm, round(sc, 4)) for sc, nm in expected]
    assert res == expected, f"предфильтр потерял точный топ-N: {res} вместо {expected}"
    assert res[2][0] == "Три-гамма-дубль", "ничья на границе решилась отбором предфильтра, а не точной оценкой"


@test
def test_semantic_search_prefilter_reduces_candidates_and_falls_back(tmp: Path):
    """T7: предфильтр реально сужает кандидатов; невозможный путь — честный полный перебор.

    На корпусе в 600 векторов точное скалярное считается по горсти кандидатов, а не по
    всей базе (живой запуск: 48 из 600, 3.23×). И когда предфильтр невозможен — осей в
    индексе нет или лимит больше числа карточек — поиск делает медленный, но точный
    полный перебор, а не ошибку.
    """
    import math

    root = make_project(tmp)
    n = 600
    names = [f"Карта-{i:03d}" for i in range(n)]
    # золотой угол: вектора равномерно размазаны по кругу, совпадающих направлений нет
    vecs = [[math.cos(math.radians((i * 137.50776405) % 360)),
             math.sin(math.radians((i * 137.50776405) % 360))] for i in range(n)]
    qv = [1.0, 0.0]
    limit = 10
    out = _write_embed_index(root, names, vecs, with_pf=True)

    s = [out[i * 2] for i in range(n)]
    expected = sorted(((s[i], names[i]) for i in range(n)), reverse=True)[:limit]
    expected = [(nm, round(sc, 4)) for sc, nm in expected]

    E, res = _embed_search(root, qv, limit)
    assert E.LAST_SEARCH["prefilter"] is True, "поиск обошёл предфильтр, хотя он в индексе"
    cands = E.LAST_SEARCH["candidates"]
    assert limit <= cands < n // 2, f"предфильтр не сузил кандидатов: {cands} из {n}"
    assert res == expected, "после сузчения результат разошёлся с полным точным перебором"

    # предфильтра в индексе нет (старый файл / осей не вышло) — полный перебор без ошибки
    _write_embed_index(root, names, vecs, with_pf=False)
    E, res = _embed_search(root, qv, limit)
    assert E.LAST_SEARCH["prefilter"] is False, "без предфильтра должен идти полный перебор"
    assert E.LAST_SEARCH["candidates"] == n, "полный перебор не посмотрел все карточки"
    assert res == expected, "откат на полный перебор потерял карточки"

    # лимит больше числа карточек: предфильтр бессмыслен, тоже полный перебор
    names6 = ["А", "Б", "В", "Г", "Д", "Е"]
    vecs6 = [[math.cos(math.radians(a)), math.sin(math.radians(a))]
             for a in (0.0, 20.0, 40.0, 80.0, 120.0, 150.0)]
    out6 = _write_embed_index(root, names6, vecs6, with_pf=True)
    s6 = [out6[i * 2] for i in range(len(names6))]
    exp6 = sorted(((s6[i], names6[i]) for i in range(len(names6))), reverse=True)
    exp6 = [(nm, round(sc, 4)) for sc, nm in exp6]
    E, res = _embed_search(root, qv, 60)
    assert E.LAST_SEARCH["prefilter"] is False and E.LAST_SEARCH["candidates"] == 6, f"лимит ≥ число карточек должен идти полным перебором: {E.LAST_SEARCH}"
    assert res == exp6, f"полный перебор по маленькой базе: {res} вместо {exp6}"


@test
def test_t6_critic_overlap_worker_with_next(tmp: Path):
    """T6: критик одного источника не держит воркера следующего.

    Регрессия: width=2, а очередь гонялась по одному — воркер i+1 стартал, когда
    критик i уже вернулся, и «параллелизм» был очередью исполнителя, а не обработкой.
    Проверка без настенного магического порога: второй воркер обязан стартовать, пока
    первый источник ещё обрабатывается (а заодно — до того, как его критик кончится).
    Сериальная обработка стартует второго на 0.2 с (воркер 0.1 + критик 0.1) позже,
    параллельная — на миллисекунды.
    """
    import threading
    from unittest.mock import patch

    sys.path.insert(0, str(KIT / "scripts"))
    from agent_core import parse_config
    from agent_runner import run_build

    root = make_project(tmp)
    cfg = parse_config({
        'AURORA_AGENT_BACKEND_1_URL': 'http://test',
        'AURORA_AGENT_BACKEND_1_MODEL': 'test',
        'AURORA_AGENT_BACKEND_1_WIDTH': '2',
        'AURORA_AGENT_PARALLEL': '2',
        'AURORA_AGENT_BUDGET_MIN': '20',
        'AURORA_AGENT_MAX_STEPS': '10',
        'AURORA_AGENT_REQUEST_TIMEOUT': '300',
    })

    marks, lock = {}, threading.Lock()

    def mock_solve(cfg_, *a, **k):
        src = a[2]
        with lock:
            marks[src] = {'w0': time.monotonic(), 'c0': None, 'c1': None}
        time.sleep(0.1)                      # воркер
        with lock:
            marks[src]['c0'] = time.monotonic()
        time.sleep(0.1)                      # критик
        with lock:
            marks[src]['c1'] = time.monotonic()
        return {'alias': 't', 'status': 'разобран', 'backends': [], 'degraded': False, 'note': ''}

    sources = [('Confluence', 'f1.md', 1), ('Confluence', 'f2.md', 1)]
    with patch('agent_runner.read_partition', return_value=sources), patch('agent_runner.solve_source', side_effect=mock_solve):
        res = run_build(cfg, str(root), False, True, 0)

    assert len(marks) == 2, f"обработаны не оба источника: {list(marks)}"
    first, second = sorted(marks, key=lambda s_: marks[s_]['w0'])
    f, s = marks[first], marks[second]
    assert s['w0'] < f['c0'], (
        f"второй воркер стартовал через {s['w0'] - f['w0']:.3f} с после первого — только "
        f"после того, как воркер первого кончился: источники гоняются по одному")
    assert s['w0'] < f['c1'], (
        f"второй воркер стартовал через {s['w0'] - f['w0']:.3f} с — критик первого "
        "источника догнал следующий: параллелизма нет")
    assert len(res["steps"]) == 2 and all(x["status"] == "разобран" for x in res["steps"]), f"run_build потерял источник: {res['steps']}"

@test
def test_aliases_batches_independent_conflicts_concurrently(tmp: Path):
    """T9: run_aliases при «одновременно» = 2 и пуле из 2 — два конфликта сразу.
    
    Регрессия: aliases был сериальным for-циклом и пул читали только build и distill.
    Проверяем перекрытие интервалов, а не настенное время: два заглушенных
    solve_conflict «думают» по 0.1 с, и сериальная обработка не может перекрыть эти
    интервалы, а пул на два потока — не может не перекрыть.
    """
    import threading
    from unittest.mock import patch
    
    sys.path.insert(0, str(KIT / "scripts"))
    from agent_core import parse_config
    from agent_runner import run_aliases
    
    root = make_project(tmp)
    cfg = parse_config({
        'AURORA_AGENT_BACKEND_1_URL': 'http://test',
        'AURORA_AGENT_BACKEND_1_MODEL': 'test',
        'AURORA_AGENT_BACKEND_1_WIDTH': '2',
        'AURORA_AGENT_PARALLEL': '2',
        'AURORA_AGENT_BUDGET_MIN': '20',
        'AURORA_AGENT_MAX_STEPS': '10',
        'AURORA_AGENT_REQUEST_TIMEOUT': '300',
    })
    
    marks, lock = {}, threading.Lock()
    
    def mock_solve(cfg_, *a, **k):
        alias = a[1]
        t0 = time.monotonic()
        with lock:
            marks[alias] = [t0, None, threading.get_ident()]
        time.sleep(0.1)
        with lock:
            marks[alias][1] = time.monotonic()
        return {'alias': alias, 'status': 'уточнил бы', 'backends': [], 'degraded': False, 'note': ''}
    
    conflicts = [('a', 'x'), ('b', 'y')]
    with patch('agent_runner.read_conflicts', return_value=conflicts), patch('agent_runner.solve_conflict', side_effect=mock_solve):
        res = run_aliases(cfg, str(root), False, True, 0)
    
    assert len(marks) == 2, f"обработаны не оба конфликта: {list(marks)}"
    (a0, a1, ta), (b0, b1, tb) = sorted(marks.values(), key=lambda p: p[0])
    assert ta != tb or (a0 < b1 and b0 < a1), (
        f"интервалы solve_conflict ({a0:.3f}–{a1:.3f}, {b0:.3f}–{b1:.3f}) не пересекаются "
        "и потоки один — обработка по очереди, а не пулом на два потока")
    assert len(res["steps"]) == 2 and all(s["status"] == "уточнил бы" for s in res["steps"]), f"run_aliases потерял конфликт: {res['steps']}"
    
    
@test
def test_aliases_serial_fallback_without_parallelism(tmp: Path):
    """T9: width без «одновременно» — 1: run_aliases гоняет solve_conflict в главном
    потоке строго один за другим, какой бы широкой ни была ширина шлюза.
    
    Детерминизм: сериальный путь не запускает исполнителя, поэтому интервалы
    не пересекаются и поток — главный.
    """
    import threading
    from unittest.mock import patch
    
    sys.path.insert(0, str(KIT / "scripts"))
    from agent_core import parse_config
    from agent_runner import run_aliases
    
    root = make_project(tmp)
    main_thread = threading.get_ident()
    
    def make_cfg(parallel=None):
        env = {'AURORA_AGENT_BACKEND_1_URL': 'http://test',
               'AURORA_AGENT_BACKEND_1_MODEL': 'test',
               'AURORA_AGENT_BACKEND_1_WIDTH': '2',
               'AURORA_AGENT_BUDGET_MIN': '20',
               'AURORA_AGENT_MAX_STEPS': '10',
               'AURORA_AGENT_REQUEST_TIMEOUT': '300'}
        if parallel is not None:
            env['AURORA_AGENT_PARALLEL'] = parallel
        return parse_config(env)
    
    def run_once(cfg_, n_conflicts):
        marks, lock = {}, threading.Lock()
        
        def mock_solve(cfg2, *a, **k):
            alias = a[1]
            t0 = time.monotonic()
            with lock:
                marks[alias] = [t0, None, threading.get_ident()]
            time.sleep(0.05)
            with lock:
                marks[alias][1] = time.monotonic()
            return {'alias': alias, 'status': 'уточнил бы', 'backends': [], 'degraded': False,
                    'note': ''}
        
        conflicts = [(f'c{i}', f'x{i}') for i in range(n_conflicts)]
        with patch('agent_runner.read_conflicts', return_value=conflicts), patch('agent_runner.solve_conflict', side_effect=mock_solve):
            res = run_aliases(cfg_, str(root), False, True, 0)
        return marks, res
    
    def assert_serial(marks, why):
        for k1 in marks:
            for k2 in marks:
                if k1 >= k2:
                    continue
                i1, i2 = marks[k1], marks[k2]
                assert i1[2] == main_thread and i2[2] == main_thread, f"{why}: solve_conflict ушёл в поток исполнителя — это параллельность"
                assert not (i1[0] < i2[1] and i2[0] < i1[1]), f"{why}: интервалы конфликтов пересекаются — сериальный режим стал параллельным"
    
    # «одновременно» незаданное / = 1: два конфликта строго один за другим, в главном потоке
    for parallel in (None, "1"):
        why = f"PARALLEL={'не задан' if parallel is None else parallel}"
        marks, res = run_once(make_cfg(parallel=parallel), 2)
        assert len(marks) == 2 and len(res["steps"]) == 2
        assert_serial(marks, why)
        assert all(s["status"] == "уточнил бы" for s in res["steps"]), res["steps"]
    
@test
def test_aliases_dependent_conflicts_stay_serial(tmp: Path):
    """T9: конфликты над общей карточкой — сериально, даже в параллельном отделе.

    Гонка: параллельный отдел скармливал все конфликты в пул сразу, и два конфликта
    над одной карточкой решались одновременно. А решение одного переписывает alias в
    базе и меняет картину для следующего — пара над общей карточкой обязана идти
    строго один за другим. Проверяем непересечение интервалов solve_conflict:
    два заглушенных шага "думают" по 0.1 с, сериальная обработка не может их
    перекрыть, а пул на два потока — почти наверняка может.
    """
    import threading
    from unittest.mock import patch

    sys.path.insert(0, str(KIT / "scripts"))
    from agent_core import parse_config
    from agent_runner import run_aliases

    root = make_project(tmp)
    cfg = parse_config({
        'AURORA_AGENT_BACKEND_1_URL': 'http://test',
        'AURORA_AGENT_BACKEND_1_MODEL': 'test',
        'AURORA_AGENT_BACKEND_1_WIDTH': '2',
        'AURORA_AGENT_PARALLEL': '2',
        'AURORA_AGENT_BUDGET_MIN': '20',
        'AURORA_AGENT_MAX_STEPS': '10',
        'AURORA_AGENT_REQUEST_TIMEOUT': '300',
    })

    def run_once(conflicts):
        marks, lock = {}, threading.Lock()

        def mock_solve(cfg_, *a, **k):
            alias = a[1]
            t0 = time.monotonic()
            with lock:
                marks[alias] = [t0, None]
            time.sleep(0.1)
            with lock:
                marks[alias][1] = time.monotonic()
            return {'alias': alias, 'status': 'уточнил бы', 'backends': [], 'degraded': False,
                    'note': ''}

        with patch('agent_runner.read_conflicts', return_value=conflicts), \
                patch('agent_runner.solve_conflict', side_effect=mock_solve):
            res = run_aliases(cfg, str(root), False, True, 0)
        return marks, res

    # два конфликта над общей карточкой «x» — строго один за другим
    marks, res = run_once([('a', 'x'), ('b', 'x')])
    assert len(marks) == 2, f"обработаны не оба конфликта: {list(marks)}"
    (a0, a1), (b0, b1) = marks['a'], marks['b']
    assert not (a0 < b1 and b0 < a1), (
        f"конфликты над общей карточкой «x» шли параллельно: интервалы {a0:.3f}–{a1:.3f} и "
        f"{b0:.3f}–{b1:.3f} пересекаются, а решение одного меняет картину для следующего")
    assert len(res["steps"]) == 2 and all(s["status"] == "уточнил бы" for s in res["steps"]), \
        f"run_aliases потерял конфликт: {res['steps']}"

    # граница: одиночный конфликт в параллельном режиме — один шаг, без падения
    marks, res = run_once([('a', 'x')])
    assert len(marks) == 1, f"одиночный конфликт: {list(marks)}"
    assert len(res["steps"]) == 1 and res["steps"][0]["status"] == "уточнил бы", \
        f"одиночный конфликт сломал прогон: {res['steps']}"


@test
def test_aliases_mixed_groups_parallel_across_serial_within(tmp: Path):
    """T9: группы конфликтов по карточкам: внутри группы — сериально, между — параллельно.

    a и b над карточкой «x» и c над «y»: a с b обязаны идти один за другим (общая
    карточка), а c — не конфликтует с группой и обязан успеть стартовать, пока группа
    ещё работает: c стартует ДО конца объединённого интервала a+b. Пул на два потока:
    группа и одиночка уходят в разные воркеры.
    """
    import threading
    from unittest.mock import patch

    sys.path.insert(0, str(KIT / "scripts"))
    from agent_core import parse_config
    from agent_runner import run_aliases

    root = make_project(tmp)
    cfg = parse_config({
        'AURORA_AGENT_BACKEND_1_URL': 'http://test',
        'AURORA_AGENT_BACKEND_1_MODEL': 'test',
        'AURORA_AGENT_BACKEND_1_WIDTH': '2',
        'AURORA_AGENT_PARALLEL': '2',
        'AURORA_AGENT_BUDGET_MIN': '20',
        'AURORA_AGENT_MAX_STEPS': '10',
        'AURORA_AGENT_REQUEST_TIMEOUT': '300',
    })

    marks, lock = {}, threading.Lock()

    def mock_solve(cfg_, *a, **k):
        alias = a[1]
        t0 = time.monotonic()
        with lock:
            marks[alias] = [t0, None]
        time.sleep(0.1)
        with lock:
            marks[alias][1] = time.monotonic()
        return {'alias': alias, 'status': 'уточнил бы', 'backends': [], 'degraded': False,
                'note': ''}

    conflicts = [('a', 'x'), ('b', 'x'), ('c', 'y')]
    with patch('agent_runner.read_conflicts', return_value=conflicts), \
            patch('agent_runner.solve_conflict', side_effect=mock_solve):
        res = run_aliases(cfg, str(root), False, True, 0)

    assert len(marks) == 3, f"обработаны не все три конфликта: {list(marks)}"
    (a0, a1), (b0, b1), (c0, c1) = marks['a'], marks['b'], marks['c']
    assert not (a0 < b1 and b0 < a1), (
        f"a и b над общей карточкой «x» шли параллельно: интервалы {a0:.3f}–{a1:.3f} и "
        f"{b0:.3f}–{b1:.3f} пересекаются, а внутри группы — строго по одному")
    group_end = max(a1, b1)
    assert c0 < group_end, (
        f"c (карточка «y», с группой не конфликтует) стартовал в {c0:.3f} — уже после "
        f"конца группы {group_end:.3f}: независимые группы гоняются по очереди")
    assert len(res["steps"]) == 3 and all(s["status"] == "уточнил бы" for s in res["steps"]), \
        f"run_aliases потерял конфликт: {res['steps']}"
@test
def test_embed_prefilter_scale(tmp: Path):
    """T7: предфильтр сохраняет точный топ-N на масштабном наборе (1000–1500 векторов).

    Проверяем, что при большом количестве векторов (1000 шт. × 768 измерений)
    предфильтр не теряет лучшие совпадения. Оракул (полный перебор) и ассистент
    должны давать идентичный результат.
    """
    import random
    import math
    n = 1000  # число векторов (нижняя граница масштаба 1000-1500)
    dim = 768  # размерность (стандарт для bge-m3)
    limit = 10  # размер выдачи топ-N

    # Создаём детерминированный вектор запроса
    rng = random.Random(42)

    # Генерируем вектор запроса: случайные числа → нормализуем
    raw_qvec = [rng.gauss(0, 1) for _ in range(dim)]
    q_vec_norm = math.sqrt(sum(x**2 for x in raw_qvec))
    qv = [x / q_vec_norm for x in raw_qvec]

    root = make_project(tmp)
    names = [f"Карта-{i:04d}" for i in range(n)]
   
    # Остальные измерения — малый шум
    # Это создаёт сильную вариативность, которую предфильтр может отсечь
    golden_angle = 137.50776405
    vecs = []
    for i in range(n):
        angle_rad = math.radians((i * golden_angle) % 360)
        # Сильная компонентная на 2D плоскости для работы предфильтра
        main_strength = 1.0
        noise_strength = 0.01
        v = [0.0] * dim
        v[0] = main_strength * math.cos(angle_rad)
        v[1] = main_strength * math.sin(angle_rad)
        for j in range(2, dim):
            v[j] = noise_strength * (i % 17 - 8) / 8  # детерминированный шум
        vecs.append(v)
    # Записываем индекс с предфильтром
    out = _write_embed_index(root, names, vecs, with_pf=True)

    # Измеряем время работы (без assert ограничения)
    t0 = time.monotonic()
    E, res = _embed_search(root, qv, limit)
    elapsed = time.monotonic() - t0
    print(f"[scale] n={n} dim={dim} search={elapsed:.3f}s")

    # Оракул: полный перебор по сохраненным векторам (float32)
    scores = []
    for i in range(n):
        dot = sum(qv[j] * float(out[i * dim + j]) for j in range(dim))
        scores.append((dot, names[i]))
    scores_sorted = sorted(scores, key=lambda x: x[0], reverse=True)
    expected = [(name, round(score, 4)) for score, name in scores_sorted[:limit]]

    # Проверка: предфильтр включен
    assert E.LAST_SEARCH["prefilter"] is True, "поиск обошёл предфильтр, хотя он в индексе"

    # Проверка: предфильтр действительно сузил пространство
    cands = E.LAST_SEARCH.get("candidates", n)
    assert 10 <= cands < n, f"предфильтр не сузил кандидатов: {cands} из {n}"

    # Жёсткая проверка: точное совпадение с оракулом
    assert res == expected, f"предфильтр потерял точный топ-N: {res}\nvs\n{expected}"

    # Проверка на вырожденность фикстуры: топ-N не должен иметь ничью на границе
    # (иначе результат стал бы недетерминированным из-за произвольной сортировки)
    kscore = scores_sorted[limit - 1][0]
    near_k = sum(1 for s in scores_sorted if abs(s[0] - kscore) < 1e-9)
    assert near_k <= 1, f"фикстура сломана: {near_k} векторов с одинаковой оценкой на границе топ-{limit}"


@test
def test_a_slow_backend_is_not_a_dead_one(tmp: Path):
    """Молчание по сроку — не смерть, и того, кто только что ответил, оно не хоронит.

    С живого контура: человек выбрал бэкенд №2, тот написал ответ, а следом Момус на
    ТОЙ ЖЕ модели не уложился в срок — и в отчёте появилось «ни один бэкенд не ответил
    осмысленно». Модель при этом работала: её ответ человек читал на экране. Пятнадцать
    минут карантина следом отняли бы её и у остальных вопросов.

    Различие простое: сервер, ответивший недавно, жив и просто думает дольше отпущенного.
    Это лечится сроком (`AURORA_AGENT_REQUEST_TIMEOUT`), а не ожиданием.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")
    importlib.reload(A)

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a",
                          "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_REQUEST_TIMEOUT": "300"})
    answer = {"choices": [{"message": {"content": "ответ"}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    state = {"first": True}

    def flaky(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        if state["first"]:
            state["first"] = False
            return (200, answer, "", 0.5)
        return (None, None, "TimeoutError: timed out", timeout)

    A.DOWN.clear(); A.LAST_OK.clear()
    ok = A.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                     transport=flaky, deadline=time.time() + 5, sleep=lambda s: None)
    assert ok["ok"], ok["log"]
    assert A.LAST_OK.get(1), "успешный ответ не отмечен — судить о свежести будет нечем"

    # Срок запроса — явно и короткий. Пауза между кругами здесь пустышка, а срок вызова идёт
    # по настоящим часам и продлевается на долю запасного (`FAIR_SHARE` от срока запроса):
    # со сроком по умолчанию тест крутил кольцо вхолостую шесть минут, а с 1.122.0
    # (срок без замера скорости — потолок) крутил бы двенадцать.
    slow = A.call_role(cfg, "qa", [{"role": "user", "content": "проверь"}],
                       transport=flaky, deadline=time.time() + 3, sleep=lambda s: None,
                       request_timeout=2)
    assert not slow["ok"], "таймаут принят за ответ"
    assert 1 not in A.DOWN, (
        "бэкенд, ответивший секунду назад, посажен в карантин на 15 минут из-за одного "
        "таймаута — следующий вопрос уйдёт мимо живой модели")
    assert slow.get("timed_out"), "вызов не отличил молчание по сроку от отказа"
    joined = " ".join(slow["log"])
    assert "не уложился" in joined, f"таймаут назван чужим именем: {slow['log']}"
    assert "сроком запроса" in joined and "«Модели»" in joined, \
        f"человеку не назван рычаг — он пойдёт чинить связь: {slow['log']}"
    assert "ни один бэкенд не ответил осмысленно" not in joined, \
        "итог по-прежнему читается как «серверов нет»"

    # Тот, от кого давно не было ответа, в карантин садится как раньше.
    A.DOWN.clear(); A.LAST_OK.clear()
    dead = A.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                       transport=lambda k, b, pl, to: (404, None, "нет /slots", 0.0)
                       if k == "slots" else (None, None, "TimeoutError: timed out", to),
                       deadline=time.time() + 3, sleep=lambda s: None)
    assert not dead["ok"] and 1 in A.DOWN, \
        "молчащего с самого начала перестали сажать в карантин — его будут спрашивать вечно"


@test
def test_the_check_gets_as_long_as_the_answer_took(tmp: Path):
    """Момусу отпущено по цене ответа, а не по одной цифре из настройки.

    Он читает тот же пак плюс сам ответ — работа того же порядка, промпт даже больше.
    Модель, которой ответ дался за пять минут, в пять минут проверки не уложится никогда,
    и предел на запрос это не обойти: дедлайна мало, запрос режется по `request_timeout`.
    Поэтому проверка получает свой предел — щедрее, но с потолком: человек ждёт.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    cfg = {"request_timeout": 300, "thinking": False}
    assert R.momus_timeout(cfg, 0) == 300, "без замера ответа берём настройку как была"
    # Момус с рассуждениями — от срока рассуждающего вызова (1.118.0): до 24 тыс. токенов
    # проверки на PRJ-A, больше 300 с под нагрузкой шлюза.
    A = importlib.import_module("agent_core")
    saved = dict(A.USAGE)
    try:
        A.USAGE.update(tokens_out=0, gen_seconds=0.0)
        assert R.momus_timeout({"request_timeout": 300}, 0) == 1200, \
            "рассуждающему Момусу дан срок без рассуждений — проверка оборвётся на середине"
    finally:
        A.USAGE.clear(); A.USAGE.update(saved)
    assert R.momus_timeout(cfg, 40) == 300, "быстрый ответ не должен УРЕЗАТЬ проверку"
    assert R.momus_timeout(cfg, 280) == 420, "медленный ответ не поднял предел проверки"
    assert R.momus_timeout(cfg, 5000) == 600, "потолок не держит: человек ждёт ответа"

    # и этот предел действительно доезжает до вызова
    seen = {}

    def fake_call(cfg, role, messages, **kw):
        seen.update(kw); seen["role"] = role
        return {"ok": False, "log": ["№2 m: не уложился в 420 с"], "timed_out": True}

    mo = R.run_momus(cfg, "пак", "вопрос", "ответ", fake_call, answered_in=280)
    assert seen["role"] == "qa", seen
    assert seen.get("request_timeout") == 420, \
        f"предел на запрос не передан — вызов снова обрежется по настройке: {seen}"
    assert seen["deadline"] > time.time() + 400, "дедлайн остался коротким"
    assert mo["timed_out"] and mo["given"] == 420, mo

    # отчёт называет причину сроком, а не недоступностью
    text = R.report_ask({"ok": True, "answer": "текст", "cards": ["К"], "total": 1,
                         "model": "m", "backend": 2, "seconds": 534.3, "momus": mo},
                        "вопрос", cfg)
    assert "не успел" in text and "420" in text, text
    assert "срок запроса" in text and "«Модели»" in text, "человеку не назван рычаг"
    assert "не проверил ответ" not in text, \
        "медленную проверку по-прежнему объявляют несостоявшейся без причины"


@test
def test_probe_asks_the_same_settings_and_every_role_model(tmp: Path):
    """Проверка связи читает настройку движком и спрашивает каждую модель кольца.

    Две ошибки, обе с живого контура и обе — «своя копия вместо общего кода».

    Копия чтения `.env` смотрела только в папку проекта, а движок складывает настройку
    слоями (кит < проект < окружение) и бэкенды держит в файле **кита**. Проверка
    печатала «бэкенды не настроены» там, где движок видел три.

    И спрашивала она одну модель на шлюз, хотя у ролей они разные: отчёт называл
    `deepseek-v4-flash` (роль qa), а проверка ходила к модели работника и говорила
    «жив». «Шлюз доступен» без имени модели не значит ничего.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")
    P = importlib.import_module("agent_probe")
    importlib.reload(P)

    src = (KIT / "scripts/agent_probe.py").read_text(encoding="utf-8")
    assert "AG.config()" in src, \
        "проверка снова читает настройку сама — разойдётся с движком"
    assert "def read_env" not in src, "осталась своя копия чтения .env"

    env = {"AURORA_AGENT_BACKEND_1_URL": "http://one/v1",
           "AURORA_AGENT_BACKEND_1_KEY": "k",
           "AURORA_AGENT_BACKEND_1_MODEL_WORKER": "рабочая",
           "AURORA_AGENT_BACKEND_1_MODEL_QA": "судья",
           "AURORA_AGENT_BACKEND_2_URL": "http://two/v1",
           "AURORA_AGENT_BACKEND_2_MODEL": "общая"}
    old_raw, A.raw_config = A.raw_config, lambda: env
    try:
        rows = P.backends()
    finally:
        A.raw_config = old_raw

    got = [(r["n"], r["model"]) for r in rows]
    assert (1, "рабочая") in got and (1, "судья") in got, \
        f"спрошены не все модели шлюза — падение одной останется невидимым: {got}"
    assert (2, "общая") in got, f"шлюз с одной моделью на всё потерян: {got}"
    assert len([r for r in rows if r["n"] == 1]) == 2, \
        f"модели шлюза №1 не разделены по строкам: {got}"
    qa = next(r for r in rows if r["model"] == "судья")
    assert "qa" in qa["roles"], qa
    assert qa["key"] == "k" and qa["url"] == "http://one/v1", qa

    # пустое имя модели в запрос не уходит: шлюз ответит «Missing model field», и живой
    # контур будет объявлен сломанным — так и случилось до этой правки
    assert all(r["model"] for r in rows), f"в проверку ушло пустое имя модели: {rows}"


@test
def test_a_placeholder_is_not_an_answer(tmp: Path):
    """Пустышка выведена из выдачи одним правилом и видна отдельной картой.

    Карточка, заведённая под ссылку, знания не несёт. Раньше её признаком была метка в
    тегах, а читали метку пять скриптов пятью разными выражениями — и каждое новое место
    про пустышки забывало. Они уходили в семантический индекс и всплывали в поиске как
    термины, у которых есть определение, оттесняя карточки, где определение написано.
    На живом проекте таких имён тысячи.

    Признак теперь один — `status: placeholder`, и решает его одна функция.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    AC = importlib.import_module("aurora_common")
    E = importlib.import_module("kb_embed")
    importlib.reload(E)

    assert AC.PLACEHOLDER in AC.STATUSES, "статус не объявлен — линтер сочтёт его чужим"
    assert AC.is_placeholder({"status": "placeholder"}, ""), "статус не читается"
    assert AC.is_placeholder({"tags": "[заготовка]"}, ""), \
        "старая метка перестала читаться — базы прошлых версий ослепнут"
    assert AC.is_placeholder({}, "_Заготовка: знания пока нет._"), "старое тело не читается"
    assert not AC.is_placeholder({"status": "knowledge"}, "Определение написано."), \
        "знание принято за пустышку"

    root = make_project(tmp)
    card(root, "Concepts/Полная.md", status="knowledge",
         body="Профиль обслуживания — набор параметров, определяющий доступные услуги.")
    card(root, "Concepts/Пустая.md", status="placeholder", tags="[заготовка]",
         body="_Заготовка: ссылка на это понятие уже есть, знания пока нет._")

    # из семантического индекса пустышка не попадает вовсе
    cwd = os.getcwd()
    try:
        os.chdir(root)
        texts = E.card_texts()
    finally:
        os.chdir(cwd)
    assert "Полная" in texts and "Пустая" not in texts, \
        f"пустышка ушла в индекс и будет всплывать в поиске: {sorted(texts)}"

    # и получает свою карту, отдельную от тематических
    run("kb_moc.py", "--apply", "--allow-dirty", cwd=root)
    holes = root / "AuroraKnowledgeDB" / "MOC" / "Пустышки.md"
    assert holes.is_file(), "нет карты пустышек — увидеть их разом негде"
    body = holes.read_text(encoding="utf-8")
    assert "Пустая" in body and "Полная" not in body, body
    for other in (root / "AuroraKnowledgeDB" / "MOC").glob("*.md"):
        if other.name != "Пустышки.md":
            assert "[[Пустая" not in other.read_text(encoding="utf-8"), \
                f"пустышка попала в тематическую карту {other.name}"


@test
def test_a_filled_placeholder_stops_being_one(tmp: Path):
    """Появилось определение — отметка снимается, карточка возвращается в выдачу.

    Определение приходит тремя путями: его пишет человек, приносит `agent:distill` из
    источника или добавляет разбор. Ни один не обязан помнить про статус. Не снимай
    отметку по самому тексту — карточка с готовым определением осталась бы вне поиска, и
    база молчала бы о том, что в ней написано.
    """
    root = make_project(tmp)
    card(root, "Concepts/Наполненная.md", status="placeholder", tags="[заготовка]",
         body="Профиль обслуживания абонента — набор параметров, определяющий доступные "
              "абоненту услуги и порядок их тарификации. Назначается при заключении "
              "договора, меняется заявкой.\n\n## Упоминается в\n\n- [[Расчёт]]")
    card(root, "Concepts/Так-и-пустая.md", status="placeholder", tags="[заготовка]",
         body="_Заготовка: ссылка на это понятие уже есть, знания пока нет._\n\n"
              "## Упоминается в\n\n- [[Расчёт]]")

    run("kb_fix.py", "--frontmatter", "--apply", "--allow-dirty", cwd=root)
    filled = (root / "AuroraKnowledgeDB/Concepts/Наполненная.md").read_text(encoding="utf-8")
    empty = (root / "AuroraKnowledgeDB/Concepts/Так-и-пустая.md").read_text(encoding="utf-8")
    assert "status: draft" in filled, \
        f"наполненная карточка осталась пустышкой и вне поиска:\n{filled[:300]}"
    assert "status: placeholder" in empty, \
        "с настоящей пустышки сняли отметку — она вернётся в выдачу пустой"


@test
def test_a_retold_placeholder_stays_one_and_a_lost_mark_returns(tmp: Path):
    """Пересказ пустоты — не знание: отметка не снимается, а потерянная возвращается.

    Живой проект, 1.104.0. `agent:distill` писал пустышке «тезис» — пересказывал её
    служебные строки: «Карточка упоминается в [[X]].», «Раздел источника: «Упоминается в».»
    Правило наполнения отсекало `- [[X]]`, но не пересказ, и четыре таких строки перевешивали
    порог: 62 пустышки за один прогон ремонта ушли в поиск как знание.

    Обратный ремонт: карточка без источника, где кроме слов о пустоте ничего нет, получает
    отметку назад. Короткое настоящее определение и карточка с источником остаются знанием.
    Вынесенная карточка, заведённая с `sources: []`, получает источники донора.
    """
    root = make_project(tmp)
    retold = ("{name} — заготовка: ссылка на это понятие уже есть, знаний пока нет.\n"
              "В источнике есть: «Наполните её при следующем разборе источника — ссылки "
              "переписывать не придётся».\n"
              "Раздел источника: «Упоминается в».\n"
              "Карточка упоминается в [[Карточка-заказа.-Основные-элементы]].\n"
              "Карточка упоминается в [[Карточка-заказа.-Блок-Доставка]].\n"
              "Карточка упоминается в [[Карточка-заказа.-Поле-номер-версии]].\n"
              "Карточка упоминается в [[ER-Sta-Accepted]].")
    card(root, "Concepts/Пересказанная.md", status="placeholder", tags="[заготовка]",
         kind="knowledge", body=retold.format(name="Пересказанная"))
    card(root, "Concepts/Потерявшая.md", status="draft", tags="[]", kind="knowledge",
         body=retold.format(name="Потерявшая"))
    card(root, "Concepts/ТК.md", status="draft", kind="knowledge",
         body="ТК — zip-архив, содержащий вложенные документы.")
    card(root, "Concepts/С-источником.md", status="draft", kind="knowledge",
         sources='\n  - "Sources/Confluence/Стр.md"', body=retold.format(name="С-источником"))
    card(root, "Concepts/Донор.md", status="draft", kind="knowledge",
         sources='\n  - "Sources/Confluence/Алгоритм.md"',
         body="Донор — алгоритм расчёта, из которого вынесено определение [[Вынесенная]].")
    card(root, "Concepts/Вынесенная.md", status="draft", kind="knowledge", sources="[]",
         body="Вынесенная — единственная версия с номером регистрации.\n\n"
              "_Перенесено из [[Донор]]._")
    # Формулировки служебной речи, снятые с живого проекта: фильтр по фразам пропускал их все.
    card(root, "Concepts/Потерявшая-2.md", status="draft", tags="[]", kind="knowledge",
         body="Её нужно заполнить при следующем разборе источника, и ссылки переписывать "
              "не придётся.\nПонятие упоминается в [[Первая-карточка]].\n"
              "Ссылка на это понятие уже есть.\nНазвано в карточках:\n"
              "- Отправка-начислений-и-сторно-по-счёту-в-учётную-систему\n"
              "[[Вторая-карточка]]\nРасшифровки база пока не знает.\n"
              "Знания о предмете пока нет.")
    # «Упоминается в» внутри знания — знание: в строке есть предмет.
    card(root, "Concepts/ГОСТ-термин.md", status="placeholder", tags="[заготовка]",
         kind="knowledge",
         body="Термин упоминается в ГОСТ Р 1.2-2016 и означает порядок пересчёта сумм по "
              "курсу валюты договора на дату регистрации документа в системе.")
    # Пустышка, наполненная выносом: одна фраза короче порога, но это знание.
    card(root, "Concepts/Термин-из-выноса.md", status="placeholder", tags="[заготовка]",
         kind="knowledge",
         body="Термин-из-выноса — zip-архив с документами.\n\n_Перенесено из [[Донор]]._")
    # Пустышка говорит о себе своим именем и называет соседей без скобок ссылки.
    card(root, "Concepts/ER Acc ID.md", status="draft", tags="[]", kind="knowledge",
         body="ER Acc ID — понятие, на которое уже есть ссылка, но о котором пока нет знаний.\n"
              "ER Acc ID упоминается в ALG-3.18_Получение_остатка_по_клиентам_из_учётной_системы.\n"
              "Ссылка на это понятие уже существует.\n"
              "Следующее наполнение предусмотрено при следующем разборе источника.\n"
              "Далее в перечне: Проверка-установки-криптопровайдера.\n"
              "Упоминается в SPR-031 ([[SPR-031-Справочник-единиц]]).")

    run("kb_fix.py", "--frontmatter", "--apply", "--allow-dirty", cwd=root)
    kb = root / "AuroraKnowledgeDB/Concepts"
    read = lambda n: (kb / f"{n}.md").read_text(encoding="utf-8")  # noqa: E731

    assert "status: placeholder" in read("Пересказанная"), \
        f"пересказ пустоты принят за знание — пустышка ушла в поиск:\n{read('Пересказанная')[:400]}"
    assert "status: placeholder" in read("Потерявшая"), \
        f"пустышке, потерявшей отметку, она не вернулась:\n{read('Потерявшая')[:400]}"
    assert "status: draft" in read("ТК"), "короткое настоящее определение объявлено пустышкой"
    assert "status: draft" in read("С-источником"), \
        "карточку с источником объявили пустышкой — знание из документа ушло из поиска"
    assert card_srcs(read("Вынесенная")) == ["Sources/Confluence/Алгоритм.md"], \
        f"вынесенной карточке не вернулся источник донора: {card_srcs(read('Вынесенная'))}"
    body_before = read("Вынесенная").split("\n---", 1)[1]
    assert "единственная версия с номером регистрации" in body_before, \
        "возврат источника тронул тело карточки"
    assert "status: placeholder" in read("Потерявшая-2"), \
        f"служебная речь другими словами принята за знание:\n{read('Потерявшая-2')[:500]}"
    assert "status: draft" in read("ГОСТ-термин"), \
        "знание со словами «упоминается в» объявлено служебной речью — оно ушло из поиска"
    moved = read("Термин-из-выноса")
    assert "status: draft" in moved and "tags: [заготовка]" not in moved, \
        f"пустышка, наполненная выносом, осталась пустышкой:\n{moved[:300]}"
    assert card_srcs(moved) == ["Sources/Confluence/Алгоритм.md"], \
        f"наполненной выносом карточке не вернулся источник донора: {card_srcs(moved)}"
    assert "status: placeholder" in read("ER Acc ID"), \
        f"своё имя и неоформленные ссылки приняты за знание:\n{read('ER Acc ID')[:500]}"


@test
def test_the_panel_recognises_the_ratchet_by_its_escape(_t):
    """Панель узнаёт храповик по названию его обхода, а не по тексту отказа.

    Текст менялся: «плотность ошибок» → «в том, что вы коммитите, ошибок N — они ваши».
    Привязка к тексту отвалилась молча, и панель переставала предлагать «зафиксировать
    всё равно» ровно тогда, когда это нужно, — показывая человеку сырой вывод хука.

    `AURORA_SKIP_RATCHET` хук называет там и только там, где отказ можно снять: отказ по
    внутренним названиям снимается иначе и такой кнопки не заслуживает.
    """
    ck = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"ratchet": "AURORA_SKIP_RATCHET" in tail' in ck, \
        "панель узнаёт храповик по тексту сообщения — он уже менялся однажды"

    hook = (KIT / "scripts/aurora_hooks.py").read_text(encoding="utf-8")
    refuse = hook[hook.index("в том, что вы коммитите"):]
    assert "AURORA_SKIP_RATCHET" in refuse[:900], \
        "хук не называет обход в тексте отказа — панели не по чему его узнать"
    terms = hook[hook.index("внутренние названия"):] if "внутренние названия" in hook else ""
    if terms:
        assert "AURORA_SKIP_RATCHET" not in terms[:600], \
            "отказ по внутренним названиям выдаёт себя за храповик — его снимать нельзя"


@test
def test_the_bridge_updates_every_lagging_project_at_once(tmp: Path):
    """Мостик обновляет движок во всех отставших проектах одной кнопкой.

    Проектов на машине десятки. Отставший движок ломает маршрут на середине, объявив
    предыдущие шаги успешными, — но пока обновление стоит десяти кликов на проект, оно
    не делается вовсе, и проекты копят отставание годами.

    Сначала предпросмотр: человек видит поимённо, что тронется. Обновление переписывает
    движок в чужих папках — молча такое не делают.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = tmp / "машина"
    root.mkdir()
    made = {}
    for name, ver in (("старый", "1.0.0"), ("свежий", ck.kit_version())):
        here = root / name
        here.mkdir()
        d = make_project(here)          # заводит `<here>/project` со структурой базы
        made[name] = d
        (d / "aurora.config.yaml").write_text(f"project:\n  name: {name}\n",
                                              encoding="utf-8")
        (d / "AuroraKnowledgeDB/meta").mkdir(parents=True, exist_ok=True)
        (d / "AuroraKnowledgeDB/meta/aurora_version.txt").write_text(ver, encoding="utf-8")
    stale, fresh = made["старый"], made["свежий"]

    dry = ck.update_all_projects([str(root)], False)
    assert "error" not in dry, dry
    names = [r["path"] for r in dry["projects"]]
    assert any(str(stale) in n for n in names), \
        f"отставший проект не попал в обновление: {names}"
    assert not any(str(fresh) in n for n in names), \
        "проект на текущей версии тронут без нужды"
    assert dry["apply"] is False, "предпросмотр объявлен применением"
    assert (stale / "AuroraKnowledgeDB/meta/aurora_version.txt").read_text(
        encoding="utf-8").strip() == "1.0.0", "предпросмотр записал версию"

    done = ck.update_all_projects([str(root)], True)
    assert done["updated"] >= 1 and done["failed"] == 0, done
    assert (stale / "AuroraKnowledgeDB/meta/aurora_version.txt").read_text(
        encoding="utf-8").strip() == ck.kit_version(), "версия не проставлена"

    # и кнопка на Мостике действительно ведёт сюда, а не в раздел «Версия»
    ui = panel_sources()
    assert "async function updateAllProjects()" in ui, "на Мостике нет обработчика"
    assert "/api/update-all" in ui, "кнопка не зовёт ручку массового обновления"
    assert "Нажмите, чтобы обновить все" in ui, \
        "карточка не говорит, что она нажимается — человек не догадается"
    assert "confirm(" in ui[ui.index("async function updateAllProjects()"):
                            ui.index("function metric(")], \
        "обновление чужих папок без подтверждения"


@test
def test_updating_the_engine_refreshes_the_git_hook(tmp: Path):
    """Обновление движка переставляет git-хук, если он наш.

    Хук — установленная КОПИЯ в `.git/hooks/`, а не файл движка. Обновление везло новый
    `aurora_hooks.py` и оставляло в проекте хук, поставленный при заведении. На живом
    проекте так и вышло: храповик, переделанный в ките месяцы назад (абсолютный счёт →
    плотность, отказ → предупреждение), не доехал ни разу — человек воевал с поведением,
    которого в ките уже нет, и обходил хук `--no-verify` на каждом коммите.

    Чужой хук не трогаем: его ставил не движок, и молча перезаписать значит потерять
    чужую работу.
    """
    root = make_project(tmp, git=True)
    run("aurora_hooks.py", "--install", "--mode", "ratchet", cwd=root)
    hook = root / ".git" / "hooks" / "pre-commit"
    assert hook.is_file(), "хук не поставился — проверять нечего"

    # состарим хук: подменим тело на «прежнюю версию»
    old_text = hook.read_text(encoding="utf-8")
    hook.write_text(old_text.replace("плотность ошибок", "СТАРЫЙ_МАРКЕР"), encoding="utf-8")
    assert "плотность ошибок" not in hook.read_text(encoding="utf-8")

    cp = subprocess.run([sys.executable, str(SCRIPTS / "aurora_update.py"), str(root),
                         "--apply"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    fresh = hook.read_text(encoding="utf-8")
    assert "плотность ошибок" in fresh, \
        "хук не переставлен — исправления в нём не доедут до проектов никогда"
    assert "режим: ratchet" in fresh, "режим не сохранён: его выбирал человек"

    # чужой хук остаётся чужим
    hook.write_text("#!/bin/sh\n# мой собственный хук\nexit 0\n", encoding="utf-8")
    subprocess.run([sys.executable, str(SCRIPTS / "aurora_update.py"), str(root),
                    "--apply"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "мой собственный хук" in hook.read_text(encoding="utf-8"), \
        "чужой хук перезаписан молча — потеряна работа человека"


ATTACH_PAGE = (
    '<p>Текст до.</p>'
    '<ac:structured-macro ac:name="view-file"><ac:parameter ac:name="name">'
    '<ri:attachment ri:filename="law-1.pdf"/></ac:parameter></ac:structured-macro>'
    '<ac:structured-macro ac:name="viewpdf"><ac:parameter ac:name="name">'
    '<ri:attachment ri:filename="Приложение 2.pdf"/></ac:parameter></ac:structured-macro>'
    '<ac:structured-macro ac:name="view-file"><ac:parameter ac:name="name">'
    '<ri:attachment ri:filename="foreign.pdf"><ri:page ri:content-title="Другая"/>'
    '</ri:attachment></ac:parameter></ac:structured-macro>'
    '<p><ac:image><ri:attachment ri:filename="image-2026-6-3_10-39-19-1.png"/></ac:image></p>'
    '<p><ac:image><ri:url ri:value="https://img.example.com/pic_1.png"/></ac:image></p>'
    '<p>Файл: <ac:link><ri:attachment ri:filename="report.docx"/>'
    '<ac:plain-text-link-body><![CDATA[Отчёт]]></ac:plain-text-link-body></ac:link></p>'
    '<p><ac:link><ri:attachment ri:filename="setup.exe"/></ac:link> '
    '<ac:link><ri:attachment ri:filename="run.bat"/></ac:link> '
    '<ac:link><ri:attachment ri:filename="gone.pdf"/></ac:link></p>')


def _attach_exporter(tmp: Path, fetch=None, attachments=None, root=None, page=None,
                     attach_error=None, force=True, sizes=None, limits=None, **policy):
    """Экспортёр одной страницы с подставным API (без сети) → (CE, exp, root, fetched)."""
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    CE = importlib.import_module("confluence_export")
    importlib.reload(CE)
    root = root or make_project(tmp)
    fetched = []
    have = attachments if attachments is not None else {
        "law-1.pdf": "/d/law", "Приложение 2.pdf": "/d/app2", "report.docx": "/d/rep",
        "image-2026-6-3_10-39-19-1.png": "/d/img", "setup.exe": "/d/exe", "run.bat": "/d/bat"}

    class Api:
        def page(self, pid):
            return {"title": "Руководство", "version": {"number": 1, "when": "2026-10-16"},
                    "body": {"storage": {"value": page or ATTACH_PAGE}}, "space": {"key": "S"},
                    "_links": {"webui": f"/p/{pid}"}}

        def children(self, pid):
            return []

        def attachments(self, pid):
            if attach_error:
                raise attach_error
            return dict(have)

        def fetch(self, url, max_bytes=None):
            fetched.append(url)
            if limits is not None:
                limits.append(max_bytes)
            if fetch:
                return fetch(url)
            blob = f"bytes of {url}".encode("utf-8")
            if max_bytes is not None and len(blob) > max_bytes:
                raise CE.FileTooLarge("файл больше предела")
            return blob

        def user_name(self, key):
            return key

    api = Api()
    if sizes is not None:       # {имя: (размер или None, тип)}: ответ со свойствами вложений
        api.attachment_info = lambda pid: {
            n: {"link": l, "size": sizes.get(n, (None, ""))[0], "media": sizes.get(n, (None, ""))[1]}
            for n, l in have.items()}
    here = os.getcwd()
    os.chdir(root)          # кэш страниц читается относительно корня проекта
    try:
        exp = CE.Exporter(api, str(root / "Sources/Confluence"), "https://wiki.example.com",
                          "S", force, **policy)
    finally:
        os.chdir(here)
    return CE, exp, root, fetched


@test
def test_confluence_attachments_become_local_files_and_links(tmp: Path):
    """Вложения страницы доезжают до зеркала: PDF из view-file, картинки, ссылки на файлы.

    Раньше view-file молча исчезал, картинка оставалась текстом «![имя]» без файла, а
    список вложений наполняли только схемы. Теперь файлы лежат в `<страница>_assets/`,
    тело ссылается на них, исполняемое не качается, чужая страница не роняет разбор.
    """
    import urllib.parse as up
    CE, exp, root, fetched = _attach_exporter(tmp)
    here = os.getcwd()
    os.chdir(root)
    try:
        exp.walk("5001", [])
    finally:
        os.chdir(here)
    mirror = root / "Sources/Confluence"
    page = (mirror / "Руководство.md").read_text(encoding="utf-8")
    folder = mirror / "Руководство_assets"
    q = lambda n: up.quote(n)
    for name in ("law-1.pdf", "Приложение 2.pdf", "report.docx",
                 "image-2026-6-3_10-39-19-1.png"):
        assert (folder / name).is_file(), f"вложение {name} не скачано: {sorted(os.listdir(folder))}"
        assert f"](Руководство_assets/{q(name)})" in page, f"нет ссылки на {name}:\n{page}"
    assert "[law-1.pdf](Руководство_assets/law-1.pdf)" in page, "view-file не стал ссылкой"
    assert "![image-2026-6-3\\_10-39-19-1.png](Руководство_assets/" in page, page
    assert "[Отчёт](Руководство_assets/report.docx)" in page, "метка ссылки на файл потеряна"
    assert "![](https://img.example.com/pic_1.png)" in page, "внешняя картинка тронута"
    assert "[вложение другой страницы: foreign.pdf]" in page, page
    assert "aurora-asset:" not in page, f"заглушка осталась в тексте:\n{page}"
    assert "\n## Вложения страницы\n" in page
    # исполняемое не скачивается никогда и не запрашивается; лежит строкой с оригиналом
    assert not (folder / "setup.exe").exists() and not (folder / "run.bat").exists()
    assert not any(u in ("/d/exe", "/d/bat") for u in fetched), fetched
    assert ("- не скачан (исполняемый тип): [setup.exe]"
            "(https://wiki.example.com/download/attachments/5001/setup.exe)") in page, page
    assert "- не скачан (вложения с таким именем на странице нет): [gone.pdf](" in page
    assert "[gone.pdf](https://wiki.example.com/download/attachments/5001/gone.pdf)" in page

    # повторный прогон — тот же markdown, ни одной записи
    first = page
    exp2 = _attach_exporter(tmp, root=root)[1]
    os.chdir(root)
    try:
        exp2.walk("5001", [])
    finally:
        os.chdir(here)
    assert (mirror / "Руководство.md").read_text(encoding="utf-8") == first
    assert exp2.written == 0, "страница без правок переписана"


@test
def test_confluence_attachment_failure_keeps_the_old_copy(tmp: Path):
    """Сбой скачивания одного файла не стирает его прежнюю копию и не роняет страницу."""
    CE, exp, root, fetched = _attach_exporter(tmp)
    here = os.getcwd()
    os.chdir(root)
    try:
        exp.walk("5001", [])
        folder = root / "Sources/Confluence/Руководство_assets"
        before = (folder / "law-1.pdf").read_bytes()

        def broken(url):
            if url == "/d/law":
                raise OSError("обрыв")
            return b"new " + url.encode("utf-8")
        exp2 = _attach_exporter(tmp, fetch=broken, root=root)[1]
        exp2.walk("5001", [])
    finally:
        os.chdir(here)
    assert (folder / "law-1.pdf").read_bytes() == before, "прежняя копия стёрта или испорчена"
    assert (folder / "report.docx").read_bytes().startswith(b"new "), "соседние файлы не обновлены"
    assert exp2.failed == 0 and exp2.attach_failed == 1, (exp2.failed, exp2.attach_failed)
    page = (root / "Sources/Confluence/Руководство.md").read_text(encoding="utf-8")
    assert "[law-1.pdf](Руководство_assets/law-1.pdf)" in page, "ссылка на прежнюю копию потеряна"

    # без прежней копии сбой — строка с оригиналом, а не пустое место
    os.chdir(root)
    try:
        (folder / "law-1.pdf").unlink()
        exp3 = _attach_exporter(tmp, fetch=broken, root=root)[1]
        exp3.walk("5001", [])
    finally:
        os.chdir(here)
    page = (root / "Sources/Confluence/Руководство.md").read_text(encoding="utf-8")
    assert "- не скачан (ошибка скачивания): [law-1.pdf](https://wiki.example.com/" in page, page


@test
def test_name_repair_follows_attachment_sections_and_inline_links(tmp: Path):
    """`kb_names` узнаёт «## Вложения страницы» и правит встроенные ссылки на файл."""
    import importlib
    import urllib.parse as up
    sys.path.insert(0, str(SCRIPTS))
    N = importlib.import_module("kb_names")
    importlib.reload(N)
    root = make_project(tmp, git=True)
    m = root / "Sources/Confluence"
    long = "Очень длинное имя приложения " * 4 + ".pdf"
    (m / "Стр_assets").mkdir(parents=True)
    (m / "Стр_assets" / long).write_bytes(b"pdf")
    body = (f"Смотрите [файл](Стр_assets/{up.quote(long)}) и ![рис](Стр_assets/{up.quote(long)})."
            f" Чужое: [x](https://example.com/Стр_assets/{up.quote(long)})\n\n"
            f"## Вложения страницы\n\n- [{long}](Стр_assets/{up.quote(long)})\n")
    (m / "Стр.md").write_text(
        '---\npage_id: 7001\ntitle: "Стр"\nbreadcrumbs: "Стр"\ncontent_hash: 0\n---\n\n# Стр\n\n'
        + body, encoding="utf-8")
    (m / "sync_state.md").write_text(
        "<!-- Confluence sync state -->\n**Sync Date:** 2026-10-16\n**Pages:** 1\n\n"
        "| # | Page ID | Title | Local Path | Status |\n|---|---|---|---|---|\n"
        "| 1 | 7001 | Стр | Стр.md | SYNCED |\n", encoding="utf-8")
    here = os.getcwd()
    os.chdir(root)
    try:
        plan = N.mirror_plan(str(root), str(m))
        assert plan and plan[0]["assets"].get(long) and plan[0]["assets"][long] != long, \
            f"раздел вложений не узнан, план: {plan}"
        new = plan[0]["assets"][long]
        N.apply_mirror(str(root), str(m), plan)
    finally:
        os.chdir(here)
    text = (m / "Стр.md").read_text(encoding="utf-8")
    assert (m / "Стр_assets" / new).is_file() and not (m / "Стр_assets" / long).exists()
    assert text.count(f"](Стр_assets/{up.quote(new)})") == 3, text
    assert f"- [{new}](Стр_assets/{up.quote(new)})" in text, "метка строки списка не обновлена"
    assert f"](https://example.com/Стр_assets/{up.quote(long)})" in text, "чужой адрес переписан"
    assert not N.mirror_plan(str(root), str(m)), "повторная починка снова что-то переименует"


def _in_root(root, fn):
    here = os.getcwd()
    os.chdir(root)
    try:
        return fn()
    finally:
        os.chdir(here)


def _file_link(name: str, label: str = "") -> str:
    return (f'<ac:link><ri:attachment ri:filename="{name}"/>'
            f'<ac:plain-text-link-body><![CDATA[{label or name}]]></ac:plain-text-link-body></ac:link>')


@test
def test_confluence_attachment_outage_keeps_diagrams_and_copies(tmp: Path):
    """Список вложений недоступен — папка страницы не чистится: схемы и PDF на месте."""
    page = ('<ac:structured-macro ac:name="drawio"><ac:parameter ac:name="diagramName">схема1'
            '</ac:parameter></ac:structured-macro>' + _file_link("law.pdf"))
    have = {"схема1": "/d/s1", "law.pdf": "/d/law"}
    _CE, exp, root, _f = _attach_exporter(tmp, attachments=have, page=page)
    _in_root(root, lambda: exp.walk("5001", []))
    folder = root / "Sources/Confluence/Руководство_assets"
    assert sorted(os.listdir(folder)) == ["law.pdf", "схема1.drawio"], os.listdir(folder)
    exp2 = _attach_exporter(tmp, attachments=have, page=page, root=root,
                            attach_error=OSError("сеть"))[1]
    _in_root(root, lambda: exp2.walk("5001", []))
    assert sorted(os.listdir(folder)) == ["law.pdf", "схема1.drawio"], \
        f"сбой списка вложений стёр схему: {sorted(os.listdir(folder))}"


@test
def test_confluence_blocked_types_are_not_bypassed_by_trailing_dots(tmp: Path):
    """«setup.exe.» и «x.cmd..» ложатся на диск как setup.exe и x.cmd — не скачиваются."""
    names = ["setup.exe.", "x.cmd..", "SETUP.EXE", "tool.sh "]
    page = "".join(_file_link(n) for n in names)
    have = {n: "/d/" + n.strip() for n in names}
    _CE, exp, root, fetched = _attach_exporter(tmp, attachments=have, page=page)
    _in_root(root, lambda: exp.walk("5001", []))
    folder = root / "Sources/Confluence/Руководство_assets"
    left = sorted(os.listdir(folder)) if folder.is_dir() else []
    assert not left, f"исполняемое скачано: {left}"
    assert not fetched, f"исполняемое запрошено: {fetched}"
    page_md = (root / "Sources/Confluence/Руководство.md").read_text(encoding="utf-8")
    assert page_md.count("не скачан (исполняемый тип)") == len(names), page_md


@test
def test_confluence_markdown_attachment_is_not_mirrored(tmp: Path):
    """Вложение notes.md не ложится в _assets/ (его приняли бы за страницу и сироту)."""
    page = _file_link("notes.md") + _file_link("Doc.MARKDOWN") + _file_link("data.txt")
    have = {"notes.md": "/d/n", "Doc.MARKDOWN": "/d/m", "data.txt": "/d/t"}
    _CE, exp, root, fetched = _attach_exporter(tmp, attachments=have, page=page)
    _in_root(root, lambda: exp.walk("5001", []))
    folder = root / "Sources/Confluence/Руководство_assets"
    assert sorted(os.listdir(folder)) == ["data.txt"], sorted(os.listdir(folder))
    assert sorted(fetched) == ["/d/t"], fetched
    page_md = (root / "Sources/Confluence/Руководство.md").read_text(encoding="utf-8")
    assert "не скачан (markdown внутри зеркала): [notes.md](https://wiki.example.com/" in page_md


@test
def test_confluence_failed_attachment_page_is_refetched_next_sync(tmp: Path):
    """Сбой вложения — страница не попадает в кэш свежих: следующий синк качает снова."""
    page = _file_link("law.pdf")
    have = {"law.pdf": "/d/law"}

    def broken(url):
        raise OSError("таймаут")
    _CE, exp, root, _f = _attach_exporter(tmp, attachments=have, page=page, fetch=broken,
                                          force=False)

    def first():
        exp.walk("5001", [], "", 1)
        exp.save_cache()
    _in_root(root, first)
    assert exp.attach_failed == 1 and "5001" not in exp.fresh, \
        "страница со сбойным вложением записана в кэш как свежая"
    _CE2, exp2, _r, fetched2 = _attach_exporter(tmp, attachments=have, page=page, root=root,
                                                force=False)

    def second():
        exp2.walk("5001", [], "", 1)
        exp2.save_cache()
    _in_root(root, second)
    assert fetched2 == ["/d/law"], f"страница пропущена по кэшу: {fetched2}, {exp2.skipped}"
    md = (root / "Sources/Confluence/Руководство.md").read_text(encoding="utf-8")
    assert "не скачан" not in md and (root / "Sources/Confluence/Руководство_assets/law.pdf").is_file()
    # теперь всё хорошо — третий синк пропускает страницу по версии
    exp3 = _attach_exporter(tmp, attachments=have, page=page, root=root, force=False)
    _in_root(root, lambda: exp3[1].walk("5001", [], "", 1))
    assert exp3[3] == [] and exp3[1].skipped == 1, (exp3[3], exp3[1].skipped)


@test
def test_confluence_attachment_links_encode_special_page_stems(tmp: Path):
    """Заголовок с «#» и «%» даёт кодируемый стем в ссылке; kb_names узнаёт такую ссылку."""
    import importlib
    import urllib.parse as up
    page = _file_link("law.pdf")
    CE, exp, root, _f = _attach_exporter(tmp, attachments={"law.pdf": "/d/law"}, page=page)
    exp.api.page = (lambda orig: lambda pid: dict(orig(pid), title="НДС 20% и C#"))(exp.api.page)
    _in_root(root, lambda: exp.walk("5001", []))
    mirror = root / "Sources/Confluence"
    md = (mirror / "НДС_20%_и_C#.md").read_text(encoding="utf-8")
    assert "](НДС_20%25_и_C%23_assets/law.pdf)" in md, md
    assert (mirror / "НДС_20%_и_C#_assets/law.pdf").is_file()
    sys.path.insert(0, str(SCRIPTS))
    N = importlib.import_module("kb_names")
    importlib.reload(N)
    assert N.own_stem("НДС_20%25_и_C%23", "НДС_20%_и_C#") and not N.own_stem("Другая", "НДС_20%")
    assert up.unquote("НДС_20%25_и_C%23") == "НДС_20%_и_C#"
    assert CE.link_stem("НДС_20%_и_C#_x?") == "НДС_20%25_и_C%23_x%3F"


@test
def test_confluence_long_attachment_name_keeps_its_short_extension(tmp: Path):
    """Длинное имя с расширением вне списка (.xlsm) не теряет расширение; «.Итог» — часть имени."""
    CE, exp, root, _f = _attach_exporter(tmp)
    folder = "П" * 100 + "_assets"
    long = "Очень длинное название отчёта о проделанной работе за 2026 год и ещё много слов"
    got = exp.asset_name(long + ".xlsm", folder)
    assert got.endswith(".xlsm"), got
    import re
    assert re.search(r"~[0-9a-f]{6}$", exp.asset_name(long + ".Итог", folder)), \
        "«.Итог» принят за расширение"
    assert exp.asset_name("Стр4.Свод.2", folder) == "Стр4.Свод.2"


@test
def test_name_repair_ignores_links_that_escape_the_page_folder(tmp: Path):
    """Ссылка вида `..%2F..%2Ffile` в тексте страницы не переносит файлы проекта."""
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    N = importlib.import_module("kb_names")
    importlib.reload(N)
    root = make_project(tmp, git=True)
    m = root / "Sources/Confluence"
    m.mkdir(parents=True, exist_ok=True)
    victim = root / "secret_demo.txt"
    victim.write_text("данные проекта", encoding="utf-8")
    (m / "Стр_assets").mkdir()
    for body in ("[x](Стр_assets/..%2F..%2F..%2Fsecret_demo.txt)",
                 "[x](Стр_assets/..%2F..%2F..%2Fsecret_demo.txt)\n\n## Вложения страницы\n\n"
                 "- [../../../secret_demo.txt](Стр_assets/..%2F..%2F..%2Fsecret_demo.txt)"):
        (m / "Стр.md").write_text(
            '---\npage_id: 7001\ntitle: "Стр"\nbreadcrumbs: "Стр"\ncontent_hash: 0\n---\n\n'
            "# Стр\n\n" + body + "\n\n## Схемы страницы\n\n", encoding="utf-8")
        (m / "sync_state.md").write_text(
            "<!-- Confluence sync state -->\n**Sync Date:** 2026-10-16\n**Pages:** 1\n\n"
            "| # | Page ID | Title | Local Path | Status |\n|---|---|---|---|---|\n"
            "| 1 | 7001 | Стр | Стр.md | SYNCED |\n", encoding="utf-8")

        def run():
            plan = N.mirror_plan(str(root), str(m))
            assert not any(".." in k for it in plan for k in it["assets"]), plan
            # даже прямой план с таким именем не двигает файл за пределы папки страницы
            N.apply_mirror(str(root), str(m), [{"pid": "7001", "title": "Стр", "old": "Стр.md",
                           "new": "Стр.md", "assets": {"../../../secret_demo.txt": "x_secret.txt"}}])
        _in_root(root, run)
        assert victim.read_text(encoding="utf-8") == "данные проекта", "файл проекта перенесён"


def _sub(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_conf(tmp: Path, conf_lines: str) -> dict:
    """read_config() по конфигу проекта с заданными строками в блоке confluence."""
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    CE = importlib.import_module("confluence_export")
    importlib.reload(CE)
    tmp.mkdir(parents=True, exist_ok=True)
    root = make_project(tmp)
    (root / "aurora.config.yaml").write_text(
        'atlassian:\n  confluence:\n    base_url: "https://wiki.example.com"\n'
        '    space: "S"\n' + conf_lines + '    sync_roots: []\n  jira:\n    base_url: ""\n'
        'paths:\n  sources_confluence: Sources/Confluence\n', encoding="utf-8")
    return CE, CE.read_config(str(root))


@test
def test_confluence_attachment_settings_are_read_from_config(tmp: Path):
    """attachments / attachment_max_mb читаются из блока confluence; плохое значение — умолчание."""
    CE, cfg = _read_conf(tmp / "a", "")
    assert (cfg["attachments"], cfg["attachment_max_mb"]) == ("all", 25.0), cfg
    for text, mode, mb in (("    attachments: images\n    attachment_max_mb: 1\n", "images", 1.0),
                           ('    attachments: "none"\n    attachment_max_mb: 0\n', "none", 0.0),
                           ("    attachments: Images\n    attachment_max_mb: 0.5\n", "images", 0.5),
                           ("    attachments: всё\n    attachment_max_mb: много\n", "all", 25.0),
                           ("    attachment_max_mb: -3\n", "all", 25.0),
                           ("    # attachments: none\n    # attachment_max_mb: 1\n", "all", 25.0)):
        _c, cfg = _read_conf(tmp / ("b" + str(abs(hash(text)))), text)
        assert (cfg["attachments"], cfg["attachment_max_mb"]) == (mode, mb), (text, cfg)


@test
def test_confluence_attachment_settings_reach_the_exporter(tmp: Path):
    """Режим и предел из конфига доходят до Exporter через run_export (без сети: корней нет)."""
    CE, cfg = _read_conf(tmp, "    attachments: images\n    attachment_max_mb: 3\n")
    here = os.getcwd()
    os.chdir(tmp)
    try:
        exp = CE.run_export(cfg, [], str(tmp / "out"), "", False)
    finally:
        os.chdir(here)
    assert (exp.att_mode, exp.att_max_mb) == ("images", 3.0), (exp.att_mode, exp.att_max_mb)
    assert (CE.Exporter.att_mode, CE.Exporter.att_max_mb) == ("all", 25.0), "умолчание класса"


@test
def test_confluence_attachment_size_from_metadata_blocks_before_fetch(tmp: Path):
    """Размер из свойств вложения больше предела: файл не качается, остаётся строка с причиной."""
    mb = 1024 * 1024
    sizes = {"law-1.pdf": (mb + 1, "application/pdf"), "report.docx": (mb, "application/x"),
             "Приложение 2.pdf": (0, ""), "image-2026-6-3_10-39-19-1.png": (5, "image/png")}
    CE, exp, root, fetched = _attach_exporter(tmp, sizes=sizes, att_max_mb=1)
    exp.walk("5001", [])
    page = (root / "Sources/Confluence/Руководство.md").read_text(encoding="utf-8")
    assert "/d/law" not in fetched, "файл больше предела скачан"
    assert ("- не скачан (больше 1 МБ): [law-1.pdf]"
            "(https://wiki.example.com/download/attachments/5001/law-1.pdf)") in page, page
    assert "[law-1.pdf](https://wiki.example.com/download/attachments/5001/law-1.pdf)" in page
    assert {"/d/rep", "/d/app2", "/d/img"} <= set(fetched), fetched      # ровно на пороге — качается
    assert (root / "Sources/Confluence/Руководство_assets/report.docx").is_file()
    assert exp.attach_skipped >= 1 and not (root / "Sources/Confluence/Руководство_assets/law-1.pdf").exists()


@test
def test_confluence_attachment_unknown_size_is_limited_while_reading(tmp: Path):
    """Размера в ответе нет: чтение ограничено пределом (max_bytes), превышение — «не скачан»."""
    limits: list = []
    # у «bytes of /d/law» 14 байт: предел 1 МБ его пропускает, а крошечный — нет
    CE, exp, root, fetched = _attach_exporter(_sub(tmp / "u"), limits=limits)
    exp.walk("5001", [])
    assert limits and all(x == 1024 * 1024 * 25 for x in limits), limits
    CE2, exp2, root2, _f = _attach_exporter(_sub(tmp / "t"), limits=[], att_max_mb=1e-5)
    exp2.walk("5001", [])
    page = (root2 / "Sources/Confluence/Руководство.md").read_text(encoding="utf-8")
    assert "не скачан (больше 1e-05 МБ): [law-1.pdf]" in page, page
    assert exp2.attach_failed == 0 and exp2.attach_skipped >= 3
    # 0 — без предела: чтение без max_bytes
    limits3: list = []
    _C, exp3, _r, _f3 = _attach_exporter(_sub(tmp / "z"), limits=limits3, att_max_mb=0)
    exp3.walk("5001", [])
    assert limits3 and all(x is None for x in limits3), limits3


@test
def test_confluence_attachment_modes_images_and_none(tmp: Path):
    """images качает только картинки (по расширению или типу), none — ничего; ссылки ведут на оригинал."""
    sizes = {"report.docx": (3, "image/png")}      # тип картинки у файла без картиночного расширения
    CE, exp, root, fetched = _attach_exporter(_sub(tmp / "i"), sizes=sizes, att_mode="images")
    exp.walk("5001", [])
    page = (root / "Sources/Confluence/Руководство.md").read_text(encoding="utf-8")
    assert sorted(fetched) == ["/d/img", "/d/rep"], fetched
    assert ("- не скачан (режим images): [law-1.pdf]"
            "(https://wiki.example.com/download/attachments/5001/law-1.pdf)") in page, page
    assert "[image-2026-6-3_10-39-19-1.png](Руководство_assets/" in page
    CE2, exp2, root2, fetched2 = _attach_exporter(_sub(tmp / "n"), att_mode="none")
    exp2.walk("5001", [])
    page2 = (root2 / "Sources/Confluence/Руководство.md").read_text(encoding="utf-8")
    assert fetched2 == [], fetched2
    assert "(режим none)" in page2 and "(Руководство_assets/law-1.pdf)" not in page2
    assert "](https://wiki.example.com/download/attachments/5001/law-1.pdf)" in page2
    # схемы режиму не подчиняются: исходник диаграммы качается и при none
    sch = '<ac:structured-macro ac:name="drawio"><ac:parameter ac:name="diagramName">Схема</ac:parameter></ac:structured-macro>'
    _C, exp3, root3, fetched3 = _attach_exporter(_sub(tmp / "s"), att_mode="none", page=sch,
                                                 attachments={"Схема": "/d/sch"})
    exp3.walk("5001", [])
    assert fetched3 == ["/d/sch"], fetched3


@test
def test_rest_fetch_limit_stops_reading_at_max_bytes(tmp: Path):
    """RestApi.fetch(max_bytes): читает на байт больше предела и бросает FileTooLarge; без аргумента — как раньше."""
    import importlib
    import io
    sys.path.insert(0, str(SCRIPTS))
    SC = importlib.import_module("sources_core")
    importlib.reload(SC)

    class Resp(io.BytesIO):
        def __init__(self, data, length=None):
            super().__init__(data)
            self.headers = {"Content-Length": str(length)} if length is not None else {}
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    class Api(SC.RestApi):
        def _url(self, path):
            return "https://wiki.example.com" + path

    real = SC.safe_urlopen
    try:
        api = Api("https://wiki.example.com", "")
        api._seen = True
        SC.safe_urlopen = lambda req, timeout=0: Resp(b"x" * 10)
        assert api.fetch("/f") == b"x" * 10
        assert api.fetch("/f", max_bytes=10) == b"x" * 10
        for resp in (Resp(b"x" * 11), Resp(b"x" * 3, length=11)):
            SC.safe_urlopen = lambda req, timeout=0, r=resp: r
            try:
                api.fetch("/f", max_bytes=10)
            except SC.FileTooLarge:
                pass
            else:
                raise AssertionError("превышение предела не замечено")
    finally:
        SC.safe_urlopen = real
        importlib.reload(SC)


@test
def test_setup_form_keeps_confluence_attachment_settings(tmp: Path):
    """Форма настроек не стирает confluence.attachments / attachment_max_mb; повтор даёт тот же текст."""
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    S = importlib.import_module("aurora_setup")
    importlib.reload(S)
    cfg = tmp / "aurora.config.yaml"
    cfg.write_text(
        'atlassian:\n  confluence:\n    base_url: "https://wiki.example.com"\n    space: "S"\n'
        '    attachments: images\n    attachment_max_mb: 7\n    sync_roots: []\n'
        '  jira:\n    base_url: ""\n', encoding="utf-8")
    c = {"name": "Demo", "slug": "demo", "conf_url": "https://wiki.example.com", "conf_space": "S",
         "jira_url": "", "jira_key": "D", "jira_jql": "project = D", "trust_statuses": "Done",
         "assumption_statuses": "Open", "scrub": "off", "sync_roots": [],
         "trusted_sources": "", "trusted_branches": "", "threshold": "70"}
    S.write_config(cfg, dict(c))
    first = cfg.read_text(encoding="utf-8")
    assert "    attachments: images\n    attachment_max_mb: 7\n" in first, first
    S.write_config(cfg, dict(c))
    assert cfg.read_text(encoding="utf-8") == first, "повторное сохранение изменило конфиг"
    # без ключей в прежнем конфиге форма их не добавляет
    cfg2 = tmp / "plain" / "aurora.config.yaml"
    cfg2.parent.mkdir()
    S.write_config(cfg2, dict(c))
    assert "\n    attachments:" not in cfg2.read_text(encoding="utf-8")
    assert "attachment_max_mb:" not in cfg2.read_text(encoding="utf-8")
