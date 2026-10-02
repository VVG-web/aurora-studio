"""Проверки движка Aurora, часть 6 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import subprocess
import sys
import time

from harness import (  # noqa: F401
    KIT,
    make_project,
    panel_sources,
    run,
    test,
    why,
)


@test
def test_restart_does_not_silently_kill_a_running_job(tmp: Path):
    """Вывод прогона идёт в трубу панели: убьём панель — прогон умрёт следом.

    Не сразу, а на первой же строке, которую он попытается напечатать. Ночной разбор
    базы теряется от одного обновления кита, и человек узнаёт об этом по «задание шага
    потеряно». Дважды за одну сессию так и вышло, причём оба раза перезапускал не он.

    Список работающего живёт на диске, а не в памяти: знать о нём должен ДРУГОЙ процесс —
    тот, который собирается убить работающий.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "def running_now(" in src and "RUNNING" in src, \
        "панель не оставляет следа о том, что сейчас работает"
    at = src.index("if a.restart and prev.get(\"pid\")")
    guard = src[at:at + 1400]
    assert "running_now()" in guard and "a.force" in guard, \
        "перезапуск убивает работу молча"
    assert "return 2" in guard, "отказ не останавливает перезапуск"
    assert "--force" in src, "нет способа перезапустить осознанно"
    assert "os.remove(RUNNING)" in src, \
        "после падения мёртвые записи навсегда запретят перезапуск"

    # Запрет без кнопки «прервать» — тупик: ни остановить, ни перезапустить.
    assert "def stop_job(" in src and '"/api/job/stop"' in src, \
        "прогон нельзя прервать — остаётся ждать часами"
    assert "proc.terminate()" in src and "proc.kill()" in src, \
        "прерывание не доводится до конца, если процесс не отозвался"
    # Панель заводилась, чтобы не ходить в терминал: за собственным перезапуском тоже.
    assert "def restart_self(" in src and '"/api/restart"' in src, \
        "перезапуск панели возможен только из терминала"
    assert 'AURORA_COCKPIT_TOKEN' in src, \
        "после перезапуска из панели открытая вкладка перестанет работать: токен другой"

    ui = panel_sources()
    assert 'id="consoleStop"' in ui and "/api/job/stop" in ui, "нет кнопки «Прервать»"
    # Кнопка нужна ОБОИМ способам опроса. Она висела только на одиночном запуске, а
    # маршрут — то, что идёт часами, — опрашивает задание своим циклом, и там её не было.
    assert "function armStop(" in ui, "управление кнопкой размазано по двум циклам"
    # Окно — на весь runStep, а не на «сколько было»: F5 легально добавил в цикл шага
    # слежение за тишиной, и функция выросла. Иллюзия «кнопка пропала» от короткого среза.
    # Срез — до конца функции: в 1.122.0 отказ сервера стал печататься в консоль, runStep
    # снова вырос, и фиксированное окно в 2200 знаков потеряло конец функции.
    at = ui.index("async function runStep(")
    step = ui[at:ui.index("\n}\n", at)]
    assert "armStop(res.job)" in step, "у шага маршрута нет кнопки «Прервать»"
    assert "armStop(null)" in step, "кнопка остаётся висеть после конца шага"
    assert "ROUTE.stopped" in ui, \
        "прерывание человеком показывается как отказ команды: «разберитесь с ней»"
    assert "restartPanel" in ui and "Перезапустить панель" in ui, \
        "полоса про устаревший процесс всё ещё отсылает в терминал"
    # Прерванный процесс возвращает отрицательный код: «код 2 и выше» его пропускало,
    # и маршрут после осознанного прерывания шёл дальше.
    assert "const failed = rc => rc >= 2 || rc < 0;" in ui, \
        "маршрут не считает прерывание поводом остановиться"
    assert "rc >= 2){ ROUTE.failed" not in ui, "остались проверки, пропускающие прерывание"

    # Отметка ставится вокруг запуска команды, а не где-то рядом.
    run = src[src.index("mark_running(job[\"id\"], cmd, project, True)") - 400:]
    assert "mark_running(job[\"id\"], cmd, project, False)" in run[:4000], \
        "запись о работе не снимается по окончании — перезапуск запретится навсегда"

    was = ck.running_now()
    ck.mark_running("тест", "agent:distill", "/x/Проект", True)
    now = ck.running_now()
    assert now.get("тест", {}).get("cmd") == "agent:distill", "работа не отмечена"
    assert now["тест"]["project"] == "Проект", "не сказано, в каком проекте идёт работа"
    ck.mark_running("тест", "agent:distill", "/x/Проект", False)
    assert "тест" not in ck.running_now(), "отметка не снялась"
    assert ck.running_now() == was, "список работающего изменился после теста"


@test
def test_index_description_carries_no_link_markup(tmp: Path):
    """Описание карточки в оглавлении не несёт разметки ссылок.

    Описание — первая содержательная строка тела, обрезанная по длине. Если в строке была
    вики-ссылка, обрезка рубила её пополам, и в описание попадали открывающие скобки без
    закрывающих:

        …ника, смотри [[ALG-145_Получение_сведен…

    В таблице оглавления следующая ячейка закрывала их своей разметкой — получалась
    ссылка на имя-обрубок. Она не ведёт никуда **по построению**, линтер объявлял
    оглавление отставшим, а `kb:index` считал, что всё на месте, и писал тот же обрубок
    заново. Две команды говорили об одном файле противоположное, и починить это
    пересборкой было нельзя.

    Описанию ссылки не нужны: на карточку уже ведёт первая колонка строки.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    ki = importlib.import_module("kb_index")
    importlib.reload(ki)

    long_link = "[[ALG-145_Получение_сведений_и_изменение_статуса_документа]]"
    text = ("Карточка-заготовка без содержательного определения, наполняется при "
            "разборе источника, смотри " + long_link + " далее")
    got = ki.first_sentence(text)
    assert "[[" not in got and "]]" not in got, (
        f"описание несёт разметку ссылки: {got[-50:]!r} — при обрезке она рвётся, "
        "и оглавление получает ссылку на имя-обрубок")
    assert "ALG-145" in got or len(got) <= 120, "текст ссылки потерян целиком"

    # Короткая строка со ссылкой — тоже без разметки, но со словами.
    short = ki.first_sentence("Смотри [[Курс валют ЦБ]] и далее")
    assert "[[" not in short and "Курс валют ЦБ" in short, \
        f"текст ссылки должен остаться словами: {short!r}"


@test
def test_double_brackets_before_a_url_are_not_a_card_link(tmp: Path):
    """`[[текст]](адрес)` — это markdown-ссылка, а не ссылка на карточку.

    Найдено на живой базе: линтер объявлял битыми ссылки вида

        [[Статус: готово]](https://…/viewpage.action?pageId=327458578)

    Это markdown-ссылка, у которой сам текст пришёл из источника в квадратных скобках —
    после конвертации получилось двойное открытие. Карточки «Статус: готово» нет и быть
    не должно, а линтер требовал её завести. То же с обычными сносками `[[1]](#fn1)`.

    Такие ошибки хуже пропущенных: человек идёт заводить карточку под то, что карточкой
    не является, а число в отчёте растёт само по себе при каждом новом разборе.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    ac = importlib.import_module("aurora_common")
    importlib.reload(ac)

    assert ac.link_refs("см. [[Курс валют ЦБ]] далее") == ["Курс валют ЦБ"], \
        "настоящая вики-ссылка перестала распознаваться"
    assert ac.link_refs("[[Статус: готово]](https://example.com/x) RYl:X") == [], (
        "markdown-ссылка с текстом в скобках принята за ссылку на карточку — "
        "линтер потребует завести карточку «Статус: готово»")
    assert ac.link_refs("текст [[1]](#fn1) ещё") == [], \
        "сноска принята за ссылку на карточку"
    assert ac.link_refs("[Курс валют](Курс-валют.md)") == [], \
        "обычная markdown-ссылка не вики-ссылка"

    # Перепись ссылок такую конструкцию тоже не должна трогать: это чужой синтаксис.
    txt = "[[Статус: готово]](https://example.com/x) и [[Курс валют ЦБ]]"
    got = ac.rewrite_links(txt, {"Курс валют ЦБ": "Курс-валют-ЦБ", "Статус: готово": "Ой"})
    assert "[[Статус: готово]](https://example.com/x)" in got, \
        "перепись покорёжила markdown-ссылку, приняв её за вики-ссылку"
    assert "[[Курс-валют-ЦБ]]" in got, "настоящая ссылка не переписалась"


@test
def test_a_clear_refusal_is_not_a_dead_provider(tmp: Path):
    """Внятный отказ живого сервера не сажает его в пятнадцатиминутный карантин.

    Карантин придуман против молчащего провайдера: не спрашивать мёртвого на каждом
    источнике. Но под него попадал любой ответ не-200, включая те, что ожиданием не
    лечатся:

        400  запрос не по вкусу шлюза — правится запросом
        401  ключ неверен или истёк    — правится ключом
        404  нет такой модели          — правится настройкой

    Сервер при этом жив и ответил за доли секунды. Пятнадцать минут карантина здесь не
    помогают, а мешают: причина скрыта за строкой «не отвечал», и человек ищет проблему
    в сети вместо настройки.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")
    importlib.reload(A)

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a",
                          "AURORA_AGENT_BACKEND_1_MODEL": "m"})

    def refuse(code, text):
        def transport(kind, b, payload, timeout):
            if kind == "slots":
                return (404, None, "нет /slots", 0.0)
            return (code, None, text, 0.05)
        return transport

    for code, text in ((400, "Unrecognized request argument supplied: _slots"),
                       (401, "Invalid authentication credentials"),
                       (404, "The model does not exist")):
        A.DOWN.clear()
        started = time.time()
        r = A.call_role(cfg, "worker", [{"role": "user", "content": "?"}],
                        transport=refuse(code, text), deadline=time.time() + 5,
                        sleep=lambda s: None)
        assert not r["ok"], f"отказ {code} принят за успех"
        # Внятный отказ ожиданием не лечится: вызов обязан закончиться сразу, а не гонять
        # тот же запрос по кругу до срока. Прежде этот тест сам шёл девять минут — ровно
        # столько кольцо переспрашивало сервер, уже сказавший «нет».
        assert time.time() - started < 3, \
            f"HTTP {code} переспрашивается по кругу до срока — это опрос впустую"
        assert 1 not in A.DOWN, (
            f"HTTP {code} посадил живой шлюз в карантин на 15 минут — а он ответил за "
            "0.05 с, и ожидание тут ничего не чинит")
        joined = " ".join(r["log"])
        assert str(code) in joined or text[:20] in joined, \
            f"причина отказа не названа человеку: {r['log']}"

    # А молчание — по-прежнему карантин: мёртвого не спрашивают на каждом источнике.
    A.DOWN.clear()
    dead = lambda kind, b, payload, timeout: (None, None, "Connection refused", 0.0)
    started = time.time()
    r = A.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=dead,
                    deadline=time.time() + 5, sleep=lambda s: None)
    assert 1 in A.DOWN, "молчащий провайдер обязан попадать в карантин"
    assert time.time() - started < 3 and "повторять незачем" in " ".join(r["log"]), \
        "шлюз в карантине дольше вызова всё равно ждут до конца срока"


@test
def test_ping_probes_instead_of_reading_the_quarantine(tmp: Path):
    """Проверка связи опрашивает шлюз, а не пересказывает отметку карантина.

    Бэкенд, не ответивший однажды, помечается недоступным на пятнадцать минут: это верно
    для рабочих вызовов — не спрашивать мёртвого на каждом источнике. Но `agent:ping`
    наследовал ту же отметку и печатал «не отвечал, вернёмся через 865 с».

    Это сообщение о **пропуске**, а не результат опроса: к шлюзу никто не обращался.
    Человек видел «недоступен» у сервера, которым в ту же минуту пользовались другие
    программы, и каждая следующая проверка повторяла ту же строку — потому что проверка
    и была причиной, по которой отметку не снимали.

    Проверка связи, показывающая кеш, бесполезна ровно тогда, когда нужна.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")
    importlib.reload(A)

    src = (KIT / "scripts/agent_core.py").read_text(encoding="utf-8")
    body = src[src.index("def cmd_ping("):]
    body = body[:body.index("\ndef ", 10)]
    assert "DOWN.pop" in body, (
        "cmd_ping не снимает карантин перед опросом — покажет метку вместо проверки")

    # И по существу: помеченный бэкенд всё равно опрашивается.
    A.DOWN[1] = time.time() + 900
    asked = []

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        asked.append(b["n"])
        return (200, {"choices": [{"message": {"content": "готов"},
                                   "finish_reason": "stop"}]}, "", 0.1)

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a",
                          "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    A.DOWN[1] = time.time() + 900
    r = A.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=transport,
                    deadline=time.time() + 10, sleep=lambda s: None)
    assert not asked and not r["ok"], \
        "рабочий вызов обязан щадить помеченный бэкенд — иначе мёртвого спросят на каждом источнике"
    A.DOWN.pop(1, None)
    r2 = A.call_role(cfg, "worker", [{"role": "user", "content": "?"}], transport=transport,
                     deadline=time.time() + 10, sleep=lambda s: None)
    assert asked == [1] and r2["ok"], f"после снятия метки опрос не состоялся: {r2['log']}"


