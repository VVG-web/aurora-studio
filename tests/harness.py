"""Каркас тестов движка: регистрация проверок, помощники, заготовки проектов.

Проверки лежат в tests/cases/part_NN.py, запускает их tests/run_tests.py.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
import sys
import tempfile
import time
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
# Кухня разработки — `Development/` кита, вне git. Проверки, которым она нужна, на чистой
# копии кита (в том числе в проверке GitHub) сверяют только то, что едет в git.
KITCHEN = KIT / "Development" / "QA"


def _git_author_for_fixtures() -> None:
    """Автор коммитов в заготовках — если на машине он не задан.

    У разработчика имя и почта лежат в `~/.gitconfig`; на чистой машине (проверка GitHub)
    их нет, и каждый `git commit` в заготовке падал кодом 128 «Please tell me who you are».
    Глобальный конфиг подставляется временный: у настройки самого репозитория приоритет
    выше, и тесты, задающие своё имя, остаются при нём.

    Автор — только человек, никогда не имя программы или модели (правило Авроры для любого
    авторства). Здесь это тот, кто запустил проверку: на GitHub — автор push
    (`GITHUB_ACTOR`), на машине — пользователь системы.
    """
    have = subprocess.run(["git", "config", "--get", "user.email"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
    if have or os.environ.get("GIT_CONFIG_GLOBAL"):
        return
    import getpass
    who = os.environ.get("GITHUB_ACTOR") or getpass.getuser() or "user"
    mail = (f"{who}@users.noreply.github.com" if os.environ.get("GITHUB_ACTOR")
            else f"{who}@localhost")
    cfg = Path(tempfile.mkdtemp(prefix="aurora-tests-git-")) / "gitconfig"
    cfg.write_text(f"[user]\n\tname = {who}\n\temail = {mail}\n", encoding="utf-8")
    os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)


_git_author_for_fixtures()
SCRIPTS = KIT / "scripts"
VERBOSE = "-v" in sys.argv
RESULTS: list = []
REGISTRY: list = []

# Прогон не читает личный конфиг машины. Без этого тест, объявивший один бэкенд с окном
# в 8 000, видел ещё три из `.env.aurora.local` разработчика: `prompt_budget` берёт самое
# широкое окно кольца и возвращал чужие 200 000. Такой прогон зелёный или красный в
# зависимости от того, чья машина его запустила, — и один релиз уже вышел с красным.
# Тесты, которым нужно кольцо из нескольких бэкендов, объявляют его сами через окружение:
# слой окружения изоляция не трогает.
os.environ["AURORA_TESTS_ISOLATED"] = "1"
for _k in [k for k in os.environ if k.startswith("AURORA_AGENT_BACKEND_")]:
    del os.environ[_k]


# ------------------------------------------------------------------ каркас

def run(script: str, *args, cwd: Path, expect_rc=None) -> subprocess.CompletedProcess:
    cp = subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                        cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if VERBOSE:
        print(f"    $ {script} {' '.join(args)} → rc={cp.returncode}")
        print("      " + "\n      ".join(cp.stdout.strip().splitlines()[:12]))
    if expect_rc is not None and cp.returncode != expect_rc:
        raise AssertionError(f"{script} {' '.join(args)}: rc={cp.returncode}, ждали {expect_rc}\n"
                             f"{cp.stdout}\n{cp.stderr}")
    return cp


def ui_source() -> str:
    """Страница панели целиком: оболочка index.html и подставляемые в неё panel.css/panel.js.

    Тем же правилом, каким её собирает сервер (`aurora_cockpit.ui_source`): проверка
    «в панели есть X» не должна зависеть от того, в каком из файлов X лежит.
    """
    folder = KIT / "cockpit" / "ui"
    html = (folder / "index.html").read_text(encoding="utf-8")
    return re.sub(r"^<!--@include ([\w.-]+)-->\n",
                  lambda m: (folder / m.group(1)).read_text(encoding="utf-8"),
                  html, flags=re.M)


def panel_sources() -> str:
    """Панель целиком: ядро плюс все модули.

    Раздел панели переехал из одного файла в папку (`cockpit/modules/<id>/`), и проверка
    «панель умеет X» обязана смотреть туда же. Иначе она ловит переезд вместо дефекта:
    строка ушла в каталог модуля, а тест говорит, что возможность пропала.

    По той же причине сюда входят и каталоги строк ядра (`cockpit/i18n/*.json`): надпись
    живёт там, а не в коде, и проверка «человеку сказано то-то» обязана читать её оттуда.
    """
    parts = [ui_source()]
    lang = KIT / "cockpit" / "i18n"
    if lang.is_dir():
        for f in sorted(lang.glob("*.json")):
            parts.append(f.read_text(encoding="utf-8"))
    mods = KIT / "cockpit" / "modules"
    if mods.is_dir():
        for f in sorted(mods.rglob("*")):
            if f.suffix in (".html", ".js", ".json") and f.is_file():
                parts.append(f.read_text(encoding="utf-8"))
    return "\n".join(parts)


def stub_messages(messages, kw):
    """Сообщения так, как собрал бы их `call_role`. Для заглушек вместо `call`.

    Часть вызовов передаёт не готовые сообщения, а `trim=(весь текст, собрать)`: текст
    режется под окно того бэкенда, который возьмёт запрос, и собирается уже внутри
    `call_role`. Заглушка, читающая `messages[0]`, на таком вызове видит пустой список —
    и падает по индексу вместо того, чтобы проверить промпт. Один помощник на все
    заглушки: перенос очередного вызова на `trim` не должен ронять чужой тест.
    """
    trim = kw.get("trim")
    if trim:
        whole, build = trim
        return build(whole)
    return messages


def make_project(tmp: Path, git: bool = False) -> Path:
    """Пустой проект со стандартной структурой (из structure_dirs.txt) и движком."""
    root = tmp / "project"
    root.mkdir()
    for line in (KIT / "structure_dirs.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            (root / line).mkdir(parents=True, exist_ok=True)
    (root / ".aurora" / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy(KIT / "structure_dirs.txt", root / ".aurora" / "structure_dirs.txt")
    for s in SCRIPTS.glob("*.py"):
        shutil.copy(s, root / ".aurora" / "scripts" / s.name)
    # модули источников: манифесты и папки их зеркал (в проекте это делает install/update)
    (root / ".aurora" / "connectors").mkdir(parents=True, exist_ok=True)
    for man in (KIT / "connectors").glob("*/connector.json"):
        m = json.loads(man.read_text(encoding="utf-8"))
        shutil.copy(man, root / ".aurora" / "connectors" / f"{m['id']}.json")
        (root / m["mirror"]["default_path"]).mkdir(parents=True, exist_ok=True)
    (root / ".aurora" / "skills" / "aurora-vault").mkdir(parents=True, exist_ok=True)
    (root / ".aurora" / "skills" / "aurora-vault" / "SKILL.md").write_text("stub", encoding="utf-8")
    (root / "aurora.config.yaml").write_text(
        'project:\n  name: "Test"\n  slug: "Test"\natlassian:\n  confluence:\n'
        '    space: "T"\n  jira:\n    project_key: "T"\n', encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
        subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "init"], cwd=str(root), check=True)
    return root


# Раздел → тип карточки: тот же список, что в kb_lint. Фикстура без `type:` — это
# карточка, на которую линтер справедливо ругается, а не «обычная карточка».
SECTION_TYPE = {
    "Concepts": "concept", "Processes": "process", "Glossary": "glossary",
    "Systems": "system", "Roles": "role", "Statuses": "status-model",
    "Reference": "reference", "Requirements": "requirement", "Specs": "spec",
    "Decisions": "decision", "Questions": "question", "MOC": "moc",
}


def card(root: Path, rel: str, body: str = "", **fm) -> Path:
    p = root / "AuroraKnowledgeDB" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    fm.setdefault("type", SECTION_TYPE.get(rel.split("/")[0], "concept"))
    head = "".join(f"{k}: {v}\n" for k, v in fm.items())
    p.write_text(f"---\ntitle: \"{p.stem}\"\n{head}---\n\n# {p.stem}\n\n{body}\n", encoding="utf-8")
    return p


def card_srcs(text: str) -> list:
    """Источники карточки — тем же чтением, каким живёт движок.

    Проверять провенанс строкой `source: "..."` нельзя: запись у него менялась (одно поле
    → список), и тест ловил бы формат вместо смысла. Читаем так же, как читает движок.
    """
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    return importlib.import_module("aurora_common").card_sources(text)


def count_cards(root: Path) -> int:
    return len(list((root / "AuroraKnowledgeDB").rglob("*.md")))


# Прогон одной проверки. Без него любая правка одной вещи стоила полного прогона на
# несколько минут — и проверялась поэтому реже, чем следовало.
ONLY = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--only=")),
            (sys.argv[sys.argv.index("--only") + 1]
             if "--only" in sys.argv and len(sys.argv) > sys.argv.index("--only") + 1 else ""))


def why(e: BaseException) -> str:
    """Провал без пояснения — всё равно провал.

    `assert x` без текста даёт пустую строку. Она уходила в RESULTS как есть, а сводка
    считала провалом только непустое (`if e`): тест печатал ❌, засчитывался пройденным
    и прогон возвращал 0. Красное читалось зелёным — и так уехал целый релиз.
    """
    return str(e) or "(без пояснения — добавьте текст в assert)"


# Инварианты — source-scan проверки: читают текст кода движка (scripts/*.py) через
# `.read_text(`, потому регрессия в этих файлах ловится ТОЛЬКО их прогоном. Любой
# `--only=X` обязан их гонять (урок T5: литерал сломал source-scan тест, его поймал
# только полный прогон). Список зафиксирован по точкам чтения, каждая проверка <2 с.
INVARIANTS = frozenset({
    "build can run the whole plan overnight",
    "ask tab names the model and lets you pick it",
    "console names the model and the speed",
    "night run waits out a dropped connection",
    "oversized request does not kill the provider",
    "long source is not silently cut",
    "the agent can hold a conversation and use tools",
    "width probe measures work not noise",
    "section is the type written as a folder",
    "adapter answers every caller in parallel",
    "console says which step uses the threads",
})


def select_tests(only: str = "", smoke: bool = False, no_invariants: bool = False, shard=None):
    """Выборка (display_name, fn, is_invariant) в порядке регистрации.

    `shard=(i, n)` — каждая n-я проверка, начиная с i-й; инварианты остаются только в первой доле,
    чтобы все доли вместе прогоняли каждую проверку ровно один раз.
    """
    chosen = _select(only, smoke, no_invariants)
    if shard and not smoke:
        i, n = shard
        if not (n >= 1 and 1 <= i <= n):
            raise SystemExit(f"--shard={i}/{n}: доля вне диапазона")
        rest = [c for c in chosen if not c[2]]
        chosen = ([c for c in chosen if c[2]] if i == 1 else []) + \
                 [c for k, c in enumerate(rest) if k % n == i - 1]
    return chosen


def _select(only: str, smoke: bool, no_invariants: bool):
    chosen = []
    for name, fn in REGISTRY:
        is_inv = name in INVARIANTS
        if smoke:
            if is_inv:
                chosen.append((name, fn, True))
        elif only:
            if only.lower() in name.lower():
                chosen.append((name, fn, is_inv))
            elif is_inv and not no_invariants:
                chosen.append((name, fn, True))
        else:
            if no_invariants and is_inv:
                continue
            chosen.append((name, fn, is_inv))
    return chosen


def test(fn):
    """Регистрация проверки (исполняет драйвер после импорта, перед main())."""
    name = fn.__name__.replace("test_", "").replace("_", " ")
    REGISTRY.append((name, fn))
    return fn


# ------------------------------------------------------------------- тесты

def _write_minimal_docx(path: Path) -> None:
    """Минимальный настоящий .docx (zip с document.xml) — без внешних зависимостей."""
    import zipfile
    path.parent.mkdir(parents=True, exist_ok=True)
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    doc = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>'
           f'<w:p><w:r><w:t>Тестовый абзац</w:t></w:r></w:p></w:body></w:document>')
    ct = ('<?xml version="1.0" encoding="UTF-8"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
          'officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)


def term_regex(terms: list):
    """Приватные названия — по границам слова, а не по подстроке.

    Короткое внутреннее название нередко оказывается началом обычного русского слова —
    и тогда защита ловит живую фразу вместо утечки. Ложное срабатывание хуже пропуска:
    его обходят, переписывая нормальный текст, и защита превращается в помеху, которую
    учатся игнорировать. Границей слова считаем букву или цифру, поэтому имя файла с
    подчёркиванием или дефисом по-прежнему ловится.

    Но русское название склоняется, и в списке оно стоит основой. Граница справа эту
    основу и убивала: основа «Примор» не ловила «Приморья» — название заказчика прошло
    проверку зелёным и было поймано глазами перед самой отправкой. Поэтому основа
    объявляется явно, звёздочкой на конце: `Примор*` ловит любое продолжение слова.
    Угадывать, где основа, а где целое слово, проверка не вправе — это решение того,
    кто ведёт список.
    """
    body = "|".join(re.escape(t.rstrip("*")) + ("" if t.endswith("*")
                                                else "(?![0-9A-Za-zА-Яа-яЁё])")
                    for t in terms)
    return re.compile(rf"(?<![0-9A-Za-zА-Яа-яЁё])(?:{body})", re.I)


def _js_function(ui: str, head: str) -> str:
    """Тело функции верхнего уровня из index.html: до первой закрывающей скобки в нулевом столбце."""
    start = ui.index(head)
    return ui[start:ui.index("\n}\n", start)]


def _write_embed_index(root: Path, names: list, vecs: list, with_pf: bool):
    """Индекс эмбеддингов с известными векторами в проекте (bin v2 + json-карта).

    → массив векторов, реально сохранённых: в файле float32, и точные оценки оракул
    должен считать с него, а не с двойных из фикстуры.
    """
    import array
    import importlib

    sys.path.insert(0, str(KIT / "scripts"))
    E = importlib.import_module("kb_embed")
    dim = len(vecs[0])
    out = array.array("f")
    cards = {}
    for i, (nm, v) in enumerate(zip(names, vecs)):
        cards[nm] = {"hash": E.digest(nm), "row": i}
        out.extend(v)
    pf = E.build_prefilter(out, dim, len(cards)) if with_pf else None
    (root / "AuroraKnowledgeDB" / "meta").mkdir(parents=True, exist_ok=True)
    old_cwd = os.getcwd()
    try:
        os.chdir(str(root))
        E.save_index("bge-m3", dim, cards, out, pf)
    finally:
        os.chdir(old_cwd)
    return out


def _embed_search(root: Path, qv: list, limit: int):
    """E.search в проекте с контролируемым вектором запроса — без сети. → (E, выдача)."""
    import importlib
    from unittest.mock import patch

    sys.path.insert(0, str(KIT / "scripts"))
    E = importlib.import_module("kb_embed")
    old_cwd = os.getcwd()
    try:
        os.chdir(str(root))
        with patch.object(E, "embed", return_value=[qv]):
            return E, E.search("запрос", {"backends": [], "request_timeout": 5},
                               "bge-m3", limit=limit)
    finally:
        os.chdir(old_cwd)


def _mirror_page(root: Path, rel: str, pid: int, text: str) -> str:
    """Файл зеркала Confluence с шапкой, как её пишет синк. → путь от корня проекта."""
    import hashlib
    p = root / "Sources/Confluence" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5(text.encode("utf-8")).hexdigest()[:16]
    crumbs = rel.rsplit("/", 1)[0]
    p.write_text(f"---\npage_id: {pid}\ntitle: \"x\"\nbreadcrumbs: \"{crumbs}\"\n"
                 f"content_hash: {h}\n---\n\n# x\n\n{text}\n", encoding="utf-8")
    return "Sources/Confluence/" + rel


def _sync_state(root: Path, rows: list) -> None:
    """Состояние синка: [(page_id, путь в зеркале)] — то, что нашёл последний обход."""
    body = "".join(f"| {i} | {pid} | x | {rel} | SYNCED |\n" for i, (pid, rel) in enumerate(rows, 1))
    (root / "Sources/Confluence/sync_state.md").write_text(
        "**Sync Date:** 2026-09-24\n\n| # | Page ID | Title | Local Path | Status |\n"
        "|---|---|---|---|---|\n" + body, encoding="utf-8")


def _quoted_card(root: Path, rel: str, sources: list, blocks: list, extra: str = "") -> Path:
    """Карточка с разделом дословного текста: blocks = [(подпись или None, текст)]."""
    p = root / "AuroraKnowledgeDB" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    src = "".join(f'  - "{x}"\n' for x in sources)
    quoted = "\n\n".join((f"### {k}\n\n{t}" if k else t) for k, t in blocks)
    p.write_text(f"---\ntitle: \"{p.stem}\"\ntype: process\nsources:\n{src}{extra}---\n\n"
                 f"Тезис карточки.\n\n## Источник (перенесено дословно)\n\n{quoted}\n\n"
                 "## История изменений\n\n- 2026-09-01: создана\n", encoding="utf-8")
    return p


def set_home(path):
    """Подменить домашнюю папку на время проверки. → функция, возвращающая прежнюю.

    `os.path.expanduser("~")` читает `HOME` на POSIX и `USERPROFILE` на Windows: проверка,
    выставившая одну `HOME`, на Windows писала в настоящий профиль того, кто её запустил,
    и не находила файлов, положенных в подменённый.
    """
    names = ("HOME", "USERPROFILE")
    saved = {k: os.environ.get(k) for k in names}
    for k in names:
        os.environ[k] = str(path)

    def restore():
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restore


def _cockpit_on(kit: Path):
    """Модуль панели, направленный на пробный кит: KIT, список прогонов и дом — во временной папке."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    saved = (ck.KIT, ck.RUNNING, ck._http_get, dict(ck.CACHE))
    ck.KIT, ck.RUNNING = str(kit), str(kit / "running.json")
    ck.CACHE.pop("kit_status", None)
    restore_home = set_home(kit.parent / "home")

    def restore():
        ck.KIT, ck.RUNNING, ck._http_get = saved[0], saved[1], saved[2]
        restore_home()
        ck.CACHE.clear()
        ck.CACHE.update(saved[3])
    return ck, restore


