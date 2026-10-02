"""Проверки движка Aurora, часть 7 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import time

from harness import (  # noqa: F401
    ui_source,
    KIT,
    KITCHEN,
    SCRIPTS,
    card,
    make_project,
    panel_sources,
    run,
    test,
    why,
)


@test
def test_aliases_bridging_conflict_merges_groups(tmp: Path):
    """T9: конфликт-мост склеивает группы, а не уходит в первую попавшуюся.

    Раскладка `a→x`, `b→y`, `c→{x,y}`: жадная группировка клала `c` в ПЕРВУЮ группу, с
    которой он пересёкся, и останавливалась. Получались `{x,y}` c [a,c] и `{y}` c [b] —
    две группы, делящие карточку `y`, и они шли параллельно. Ровно та гонка, ради
    которой T9 и писался: решение по одному синониму переписывает alias в базе и меняет
    картину для другого.

    Проверяем не устройство группировки, а её смысл: никакие два конфликта над общей
    карточкой не пересекаются во времени — как бы группы ни легли.
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
        'AURORA_AGENT_BACKEND_1_WIDTH': '4',
        'AURORA_AGENT_PARALLEL': '4',
        'AURORA_AGENT_BUDGET_MIN': '20',
        'AURORA_AGENT_MAX_STEPS': '10',
        'AURORA_AGENT_REQUEST_TIMEOUT': '300',
    })

    marks, lock = {}, threading.Lock()
    # Длительности разные нарочно: при жадной группировке «b» идёт своей группой и
    # держится долго, а «c» стартует сразу после короткого «a» — и накладывается на «b»
    # поверх общей карточки «y». С равными длительностями гонка существует, но прячется:
    # «b» успевает закончить ровно к старту «c», и тест ловит удачу, а не поведение.
    naps = {'a': 0.05, 'b': 0.40, 'c': 0.05}

    def mock_solve(cfg_, *a, **k):
        alias = a[1]
        t0 = time.monotonic()
        with lock:
            marks[alias] = [t0, None]
        time.sleep(naps.get(alias, 0.05))
        with lock:
            marks[alias][1] = time.monotonic()
        return {'alias': alias, 'status': 'уточнил бы', 'backends': [], 'degraded': False,
                'note': ''}

    conflicts = [('a', 'x'), ('b', 'y'), ('c', ['x', 'y'])]
    with patch('agent_runner.read_conflicts', return_value=conflicts), \
            patch('agent_runner.solve_conflict', side_effect=mock_solve):
        res = run_aliases(cfg, str(root), False, True, 0)

    assert len(marks) == 3, f"обработаны не все три конфликта: {list(marks)}"
    cards = {'a': {'x'}, 'b': {'y'}, 'c': {'x', 'y'}}
    for one, two in (('a', 'c'), ('b', 'c')):
        (s1, e1), (s2, e2) = marks[one], marks[two]
        assert not (s1 < e2 and s2 < e1), (
            f"«{one}» и «{two}» делят карточку {sorted(cards[one] & cards[two])} и шли "
            f"параллельно: {s1:.3f}–{e1:.3f} и {s2:.3f}–{e2:.3f}. Конфликт-мост обязан "
            "склеивать группы, а не уходить в первую пересёкшуюся")
    assert len(res["steps"]) == 3 and all(s["status"] == "уточнил бы" for s in res["steps"]), \
        f"run_aliases потерял конфликт: {res['steps']}"