@test
def test_gateway_gets_only_what_it_understands(tmp: Path):
    """В шлюз уходит запрос по стандарту, без внутренних полей движка.

    С живого контура: шлюз, которым успешно пользуются chatbox и opencode, у Авроры
    числился недоступным. Модель существовала, ключ был верен, сервер отвечал за 0.08 с.
    Ломался сам запрос: `default_transport` отдавал в HTTP **весь** payload, а в нём
    лежат поля для внутреннего адаптера —

        _slots      сколько процессов держать пулу (пула больше нет — снято в 1.139.0)
        guard       сторож исходящего для инструментов
        role        роль вызова
        tools_root  корень файлов проекта
        mcp         настройка MCP-серверов
        history     история разговора (в HTTP она уже слита в messages)

    Строгий шлюз на неизвестное поле отвечает `400 Unrecognized request argument`, движок
    объявлял бэкенд мёртвым на пятнадцать минут и показывал «не отвечал». Повтор без
    `chat_template_kwargs` при 400 уже был — то есть про капризные шлюзы знали, — но
    `_slots` не снимался никогда, и повтор падал так же.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")
    importlib.reload(A)

    sent = {}

    def fake_http(url, payload, key, timeout):
        sent["url"], sent["payload"] = url, payload
        return 200, {"choices": [{"message": {"content": "ок"}}]}, "", 0.01

    real, A.http_json = A.http_json, fake_http
    try:
        A.default_transport("chat", {"url": "http://x/v1", "key": "k", "n": 1},
                            {"model": "m", "messages": [{"role": "user", "content": "?"}],
                             "max_tokens": 1, "chat_template_kwargs": {"enable_thinking": False},
                             "role": "worker", "tools_root": "/tmp", "mcp": {},
                             "mcp_active": [], "guard": {"ready": False}},
                            5.0)
    finally:
        A.http_json = real

    got = set(sent["payload"])
    internal = {"role", "tools_root", "mcp", "mcp_active", "guard"}
    leaked = sorted(got & internal)
    assert not leaked, (
        f"в шлюз ушли внутренние поля движка: {leaked}. Строгий шлюз отвечает на них "
        "400 Unrecognized request argument, а движок объявляет его недоступным на 15 минут")
    assert {"model", "messages"} <= got, f"из запроса пропало нужное: {sorted(got)}"
    assert "max_tokens" in got, "max_tokens — стандартное поле, его снимать нельзя"


@test
def test_a_backend_with_a_free_slot_is_not_busy(tmp: Path):
    """Занят тот шлюз, у которого заняты ВСЕ слоты, а не хоть один.

    `llama.cpp` не отказывает при нагрузке, а молча ставит в очередь, поэтому занятость
    смотрят в `/slots`. Но проверка объявляла шлюз занятым, если работает **хоть один**
    слот: `any(is_processing)`. У сервера с четырьмя слотами один занятый закрывал
    остальные три.

    На живом прогоне это стоило трети партии. В вердикте: «разобрано 9 из 15, одна и та
    же ошибка 3 раза подряд: №3: слот занят (/slots) — дальше по кольцу; дедлайн
    исчерпан». Кольцо обходило свободные шлюзы, упиралось в дедлайн, и шаг падал —
    при живых серверах с незанятой ёмкостью.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A
    b = {"n": 2, "url": "http://x/v1"}

    def slots(state):
        return lambda kind, _b, _p, _t: (200, state, "", 0.0)

    assert not A.busy(b, slots([{"is_processing": False}] * 4)), \
        "все слоты свободны — шлюз не занят"
    assert not A.busy(b, slots([{"is_processing": True}] + [{"is_processing": False}] * 3)), (
        "один занятый слот из четырёх закрыл весь шлюз — кольцо пройдёт мимо сервера, "
        "готового взять работу, и упрётся в дедлайн")
    assert not A.busy(b, slots([{"is_processing": True}] * 3 + [{"is_processing": False}])), \
        "последний свободный слот всё ещё можно занять"
    assert A.busy(b, slots([{"is_processing": True}] * 4)), \
        "все слоты в работе — вот теперь занят"

    # Шлюз без /slots: проверка неприменима, а не «занят».
    assert not A.busy(b, lambda kind, _b, _p, _t: (404, None, "нет /slots", 0.0)), \
        "шлюз без /slots объявлен занятым — так отключается всё кольцо разом"
    assert not A.busy(b, slots([])), "пустой список слотов — не повод считать занятым"


@test
def test_a_flaky_gateway_is_not_a_dead_one(tmp: Path):
    """Шаг, который сделал работу и споткнулся, повторяется сразу, а не через четверть часа.

    С живого прогона: с 02:29 до 06:54 маршрут девять раз подряд гонял `agent:distill` и
    ни разу не сдвинулся дальше. Шлюз был не мёртвый, а мигающий — почти каждая попытка
    переписывала по 12–15 карточек и всё равно уходила в пятнадцатиминутное ожидание,
    потому что в выводе были признаки офлайна. Оборот двигался только на попытках с нулём
    сбоев; таких за четыре с половиной часа выпало две, а около двух часов ушло в простой
    при живых бэкендах.

    Пятнадцать минут — верная пауза для лежащего шлюза и вредная для мигающего. Отличать
    их можно по тому, что шаг напечатал о сделанном: есть работа — есть связь.
    """
    ui = panel_sources()

    assert "ROUTE_FLAKY_RETRY_MS" in ui, \
        "нет короткой паузы для мигающего шлюза — маршрут снова будет стоять при живых бэкендах"
    assert "const madeProgress" in ui, "нет признака «шаг сделал работу»"

    # Признак ищем в тех числах, которые шаги печатают на самом деле.
    for pat in ("переписано:", "разобрано", "уточнено:", "тезисов:"):
        assert pat in ui[ui.index("const DID_WORK"):ui.index("const madeProgress") + 400], \
            f"признак прогресса не знает про «{pat}» — такой шаг сочтут безрезультатным"

    body = ui[ui.index("const attempt = async () =>"):]
    body = body[:body.index("const manualRetry")]
    assert "madeProgress" in body, \
        "автоповтор не различает мигающий шлюз и лежащий — вернётся простой на два часа"
    assert "if (!flaky) cy.attempt++" in body, (
        "попытки считаются и для мигающего шлюза: восемь удачных попыток подряд исчерпают "
        "лимит и остановят ожидание, хотя связь есть и работа идёт")


