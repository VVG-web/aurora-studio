"""Проверки движка Aurora, часть 2 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys

from harness import (  # noqa: F401
    KIT,
    SCRIPTS,
    _js_function,
    card,
    card_srcs,
    make_project,
    panel_sources,
    run,
    stub_messages,
    test,
    why,
)


@test
def test_jira_matches_stories_by_title(tmp: Path):
    """История и задача связываются по названию: одинаковый номер — ещё не связь."""
    root = make_project(tmp)
    mirror = root / "Sources/JIRA"; mirror.mkdir(parents=True, exist_ok=True)
    us = root / "Artifacts/us"; us.mkdir(parents=True, exist_ok=True)

    def issue(name, key, title):
        (mirror / f"{name}.md").write_text(
            f"# {key}: {title}\n\n- **URL:** https://jira.example/browse/{key}\n"
            "- **Type:** Task\n- **Status:** В работе\n", encoding="utf-8")

    def story(uid, title, jira=""):
        link = f"| Ссылка_на_JIRA | [{jira}](https://jira.example/browse/{jira}) |\n" if jira else ""
        (us / f"{uid}._{title.replace(' ', '_')}.md").write_text(
            f"# Задача на разработку истории\n\n| | |\n| --- | --- |\n"
            f"| Название | {uid}. {title} |\n{link}", encoding="utf-8")

    story("US-3.1.1", "Приём Заявка из ГП 3", "PRJ-1")
    issue("t1", "PRJ-1", "US-3.1.1. Приём Заявка из ГП 3")           # совпало
    story("US-3.2.2", "Проверка текстовых полей")
    issue("t2", "PRJ-2", "US-3.2.2. Логирование операций")          # номер тот, имя другое
    story("US-3.3.3", "История без задачи")
    issue("t4", "PRJ-4", "US-9.9.9. Задача без истории")

    cp = run("jira_status.py", cwd=root)
    out = cp.stdout
    assert "Истории и задачи: 1 совпали по названию" in out, f"матч по названию не сработал:\n{out}"
    renamed = out.split("Названия разошлись")[1].split("Истории без задачи")[0]
    assert "US-3.2.2" in renamed and "PRJ-2" in renamed, "расхождение названий не показано"
    assert "US-3.1.1" not in renamed, "совпавшая пара попала в расхождения"
    assert "US-3.3.3" in out.split("Истории без задачи")[1][:200], "история без задачи не найдена"
    assert "US-9.9.9" in out, "задача без истории не найдена"
    # «US-4.4.3» сам похож на ключ задачи: номер истории должен срезаться первым
    import importlib, sys as _s
    _s.path.insert(0, str(SCRIPTS)); js = importlib.import_module("jira_status")
    assert js.norm_title("US-4.4.3. Печатная форма") == js.norm_title("PRJ-470: US-4.4.3. Печатная форма"), \
        "номер истории похож на ключ задачи — он должен срезаться первым"


@test
def test_retire_cleans_templates_too(tmp: Path):
    """Шаблон — источник новых карточек: поле, оставшееся в нём, вернётся в базу."""
    root = make_project(tmp, git=True)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/spec_template.md").write_text(
        '---\ntitle: "Шаблон"\nstatus: draft\naudience: [SA, Dev]\n---\n\nтело\n', encoding="utf-8")
    card(root, "Glossary/Термин.md", "тело", status="imported")

    cp = run("aurora_doctor.py", cwd=root)
    assert "в шаблонах" in cp.stdout, "doctor молчит о выведенных полях в шаблонах"

    run("kb_fix.py", "--retire", "--apply", "--allow-dirty", cwd=root)
    assert "audience" not in (root / "Templates/spec_template.md").read_text(encoding="utf-8"), \
        "kb:retire не почистил шаблон"
    cp = run("aurora_doctor.py", cwd=root)
    assert "в шаблонах" not in cp.stdout, "предупреждение осталось после чистки"


@test
def test_cockpit_ui_version_tracks_kit(tmp: Path):
    """Панель не должна молча отстать от ядра: отставший интерфейс выглядит рабочим."""
    ui = panel_sources()
    m = re.search(r'const UI_VERSION = "([^"]+)"', ui)
    assert m, "в панели не объявлена версия UI_VERSION"
    kit = (KIT / "VERSION").read_text(encoding="utf-8").strip()
    ui_v, kit_v = m.group(1), kit
    assert ui_v == kit_v, (
        f"панель собрана под {ui_v}, ядро {kit_v} — версии разошлись. ",
        "Либо обновите cockpit/ui/panel.js под новые команды и метрики, ",
        "либо поднимите UI_VERSION осознанно")
    assert ui_v.split(".")[:2] == kit_v.split(".")[:2], (
        f"панель собрана под {ui_v}, ядро {kit_v} — младшая версия разошлась. "
        "Либо обновите cockpit/ui/panel.js под новые команды и метрики, "
        "либо поднимите UI_VERSION осознанно")


@test
def test_cockpit_apply_is_reachable(tmp: Path):
    """Пишущую команду нужно уметь применить: панель без «Применить» умеет только dry-run."""
    ui = panel_sources()
    assert 'id="consoleApply"' in ui, "в консоли нет места для кнопки «Применить»"
    assert "PENDING_APPLY" in ui, "панель не помнит предпросмотр — применять нечего"
    # признак жил в RUN, а RUN пересоздаётся при каждом открытии ящика: кнопка на нём
    # включалась только пока ящик закрыт
    assert "RUN.previewed" not in ui, \
        "«Применить» снова зависит от признака, который сбрасывается при открытии команды"


@test
def test_panel_opens_without_waiting(tmp: Path):
    """Панель не должна выглядеть зависшей на старте.

    Реестр собирается из `--help` полусотни скриптов, а проверка окружения импортировала
    тяжёлые модули — на каждое открытие уходили секунды, и панель казалась мёртвой.
    `--help` теперь спрашивается один раз на скрипт, реестр кэшируется на диске, наличие
    модуля проверяется поиском, а не импортом.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    sys.path.insert(0, str(KIT / "cockpit"))
    import kit_commands as K
    import aurora_cockpit as ck

    K._HELP.clear()
    calls = {"n": 0}
    real = K.help_text

    def counted(impl):
        if impl.split()[0] not in K._HELP:
            calls["n"] += 1
        return real(impl)

    K.help_text = counted
    try:
        impl = "kb_lint.py"
        for fn in (K.flags_of, K.flag_help, K.flag_args, K.required_flags):
            fn(impl)
        assert calls["n"] == 1, f"`--help` запускается {calls['n']} раза на одну команду"
    finally:
        K.help_text = real

    assert "importlib.util.find_spec" in (KIT / "cockpit/aurora_cockpit.py").read_text(
        encoding="utf-8"), "наличие модуля снова проверяется импортом"
    ck.CACHE.clear()
    first = ck.environment()
    assert ck.environment() is first, "окружение пересчитывается на каждый запрос"


@test
def test_cockpit_runlog_lives_in_the_project(tmp: Path):
    """Журнал запусков — файл проекта, а не память вкладки.

    «Когда последний раз обновляли зеркала» спрашивает вся команда, а ответ, лежащий в
    localStorage одного браузера, отвечает только одному человеку. Поэтому: файл в
    `.aurora/`, версия ядра и автор в записи, и чтение его панелью обратно.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as ck
    root = make_project(tmp)
    ck.write_runlog(str(root), "sync:jira", 0, "sync:jira --force")
    ck.write_runlog(str(root), "kit:doctor", 1, "kit:doctor")
    log = (root / "AuroraKnowledgeDB/meta/run_log.md")       # с 1.158.0 — не в папке движка
    assert log.is_file(), "журнал не лёг в проект — команда его не увидит"
    runs = ck.read_runlog(str(root))
    assert set(runs) == {"sync:jira", "kit:doctor"}, runs
    assert runs["kit:doctor"]["rc"] == 1, "код возврата потерян"
    assert runs["sync:jira"]["kit"] == ck.kit_version(), \
        "в записи нет версии ядра — непонятно, на чём команда отработала"
    assert runs["sync:jira"]["who"], "непонятно, кто запускал"

    # повторный запуск заменяет строку, а не копит их: файл едет в git и не должен
    # превращаться в источник конфликтов при слиянии веток
    ck.write_runlog(str(root), "sync:jira", 2, "sync:jira")
    assert ck.read_runlog(str(root))["sync:jira"]["rc"] == 2
    rows = [l for l in log.read_text(encoding="utf-8").splitlines()
            if l.startswith("| sync:jira |")]
    assert len(rows) == 1, f"журнал копит дубли вместо строки на команду: {rows}"

    # панель отдаёт журнал вместе со здоровьем — иначе отметкам у команд неоткуда взяться
    assert "runs" in ck.health(str(root)), "журнал не попадает в /api/health"

    # …но ЖДАТЬ здоровья журнал не должен. Он читается мгновенно, а здоровье зовёт
    # несколько команд: на живом проекте девять секунд. Всё это время «Консоль»
    # показывала «выберите проект» при выбранном проекте, и человек читал это как
    # «журнал потерян» — так и написал. Проверки на это не было.
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"/api/runlog"' in src, "у журнала нет своего маршрута — он едет внутри здоровья"

    ui = panel_sources()
    assert "function assistantTasks" in ui and "task.label" in ui, \
        "задания ассистенту из консоли нечем забрать в буфер"
    assert "S.health && S.health.runs" in ui, "панель снова читает историю из браузера"
    assert "async function loadRuns()" in ui, "журнал не тянется отдельно от здоровья"
    pick = ui[ui.index("async function pick("):ui.index("async function pick(") + 3200]
    assert "loadRuns()" in pick, "выбор проекта не обновляет журнал"
    assert pick.index("loadRuns()") < pick.index('api("/api/health'), \
        "журнал тянется ПОСЛЕ здоровья — значит ждёт его, и отвязка бессмысленна"
    assert 'if (view==="console")' in ui and "loadRuns()" in ui[ui.index('if (view==="console")'):
                                                               ui.index('if (view==="console")') + 200], \
        "переход на «Консоль» не обновляет журнал"
    entry = ui[ui.index('if (view==="console")'):ui.index('if (view==="console")') + 200]
    for call in ("renderHistory()", "drawTaskButton()", "drawLiveJobs()"):
        assert call in entry, \
            f"на входе в «Консоль» не восстанавливается {call}: журнал, задание и то, " \
            f"что идёт прямо сейчас"
    # Разделы спрашивают журнал через ctx (`ctx.runs.last`), ядро — напрямую: считаем оба.
    assert ui.count("lastRun(") + ui.count("runs.last(") >= 3, \
        "отметка последнего запуска стоит не везде: команды, сценарии, журнал"


@test
def test_cockpit_skins_declare_supported_core(tmp: Path):
    """Скин красит то, чего в панели могло ещё не быть — значит, обязан назвать версию."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as ck
    for s in ck.skins():
        head = ck.skin_css(s["id"])[:400]
        assert "for:" in head, f"скин {s['id']} не объявляет версию ядра"
        assert s["for"], f"скин {s['id']}: версия не разобралась"
        assert not s["behind"], \
            f"скин {s['id']} собран под {s['for']}, а ядро {ck.kit_version()}"


@test
def test_cockpit_marks_command_outcome(tmp: Path):
    """Код 1 — это «нашла, что чинить», а не «сломалась»: красить их одинаково — врать.

    doctor с ошибками и аудит с расхождениями отрабатывают штатно и возвращают 1;
    не пустивший git-гейт возвращает 2. Отметка у команды должна их различать.
    """
    ui = panel_sources()
    m = re.search(r"function rcMark\(rc\)\{(.*?)\n\}", ui, re.S)
    assert m, "нет единой трактовки кода возврата — цвета разъедутся по экранам"
    body = m.group(1)
    assert '"ok"' in body and '"warn"' in body and '"bad"' in body, \
        "исходов по-прежнему два: «успех» и «всё плохо»"
    assert "rc === 1" in body, "код 1 не отделён от настоящего сбоя"
    # Список команд уехал в раздел-папку: журнал он спрашивает через ctx.
    assert ("lastRun(r.cmd)" in ui or "ctx.runs.last(r.cmd)" in ui), \
        "в списке команд не видно итога последнего запуска"