@test
def test_ring_survives_an_overflow_and_pays_the_spare_once(tmp: Path):
    """Кольцо: отказ по длине запроса — это строка в журнале, а не падение вызова.

    Два дефекта одной правки (семафор ширины, 1.100.3):

    1. Ветка «запрос длиннее окна модели» собирала строку тремя аргументами
       `log.append(a, b, c)` вместо склейки литералов — а `list.append` берёт ровно
       один. Любой бэкенд, отказавший по длине, ронял весь вызов `TypeError`. Это тот
       самый путь, ради которого окна вообще объявляют.

    2. `tried.add(b["n"])` выпал: множество заводилось и читалось, но не наполнялось.
       Поэтому «даю запасному свой срок» продлевал дедлайн одному и тому же бэкенду
       снова и снова — вызов тянулся дольше, чем разрешает request_timeout.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A

    # Окно НЕ объявлено: `fits` пропускает запрос, а шлюз отказывает по длине сам —
    # так и бывает в жизни, когда настоящий предел сервера меньше, чем думает человек.
    # Именно этот путь и падал.
    env = {"AURORA_AGENT_BACKEND_1_URL": "http://a", "AURORA_AGENT_BACKEND_1_MODEL": "m1",
           "AURORA_AGENT_BACKEND_2_URL": "http://b", "AURORA_AGENT_BACKEND_2_MODEL": "m2",
           "AURORA_AGENT_REQUEST_TIMEOUT": "10"}
    cfg = A.parse_config(env)

    # 1. Первый отказывает по длине, второй отвечает: вызов обязан дойти до второго.
    ok_body = {"choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}]}

    def overflow_then_ok(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        if b["n"] == 1:
            return (400, None, "This model's maximum context length is 1000 tokens", 0.0)
        return (200, ok_body, "", 0.1)

    r = A.call_role(cfg, "worker", [{"role": "user", "content": "x"}],
                    transport=overflow_then_ok, deadline=time.time() + 60,
                    sleep=lambda s: None)
    assert r["ok"] and r["backend"] == 2, \
        f"отказ по длине уронил кольцо вместо перехода к следующему: {r}"
    assert any("длиннее окна" in l for l in r["log"]), \
        f"причина отказа первого не названа словами: {r['log']}"

    # 2. Честный срок запасному даётся один раз на бэкенд, а не на каждый круг.
    def always_empty(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        return (200, {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]},
                "", 0.1)

    r2 = A.call_role(cfg, "worker", [{"role": "user", "content": "x"}],
                     transport=always_empty, deadline=time.time() + 0.2,
                     sleep=lambda s: None)
    gifts = [l for l in r2["log"] if "даю запасному свой срок" in l]
    assert len(gifts) <= len(cfg["backends"]), (
        "срок запасному выдан больше раза на бэкенд — значит tried не наполняется, "
        f"и вызов может тянуться дольше request_timeout: {gifts}")


@test
def test_a_failure_without_words_is_still_a_failure(tmp: Path):
    """Прогон, который считает молчаливый провал успехом, хуже отсутствующего прогона.

    Ровно это и случилось: `assert` без текста давал пустую строку, сводка печатала
    «Пройдено: 225/225» при напечатанном ❌ и выходила с кодом 0.
    """
    assert why(AssertionError()), \
        "assert без пояснения даёт пустую строку, а сводка считает провалом только " \
        "непустое — молчаливое падение засчитается пройденным"
    assert why(AssertionError("связи не совпали")) == "связи не совпали", \
        "пояснение из assert потерялось по дороге в отчёт"
    beaten = [(n, e) for n, e in [("тихий", why(AssertionError()))] if e]
    assert beaten, "сводка всё ещё отбрасывает провал без пояснения"


@test
def test_base_graph_shows_the_base_not_a_guess(tmp: Path):
    """Граф базы — то, что в ней написано: ссылки в тексте и `related:`.

    Не выведенные правилами связи: их считает `--cards` и кладёт в те же `related:` —
    значит в граф они попадут только после того, как человек их принял. Граф, который
    показывает догадку, нельзя использовать для навигации: пойдёшь по связи, которой
    в базе нет.
    """
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB" / "Concepts"
    kb.mkdir(parents=True, exist_ok=True)
    pairs = {"Заявка": ["Документ"], "Документ": ["Заявка", "Подпись"],
             "Подпись": ["Документ"], "Одинокая": []}
    for name, links in pairs.items():
        body = "\n".join(f"См. [[{l}]]." for l in links) or "Ни с чем не связана."
        st = "draft" if name == "Одинокая" else "knowledge"
        (kb / f"{name}.md").write_text(
            f"---\ntype: concept\nstatus: {st}\nkind: knowledge\n---\n\n# {name}\n\n{body}\n",
            encoding="utf-8")
    (root / "AuroraKnowledgeDB" / "README.md").write_text(
        "# База\n\nПример ссылки: [[Заявка]].\n", encoding="utf-8")

    out = root / "AuroraKnowledgeDB" / "meta" / "graph.json"
    # Зеркала Confluence в проекте нет — и это не должно мешать: граф базы читает
    # только карточки. Требовать зеркало значило бы оставить без графа проект,
    # собранный из Raw/, и свежий проект, где зеркала ещё нет.
    shutil.rmtree(root / "Sources" / "Confluence", ignore_errors=True)
    assert not (root / "Sources" / "Confluence").is_dir(), \
        "фикстура сама создала зеркало по манифесту коннектора — проверка «граф без " \
        "зеркала» перестала проверять то, ради чего написана"
    cp = subprocess.run([sys.executable, str(KIT / "scripts/kb_graph.py"),
                         "--cards-json", str(out)], cwd=root,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 0, f"граф без зеркала не построился:\n{cp.stderr[:400]}"
    data = json.loads(out.read_text(encoding="utf-8"))

    ids = {n["id"] for n in data["nodes"]}
    assert ids == set(pairs), f"в графе не то, что в базе: {sorted(ids)}"
    assert "README" not in ids, \
        "README базы принят за карточку — линтер уже однажды сделал эту ошибку"
    got = {tuple(sorted((e["from"], e["to"]))) for e in data["edges"]}
    assert got == {("Документ", "Заявка"), ("Документ", "Подпись")}, \
        f"связи не совпадают с написанным в базе: {sorted(got)}"
    assert data["orphans"] == 1, "карточка без связей не посчитана"
    assert any(n["id"] == "Одинокая" and n["status"] == "draft" for n in data["nodes"]), \
        "статус не доехал: черновик и знание в графе неразличимы"


@test
def test_graph_is_a_way_into_the_card(tmp: Path):
    """Граф, из которого нельзя попасть в карточку, — картинка «смотрите, красиво».

    Её посмотрят один раз. Поэтому: клик по узлу открывает карточку в редакторе,
    окрестность показывается вместо всей базы, а тяжёлый расчёт живёт в кэше с
    отметкой времени — экран, который открывается несколько секунд, открывать перестанут.
    """
    ui = panel_sources()
    assert '"id": "graph"' in ui and 'id="graphBox"' in ui, "раздела графа нет"
    # Граф — раздел-папка: его поднимает и перерисовывает общий код разделов, а не
    # строчка в `show()`. Проверяем то же самое там, где оно теперь живёт.
    assert "export function mount(" in (KIT / "cockpit/modules/graph/view.js")\
        .read_text(encoding="utf-8"), "переход в раздел ничего не рисует"
    assert "ctx.openPath(evt.target.data().path)" in ui, "клик по узлу никуда не ведёт"
    assert "function neighbourhood(" in ui, "показывается вся база сразу"
    assert "graph.toobig" in ui, "клубок из всей базы не объяснён человеку"
    # Порог обязан считаться по тому, что реально идёт в раскладку. На живой базе у
    # карточки-концентратора 556 соседей на первой ступени и 938 на второй: окрестность
    # оказывается почти всей базой, раскладка вешает вкладку, и человек видит зависшую
    # панель вместо графа. Защита «только для показа всей базы» здесь не срабатывала.
    assert "if (nodes.length > LIMIT" in ui, \
        "порог привязан к режиму, а не к числу узлов на экране"
    assert "graph.hub" in ui, "огромная окрестность не объяснена — выглядит как зависание"
    assert "function ensureCyto" in ui and "let CYTO = null;" in ui, \
        "библиотека графа грузится при старте панели"
    assert "/vendor/cytoscape/dist/cytoscape.min.js" in ui, "граф не знает, откуда взять библиотеку"
    assert 'node[draft = 1]' in ui, \
        "черновик неотличим от знания: строить на нём требования нельзя, и это видно должно быть до открытия"

    v = KIT / "cockpit/vendor/cytoscape"
    assert (v / "dist/cytoscape.min.js").is_file(), "библиотеки графа нет в поставке"
    assert (v / "VERSION").is_file() and (v / "LICENSE").is_file(), "чужой код без версии и лицензии"
    size = sum(p.stat().st_size for p in v.rglob("*") if p.is_file())
    assert size < 1_500_000, f"в вендор поехало лишнее: {size // 1000} КБ"

    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"/api/graph"' in srv and "meta" in srv, "нет эндпоинта графа"
    at = srv.index("def graph_state(")
    body = srv[at:srv.index("\ndef ", at + 10)]
    assert "graph.json" in body and 'data["when"]' in body, \
        "граф считается заново при каждом открытии, и когда посчитан — неизвестно"
    assert "движок проекта не умеет строить граф" in body, \
        "отставший движок объясняется трассировкой argparse"
    assert "stale_reason" in body and "stale_reason" in ui, \
        "прежний граф показан как свежий: человек построит решение на вчерашнем"

    # Кэш — производная, а не работа человека: битый файл чинится сам.
    proj = tmp / "гп"
    (proj / "AuroraKnowledgeDB" / "Concepts").mkdir(parents=True)
    (proj / ".opencode" / "scripts").mkdir(parents=True)
    (proj / "aurora.config.yaml").write_text("project:\n  name: Г\n", encoding="utf-8")
    shutil.copy(KIT / "scripts/kb_graph.py", proj / ".opencode/scripts/kb_graph.py")
    for dep in ("aurora_common.py",):
        shutil.copy(KIT / "scripts" / dep, proj / ".opencode/scripts" / dep)
    (proj / "AuroraKnowledgeDB" / "Concepts" / "Одна.md").write_text(
        "---\ntype: concept\nstatus: knowledge\n---\n\n# Одна\n", encoding="utf-8")
    cache = proj / "AuroraKnowledgeDB" / "meta" / "graph.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text("{сломано", encoding="utf-8")
    healed = ck.graph_state(str(proj))
    assert healed.get("nodes"), f"битый кэш не починился сам: {healed.get('error')}"


@test
def test_finished_artifact_lands_in_the_editor_with_a_publish_button(tmp: Path):
    """Готовый документ открывается в редакторе сам, и опубликовать его можно оттуда.

    Человек просил не искать файл после производства. И публикация из редактора обязана
    сначала показать **чистовик** — ровно то, что уйдёт: граница производства в тексте
    невидима, а «Допущения» на странице у заказчика читаются как часть спецификации.
    """
    ui = panel_sources()
    assert "Документ: `" in ui and "openPath(done)" in ui, \
        "готовый артефакт не открывается в редакторе"
    assert 'ctx.view === "work"' in ui, \
        "документ открывается поверх экрана, на который человек уже ушёл"
    assert "function publishFromEditor" in ui and "/api/files/clean" in ui, \
        "публикация из редактора не показывает чистовик"
    pub = ui[ui.index("async function publishFromEditor("):
             ui.index("async function publishFromEditor(") + 2000]
    assert pub.index("api(\"/api/files/clean") < pub.index('cmd:"ship:publish"'), \
        "публикация уходит раньше, чем человек увидел, что именно уйдёт"
    assert "editor.publish_nomark" in ui, \
        "документ без маркера публикуется молча — уедет всё тело целиком"
    assert "function publishTarget" in ui, \
        "кнопка публикации видна там, где публикация не применима"
    # «Незаконченные» обязаны заканчиваться действием, а не списком.
    assert ('onclick:()=>openPath(x.path)' in ui
            or 'onclick: () => ctx.openPath(x.path)' in ui), \
        "список незаконченных не ведёт в документ — станет счётчиком, на который не смотрят"

    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    sys.path.insert(0, str(KIT / "scripts"))
    from aurora_common import MADE_MARK

    root = tmp / "proj"
    (root / "Artifacts" / "ac").mkdir(parents=True)
    (root / "Artifacts" / "ac" / "AC-1.md").write_text(
        "---\ntype: ac\nstatus: ready\n---\n\n# AC\n\nТребование.\n\n"
        + MADE_MARK + "\n\n## Допущения\n\n- движок предположил\n", encoding="utf-8")
    prev = ck.clean_preview(str(root), "Artifacts/ac/AC-1.md")
    assert "Требование." in prev["clean"], "чистовик пуст"
    assert "Допущения" not in prev["clean"] and MADE_MARK not in prev["clean"], \
        "в предпросмотре видна кухня — значит она уедет и заказчику"
    assert "type: ac" not in prev["clean"], "шапка движка уходит наружу"
    assert prev["cut"] > 0 and prev["marked"], "не сказано, сколько осталось в черновике"


@test
def test_choosing_a_project_refills_the_screen_you_are_standing_on(tmp: Path):
    """Выбор проекта обязан перерисовать текущий экран, а не только тот, куда уйдут.

    Список видов артефактов и дерево файлов наполнял **только переход на вкладку**.
    Значит выбор проекта, сделанный стоя на «Продуктивности» или «Файлах», оставлял
    экран пустым навсегда: перерисовать его было некому. Человек видит пустоту при
    выбранном проекте и уходит искать поломку там, где её нет.

    Ровно эта беда уже случалась с «Зеркалами» — предупреждение об этом написано прямо
    в `pick()`, двумя строками выше. Значит одного комментария мало: нужна проверка,
    которая не даст добавить следующий экран с той же дырой.
    """
    ui = panel_sources()
    pick = ui[ui.index("async function pick("):ui.index("async function pick(") + 3000]
    # «Зеркала» и «Здоровье» уехали в разделы-папки: их перерисовывает `refreshModules()`,
    # и теперь это правило для всех модулей сразу, а не список, который надо помнить.
    # Виды артефактов и маршруты — теперь разделы-папки: их перерисовывает refreshModules.
    for call in ("refreshModules()", "renderFiles()"):
        assert call in pick, f"выбор проекта не перерисовывает экран: нет {call}"

    # Пустой список обязан объяснять себя. И он не имеет права чиститься до того, как
    # стало чем наполнять: один сорвавшийся запрос стирал настроенные виды артефактов,
    # и это выглядело как «настройки пропали».
    # Список видов наполняет раздел «Продуктивность» — он же и объясняет пустоту.
    work = (KIT / "cockpit/modules/work/view.js").read_text(encoding="utf-8")
    ru = (KIT / "cockpit/modules/work/i18n/ru.json").read_text(encoding="utf-8")
    at = work.index("async function fillKinds(")
    fill = work[at:at + 1800]
    assert "work.pick_project_first" in fill and "сначала выберите проект" in ru, \
        "пустой список молчит о причине"
    assert fill.index("if (!Object.keys(kinds).length)") < fill.rindex('sel.innerHTML = ""'), \
        "список чистится раньше, чем известно, есть ли чем наполнить"
    assert "work.kinds_none" in fill and "не объявлено ни одного вида" in ru, \
        "проект без артефактов неотличим от сорвавшегося запроса"

    # Дерево тоже: пустое место человек читает как «файлов нет», а не «ещё читаю».
    rf = ui.index("async function renderFiles(")
    tree = ui[rf:rf + 1600]
    assert "files.loading" in tree, "дерево молчит, пока читается"
    assert "files.failed" in tree, "отказ дерева неотличим от пустого проекта"


@test
def test_default_language_does_not_depend_on_the_network(tmp: Path):
    """Русский каталог уезжает в самой странице, а не отдельным запросом.

    Пока за ним ходили по сети, панель зависела от ответа до первой отрисовки: сервер
    не ответил — и вместо надписей человек видит имена ключей. Хуже того, запрос стоял
    первым в загрузке, то есть отказ по нему ставил под угрозу весь экран.
    """
    ui = panel_sources()
    assert '"__AURORA_I18N__"' in ui, "в странице нет места под каталог по умолчанию"
    assert "const RU = " in ui and "s = RU[key]" in ui, \
        "нет отката на русский, когда в другом языке ключа нет"
    at = ui.index("async function loadI18n(")
    boot = ui[at:at + 1600]
    assert 'if (lang === "ru")' in boot, "за русским всё ещё ходят по сети"
    assert "try {" in boot and "catch" in boot, \
        "отказ сети за чужим языком роняет загрузку панели"

    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "__AURORA_I18N__" in srv, "сервер не подставляет каталог в страницу"

    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    strings = ck.i18n_catalogue("ru").get("strings") or {}
    assert strings, "русский каталог пуст — подставлять нечего"
    blob = json.dumps(strings, ensure_ascii=False)
    assert "</script>" not in blob, \
        "строка каталога способна закрыть тег скрипта и сломать всю страницу"
    page = ui.replace('"__AURORA_I18N__"', blob)
    at2 = page.index("const RU = ")
    assert '"__AURORA_I18N__"' not in page[at2:at2 + 200], "каталог не подставился"


@test
def test_interface_language_is_a_file_not_a_rewrite(tmp: Path):
    """Языки закладываются механизмом, а не разовым выносом 982 строк.

    Разовый вынос трогает каждый экран и не даёт человеку ничего видимого — идеальные
    условия, чтобы сломать работающее и узнать об этом от пользователя. Новый код
    пишется через каталог сразу, старые экраны переезжают тогда, когда их и так правят.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    langs = ck.languages()
    assert any(l["id"] == "ru" for l in langs), "русского каталога нет"
    ru = ck.i18n_catalogue("ru")
    assert ru["lang"] == "ru" and ru["strings"], "каталог пуст"
    assert ru["strings"].get("nav.files"), "строк нового раздела нет в каталоге"

    # Языка нет — берём русский, а не пустой экран и не имена ключей.
    assert ck.i18n_catalogue("de")["lang"] == "ru", "неизвестный язык не откатился на русский"
    assert ck.i18n_catalogue("../../etc/passwd")["lang"] == "ru", "путь в имени языка"

    # Битый каталог откатывается целиком и называет причину. Оставить его выбранным
    # значило бы показать русский экран под именем другого языка: человек решит, что
    # перевода нет, и чинить не станет.
    bad = KIT / "cockpit/i18n/zz.json"
    bad.write_text("{сломано", encoding="utf-8")
    try:
        broken = ck.i18n_catalogue("zz")
        assert broken["lang"] == "ru", "битый каталог остался выбранным языком"
        assert broken["warning"], "поломка каталога не названа"
        assert all(l["id"] != "zz" for l in ck.languages()), \
            "битый каталог предлагается к выбору"
    finally:
        bad.unlink()
    assert not ck.i18n_catalogue("ru")["warning"], "предупреждение на исправном каталоге"
    assert 'if (d.warning) toast(' in ui_source(), \
        "сервер назвал поломку, а панель её не показывает"

    ui = panel_sources()
    assert "data-i18n" in ui and "function applyI18n" in ui, "разметка не умеет переводиться"
    # Возврат на язык по умолчанию обязан вернуть и строки: каталог живёт в одной
    # переменной, и без сброса панель оставалась на прошлом языке до перезагрузки.
    back = ui[ui.index("async function loadI18n("):]
    back = back[:back.index("\n}\n")]
    assert "I18N = RU;" in back, "возврат на русский не возвращает строки"
    # Список языков приезжает и тому, кто сидит на русском, иначе уйти с него некуда.
    assert '"/api/i18n?lang=ru"' in back, "список языков спрашивают только не с русского"
    assert "await loadI18n();" in ui, "строки грузятся после первой отрисовки — экран моргнёт"
    i18n_dir = KIT / "cockpit/i18n"
    assert (i18n_dir / "ru.json").is_file(), "каталог языка не файлом рядом с темами"
    # Новый язык = новый файл: ни сервер, ни панель править не нужно.
    assert "os.listdir(I18N_DIR)" in (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8"), \
        "список языков захардкожен — добавление языка станет правкой кода"


@test
def test_editor_ships_prebuilt_and_pruned(tmp: Path):
    """Редактор приезжает собранным: сборки в ките нет и не будет.

    Панель — self-contained HTML, сервер — стандартная библиотека. Это условие закрытого
    контура: на машине аналитика может не быть ни интернета, ни Node. Цена — мегабайты
    в репозитории, и они не должны расти вдвое от невнимательности: полный `dist` тянет
    23 МБ, из которых половина — то, чем мы не пользуемся.
    """
    v = KIT / "cockpit/vendor/vditor"
    assert v.is_dir(), "редактора нет в поставке"
    # Раскладка повторяет пакет: библиотека тянет своё по пути `<cdn>/dist/…`, и
    # плоская папка давала 404 на языке интерфейса и на mermaid — редактор поднимался
    # без них и молчал об этом.
    assert (v / "dist/index.min.js").is_file() and (v / "dist/index.css").is_file(), "нет сборки"
    assert (v / "VERSION").is_file() and (v / "LICENSE").is_file(), \
        "чужой код без версии и лицензии"
    for keep in ("dist/js/lute", "dist/js/mermaid", "dist/js/katex",
                 "dist/js/i18n/ru_RU.js"):
        assert (v / keep).exists(), f"вычищено нужное: {keep}"
    for drop in ("dist/js/mathjax", "dist/js/graphviz", "dist/js/echarts",
                 "dist/js/markmap", "dist/ts", "dist/index.js", "dist/types"):
        assert not (v / drop).exists(), f"неиспользуемое в репозитории: {drop}"

    size = sum(p.stat().st_size for p in v.rglob("*") if p.is_file())
    assert size < 14_000_000, f"вендор разросся: {size // 1_000_000} МБ (ждали ~10)"

    assert not (KIT / "package.json").exists(), "в ките завелась сборка"
    ui = panel_sources()
    assert "/vendor/vditor" in ui, "панель не знает, откуда брать редактор"
    assert "cdn.jsdelivr" not in ui and "unpkg.com" not in ui, \
        "панель тянет библиотеку из интернета — в закрытом контуре это пустой экран"
    assert "function ensureVditor" in ui and "VDITOR_READY" in ui, \
        "10 МБ грузятся при старте: панель обязана открываться сразу"

    # Раздаём только известные типы и только из этой папки.
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "STATIC_TYPES" in srv and "inside(base, rel)" in srv, \
        "статика раздаётся без проверки пути и типа"


@test
def test_save_button_compares_text_not_a_touched_flag(tmp: Path):
    """«Сохранить» неактивна без изменений — и «без изменений» считается сравнением текста.

    В режиме «как в Word» редактор пересобирает разметку своим сериализатором: файл
    расходится с исходным сразу после открытия, ничего не тронув. Внутренний признак
    «трогали» загорелся бы сам, и защита от дифа на весь файл исчезла бы молча — а в
    git-базе такой диф означает, что настоящую правку в нём не найти.
    """
    ui = panel_sources()
    assert "function wholeText()" in ui and 'F.dirty = wholeText() !== F.orig;' in ui, \
        "признак изменений берётся не из сравнения текста целого документа"
    # Шапка правится отдельно от тела — значит и сравнивать надо документ целиком,
    # иначе правка одной только шапки не включит кнопку «Сохранить».
    assert "function splitFrontmatter" in ui and 'return (F.fm || "")' in ui, \
        "шапка не отделена от тела: в режиме «как в Word» редактор её перепишет"
    assert 'if (mode === "wysiwyg" && now !== text)' in ui, \
        "расхождение сразу после открытия в WYSIWYG не проверяется"
    assert "editor.wysiwyg_warn" in ui, "о переписанной разметке человеку не говорят"
    assert '$("#fileSave").disabled = true;' in ui, "кнопка активна на нетронутом файле"
    assert 'mode: localStorage' not in ui, "режим не запоминается — выбирать придётся каждый раз"
    assert '"aurora-editor-mode"' in ui, "выбранный режим не сохраняется"
    ru = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    assert "{n}" in ru["editor.wysiwyg_warn"], \
        "предупреждение не называет число расходящихся строк — цена не видна"

    # Живой прогон: редактор сам написал «предпросмотр требует 20901мс» на карточке в
    # 11 КБ, а `_index.md` живой базы весит 171 КБ. Без порогов панель подвисала бы
    # молча, и человек решил бы, что сломалась она, а не документ велик.
    assert "PREVIEW_LIMIT" in ui and "EDIT_LIMIT" in ui, "порогов размера нет"
    assert "editor.too_big" in ui and "editor.heavy" in ui, \
        "порог сработает молча: человеку не сказано, почему вида нет"
    assert 'F.ed.disabled()' in ui, \
        "в файле «только для чтения» можно печатать: работа пропадёт при первом уходе"
    for key in ("editor.too_big", "editor.heavy"):
        assert "{kb}" in ru[key], f"{key} не называет размер — непонятно, что за файл"


@test
def test_files_section_is_reachable_and_explains_itself(tmp: Path):
    """Раздел «Файлы» — место, куда человек идёт искать документ, а не запускать программу."""
    ui = panel_sources()
    assert 'data-view="files"' in ui and 'id="view-files"' in ui, "раздела нет"
    # После «Продуктивности»: к файлам возвращаются часто, но начинают не с них.
    orders = {m["id"]: m["order"] for m in __import__("json").loads(
        subprocess.run([sys.executable, "-c",
            "import sys, json; sys.path.insert(0, %r); import aurora_cockpit as ck;"
            " print(json.dumps(ck.modules()))" % str(KIT / "cockpit")],
            capture_output=True, text=True, encoding="utf-8", errors="replace").stdout)}
    assert orders["work"] < 50 < orders["ask"], \
        "раздел «Файлы» стоит не после «Продуктивности»"
    assert 'if (view==="files") renderFiles();' in ui, "переход в раздел ничего не рисует"
    for handler in ('$("#fileSave")?.addEventListener', '$("#fileSearch")?.addEventListener',
                    '$("#fileFolder")?.addEventListener', '$("#fileMode")?.addEventListener'):
        assert handler in ui, f"кнопка без обработчика: {handler}"
    assert 'e.key === "s"' in ui, "Ctrl+S не сохраняет — а он и есть подтверждение"
    assert "resolveConflict" in ui, "расхождение с диском некому показать"

    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    for route in ("/api/files/tree", "/api/files/read", "/api/files/write",
                  "/api/files/reveal", "/api/git", "/api/git/commit", "/api/git/push",
                  "/api/i18n"):
        assert f'"{route}"' in srv, f"панель зовёт маршрут, которого нет: {route}"
        assert route in ui, f"сервер отвечает на {route}, но панель его не зовёт"


@test
def test_reveal_hands_the_path_to_the_os_without_a_shell(tmp: Path):
    """Имя файла — чужой текст, и `; rm -rf` в нём не должно ничего значить."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    body = src[src.index("def reveal("):src.index("def reveal(") + 1400]
    assert "shell=True" not in body, "путь уходит в оболочку"
    assert "subprocess.Popen(cmd)" in body, "команда собирается не списком"

    root = tmp / "proj"
    root.mkdir()
    assert ck.reveal(str(root), "../снаружи.md").get("error"), \
        "проводник открывает путь за пределами проекта"
    assert ck.reveal(str(root), "нет-такого.md").get("error"), \
        "проводник зовётся на несуществующий файл"


@test
def test_skills_land_in_one_shared_folder(tmp: Path):
    """Скиллы ставятся в один каталог агента и не расходятся копиями.

    Скиллы лежат в репозитории кита, а агент ищет их в `~/.claude/skills`: без копии
    `/aurora-vault` не находится ни в одном диалоге. Каталог именно один — две копии
    одного скилла расходятся на первой правке, и потом не понять, какая отвечала.
    """
    src = KIT / "scripts/install_skills.py"
    assert src.is_file(), "нет команды установки скиллов"
    body = src.read_text(encoding="utf-8")
    assert '.claude" / "skills"' in body, "общий каталог должен быть ~/.claude/skills"
    assert "symlink_to" in body, "остальные harness должны получать ссылку, а не копию"

    out = subprocess.run([sys.executable, str(src), "--status"],
                         cwd=str(KIT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, out.stderr[:300]
    for name in ("aurora-vault", "aurora-dev"):
        assert name in out.stdout, f"скилл {name} не попал в установку"

    dry = subprocess.run([sys.executable, str(src)], cwd=str(KIT),
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    assert "--apply" in dry or "делать нечего" in dry, \
        "без --apply установка обязана только показывать"

    # установка встроена в жизненный цикл: иначе про неё забудут
    entry = (KIT / "aurora.py").read_text(encoding="utf-8")
    assert entry.count("install_skills.py") >= 2, \
        "скиллы должны ставиться и при создании проекта, и при обновлении движка"
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "kit:skills" in reg, "нет команды kit:skills в реестре"
    assert "dev:install-skill" not in reg, \
        "частная установка одного скилла осталась рядом с общей — два пути к одному"


@test
def test_dev_skill_is_installable_and_asks_for_coverage(tmp: Path):
    """Скилл разработчика находится в новом диалоге, а `--cover` даёт готовое задание.

    Скилл лежит в репозитории кита, а агент ищет скиллы в домашней папке: без установки
    `/aurora-dev` в другом диалоге просто не найдётся, и весь контур останется бумажным.
    Задание же нужно потому, что модель, дорабатывавшая код, знает про свои изменения —
    но не знает правил этого контура.
    """
    # Кухня разработки (`Development/QA`) в git не едет: на чистой копии кита — в проверке
    # GitHub — задание собрать не из чего, и проверяется только сам скилл.
    if KITCHEN.is_dir():
        cov = subprocess.run([sys.executable, str(KIT / "scripts/dev_qa.py"), "--cover"],
                             cwd=str(KIT), capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert cov.returncode == 0, cov.stderr[:300]
        task = cov.stdout
        assert "ЗАДАНИЕ АССИСТЕНТУ" in task, "нет блока для копирования в другой диалог"
        for must in ("автотест", "тест-кейс QA", "сценарий", "--check", "--list", "covers"):
            assert must in task, f"в задании не сказано про «{must}»"
        assert "предпочтительный вариант ВСЕГДА" in task, \
            "не задан приоритет автотеста над кейсом — модель заведёт кейс на всё подряд"
        assert "--new case" in task and "--new scenario" in task, \
            "модель не узнает, чем заводить документы"

    skill = (KIT / "skills/aurora-dev/SKILL.md").read_text(encoding="utf-8")
    assert "Если вас позвали после разработки фичи" in skill, \
        "в скилле нет рецепта для самого частого случая"
    assert "kit:skills" in skill, "не сказано, чем ставится скилл"


@test
def test_set_alias_is_a_scalpel(tmp: Path):
    """Точечная замена синонима: одна строка списка, остальное байт в байт.

    Агент решает, каким должен стать синоним, но резать по живой шапке ему нельзя: модель
    умеет только перегенерировать файл целиком и вместе с одной строкой переписывает поля,
    теги и тело. «Не найдено» обязано быть кодом возврата, а не строкой в отчёте, — иначе
    агент засчитает несделанную работу.
    """
    root = make_project(tmp, git=True)
    card(root, "Concepts/ALG-309-Получение-курсов.md", "Тело, которое нельзя трогать.",
         aliases='["Курс валют", "ALG-309"]', type="concept")
    before = (root / "AuroraKnowledgeDB/Concepts/ALG-309-Получение-курсов.md").read_text(
        encoding="utf-8")

    run("kb_fix.py", "--set-alias", "ALG-309-Получение-курсов", "--old", "Курс валют",
        "--new", "Курсы валют (алгоритм)", "--apply", "--allow-dirty", cwd=root)
    after = (root / "AuroraKnowledgeDB/Concepts/ALG-309-Получение-курсов.md").read_text(
        encoding="utf-8")
    assert "Курсы валют (алгоритм)" in after and "ALG-309" in after, after[:300]
    assert "Тело, которое нельзя трогать." in after, "тело карточки пострадало"
    assert before.count("\n") == after.count("\n"), "изменилось число строк — правка не точечная"

    # идемпотентность: повтор ничего не портит и не дублирует
    run("kb_fix.py", "--set-alias", "ALG-309-Получение-курсов", "--old", "Курс валют",
        "--new", "Курсы валют (алгоритм)", "--apply", "--allow-dirty", cwd=root)
    twice = (root / "AuroraKnowledgeDB/Concepts/ALG-309-Получение-курсов.md").read_text(
        encoding="utf-8")
    assert twice == after, "повторный вызов изменил файл"

    # неточное имя резолвится по хвосту — модель называет карточку как видит
    run("kb_fix.py", "--set-alias", "Получение-курсов", "--old", "ALG-309",
        "--new", "ALG-309 (алгоритм)", "--apply", "--allow-dirty", cwd=root)
    assert "ALG-309 (алгоритм)" in (root / "AuroraKnowledgeDB/Concepts/ALG-309-Получение-курсов.md").read_text(encoding="utf-8")

    # несделанная работа — ненулевой код возврата
    miss = run("kb_fix.py", "--set-alias", "Нет-такой-карточки", "--old", "X", "--new", "Y",
               "--apply", "--allow-dirty", cwd=root, expect_rc=1)
    assert "не найдена" in miss.stdout + miss.stderr


@test
def test_agent_work_rolls_back_whole(tmp: Path):
    """Откат снимает и новые файлы: иначе обещание в отчёте — неправда.

    `git reset --hard <чекпойнт>` не трогает то, чего git ещё не видел, а сборка карточек
    создаёт именно новые файлы. На живом прогоне откат оставил карточки в базе, а
    следующий чекпойнт закоммитил их как работу человека.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp, git=True)
    card(root, "Concepts/Старая.md", "Была до агента.", type="concept")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-qm", "до агента"], cwd=str(root), check=True)

    cp = R.checkpoint(str(root), "agent:build", True)
    card(root, "Concepts/Новая-от-агента.md", "Собрана агентом.", type="concept")
    done = R.commit_result(str(root), "agent:build", "источников разобрано: 1", True)
    assert done["ok"] and done["sha"], done

    subprocess.run(["git", "reset", "--hard", cp["sha"]], cwd=str(root),
                   capture_output=True, check=True)
    assert not (root / "AuroraKnowledgeDB/Concepts/Новая-от-агента.md").exists(), \
        "новая карточка пережила откат — обещание «одной строкой» не выполнено"
    assert (root / "AuroraKnowledgeDB/Concepts/Старая.md").exists(), "откат снёс лишнее"

    # правка человека, сделанная пока агент работал, агенту не принадлежит
    card(root, "Concepts/Ещё-одна.md", "Собрана агентом.", type="concept")
    (root / "Artifacts").mkdir(exist_ok=True)
    (root / "Artifacts" / "human-edit.md").write_text("правил человек", encoding="utf-8")
    R.commit_result(str(root), "agent:build", "источников разобрано: 1", True)
    left = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=str(root),
                          capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    assert "human-edit" in left, "коммит агента забрал чужую работу вне базы знаний"