@test
def test_two_writing_runs_do_not_share_one_base(tmp: Path):
    """Второй пишущий прогон агента в ту же базу останавливается замком.

    Отметка `.running.json` живёт в ките и известна только панели: команду, запущенную
    мимо неё — из терминала, из другого харнесса, вторым окном, — не останавливало ничто.
    На живом проекте так и вышло: маршрут панели и терминальный цикл строили базу
    одновременно, два процесса читали и писали один манифест. Обошлось, но это удача:
    потерянная отметка «разобрано» — это повторный разбор источника и двойники карточек.

    Замок только на запись: читающие задачи не мешают никому и чужой замок не снимают.
    Мёртвый процесс замок не держит — иначе прогон, убитый по Ctrl+C, запер бы базу.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    ar = importlib.import_module("agent_runner")
    importlib.reload(ar)

    root = make_project(tmp)
    got, busy = ar.writing_lock(str(root), "build")
    assert got and not busy, f"первый прогон не взял замок: {busy}"

    lock = Path(root) / ar.LOCK
    assert lock.is_file(), "замок не записан в проект"

    # Второй — тем же процессом: замок наш, значит это тот же прогон, и он проходит.
    # Проверяем именно ЧУЖОЙ: подменяем pid на живой процесс, которым точно не являемся.
    import json as _j
    held = _j.loads(lock.read_text(encoding="utf-8"))
    held["pid"] = os.getppid()          # родитель жив, но это не мы
    lock.write_text(_j.dumps(held), encoding="utf-8")
    got2, busy2 = ar.writing_lock(str(root), "distill")
    assert not got2, "второй пишущий прогон зашёл в базу поверх первого"
    assert "идёт пишущий прогон" in busy2, f"причина отказа не названа словами: {busy2}"

    # Чужой замок читающий прогон не снимает.
    ar.release_lock(str(root))
    assert lock.is_file(), "чужой замок снят — дверь второму писателю снова открыта"

    # Процесс, которому нам не дано сигналить, — ЖИВ, а не мёртв. Системный процесс
    # усыновляет любой прогон, чей родитель ушёл; записав его в мёртвые, движок снимал
    # замок и пускал второго писателя в базу.
    held["pid"] = 1
    lock.write_text(_j.dumps(held), encoding="utf-8")
    got_sys, busy_sys = ar.writing_lock(str(root), "distill")
    assert not got_sys, "замок чужого процесса снят: отказ в правах принят за смерть"
    assert "идёт пишущий прогон" in busy_sys, busy_sys

    # Мёртвый процесс базу не запирает.
    held["pid"] = 99999999
    lock.write_text(_j.dumps(held), encoding="utf-8")
    got3, _ = ar.writing_lock(str(root), "build")
    assert got3, "замок от мёртвого процесса запер базу навсегда"

    ar.release_lock(str(root))
    assert not lock.is_file(), "свой замок не снялся"


@test
def test_verdict_is_a_function_not_a_local_string(tmp: Path):
    """Имя `verdict` в `main()` обязано остаться функцией-оракулом.

    В ветке `agent:ask` заводилась локальная строка `verdict = "… Момус: чисто"`. Для
    Python этого достаточно, чтобы считать имя локальным на ВСЮ функцию: в конце `main()`,
    где вызывается `verdict(res, apply)`, оно оказывалось «ещё не присвоенным», и любой
    прогон, дошедший до оракула не через `ask`, падал:

        UnboundLocalError: cannot access local variable 'verdict'

    Прогон при этом уже отработал и записал результат в базу — падал он на последней
    строке. Человек видел красное там, где всё получилось, а маршрут считал шаг
    провалившимся и останавливался.

    Проверяем не текст, а факт: в теле `main()` нет присваивания имени, которым названа
    функция-оракул. Такое затенение не ловится ни линтером, ни глазами при чтении диффа —
    ловится только правилом.
    """
    import ast
    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    top = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "verdict" in top, "функция-оракул verdict пропала из модуля"

    main = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    assert main, "в agent_runner нет main()"
    shadowed = set()
    for node in ast.walk(main[0]):
        targets = (node.targets if isinstance(node, ast.Assign) else
                   [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else [])
        for tgt in targets:
            if isinstance(tgt, ast.Name) and tgt.id in top:
                shadowed.add(tgt.id)
    assert not shadowed, (
        f"в main() присваиваются имена модульных функций: {sorted(shadowed)} — "
        "Python считает их локальными на всю функцию, и вызов такой функции ниже по коду "
        "падает UnboundLocalError уже после того, как работа сделана")


@test
def test_aliases_report_survives_leftovers_and_rejections(tmp: Path):
    """Отчёт о синонимах не должен падать, когда работа осталась или критик отклонил.

    С живого прогона: `agent:aliases` разобрал 14 конфликтов из 15 — восемь минут работы
    модели, всё записано в базу — и упал на составлении отчёта:

        UnboundLocalError: cannot access local variable 'L'

    В `verdict()`, который обязан вернуть пару «успех, почему», лежали два блока текста
    отчёта с `L += [...]`, а `L` там нет вовсе: он живёт в `report()`. Ветки срабатывают,
    когда критик что-то отклонил или осталась работа на следующий прогон, — то есть на
    любом непустом прогоне живой базы.

    Цена ошибки не в трассировке: работа сделана и записана, а команда объявлена
    неуспешной, и маршрут считает шаг провалившимся.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    ar = importlib.import_module("agent_runner")
    importlib.reload(ar)

    res = {
        "steps": [{"alias": "Курс валют", "status": "уточнено", "note": "разведено"},
                  {"alias": "Заявка", "status": "отклонено критиком", "note": "не согласен"}],
        "seconds": 12.0, "total_conflicts": 5, "limited": False, "left": 3,
        "stopped": "дошли до лимита шагов (2)",
        "before": {"conflicts": 5, "errors": 10},
        "after": {"conflicts": 4, "errors": 10},
    }
    ok, why = ar.verdict(res, True)          # раньше здесь был UnboundLocalError
    assert isinstance(ok, bool) and isinstance(why, str), \
        f"вердикт вернул не пару «успех, почему»: {(ok, why)}"

    cfg = ar.AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x",
                              "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    text = ar.report(res, {"ok": True, "sha": "", "why": ""}, True, True, cfg)
    assert "Осталось на следующий прогон: 3" in text, (
        "отчёт молчит про оставшуюся работу — человек не узнает, что прогон надо повторить")
    assert "критик не согласился" in text, \
        "отчёт молчит про отклонённое критиком: эти конфликты остались как были"


@test
def test_a_stub_is_named_like_a_real_card(tmp: Path):
    """Заготовка называется по тем же правилам, что настоящая карточка.

    Имя файла заготовки бралось из текста ссылки дословно — снимались только символы,
    запрещённые файловой системой. На живой базе это давало две беды сразу, и обе росли
    с каждым оборотом маршрута:

    * ссылка «[[US-3.6.6 Получение сальдо…]]» заводила карточку с кодом документа в имени,
      и линтер справедливо звал её артефактом. Три такие появились за один вечер, а всего
      в отчёте их набралось 68 — база «портилась» ровно на своём росте;
    * пробелы и подчёркивания оставались как есть, и понятие получало файл, который
      сборка карточки потом не воспроизводила: рядом заводился двойник.

    Код документа при этом терять нельзя: он и исходное написание ссылки уходят в
    синонимы, иначе заготовка рождается уже битой — ссылка на неё не сойдётся.
    """
    root = make_project(tmp)
    kb = root / "AuroraKnowledgeDB"
    (kb / "Concepts").mkdir(parents=True, exist_ok=True)
    (kb / "Concepts" / "Приём-начислений.md").write_text(
        '---\ntitle: "Приём начислений"\naliases: []\ntype: concept\n'
        'status: knowledge\nkind: knowledge\n---\n\n# Приём начислений\n\n'
        'См. [[US-3.6.6 Получение сальдо по Заявителям]] и [[ALG-082_Выбор_профиля]].\n',
        encoding="utf-8")

    cp = subprocess.run([sys.executable, str(KIT / "scripts/kb_fix.py"), "--stubs", "--apply"],
                        cwd=root, capture_output=True, text=True)
    assert cp.returncode == 0, f"заготовки не завелись:\n{cp.stdout[-400:]}{cp.stderr[-400:]}"

    made = {p.name for p in (kb / "Concepts").glob("*.md")}
    assert "Получение-сальдо-по-Заявителям.md" in made, (
        f"заготовка названа по тексту ссылки, вместе с кодом документа и пробелами: {made}")
    assert "ALG-082-Выбор-профиля.md" in made, \
        f"подчёркивание в имени заготовки не сведено к дефису: {made}"
    assert not any(" " in n for n in made), f"в именах заготовок остались пробелы: {made}"

    # Ссылка обязана вести в заготовку: исходное написание и код — в синонимах.
    head = (kb / "Concepts" / "Получение-сальдо-по-Заявителям.md").read_text(encoding="utf-8")
    assert "US-3.6.6" in head, "код документа потерян — ссылка по нему никуда не приведёт"
    lint = subprocess.run([sys.executable, str(KIT / "scripts/kb_lint.py"), "--summary"],
                          cwd=root, capture_output=True, text=True)
    assert "ошибок 0" in lint.stdout, (
        "заготовка родилась битой: ссылка, ради которой её завели, до неё не доходит\n"
        + lint.stdout[-400:])


@test
def test_one_rule_turns_a_title_into_a_file_name(tmp: Path):
    """Имя файла карточки считает одна функция, и точка в коде — не разделитель.

    С живой базы, 3234 карточки:

    * `card_filename` меняла точку на дефис («US-3.6.14» → «US-3-6-14»), а ссылки в базе
      пишут код с точкой. Совпадений — 113 карточек с точкой против 4 с дефисом, и на эти
      четыре вели десятки битых ссылок: `[[ALG-3.14-…]]` не находило `ALG-3-14-…`.
    * подчёркивание не приводилось к дефису ни одним из путей, а имена приходят и с ним
      (из выгрузок), и с пробелом (из названий). Один объект получал два файла: нашлось
      20 таких пар, включая `ALG-082_Выбор_профиля` рядом с `ALG-082-Выбор-профиля`.
    * `kb_fix --names` считал имя своей регуляркой вместо этой функции и расходился с ней
      по подчёркиванию: ремонт переименовывал карточку в форму, которую сборка не
      воспроизводила, — следующий разбор того же источника заводил двойника.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    ac = importlib.import_module("aurora_common")
    importlib.reload(ac)

    assert ac.card_filename("US-3.6.14 Просмотр журнала") == "US-3.6.14-Просмотр-журнала", \
        "точка в составном коде — часть имени, а не разделитель: ссылки пишут её"
    assert ac.card_filename("ALG-3.7 Обеспечение платежа") == "ALG-3.7-Обеспечение-платежа"

    same = "ALG-082-Выбор-профиля"
    for variant in ("ALG-082_Выбор_профиля", "ALG-082 Выбор профиля", "ALG-082—Выбор,профиля"):
        assert ac.card_filename(variant) == same, (
            f"«{variant}» даёт другое имя файла, чем «{same}» — один объект получит "
            "два файла, и это ровно то, как в базе завелись двадцать пар двойников")

    # Ремонт имён обязан считать имя тем же правилом, что и сборка.
    kf = importlib.import_module("kb_fix")
    importlib.reload(kf)
    assert kf.normalize_title is ac.card_filename, \
        "kb_fix считает имя не тем же правилом, что сборка карточки"

    assert ac.card_filename("Курс валют ЦБ (сервис)") == "Курс-валют-ЦБ-сервис", \
        "скобки и лишние дефисы схлопываются, как было"
    assert ac.card_filename("  «Заявка»  ") == "Заявка", "кавычки и края снимаются"


@test
def test_resuming_a_route_continues_instead_of_starting_over(tmp: Path):
    """Продолжение маршрута обязано быть продолжением, а не вторым первым прогоном.

    С живого прогона: маршрут встал после четырнадцати шагов, человек нажал «Продолжить»
    — и увидел «шаг 1 из 20» на чистой консоли. Работа-то не повторялась (пропуск
    работал), но по всем признакам на экране это выглядело как «всё началось заново»,
    и доверия к кнопке не осталось.

    Три причины, все на стороне панели:

    1. Ветка пропуска возвращалась ДО `ROUTE.done++`, поэтому счётчик считал только
       выполненные шаги: семь пропущенных — и первый настоящий шаг объявлялся первым.
    2. Начало маршрута чистило `#consoleOut` безусловно, вместе с выводом прошлой попытки.
    3. Сигнатуры сделанного брались только из `events.jsonl` последней попытки, а он у
       каждой попытки свой — на третьей попытке работа первой считалась несделанной.
    """
    ui = panel_sources()

    skip = ui[ui.index("if (SKIP_SIGS && !st.cycle && SKIP_SIGS.has(sig)){"):]
    skip = skip[:skip.index("return {rc:0, skipped:true")]
    assert "ROUTE.done++" in skip, (
        "пропущенный шаг не увеличивает счётчик — продолжение после семи сделанных шагов "
        "покажет «шаг 1 из N», и человек прочтёт это как «началось заново»")
    assert 't("route.skip_step", {step: ROUTE.done, of: ROUTE.total' in skip, \
        "строка пропуска не называет номер шага: непонятно, сколько уже позади"

    # Смотрим только маршрут: у одиночной команды и у подключения к прогону очистка
    # консоли уместна — там начинается новый вывод, а не продолжается прежний.
    body = ui[ui.index("async function runRoute("):]
    body = body[:body.index("\nasync function ")] if "\nasync function " in body else body
    assert 'const out = $("#consoleOut"); out.innerHTML = "";' not in body, (
        "маршрут стирает консоль безусловно — продолжение уносит вывод прошлой попытки, "
        "ради которого кнопку и нажимают")
    assert "if (resume) out.append" in body and "else out.innerHTML" in body, \
        "нет ветки «продолжение не стирает вывод»"

    assert "const CARRIED = new Set(SKIP_SIGS || [])" in ui, \
        "сделанное не переносится между попытками"
    assert "new Set(last.done || [])" in ui, (
        "продолжение читает только events.jsonl последней попытки — работа первой "
        "попытки на третьей будет сделана заново")
    assert "done:[...CARRIED" in ui, \
        "накопленное не сохраняется в состоянии маршрута и не переживёт перезапуск панели"


@test
def test_run_archive_shows_the_newest_first_and_caps_the_list(tmp: Path):
    """Архив прогонов: свежие сверху, в панели последние RUNS_SHOW, доступ — ко всем.

    Сравнивают обычно последний прогон с предыдущим. При старых сверху оба оказывались
    в конце списка из полусотни, и до них надо было доскроллить.

    Ограничение — на ПОКАЗ, а не на хранение: файлы лежат до RUNS_KEEP, и прогон,
    уехавший за границу показа, обязан открываться по id. Иначе «убрали из списка»
    незаметно превратится в «потеряли».
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = make_project(tmp)
    runs = Path(ck.runs_dir(str(root)))
    made = []
    for i in range(ck.RUNS_SHOW + 12):          # заведомо больше, чем показываем
        rid = f"202608{10 + i // 24:02d}-{i % 24:02d}0000-{i:04x}"
        (runs / rid).mkdir(parents=True)
        (runs / rid / "console.log").write_text(f"прогон {i}\n", encoding="utf-8")
        made.append(rid)

    shown = ck.run_archive(str(root), limit=ck.RUNS_SHOW)
    ids = [r["id"] for r in shown]
    assert len(ids) == ck.RUNS_SHOW, \
        f"в панель ушло {len(ids)} прогонов вместо {ck.RUNS_SHOW}"
    assert ids == sorted(ids, reverse=True), \
        f"порядок не от свежего к старому: {ids[:3]} …"
    assert ids[0] == max(made), \
        f"сверху не самый свежий прогон: {ids[0]}, а самый свежий {max(made)}"

    whole = ck.run_archive(str(root))
    assert len(whole) == len(made), \
        "без limit архив обязан отдавать всё: по нему проверяется доступ к логу"

    # Прогон за границей показа читается: он выпал из списка, но не с диска.
    hidden = sorted(made, reverse=True)[ck.RUNS_SHOW + 2]
    assert hidden not in ids, "проверяем именно тот, что не показан"
    got = ck.read_run_console(str(root), hidden)
    assert got.get("text", "").strip().startswith("прогон"), (
        f"старый прогон не открывается по id — ограничение показа съело доступ: {got}")

    assert ck.RUNS_SHOW <= ck.RUNS_KEEP, \
        "показываем больше, чем храним: часть строк списка вела бы в никуда"