@test
def test_build_can_run_the_whole_plan_overnight(tmp: Path):
    """Первичная сборка идёт партиями сама, а не девяноста нажатиями кнопки.

    Партия агента ограничена нарочно: обозримый прогон, обозримый откат. Но у проекта с
    тремя годами истории источников полторы тысячи, и по пятнадцать за раз это девяносто
    прогонов. Цикл делает то же самое сам — с чекпойнтом и коммитом на каждую партию,
    с потолком по времени и остановкой, если план перестал двигаться.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")

    assert "--until-done" in src and "--hours" in src, "нет режима сплошного разбора"
    # Прогресс меряется пройденными источниками, а не оставшимися: источник без
    # структуры уходит человеку, «осталось» не меняется — и ночной прогон вставал на
    # первой такой пачке, разобрав четырнадцать карточек из тысячи трёхсот.
    assert "done_after <= done_before" in src, \
        "цикл снова меряет прогресс по «осталось» — пачка отложенных его остановит"
    assert "left_after >= left_before" not in src, "старая метрика прогресса вернулась"
    assert 'commit_result(cwd, "agent:build",\n                              f"партия' in src, \
        "партии не коммитятся по отдельности — откатить можно будет только всё сразу"
    assert "if a.apply and not self_looped:" in src, \
        "результат коммитится дважды: и в цикле, и в конце"
    guard = src.split("self_looped = ")[1][:200]
    assert 'a.task == "build"' in guard and "a.until_done" in guard, \
        "итоговый коммит перестал знать про петлю сборки — коммитов снова будет два"

    # шаг есть в маршруте пересборки, и флаги существуют у команды
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    rebuild = scen.split("[rebuild]")[1].split("\n[")[0]
    # С 1.94 маршрут разбирает проект **циклами**, а не одной командой до упора: фазы по
    # всей базе при остановке на середине оставляли карточки без типов, тезисов и связей.
    assert "цикл:" in rebuild and "конец цикла" in rebuild, \
        "маршрут пересборки не нарезан циклами — остановка на середине снова даст заготовки"
    cyc = rebuild.split("цикл:")[1].split("конец цикла")[0]
    for need in ("agent:build", "kb:kind", "agent:distill", "kb:links"):
        assert need in cyc, f"в цикле нет шага {need} — оборот даёт не готовое знание"
    assert "на ночь" in rebuild, "шаг не предупреждает, что это часы работы"
    assert hasattr(R, "build_left"), "счётчик плана переименован — цикл сломается молча"


@test
def test_ask_tab_names_the_model_and_lets_you_pick_it(tmp: Path):
    """«Спросить» показывает, кто ответил, и даёт выбрать модель.

    У основной и запасной модели разные скорость и качество. Пока вкладка молчала об
    исполнителе, медленный ответ запасной выглядел как ответ основной — и человек делал
    выводы о базе по ответу другой модели.
    """
    ui = panel_sources()
    for need, why in (('id="askBackend"', "нет выбора модели"),
                      ('id="askWho"', "не видно, кто ответил"),
                      ('id="askPing"', "нельзя проверить основную модель"),
                      ('id="askPrimary"', "нет возврата на основную")):
        assert need in ui, why
    assert 'args.push("--backend", pick)' in ui, "выбор модели не уходит в команду"
    assert "(запасная)" in ui, "ответ запасной модели не отмечен как запасной"
    assert "/api/agent/retry-primary" in ui and "/api/agent/ping" in ui, \
        "кнопки не привязаны к серверу"

    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    assert '"--backend"' in src and 'cfg = {**cfg, "backends": picked}' in src, \
        "команда не умеет спрашивать конкретную модель"
    assert 'бэкенда №{a.backend} нет в настройке' in src, \
        "выбор несуществующей модели подменяется молча — человек выбирал сознательно"


@test
def test_ask_tab_refills_the_model_list_and_names_a_failure(tmp: Path):
    """Выбор модели и история «Спросить» переживают сбой запроса и называют его.

    Живой случай: вкладка открылась без истории и с пустым выбором модели. Список моделей
    отмечался собранным до ответа сервера, поэтому один неудачный запрос оставлял выбор
    пустым до перезагрузки страницы, а сбой чтения истории выглядел как «разговоров пока
    нет». Собирался список к тому же один раз и при смене проекта показывал модели прошлого.

    Проверяем поведение, а не строки: раздел — модуль ES, его функции выполняются в node
    на заглушках `ctx` (строки он спрашивает по ключу, ключи сверяем с каталогом).
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")

    ui = panel_sources()
    assert 'id="askBackendNote"' in ui, "сбою списка моделей негде показаться"

    engine = shutil.which("node")
    if not engine:
        return            # без node поведение вкладки проверить нечем — пропускаем

    view = ck.module_file("ask", "view.js")
    assert view, "раздел «Спросить» не найден среди модулей"
    ru = json.loads(Path(ck.module_file("ask", "i18n/ru.json")).read_text(encoding="utf-8"))

    harness = """
import {fillBackends, renderHistory} from "MODULE";
const el = (tag, attrs, ...kids) => ({tag, value: attrs && attrs.value,
  textContent: kids.map(k => typeof k === "string" ? k : (k && k.textContent) || "").join("")});
const select = () => ({options: [{value: "0", textContent: "по кольцу"}], value: "0", dataset: {},
  append(o){ this.options.push(o); },
  remove(i){ const [o] = this.options.splice(i, 1);
             if (o && o.value === this.value) this.value = this.options[0].value; }});
const box = () => ({kids: [], replaceChildren(...k){ this.kids = k; },
  append(...k){ this.kids.push(...k); },
  get text(){ return this.kids.map(k => k.textContent).join(" | "); }});
const DOM = {askBackend: select(), askBackendNote: {hidden: true, textContent: ""},
             askHistory: box()};
let calls = 0;
const replies = [];
// Строку раздел спрашивает по ключу: заглушка возвращает сам ключ, и проверка видит,
// ЧТО сказано, не завися от формулировки.
const ctx = {
  t: (k, vars) => k + (vars ? ":" + JSON.stringify(vars) : ""),
  el, $: q => DOM[q.slice(1)],
  api: async () => { calls++; const r = replies.shift(); if (r instanceof Error) throw r; return r; },
  toast(){}, project: null,
};
(async () => {
  const sel = DOM.askBackend, note = DOM.askBackendNote, res = {};
  const opts = () => sel.options.map(o => o.value).join(",");
  const said = () => note.hidden ? "" : note.textContent;
  ctx.project = {path: "/p/A"};
  replies.push(new Error("Failed to fetch"));
  await fillBackends(ctx);
  res.failed = {opts: opts(), note: said()};
  replies.push({backends: [{n: 1, models: {worker: "m1"}}, {n: 2, model: "m2"}]});
  await fillBackends(ctx);
  res.retried = {opts: opts(), note: said()};
  const before = calls; await fillBackends(ctx); res.sameProjectCalls = calls - before;
  sel.value = "2"; ctx.project = {path: "/p/B"};
  replies.push({backends: [{n: 2, model: "m2b"}, {n: 3, model: "m3"}]});
  await fillBackends(ctx);
  res.switched = {opts: opts(), value: sel.value,
                  labels: sel.options.map(o => o.textContent).join(",")};
  ctx.project = {path: "/p/C"}; replies.push({backends: []});
  await fillBackends(ctx);
  res.empty = {opts: opts(), note: said()};
  replies.push({error: "проект не найден среди обнаруженных"});
  await renderHistory(ctx); res.historyError = DOM.askHistory.text;
  replies.push({threads: []});
  await renderHistory(ctx); res.historyEmpty = DOM.askHistory.text;
  console.log(JSON.stringify(res));
})();
"""
    src = tmp / "ask_tab.mjs"
    # ES-модуль по пути не импортируется: на Windows нужен адрес file:///D:/…
    src.write_text(harness.replace("MODULE", Path(view).resolve().as_uri()), encoding="utf-8")
    cp = subprocess.run([engine, str(src)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert cp.returncode == 0 and cp.stdout.strip(), \
        f"функции раздела не выполнились:\n{(cp.stderr or cp.stdout)[:800]}"
    r = json.loads(cp.stdout.strip().splitlines()[-1])

    def says(text, key):
        assert key in ru, f"раздел просит ключ, которого нет в каталоге: {key}"
        assert key in text, f"сказано не то: ждали {key}, получили {text!r}"

    assert r["failed"]["opts"] == "0", f"сбой запроса моделей оставил список: {r['failed']}"
    says(r["failed"]["note"], "ask.models_failed")
    assert r["retried"]["opts"] == "0,1,2" and not r["retried"]["note"], \
        f"после сбоя список не собрался заново — выбор пуст до перезагрузки: {r['retried']}"
    assert r["sameProjectCalls"] == 0, \
        "модели того же проекта запрашиваются заново при каждом открытии вкладки"
    assert r["switched"]["opts"] == "0,2,3" and "m2b" in r["switched"]["labels"], \
        f"при смене проекта в выборе остались модели прошлого: {r['switched']}"
    assert r["switched"]["value"] == "2", \
        "смена проекта сбросила выбор модели, которая есть и в новом проекте"
    assert r["empty"]["opts"] == "0", f"пустая настройка агента оставила модели: {r['empty']}"
    says(r["empty"]["note"], "ask.models_empty")
    says(r["historyError"], "ask.history_failed")
    assert "не найден" in r["historyError"], \
        f"причина сбоя чтения истории потеряна: {r['historyError']}"
    says(r["historyEmpty"], "ask.history_empty")


@test
def test_fallback_provider_gets_a_fair_chance(tmp: Path):
    """Запасной провайдер получает своё время, а не пять секунд на исходе дедлайна.

    Живой случай: основной шлюз молчал, агент писал «ни один бэкенд не ответил» — и не
    переключался на локальную модель, хотя она была настроена. Дедлайн общий на вызов:
    первый бэкенд съедал `request_timeout` целиком, второму доставалось `deadline - now`,
    то есть пять секунд. Медленная локальная модель не отвечает за пять секунд никогда,
    поэтому переключение существовало только на бумаге.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib, time as _t
    AG = importlib.import_module("agent_core")
    AG.DOWN.clear()
    # Недавний ответ №1 от прошлой проверки в этом же процессе отменяет карантин («отвечал
    # только что»): без сброса результат зависит от того, какие проверки шли раньше.
    AG.LAST_OK.clear()

    seen = []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return 200, {"slots_idle": 1}, "", 0.0
        seen.append((b["n"], round(timeout)))
        if b["n"] == 1:
            _t.sleep(0.05)
            return 0, {}, "TimeoutError: timed out", 0.05
        return 200, {"choices": [{"message": {"content": "ответ"}}],
                     "usage": {"completion_tokens": 10}}, "", 0.1

    cfg = {"backends": [{"n": 1, "url": "http://a", "key": "", "model": "fast", "models": {}},
                        {"n": 2, "url": "http://b", "key": "", "model": "slow", "models": {}}],
           "request_timeout": 100, "thinking": False}
    r = AG.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                     transport=transport, deadline=_t.time() + 0.2, sleep=lambda s: None)
    assert r["ok"] and r["backend"] == 2, f"на запасного не переключились: {r}"
    slow = [tm for n, tm in seen if n == 2]
    assert slow and slow[0] >= 30, \
        f"запасному дали {slow} с вместо честной доли таймаута — медленная модель не успеет"
    assert AG.DOWN.get(1, 0) > _t.time(), "упавший провайдер не отмечен: его спросят снова"

    # кнопка «Вернуться на основного» снимает отметку
    AG.RETRY_FLAG.parent.mkdir(parents=True, exist_ok=True)
    AG.RETRY_FLAG.write_text("", encoding="utf-8")
    assert AG.retry_primary_asked() and not AG.DOWN, "кнопка не вернула основного в строй"

    ui = panel_sources()
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert 'id="retryPrimary"' in ui and "/api/agent/retry-primary" in ui, \
        "кнопки «Вернуться на основного» нет в консоли"
    assert "/api/agent/retry-primary" in srv, "сервер не знает такого пути"


@test
def test_project_settings_page_draws_every_block(tmp: Path):
    """«Настройки проекта» дорисовываются до конца: MCP, конфиг, агент с ролями, виды.

    Живая жалоба: «модели и роли LLM и MCP выводились на странице настройки проекта сразу
    после разделения, потом пропали». Блок MCP обходил серверы как массив, а сервер отдаёт
    их объектом «имя → настройка», как в самом mcp.json. У объекта нет forEach: исключение
    обрывало отрисовку, и всё ниже — полный текст конфига, карточка агента с моделями по
    ролям, виды артефактов — молча не появлялось. С экрана это читалось как «так задумано».
    """
    import re as _re
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as ck
    ui = panel_sources()

    body = _js_function(ui, "async function renderProject(")
    blocks = ['t("mcp.title"', 'renderMcp("project")', 't("yaml.title")',
              'renderModelsCard(box, "project")', "renderKinds(box)", "drawSetupJump(box)"]
    for b in blocks:
        assert b in body, f"на странице настроек проекта нет блока {b}"
    where = [body.index(b) for b in blocks]
    assert where == sorted(where), "блоки страницы настроек проекта переставлены"
    mcp = _js_function(ui, "async function renderMcp(")
    assert "Object.keys(d.servers || {})" in mcp and "Object.keys(kit)" in mcp, \
        "серверы MCP обходятся не как объект — страница оборвётся на первом же проекте"
    # Тот же класс ошибки в любом месте панели: метод массива у заглушки-объекта. Законно,
    # когда скобку открыл Object.keys/values/entries, — тогда метод вызывается у массива.
    stub = []
    for m in _re.finditer(r"\|\|\s*\{\}\s*\)\s*\.(?:forEach|map|filter|some|every|reduce|find)\(", ui):
        depth, i = 0, m.start()
        while i > 0:
            i -= 1
            if ui[i] == ")":
                depth += 1
            elif ui[i] == "(":
                if not depth:
                    break
                depth -= 1
        if not _re.search(r"Object\.(?:keys|values|entries)\s*$", ui[max(0, i - 24):i]):
            stub.append(ui[max(0, i - 24):m.end()].strip())
    assert not stub, f"метод массива у объекта-заглушки оборвёт отрисовку: {stub}"

    # Модели проекта — ссылка на раздел «Модели»: настройка одна на кит (1.153.0).
    card = _js_function(ui, "async function renderModelsCard(")
    assert 'show("models")' in card, "карточка моделей не ведёт в раздел «Модели»"
    # падение отрисовки обязано выйти на экран, а не прятаться в консоли
    assert 'addEventListener("unhandledrejection"' in ui and 'addEventListener("error"' in ui, \
        "исключение на странице снова пройдёт молча"

    root = make_project(tmp)
    a = ck.agent_state(str(root))
    assert a.get("source") == "models" and "mcp" in a and "backends" in a, \
        f"агент проекта читает не настройку кита: {sorted(a)}"
    assert "own" not in a and "target" not in a, "у проекта снова свой слой настройки моделей"


@test
def test_health_lands_on_the_project_it_was_counted_for(tmp: Path):
    """Замечания проекта показываются под его именем, а не под именем выбранного позже.

    Живая жалоба: «кокпит пишет, что в проекте нет .env.aurora.local — куда он потерялся?
    Его потеря — проблема». Файл лежал на месте с июля; замечание принадлежало соседнему
    проекту-стенду, у которого файла действительно нет. Здоровье считается секундами,
    человек успевает сменить проект, и ответ, пришедший позже, ложился в текущий.
    """
    import re as _re
    sys.path.insert(0, str(KIT / "cockpit"))
    import aurora_cockpit as ck
    root = make_project(tmp)
    assert ck.health(str(root)).get("project") == str(root), \
        "ответ здоровья не называет свой проект — странице не по чему отличить чужой"

    ui = panel_sources()
    helper = _js_function(ui, "function takeHealth(")
    assert "S.project.path === p.path" in helper, "помощник не сверяет, выбран ли ещё проект"
    rest = ui.replace(helper, "")
    raw = _re.findall(r"S\.health\s*=\s*(?!null\b)[\w.]+", rest)
    assert not raw, f"здоровье присваивается мимо takeHealth — вернётся подпись чужим именем: {raw}"
    # Экран здоровья переехал в раздел-модуль: сторож тот же, зовётся иначе.
    assert ("S.health.project !== S.project.path" in ui
            or "h.project !== ctx.project.path" in ui), \
        "экран здоровья рисует ответ чужого проекта"


@test
def test_one_button_ends_with_what_is_left_to_the_human(tmp: Path):
    """Маршрут «Привести базу в порядок» делает всё автоматизируемое и называет остаток.

    Живая жалоба: «я сделал все прогоны, так какого хрена ошибки? Сделай одну кнопку, а
    потом покажи, что конкретно осталось мне». Раньше остаток приходилось вычитывать из
    трёх отчётов, а «ошибки линтера» звучали как поломка — хотя это работа, требующая
    суждения: документ это или знание, слить двойников или оставить.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    route = next((s for s in ck.scenarios() if s["id"] == "fix"), None)
    assert route, "маршрута «Починить базу» нет"
    cmds = [st.get("cmd") for st in route["steps"]]
    for need in ("kb:repair", "kb:dedupe", "kb:moc", "kb:index", "ops:todo"):
        assert need in cmds, f"в кнопке «Починить» нет шага {need}"
    assert cmds[-1] == "ops:todo", "маршрут не заканчивается списком того, что осталось"
    assert not any(st.get("manual") for st in route["steps"]), \
        "в кнопке «Починить» есть шаг-человек — значит она не одна кнопка"
    # «Починить» смотрит внутрь базы: в источники ходит «Обновить», и смешивать их значит
    # заставлять ждать синхронизации того, кто просто чинит ссылки
    assert not any((c or "").startswith("sync:") for c in cmds), \
        "«Починить» ходит в источники — это работа «Обновить»"
    upd = next(s for s in ck.scenarios() if s["id"] == "update")
    ucmds = [st.get("cmd") for st in upd["steps"]]
    assert "sync:confluence" in ucmds and "agent:build" in ucmds and "kb:trust" in ucmds, \
        "«Обновить» не берёт новое из источников и не доводит его до знания"

    lint_step = next(st for st in route["steps"] if st.get("cmd") == "kb:lint")
    assert "--residue" in (lint_step.get("flags") or []), \
        "«Починить базу» не запоминает остаток — кнопка «Починить» снова будет висеть вечно"

    root = make_project(tmp, git=True)
    card(root, "Concepts/Понятие.md", "См. [[Нет-такой-карточки]].", status="draft", type="concept")
    run("kb_lint.py", "--residue", cwd=root)
    out = run("aurora_todo.py", cwd=root).stdout
    assert "Принять" not in out and "Приёмка" not in out, \
        f"в остатке снова приёмка — принимать карточки человеку не нужно:\n{out}"
    assert "Ремонт не взял" in out and "Нет-такой-карточки" in out, \
        f"остаток починки не назван поимённо:\n{out}"
    # Наполнение базы решает движок (пользователь, 29.09.2026): несделанное ремонтом —
    # пробел движка, а не «ваше решение», и честный список так и говорит.
    assert "пробел движка, а не ваше решение" in out, out
    assert "не чинится кнопкой" not in out and "нужно ваше решение" not in out, \
        f"остаток снова объявлен решением человека:\n{out}"
    todo = (SCRIPTS / "aurora_todo.py").read_text(encoding="utf-8")
    human = todo.split("HUMAN = (")[1].split("\n)\n")[0]
    assert human.count("(\"") == 1 and "контрольные вопросы" in human, \
        "в «решает человек» снова то, что давно решают агенты и ремонт"


@test
def test_console_names_the_model_and_the_speed(tmp: Path):
    """В консоли видно, кто отвечает и с какой скоростью.

    Ночной прогон идёт часами и молча меняет исполнителя: первый бэкенд занят — работа
    уходит на второй, тот отвечает вдвое медленнее, и человек видит только «стало долго».
    Скорость берём из `usage` ответа сервера: не отдал — не показываем, выдумывать
    ток/с по длине текста значит рисовать правдоподобную неправду.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    step = {"backends": [(2, "qwen3.6-35b")], "tps": 41.7}
    line = R.where(step)
    assert "qwen3.6-35b" in line and "бэкенд №2" in line and "41.7 ток/с" in line, line

    assert "ток/с" not in R.where({"backends": [(1, "m")], "tps": 0}), \
        "скорость показана там, где сервер её не отдал"
    assert R.where({"backends": []}) == "", "пустой шаг рисует пустую скобку"
    assert "+1 на проверке" in R.where({"backends": [(1, "worker-m"), (3, "critic-m")],
                                        "tps": 0}), "участие критика не видно"

    core = (KIT / "scripts/agent_core.py").read_text(encoding="utf-8")
    assert '"tps"' in core and "completion_tokens" in core, \
        "скорость не считается по usage самого сервера"


@test
def test_night_run_waits_out_a_dropped_connection(tmp: Path):
    """Обрыв связи — повод подождать, а не бросить ночь.

    Живой случай: в 24-й партии отвалился VPN. Три источника упали по таймауту, остались
    в голове плана, каждая следующая партия бралась за них снова — и цикл остановился,
    оставив 667 источников неразобранными. Сетевой сбой лечится ожиданием, как докачка
    файла: связь вернулась — работа продолжается с того же места.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    net = {"steps": [{"status": "сбой", "note": "№3 gemma: TimeoutError: timed out; "
                                                "ни один бэкенд не ответил осмысленно"}]}
    assert R.looks_offline(net), "таймаут всех бэкендов не опознан как обрыв связи"

    content = {"steps": [{"status": "сбой", "note": "карточка не собрана: имя должно быть "
                                                    "уникальным"}]}
    assert not R.looks_offline(content), \
        "содержательный сбой принят за сетевой — прогон будет ждать связь, которая есть"
    assert not R.looks_offline({"steps": [{"status": "разобран", "note": "timed out"}]}), \
        "смотрим только на сбои: слово из успешного шага не должно включать ожидание"

    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    assert "waits < OFFLINE_TRIES" in src and "time.sleep(OFFLINE_WAIT)" in src, \
        "цикл не ждёт возвращения связи"
    assert "waits = 0" in src, "счётчик ожиданий не сбрасывается после удачной партии"
    assert "time.time() < deadline" in src, \
        "ожидание не ограничено окном прогона — кнопка на ночь будет ждать вечно"


@test
def test_distill_writes_a_thesis_and_keeps_the_source(tmp: Path):
    """Тезис пишется, дословный источник остаётся под ним, шапка не задета.

    Это третья за перестройку попытка собрать файл из разобранных частей — и первые две
    вклеивали поле в тело: `split_frontmatter` отдаёт шапку без «---», и всякая сборка
    «по длине» промахивается. Здесь проверяется результат целиком, а не намерение.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp)
    card = root / "AuroraKnowledgeDB/Concepts/Расчёт.md"
    card.write_text('---\ntitle: "Расчёт"\nkind: knowledge\nstatus: draft\n'
                    'type: concept\n---\n\nИсходный текст страницы.\nВторая строка.\n',
                    encoding="utf-8")

    def fake(cfg, role, messages, deadline=None, **kw):
        if role == "qa":
            return {"ok": True, "backend": 1, "model": "qa", "log": [],
                    "text": "ВЕРДИКТ: ЧИСТО"}
        return {"ok": True, "backend": 1, "model": "w", "log": [], "tps": 10,
                "text": "Расчёт — это способ получить сумму.\nВыполняется по расписанию."}

    step = R.distill_card({"request_timeout": 60}, str(card), call=fake)
    assert step["status"] == "переписана", step
    out = "---" + step["head"] + "\n---" + step["body"]
    head = out[3:out.find("\n---", 3)]
    assert "kind: knowledge" in head and "title:" in head, "шапка потеряна"
    assert "Расчёт — это способ" in out.split("\n---", 1)[1], "тезиса нет в теле"
    assert R.QUOTES in out and "Исходный текст страницы." in out.split(R.QUOTES)[1], \
        "дословный источник не сохранён под тезисом"
    assert "Расчёт — это способ" not in head, "тезис попал в шапку"
    assert out.count("---") == 2, f"разделители шапки задвоились:\n{out[:200]}"


@test
def test_card_kind_decides_who_may_rewrite_the_body(tmp: Path):
    """Тип карточки определяется правилом на каждом прогоне; слово человека — исправление.

    До 1.146.0 записанный `kind` считался «выбором человека», хотя писал его движок: тип
    расходился с правилом у 92 карточек PRJ-C. «Контракт» в имени страницы делал документом
    интеграционный маппинг, таблица шагов — словарём алгоритм: тезис им не писали никогда.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    K = importlib.import_module("kb_kind")

    doc = K.guess("AuroraKnowledgeDB/Concepts/Договор.md", {}, "текст",
                  ["Raw/contract/ГК-2026.md"])
    assert doc[0] == "document", f"нормативный текст — не документ: {doc}"
    gloss = K.guess("AuroraKnowledgeDB/Glossary/Накладная.md", {}, "определение",
                    ["Sources/Confluence/x.md"])
    assert gloss[0] == "dictionary", f"раздел Glossary — это именование: {gloss}"
    table = "| код | имя |\n|---|---|\n| 1 | а |\n| 2 | б |\n| 3 | в |\n"
    codes = K.guess("AuroraKnowledgeDB/Concepts/Коды.md", {}, table,
                    ["Sources/Confluence/x.md"])
    assert codes[0] == "dictionary", f"таблица значений — справочник: {codes}"
    know = K.guess("AuroraKnowledgeDB/Processes/Расчёт.md", {},
                   "Абзац.\nВторой.\nТретий.\nЧетвёртый.", ["Sources/Confluence/x.md"])
    assert know[0] == "knowledge", f"пересказ страницы — знание: {know}"

    # Накопленная карточка документом уже не является: текст нормативной бумаги ценен
    # дословно, а карточка, вобравшая ещё четыре артефакта, — это знание о сущности,
    # и переписывать его тезисом можно. Иначе `agent:distill` обошёл бы её стороной.
    grown = K.guess("AuroraKnowledgeDB/Concepts/Договор.md", {}, "текст",
                    ["Raw/contract/ГК-2026.md", "Sources/Confluence/x.md"])
    assert grown[0] != "document", \
        f"карточка из пяти источников осталась «документом» — тезис ей не напишут: {grown}"

    mapping = K.guess("AuroraKnowledgeDB/Systems/Маппинг.md", {}, "текст",
                      ["Sources/Confluence/Маппинг_контракта_ВАЛЮТА.md"])
    assert mapping[0] == "knowledge", f"интеграционный контракт стал документом: {mapping}"
    legal = K.guess("AuroraKnowledgeDB/Roles/Обязанности.md", {}, "текст",
                    ["Sources/Confluence/Государственный_контракт_№_5-6.md"])
    assert legal[0] == "document", f"госконтракт — нормативный текст: {legal}"
    steps = K.guess("AuroraKnowledgeDB/Processes/Расчёт-НДС.md", {}, table,
                    ["Sources/Confluence/x.md"])
    assert steps[0] == "knowledge", f"таблица шагов алгоритма стала справочником: {steps}"

    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB/Concepts"
    for name in ("Машинное", "Своё"):
        (kb / f"{name}.md").write_text(
            f'---\ntitle: "{name}"\nkind: document\nstatus: knowledge\n---\n\nдословный текст\n',
            encoding="utf-8")
    fix = root / "Raw/corrections"
    fix.mkdir(parents=True, exist_ok=True)
    (fix / "Своё-документ.md").write_text(
        '---\ntitle: "Своё — документ"\ncorrects: "[[Своё]]"\nkind: document\n---\n\n'
        "Это текст приказа, его не пересказывают.\n", encoding="utf-8")
    out = run("kb_kind.py", "--apply", cwd=root).stdout
    assert "по исправлению человека: 1" in out, out
    assert "kind: document" in (kb / "Своё.md").read_text(encoding="utf-8"), \
        "исправление человека не удержало тип"
    assert "kind: knowledge" in (kb / "Машинное.md").read_text(encoding="utf-8"), \
        "тип, записанный движком, не пересчитан по правилу"


@test
def test_writing_a_field_never_touches_the_body(tmp: Path):
    """Проставление поля в шапке не должно задевать тело карточки.

    `split_frontmatter` отдаёт шапку БЕЗ разделителей, а хвост — начиная с «\n---».
    Команда, которая режет хвост по длине шапки, промахивается на три символа и вклеивает
    поле в первую строку тела. На живом проекте это испортило 2033 карточки за прогон —
    спас только git.
    """
    root = make_project(tmp, git=True)
    body = "первая строка тела\nвторая строка\n\n## Раздел\n\nтекст\n"
    (root / "AuroraKnowledgeDB/Concepts/Карточка.md").write_text(
        '---\ntitle: "Карточка"\nstatus: draft\ntype: concept\n'
        'source: "Sources/Confluence/x.md"\n---\n\n' + body, encoding="utf-8")
    run("kb_kind.py", "--apply", cwd=root)
    got = (root / "AuroraKnowledgeDB/Concepts/Карточка.md").read_text(encoding="utf-8")
    assert got.startswith("---\n") and "kind: knowledge" in got
    assert got.endswith(body), f"тело изменилось при записи поля:\n{got[-160:]}"
    assert got.count("---") == 2, "разделители шапки задвоились"


@test
def test_acceptance_machinery_is_gone(tmp: Path):
    """Приёмки больше нет: ни команды, ни вкладки, ни очереди.

    Процедура, потерявшая смысл, опаснее отсутствующей: человек тратит на неё время и
    считает, что делает работу. Доверие теперь вычисляется, и присваивать его некому.
    """
    assert not (KIT / "scripts/kb_verify.py").exists(), "скрипт приёмки на месте"
    cmds = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "kb:verify" not in cmds and "kb:queue" not in cmds, "команды приёмки в реестре"
    ui = panel_sources()
    assert "renderReview" not in ui and 'id="view-review"' not in ui, "вкладка приёмки в панели"
    srv = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "/api/review" not in srv, "сервер всё ещё отдаёт очередь приёмки"
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    assert "kb:verify" not in scen, "маршрут зовёт снесённую команду"
    assert "kb:trust" in scen, "пересчёт доверия не встал на её место"


@test
def test_trace_table_proves_every_link(tmp: Path):
    """Связь артефакта с задачей доказывается, а не утверждается.

    Номер сравнивается по границе токена: `10.3.1` и `10.3.11` — разные истории, и
    склеить их значит выдать чужое доверие. Косвенная связь идёт не дальше двух переходов:
    через три в большой базе связано всё со всем, и класс перестаёт что-либо значить.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    T = importlib.import_module("kb_trace_table")

    root = make_project(tmp)
    jira, conf = root / "Sources/JIRA", root / "Sources/Confluence"
    jira.mkdir(parents=True, exist_ok=True); conf.mkdir(parents=True, exist_ok=True)
    (jira / "PRJ-1.md").write_text(
        '---\nkey: "PRJ-1"\ntitle: "US-10.3.1. Оплата"\nstatus: "Закрыто"\n---\n',
        encoding="utf-8")
    (jira / "PRJ-2.md").write_text(
        '---\nkey: "PRJ-2"\ntitle: "US-10.3.11. Возврат"\nstatus: "Закрыто"\n---\n',
        encoding="utf-8")
    (conf / "AC-10.3.1-Оплата.md").write_text(
        '---\ntitle: "AC-10.3.1. Критерии оплаты"\n---\n\nсм. Алгоритм-оплаты\n',
        encoding="utf-8")
    (conf / "Алгоритм-оплаты.md").write_text(
        '---\ntitle: "Алгоритм оплаты"\n---\n\nсм. Справочник-кодов\n', encoding="utf-8")
    (conf / "Справочник-кодов.md").write_text(
        '---\ntitle: "Справочник кодов"\n---\n\nтаблица\n', encoding="utf-8")
    (conf / "Чужая-страница.md").write_text(
        '---\ntitle: "Про погоду"\n---\n\nничего общего\n', encoding="utf-8")

    t = T.build(str(root))
    direct = {os.path.basename(k): [r["key"] for r in v] for k, v in t["direct"].items()}
    assert direct.get("AC-10.3.1-Оплата.md") == ["PRJ-1"], \
        f"номер сопоставлен неверно (10.3.11 — другая история): {direct}"
    ind = {os.path.basename(k): v for k, v in t["indirect"].items()}
    assert "Алгоритм-оплаты.md" in ind, "трассировка на один переход не найдена"
    assert "Чужая-страница.md" not in ind and "Чужая-страница.md" not in direct, \
        "страница без связей получила связь"
    why = t["direct"]["Sources/Confluence/AC-10.3.1-Оплата.md"][0]["why"]
    assert "10.3.1" in why, f"связь не объяснена словами: {why}"
    assert all(r["depth"] <= 2 for rows in t["indirect"].values() for r in rows), \
        "трассировка ушла глубже двух переходов"


@test
def test_trust_is_computed_from_task_status(tmp: Path):
    """Класс доверия считается по статусам задач, а не назначается человеком.

    Одна задача-черновик перевешивает десять готовых: содержание ещё поменяется. Нет
    связей вовсе — не знание, а `draft`: подтвердить нечем, и молчаливо записать такую
    карточку в знание значит обесценить класс ровно там, где он нужен.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    U = importlib.import_module("kb_trust")

    table = {"direct": {"Sources/Confluence/A.md": [{"key": "PRJ-1", "why": "номер"}],
                        "Sources/Confluence/B.md": [{"key": "PRJ-1", "why": "номер"},
                                                    {"key": "PRJ-9", "why": "ключ"}]},
             "indirect": {"Sources/Confluence/C.md": [{"key": "PRJ-1", "trail": ["C", "A"],
                                                       "depth": 1}]}}
    st = {"PRJ-1": "Закрыто", "PRJ-9": "Бэклог"}
    trust, draft = {"закрыто"}, {"бэклог"}
    cls, why = U.source_class("Sources/Confluence/A.md", table, st, trust, draft)
    assert cls == "trusted" and "PRJ-1" in why, (cls, why)
    cls, why = U.source_class("Sources/Confluence/B.md", table, st, trust, draft)
    assert cls == "draft" and "PRJ-9" in why, f"черновая задача не перевесила: {cls} · {why}"
    assert U.source_class("Sources/Confluence/C.md", table, st, trust, draft)[0] == "trusted"
    assert U.source_class("Raw/contract/ГК.md", table, st, trust, draft)[0] == "raw"
    assert U.source_class("Sources/Confluence/Z.md", table, st, trust, draft)[0] == "unknown"
    assert U.wanted_status("unknown") == "draft", "недоказанное доверие стало знанием"
    assert U.wanted_status("raw") == "knowledge" and U.wanted_status("trusted") == "knowledge"

    # понижение не стирает знание, а записывает причину в подвал
    got = U.note_downgrade("тело карточки\n", "knowledge", "draft", "задача вернулась")
    assert "тело карточки" in got and "класс изменён" in got and U.FOOTER in got


@test
def test_trust_leaves_placeholders_alone(tmp: Path):
    """Заготовке класс доверия не положен, и её статус `kb:trust` не переписывает.

    Заготовка шла общим путём: источников нет — «unknown» — `draft`. Статус `placeholder`
    переписывался на `draft` каждым прогоном, пустышка держалась на одном теге, а его снимал
    ремонт. На живом проекте так 571 заготовка из 632 числилась черновиком и уходила в поиск
    и в долю доверия как знание, которого в ней нет.
    """
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text((cfg.read_text(encoding="utf-8") if cfg.exists() else "")
                   + "\ntrust_statuses: [Закрыто]\n", encoding="utf-8")
    trace = root / "AuroraKnowledgeDB/meta/trace"
    trace.mkdir(parents=True, exist_ok=True)
    (trace / "trace.json").write_text('{"direct": {}, "indirect": {}}', encoding="utf-8")
    card(root, "Concepts/Пустышка.md", status="placeholder", tags="[заготовка]",
         body="_Заготовка: ссылка на это понятие уже есть, знания пока нет._")
    card(root, "Concepts/Знание.md", status="draft", kind="knowledge",
         sources='\n  - "Raw/contract/ГК.md"', body="Знание — положение договора.")

    run("kb_trust.py", "--apply", cwd=root)
    stub = (root / "AuroraKnowledgeDB/Concepts/Пустышка.md").read_text(encoding="utf-8")
    real = (root / "AuroraKnowledgeDB/Concepts/Знание.md").read_text(encoding="utf-8")
    assert "status: placeholder" in stub, f"kb:trust переписал статус заготовки:\n{stub[:300]}"
    assert "trust:" not in stub, "заготовке назначен класс доверия — доверять в ней нечему"
    assert "status: knowledge" in real, \
        "знание из Raw/ перестало доверяться — правка задела соседей"


@test
def test_filling_a_placeholder_by_extraction_clears_both_marks(tmp: Path):
    """Вынос, наполнивший пустышку, снимает оба признака и приносит источник донора.

    При наполнении ставился `status: draft`, а тег `заготовка` оставался — и `is_placeholder`
    по тегу продолжал считать карточку пустышкой: вне поиска и вне доли доверия, хотя
    определение в ней уже лежит. На живом проекте так стояли 8 карточек терминов.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")
    AC = importlib.import_module("aurora_common")

    root = make_project(tmp)
    card(root, "Concepts/ТК.md", status="placeholder", tags="[заготовка]", kind="knowledge",
         body="_Заготовка: ссылка на это понятие уже есть, знания пока нет._")
    path = R.place_definition(str(root), "ТК", "ТК — zip-архив с вложенными документами.",
                              "Модель-контейнера-документов",
                              donor_sources=["Sources/Confluence/Контейнер.md"])
    text = open(path, encoding="utf-8").read()
    assert not AC.is_placeholder(AC.frontmatter(text), text), \
        f"наполненная выносом карточка осталась пустышкой:\n{text[:300]}"
    assert card_srcs(text) == ["Sources/Confluence/Контейнер.md"], \
        f"источник донора не лёг в наполненную карточку: {card_srcs(text)}"
    assert "zip-архив с вложенными документами" in text, "определение не перенесено"



@test
def test_repair_drops_dead_backlinks_from_stubs(tmp: Path):
    """Мёртвое «Упоминается в» у заготовки чинится командой, а не человеком.

    Заготовка родилась из ссылки; карточку-источник переименовали — и справка о
    происхождении стала битой ссылкой. Знания в ней нет, но приёмка вставала намертво:
    правило «битые ссылки решает человек» держало такую заготовку непроверяемой вечно.
    """
    root = make_project(tmp)
    card(root, "Concepts/Живая.md", "текст со ссылкой [[Заготовка]]",
         status="imported", type="concept")
    (root / "AuroraKnowledgeDB/Glossary/Заготовка.md").write_text(
        '---\ntitle: "Заготовка"\nstatus: draft\ntype: glossary\n'
        "tags: [заготовка]\n---\n\n# Заготовка\n\n## Упоминается в\n\n"
        "- [[Живая]]\n- [[Уехавшая]]\n- [[Тоже-уехавшая]]\n", encoding="utf-8")

    run("kb_fix.py", "--links", "--apply", "--allow-dirty", cwd=root)
    got = (root / "AuroraKnowledgeDB/Glossary/Заготовка.md").read_text(encoding="utf-8")
    assert "[[Живая]]" in got, "живое упоминание убрано вместе с мёртвыми"
    assert "Уехавшая" not in got and "Тоже-уехавшая" not in got, \
        f"мёртвые упоминания остались — убрана только первая строка:\n{got}"


@test
def test_doctor_says_a_project_without_git_has_no_undo(tmp: Path):
    """Проект без git — проект без отката, и агент в нём писать откажется.

    Живой случай: doctor говорил «OK: config and onboarding look ready», а `agent:build`
    возвращал 1 за ноль секунд — чекпойнт делать не во что. Понять по журналу, почему база
    не растёт, было нельзя.
    """
    root = make_project(tmp)                       # без git: make_project(git=True) его заводит
    cp = run("aurora_doctor.py", cwd=root)
    assert "не под git" in cp.stdout, \
        f"doctor молчит про отсутствие git — а без него агент не работает:\n{cp.stdout}"
    assert "git init" in cp.stdout, "названа беда, но не названо лечение"
    assert cp.returncode != 0, "проект без отката не может считаться готовым"

    nest = tmp / "with-git"; nest.mkdir()
    ok = make_project(nest, git=True)
    assert "не под git" not in run("aurora_doctor.py", cwd=ok).stdout, \
        "ложная тревога на проекте под git"


@test
def test_installer_marks_its_own_index_stubs(tmp: Path):
    """Заготовки оглавлений пишет установщик — и помечает их как свои.

    Без пометки `kb:index` считал их текстом человека и не трогал никогда: проект начинал
    жизнь с одиннадцати «рукотворных» оглавлений, которых никто не писал, и с вечной
    находкой, лечившейся только `--force`.
    """
    src = (KIT / "scripts/install_aurora.py").read_text(encoding="utf-8")
    i = src.index("_index.md")
    assert "generated: kb_index.py" in src[i - 400:i + 400], \
        "установщик пишет заготовку оглавления без пометки генерации"

    # и старые заготовки движок узнаёт по своей же строке — без --force
    root = make_project(tmp)
    card(root, "Concepts/Понятие.md", "тело", status="imported", type="concept")
    (root / "AuroraKnowledgeDB/Concepts/_index.md").write_text(
        "# Concepts\n\nИндекс раздела. Карточек: 0 (на 2026-01-01).\n", encoding="utf-8")
    cp = run("kb_index.py", "--apply", cwd=root, expect_rc=0)
    assert "Приняты под генерацию" in cp.stdout, \
        f"заготовку установщика движок не узнал в лицо:\n{cp.stdout}"
    assert "[[Понятие]]" in (root / "AuroraKnowledgeDB/Concepts/_index.md").read_text(
        encoding="utf-8"), "оглавление не пересобрано"


@test
def test_buttons_stay_on_the_right_when_the_row_wraps(tmp: Path):
    """Кнопки держатся справа и после переноса строки.

    `.row` переносится, а `.spacer{flex:1}` живёт на первой строке: стоит имени команды
    стать длинным или окну узким — кнопки уезжают вниз и прижимаются влево. Правая группа
    с `margin-left:auto` держится справа на любой строке.
    """
    ui = panel_sources()
    assert "flex-wrap:wrap" in ui, "разметка изменилась: перенос строк больше не включён"
    assert "margin-left:auto" in ui and ".row-right{" in ui, \
        "нет правой группы — при переносе кнопки окажутся слева внизу"
    for anchor in ('id="clearConsole"', 'id="refreshHealth"', 'id="refreshOverview"'):
        i = ui.index(anchor)
        assert "row-right" in ui[max(0, i - 400):i], \
            f"кнопка {anchor} не в правой группе"
    assert "#consoleCmd{overflow:hidden" in ui, \
        "длинная команда снова может ломать всю строку"


@test
def test_the_all_in_one_route_looks_different_from_the_dangerous_one(tmp: Path):
    """Маршрут «всё сразу» зовёт нажать, «пересобрать с нуля» — подумать.

    Оба выглядели одинаково: карточка и синяя кнопка «Пройти маршрут». Один включает в
    себя остальные, второй сносит содержимое базы вместе с принятым доверием — и цена
    ошибки у них разная на порядок.
    """
    ui = panel_sources()
    assert '.card.route-all{' in ui and '.card.route-danger{' in ui, \
        "у маршрутов нет разного оформления"
    assert 'sc.id === "all" ? "route-all"' in ui and '"rebuild" ? "route-danger"' in ui, \
        "классы не привязаны к конкретным маршрутам"
    # Подписи уехали в каталог: список маршрутов рисует раздел-модуль, строки общие.
    routes_ru = json.loads((KIT / "cockpit/i18n/ru.json").read_text(encoding="utf-8"))
    assert "★" in routes_ru.get("routes.all_in_one", "") \
       and "⚠" in routes_ru.get("routes.danger", ""), \
        "нет подписи, объясняющей, чем эти маршруты отличаются"
    assert 'kind === "route-danger" ? "danger" : "primary"' in ui, \
        "у опасного маршрута кнопка того же цвета, что у обычного"


@test
def test_hidden_really_hides_in_the_panel(tmp: Path):
    """Атрибут hidden обязан скрывать, даже когда у элемента задан свой display.

    Живой случай: «Скрыть раздел» в «Разработке» ставила `hidden` на кнопку навигации и
    уводила на «О проекте», а сама кнопка оставалась на экране. Правило `[hidden]` живёт
    в UA-стилях, и любой авторский `display` его перебивает — `nav button{display:flex}`
    как раз такой. Панель прячет так семь элементов, поэтому проверяем правило, а не один
    экран.
    """
    ui = panel_sources()
    assert re.search(r"\[hidden\]\{display:none ?!important\}", ui), \
        "нет правила [hidden]{display:none !important} — сокрытие держится на UA-стилях"
    # у элементов, которые панель прячет, свой display есть — значит правило не «на всякий»
    assert "nav button{display:flex" in ui, \
        "разметка навигации изменилась: проверьте, что правило [hidden] ещё нужно"
    assert ui.index("[hidden]{display:none") > ui.index("<style"), \
        "правило вне таблицы стилей"


@test
def test_registry_cache_belongs_to_the_engine_that_wrote_it(tmp: Path):
    """Кэш реестра, написанный другим кодом панели, не должен приниматься за свой.

    Живой случай: панель работала старым процессом (кит обновили, панель не
    перезапускали). Ключ кэша складывался из версии кита, времени правки `commands.txt`
    и признака «поднято из исходников» — всё это у старого процесса совпадало с новым.
    Старый писал в файл кита свои 62 команды, новый читал их как свои и терял шесть
    команд `dev:` — до следующей смены версии, то есть навсегда.

    Лечится меткой самого движка панели: время правки файла, снятое НА ИМПОРТЕ. Кит
    обновили после старта — метка у работающего процесса осталась вчерашней, и его кэш
    новый код не примет.
    """
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert re.search(r"^ENGINE = os\.path\.getmtime", src, re.M), \
        "нет метки движка: кэш реестра снова общий для разных версий панели"
    key = re.search(r"    key = \((.+?)\)\n", src, re.S)
    assert key and "engine=" in key.group(1), \
        "метка движка не вошла в ключ кэша — старый процесс снова отравит реестр"
    # метку снимаем на импорте, а не при сборке ключа: иначе она равна текущему mtime
    # и у старого процесса совпадёт с новым — ровно то, от чего защищаемся
    assert "ENGINE = os.path.getmtime" in src.split("def registry")[0], \
        "метка движка считается внутри registry() — она обязана быть снимком на старте"


@test
def test_qa_corpus_does_not_describe_a_removed_engine(tmp: Path):
    """Кейсы и сценарии не должны звать снятые команды и снятые статусы.

    Скиллы отстали от движка на пятнадцать команд, и по коду это было не видно. С QA то
    же самое, только хуже: по кейсу человек **проверяет** движок, и кейс, требующий
    `kb:verify --source-older-than 6`, проваливается не потому что движок плох, а потому
    что такой команды нет. Прогон учит не верить прогону.

    Выведенные из оборота кейсы (`status: deprecated`) — исключение: они историческая
    запись о том, как было, и переписывать их нельзя.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import kit_commands as K

    known = {r["cmd"] for r in K.read_registry()}
    gone_status = ("status: verified", "status: imported", "status: in-review")
    bad = []
    # только кейсы и сценарии: журналы прогонов и находки — запись о том, как было, и
    # переписывать историю нельзя, иначе прогон полугодовой давности начнёт врать
    files = list((KIT / "Development/QA/cases").glob("*.md")) \
        + list((KIT / "Development/QA/scenarios").glob("*.md"))
    for f in sorted(files):
        text = f.read_text(encoding="utf-8")
        if re.search(r"^status: deprecated", text, re.M):
            continue          # выведен из оборота: это запись о прошлом
        # Цифра в имени команды — не выдумка: `kit:i18n`. Регулярка без неё обрывала имя
        # на `kit:i` и объявляла снятой команду, которая есть. Проверка, которая врёт
        # про несуществующую поломку, обесценивает и настоящие свои находки.
        for cmd in re.findall(r"`((?:kb|ctx|make|ship|ops|sync|kit|agent|dev):[a-z0-9-]+)", text):
            if cmd not in known:
                bad.append(f"{f.name}: зовёт снятую команду {cmd}")
        # с начала строки: так пишут ожидаемый frontmatter. Внутри фразы упоминание
        # снятого статуса законно — им объясняют, почему его больше нет.
        for s in gone_status:
            if re.search(rf"^\s*{re.escape(s)}", text, re.M):
                bad.append(f"{f.name}: ждёт снятый статус «{s}»")
    assert not bad, "QA описывает движок, которого нет:\n  " + "\n  ".join(bad[:12])

    # Кейс вне сценария не гоняется никогда: он выглядит покрытием, но им не является.
    # И наоборот: сценарий не должен вести на выведенный из оборота кейс.
    live, retired, covered = set(), set(), set()
    for f in (KIT / "Development/QA/cases").glob("*.md"):
        head = f.read_text(encoding="utf-8")[:600]
        cid = (re.search(r"^id:\s*(\S+)", head, re.M) or [None, ""])[1]
        (retired if re.search(r"^status: deprecated", head, re.M) else live).add(cid)
    for f in (KIT / "Development/QA/scenarios").glob("*.md"):
        m = re.search(r"^covers:\s*\[(.+)\]", f.read_text(encoding="utf-8"), re.M)
        if m:
            covered |= {x.strip() for x in m.group(1).split(",")}
    orphan = sorted(c for c in live - covered if c)
    assert not orphan, ("кейсы не входят ни в один сценарий — гоняться они не будут: "
                        + ", ".join(orphan))
    dead = sorted(covered & retired)
    assert not dead, "сценарий ведёт на выведенный из оборота кейс: " + ", ".join(dead)


@test
def test_skills_describe_the_engine_as_it_is_now(tmp: Path):
    """Скилл — это то, что читает модель вместо кода. Отстанет он — отстанет и работа.

    Ревизия показала разрыв в полтора десятка команд: весь неймспейс `agent:` (пять
    команд, включая ту, которой собирают базу) в скилле не упоминался вовсе, а
    жизненный цикл был описан снятой шкалой `imported → in-review → verified` с приёмкой
    человеком. Модель, читающая такой скилл, будет звать несуществующие команды и ставить
    статусы, которых больше нет.

    Проверяем механически: каждая команда реестра названа в своём скилле, и снятая
    концепция не всплывает как действующая.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import kit_commands as K

    vault = (KIT / "skills/aurora-vault/SKILL.md").read_text(encoding="utf-8")
    dev = (KIT / "skills/aurora-dev/SKILL.md").read_text(encoding="utf-8")
    missing = [r["cmd"] for r in K.read_registry()
               if r["cmd"] not in (dev if r["ns"] == "dev" else vault)]
    assert not missing, f"команды есть в движке, но не в скиллах: {missing}"

    # снятые команды не должны предлагаться как рабочие
    for gone in ("kb:verify", "kb:queue"):
        assert gone not in vault, f"скилл зовёт снятую команду {gone}"

    # снятая шкала статусов: в скиллах она может быть только как «читаем легаси»
    everywhere = "\n".join(f.read_text(encoding="utf-8")
                           for f in (KIT / "skills").rglob("*.md"))
    for line in everywhere.splitlines():
        if "status: verified" in line or "status: in-review" in line:
            raise AssertionError(f"скилл предписывает снятый статус: {line.strip()[:90]}")

    # и наоборот: действующая модель знания должна быть названа
    assert "docs/knowledge-rules.md" in vault, \
        "скилл не отсылает к правилам базы — модель будет решать про статусы сама"
    assert "Момус" in vault, "Момус не описан: проверка ответов выглядит необязательной"
    assert "AURORA_AGENT_PARALLEL" in vault or "одновременно" in vault, \
        "скилл не знает про параллельный разбор"

    # Скилл читает чужой харнесс — Claude Code и любой другой ассистент. Проверка имён
    # команд ловит снятое, но не ловит расхождение по существу: команда на месте, а
    # правило, без которого её выводом нельзя пользоваться, названо только в панели.
    # Ревизия нашла четыре таких: граница чистовика, признак «без технологий», дельта
    # требования и сторож исходящих запросов.
    from aurora_common import MADE_MARK
    assert MADE_MARK in vault, \
        "маркер границы чистовика не назван в скилле дословно: чужой ассистент напишет " \
        "допущения в тело, и они уедут заказчику"
    assert "tech_agnostic" in vault or "без технологий" in vault, \
        "правило «без технологий» есть в движке, но не в скилле"
    assert "--changed" in vault and "--migration" in vault, \
        "замена требования требует дельты, а скилл про это молчит"
    assert "outbound" in vault, "сторож исходящих запросов не описан"

    # Кухня разработки переехала из `.gitignore` в ветку `development` (1.96.2).
    # Скилл, утверждающий старое, оставит правки QA без истории и без копии.
    assert "`.gitignore`: наружу" not in dev, \
        "скилл всё ещё считает Development/ игнорируемой папкой, а не веткой"
    assert "kitchen" in dev, "скилл не говорит, куда пушить кухню"


@test
def test_foreign_harness_gets_rules_not_only_paths(tmp: Path):
    """`artifact_spec` — единственное, чем чужой ассистент узнаёт, как делать документ.

    Через MCP его зовут Claude Code и любой другой харнесс, и всё, чего в ответе нет, для
    него не существует. Ревизия нашла: инструмент отдавал шаблон и папку, а признак «без
    технологий» и границу чистовика — нет. Значит чужой ассистент пишет критерии с именами
    СУБД (правило жило только в панели) и кладёт «Допущения» в тело документа — откуда они
    уезжают заказчику как часть спецификации.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    from aurora_common import MADE_MARK

    root = tmp / "proj"
    (root / "Templates").mkdir(parents=True)
    (root / "Artifacts" / "ac").mkdir(parents=True)
    (root / "Artifacts" / "opz").mkdir(parents=True)
    (root / "Templates" / "AC.md").write_text("шаблон", encoding="utf-8")
    (root / "Templates" / "OPZ.md").write_text("шаблон", encoding="utf-8")
    (root / "aurora.config.yaml").write_text(
        "artifacts:\n"
        "  ac:\n    title: \"Критерии приёмки\"\n    template: Templates/AC.md\n"
        "    out: Artifacts/ac\n    tech_agnostic: true\n"
        "  opz:\n    title: \"ОПЗ\"\n    template: Templates/OPZ.md\n"
        "    out: Artifacts/opz\n", encoding="utf-8")

    def spec(*args):
        r = subprocess.run([sys.executable, str(KIT / "scripts" / "make_kinds.py"), *args],
                           cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return r.stdout

    ac, opz, all_kinds = spec("--kind", "ac"), spec("--kind", "opz"), spec()

    assert "Templates/AC.md" in ac and "Artifacts/ac" in ac, "пути пропали"
    assert "без технологий" in ac.lower(), \
        "признак «без технологий» не доехал до ассистента — он назовёт стек в критериях"
    assert "без технологий" not in opz.lower(), \
        "правило навязано ОПЗ: там стек — предмет документа, и критик будет ругаться зря"
    assert "ac" in all_kinds and "без технологий" in all_kinds.lower(), \
        "в общем реестре не видно, у каких видов правило включено"

    for name, out in (("вид", ac), ("вид", opz)):
        assert MADE_MARK in out, f"граница чистовика не названа ({name})"
        marker = [ln for ln in out.splitlines() if MADE_MARK in ln]
        assert marker == [MADE_MARK], \
            f"маркер показан с отступом или в строке с текстом: {marker!r} — скопировав " \
            "его так, ассистент получит границу, по которой публикация ничего не отрежет"

    # Находка в конфиге не должна прятать правила: один незаполненный шаблон у одного
    # вида оставлял чужой харнесс без правил по всем остальным.
    (root / "Templates" / "OPZ.md").unlink()
    broken = spec()
    assert "без технологий" in broken.lower(), \
        "ошибка в одном виде скрыла правила остальных"


@test
def test_oversized_request_does_not_kill_the_provider(tmp: Path):
    """Запрос длиннее окна модели — не повод считать провайдера мёртвым.

    Окно контекста узнать из API нельзя, а порезано оно у каждого шлюза по-своему: одна
    и та же модель держит 252 000 у одного и 196 608 у другого. Движок про это не знал
    вовсе, и большая карточка запускала цепную реакцию: шлюз отвечает 400, движок метит
    провайдера мёртвым на 15 минут и идёт к следующему с ТЕМ ЖЕ запросом — и так по всему
    кольцу. Одна карточка гасила всех провайдеров, а в журнале это выглядело как «никто
    не отвечает».

    Теперь окно объявляется человеком, заведомо большой запрос уходит следующей модели —
    у которой окно может быть шире, — а 400 по длине не ставит метку «мёртв».
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A

    cfg = A.parse_config({
        "AURORA_AGENT_BACKEND_1_URL": "http://a/v1", "AURORA_AGENT_BACKEND_1_MODEL": "узкая",
        "AURORA_AGENT_BACKEND_1_CONTEXT": "8000",
        "AURORA_AGENT_BACKEND_2_URL": "http://b/v1", "AURORA_AGENT_BACKEND_2_MODEL": "широкая",
        "AURORA_AGENT_BACKEND_2_CONTEXT": "200000"})
    assert [b["context"] for b in cfg["backends"]] == [8000, 200000], \
        "окно контекста не читается из настроек"

    big = [{"role": "user", "content": "я" * 60000}]
    ok1, why = A.fits(cfg["backends"][0], big, None)
    assert not ok1 and "не отправляю" in why, "заведомо большой запрос всё равно уходит"
    ok2, _ = A.fits(cfg["backends"][1], big, None)
    assert ok2, "модель с широким окном пропущена — кольцо потеряло смысл"

    # окно не объявлено — не мешаем работать: движок не выдумывает чужие ограничения
    free = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a/v1",
                           "AURORA_AGENT_BACKEND_1_MODEL": "неизвестная"})
    assert A.fits(free["backends"][0], big, None)[0], \
        "без объявленного окна движок сам себе придумал предел"

    assert A.looks_like_overflow("This model's maximum context length is 8192 tokens", None)
    assert not A.looks_like_overflow("unknown field chat_template_kwargs", None), \
        "обычная 400 принята за переполнение — повтор без chat_template_kwargs пропадёт"
    src = (KIT / "scripts/agent_core.py").read_text(encoding="utf-8")
    place = src[src.index("if looks_like_overflow(err, body):"):][:400]
    assert "DOWN[" not in place, "переполнение по-прежнему метит провайдера мёртвым"


@test
def test_planner_gives_structure_to_a_shapeless_source(tmp: Path):
    """Источник без заголовков разбирает планировщик, а не человек.

    Разметки нет — резать не по чему, и такой источник уходил человеку целиком: «структуры
    нет, карточку писать чтением». Это работа, которую машина умеет: границы тем видны по
    описи абзацев, а переносит текст движок дословно, как и в обычном разборе.

    Так разбираются расшифровки встреч, выгрузки и сканы — всё, у чего автор не расставил
    заголовков. Знания в источнике нет вовсе — планировщик возвращает пустой список, и
    поведение остаётся прежним.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R

    root = make_project(tmp)
    (root / "Raw/project").mkdir(parents=True, exist_ok=True)
    # абзацы длинные не для красоты: на коротких опись выходит длиннее текста, и
    # проверить, что планировщику ушла именно она, невозможно
    text = "\n\n".join(f"Абзац {i}. Правило номер {i} и условия его применения. " * 8
                        for i in range(1, 13))
    (root / "Raw/project/Расшифровка.md").write_text(text, encoding="utf-8")

    seen = {}

    def fake(cfg, role, messages, **kw):
        seen[role] = stub_messages(messages, kw)[0]["content"]
        if role == "planner":
            rows = {"parts": [{"title": "Правила один-четыре", "from": 1, "to": 4},
                              {"title": "Правила пять-восемь", "from": 5, "to": 8}]}
            return {"ok": True, "text": json.dumps(rows, ensure_ascii=False),
                    "backend": 1, "model": "p", "tps": 9, "log": []}
        return {"ok": True, "text": json.dumps({"keep": "знание есть"}),
                "backend": 1, "model": "m", "tps": 9, "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    step = {"card": "", "status": "", "note": "", "backends": []}
    out = R.judge_empty(cfg, str(root), "Raw/project/Расшифровка.md", step, True, False,
                        fake, 0)

    assert out["status"] == "разобран по абзацам", \
        f"источник без заголовков снова ушёл человеку: {out}"
    made = sorted((root / "AuroraKnowledgeDB/Concepts").glob("Правила-*.md"))
    assert len(made) == 2, f"карточек должно быть две, а не {len(made)}"
    body = made[0].read_text(encoding="utf-8")
    assert body.count("Абзац 1. Правило номер 1") == 8, "текст не перенесён дословно"
    assert "Абзац 5." not in body, "границы не соблюдены — куски перемешались"
    assert "status: draft" in body, "карточка родилась с присвоенным доверием"
    assert "Raw/project/Расшифровка.md" in card_srcs(body), "потерян провенанс"
    # Намерение проверки — «планировщику ушла ОПИСЬ, а не текст», и мерить его длиной
    # промпта нельзя: правила в шаблоне растут, и порог начинает ловить не то. Опись
    # показывает первые слова абзаца, поэтому целого абзаца в промпте быть не должно.
    whole = f"Абзац 3. Правило номер 3 и условия его применения. " * 8
    assert whole.strip() not in seen["planner"], "планировщику отдали текст вместо описи"
    assert len(seen["planner"]) < len(text), \
        f"промпт длиннее самого источника: {len(seen['planner'])} против {len(text)}"

    # знания нет — поведение прежнее, выдумывать карточки не начинаем
    def empty_planner(cfg, role, messages, **kw):
        if role == "planner":
            return {"ok": True, "text": '{"parts": []}', "backend": 1, "model": "p",
                    "tps": 9, "log": []}
        return {"ok": True, "text": json.dumps({"keep": "знание есть"}),
                "backend": 1, "model": "m", "tps": 9, "log": []}

    step2 = {"card": "", "status": "", "note": "", "backends": []}
    out2 = R.judge_empty(cfg, str(root), "Raw/project/Расшифровка.md", step2, False, False,
                         empty_planner, 0)
    assert out2["status"] == "без секций — человеку", \
        "планировщик не нашёл границ, а движок всё равно что-то собрал"


@test
def test_planner_cuts_what_will_not_fit(tmp: Path):
    """Словарь длиннее окна режет планировщик, а текст переносит движок дословно.

    Тело словаря и документа модель не переписывает никогда — в этом смысл самих типов.
    Но словарь на сорок тысяч знаков не работает ни как словарь, ни как карточка: его не
    найти выборкой и не подать в контекст, а `agent:distill` его вообще не видел — тип
    не тот. Такому нужна не переработка, а границы.

    Планировщик получает **опись абзацев** (номер, размер, первые слова), а не текст: так
    границы выбираются даже для тела, которое в окно не влезает — тем же приёмом, которым
    `agent:build` разбирает источники по секциям. Режет движок.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R

    kb = tmp / "AuroraKnowledgeDB/Reference"
    kb.mkdir(parents=True)
    para = lambda i: f"Термин {i}. Определение термина {i} и правила применения. " * 6
    body = "\n\n".join(para(i) for i in range(1, 31))
    card = kb / "Справочник.md"
    card.write_text(f'---\nid: X\ntitle: "Справочник"\nkind: dictionary\n'
                    f'status: knowledge\ntype: reference\nsource: "Sources/C/S.md"\n'
                    f'---\n\n{R.QUOTES}\n{body}\n', encoding="utf-8")

    saw = {}

    def fake(cfg, role, messages, **kw):
        saw[role] = stub_messages(messages, kw)[0]["content"]
        if role == "planner":
            rows = {"parts": [{"title": f"Термины {k*10+1}-{k*10+10}",
                               "from": 1 + k*10, "to": 10 + k*10} for k in range(3)]}
            return {"ok": True, "text": json.dumps(rows, ensure_ascii=False),
                    "backend": 1, "model": "p", "tps": 9, "log": []}
        return {"ok": True, "text": "ТЕЗИС", "backend": 1, "model": "m", "tps": 9, "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "3000"})
    R.run_distill(cfg, str(tmp), apply=True, limit=5, momus=False, call=fake)

    assert "planner" in saw, "словарь не дошёл до планировщика"
    assert "worker" not in saw, "словарю писали тезис — тело словаря переписывать нельзя"
    # опись, а не текст: первые слова абзаца в ней есть по делу, а сам абзац целиком — нет
    assert para(1).strip() not in saw["planner"], \
        "планировщику отдали текст вместо описи: на длинном теле это не сработает"
    assert len(saw["planner"]) < len(body) / 2, \
        f"опись не короче тела ({len(saw['planner'])} против {len(body)}) — это не опись"

    parts = sorted(p for p in kb.glob("Термины*.md"))
    assert len(parts) == 3, f"частей должно быть три, а не {len(parts)}"
    first = parts[0].read_text(encoding="utf-8")
    assert para(1).strip()[:40] in first, "текст части не дословный — движок его пересказал"
    assert "part_of:" in first and "[[Термины-11-20]]" in first, \
        "часть без связей: нарезка чинит одно и ломает другое (TC-036)"
    assert "status: draft" in first, "часть родилась с присвоенным доверием"

    left = card.read_text(encoding="utf-8")
    assert "status: index" in left, "карта документа осталась знанием — попадёт в пак дважды"
    assert left.count("[[Термины-") == 3, "в карте не все части"
    assert para(1).strip()[:40] not in left, "тело осталось в карте: знание задвоилось"


@test
def test_context_pack_knows_the_model_window(tmp: Path):
    """Пак собирался вслепую к окну: `--budget` ставил человек, иначе предела не было.

    Пак — единственное место, где знание уходит модели пачкой, и он рос без ограничения:
    43 карточки на живой базе. Дальше два исхода, оба плохие: шлюз откажет по длине, либо
    модель молча прочитает начало. Окно объявлено — берём его как бюджет и **говорим** об
    этом строкой в самом паке, вместе со списком того, что не вошло.
    """
    root = make_project(tmp)
    for i in range(12):
        card(root, f"Concepts/Карточка-{i}.md", "Правило работает так. " * 60,
             status="knowledge", kind="knowledge")

    env = dict(os.environ, AURORA_AGENT_BACKEND_1_URL="http://x/v1",
               AURORA_AGENT_BACKEND_1_MODEL="m", AURORA_AGENT_BACKEND_1_CONTEXT="8000")
    cp = subprocess.run([sys.executable, str(KIT / "scripts/ctx_pack.py"), "правило"],
                        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert "Объём пака ограничен окном модели" in cp.stdout, \
        f"пак не узнал про окно модели:\n{cp.stdout[:400]}"
    assert "исчерпан" in cp.stdout, "бюджет объявлен, а лишнее всё равно вошло"

    # окно не объявлено — предела нет, и пак об этом не врёт
    bare = dict(os.environ)
    for k in list(bare):
        if k.startswith("AURORA_AGENT_"):
            bare.pop(k)
    cp2 = subprocess.run([sys.executable, str(KIT / "scripts/ctx_pack.py"), "правило"],
                         cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", env=bare)
    assert "Объём пака ограничен окном модели" not in cp2.stdout, \
        "движок придумал предел там, где окно не объявлено"


@test
def test_long_source_is_not_silently_cut(tmp: Path):
    """Текст длиннее окна не обрезается молча: либо в несколько заходов, либо отказ.

    В `distill` стояло `quotes.strip()[:12000]`, в `judge_empty` — `[:6000]`. Всё, что
    дальше, в тезис не попадало, и об этом не узнавал никто: ни отчёт, ни карточка, ни
    человек. Это худший вид потери — знание есть в базе, но его нет в тезисе, и разница
    видна только тому, кто откроет источник и прочитает его целиком.

    Теперь длина считается по объявленному окну: влезает — один заход; не влезает, но
    укладывается в три — выписки по частям и свод (каждую часть проверяет Момус); не
    укладывается — отказ с именем лечения. Окна не объявлены — движок не режет вовсе и
    не выдумывает себе предел.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R

    kb = tmp / "AuroraKnowledgeDB/Concepts"
    kb.mkdir(parents=True)

    def card(name, body):
        p = kb / name
        p.write_text(f'---\nid: X\ntitle: "{name[:-3]}"\nkind: knowledge\n'
                     f'status: knowledge\n---\n\n{R.QUOTES}\n{body}\n', encoding="utf-8")
        return p

    seen = []

    def fake(cfg, role, messages, **kw):
        txt = stub_messages(messages, kw)[0]["content"]
        seen.append(len(txt))
        if "Собери из них" in txt:
            return {"ok": True, "text": "СВОД", "backend": 1, "model": "m", "tps": 9, "log": []}
        return {"ok": True, "text": "ТЕЗИС", "backend": 1, "model": "m", "tps": 9, "log": []}

    para = "Правило работает так. " * 40 + "\n\n"
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "4000"})
    budget = A.prompt_budget(cfg, reserve_chars=len(R.PROMPT_DISTILL) + 200)
    assert budget > 0, "окно объявлено, а бюджет не посчитан"

    short = R.distill_card(cfg, str(card("Короткая.md", para * 2)), call=fake, momus=False)
    assert short["status"] == "переписана" and not short.get("parts"), \
        "короткую карточку зачем-то разрезали"

    seen.clear()
    # «Средняя» — два с половиной окна, а не число абзацев: окно под текст зависит от
    # длины задания, и задание, ставшее длиннее, не должно ломать смысл проверки.
    mid_paras = max(1, int(2.5 * budget / len(para)))
    mid = R.distill_card(cfg, str(card("Средняя.md", para * mid_paras)), call=fake, momus=False)
    assert mid["status"] == "переписана" and mid.get("parts") == 3, \
        f"текст на три захода обработан неверно: {mid}"
    assert max(seen) <= budget + len(R.PROMPT_DISTILL_PART) + 500, \
        "кусок не влез в бюджет — резали не по окну"

    huge = R.distill_card(cfg, str(card("Огромная.md", para * 60)), call=fake, momus=False)
    assert huge["status"] == "слишком длинная", "гигантская карточка молча пересказана"
    assert "kb:split" in huge["note"], "отказ без имени лечения"

    # окна не объявлены — режем? нет: движок не придумывает себе предел
    free = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    seen.clear()
    whole = R.distill_card(free, str(card("Безокна.md", para * 60)), call=fake, momus=False)
    assert whole["status"] == "переписана" and not whole.get("parts"), \
        "без объявленного окна движок сам решил порезать текст"
    assert max(seen) > 40000, "текст всё-таки обрезан"

    # и в отчёте это названо, а не растворено
    rep = R.report_distill({"steps": [huge, mid], "left": 0, "unsupported": 0,
                            "seconds": 1.0}, apply=True)
    assert "Слишком длинные для окна модели" in rep and "Огромная" in rep, \
        "отказ не попал в отчёт — человек о потере не узнает"
    assert "Собрано из частей: 1" in rep, "свод из частей не назван в отчёте"

    # в коде, а не в комментариях: комментарии как раз объясняют, почему так больше нельзя
    code = "\n".join(l for l in (KIT / "scripts/agent_runner.py")
                     .read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))
    assert "[:12000]" not in code and "[:6000]" not in code, \
        "тихое обрезание вернулось в код"


@test
def test_the_development_kitchen_stays_private(tmp: Path):
    """Кухня разработки живёт в git, но наружу не уходит.

    Семьдесят с лишним файлов — кейсы, сценарии, разборы дефектов, числа с живых
    проектов — лежали вне git: папка `Development/` целиком в `.gitignore`, потому что
    репозиторий кита публичный. Причина верная, следствие плохое: всё, написанное за
    неделю, существовало в одном экземпляре на одной машине, без истории.

    Решение — отдельная локальная ветка `development`, смонтированная как worktree на то
    же место. История есть, файлы там же, где были, `master` их по-прежнему не видит. А
    от `git push --all` защищает хук: он не главная защита (главная — понимать, что туда
    пишут), но опечатку ловит.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import aurora_hooks as H

    assert "development" in H.PRIVATE_BRANCHES, "ветка кухни не защищена от публикации"
    assert "history-private" in H.PRIVATE_BRANCHES, "прежняя приватная ветка забыта"
    assert "PUSH_HOOK" in (KIT / "scripts/aurora_hooks.py").read_text(encoding="utf-8"), \
        "хука пуша нет — после клона защиты не будет"

    # хук ставится вместе с остальными и только в ките: в проекте публиковать нечего
    src = (KIT / "scripts/aurora_hooks.py").read_text(encoding="utf-8")
    tail = src[src.index("    # Хук пуша"):][:900]
    assert "if is_kit():" in tail, \
        "хук пуша ставится и в проектах — там он бессмыслен, а мешать будет"

    # и он действительно отказывает
    repo = tmp / "репо"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "engine_manifest.txt").write_text("x", encoding="utf-8")
    hook = repo / ".git/hooks/pre-push"
    hook.write_text(H.PUSH_HOOK.format(marker=H.PUSH_MARKER,
                                       branches=" ".join(H.PRIVATE_BRANCHES)),
                    encoding="utf-8")
    hook.chmod(0o755)
    for branch, public, expect in (("development", "https://github.com/x/y.git", 1),
                                   ("master", "https://github.com/x/y.git", 0),
                                   ("development", "/tmp/private.git", 0)):
        cp = subprocess.run(["sh", str(hook), "origin", public],
                            input=f"refs/heads/{branch} abc123 refs/heads/{branch} def456\n",
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert cp.returncode == expect, \
            (f"ветка {branch} на {public}: ждали rc={expect}, получили {cp.returncode}\n"
             f"{cp.stderr[:300]}")

    # `.gitignore` обязан объяснять, куда делась папка: иначе после клона её сочтут мусором
    ign = (KIT / ".gitignore").read_text(encoding="utf-8")
    assert "git worktree add Development development" in ign, \
        "после клона папку кухни нечем восстановить — команда не записана"


@test
def test_nothing_leaves_the_perimeter_unchecked(tmp: Path):
    """Наружу уходят вопросы про механики, а не пересказ задачи заказчика.

    Запрос к внешнему серверу модель строит из промпта, а в промпте лежит задача
    аналитика и пак знаний. Без сторожа формулировки требований уедут в чужой поисковый
    API — не потому что модель злонамеренна, а потому что ей больше не из чего составить
    вопрос.

    Сторож механический: совпало четыре слова подряд после той же нормализации, что в
    поиске по базе, — не пропускаем. Это порог, а не стена: перескажет другими словами —
    пройдёт. Полная гарантия одна — не подключать `outbound`-серверы вовсе.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    sys.path.insert(0, str(KIT / "scripts" / "agents"))
    import agent_core as A
    import pydantic_ai_adapter as AD

    idea = "Возврат обеспечительного платежа после аннулирования заявки"
    pack = "## Заявка-и-статусы\nЗаявка проходит статусы. Возврат обеспечительного платежа."
    guard = {"grams": A.guard_grams([idea, pack]), "gram": A.GRAM,
             "max_words": A.MAX_QUERY_WORDS, "ready": True}
    assert guard["grams"], "сторож пуст — из текста проекта не собрано ни одной четвёрки"

    assert not AD.leaks("что такое SAGA pattern в микросервисах", guard), \
        "заблокирован вопрос про механику — ради него всё и затевалось"

    # Сторож ЗАКРЫТ по умолчанию: пустой guard значит «движок его не собрал», а не
    # «можно всё». Следующий вызывающий, забывший передать текст проекта, иначе получил
    # бы канал наружу без единой проверки. Найдено критиком после реализации.
    assert AD.leaks("любой текст задачи заказчика", {}), \
        "без собранного сторожа наружу уходит всё — это открытый канал"
    assert AD.leaks("текст", {"grams": [], "max_words": 15}), \
        "guard без отметки ready принят за разрешение"
    assert AD.leaks(idea, guard), "задача аналитика ушла наружу дословно"
    # перефразировка ФОРМОЙ ловится: нормализация та же, что в поиске
    assert AD.leaks("Возврату обеспечительных платежей после аннулирования", guard), \
        "перефразировка словоформами прошла — значит сторож ловит только цитату"
    assert AD.leaks(" ".join(["слово"] * 20), guard), "нет потолка длины запроса"

    # отказ ОБЪЯСНЯЕТСЯ: пустой результат модель истолкует как «в интернете ничего нет»
    why = AD.leaks(idea, guard)
    assert "переформулируйте" in why, "отказ без объяснения — модель уйдёт с ложным знанием"

    # журнал: обе категории, сто строк, в проекте
    root = tmp / "проект"
    (root / "Workspaces").mkdir(parents=True)
    for i in range(105):
        AD.log_outbound(str(root), "search", f"запрос {i}", "ушёл" if i % 2 else "не пропущен")
    log = (root / "Workspaces/_outbound.md").read_text(encoding="utf-8")
    rows = [l for l in log.splitlines() if l.startswith("| 20")]
    assert len(rows) == 100, f"журнал не держит сто строк: {len(rows)}"
    assert "не пропущен" in log and "ушёл" in log, \
        "в журнале одна категория: без заблокированных не понять, не мешает ли сторож"

    # роли: сервер достаётся тем, кому объявлен
    cfg = {"mcpServers": {
        "inside": {"command": "echo", "args": ["x"]},
        "search": {"command": "echo", "args": ["y"], "roles": ["planner"], "outbound": True}}}
    assert set(AD.servers_for_role(cfg, "planner")) == {"inside", "search"}, \
        "планировщику не досталось объявленного ему сервера"
    assert set(AD.servers_for_role(cfg, "worker")) == {"inside"}, \
        "воркер получил сервер, объявленный только планировщику"

    # Аргументы бывают вложенными: `{"queries": ["…"]}` или `{"query": {"text": "…"}}`.
    # Собирать только верхний уровень значит выпустить задачу целиком, а в журнал
    # записать пустую строку. Найдено критиком после реализации.
    import asyncio
    sent = []

    async def call_tool(name, args):
        sent.append(args)
        return "ушло"

    hook = AD.outbound_hook("search", guard, str(root))
    for args in ({"query": idea}, {"query": {"text": idea}}, {"queries": [idea, "ещё"]}):
        res = asyncio.run(hook(None, call_tool, "search", args))
        assert "не отправлен" in str(res), f"вложенный аргумент прошёл мимо сторожа: {args}"
    assert not sent, "запрос с текстом проекта всё-таки ушёл на сервер"
    assert "ушло" == asyncio.run(hook(None, call_tool, "search",
                                      {"query": "что такое SAGA pattern"})), \
        "безопасный вопрос не дошёл до сервера"

    ad = (KIT / "scripts/agents/pydantic_ai_adapter.py").read_text(encoding="utf-8")
    assert "process_tool_call=hook" in ad, "сторож не подключён к вызовам инструментов"
    assert "if (spec or {}).get(\"outbound\")" in ad, \
        "сторож вешается на все серверы подряд — Confluence внутри периметра фильтровать незачем"
    assert "tool_calls_limit" in ad, \
        "нет потолка на вызовы инструментов: один сложный вопрос съест бюджет прогона"


@test
def test_retrieval_is_watched_not_guessed(tmp: Path):
    """Ранжирование меняется — и это должно быть видно, а не выясняться по жалобам.

    На нём стоит всё: ответ базы, обогащение перед производством артефакта, инструменты
    ассистента. «Стало лучше» — не проверка. Поэтому есть сторож на эталонном корпусе
    (падает сам) и отчёт по живым запросам (показывает разницу с прошлым разом).

    Учёт редкости слова добавлен под этим сторожем, а не вслепую: «ЭСФ» весит больше,
    чем «статус», потому что встречается в базе реже.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import ctx_pack as P

    # редкость: частое слово весит меньше, редкое больше, неизвестное — обычно
    P.RARITY.clear()
    P.RARITY.update({"__total__": 100, "статус": 40, "заявк": 12, "эсф": 1})
    assert P.weight("статус") < P.weight("заявк") < P.weight("эсф"), \
        "редкость слова не влияет на вес — «статус» и «ЭСФ» сужают поиск одинаково"
    assert P.weight("неведомое") == 1.0, \
        "слова нет в базе — судить не по чему, вес должен быть обычным"
    assert P.weight("эсф") <= 2.0, \
        "разброс весов слишком широк: одна опечатка в запросе перевесит всё остальное"

    # сторож на корпусе: есть эталон и он сходится
    expected = KIT / "tests/corpus/RETRIEVAL.json"
    assert expected.is_file(), "нет эталона выдачи — сторожить нечем"
    saved = json.loads(expected.read_text(encoding="utf-8"))
    assert len(saved) >= 5, "эталон из пары запросов ничего не сторожит"
    assert all(v for v in saved.values()), \
        "в эталоне есть запросы, которые ничего не находят — такой сторож не сторожит"

    cp = subprocess.run([sys.executable, str(KIT / "scripts/dev_qa.py"), "--retrieval"],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert cp.returncode == 0, f"выдача по корпусу разошлась с эталоном:\n{cp.stdout[-600:]}"
    assert "Порядок не менялся" in cp.stdout, cp.stdout[-400:]

    # отчёт по живому проекту: берёт настоящие запросы и умеет сравнивать
    src = (KIT / "scripts/kb_retrieval.py").read_text(encoding="utf-8")
    assert 'line.startswith("### Вопрос")' in src, \
        "запросы читаются не тем форматом, которым их пишет agent:ask"
    assert "retrieval-last.json" in src, "не с чем сравнивать: точка отсчёта не хранится"
    # Молчание про неизмеренное — не подтверждение: отчёт писал «порядок не менялся» про
    # запрос, которого в точке сравнения не было вовсе. Найдено критиком.
    assert "Не с чем сравнить" in src and "сравнимых" in src, \
        "новый запрос считается подтверждённым — это догадка вместо факта"
    assert "P.measure_rarity(cards)" in src, \
        "отчёт считает без редкости слов — сторожит не то ранжирование, что работает"

    # Плитка связи считает по тем строкам, которые печатает `agent:ping`, а не по
    # придуманным значкам. Первая версия искала «❌», которого скрипт не пишет вовсе, —
    # и сказала бы «3 из 3 отвечают» при мёртвом третьем бэкенде. Найдено на живом
    # прогоне: ровно то, ради чего плитка и заведена.
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    sample = ("# Агент — проверка цепочки\n\n"
              "✅ №1 https://a/v1 · m1 · 7.29 с · «готов»\n"
              "✅ №2 http://b/v1 · m2 · 0.54 с · «Готов»\n"
              "✗ №3 http://c/v1 · m3 — нет связи\n"
              "✅ Эмбеддинги: bge-m3 на https://a/v1 · размерность 1024")
    st = ck.ping_state(str(tmp), sample, 0)
    assert (st["alive"], st["dead"]) == (2, 1), \
        f"плитка связи считает не по выводу agent:ping: {st['alive']}/{st['dead']}"
    assert st["embed"], "живой шлюз эмбеддингов не распознан"
    assert st["when"], "нет отметки времени: «проверено месяц назад» — тоже ответ"

    # прогон одной проверки: без него правка одной вещи стоит полного прогона
    runner = (KIT / "tests/harness.py").read_text(encoding="utf-8")
    assert "--only" in runner and "only.lower() in name.lower()" in runner, \
        "нельзя прогнать одну проверку"