@test
def test_lint_spares_artifacts_that_came_from_sources(tmp: Path):
    """Выгруженное из Confluence — законный житель базы, как бы оно ни называлось.

    Правило смотрело только на имя и объявляло чужой историей каждую страницу с «US-»
    в заголовке: на живой базе это 243 ложных срабатывания из 243. Артефакт — то, что
    сгенерировано нами: у него нет источника в зеркале.
    """
    root = make_project(tmp)
    card(root, "Concepts/US-3.1.1-Выгружено-из-вики.md", "Пришло из зеркала.",
         type="concept", source='"Sources/Confluence/История.md"')
    card(root, "Concepts/US-3.1.2-Сгенерировано-нами.md", "Родилось в проекте.",
         type="concept")

    out = run("kb_lint.py", cwd=root, expect_rc=1).stdout
    assert "US-3.1.2-Сгенерировано-нами" in out, "сгенерированный артефакт перестали замечать"
    # Смотрим именно категорию артефактов: с 1.91 линтер отдельно называет карточки без
    # связей, и одиночная фикстура попадает туда по другому поводу.
    arts = out.split("артефакты, попавшие в базу знаний")[1] if "артефакты, попавшие" in out else out
    assert "US-3.1.1-Выгружено-из-вики" not in arts.split("## ")[0], \
        "страница из зеркала объявлена чужим артефактом"