@test
def test_run_archive_keeps_the_full_console_history(tmp: Path):
    """Полный вывод прогона живёт на диске: после перезапуска можно сравнить старый и новый.

    Живой буфер процесса пропадает вместе с процессом — панель однажды перезапускали
    во время ночного разбора, и вывод потерялся. Теперь у каждого прогона папка
    `.opencode/runs/<id>/` с полным console.log и events.jsonl по шагам, архив не
    растёт вечно, а id прогона, рождённый в браузере, не становится чужим путём.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = tmp / "проект"
    root.mkdir()
    assert ck.runs_dir(str(root)) == os.path.join(str(root), ".opencode", "runs")
    assert ck.run_archive(str(root)) == [], "архива нет — список пустой, а не ошибка"

    base = ck.runs_dir(str(root))
    for rid in ("20260829-120000-aaaaaa", "20260829-110000-bbbbbb"):
        d = os.path.join(base, rid)
        os.makedirs(d)
        with open(os.path.join(d, "console.log"), "w", encoding="utf-8") as f:
            f.write("вывод прогона " + rid + "\n")
    arch = ck.run_archive(str(root))
    # Порядок обратный — свежие сверху. Раньше здесь ждали прямой хронологии; требование
    # изменилось: сравнивают последний прогон с предыдущим, и оба должны быть на виду,
    # а не в конце списка из полусотни.
    assert [a["id"] for a in arch] == sorted(
        ["20260829-120000-aaaaaa", "20260829-110000-bbbbbb"], reverse=True), \
        f"архив не от свежего к старому: {arch}"
    # Путь сверяем с id самой записи, а не с позицией в списке: позиция зависит от
    # порядка сортировки, а связь «id ↔ его файл» — нет.
    for a in arch:
        assert a["path"].endswith(os.path.join(a["id"], "console.log")), \
            f"путь записи не ведёт к её же console.log: {a}"
    got = ck.read_run_console(str(root), "20260829-120000-aaaaaa")
    assert got.get("text", "").startswith("вывод прогона 20260829-120000"), \
        f"архивный прогон не читается: {got}"
    assert "не найден" in ck.read_run_console(str(root), "нет-такого")["error"], \
        "несуществующий прогон — исключение вместо ошибки"
    # id рождается в браузере и попадает в путь — безопасен только basename
    assert "не найден" in ck.read_run_console(str(root), "../чужой/проект")["error"], \
        "id прогона из браузера стал чужим путём"

    # Архив не растёт вечно: оставляем последние RUNS_KEEP прогонов
    for i in range(ck.RUNS_KEEP + 5):
        os.makedirs(os.path.join(base, f"20260101-000000-{i:06d}"))
    ck.trim_runs(str(root))
    left = sorted(os.listdir(base))
    assert len(left) == ck.RUNS_KEEP, f"trim оставил {len(left)} прогонов вместо {ck.RUNS_KEEP}"
    assert "20260101-000000-000000" not in left, "старейший прогон не удалён"
    assert f"20260101-000000-{ck.RUNS_KEEP + 4:06d}" in left, "свежий прогон удалён"

    # Каждый прогон получает id, а stdout пишется на диск рядом с живым буфером
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    at = src.index("def start_job(")
    sj = src[at:at + 4000]
    assert 'utc_slug() + "-" + job_id[:6]' in sj, \
        "у прогона нет id в UTC — архив не соберётся в хронологию"
    assert '"run_id": run_id' in sj, "задание не помнит id своего архива"
    assert 'os.path.join(runs_dir(project), run_id)' in sj and '"console.log"' in sj, \
        "вывод прогона не пишется на диск"
    assert "run_log.write(line)" in sj and "run_log.flush()" in sj, \
        "вывод уходит на диск порциями — архив пуст до конца прогона"
    assert "trim_runs(project)" in sj, "архив растёт без ограничения"

    # id из браузера — имя папки: оба маршрута проверяют его
    assert src.count('"/" in run_id or run_id.startswith("..")') >= 2, \
        "id прогона не проверен на обоих маршрутах"
    at = src.index('\n        elif u.path == "/api/run/steps":')
    block = src[at:at + 1700]
    assert '"steps": events' in block and "json.loads(line)" in block, \
        "события шагов не читаются для продолжения маршрута"
    at = src.index('\n        if u.path == "/api/run/steps":')
    block = src[at:at + 1400]
    assert "events.jsonl" in block and "json.dumps(step, ensure_ascii=False)" in block, \
        "шаги маршрута не сохраняются строкой на шаг"
    assert '"/api/run/logs"' in src and '"/api/run/file"' in src, "нет маршрутов архива"

    ui = panel_sources()
    for token in ("function renderArchiveBox(", "function archiveWhen(", "function rtime(",
                  "function fmtDur(", 'id="exportMd"', "Продолжить маршрут",
                  "const SILENCE_MS = 120000;", '"/api/run/logs?project="',
                  '"/api/run/file?project="', '"/api/run/steps"'):
        assert token in ui, f"консоль потеряла: {token}"
    assert "Date.now() - lastLineAt > SILENCE_MS" in ui, "шаг молчит без предупреждения"
    assert "Date.now() - POLL_LAST_OUT > SILENCE_MS" in ui, \
        "одиночный прогон молчит без предупреждения"
    assert "началось в" in ui and "предыдущий шаг занял" in ui, \
        "у шага нет времени начала и цены предыдущего"



@test
def test_route_run_archive_opens_from_events(tmp: Path):
    """Прогон маршрута открывается из «Консоли»: console.log у него нет, архив — events.jsonl.

    Вечерний маршрут на живом проекте молчал в панели: запись в списке архивов есть,
    раскрытие — «архив прогона не найден». Маршрут не единый процесс, и единственное,
    что он архивирует, — журнал шагов; именно он и должен показываться при раскрытии.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = tmp / "проект"
    root.mkdir()
    base = Path(ck.runs_dir(str(root)))

    rid = "20260926-153334-route"
    (base / rid).mkdir(parents=True)
    steps = [
        {"cmd": "sync:confluence", "args": ["--prune"],
         "start": "2026-09-26T12:33:34.264Z", "end": "2026-09-26T12:35:34.676Z",
         "duration_s": 120, "rc": 0},
        {"cmd": "kb:lint", "args": [],
         "start": "2026-09-26T13:01:43.334Z", "end": "2026-09-26T13:02:02.031Z",
         "duration_s": 19, "rc": 1},
    ]
    (base / rid / "events.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in steps) + "\nбитая строка\n",
        encoding="utf-8")

    assert ck.run_archive(str(root))[0]["id"] == rid, "прогон маршрута не виден в архиве"
    got = ck.read_run_console(str(root), rid)
    text = got.get("text", "")
    assert "sync:confluence" in text and "--prune" in text, f"шага не видно: {got}"
    assert "kb:lint" in text and "kb:lint=1" in text, "сбойный шаг не помечен rc"
    assert "битая" not in text, "битая строка журнала выглянула в текст"
    assert "не найден" not in text, f"раскрытие маршрута по-прежнему «не найдено»: {got}"

    # пустой журнал — тоже не «не найден»: архив был, просто работать маршрут не успел
    (base / "20260926-160000-route").mkdir()
    (base / "20260926-160000-route" / "events.jsonl").write_text("", encoding="utf-8")
    empty = ck.read_run_console(str(root), "20260926-160000-route")
    assert empty.get("text") and "не найден" not in empty.get("error", ""), \
        f"маршрут без шагов надо объяснить, а не «не найден»: {empty}"

    # папки без журнала и чужой путь из браузера остаются «не найден»
    assert "не найден" in ck.read_run_console(str(root), "20260101-000000-route")["error"]
    assert "не найден" in ck.read_run_console(str(root), "../чужой")["error"], \
        "id прогона из браузера стал чужим путём"