def _review_module():
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    rr = importlib.import_module("review_run")
    importlib.reload(rr)
    return rr


def _review_checklist(rr):
    return rr.load_checklist(str(KIT / "scaffold/TemplatesCommon/review_v2.0.md"))


class _FakeReader:
    """Confluence без сети: страницы из словаря, номер — ключ."""

    def __init__(self, pages: dict, as_of: str = ""):
        self.pages, self.as_of, self.base = pages, as_of, "https://wiki"

    def page(self, pid):
        return dict(self.pages.get(pid) or {"id": pid, "status": "missing"})

    def resolve(self, kind, target):
        if kind == "id":
            return target, "ok"
        return "", "missing"


def _answer(cl, profile, override=None):
    """Ответ модели: все вопросы «yes», кроме переопределённых."""
    checks = {c["id"]: {"v": "yes", "evidence": "", "fix": ""}
              for c in cl["profiles"][profile] if c["decider"] != "код"}
    for cid, v in (override or {}).items():
        checks[cid] = v
    return json.dumps({"profile": profile, "checks": checks, "split": ""}, ensure_ascii=False)


SMOKE = "--smoke" in sys.argv
NO_INVARIANTS = "--no-invariants" in sys.argv
# Доля набора: `--shard=2/3` — вторая треть. На Windows полный прогон идёт вчетверо дольше, чем на
# Linux, а выпуск ждёт все проверки; три параллельные доли возвращают время, которое отнимает ОС.
SHARD = next((tuple(int(x) for x in a.split("=", 1)[1].split("/")) for a in sys.argv
              if a.startswith("--shard=")), None)

