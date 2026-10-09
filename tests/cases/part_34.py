"""Проверки движка Aurora, часть 34: обращение на GitHub из панели — баг и предложение
(1.169.0).

Каркас и помощники — tests/harness.py. В репозитории кита есть формы обращений
(`.github/ISSUE_TEMPLATE/`): «Баг» и «Предложение». Панель открывает их уже заполненными:
кнопка «Баг» в шапке каждой страницы, обе формы — в «О проекте». Поля формы заполняются по
их `id` из адреса, вариант списка — только точным совпадением с подписью. Тест держит
кнопку и форму вместе: переименованное поле или вариант иначе молча перестанут
заполняться.
"""
from __future__ import annotations

from pathlib import Path
import json
import re
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

from harness import KIT, _js_function, test, ui_source  # noqa: F401

COCKPIT = KIT / "cockpit"
FORMS = KIT / ".github" / "ISSUE_TEMPLATE"


def _form(name: str) -> dict:
    """Поля формы обращения: id → варианты списка (у поля без списка — пустой список)."""
    fields, cur, in_opts = {}, None, False
    for line in (FORMS / name).read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):      # пометки списка версий: versions:begin/end
            continue
        m = re.match(r"\s+id:\s*(\S+)", line)
        if m:
            cur, in_opts = m.group(1), False
            fields[cur] = []
            continue
        if re.match(r"\s+options:\s*$", line):
            in_opts = True
            continue
        m = re.match(r"\s+-\s+(?:label:\s*)?(.+?)\s*$", line)
        if in_opts and cur and m and not line.lstrip().startswith("- type:"):
            fields[cur].append(m.group(1).strip('"'))
        elif in_opts and not re.match(r"\s+-\s", line):
            in_opts = False
    return fields


def _issue_url(tmp: Path, kind: str, kit: dict, view: str = "health") -> str:
    """Адрес обращения, собранный самой функцией панели (node или deno)."""
    ui = ui_source()
    area = re.search(r"^const ISSUE_AREA = .*$", ui, re.M).group(0)
    js = "\n".join([
        "const UI_VERSION = '9.9.9';",
        "const I18N = {'issue.context': 'section {section} ({view}) ui {ui} {lang} {browser}'};",
        "function t(k, v){ let s = I18N[k] || k; for (const [a,b] of Object.entries(v||{}))"
        " s = s.replaceAll('{'+a+'}', b); return s; }",
        "const $ = () => ({textContent: ' Здоровье '});",
        f"const S = {{view: {json.dumps(view)}, lang: 'ru', state: {{kit: {json.dumps(kit)}}},"
        " project: {path: '/home/me/секрет-проект', name: 'Секрет'}};",
        "globalThis.navigator = globalThis.navigator || {userAgent: 'Test/1.0'};",
        area,
        _js_function(ui, "function viewLabel(") + "\n}",
        _js_function(ui, "function issueUrl(") + "\n}",
        f"console.log(issueUrl({json.dumps(kind)}));",
    ])
    engine = shutil.which("node") or shutil.which("deno")
    assert engine, "нет ни node, ни deno — адрес обращения проверить нечем"
    src = tmp / "issue.js"
    src.write_text(js, encoding="utf-8")
    cmd = [engine, str(src)] if Path(engine).stem.lower() == "node" else [engine, "run", str(src)]
    cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, cp.stderr[:800]
    return cp.stdout.strip()


@test
def test_the_bug_button_fills_the_kit_bug_form(tmp: Path):
    """«Баг» открывает форму кита «Баг» с разделом, версией, ОС и Python: каждое поле есть
    в форме, а варианты списков совпадают с её подписями. Пути и имени проекта в адресе нет:
    репозиторий открытый."""
    form = _form("bug_report.yml")
    kit = {"version": "1.169.0", "repo": "https://github.com/acme/aurora-studio/",
           "os": "macOS", "python": "3.12.4", "path": "/home/me/kit"}
    url = _issue_url(tmp, "bug", kit)
    u = urlparse(url)
    assert (u.netloc, u.path) == ("github.com", "/acme/aurora-studio/issues/new"), url
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert q["template"] == "bug_report.yml" and (FORMS / q["template"]).is_file(), q
    for field in ("area", "version", "os", "python", "extra"):
        assert field in q and field in form, f"поле {field}: в адресе или в форме его нет"
    assert q["area"] in form["area"], f"вариант «{q['area']}» в форме не найден: {form['area']}"
    assert q["os"] in form["os"] and q["version"] == "1.169.0" and q["python"] == "3.12.4", q
    assert form["version"] and all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in form["version"][:-1]), \
        f"варианты версии в форме не голые X.Y.Z — версия кита не выберется: {form['version']}"
    assert "Здоровье" in q["extra"] and "(health)" in q["extra"] and "9.9.9" in q["extra"], q
    assert "секрет" not in url.lower() and "/home/me" not in url, "в обращение ушёл путь проекта"