@test
def test_a_stalled_route_is_an_stop_not_a_pass(tmp: Path):
    """Застой цикла — остановка, а не проход: «Продолжить маршрут» появляется и там.

    Когда за оборот не убыло ничего, маршрут вставал, но конец читался как «пройден»:
    кнопки продолжения не было, и недоделанная работа запускалась с начала. Теперь застой
    ставит ROUTE.stalled, баннер его честно называет, а продолжение пропускает только шаги
    с кодом ровно 0 — код 1 («отработала и нашла, что чинить») повторять надо.
    """
    ui = panel_sources()
    assert "ROUTE.stalled = true" in ui, \
        "застой не помечен флагом — не отличить его от прохода и не дать «Продолжить»"
    assert "ROUTE.failed || ROUTE.stalled" in ui, \
        "застой не считается остановкой: bad решает по одному ROUTE.failed"
    assert "застой" in ui, "в интерфейсе нет «застой» — баннер не честен о причине"
    assert "if (st.rc===0||st.rc===1) sigs.add" not in ui, \
        "продолжение до сих пор пропускает шаги с кодом 1 — они «нашли, что чинить», а не прошли"
    assert "if (st.rc===0) sigs.add" in ui, \
        "продолжение собирает сигнатуры не только с успешных шагов"
    assert "Продолжить маршрут" in ui, "кнопку продолжения потеряли совсем"


@test
def test_a_stopped_route_survives_a_panel_restart(tmp: Path):
    """Остановленный маршрут переживает перезапуск панели: «Продолжить маршрут» после него.

    Состояние последнего остановленного маршрута лежит в `.opencode/state/last_route.json`,
    а не только в памяти вкладки: консоль читает его на загрузке и поднимает кнопку. Конец
    маршрута его пишет (застой/отказ/ручная остановка), полный проход — стирает. Битый файл —
    None, а не исключение: чужой обрывок не должен ронять панель.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = tmp / "проект"
    root.mkdir()
    assert ck.route_state_path(str(root)) == \
        os.path.join(str(root), ".opencode", "state", "last_route.json")
    assert ck.read_route_state(str(root)) is None, \
        "файла нет — состояние читается как объект вместо None"

    wrote = ck.write_route_state(str(root), {"scId": "update", "title": "Обновить базу",
                                           "at": "2026-08-30T04:12:00"})
    assert wrote.get("ok") is True, f"запись состояния не удалась: {wrote}"
    path = root / ".opencode/state/last_route.json"
    assert path.exists(), "файл последнего маршрута не появился"
    state = ck.read_route_state(str(root))
    assert state and state.get("scId") == "update" and state.get("title") == "Обновить базу", \
        f"состояние не пережило запись->чтение: {state}"
    assert state.get("at") == "2026-08-30T04:12:00", "метка времени не сохранилась"

    assert ck.clear_route_state(str(root)).get("ok") is True
    assert ck.read_route_state(str(root)) is None, "после очистки состояние осталось"
    assert not path.exists(), "файл состояния не удалён"
    assert ck.clear_route_state(str(root)).get("ok") is True, \
        "повторная очистка без файла — ошибка вместо «нечего чистить»"

    path.write_text("{ не json", encoding="utf-8")
    assert ck.read_route_state(str(root)) is None, \
        "битый файл состояния — исключение вместо None"

    # Источник: эндпоинт есть и на чтение, и на запись; консоль пишет и читает состояние
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert src.count('"/api/route/state"') >= 2, \
        "эндпоинт состояния маршрута только с одной стороны (нужны GET и POST)"
    assert "last_route.json" in src, "эндпоинт не знает имени файла состояния"
    ui = panel_sources()
    assert '"/api/route/state?project="' in ui, \
        "вкладка «Консоль» не читает состояние остановленного маршрута при загрузке"
    assert '"/api/route/state", {method:"POST"' in ui, \
        "конец маршрута не пишет состояние в проект"
    assert "остановлен в" in ui, \
        "на кнопке после перезапуска нет времени и причины остановки"

@test
def test_a_stalled_route_stops_honestly_and_resume_skips_only_success(tmp: Path):
    """Застой останавливает маршрут честно, а продолжение повторяет только успешное.

    Оборот, не убавивший работы, — это не «пройдено»: остановка по застою (stall) отделена
    от отказа (`ROUTE.stalled`), ей полагается жёлтая плашка с честным текстом и кнопка
    «Продолжить маршрут». Продолжение накапливает сигнатуры шагов, завершившихся ровно
    кодом 0: код 1 («нашла, что чинить») не сигнатура успеха и обязан повторяться. Ручная
    остановка и остановка по застою определяют причину в одной ветке; стоп цикла по
    требованию выставляет оба флага разом.
    """
    ui = panel_sources()

    assert "ROUTE.stalled = true;" in ui, \
        "флаг застоя не ставится — по нему отличается «застой» от «пройден»"
    assert "const bad = ROUTE.failed || ROUTE.stalled;" in ui, \
        "решение «остановить» не учитывает застой — bad решает по одному ROUTE.failed"
    assert "остановлен: застой — работа не убывает" in ui, \
        "нет честной жёлтой плашки о причине застоя"
    assert 'className = "chip warn"' in ui, \
        "плашка застоя не жёлтая — выглядит как успех"
    assert "if (bad && S.lastRoute && S.project){" in ui, \
        "кнопка «Продолжить маршрут» не связана с состоянием bad"

    assert "if (st.rc===0) sigs.add" in ui, \
        "продолжение не собирает сигнатуры только с успешных шагов"
    assert "if (st.rc===0||st.rc===1) sigs.add" not in ui, \
        "продолжение снова пропускает rc 1 — а их повторять надо"

    assert 'ROUTE.stopped ? "stopped"' in ui, \
        "причина остановки в кнопке не читает код остановки по застою"
    assert "ROUTE.stopped = st.cmd; ROUTE.failed = st.cmd" in ui, \
        "стоп цикла по требованию не выставляет оба флага остановки"


@test
def test_a_route_waits_for_the_network_like_the_engine(tmp: Path):
    """Маршрут ждёт сеть ровно по признакам движка, а не глотает обрыв.

    Признаки офлайна в панели — буквальная копия списка `agent_runner`: обе стороны
    отличают «упала связь» от «источник молчит». Паритет проверяем как инвариант равенства
    множеств: изменение списка на любой стороне — расхождение, которое обязано дойти до
    человека. Дальше — протокол ожидания: шаг с кодом 1 и офлайн-текстом не роняет
    маршрут, а ставит его «ждёт сеть», накопительно до лимита попыток, и позволяет
    выйти из ожидания руками («Попробовать сейчас» / «не ждать»).
    """
    engine = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    ui = panel_sources()

    m = re.search(r"OFFLINE_SIGNS\s*=\s*[\[(](.*?)[\])]", engine, re.S)
    assert m, "в agent_runner не найден список OFFLINE_SIGNS"
    engine_signs = set(re.findall(r"[\"']([^\"']+)[\"']", m.group(1)))

    start = ui.index("const OFFLINE_SIGNS = [") + len("const OFFLINE_SIGNS = [")
    end = ui.index("];", start)
    ui_block = ui[start:end]
    ui_signs = set(re.findall(r"[\"']([^\"']+)[\"']", ui_block))

    assert engine_signs == ui_signs, \
        "офлайн-признаки панели и движка разошлись: в панели лишние " \
        + repr(sorted(ui_signs - engine_signs)) + ", не хватает " \
        + repr(sorted(engine_signs - ui_signs))

    assert "const ROUTE_OFFLINE_RETRY_MS = 15 * 60 * 1000;" in ui, \
        "нет паузы ожидания сети (15 минут)"
    assert "const ROUTE_OFFLINE_TRIES = 8;" in ui, \
        "нет лимита попыток ожидания сети"
    assert "const looksOffline" in ui, "нет проверки текста на офлайн"
    # Все вызовы передают вывод шага массивом строк (буфер runStep), а не строкой: проверка
    # обязана принимать и то и другое. (text||"").toLowerCase() на массиве — TypeError, и
    # первый шаг с кодом 1 убивает весь маршрут без единой строки в консоли («Починить
    # базу» останавливался на 1/13, 2026-08-30).
    m_lo = re.search(r"const looksOffline\s*=\s*text\s*=>\s*\{(.*?)\};", ui, re.S)
    assert m_lo, "не найдена реализация looksOffline"
    assert "Array.isArray" in m_lo.group(1) and ".join(" in m_lo.group(1), "looksOffline не принимает массив строк: маршрут гибнет на первом шаге с кодом 1"
    assert "const waitNetworkCycle" in ui, "нет цикла ожидания сети"
    assert "ждёт сеть (попытка" in ui, "нет текста о состоянии ожидания сети"
    assert "Перестал ждать сеть:" in ui, "нет текста о потолке ожидания"
    assert ui.count("Попробовать сейчас") >= 2, \
        "кнопка «Попробовать сейчас» не во всех ветках ожидания (живой цикл, потолок, догон)"
    assert "не ждать" in ui, "нет кнопки выйти из ожидания без сети"

    assert "attempts: cy.attempt" in ui and "nextRetryAt: cy.nextRetryAt" in ui, \
        "не сохраняются попытка и время следующего повтора для перезапуска"
    assert "if (state.reason === \"offline\")" in ui, \
        "догон остановленного на сети маршрута не распознаёт причину offline"
    assert "showOfflineResume(state)" in ui, "нет продолжения после возвращения сети"
    assert "attempts: last.attempts" in ui, \
        "продолжение не переносит счётчик попыток из сохранённого состояния"


@test
def test_a_failed_command_can_be_retried_as_the_next_attempt(tmp: Path):
    """Упавшая (код ≥ 2) команда получает «Попробовать снова» как следующую попытку.

    `fire` запоминает последний шаг (`S.lastStep`) с полем `failed`; провал пишется только
    для своей же попытки и только при коде ≥ 2 — прерванная команда (отрицательный код) и
    код 1 (не ошибка, а «нашли, что чинить») кнопки не получают. Повтор — это снова тот же
    id, шаг нумеруется `n+1`, метка «попытка N» в обеих точках, команда уходит тем же
    `cmd`/`args`. Дополнено инвариантом хранения: файл состояния маршрута лежит в общей
    папке проекта, рядом с UI-тестами, а не разъехался.
    """
    ui = panel_sources()

    assert "S.lastStep = {cmd:r.cmd, args, n, failed:false, job:res.job};" in ui, \
        "fire не запоминает шаг попытки с полем failed"
    assert ui.count('t("run.attempt", {n})') >= 2, \
        "метка номера попытки не в обеих точках (fire и retryStep)"
    assert "prev.failed && prev.cmd === r.cmd" in ui, \
        "счёт попыток не привязан к тому же шагу с прошлого провала"
    assert "S.lastStep.failed = (rc >= 2);" in ui, \
        "провал ставится не по коду >= 2"
    assert "if (S.lastStep && S.lastStep.job === id){" in ui, \
        "провал пишется не под защитой «это наша попытка»"
    assert 'if (rc >= 2) $("#consoleApply").append(el("button",{class:"btn sm gold",' in ui, \
        "кнопка повтора появляется не ровно на коде >= 2"
    assert "Попробовать снова" in ui, "нет кнопки «Попробовать снова»"
    assert "const n = st.n + 1;" in ui, "повтор не увеличивает номер попытки"
    assert "cmd:st.cmd, args:st.args" in ui, \
        "повтор не шлёт ту же команду теми же аргументами"

    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    root = tmp / "проект"
    root.mkdir()
    assert ck.route_state_path(str(root)) == \
        os.path.join(str(root), ".opencode", "state", "last_route.json"), \
        "файл состояния маршрута уехал из общей папки AuroraKnowledgeDB/meta/"


@test
def test_file_tree_is_a_tree_and_says_what_it_hides(tmp: Path):
    """Раздел «Файлы» разбирался критиком на живом проекте: 3882 файла, 89 000 пикселей
    прокрутки, имена в капсе, полторы тысячи из четырёх без единого слова об этом.

    Дерево обязано быть деревом: девять папок верхнего уровня вместо плоского списка
    полных путей. Свёрнутый список из 382 путей — та же стена, только другой формы.
    """
    ui = panel_sources()
    assert "function treeOf(" in ui and "node.dirs" in ui, \
        "дерево осталось плоским списком путей"
    assert "SHOW_LIMIT" in ui and "files.shown" in ui, \
        "обрезка списка проходит молча — он выглядит полным"
    assert '#fileTree button{text-transform:none' in ui, \
        "скин красит имена файлов: имя — данные, а не интерфейс"
    assert 'b.setAttribute("aria-current", "true")' in ui, \
        "открытый файл в дереве не отмечен — человек теряет место"
    assert 'mark.scrollIntoView' in ui, "к открытому файлу не подводится прокрутка"
    assert "FILTERS" in ui and '"изменённые"' in ui, \
        "нет быстрых фильтров: «что я трогал сегодня» приходится искать глазами"
    # `dirty` уже занят признаком несохранённых правок редактора: второе значение под
    # тем же именем превратило множество в булево и уронило дерево на первом файле.
    assert "F.changed" in ui and "F.dirty.has" not in ui, \
        "множество изменённых файлов названо тем же именем, что признак правок редактора"
    assert "git.unknown" in ui, \
        "«не смогли спросить» показывается как «проект не под git»"

    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    root = tmp / "proj"
    for d in (".ruff_cache", ".claude", "Workspaces", "AuroraKnowledgeDB/meta"):
        (root / d).mkdir(parents=True)
    (root / ".ruff_cache" / "мусор.md").write_text("x", encoding="utf-8")
    (root / "Workspaces" / "своё.md").write_text("x", encoding="utf-8")
    tree = ck.file_tree(str(root))
    dirs = {f["dir"].split("/")[0] for f in tree["files"]}
    assert not any(d.startswith(".") for d in dirs), \
        f"папки инструментов в дереве проекта: {sorted(dirs)}"
    assert tree.get("create_dirs"), "панель не знает, где можно заводить файлы"


@test
def test_creating_and_removing_files_respects_the_structure(tmp: Path):
    """Панель управления файлами обещала создание — и не умела его.

    За этим человек шёл в системный проводник, то есть ровно туда, откуда мы его уводили.
    Но структура папок фиксирована движком: «создать» в `Sources/Confluence` означало бы
    файл, который сотрёт следующий синк, а удаление из базы знаний нарушает инвариант 2.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = tmp / "proj"
    for d in ("Workspaces", "Sources/Confluence", "AuroraKnowledgeDB/Concepts",
              "AuroraKnowledgeDB/meta"):
        (root / d).mkdir(parents=True)
    (root / "AuroraKnowledgeDB" / "Concepts" / "К.md").write_text(
        "---\ntype: concept\nstatus: knowledge\n---\n\n# К\n", encoding="utf-8")

    made = ck.file_create(str(root), "Workspaces/проба")
    assert made.get("path") == "Workspaces/проба.md", f"не создан: {made}"
    assert ck.file_create(str(root), "Workspaces/проба.md").get("error"), \
        "повторное создание молча затирает существующий файл"
    assert ck.file_create(str(root), "Sources/Confluence/x.md").get("error"), \
        "файл заведён в зеркале — его сотрёт следующий синк"
    assert ck.file_create(str(root), "в-корне.md").get("error"), \
        "файл заведён в корне: структура папок фиксирована движком"
    for bad in ("../снаружи.md", "Workspaces/../../побег.md", "Workspaces/../Sources/x.md"):
        assert ck.file_create(str(root), bad).get("error"), f"путь наружу принят: {bad}"
    assert not (tmp / "снаружи.md").exists() and not (tmp / "побег.md").exists()

    ren = ck.file_rename(str(root), "Workspaces/проба.md", "другое")
    assert ren.get("path") == "Workspaces/другое.md", f"не переименован: {ren}"
    assert ren.get("note"), \
        "не сказано, что ссылки на прежнее имя стали битыми — карточка выпадет из базы молча"
    assert ck.file_rename(str(root), "Workspaces/другое.md", "../побег")["path"] \
        .startswith("Workspaces/"), "переименованием можно вынести файл из папки"

    assert ck.file_delete(str(root), "AuroraKnowledgeDB/Concepts/К.md").get("error"), \
        "карточку удалили из базы: устаревшее заменяют, а не стирают (инвариант 2)"
    assert (root / "AuroraKnowledgeDB" / "Concepts" / "К.md").is_file()
    assert ck.file_delete(str(root), "Workspaces/побег.md").get("ok"), "черновик не удаляется"

    # Недавние живут в проекте, а не в браузере: список у команды один.
    ck.recent(str(root), "Workspaces/нет-такого.md")
    assert "Workspaces/нет-такого.md" not in ck.recent(str(root)), \
        "в недавних остаются исчезнувшие файлы"
    assert (root / "AuroraKnowledgeDB" / "meta" / "recent-files.json").is_file(), \
        "недавние хранятся в браузере — второй аналитик их не увидит"