TEMPLATE_PAR = ("При правках стори после прохождения ревью ОБЯЗАТЕЛЬНО писать комментарий "
                "при сохранении страницы - что поменялось (около кнопки SAVE)")
TEMPLATE_ROW = "| RYo:свойствоАвтор изменений | @Иванова Анна Петровна |"
TEMPLATE_PAR2 = ("Указать название страницы: код RY и название алгоритма, как принято в разделе "
                 "требований к оформлению страниц проекта; код берётся из реестра требований")


def _template_mirror(root: Path, n: int = 22) -> list:
    """Зеркало вики, где у каждой страницы — шаблон, строка истории и своё знание."""
    base = root / "Sources/Confluence/Алгоритмы"
    base.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        p = base / f"ALG-{100 + i:03d}_Алгоритм_{i}.md"
        p.write_text(
            f"---\npage_id: {5000 + i}\ntitle: \"ALG-{100 + i} Алгоритм {i}\"\n---\n"
            f"# ALG-{100 + i} Алгоритм {i}\n\n# Описание\n\n{TEMPLATE_PAR}\n\n{TEMPLATE_PAR2}\n\n"
            f"| Код | RYk:ALG-{100 + i} |\n|---|---|\n{TEMPLATE_ROW}\n"
            f"| RYo:свойствоОписание | Алгоритм {i} считает сумму налога по ставке {i} процентов |\n\n"
            f"# Шаги алгоритма\n\nШаг первый: взять ставку {i}. Шаг второй: умножить на базу "
            f"и записать результат в журнал расчётов номер {i}.\n", encoding="utf-8")
        paths.append(p)
    return paths