@test
def test_split_makes_atoms_and_keeps_the_document(tmp: Path):
    """Раздутая карточка режется по своим заголовкам, а сама становится картой документа.

    Атомарность — основа и поиска, и чтения: карточку на тридцать тысяч знаков не найти
    выборкой и не прочитать в контексте. Границы тем в ней уже расставлены заголовками,
    спрашивать о них модель незачем. Принадлежность документу при этом не теряется.
    """
    root = make_project(tmp)
    big = "\n\n".join(f"## Часть {i}\n\n" + "текст. " * 80 for i in range(1, 4))
    card(root, "Concepts/Большая.md", big, type="concept",
         source='"Sources/Confluence/Документ.md"')

    run("kb_fix.py", "--split", "Большая", "--apply", "--allow-dirty", cwd=root)
    parts = list((root / "AuroraKnowledgeDB" / "Concepts").glob("Часть-*.md"))
    assert len(parts) == 3, [p.name for p in parts]
    first = parts[0].read_text(encoding="utf-8")
    assert 'part_of: "[[Большая]]"' in first, "часть не помнит, откуда она"
    assert "Sources/Confluence/Документ.md" in first, "часть потеряла источник"
    kept = (root / "AuroraKnowledgeDB/Concepts/Большая.md").read_text(encoding="utf-8")
    assert "[[Часть-1|Часть 1]]" in kept, "исходная карточка не стала картой документа"
    assert "текст. текст." not in kept, "тело осталось в карте — резать было незачем"


@test
def test_graph_links_cards_to_their_terms(tmp: Path):
    """Карточка ссылается на определение термина — и не наоборот.

    Правила RY и номеров историй точны, но узки: на живой базе они покрывали 588 карточек
    из 1692. Термин — третий способ, которым связи уже записаны в текстах. Обратная связь
    запрещена намеренно: «Заявитель» упомянут почти везде, и определение превратилось бы
    в свалку из девятисот ссылок.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    G = importlib.import_module("kb_graph")

    root = make_project(tmp)
    card(root, "Glossary/Обеспечительный-платёж.md", "Определение термина.", type="glossary")
    card(root, "Concepts/Возврат-средств.md",
         "При отказе обеспечительный платёж возвращается заявителю.", type="concept")
    card(root, "Concepts/Погода.md", "Ничего общего.", type="concept")

    import os as _os
    cwd = _os.getcwd()
    try:
        _os.chdir(root)
        pairs = G.glossary_links()
    finally:
        _os.chdir(cwd)
    froms = {a.split("/")[-1] for a, _b in pairs}
    tos = {b.split("/")[-1] for _a, b in pairs}
    assert "Возврат-средств.md" in froms, "карточка не связалась с определением термина"
    assert "Погода.md" not in froms, "связь возникла там, где термина нет"
    assert tos == {"Обеспечительный-платёж.md"}, tos


@test
def test_context_index_shows_the_whole_base_cheaply(tmp: Path):
    """Оглавление: строка на карточку, чтобы модель увидела базу целиком.

    Выборка находит то, что человек назвал словами. Оглавление решает другую задачу —
    показать, чего он не назвал. Поэтому строка предельно скупа: раздел даёт
    группировка, суть берётся из `summary`, а пока его нет — из первой содержательной
    строки тела, минуя заголовки и разметку таблиц.
    """
    root = make_project(tmp)
    card(root, "Concepts/Курс-валюты.md",
         "# Заголовок\n\n> **История изменений**\n\n| a | b |\n\nКурс ЦБ берётся на дату подачи.",
         status="verified", type="concept")
    card(root, "Glossary/Термин.md", "Пояснение термина.", status="verified",
         type="glossary", summary='"Одна фраза про термин"')

    out = run("ctx_pack.py", "любая тема", "--index", "--no-log", cwd=root).stdout
    assert "## Concepts (1)" in out and "## Glossary (1)" in out, out[:400]
    assert "Одна фраза про термин" in out, "summary из шапки не попал в оглавление"
    assert "Курс ЦБ берётся на дату подачи" in out, \
        "вместо сути в оглавление попала разметка источника"
    assert "История изменений" not in out, "служебная цитата принята за суть карточки"


@test
def test_registry_cache_keeps_kit_and_project_apart(tmp: Path):
    """Кэш реестра помнит, откуда поднята панель: из проекта команд `dev:` не видно.

    Ключ без этого признака делал кэш общим: панель, запущенная в проекте, записывала
    «реестр без dev» в файл кита — и раздел разработки исчезал из панели до следующей
    смены версии. Нашлось это прогоном тестов: они и портили кэш.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    ck.CACHE.pop("registry", None)
    rows = ck.registry()
    assert any(r["cmd"].startswith("dev:") for r in rows), \
        "из кита команды разработки не видны"

    cache = json.loads((KIT / "cockpit" / ".registry-cache.json").read_text(encoding="utf-8"))
    assert "src=" in cache["key"], cache["key"]
    assert any(r["cmd"].startswith("dev:") for r in cache["rows"]), \
        "в файле кита лежит реестр без dev: его записало чужое дерево"

    # и обратное: реестр, собранный по ЧУЖОМУ kit_commands, в файл кита попасть не может.
    # В одном процессе панель и тесты работают с несколькими деревьями, и первый импорт
    # выигрывает — так шесть команд `dev:` и пропадали из панели до смены версии.
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "os.path.samefile(os.path.dirname(os.path.abspath(K.__file__))" in src, \
        "движок не проверяет, чей kit_commands у него в руках"
    assert "if not ours:" in src and "на диск кита не пишем" in src, \
        "чужой реестр по-прежнему может лечь в файл кита"