@test
def test_reports_keep_their_previous_versions(tmp: Path):
    """Отчёт собирается в один и тот же файл, и каждая сборка затирает прежний.

    Ошибка в выгрузке или в ростере — и вместо рабочего отчёта остаётся испорченный, а
    сравнить показатели с прошлой неделей уже не с чем. Копия делается при взгляде на
    вкладку, а не по нажатию кнопки: отчёт собирают и маршрутом, и из терминала.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = tmp / "proj"
    (root / "Artifacts" / "reports").mkdir(parents=True)
    out = root / "Artifacts" / "reports" / "r.html"
    out.write_text("<h1>первый</h1>", encoding="utf-8")
    first = ck.keep_version(str(root), "analyst", str(out))
    assert len(first) == 1, "первая сборка не сохранилась"

    assert len(ck.keep_version(str(root), "analyst", str(out))) == 1, \
        "тот же файл сохраняется снова и снова — история станет мусором"

    out.write_text("<h1>второй</h1>", encoding="utf-8")
    os.utime(out, (time.time() + 120, time.time() + 120))
    two = ck.keep_version(str(root), "analyst", str(out))
    assert len(two) == 2, "новая сборка не попала в историю"
    assert two[0]["stamp"] > two[1]["stamp"], "свежие версии не сверху"

    # Имя версии приходит из браузера: подставить в него путь стоит недорого.
    for bad in ("../../../etc/passwd", "..%2Fx", "/etc/passwd", ""):
        assert not ck.report_version_path(str(root), "analyst", bad), \
            f"именем версии можно вытащить чужой файл: {bad!r}"

    gone = ck.forget_version(str(root), "analyst", two[0]["stamp"])
    assert gone.get("ok") and gone["left"] == 1, f"версия не удалилась: {gone}"
    assert ck.forget_version(str(root), "analyst", two[0]["stamp"]).get("error"), \
        "удаление несуществующей версии проходит молча"

    # Отсутствие отчёта — не повод падать: вкладку открывают и на пустом проекте.
    out.unlink()
    assert ck.keep_version(str(root), "analyst", str(out)) == ck.versions(str(root), "analyst")

    ui = panel_sources()
    assert "Прежние версии" in ui and "/api/report/forget" in ui, \
        "историю негде посмотреть и нечем почистить"
    assert "stamp=" in ui, "старую версию нельзя открыть"
    assert "Вернуть её будет неоткуда" in ui, \
        "удаление версии без предупреждения — необратимая потеря по одному нажатию"
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"history": keep_version(' in src, \
        "история собирается не для каждого отчёта вкладки, а для одного"


@test
def test_panel_admits_it_is_running_old_code(tmp: Path):
    """Разметка отдаётся с диска свежая, а процесс отвечает старым кодом.

    Обновили кит, не перезапустив панель — на экране новые кнопки, а API под ними нет.
    Человек нажимает и получает «неизвестный маршрут»: он ищет поломку в себе, хотя
    достаточно перезапуска. Предупреждение об этом было написано — и не срабатывало
    ни разу: сервер клал признак рядом с `ui`, а панель читала его внутри `ui`.
    """
    src = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    block = src[src.index('"ui": {'):src.index('"projects": projects,')]
    assert "stale_process" in block, \
        "признак «процесс старее файлов» лежит не там, где его читает панель"
    ui = panel_sources()
    assert "S.state.ui.stale_process" in ui, "панель перестала проверять устаревший процесс"
    assert 'self.send_header("Cache-Control", "no-store")' in src, \
        "страница кэшируется: обновление кита останется невидимым до очистки кэша"

    # Режим, о котором знает только подсказка при наведении, не существует.
    assert "можно написать «авто»" in ui, "про «авто» сказано только во всплывающей подсказке"
    assert "Замерить шлюзы" in ui, "измерить ширину можно только из терминала"


@test
def test_route_works_until_the_work_is_done_and_saves_each_lap(tmp: Path):
    """Маршрут идёт, пока есть работа, и фиксирует каждый оборот.

    Считались только источники: они кончались, цикл завершался, и маршрут отчитывался
    «пройден» при восьмистах карточках без единого тезиса. «База знает всё, что появилось
    в источниках» — это про знание, а не про разбор.

    И фиксация: прогон идёт часами, человек вправе выключить его в любую минуту. Без
    коммита прерванная работа осталась бы незафиксированной, а двенадцать команд движка
    не работают по грязному дереву — следующий запуск встал бы на первом шаге.
    """
    ui = panel_sources()
    at = ui.index("const leftByKind = lines =>")
    fn = ui[at:at + 700]
    assert "Источников в плане" in fn and "осталось:" in fn, \
        "остаток считается по одному виду работы — маршрут закончится раньше работы"
    # Виды работы считаются РАЗДЕЛЬНО. Сложенные в один максимум, они врали: разбор
    # добавляет карточки, переосмысление их разбирает, суммарный остаток стоит — и цикл
    # объявлял гонку застоем. На живой базе он так и встал на 814.
    assert 'out["источники"]' in fn and 'out["карточки"]' in fn, \
        "остатки разных видов работы слиты в одно число"
    cycle0 = ui[ui.index("for (ROUTE.lap = 1"):ui.index("ROUTE.lap = 0;")]
    assert "const moved = names.filter" in cycle0, \
        "цикл встаёт, когда не убыл общий остаток, а не когда не сдвинулось ничего"
    assert 't("route.grew"' in cycle0, \
        "гонка разбора с переосмыслением не названа человеку числами"

    cycle = ui[ui.index("for (ROUTE.lap = 1"):ui.index("ROUTE.lap = 0;")]
    assert '"/api/git/commit"' in cycle, "оборот не фиксируется — прерванный прогон пропадёт"
    assert "skip_ratchet:true" in cycle, \
        "фиксация оборота упрётся в храповик: ночной прогон встанет посреди базы"
    assert 't("route.lap_unsaved"' in cycle, \
        "неудачная фиксация проходит молча — человек решит, что работа сохранена"
    assert "CYCLE_LIMIT" in ui, "у цикла нет предохранителя"

    # Вариаций прогона быть не должно: маршрут один и работает до конца.
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    assert scen.count("[update]") == 1, "маршрутов обновления базы больше одного"
    assert "выключайте когда угодно" in scen, \
        "маршрут не обещает человеку, что его можно прервать"


@test
def test_console_says_which_step_uses_the_threads(tmp: Path):
    """Шаг в девять потоков и шаг в один выглядят в консоли одинаково.

    Разница между ними — ночь против часа, а человек, настроивший «одновременно»,
    вправе знать, где эта настройка работает, а где не применяется вовсе: разбор
    источников и разбор синонимов идут по очереди при любом потолке.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    ag = importlib.import_module("agent_core")
    importlib.reload(ag)
    ar = importlib.import_module("agent_runner")
    importlib.reload(ar)

    wide = ag.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a/v1",
                            "AURORA_AGENT_BACKEND_1_WIDTH": "9",
                            "AURORA_AGENT_PARALLEL": "9"})
    line = ar.threads_line(wide, 9)
    assert "потоков: 9" in line and "№1×9" in line, \
        f"параллельный шаг не называет ни числа потоков, ни шлюзов: {line}"

    # Раскладка — по тем слотам, что реально пойдут в работу. Печатали весь пул, и
    # строка противоречила сама себе: «потоков: 30 · слоты по шлюзам: №1×99». Строка
    # заведена, чтобы говорить правду о параллельности, и врать ей нельзя вдвойне.
    huge = ag.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a/v1",
                            "AURORA_AGENT_BACKEND_1_WIDTH": "99",
                            "AURORA_AGENT_PARALLEL": "99"})
    cut = ar.threads_line(huge, 30)
    assert "потоков: 30" in cut and "№1×30" in cut, \
        f"строка про потоки противоречит сама себе: {cut}"

    # Шаг, который не распараллеливается, обязан сказать это, а не молчать: иначе
    # человек ждёт ускорения от настройки, на этот шаг не влияющей.
    seq = ar.threads_line(wide, 1)
    assert "не распараллеливается" in seq and "9" in seq, \
        f"последовательный шаг молчит про потолок: {seq}"

    # Рассуждения по ролям. Замер на живом шлюзе: пересказ карточки в тезис с
    # рассуждениями — 66 секунд на три карточки, без них 5,7. В одиннадцать раз быстрее,
    # а тезис выходит не хуже, местами точнее: пересказ — извлечение, а не суждение.
    # Судят критик и Момус, и им рассуждения нужны.
    mixed = ag.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a/v1",
                             "AURORA_AGENT_BACKEND_1_MODEL": "m",
                             "AURORA_AGENT_THINKING": "1",
                             "AURORA_AGENT_THINKING_WORKER": "0"})
    seen = {}

    def spy(role):
        def tr(kind, b, pl, to):
            if pl:
                seen[role] = pl.get("chat_template_kwargs", {}).get("enable_thinking")
            return 200, {"choices": [{"message": {"content": "ок"}}], "usage": {}}, "", 0.1
        return tr

    for role in ("worker", "qa"):
        ag.call_role(mixed, role, [{"role": "user", "content": "x"}],
                     transport=spy(role), deadline=9e9, sleep=lambda s: None)
    assert seen.get("worker") is False, "роль не может выключить себе рассуждения"
    assert seen.get("qa") is True, "роль без своей настройки перестала слушать общую"

    narrow = ag.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://a/v1",
                              "AURORA_AGENT_PARALLEL": "1"})
    why = ar.threads_line(narrow, 1)
    assert "«одновременно» = 1" in why and "Настройка кита" in why, \
        f"не сказано, где именно поднять потолок: {why}"

    src = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    # Занятость считается живой, а не выводится из ширины пула: пул может простаивать.
    assert "busy_lock" in src and "потоков {busy}/{width}" in src, \
        "в строке прогресса не видно, сколько потоков занято прямо сейчас"
    assert src.count("threads_line(") >= 4, \
        "не все длинные шаги объявляют свою параллельность"
    assert "· 1 поток ·" in src, \
        "последовательные шаги не помечают строки прогресса"


