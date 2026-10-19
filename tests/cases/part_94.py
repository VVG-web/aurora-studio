"""Проверки движка Aurora, часть 94. Каркас и помощники — tests/harness.py.

Контур выпуска документов (BL-SEC-22): единый предохранитель выходных команд, фиксация сданной версии
по тому, что передали (чистовик, хеш, чистое дерево, проверка `--verify`), безопасные пути публикации
и цепочка «куда публиковать». Сеть, Confluence и модель не используются; проекты — во временных папках.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, run, test  # noqa: F401

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

MARK = "<!-- ниже — производство, в чистовик не идёт -->"
CLEAN = "# Спецификация\n\nПлатёж проводится за пять дней.\n"
KITCHEN = "\n## Допущения\n\n- срок взят из практики\n\n## Замечания критика\n\n- мало примеров\n"


def _doc(head: str = "type: deliverable\ndoc: Spec\nversion: \"1.0\"\nstatus: ready\n", body: str = "") -> str:
    return f"---\n{head}---\n\n" + (body or CLEAN + "\n" + MARK + "\n" + KITCHEN)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=str(root),
                   check=True, capture_output=True)


@test
def test_outward_gate_blocks_what_cannot_be_taken_back(_t):
    """Предохранитель: производство без границы, остатки шаблона, draft и персональные данные — блок; всё чистое — нет."""
    import aurora_common as C
    codes = lambda text, **kw: sorted(c for lvl, c, _m in C.outward_gate(text, None, **kw) if lvl == "block")  # noqa: E731
    assert codes(_doc()) == [], codes(_doc())
    # границы нет, а разделы производства есть — уедут наружу целиком
    assert codes(_doc(body=CLEAN + KITCHEN)) == ["no-marker"]
    # то же самое внутри блока кода — не производство
    assert codes(_doc(body=CLEAN + "\n```\n## Допущения\n```\n")) == []
    # остатки шаблона; в блоке кода и в одинарных обратных кавычках — нет
    assert codes(_doc(body=CLEAN + "\nТекст {{PROJECT}} и [ПРИМЕР]\n")) == ["template-leftovers"]
    assert codes(_doc(body=CLEAN + "\nКоманда `{{name}}` и\n```\n{{x}}\n```\n")) == []
    # остатки за границей чистовика не мешают: они в кухне
    assert codes(_doc(body=CLEAN + "\n" + MARK + "\n{{кухня}}\n")) == []
    assert codes(_doc(head="status: draft\n")) == ["draft"]
    assert codes(_doc(head="status: \"черновик\"\n")) == ["draft"]
    pd = _doc(body=CLEAN + "\nПишите на ivan.petrov@example.com\n")
    assert codes(pd, scrub="mask") == ["privacy"] and codes(pd, scrub="report") == [], codes(pd, scrub="mask")
    # рабочая атрибуция — не персональные данные
    assert codes(_doc(body=CLEAN + "\nAssignee: ivan.petrov@example.com\n"), scrub="mask") == []
    # блок можно снять только явным кодом
    found = C.outward_gate(_doc(body=CLEAN + KITCHEN), None)
    assert C.gate_blocked(found, ["no-marker"]) == [] and len(C.gate_blocked(found, [])) == 1


def _publish_project(tmp: Path) -> Path:
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace(
        '    space: "T"\n', '    space: "T"\n    base_url: "https://wiki.example.com"\n'), encoding="utf-8")
    (root / "Artifacts" / "reports").mkdir(parents=True, exist_ok=True)
    return root


@test
def test_publish_refuses_a_document_without_the_clean_boundary(tmp: Path):
    """`ship:publish --apply` на документе с кухней без границы отказывает (код 2) до любых обращений к Confluence."""
    root = _publish_project(tmp)
    doc = root / "Artifacts" / "reports" / "итог.md"
    doc.write_text(_doc(body=CLEAN + KITCHEN), encoding="utf-8")
    dry = run("publish_doc.py", "Artifacts/reports/итог.md", cwd=root)
    assert "[no-marker]" in dry.stdout and "--apply откажет" in dry.stdout and dry.returncode in (0, 1), dry.stdout + dry.stderr
    cp = run("publish_doc.py", "Artifacts/reports/итог.md", "--apply", cwd=root)
    assert cp.returncode == 2 and "--allow no-marker" in cp.stderr, (cp.returncode, cp.stdout, cp.stderr)
    # осознанный обход проходит предохранитель (дальше — нет доступа к Confluence: токена в тесте нет)
    cp = run("publish_doc.py", "Artifacts/reports/итог.md", "--apply", "--allow", "no-marker", cwd=root)
    assert cp.returncode == 1 and "нет доступа к Confluence" in cp.stderr, (cp.returncode, cp.stdout, cp.stderr)
    bad = run("publish_doc.py", "Artifacts/reports/итог.md", "--allow", "опечатка", cwd=root)
    assert bad.returncode == 2 and "invalid choice" in bad.stderr, bad.stderr


@test
def test_publish_path_guard_sees_absolute_paths_and_case(tmp: Path):
    """Закрытые слои не публикуются ни абсолютным путём, ни другим регистром (там, где ФС его не различает)."""
    root = _publish_project(tmp)
    (root / "Raw" / "customer").mkdir(parents=True, exist_ok=True)
    secret = root / "Raw" / "customer" / "договор.md"
    secret.write_text(_doc(), encoding="utf-8")
    for arg in ("Raw/customer/договор.md", str(secret)):
        cp = run("publish_doc.py", arg, cwd=root)
        assert cp.returncode == 1 and "не публикуется" in cp.stderr, (arg, cp.stderr)
    alias = root / "raw" / "customer" / "договор.md"
    if alias.exists():           # macOS и Windows: тот же файл под другим регистром
        cp = run("publish_doc.py", "raw/customer/договор.md", cwd=root)
        assert cp.returncode == 1 and "не публикуется" in cp.stderr, cp.stderr


@test
def test_publish_parent_comes_from_cli_header_or_config(tmp: Path):
    """Родитель новой страницы: `--parent` → `publish_parent` шапки (вид с publish_url) → конфиг; адреса разбираются."""
    import publish_doc as P
    assert P.parent_from("12345") == ("12345", None)
    assert P.parent_from("https://wiki.example.com/pages/viewpage.action?pageId=777") == ("777", None)
    assert P.parent_from("https://wiki.example.com/spaces/T/pages/4242/Some+Title") == ("4242", None)
    assert P.parent_from("https://wiki.example.com/display/T/Мой+раздел") == ("", ("T", "Мой раздел"))
    assert P.parent_from("https://wiki.example.com/display/T/Мой+раздел/") == ("", ("T", "Мой раздел"))
    assert P.parent_from("") == ("", None) and P.parent_from("просто слова") == ("", None)
    cfg = {"publish_parent": "30"}
    assert P.choose_parent("10", {"publish_parent": "20"}, cfg) == ("10", "--parent")
    assert P.choose_parent("", {"publish_parent": '"20"'}, cfg) == ("20", "publish_parent в шапке документа")
    assert P.choose_parent("", {}, cfg) == ("30", "confluence.publish_parent в конфиге")
    assert P.choose_parent("", {}, {}) == ("", "")
    # конфиг читается тем же чтением, что base_url и space
    import confluence_export as CE
    root = _publish_project(tmp)
    cfgp = root / "aurora.config.yaml"
    cfgp.write_text(cfgp.read_text(encoding="utf-8").replace(
        '    space: "T"\n', '    space: "T"\n    publish_parent: "9001"\n'), encoding="utf-8")
    here = os.getcwd()
    os.chdir(root)
    try:
        assert CE.read_config()["publish_parent"] == "9001"
    finally:
        os.chdir(here)


@test
def test_publish_does_not_turn_dangerous_links_into_clickable_ones(_t):
    """`[текст](javascript:…)`, `data:`, `file:` на странице у заказчика кликабельными не становятся."""
    import publish_doc as P
    out = P.inline("[a](javascript:alert(1)) [b](JaVaScRiPt:x) [c](data:text/html,x) [d](file:///etc/passwd) "
                   "[e](https://example.com/a?b=1&c=2) [f](../rel.md) [g](#якорь) [h](mailto:a@example.com)")
    for bad in ("javascript", "data:", "file:"):
        assert bad.lower() not in out.lower().replace("<a href", ""), (bad, out)
    for good in ('href="https://example.com/a?b=1&amp;c=2"', 'href="../rel.md"', 'href="#якорь"', 'href="mailto:a@example.com"'):
        assert good in out, (good, out)
    assert out.count("<a href") == 4, out                    # https, относительная, якорь, mailto — остальные стали текстом


def _released_project(tmp: Path) -> Path:
    root = make_project(tmp, git=True)
    work = root / "Deliverables" / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "Spec_v1.0.md").write_text(_doc(), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "doc")
    return root


@test
def test_release_freezes_what_was_handed_over_and_proves_it(tmp: Path):
    """Снимок = чистовик (без кухни) с хешем и коммитом; `--verify` ловит правку и удаление после передачи."""
    root = _released_project(tmp)
    cp = run("ship_doc.py", "Deliverables/work/Spec_v1.0.md", "--release", "--apply", "--date", "2026-10-19", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    snap = root / "Deliverables" / "released" / "Spec_v1.0_2026-10-19.md"
    text = snap.read_text(encoding="utf-8")
    assert "Платёж проводится за пять дней." in text, text
    assert "Допущения" not in text and MARK not in text, "в снимок уехала кухня производства"
    import aurora_common as C
    sha = C.frontmatter(text).get("sha256")
    assert sha and sha == C.release_hash(text) and C.frontmatter(text).get("released_commit"), text
    assert "Допущения" in (root / "Deliverables" / "work" / "Spec_v1.0.md").read_text(encoding="utf-8"), \
        "рабочая копия должна остаться целой"
    ok = run("ship_doc.py", "--verify", cwd=root)
    assert ok.returncode == 0 and "не менялись" in ok.stdout, ok.stdout + ok.stderr
    # правка сданного из терминала: хеш расходится (на старом коде этого не видел никто)
    snap.write_text(text.replace("пять дней", "три дня"), encoding="utf-8")
    bad = run("ship_doc.py", "--verify", cwd=root)
    assert bad.returncode == 1 and "Spec_v1.0_2026-10-19.md" in bad.stdout and "хеш" in bad.stdout, bad.stdout
    snap.write_text(text, encoding="utf-8")
    # удаление после коммита
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "released")
    assert run("ship_doc.py", "--verify", cwd=root).returncode == 0
    snap.unlink()
    gone = run("ship_doc.py", "--verify", cwd=root)
    assert gone.returncode == 1 and "удалён после коммита" in gone.stdout, gone.stdout
    # перевод строк (git на Windows) хеш не ломает
    snap.write_text(text.replace("\n", "\r\n"), encoding="utf-8", newline="")
    assert C.release_hash(snap.read_bytes().decode("utf-8")) == sha


@test
def test_release_refuses_a_document_that_is_not_in_the_commit(tmp: Path):
    """`released_commit` обещает, что версия была в коммите: правки не закоммичены — отказ, либо явный --allow-dirty."""
    root = _released_project(tmp)
    doc = root / "Deliverables" / "work" / "Spec_v1.0.md"
    doc.write_text(doc.read_text(encoding="utf-8").replace("пять дней", "шесть дней"), encoding="utf-8")
    cp = run("ship_doc.py", "Deliverables/work/Spec_v1.0.md", "--release", "--apply", "--date", "2026-10-19", cwd=root)
    assert cp.returncode == 2 and "незакоммиченные" in cp.stdout and "--allow-dirty" in cp.stderr, cp.stdout + cp.stderr
    assert not list((root / "Deliverables" / "released").glob("*.md")), "снимок записан несмотря на отказ"
    cp = run("ship_doc.py", "Deliverables/work/Spec_v1.0.md", "--release", "--apply", "--date", "2026-10-19",
             "--allow-dirty", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    snap = next((root / "Deliverables" / "released").glob("*.md")).read_text(encoding="utf-8")
    assert "released_dirty: true" in snap and "шесть дней" in snap, snap


@test
def test_release_names_and_paths_are_safe(tmp: Path):
    """`doc` и `version` из шапки не выводят снимок за released/; соседняя папка `work2` не считается `work`."""
    root = make_project(tmp, git=True)
    work = root / "Deliverables" / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "Evil_v1.md").write_text(_doc(head="type: deliverable\ndoc: ../../outside\nversion: \"../x\"\n"),
                                     encoding="utf-8")
    other = root / "Deliverables" / "work2"
    other.mkdir(parents=True, exist_ok=True)
    (other / "X_v1.md").write_text(_doc(), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "docs")
    cp = run("ship_doc.py", "Deliverables/work/Evil_v1.md", "--release", "--apply", "--date", "2026-10-19", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    made = [p for p in (root / "Deliverables" / "released").iterdir()]
    assert len(made) == 1 and made[0].parent == root / "Deliverables" / "released", made
    assert "/" not in made[0].name and ".." not in made[0].name, made[0].name
    assert not (root / "outside").exists() and not list((root / "Deliverables").glob("*outside*")), \
        "снимок вышел за Deliverables/released/"
    bad = run("ship_doc.py", "Deliverables/work2/X_v1.md", "--release", "--apply", cwd=root)
    assert bad.returncode == 1 and "замораживать можно только" in bad.stderr, bad.stderr


@test
def test_export_and_release_go_through_the_same_gate_and_pandoc_runs_sandboxed(tmp: Path):
    """Экспорт и фиксация — через тот же предохранитель; pandoc запускается с --sandbox."""
    import ship_doc as S
    root = make_project(tmp, git=True)
    work = root / "Deliverables" / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "A_v1.md").write_text(_doc(body=CLEAN + KITCHEN), encoding="utf-8")
    (work / "B_v1.md").write_text(_doc(), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "docs")
    rel = run("ship_doc.py", "Deliverables/work/A_v1.md", "--release", "--apply", cwd=root)
    assert rel.returncode == 2 and "[no-marker]" in rel.stdout, rel.stdout + rel.stderr
    seen = []
    real_run, real_find = S.subprocess.run, S.find_bin

    def spy(cmd, *a, **kw):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "pandoc 3.1\n", "")
        seen.append([str(c) for c in cmd])
        Path(cmd[cmd.index("-o") + 1]).write_text("docx", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    S.subprocess.run = spy
    S.find_bin = lambda name: "/usr/bin/pandoc"
    here = os.getcwd()
    os.chdir(root)
    try:
        assert S.export("Deliverables/work/A_v1.md", "docx", "", "", False) == 2, "экспорт мимо предохранителя"
        assert S.export("Deliverables/work/A_v1.md", "docx", "", "", False, ["no-marker"]) == 0
        assert S.export("Deliverables/work/B_v1.md", "docx", "", "", False) == 0
    finally:
        os.chdir(here)
        S.subprocess.run, S.find_bin = real_run, real_find
        importlib.reload(S)
    assert seen and all("--sandbox" in c for c in seen), seen


@test
def test_spec_pack_refuses_personal_data_under_mask_mode(tmp: Path):
    """Бандл спеки уходит подрядчику: при `privacy.scrub: mask` персональные данные в нём — отказ, `--allow privacy` снимает."""
    from harness import card
    root = make_project(tmp)
    card(root, "Specs/SPEC-012-Обмен.md", "Контакт: ivan.petrov@example.com", type="spec", spec_id="SPEC-012",
         status="draft", version='"1.2"')
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "privacy:\n  scrub: mask\n", encoding="utf-8")
    cp = run("spec_pack.py", "SPEC-012", "--apply", cwd=root)
    assert cp.returncode == 1 and "[privacy]" in cp.stderr, (cp.returncode, cp.stdout, cp.stderr)
    assert not list((root / "Deliverables" / "work" / "spec-packs").glob("*.md")), "бандл записан несмотря на отказ"
    ok = run("spec_pack.py", "SPEC-012", "--apply", "--allow", "privacy", cwd=root)
    assert ok.returncode == 0 and list((root / "Deliverables" / "work" / "spec-packs").glob("*.md")), ok.stderr
    cfg.write_text(cfg.read_text(encoding="utf-8").replace("scrub: mask", "scrub: report"), encoding="utf-8")
    for old in (root / "Deliverables" / "work" / "spec-packs").glob("*.md"):
        old.unlink()
    assert run("spec_pack.py", "SPEC-012", "--apply", cwd=root).returncode == 0, "в режиме report бандл должен собираться"


@test
def test_outward_gate_sees_heading_variants_a_broken_boundary_and_crlf(_t):
    """Заголовки производства в любой записи, повреждённая граница и CRLF не проходят; ссылка `[пример]` — не остаток шаблона."""
    import aurora_common as C
    codes = lambda text, **kw: sorted(c for lvl, c, _m in C.outward_gate(text, None, **kw) if lvl == "block")  # noqa: E731
    for head in ("## допущения", "## Допущения ##", "## Допущения:", "## **Допущения**", "### 2. Допущения",
                 "## Замечания критика (авто)", "##\u00a0Допущения", "Допущения\n=========", "Допущения\n---------"):
        assert codes(_doc(body=CLEAN + "\n" + head + "\n\nтекст\n")) == ["no-marker"], head
    # название, только начинающееся с раздела производства, — содержание документа: предупреждение, не блок
    loose = _doc(body=CLEAN + "\n## 3. Допущения и ограничения проекта\n\nтекст\n")
    assert codes(loose) == [] and [c for lv, c, _m in C.outward_gate(loose, None) if lv == "warn"] == ["production-heading"]
    assert codes(_doc(body=CLEAN + "\n## Допущения\n")) == ["no-marker"] and codes(_doc(body=CLEAN + "\n## Обзор\n")) == []
    # граница есть, но испорчена: другое тире, хвостовой пробел — `clean_copy` её не режет
    for bad in ("<!-- ниже - производство, в чистовик не идёт -->", MARK + " ", "<!-- ниже — производство, в чистовик не идёт  -->"):
        assert codes(_doc(body=CLEAN + "\n" + bad + "\n" + KITCHEN)) == ["no-marker"], bad
    # текст из байтов с CRLF (Windows) ведёт себя так же, как с LF
    assert codes(_doc(body=CLEAN + KITCHEN).replace("\n", "\r\n")) == ["no-marker"]
    assert codes(_doc().replace("\n", "\r\n")) == []
    # остатки шаблона: тот же перечень, что у линтера артефактов; обычная ссылка [пример] — не остаток
    assert codes(_doc(body=CLEAN + "\n<!-- ОПЦИОНАЛЬНО -->\n")) == ["template-leftovers"]
    assert codes(_doc(body=CLEAN + "\n<!-- УДАЛИТЬ ПЕРЕД ПУБЛИКАЦИЕЙ -->\n")) == ["template-leftovers"]
    assert codes(_doc(body=CLEAN + "\nСм. [пример](x.md) и [Пример] ниже.\n")) == []
    import artifact_lint as AL
    assert AL._TEMPLATE_COMMENT is C.TEMPLATE_COMMENT, "два перечня остатков шаблона разошлись"


@test
def test_released_check_ignores_the_folder_guide_and_reads_non_ascii_names(tmp: Path):
    """README.md папки (его обновляет кит) — не сданное; кириллическое имя называется читаемо; бинарник сверяется по хешу."""
    import aurora_common as C
    root = make_project(tmp, git=True)
    work = root / "Deliverables" / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "Спека_v1.0.md").write_text(_doc(head="type: deliverable\ndoc: Спека\nversion: \"1.0\"\nstatus: ready\n"),
                                       encoding="utf-8")
    rel = root / "Deliverables" / "released"
    rel.mkdir(parents=True, exist_ok=True)
    (rel / "README.md").write_text("guide v1\n", encoding="utf-8")
    (rel / "built.docx").write_bytes(b"docx-bytes-v1")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "docs")
    cp = run("ship_doc.py", "Deliverables/work/Спека_v1.0.md", "--release", "--apply", "--date", "2026-10-19",
             "--binary", "Deliverables/released/built.docx", cwd=root)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    snap = rel / "Спека_v1.0_2026-10-19.md"
    assert "binary_sha256:" in snap.read_text(encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "released")
    (rel / "README.md").write_text("guide v2 (обновил кит)\n", encoding="utf-8")
    assert C.released_problems(str(root)) == [], "обновление описания папки названо правкой сданного"
    snap.write_text(snap.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
    problems = C.released_problems(str(root))
    assert any(p.startswith("Спека_v1.0_2026-10-19.md: ") for p in problems) and not any("\\3" in p for p in problems), problems
    (rel / "built.docx").write_bytes(b"docx-bytes-OTHER")
    assert any("built.docx" in p and "заменили после передачи" in p for p in C.released_problems(str(root)))
    # второй релиз с тем же именем бинарника и другим содержимым не затирает сданный
    other = tmp / "built.docx"
    other.write_bytes(b"docx-bytes-v2")
    (work / "Спека_v1.1.md").write_text(_doc(head="type: deliverable\ndoc: Спека\nversion: \"1.1\"\nstatus: ready\n"),
                                       encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "v1.1")
    bad = run("ship_doc.py", "Deliverables/work/Спека_v1.1.md", "--release", "--apply", "--date", "2026-10-20",
              "--binary", str(other), cwd=root)
    assert bad.returncode == 1 and "уже сдан с другим содержимым" in bad.stderr, bad.stdout + bad.stderr


@test
def test_export_tells_that_images_are_not_embedded_and_can_embed_them_on_request(tmp: Path):
    """Песочница pandoc не встраивает картинки — об этом сказано; `--embed-images` отключает её для доверенного документа."""
    import ship_doc as S
    root = make_project(tmp)
    work = root / "Deliverables" / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "P_v1.md").write_text(_doc(body=CLEAN + "\n![схема](pic.png)\n"), encoding="utf-8")
    seen, version = [], ["pandoc 3.1"]
    real_run, real_find = S.subprocess.run, S.find_bin

    def spy(cmd, *a, **kw):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, version[0] + "\n", "")
        seen.append([str(c) for c in cmd])
        Path(cmd[cmd.index("-o") + 1]).write_text("docx", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    S.subprocess.run = spy
    S.find_bin = lambda name: "/usr/bin/pandoc"
    here = os.getcwd()
    os.chdir(root)
    import contextlib
    import io
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            assert S.export("Deliverables/work/P_v1.md", "docx", "", "", False) == 0
            assert S.export("Deliverables/work/P_v1.md", "docx", "", "", False, (), True) == 0
            version[0] = "pandoc 2.9.2"
            assert S.export("Deliverables/work/P_v1.md", "docx", "", "", False) == 0
    finally:
        os.chdir(here)
        S.subprocess.run, S.find_bin = real_run, real_find
        importlib.reload(S)
    assert "--sandbox" in seen[0] and "--sandbox" not in seen[1] and "--sandbox" not in seen[2], seen
    assert "картинок: 1" in out.getvalue() and "--embed-images" in out.getvalue(), out.getvalue()
    assert "pandoc старше 2.15" in out.getvalue(), out.getvalue()


@test
def test_doctor_reports_released_versions_that_were_touched(tmp: Path):
    """kit:doctor называет сданную версию, которую правили после передачи."""
    import aurora_common as C
    root = _released_project(tmp)
    run("ship_doc.py", "Deliverables/work/Spec_v1.0.md", "--release", "--apply", "--date", "2026-10-19", cwd=root)
    assert C.released_problems(str(root)) == []
    snap = root / "Deliverables" / "released" / "Spec_v1.0_2026-10-19.md"
    snap.write_text(snap.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
    problems = C.released_problems(str(root))
    assert len(problems) == 1 and "правили после передачи" in problems[0], problems
    cp = subprocess.run([sys.executable, ".aurora/scripts/aurora_doctor.py"], cwd=str(root), capture_output=True,
                        text=True, encoding="utf-8", errors="replace", timeout=180)
    assert "Spec_v1.0_2026-10-19.md" in cp.stdout + cp.stderr, cp.stdout[-1500:]


@test
def test_export_does_not_carry_foreign_html_or_script_links_into_the_output(_t):
    """Текст приходит из базы: `--sandbox` не мешает pandoc записать в html `<script>` и `javascript:` — это закрыто до pandoc.

    Адрес со схемой `javascript:`, `data:`, `vbscript:`, `file:` в ссылке markdown заменяется на `#blocked` (подпись
    остаётся; вложенные скобки, сущности вроде `&Tab;` и ссылки-определения на следующей строке не помеха), а готовый
    html проходит `scrub_html`; в html-экспорте начало тега вне кода экранируется (код и автоссылки не трогаются);
    без `--sandbox` картинки заменяются подписью; всё это быстро."""
    import time
    import ship_doc as S
    for evil in ("[a](javascript:alert(1))", "[a [b]](javascript:alert(1))", "[a\\]b](javascript:alert(1))",
                 "[![i](a.png)](javascript:alert(1))", "[x][1]\n\n[1]:\njavascript:alert(1)", "[a](java&Tab;script:alert(1))",
                 "[a](java&NewLine;script:x)", "[a](java&#9;script:x)", "[a]( JavaScript:x)", "[a](java&#115;cript:x)",
                 "[e](data:text/html;base64,AAA)", "[r](file:///etc/passwd)", "[v](vbscript:x)"):
        out = S.neutralize_links(evil)
        assert "#blocked" in out and "script:" not in out.replace("&#", "").casefold().replace("java", ""), (evil, out)
    t = S.neutralize_links("[c](https://example.org) ![i](data:image/png;base64,AAA) <javascript:alert(1)>\n")
    assert "[c](https://example.org)" in t and "![i](data:image/png;base64,AAA)" in t and "<javascript" not in t, t
    page = ('<a href="javascript:x">a</a> <a href=\'java&#115;cript:x\'>b</a> <a href="jav&Tab;ascript:x">c</a> '
            '<img src="data:image/png;base64,AA"> <a href="https://e.org">d</a> <a href="data:text/html,x">e</a>')
    fixed = S.scrub_html(page)
    assert fixed.count('href="#blocked"') == 4 and 'src="data:image/png;base64,AA"' in fixed and 'href="https://e.org"' in fixed, fixed
    # то, что в самом деле выходит из pandoc, чисто (если pandoc есть на машине)
    pandoc = S.find_bin("pandoc")
    if pandoc:
        md = S.escape_raw_html(S.neutralize_links("# T\n\n[a [b]](javascript:alert(1)) [x][1] <br> <script>y()</script>\n\n[1]:\njavascript:alert(2)\n"))
        cp = subprocess.run([pandoc, "-f", "gfm", "-t", "html", "--sandbox"], input=md, text=True, encoding="utf-8",
                            capture_output=True, timeout=120)
        assert cp.returncode == 0 and "javascript:" not in S.scrub_html(cp.stdout) and "<script" not in cp.stdout, cp.stdout
    started = time.process_time()
    for bad in ("[a](" * 100000, "[a](" + " " * 100000, "<javascript:" * 100000, "[" * 200000, "](" * 100000):
        S.neutralize_links(bad)
        S.scrub_html(bad)
        S.strip_images(bad)
    assert time.process_time() - started < 5, "разбор ссылок стал квадратичным"
    h = S.escape_raw_html("# T\n\n<script>x()</script>\n\nтекст `<b>` и <b onclick=x>ж</b> <https://example.org/a> "
                          "<me@example.org>\n\n```html\n<script>в коде</script>\n```\n\n<!-- c -->\n")
    assert "&lt;script>x()&lt;/script>" in h and "&lt;b onclick=x>ж</b>" in h and "&lt;!-- c -->" in h, h
    assert "`<b>`" in h and "<script>в коде</script>" in h and "<https://example.org/a>" in h and "<me@example.org>" in h, h
    # теги без атрибутов, которые ничего не исполняют (<br> в ячейке таблицы Confluence), не превращаются в видимый текст
    assert S.escape_raw_html("a<br>b H<sub>2</sub>O <kbd>Ctrl</kbd> <br/> <BR />") == "a<br>b H<sub>2</sub>O <kbd>Ctrl</kbd> <br/> <BR />"
    assert S.escape_raw_html("<br onclick=x> <b style='x'> <img src=x> <a href=y>") == "&lt;br onclick=x> &lt;b style='x'> &lt;img src=x> &lt;a href=y>"
    assert S.strip_images("до ![схема](a.png) и <img src=x> после") == "до схема и  после"
    started = time.process_time()
    for bad in ("<a " * 100000 + "`" * 50000, ("`" * 15 + "a ") * 30000, "<a `x" * 30000):
        S.escape_raw_html(bad)
    assert time.process_time() - started < 5, "разбор сырого HTML стал квадратичным"


@test
def test_gate_and_scanners_survive_hostile_text_and_see_what_the_boundary_hides(tmp: Path):
    """Незакрытые ограждения и длинные цепочки точек не вешают предохранитель и сканер ПДн; заголовок производства выше
    границы заметен (предупреждение, не блок: «Допущения» бывают содержанием); BOM в начале файла не прячет `status: draft`."""
    import time
    import aurora_common as C
    import kb_scrub
    started = time.process_time()
    C._without_code("```a\n" * 50000)
    C._without_code(("```\n~~~\n" * 20000))
    kb_scrub.scan("a." * 20000)
    kb_scrub.scan("x" * 100 + "@" + "b." * 20000)
    assert time.process_time() - started < 10, "разбор текста стал квадратичным"
    assert C._without_code("до\n```\n{{x}}\n```\nпосле {{y}} `{{z}}`") == "до\n \nпосле {{y}}  "
    assert "{{x}}" in C._without_code("```\n{{x}}\n"), "незакрытое ограждение остаётся текстом, как прежде"
    assert kb_scrub.scan("пишите ivan.petrov@example.org срочно")[0][0] == "почта"
    above = _doc(body=CLEAN + "\n## Допущения\n\nвходит в содержание\n\n" + MARK + "\n" + KITCHEN)
    found = C.outward_gate(above, None)
    assert [(lv, c) for lv, c, _m in found] == [("warn", "production-heading")], found
    assert C.gate_blocked(found, []) == []
    root = _publish_project(tmp)
    doc = root / "Artifacts" / "reports" / "итог.md"
    doc.write_bytes(b"\xef\xbb\xbf" + _doc(head="type: deliverable\nstatus: draft\n").encode("utf-8"))
    cp = run("publish_doc.py", "Artifacts/reports/итог.md", "--apply", cwd=root)
    assert cp.returncode == 2 and "[draft]" in cp.stdout, (cp.returncode, cp.stdout, cp.stderr)
    assert [c for lv, c, _m in C.outward_gate("\ufeff" + _doc(head="status: draft\n"), None) if lv == "block"] == ["draft"]
    # снимок, сохранённый редактором с BOM и правленный после передачи, хеш всё равно ловит
    rel = root / "Deliverables" / "released"
    rel.mkdir(parents=True, exist_ok=True)
    good = _doc(head='type: deliverable\nsha256: "%s"\n' % C.release_hash(_doc(body=CLEAN)), body=CLEAN)
    (rel / "A_2026-10-19.md").write_bytes(b"\xef\xbb\xbf" + good.encode("utf-8"))
    assert C.released_problems(str(root)) == [], C.released_problems(str(root))
    (rel / "A_2026-10-19.md").write_bytes(b"\xef\xbb\xbf" + good.replace("пять", "три").encode("utf-8"))
    assert len(C.released_problems(str(root))) == 1


@test
def test_find_by_title_does_not_let_the_space_add_query_parameters(_t):
    """Пространство из `publish_parent` шапки уходит в запрос кодированным: `A&limit=1` не добавляет параметр."""
    import confluence_export as CE
    import publish_doc as P
    for cls in (P.Writer, CE.Api):
        asked = []
        api = object.__new__(cls)
        api.get = lambda path, _a=asked: _a.append(path) or {}
        (api.find_by_title if cls is P.Writer else api.by_title)("A&limit=1", "Т")
        assert asked and "spaceKey=A%26limit%3D1&" in asked[0] and "spaceKey=A&limit" not in asked[0], asked


@test
def test_release_recognises_the_same_binary_under_another_case_of_its_path(tmp: Path):
    """`--binary` с другим регистром пути к уже сданному файлу — тот же файл, а не `SameFileError` посреди фиксации."""
    import ship_doc as S
    folder = tmp / "Deliverables" / "released"
    folder.mkdir(parents=True)
    f = folder / "built.docx"
    f.write_bytes(b"x")
    other = tmp / "deliverables" / "released" / "built.docx"
    assert S._same_file(str(f), str(f)) and not S._same_file(str(f), str(tmp / "нет.docx"))
    if other.exists():                   # macOS и Windows: ФС не различает регистр
        assert S._same_file(str(f), str(other)), "тот же файл принят за другой"


@test
def test_panel_does_not_send_allow_to_a_project_script_that_predates_the_guard(tmp: Path):
    """Проект не обновлён после выхода кита: старый `publish_doc.py` флага `--allow` не знает и отверг бы запуск — панель его не шлёт."""
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    old = tmp / "old.py"
    old.write_text('ap.add_argument("--apply")\nap.add_argument("--allow-dirty")\n', encoding="utf-8")
    new = tmp / "new.py"
    new.write_text('ap.add_argument("--allow", action="append")\n', encoding="utf-8")
    args = ["a.md", "--apply", "--allow", "no-marker", "--allow", "draft"]
    assert ck._args_for_old_script(str(old), args) == ["a.md", "--apply"]
    assert ck._args_for_old_script(str(new), args) == args
    assert ck._args_for_old_script(str(tmp / "нет.py"), args) == args
    assert ck._args_for_old_script(str(old), ["a.md", "--apply"]) == ["a.md", "--apply"]


@test
def test_setup_form_keeps_the_publish_parent(tmp: Path):
    """`confluence.publish_parent` (родитель `ship:publish`) форма настроек не стирает: ни номер, ни адрес; повтор — тот же текст."""
    S = importlib.import_module("aurora_setup")
    importlib.reload(S)
    c = {"name": "Demo", "slug": "demo", "conf_url": "https://wiki.example.com", "conf_space": "S",
         "jira_url": "", "jira_key": "D", "jira_jql": "project = D", "trust_statuses": "Done",
         "assumption_statuses": "Open", "scrub": "off", "sync_roots": [],
         "trusted_sources": "", "trusted_branches": "", "threshold": "70"}
    for n, value in enumerate(("9001", "https://wiki.example.com/display/S/Родитель%20публикации")):
        cfg = tmp / f"p{n}" / "aurora.config.yaml"
        cfg.parent.mkdir()
        cfg.write_text('atlassian:\n  confluence:\n    base_url: "https://wiki.example.com"\n    space: "S"\n'
                       f'    publish_parent: "{value}"\n    sync_roots: []\n'
                       '  jira:\n    base_url: ""\n', encoding="utf-8")
        S.write_config(cfg, dict(c))
        first = cfg.read_text(encoding="utf-8")
        assert f'    publish_parent: "{value}"\n' in first, first
        S.write_config(cfg, dict(c))
        assert cfg.read_text(encoding="utf-8") == first, "повторное сохранение изменило конфиг"
    plain = tmp / "plain" / "aurora.config.yaml"
    plain.parent.mkdir()
    S.write_config(plain, dict(c))
    assert "publish_parent" not in plain.read_text(encoding="utf-8"), "форма сама выдумала родителя"