@test
def test_long_step_reports_progress_and_duration(tmp: Path):
    """Долгий шаг показывает, что идёт, а журнал помнит, сколько он занял.

    Агент печатал отчёт только в конце: на живом прогоне это двадцать минут пустой
    консоли, по которой невозможно отличить работу от повисшего процесса. Второй
    источник ответа — прошлый прогон: у команд разброс от секунды до получаса.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    R = importlib.import_module("agent_runner")
    ck = importlib.import_module("aurora_cockpit")

    import time as _t
    line = R.progress(3, 15, _t.time() - 120)
    assert "[3/15]" in line and "20%" in line, line
    assert "осталось ~" in line, "нет оценки остатка — главного, что нужно у экрана"
    assert "осталось" not in R.progress(15, 15, _t.time() - 120), \
        "на последнем шаге обещать остаток нечестно"

    root = make_project(tmp)
    ck.write_runlog(str(root), "agent:build", 0, "agent:build --apply", 754)
    again = ck.read_runlog(str(root))
    assert again["agent:build"]["secs"] == 754, again

    # журнал старого формата (без колонки секунд) читается по-прежнему
    path = root / ".opencode" / "run_log.md"
    path.write_text(path.read_text(encoding="utf-8").replace(" | 754 |", " |"),
                    encoding="utf-8")
    old = ck.read_runlog(str(root))
    assert old["agent:build"]["rc"] == 0 and old["agent:build"]["secs"] == 0, old


@test
def test_doctor_accepts_folders_declared_by_artifacts(tmp: Path):
    """Папка, объявленная в реестре артефактов, — законная, а не нарушение схемы.

    Ловушка выглядела так: человек заводит вид документа в панели, движок создаёт под
    него папку, а doctor тут же называет её папкой вне схемы. Реестр видов — такое же
    основание для папки, как реестр модулей для зеркала в Sources/.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "pr.md").write_text("шаблон", encoding="utf-8")
    (root / "Своя-папка-вне-схемы").mkdir()

    ck.kinds_write(str(root), {"pr": {"title": "ПР", "template": "Templates/pr.md",
                                      "out": "Deliverables/opz"}})
    assert (root / "Deliverables" / "opz").is_dir()

    out = run("aurora_doctor.py", cwd=root, expect_rc=None).stdout
    assert "Deliverables/opz" not in out, \
        "папка из реестра артефактов объявлена нарушением схемы"
    assert "Своя-папка-вне-схемы" in out, \
        "папка, которую никто не объявлял, перестала замечаться — проверка ослабла"


@test
def test_doctor_accepts_folders_the_project_declared(tmp: Path):
    """Своя папка проекта, объявленная в конфиге, законна здесь — и только здесь.

    У проекта бывает папка, которой нет у других: наследие прежней базы, вложения. Добавить
    её в схему кита значит завести пустую такую же во всех проектах; объявление в
    `aurora.config.yaml` делает её законной в одном проекте. Необъявленная папка при этом
    ловится как прежде, а путь за пределы проекта объявлением не считается.
    """
    root = make_project(tmp)
    media = root / "AuroraKnowledgeDB" / "media"
    media.mkdir(parents=True, exist_ok=True)
    (media / "Схема.png.md").write_text('---\ntitle: "Схема.png"\n---\n', encoding="utf-8")
    (root / "AuroraKnowledgeDB" / "Необъявленная").mkdir()

    def schema_errors() -> str:
        out = run("aurora_doctor.py", cwd=root, expect_rc=None).stdout
        return " ".join(l for l in out.splitlines() if "вне схемы движка" in l)

    assert "AuroraKnowledgeDB/media" in schema_errors(), \
        "необъявленная папка не замечена — проверка слепа ещё до объявления"

    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8")
                   + "\nextra_structure_dirs: [AuroraKnowledgeDB/media, ../за-пределами]\n",
                   encoding="utf-8")
    after = schema_errors()
    assert "AuroraKnowledgeDB/media" not in after, \
        f"папка, объявленная проектом, названа нарушением схемы: {after}"
    assert "AuroraKnowledgeDB/Необъявленная" in after, \
        "необъявленная папка перестала замечаться — объявление ослабило проверку целиком"

    sys.path.insert(0, str(SCRIPTS))
    import importlib
    D = importlib.import_module("aurora_doctor")
    cwd = os.getcwd()
    try:
        os.chdir(root)
        declared = D.project_dirs()
    finally:
        os.chdir(cwd)
    assert "AuroraKnowledgeDB/media" in declared and not any(".." in p for p in declared), \
        f"путь за пределы проекта принят объявлением: {declared}"


@test
def test_artifact_kinds_are_declared_and_editable(tmp: Path):
    """Виды документов объявляет проект, а не движок, и правятся они из панели.

    Формы у заказчиков разные: ОПЗ, проектное решение, руководство — у каждого свой
    шаблон и своя папка. Движок не может знать их наперёд, поэтому реестр открытый и
    живёт в конфиге проекта: в git, виден любой IDE и ассистенту через MCP.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    MK = importlib.import_module("make_kinds")
    ck = importlib.import_module("aurora_cockpit")

    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates" / "pr.md").write_text("шаблон", encoding="utf-8")

    r = ck.kinds_write(str(root), {
        "pr": {"title": "Проектное решение", "template": "Templates/pr.md",
               "out": "Deliverables/work"}})
    assert r.get("ok"), r
    assert (root / "Deliverables" / "work").is_dir(), \
        "папка результата не создана — объявили и не найдём в момент записи"

    kinds = MK.read_kinds(str(root))
    assert kinds["pr"]["template"] == "Templates/pr.md", kinds
    assert not MK.check(str(root), kinds), MK.check(str(root), kinds)

    # несуществующий шаблон объявить можно, но команда об этом скажет
    ck.kinds_write(str(root), {"pr": {"title": "П", "template": "Templates/нет.md",
                                      "out": "Deliverables/work"}})
    bad = MK.check(str(root), MK.read_kinds(str(root)))
    assert bad and "шаблона нет" in bad[0][1], bad

    # имя типа — не произвольная строка: по нему зовут инструмент и создают папку
    assert "error" in ck.kinds_write(str(root), {"ПР ": {"title": "x"}}), \
        "принято недопустимое имя типа"

    # правка реестра не портит остальной конфиг
    cfg = (root / "aurora.config.yaml").read_text(encoding="utf-8")
    assert "project:" in cfg and "atlassian:" in cfg, "перезапись секции задела чужие"


@test
def test_embeddings_are_configured_separately(tmp: Path):
    """Свой сервис векторов: адрес, ключ и модель настраиваются отдельно от чата.

    Кит поднимают в разных контурах. Где-то эмбеддинги на том же шлюзе, где-то — своим
    сервисом с другим адресом и без ключа. Пустой адрес обязан означать «как у чата»,
    иначе переезд превращается в правку файлов руками.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    AG = importlib.import_module("agent_core")
    E = importlib.import_module("kb_embed")

    same = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://gateway/v1",
                            "AURORA_AGENT_BACKEND_1_KEY": "k"})
    assert same["embed"] == {"url": "", "key": "", "model": "bge-m3",
                             "fallback": False}, same["embed"]
    assert E.endpoints(same)[0]["url"] == "http://gateway/v1", "не взято кольцо агента"

    own = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://gateway/v1",
                           "AURORA_EMBED_URL": "http://vectors.example.com/v1/",
                           "AURORA_EMBED_MODEL": "e5-large", "AURORA_EMBED_KEY": "e"})
    assert own["embed"]["model"] == "e5-large" and own["embed"]["key"] == "e"
    ends = E.endpoints(own)
    assert len(ends) == 1 and ends[0]["url"] == "http://vectors.example.com/v1", ends
    assert "gateway" not in json.dumps(ends), "чат-бэкенды подмешались к своему сервису"

    # панель настраивает те же переменные и не выпускает ключ наружу
    sys.path.insert(0, str(KIT / "cockpit"))
    ck = importlib.import_module("aurora_cockpit")
    assert "не агентские" in (ck.agent_write_env("", {"PATH": "/tmp"}).get("error") or ""), \
        "панель приняла постороннюю переменную"
    assert not (ck.agent_write_env(str(tmp), {"AURORA_EMBED_MODEL": "e5-large"}).get("error")), \
        "панель не приняла настройку эмбеддингов"