@test
def test_push_guard_reads_the_content_not_just_the_branch(tmp: Path):
    """Публикацию стерегут по содержимому уезжающих коммитов, а не только по имени ветки.

    До 1.100.1 содержимое не смотрел никто: хук сообщений читает текст коммита, линтер —
    базу знаний. Через эту дыру в публичный репозиторий уехали рабочая папка стороннего
    инструмента (адрес внутреннего шлюза) и файл состояния прогона с именем живого
    проекта.

    Смотреть надо **добавленные строки диапазона**, а не итоговое дерево: файл, заведённый
    и убранный внутри одной серии коммитов, из дерева исчезает, а в истории остаётся.
    """
    root = tmp / "repo"
    (root / "local").mkdir(parents=True)
    (root / "local" / "private_terms.txt").write_text("ТайныйПроект\nexample.com\n", encoding="utf-8")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)

    def commit(msg: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(git + ["commit", "-qm", msg, "--no-verify"], cwd=root, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True).stdout.strip()

    (root / "чисто.md").write_text("маршрут не спотыкается о флаг\n", encoding="utf-8")
    base = commit("основа")

    def scan(old: str, new_: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push"],
            cwd=root, input=f"refs/heads/main {new_} refs/heads/main {old}\n",
            capture_output=True, text=True)

    # 1. Утечка, которая не дожила до итогового дерева: заведена и убрана в диапазоне.
    (root / "утечка.json").write_text('{"url": "https://api.example.com/v1"}\n', encoding="utf-8")
    mid = commit("завёл")
    (root / "утечка.json").unlink()
    head = commit("убрал")
    cp = scan(base, head)
    assert cp.returncode == 1, \
        "файл заведён и убран внутри диапазона — из дерева исчез, но в истории остался"
    assert "example.com" in cp.stderr and "утечка.json" in cp.stderr, \
        f"хук не назвал ни термин, ни файл:\n{cp.stderr}"

    # 2. Честный push не блокируется словом, внутри которого оказалось название.
    (root / "обычное.md").write_text("маршрут не спотыкается, а ТайныйПроектор — прибор\n",
                                     encoding="utf-8")
    clean = commit("обычная правка")
    cp = scan(head, clean)
    assert cp.returncode == 0, (
        "название внутри длинного слова приняли за утечку — хук, ловящий подстроку, "
        f"блокирует живую работу:\n{cp.stderr}")

    # 3. Настоящее вхождение по границам слова — ловится.
    (root / "плохое.md").write_text("сделано для ТайныйПроект в августе\n", encoding="utf-8")
    bad = commit("имя проекта в тексте")
    cp = scan(clean, bad)
    assert cp.returncode == 1 and "ТайныйПроект" in cp.stderr, \
        f"имя проекта отдельным словом не поймано:\n{cp.stderr}"

    # 4. Нет списка названий — проверка спит, а не роняет push.
    (root / "local" / "private_terms.txt").unlink()
    assert scan(clean, bad).returncode == 0, \
        "без списка названий хук обязан молчать: иначе он ломает любой чужой клон"


@test
def test_context_is_cut_to_the_backend_that_answers(tmp: Path):
    """Текст режется по окну ТОГО бэкенда, который отвечает, а не по одному числу на всех.

    Резать до выбора бэкенда нечем: в этот момент неизвестно, кто ответит, — и одно
    число на кольцо неверно в обе стороны. По самому широкому окну узкий бэкенд получает
    то, что в него не влезет, и `fits` его пропускает; по самому узкому широкий бэкенд,
    который взял бы всё, получает огрызок — знание теряется ради модели, которая и не
    отвечала. Бэкенды разные, и каждый обязан получить столько, сколько держит.

    Отсюда же честность про обрезание: сколько модель увидела, знает только тот вызов,
    который её выбрал, — значит `cut` обязан возвращаться из вызова, а не считаться
    заранее по гипотетическому бэкенду.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A

    whole = "Я" * 200_000
    build = lambda part: [{"role": "user", "content": "Разбери источник:\n" + part}]
    seen = {}

    def transport(kind, b, payload, timeout):
        if kind == "slots":
            return (404, None, "нет /slots", 0.0)
        seen[b["n"]] = sum(len(m.get("content") or "") for m in payload["messages"])
        return (200, {"choices": [{"message": {"content": "ок"}, "finish_reason": "stop"}]},
                "", 0.1)

    # Узкий отвечает первым: он и режет — но только для себя.
    narrow_first = A.parse_config({
        "AURORA_AGENT_BACKEND_1_URL": "http://narrow", "AURORA_AGENT_BACKEND_1_MODEL": "m",
        "AURORA_AGENT_BACKEND_1_CONTEXT": "8000",
        "AURORA_AGENT_BACKEND_2_URL": "http://wide", "AURORA_AGENT_BACKEND_2_MODEL": "m",
        "AURORA_AGENT_BACKEND_2_CONTEXT": "128000",
        "AURORA_AGENT_REQUEST_TIMEOUT": "30"})
    r1 = A.call_role(narrow_first, "worker", [], transport=transport,
                     trim=(whole, build), deadline=time.time() + 30, sleep=lambda s: None)
    assert r1["ok"] and r1["backend"] == 1, f"узкий бэкенд не ответил: {r1}"
    narrow_seen = seen[1]

    # Широкий отвечает первым (узкий выключен): он обязан увидеть СУЩЕСТВЕННО больше.
    seen.clear()
    wide_only = A.parse_config({
        "AURORA_AGENT_BACKEND_1_URL": "http://wide", "AURORA_AGENT_BACKEND_1_MODEL": "m",
        "AURORA_AGENT_BACKEND_1_CONTEXT": "128000",
        "AURORA_AGENT_REQUEST_TIMEOUT": "30"})
    r2 = A.call_role(wide_only, "worker", [], transport=transport,
                     trim=(whole, build), deadline=time.time() + 30, sleep=lambda s: None)
    assert r2["ok"], f"широкий бэкенд не ответил: {r2}"
    wide_seen = seen[1]

    assert wide_seen > narrow_seen * 5, (
        f"широкий бэкенд увидел {wide_seen} символов, узкий — {narrow_seen}: текст режется "
        f"по одному числу на всё кольцо, а не по окну отвечающего")
    assert r1["cut"] > r2["cut"] >= 0, (
        f"обрезание не вернулось из вызова: узкий cut={r1.get('cut')}, "
        f"широкий cut={r2.get('cut')} — а «пусто по огрызку не вердикт» держится на нём")

    # Окно не объявлено — движок не выдумывает предел и отправляет всё.
    seen.clear()
    silent = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x",
                             "AURORA_AGENT_BACKEND_1_MODEL": "m",
                             "AURORA_AGENT_REQUEST_TIMEOUT": "30"})
    r3 = A.call_role(silent, "worker", [], transport=transport, trim=(whole, build),
                     deadline=time.time() + 30, sleep=lambda s: None)
    assert r3["ok"] and r3["cut"] == 0 and seen[1] > len(whole), (
        f"необъявленное окно приняли за предел: увидено {seen.get(1)}, cut={r3.get('cut')}")


@test
def test_aliases_worker_crash_is_not_swallowed(tmp: Path):
    """Падение воркера обязано дойти до человека, а не исчезнуть в футуре.

    `executor.submit(...)` без чтения результата прячет исключение внутри Future: группа
    молча не обрабатывается, а прогон отчитывается как успешный. Для параллельного build
    результат читался (`future.result()`), для синонимов — нет: в одном файле два разных
    подхода к одной опасности.
    """
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

    def crash(cfg_, *a, **k):
        raise RuntimeError("воркер упал на разборе синонима")

    conflicts = [('a', 'x'), ('b', 'y')]
    with patch('agent_runner.read_conflicts', return_value=conflicts), \
            patch('agent_runner.solve_conflict', side_effect=crash):
        try:
            res = run_aliases(cfg, str(root), False, True, 0)
        except RuntimeError:
            return                      # долетело наружу — это честно
    assert res.get("stopped") or any(s["status"] in ("сбой", "стоп") for s in res["steps"]), (
        "воркер упал, а прогон отчитался как успешный: исключение осталось в Future, "
        f"которую никто не прочитал. Отчёт: {res}")


@test
def test_slot_semaphore_matches_the_pool(tmp: Path):
    """Ширина канала к бэкенду — ровно та, что раздал `pool`, и ни шире, ни уже.

    Семафор считал её сам: `backend.get("width") or 1`. А `width` по умолчанию 0, и
    бэкенд без объявленной ширины получал канал в ОДИН запрос — хотя `pool` делит между
    такими общий потолок. Параллельность молча схлопывалась, а консоль объявляла N
    потоков. Обратная сторона той же самодеятельности: при ширине 9 и потолке 4 семафор
    пропускал 9, то есть нарушал потолок.

    Правило про ширину живёт в `pool` — второй его копии быть не должно.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A

    one = {"AURORA_AGENT_BACKEND_1_URL": "http://a", "AURORA_AGENT_BACKEND_1_MODEL": "m"}
    two = dict(one, AURORA_AGENT_BACKEND_2_URL="http://b",
               AURORA_AGENT_BACKEND_2_MODEL="m")
    cases = [
        ("объявленная ширина при потолке «авто»",
         dict(one, AURORA_AGENT_BACKEND_1_WIDTH="9", AURORA_AGENT_PARALLEL="авто"), 1, 9),
        ("ширина не объявлена — делит общий потолок",
         dict(one, AURORA_AGENT_PARALLEL="8"), 1, 8),
        ("объявленная ширина шире потолка — режет потолок",
         dict(one, AURORA_AGENT_BACKEND_1_WIDTH="9", AURORA_AGENT_PARALLEL="4"), 1, 4),
        ("бэкенд вне параллельности — один запрос за раз",
         dict(two, AURORA_AGENT_BACKEND_2_PARALLEL="0",
              AURORA_AGENT_BACKEND_1_WIDTH="4", AURORA_AGENT_PARALLEL="4"), 2, 1),
    ]
    for name, env, n, want in cases:
        cfg = A.parse_config(env)
        A._SEM.clear()
        backend = [b for b in cfg["backends"] if b["n"] == n][0]
        sem = A._slot_semaphore(backend, cfg)
        got = sem._value
        assert got == want, (
            f"{name}: канал к бэкенду №{n} шириной {got}, а `pool` раздал "
            f"{A.pool(cfg).count(n)} слотов — ожидали {want}")


@test
def test_build_plan_inprocess_does_not_retry_or_wander(tmp: Path):
    """В-процессе build_plan: сбой не выполняется второй раз, и папка процесса не гуляет.

    Два дефекта T5:

    1. `except Exception` накрывал не только импорт, но и сам `build_card`, а фолбэк
       перезапускал ту же команду подпроцессом. Падение ПОСЛЕ частичной записи карточки
       означало вторую запись — побочный эффект дважды. Сеть безопасности нужна только
       на импорт: сбой сборки это сбой шага, а не повод сделать его другим способом.

    2. `os.chdir` — свойство процесса, а не потока. На длинном пути (`--card`) он висел
       на всё время сборки, и соседний поток, читающий файл по относительному пути,
       прочитал бы не ту папку. Держать корректность на предположении о том, чем заняты
       соседи, нельзя.
    """
    import threading
    from unittest.mock import patch

    sys.path.insert(0, str(KIT / "scripts"))
    import agent_runner as AR

    root = make_project(tmp)
    src = root / "Sources" / "Confluence" / "источник.md"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("# Заголовок\n\nТекст источника.\n", encoding="utf-8")

    # 1. Сбой внутри build_card не должен уходить в subprocess-фолбэк.
    AR._BP_MODULES.clear()
    mod = AR._bp_import(str(root))
    calls = {"card": 0, "fallback": 0}

    def boom(*a, **k):
        calls["card"] += 1
        raise RuntimeError("диск кончился на половине карточки")

    def fake_run_command(*a, **k):
        calls["fallback"] += 1
        return {"ok": True, "rc": 0, "out": "", "refused": ""}

    with patch.object(mod, "build_card", side_effect=boom), \
            patch.object(AR, "run_command", side_effect=fake_run_command):
        res = AR.run_build_plan(str(root), ["--card", "Карточка", "--source",
                                            "Sources/Confluence/источник.md",
                                            "--to", "Concepts", "--apply"])
    assert calls["card"] == 1, f"build_card вызван {calls['card']} раз — ожидался один"
    assert calls["fallback"] == 0, (
        "сбой сборки увёл в subprocess-фолбэк: карточка, записанная наполовину, будет "
        "записана второй раз")
    assert not res["ok"], f"сбой сборки выдан за успех: {res}"

    # 2. Папка процесса не меняется, пока идёт сборка карточки.
    AR._BP_MODULES.clear()
    here = os.getcwd()
    seen, done = [], threading.Event()

    def slow_card(*a, **k):
        time.sleep(0.25)
        return 0

    def watcher():
        while not done.is_set():
            seen.append(os.getcwd())
            time.sleep(0.01)

    mod = AR._bp_import(str(root))
    with patch.object(mod, "build_card", side_effect=slow_card):
        w = threading.Thread(target=watcher, daemon=True)
        w.start()
        AR.run_build_plan(str(root), ["--card", "Карточка", "--source",
                                      "Sources/Confluence/источник.md",
                                      "--to", "Concepts", "--apply"])
        done.set()
        w.join(timeout=2)
    wandered = sorted({p for p in seen if p != here})
    assert not wandered, (
        f"во время сборки папка процесса уходила в {wandered} — соседний поток, читающий "
        f"относительный путь, прочитал бы не ту папку")
    assert os.getcwd() == here, "папка процесса не вернулась на место"

    # 2б. То же для отметки «разобрано». Ветка короткая, но папка процесса общая: пока
    #     она уведена, любой относительный путь в соседнем потоке читает не ту папку.
    AR._BP_MODULES.clear()
    seen2, done2 = [], threading.Event()

    def slow_done(*a, **k):
        time.sleep(0.25)
        return 0

    def watcher2():
        while not done2.is_set():
            seen2.append(os.getcwd())
            time.sleep(0.01)

    mod = AR._bp_import(str(root))
    with patch.object(mod, "mark_done", side_effect=slow_done):
        w2 = threading.Thread(target=watcher2, daemon=True)
        w2.start()
        AR.run_build_plan(str(root), ["--done", "Sources/Confluence/источник.md",
                                      "--cards", "1"])
        done2.set()
        w2.join(timeout=2)
    wandered2 = sorted({p for p in seen2 if p != here})
    assert not wandered2, (
        f"во время отметки «разобрано» папка процесса уходила в {wandered2}")

    # 3. Два проекта в одном процессе получают каждый свой корень базы.
    #    Кеш по одному лишь файлу движка отдавал бы второму проекту модуль, уже
    #    привязанный к первому, и карточки уехали бы в чужую базу.
    AR._BP_MODULES.clear()
    (tmp / "второй").mkdir(parents=True, exist_ok=True)
    other = make_project(tmp / "второй")
    m1 = AR._bp_import(str(root))
    m2 = AR._bp_import(str(other))
    assert os.path.abspath(m1.KB_ROOT).startswith(os.path.abspath(str(root))), \
        f"первый проект потерял свой корень: {m1.KB_ROOT}"
    assert os.path.abspath(m2.KB_ROOT).startswith(os.path.abspath(str(other))), (
        f"второй проект получил корень первого: {m2.KB_ROOT} — карточки уехали бы "
        f"в чужую базу")
    assert m1.MANIFEST != m2.MANIFEST, "манифест общий на два проекта"


@test
def test_build_stop_really_stops_the_work(tmp: Path):
    """Остановка параллельного build обязана остановить работу, а не только отчёт.

    Регрессия: все задачи уходили в пул разом, а `break` из `as_completed` выходил в
    `with ThreadPoolExecutor`, который на выходе делает `shutdown(wait=True)` — очередь
    дорабатывалась до конца. Бюджет, лимит шагов и «одна и та же ошибка N раз подряд»
    оказывались пожеланиями: с `--apply` база продолжала меняться уже после того, как
    прогон решил остановиться.

    Считаем не шаги в отчёте, а фактические входы в solve_source: именно они трогают базу.
    """
    import threading
    from unittest.mock import patch

    sys.path.insert(0, str(KIT / "scripts"))
    from agent_core import parse_config
    import agent_runner
    from agent_runner import run_build

    root = make_project(tmp)
    cfg = parse_config({
        'AURORA_AGENT_BACKEND_1_URL': 'http://test',
        'AURORA_AGENT_BACKEND_1_MODEL': 'test',
        'AURORA_AGENT_BACKEND_1_WIDTH': '2',
        'AURORA_AGENT_PARALLEL': '2',
        'AURORA_AGENT_BUDGET_MIN': '20',
        'AURORA_AGENT_MAX_STEPS': '50',
        'AURORA_AGENT_REQUEST_TIMEOUT': '300',
    })

    entered, lock = [], threading.Lock()

    def always_fails(cfg_, *a, **k):
        with lock:
            entered.append(a[2])
        time.sleep(0.02)
        return {'alias': 't', 'status': 'сбой', 'backends': [], 'degraded': False,
                'note': 'шлюз недоступен'}

    sources = [('Confluence', f'f{i}.md', 1) for i in range(24)]
    with patch('agent_runner.read_partition', return_value=sources), \
            patch('agent_runner.solve_source', side_effect=always_fails):
        res = run_build(cfg, str(root), False, True, 0)

    limit = agent_runner.SAME_FAIL_LIMIT
    # Порог с запасом на уже начатые: сколько потоков в работе, столько шагов могут
    # завершиться после решения остановиться. Но не все 24 — очередь обязана свернуться.
    ceiling = limit + 2 * 2
    assert len(entered) <= ceiling, (
        f"после {limit} одинаковых ошибок в работу вошло {len(entered)} источников из "
        f"{len(sources)} — очередь доработала вместо остановки, и с --apply это правки "
        f"в базе после решения остановиться")
    assert res.get("stopped"), "прогон остановился, но причина не названа в отчёте"