@test
def test_the_feature_button_opens_the_feature_form(tmp: Path):
    """«Предложить улучшение» — форма «Предложение» с версией, ОС и разделом, откуда
    нажали; без известного репозитория — репозиторий кита по умолчанию."""
    form = _form("feature_request.yml")
    url = _issue_url(tmp, "feature", {"version": "1.169.0", "os": "Linux", "python": "3.12.4"},
                     view="about")
    u = urlparse(url)
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert u.path == "/VVG-web/aurora-studio/issues/new", url
    assert q["template"] == "feature_request.yml" and (FORMS / q["template"]).is_file(), q
    assert set(q) - {"template"} <= set(form), f"полей {set(q) - set(form)} в форме нет"
    assert "(about)" in q["extra"] and q["version"] == "1.169.0" and q["os"] in form["os"], q
    assert "python" not in q and "area" not in q, "в «Предложение» ушли поля, которых в нём нет"


@test
def test_the_server_tells_os_python_and_repo_but_no_paths(tmp: Path):
    """Сервер отдаёт для формы адрес репозитория кита (форк — свой), ОС подписью из формы и
    версию Python; ОС, которой в форме нет, остаётся пустой — человек выберет сам."""
    if str(COCKPIT) not in sys.path:
        sys.path.insert(0, str(COCKPIT))
    import aurora_cockpit as ck
    saved = (ck.kit_repo, ck.platform.system, dict(ck.CACHE))
    form_os = _form("bug_report.yml")["os"]
    try:
        ck.kit_repo = lambda: ("acme/fork", "master", "https://github.com/acme/fork")
        for system, want in (("Darwin", "macOS"), ("Windows", "Windows"), ("Linux", "Linux"),
                             ("FreeBSD", "")):
            ck.CACHE.pop("issue_facts", None)
            ck.platform.system = lambda s=system: s
            facts = ck.issue_facts()
            assert facts["os"] == want and facts["repo"] == "https://github.com/acme/fork", facts
            assert set(facts) == {"repo", "os", "python"}, facts
            assert not want or want in form_os, f"ОС «{want}» нет среди вариантов формы {form_os}"
    finally:
        ck.kit_repo, ck.platform.system = saved[0], saved[1]
        ck.CACHE.clear()
        ck.CACHE.update(saved[2])
    src = (COCKPIT / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"path": KIT, **issue_facts()}' in src, "состояние панели не несёт данных для формы"


@test
def test_every_page_has_the_bug_button_and_about_has_both(tmp: Path):
    """Кнопка «Баг» — в шапке, значит, на каждой странице; «О проекте» — обе формы и список
    открытых обращений, с предупреждением не вставлять данные клиентов."""
    ui = ui_source()
    assert 'id="bugBtn"' in ui and '$("#bugBtn").onclick = () => openIssue("bug")' in ui
    assert "openIssue, issueUrl}" in ui, "разделам не отдана функция обращения"
    about = (COCKPIT / "modules" / "about" / "view.js").read_text(encoding="utf-8")
    assert 'ctx.ui.openIssue("bug")' in about and 'ctx.ui.openIssue("feature")' in about
    for lang in ("ru", "en"):
        cat = json.loads((COCKPIT / "modules" / "about" / "i18n" / f"{lang}.json")
                         .read_text(encoding="utf-8"))
        for key in ("about.feedback", "about.report_bug", "about.request_feature",
                    "about.feedback_privacy"):
            assert cat.get(key), f"{lang}: нет строки {key}"