@test
def test_embedding_ring_is_independent_from_the_chat_ring(tmp: Path):
    """Чат и вектора — на разных шлюзах, и номера шлюзов идут с пропусками.

    Живая просьба: «llm работает на backend 2, embedding на backend 4». Прежде это было
    невозможно дважды. Кольцо векторов строилось из чатового: задан свой адрес — ровно
    один адрес без запасного, не задан — всё кольцо чата, включая шлюзы, которые векторов
    не считают. А разбор настроек шёл циклом «пока есть следующий номер» и обрывался на
    первой дыре: объявленный №4 без №3 не читался вовсе, и человек видел бы настроенный
    шлюз, который нигде не используется, без единой подсказки почему.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    AG = importlib.import_module("agent_core")
    E = importlib.import_module("kb_embed")

    env = {"AURORA_AGENT_BACKEND_2_URL": "http://chat2/v1",
           "AURORA_AGENT_BACKEND_2_MODEL": "27b",
           "AURORA_AGENT_BACKEND_4_URL": "http://vec4/v1",
           "AURORA_AGENT_BACKEND_4_EMBED_MODEL": "bge-m3",
           "AURORA_AGENT_BACKEND_5_URL": "http://vec5/v1",
           "AURORA_AGENT_BACKEND_5_EMBED_MODEL": "e5-large",
           "AURORA_EMBED_MODEL": "bge-m3"}
    cfg = AG.parse_config(env)
    assert [b["n"] for b in cfg["backends"]] == [2, 4, 5], \
        f"пропуск в нумерации потерял шлюз: {[b['n'] for b in cfg['backends']]}"
    assert AG.pool(cfg) == [2] and [b["n"] for b in AG.ring_order(cfg)] == [2], \
        "векторный шлюз взят в кольцо чата — вызов уйдёт на сервер без чат-модели"
    assert [r["n"] for r in E.endpoints(cfg)] == [4], \
        "кольцо векторов строится не самостоятельно"

    on = AG.parse_config({**env, "AURORA_EMBED_FALLBACK": "1"})
    assert [r["n"] for r in E.endpoints(on)] == [4], \
        "шлюз с ДРУГОЙ моделью взят в кольцо: его вектора в общий индекс не лягут"
    both = AG.parse_config({**env, "AURORA_EMBED_FALLBACK": "1",
                            "AURORA_AGENT_BACKEND_6_URL": "http://vec6/v1",
                            "AURORA_AGENT_BACKEND_6_EMBED_MODEL": "bge-m3",
                            "AURORA_EMBED_URL": "http://tei/v1"})
    assert [r["url"] for r in E.endpoints(both)] == \
        ["http://tei/v1", "http://vec4/v1", "http://vec6/v1"], E.endpoints(both)

    # Вектор чужой размерности не ложится в индекс молча: поиск после такой подмены не
    # падает, а перестаёт находить — и чинить пошли бы базу, а не настройку.
    saved_index, saved_http = E.load_index, AG.http_json
    try:
        E.load_index = lambda: {"dim": 4, "cards": {"a": {}}, "model": "bge-m3"}
        AG.http_json = lambda url, payload, key, timeout: (
            200, {"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]}, "", 0.1)
        assert E.embed(["текст"], on, "bge-m3") == [], \
            "вектор другой размерности принят — индекс испорчен молча"
    finally:
        E.load_index, AG.http_json = saved_index, saved_http

    ui = panel_sources()
    assert "AURORA_EMBED_FALLBACK" in ui and 'pre+"EMBED_MODEL"' in ui, \
        "в панели нечем объявить модель векторов и запасной путь"
    assert "backendBlock(1), backendBlock(2), backendBlock(3)" not in ui, \
        "блоки шлюзов снова жёстко три — четвёртый негде объявить"


@test
def test_agent_card_writes_where_it_reads(tmp: Path):
    """Карточка агента сохраняет туда же, откуда читает.

    Панель показывает две карточки: общая настройка машины и то, что переопределяет
    проект. Читали они по-разному, а писали одинаково — «есть выбранный проект, значит
    туда». Правка в карточке кита уезжала в проект, карточка кита перечитывала кит и
    показывала прежнее: человек нажимал «Сохранить» и видел, что всё сбросилось, — а
    настройка тем временем меняла один проект вместо машины.
    """
    ui = panel_sources()
    card = ui[ui.index("async function renderAgentCard"):]
    card = card[:card.index("\n/* ")] if "\n/* " in card else card

    assert "const scopeTarget = (scope === \"project\" && S.project)" in card, \
        "цель карточки не вычисляется из её же области"
    for call in ("/api/agent/env", "/api/agent/ping"):
        i = card.index(call)
        body = card[i:i + 260]
        assert "scopeTarget" in body, f"{call} шлёт не цель карточки, а выбранный проект"
    assert "S.project?S.project.path:\"\"" not in card and \
           "S.project ? S.project.path : \"\"" not in card.replace(
               "const scopeTarget = (scope === \"project\" && S.project) ? S.project.path : \"\";", ""), \
        "осталось место, где карточка пишет в выбранный проект помимо своей области"

    # у карточек разные ключи «несохранённого»: правка в одной не метит другую
    assert 'const dirtyKey = "agent-" + scope;' in card, \
        "обе карточки делят один ключ — предупреждение об изменениях будет врать"

    # карточка называет свою область, и запись сверяется с ней
    assert '{project: scopeTarget, scope, vars:AGV}' in card, \
        "карточка не сообщает серверу, из какой она области"

    # сама запись кладёт переменные в тот файл, который назван целью
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    project = tmp / "proj"
    project.mkdir()
    r = ck.agent_write_env(str(project), {"AURORA_AGENT_PARALLEL": "4"}, "project")
    assert r.get("ok") and str(project) in r["target"], r
    assert "AURORA_AGENT_PARALLEL=4" in (project / ".env.aurora.local").read_text(encoding="utf-8")

    # Настройки кита общие для всех проектов, настройки проекта — только его. Пути,
    # ведущие из одной области в другую, закрыты: иначе правка одного проекта молча
    # меняет поведение остальных.
    lost = ck.agent_write_env("", {"AURORA_AGENT_PARALLEL": "9"}, "project")
    assert "без пути" in (lost.get("error") or ""), \
        f"правка проекта ушла бы в общую настройку кита: {lost}"
    stray = ck.agent_write_env(str(project), {"AURORA_AGENT_PARALLEL": "9"}, "kit")
    assert "только в разделе" in (stray.get("error") or ""), \
        f"правка кита ушла бы в проект: {stray}"


@test
def test_mcp_speaks_protocol_and_never_writes(tmp: Path):
    """MCP отдаёт базу ассистенту и только читает; stdout занят протоколом.

    Любой print движка («посчитано 12 из 40») встанет посреди JSON-RPC и оборвёт сессию:
    stdout здесь не место для сообщений, а канал. И ни один инструмент не пишет в базу —
    чужой ассистент не участвует в приёмке знания и не проходит git-guard.
    """
    root = make_project(tmp)
    card(root, "Concepts/Обеспечение.md", "Правила обеспечения поставки.",
         status="verified", type="concept")
    before = sorted(p.name for p in (root / "AuroraKnowledgeDB").rglob("*.md"))

    calls = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "kb_search", "arguments": {"query": "обеспечение"}}},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "kb_card", "arguments": {"name": "Обеспечение"}}}]
    proc = subprocess.run(
        [sys.executable, str(KIT / "scripts" / "aurora_mcp.py"), "--project", str(root)],
        input="\n".join(json.dumps(c) for c in calls), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180)
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    got = sorted((json.loads(l) for l in lines),   # падает, если в канал попал чужой текст
                 key=lambda m: m["id"])            # вызовы идут в потоках — порядок не обещан
    assert len(got) == 4, [l[:80] for l in lines]
    assert got[0]["result"]["serverInfo"]["name"].startswith("aurora-"), got[0]
    names = {t["name"] for t in got[1]["result"]["tools"]}
    assert {"kb_search", "kb_card", "kb_context", "kb_index", "kb_ask",
            "artifact_spec"} == names, names
    assert "Обеспечение" in got[2]["result"]["content"][0]["text"]
    assert "Правила обеспечения" in got[3]["result"]["content"][0]["text"]
    assert sorted(p.name for p in (root / "AuroraKnowledgeDB").rglob("*.md")) == before, \
        "чтение базы через MCP изменило файлы"

    # Проектов у аналитика несколько, и подключены они одновременно. Сервер обязан
    # называть свой: одинаковые имена карточек в двух базах модель не различит, а знание
    # одного заказчика не должно попасть в артефакт другого.
    assert got[0]["result"]["serverInfo"]["name"].startswith("aurora-"), \
        got[0]["result"]["serverInfo"]
    assert all("База проекта" in tool["description"] for tool in got[1]["result"]["tools"]), \
        "инструмент не называет базу, к которой обращается"

    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    M = importlib.import_module("aurora_mcp")
    block = M.config_block([str(root), "/tmp/другой-проект"])["mcpServers"]
    assert len(block) == 2 and all(k.startswith("aurora-") for k in block), block
    # у каждого сервера свой путь, и он передан явно: перепутать базы нельзя
    paths = set()
    for rec in block.values():
        assert "--project" in rec["args"], rec
        paths.add(rec["args"][rec["args"].index("--project") + 1])
    assert len(paths) == 2, paths

    # Кривой аргумент от модели не должен ронять сервер, а значение, похожее на флаг, —
    # попадать в argparse скрипта как флаг.
    bad = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "kb_search", "arguments": {"query": "обеспечение", "limit": "abc"}}},
           {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "kb_context", "arguments": {"topic": "--index"}}},
           {"jsonrpc": "2.0", "id": 3, "method": "ping"}]
    proc = subprocess.run(
        [sys.executable, str(KIT / "scripts" / "aurora_mcp.py"), "--project", str(root)],
        input="\n".join(json.dumps(c) for c in bad), capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180)
    got = sorted((json.loads(l) for l in proc.stdout.splitlines() if l.strip()),
                 key=lambda m: m["id"])
    assert len(got) == 3, f"сервер упал на кривом аргументе: {proc.stderr[-300:]}"
    assert "usage:" not in got[1]["result"]["content"][0]["text"], \
        "тема «--index» разобрана как флаг ctx_pack"


@test
def test_a_slow_mcp_call_does_not_block_the_others(tmp: Path):
    """Долгий kb_ask (минуты) не держит ping и поиск; ответы не слипаются в канале."""
    import io
    import threading
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    M = importlib.import_module("aurora_mcp")
    release = threading.Event()
    real_call, real_channel, real_stdin = M.call_tool, M.CHANNEL, sys.stdin
    out = io.StringIO()

    def fake_call(project, name, args):
        if name == "kb_ask":
            release.wait(20)
            return "медленный ответ"
        return "быстрый ответ"

    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "kb_ask", "arguments": {"question": "?"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "kb_search", "arguments": {"query": "x"}}}]
    try:
        M.call_tool, M.CHANNEL = fake_call, out
        sys.stdin = io.StringIO("\n".join(json.dumps(m) for m in msgs))
        t = threading.Thread(target=M.serve, args=("/tmp/нет",), daemon=True)
        t.start()
        for _ in range(100):                      # быстрые ответы приходят, пока kb_ask висит
            ids = [json.loads(l)["id"] for l in out.getvalue().splitlines() if l.strip()]
            if {2, 3} <= set(ids):
                break
            time.sleep(0.05)
        assert {2, 3} <= set(ids) and 1 not in ids, \
            f"ping и поиск ждали долгий вызов: {ids}"
        release.set()
        t.join(10)
        rows = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        assert sorted(r["id"] for r in rows) == [1, 2, 3], "ответ потерян или слип в канале"
        assert not t.is_alive(), "сервер не завершился после закрытия входа"
    finally:
        release.set()
        M.call_tool, M.CHANNEL, sys.stdin = real_call, real_channel, real_stdin
    assert M.ASK_TIMEOUT < 600, "kb_ask снова ждёт десять минут"


@test
def test_dropping_an_alias_leaves_the_same_word_in_tags(tmp: Path):
    """Снять синоним — не значит вычистить слово из всей шапки.

    `drop_alias` для списка столбиком искал пункт `- слово` по всей шапке, и тег с тем же
    словом уходил вместе с синонимом.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    F = importlib.import_module("kb_fix")
    text = ("---\ntitle: \"X\"\naliases:\n  - Заявка\n  - Иное\ntags:\n  - Заявка\n"
            "  - прочее\n---\nтело\n")
    out = F.drop_alias(F.Card("AuroraKnowledgeDB/Concepts/X.md", text), "Заявка")
    assert "aliases:\n  - Иное\ntags:\n  - Заявка\n  - прочее" in out, out


@test
def test_graph_insights_name_communities_bridges_islands(tmp: Path):
    """Карта связей: сообщества, мосты и острова считаются, а не угадываются.

    «73% карточек связаны» не отвечает на вопрос, который у человека есть: где знание
    разорвано. Отвечают острова (сюда не дойти по ссылкам) и мосты (единственная связь
    между темами: порвётся — темы разъедутся, и никто не заметит).
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    G = importlib.import_module("kb_graph")

    # две плотные группы, один мост между ними и одинокая карточка
    pairs = {}
    def link(a, b):
        pairs.setdefault(a, set()).add(b); pairs.setdefault(b, set()).add(a)
    for a, b in (("a1","a2"),("a2","a3"),("a3","a1")): link(a, b)
    for a, b in (("b1","b2"),("b2","b3"),("b3","b1")): link(a, b)
    link("a1", "b1")                       # мост
    pairs.setdefault("одиночка", set())

    ins = G.insights(pairs, {})
    assert ("a1", "b1") in ins["bridges"], ins["bridges"]
    assert "одиночка" in ins["islands"], ins["islands"]
    assert ins["label"]["a2"] == ins["label"]["a3"], "плотная группа не собралась"
    assert len({ins["label"][x] for x in ("a2", "b2")}) == 2, "две группы слились в одну"


@test
def test_semantic_index_is_optional_and_hybrid(tmp: Path):
    """Смысл добавляется к словам, а не заменяет их — и выборка не падает без индекса.

    Вектора считает внешняя модель, а внешнее недоступно ровно тогда, когда нужнее
    всего: нет сети, сменили модель, индекс не собран. Пак обязан в этом случае просто
    собраться по словам, а не отказать.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    C = importlib.import_module("ctx_pack")
    E = importlib.import_module("kb_embed")

    root = make_project(tmp)
    card(root, "Concepts/Возврат-обеспечения.md",
         "Возврат обеспечения заявителю при отказе в выпуске.",
         status="verified", type="concept")

    # индекса нет — пак собирается словами и молчит про смысл
    out = run("ctx_pack.py", "возврат обеспечения", "--no-log", cwd=root).stdout
    assert "поиск по словам" in out and "и смыслу" not in out, out[:200]
    assert "Возврат-обеспечения" in out or "Возврат обеспечения" in out

    # индекс чужой модели не годится: молча считать его своим — врать о выборке
    import os as _os, json as _json
    meta = root / "AuroraKnowledgeDB" / "meta"
    meta.mkdir(parents=True, exist_ok=True)
    (meta / "embeddings.json").write_text(_json.dumps(
        {"model": "другая-модель", "dim": 2, "built": "2026-01-01",
         "cards": {"Возврат-обеспечения": {"hash": "x", "row": 0}}}), encoding="utf-8")
    cwd = _os.getcwd()
    try:
        _os.chdir(root)
        importlib.reload(E)
        assert E.search("вопрос", {"backends": [], "request_timeout": 5}, "bge-m3") == [], \
            "индекс, собранный другой моделью, принят за свой"
    finally:
        _os.chdir(cwd)

    # близость по смыслу поднимает карточку, но только выше порога случайности
    fake = type("C", (), {"stem": "Возврат-обеспечения", "title": "Возврат обеспечения",
                          "aliases": [], "tags": "", "text": "текст", "summary": ""})()
    base = C.score(fake, "деньги за поставку")
    weak = C.score(fake, "деньги за поставку", {"Возврат-обеспечения": 0.30})
    strong = C.score(fake, "деньги за поставку", {"Возврат-обеспечения": 0.75})
    assert weak == base, "случайная близость 0.30 не должна ничего добавлять"
    assert strong > base, "близость 0.75 не повлияла на отбор"


@test
def test_context_pack_finds_by_words_not_by_phrase(tmp: Path):
    """Пак ищет по словам запроса, а не по фразе целиком.

    Живой запрос аналитика — предложение: «вернуть обеспечительный платёж после
    аннулирования». Такой строки в базе нет никогда, и пак собирался из трёх случайных
    карточек. Слова из неё есть на каждой второй странице, а словоформы («платежа» —
    «платёж») должны сходиться, иначе половина базы невидима.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    C = importlib.import_module("ctx_pack")

    assert C.norm("обеспечительного") == C.norm("обеспечительный"), "словоформы разошлись"
    assert C.norm("платежа") == C.norm("платёж"), "ё и словоформа разводят пару"
    assert "как" not in C.words("как вернуть платёж"), "стоп-слово попало в запрос"

    root = make_project(tmp)
    card(root, "Concepts/Возврат-обеспечительного-платежа.md",
         "Заявитель возвращает обеспечительный платёж после аннулирования документа.",
         status="verified", type="concept")
    card(root, "Concepts/Погода-в-городе.md", "Про погоду и ничего больше.",
         status="verified", type="concept")

    out = run("ctx_pack.py", "вернуть обеспечительный платёж после аннулирования",
              "--no-log", cwd=root).stdout
    assert "Возврат-обеспечительного-платежа" in out or "Возврат обеспечительного платежа" in out, \
        "карточка по теме не найдена по свободной формулировке"
    assert "Погода" not in out, "в пак попала карточка, не имеющая отношения к запросу"


@test
def test_pack_answers_where_development_got_to(tmp: Path):
    """Статус задачи приходит в пак из зеркала Jira, а не из карточек.

    Живой случай: аналитик спросил статус истории US-4.7.2, база честно ответила «не
    знаю» — а задача лежала в зеркале рядом, со статусом «Бэклог». Знание было в проекте,
    но не на пути к модели: пак собирается только из карточек. Распылять статус по
    карточкам нельзя — он неправда на следующий день после переноса задачи; значит его
    надо читать из зеркала при каждой сборке пака.
    """
    root = make_project(tmp)
    mirror = root / "Sources/JIRA"
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "PRJ-480.md").write_text(
        '---\nkey: "PRJ-480"\ntitle: "US-4.7.2. Центр уведомлений"\ntype: "История"\n'
        'status: "Бэклог"\nepic_title: "Epic 4.7 Информационные разделы"\n'
        'updated: "2026-06-10 15:06:39"\n---\n\n# PRJ-480\n', encoding="utf-8")
    (mirror / "PRJ-999.md").write_text(
        '---\nkey: "PRJ-999"\ntitle: "US-1.1.1. Чужая история"\nstatus: "Готово"\n---\n',
        encoding="utf-8")
    card(root, "Concepts/Центр-уведомлений.md",
         "Центр уведомлений собирает сообщения заявителю в одном разделе.",
         status="verified", type="concept")

    out = run("ctx_pack.py", "какой статус у истории US-4.7.2 Центр уведомлений",
              "--no-log", cwd=root).stdout
    assert "Состояние разработки" in out, "раздела со статусами задач в паке нет"
    assert "PRJ-480" in out and "Бэклог" in out, f"статус задачи не дошёл до модели:\n{out}"
    assert "PRJ-999" not in out, "в пак попала задача, о которой не спрашивали"
    assert "не карточки базы" in out, \
        "снимок внешней системы не отделён от знания — модель сошлётся на него как на факт"

    # Вопрос не про задачи — таблицы быть не должно: пак не место для выгрузки бэклога
    plain = run("ctx_pack.py", "центр уведомлений заявителя", "--no-log", cwd=root).stdout
    assert "Состояние разработки" not in plain, \
        "статусы задач подмешиваются в каждый пак, хотя о них не спрашивали"

    # Карточек по теме нет, а задача есть — это ответ, а не «ничего не найдено»
    only = run("ctx_pack.py", "статус US-1.1.1", "--no-log", cwd=root, expect_rc=0).stdout
    assert "PRJ-999" in only and "карточек по теме нет" in only, \
        f"пак сдался там, где зеркало знает ответ:\n{only}"



@test
def test_build_card_is_idempotent_for_the_same_source(tmp: Path):
    """Повторный разбор того же источника не падает, а конфликт имён — падает.

    Источник правят и разбирают снова: на живом прогоне обновлённая страница уронила
    агента отказом «карточка уже есть». Та же карточка из того же источника — это
    повторный проход. Чужое имя из другого источника — настоящий конфликт.
    """
    root = make_project(tmp)
    for name in ("первый", "второй"):
        src = root / "Raw" / "project" / f"{name}.md"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text(f"# Тема\n\n" + "текст. " * 60, encoding="utf-8")

    run("build_plan.py", "--card", "Общая тема", "--source", "Raw/project/первый.md",
        "--sections", "1", "--to", "Concepts", "--apply", cwd=root)
    again = run("build_plan.py", "--card", "Общая тема", "--source", "Raw/project/первый.md",
                "--sections", "1", "--to", "Concepts", "--apply", cwd=root)
    assert again.returncode == 0 and "без изменений" in again.stdout, again.stdout[-200:]
    card_text = (root / "AuroraKnowledgeDB/Concepts/Общая-тема.md").read_text(encoding="utf-8")
    assert card_text.count("текст.") == 60, \
        f"повтор из того же источника задвоил текст: {card_text.count('текст.')} вместо 60"

    clash = run("build_plan.py", "--card", "Общая тема", "--source", "Raw/project/второй.md",
                "--sections", "1", "--to", "Concepts", "--apply", cwd=root, expect_rc=1)
    assert "из другого источника" in clash.stdout + clash.stderr


@test
def test_build_card_refuses_section_outside_schema(tmp: Path):
    """`--to` принимает только разделы схемы: иначе карточка ложится в новую папку.

    На живом прогоне агент завёл `Models`, `Модель данных` и `Требования` — движок
    послушно создал папки, а doctor нашёл их блокером уже после того, как карточки
    туда легли. Схему базы расширяют релизом кита, а не значением флага.
    """
    root = make_project(tmp)
    src = root / "Raw" / "project" / "источник.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Тема\n\n" + "текст. " * 60, encoding="utf-8")

    bad = run("build_plan.py", "--card", "Карточка", "--source", "Raw/project/источник.md",
              "--sections", "1", "--to", "Модель данных", "--apply", cwd=root, expect_rc=1)
    assert "нет в схеме" in bad.stdout + bad.stderr
    assert not (root / "AuroraKnowledgeDB" / "Модель данных").exists(), \
        "папка вне схемы всё равно создалась"

    run("build_plan.py", "--card", "Карточка", "--source", "Raw/project/источник.md",
        "--sections", "1", "--to", "Concepts", "--apply", cwd=root)
    assert (root / "AuroraKnowledgeDB/Concepts/Карточка.md").exists()


@test
def test_build_plan_keeps_its_own_output_out_of_the_plan(tmp: Path):
    """Карточка, собранная в Reference, не возвращается в план новым источником.

    В справочниках источник — сам справочник, который вели руками. Извлечённая из него
    карточка ложится рядом, и план начинал расти от собственной работы: разобрал
    источник — получил источник. На живом проекте так набралось 54 фантомных источника.
    Отличаем по `source:` в шапке.
    """
    root = make_project(tmp)
    ref = root / "AuroraKnowledgeDB" / "Reference"
    ref.mkdir(parents=True, exist_ok=True)
    (ref / "Справочник-кодов.md").write_text(
        "---\ntitle: \"Справочник кодов\"\ntype: reference\n---\n\n# Коды\n\n"
        + "| Код | Значение |\n|---|---|\n" + "| A | значение |\n" * 30, encoding="utf-8")
    (ref / "Извлечённая-тема.md").write_text(
        "---\ntitle: \"Извлечённая тема\"\ntype: reference\n"
        "source: \"AuroraKnowledgeDB/Reference/Справочник-кодов.md\"\n---\n\n"
        + "# Тема\n\n" + "текст. " * 60, encoding="utf-8")

    out = run("build_plan.py", "--tasks", "0", cwd=root).stdout
    assert "Справочник-кодов.md" in out, "рукописный справочник пропал из плана"
    assert "Извлечённая-тема.md" not in out, \
        "карточка, собранная движком, вернулась в план новым источником"


@test
def test_slice_shows_agent_more_than_a_person(tmp: Path):
    """`--slice-chars` управляет длиной превью секции.

    Человек смотрит в сам источник, ему хватает строки. Агент источника не открывает и
    судит только по этому тексту: на коротком превью он объявил пустым нормальный
    справочник — «содержимого не видно», — и источник ушёл бы из плана навсегда.
    """
    root = make_project(tmp)
    src = root / "Raw" / "project" / "источник.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Тема\n\n" + "буквы " * 400, encoding="utf-8")

    short = run("build_plan.py", "--slice", "Raw/project/источник.md", cwd=root).stdout
    long = run("build_plan.py", "--slice", "Raw/project/источник.md",
               "--slice-chars", "900", cwd=root).stdout
    head = lambda s: s.split("ЗАДАНИЕ", 1)[0]
    assert len(head(long)) > len(head(short)) + 500, \
        "длинное превью не длиннее короткого — агент по-прежнему судит вслепую"


@test
def test_agent_build_judges_sources_without_structure(tmp: Path):
    """Источник без секций: пусто (отметить) или человеку — но не «всё человеку».

    В живом плане сотнями лежат страницы-оглавления: заголовок и ссылка. Сваливать их
    человеку — значит не разобрать план никогда. Отметку «пусто» подтверждает второе
    мнение: она убирает источник из плана навсегда, и потерянное знание само не всплывёт.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp, git=True)
    src = root / "Raw" / "project" / "оглавление.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Оглавление\n\n[ссылка](http://example.org)\n" + "\u00a0 " * 120,
                   encoding="utf-8")
    cfg = R.AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
                             "AURORA_AGENT_BACKEND_1_MODEL": "m"})

    def answer(text):
        return lambda c, role, msgs, **kw: {"ok": True, "text": text, "reasoning": "",
                                            "backend": 1, "model": "test", "seconds": 0.1,
                                            "waited": 0, "ring": 1, "log": []}

    both_empty = R.solve_source(cfg, str(root), "Raw/project", "Raw/project/оглавление.md",
                                False, True, call=answer('{"empty": "только ссылка"}'))
    assert both_empty["status"] == "отметил бы пустым", both_empty

    # мнения разошлись — источник остаётся человеку, а не уходит из плана
    calls = iter(['{"empty": "только ссылка"}', '{"keep": "тут есть правило"}'])
    split = R.solve_source(cfg, str(root), "Raw/project", "Raw/project/оглавление.md",
                           False, True,
                           call=lambda c, role, msgs, **kw: answer(next(calls))(c, role, msgs))
    assert split["status"] == "без секций — человеку", split
    assert "разошлись" in split["note"], split["note"]


@test
def test_agent_build_oracle_counts_processed_not_left(tmp: Path):
    """Оракул сборки считает по «обработано»: «осталось» умеет расти само.

    Правка источника возвращает его в план значком ♻️ — и прогон, сделавший всё верно,
    выглядел сбойным. На живом прогоне так и вышло: «план сдвинулся на 41, а агент
    объявил разобранными 42», причём 42 действительно ушли из плана.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    steps = [{"alias": "и", "status": "разобран", "note": "", "backends": [], "degraded": False},
             {"alias": "т", "status": "разобран", "note": "", "backends": [], "degraded": False}]

    # рядом правили источник: осталось не убавилось, но обработано выросло на два
    grew = {"steps": steps, "total": 2, "left": 0, "stopped": "", "limited": False,
            "before": {"left": 100, "done": 10, "errors": 5},
            "after": {"left": 100, "done": 12, "errors": 5}}
    ok, why = R.verdict_build(grew, apply=True)
    assert ok, f"честный прогон признан сбойным: {why}"

    # а вот объявил больше, чем засчитал движок, — это уже расхождение
    lied = {**grew, "after": {"left": 99, "done": 11, "errors": 5}}
    ok2, why2 = R.verdict_build(lied, apply=True)
    assert not ok2 and "движок засчитал" in why2, why2


@test
def test_health_shows_where_we_are_in_building(tmp: Path):
    """Дашборд знает, сколько источников ждут разбора и чем кончился прогон агента.

    Панель показывала здоровье уже собранного и молчала о том, сколько собрать осталось:
    главное число всей работы человек узнавал, только запустив сборку.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    root = make_project(tmp)
    src = root / "Raw" / "project" / "источник.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Тема\n\n" + "текст. " * 60, encoding="utf-8")
    runs = root / "AuroraKnowledgeDB" / "meta" / "agent-runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "2026-08-10_1200_build.md").write_text(
        "# Агент · сборка базы\n\n**Оракул:** ✗ не разобрано: 2\n\n"
        "## Осталось на следующий прогон: 7\n", encoding="utf-8")

    b = ck.build_progress(str(root))
    assert b["total"] >= 1 and b["left"] >= 1, b
    a = ck.last_agent_run(str(root))
    assert a["task"] == "build" and a["ok"] is False and a["left"] == 7, a
    assert "не разобрано" in a["why"], a


@test
def test_agent_build_walks_the_plan_not_one_partition(tmp: Path):
    """Без --partition агент идёт по плану подряд, а не по одной партии.

    Партии придуманы под контекст модели, которой человек отдаёт задание целиком. Агент
    берёт по одному источнику: партия, где осталось лишь неподъёмное, заставляла каждый
    следующий прогон брать то же самое и отчитываться «разобрано 0».
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    seen = {}

    def fake(cwd, script, args, timeout=300):
        seen["args"] = args
        return {"ok": True, "rc": 0, "out": "", "refused": ""}

    orig, R.run_command = R.run_command, fake
    try:
        R.read_partition("/tmp", 0)
        assert seen["args"] == ["--tasks", "0"], seen["args"]
        R.read_partition("/tmp", 3)
        assert seen["args"] == ["--partition", "3"], seen["args"]
    finally:
        R.run_command = orig


@test
def test_agent_build_refuses_cards_from_same_sections(tmp: Path):
    """Две карточки из одних секций — одно тело под двумя именами. Считается, не спрашивается.

    Живой прогон собрал две карточки с разными именами из одних и тех же секций 3,4:
    тела вышли дословно одинаковыми. Критик пропустил — и правильно, что мы на него не
    рассчитываем: пересечение множеств проверяется счётом, а не мнением модели.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    secs = [(1, "Первая", 500, ""), (2, "Вторая", 500, ""), (3, "Третья", 500, "")]

    why = R.check_cards([{"title": "А", "sections": "1,2"}, {"title": "Б", "sections": "2,3"}], secs)
    assert "секция 2" in why and "одним телом" in why, why
    assert not R.check_cards([{"title": "А", "sections": "1"},
                              {"title": "Б", "sections": "2-3"}], secs), "честный разбор отклонён"
    assert "которых нет" in R.check_cards([{"title": "А", "sections": "7"}], secs)

    # служебные секции — постоянный список, а не предмет спора двух моделей
    with_service = secs + [(4, "История изменений", 200, "")]
    assert "служебная секция" in R.check_cards([{"title": "А", "sections": "1,4"}], with_service)
    assert not R.check_cards([{"title": "А", "sections": "1"}], with_service)
    assert "не разобраны номера" in R.check_cards([{"title": "А", "sections": "ага"}], secs)


@test
def test_agent_sees_every_conflict_not_just_printed(tmp: Path):
    """Агент получает все конфликты, а не первые 15 из отчёта для человека.

    Отчёт `kb:repair --aliases` режет список: читать восемнадцатую строку человеку незачем.
    Агент, читавший тот же текст, разбирал 15 из 19 и честно докладывал «каждый конфликт
    разобран» — оракул подтверждал успех, не зная о четырёх невидимых. Обрезка для глаз
    не должна становиться обрезкой для машины, а оракул обязан ловить неполный список.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp, git=True)
    for i in range(18):
        card(root, f"Processes/Процесс-{i}.md", "Процесс.", aliases=f'["Имя-{i}"]',
             type="process")
        card(root, f"Systems/Система-{i}.md", "Система.", aliases=f'["Имя-{i}"]',
             type="system")

    conflicts = R.read_conflicts(str(root))
    assert len(conflicts) == 18, f"агент увидел {len(conflicts)} конфликтов из 18"
    assert all(len(cards) == 2 for _alias, cards in conflicts), "карточки конфликта потеряны"

    # оракул не принимает прогон, где список пришёл короче, чем видит линтер
    blind = {"steps": [{"alias": "Имя-0", "status": "уточнено", "note": "", "backends": [],
                        "degraded": False}],
             "before": {"conflicts": 18, "errors": 18}, "after": {"conflicts": 17, "errors": 18},
             "total_conflicts": 1, "limited": False, "seconds": 1.0}
    ok, why = R.verdict(blind, apply=True)
    assert not ok and "неполным" in why, f"оракул принял прогон со слепым пятном: {why}"

    # с явным --limit это осознанная проба, а не слепота
    ok, _why = R.verdict({**blind, "limited": True}, apply=True)
    assert ok, "оракул не принял пробу по --limit"
