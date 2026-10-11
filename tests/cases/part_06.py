"""Проверки движка Aurora, часть 6 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import shutil
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
    assert "os.remove(RUNNING)" in src and "drop_dead_running()" in src, \
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
    # Маршрут останавливает сервер: «Прервать» шлёт остановку маршруту, а итог называет её
    # прерыванием человека, а не отказом команды.
    rr = (KIT / "cockpit/route_runner.py").read_text(encoding="utf-8")
    assert '"/api/route/stop"' in ui and 't("route.stopped_by_you", cmd=bad) if self.stopped' in rr, \
        "прерывание человеком показывается как отказ команды: «разберитесь с ней»"
    assert "restartPanel" in ui and "Перезапустить панель" in ui, \
        "полоса про устаревший процесс всё ещё отсылает в терминал"
    # Прерванный процесс возвращает отрицательный код: «код 2 и выше» его пропускало,
    # и маршрут после осознанного прерывания шёл дальше.
    assert 'res["rc"] >= 2 or res["rc"] < 0' in rr, \
        "маршрут не считает прерывание поводом остановиться"
    assert 'res["rc"] >= 2:' not in rr, "остались проверки, пропускающие прерывание"

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
                             "mcp_active": [], "guard": {"ready": False},
                             # прямой HTTP — выбор вызова (с 1.161.0 адаптер берётся из него)
                             "adapter": "openai_compat"},
                            5.0)
    finally:
        A.http_json = real

    got = set(sent["payload"])
    internal = {"role", "tools_root", "mcp", "mcp_active", "guard", "adapter"}
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
    # Ожидание сети ведёт сервер (`cockpit/route_runner.py`, 1.152.0).
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    rr = importlib.import_module("route_runner")
    assert rr.FLAKY_RETRY_S <= 60 < rr.OFFLINE_RETRY_S, \
        "нет короткой паузы для мигающего шлюза — маршрут снова будет стоять при живых бэкендах"
    # Признак ищем в тех числах, которые шаги печатают на самом деле.
    for line in ("переписано: 12", "разобрано 3 из 15", "уточнено: 4", "тезисов: 7"):
        assert rr.made_progress([line]), \
            f"признак прогресса не знает про «{line}» — такой шаг сочтут безрезультатным"
    assert not rr.made_progress(["переписано: 0", "Connection refused"])
    src = (KIT / "cockpit/route_runner.py").read_text(encoding="utf-8")
    body = src[src.index("def _wait_network"):src.index("def run_one")]
    assert "made_progress" in body, \
        "автоповтор не различает мигающий шлюз и лежащий — вернётся простой на два часа"
    assert "if not flaky:\n                    attempt += 1" in body, (
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
    held["pid"] = 4 if os.name == "nt" else 1       # системный процесс; на Windows это «System»
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
                        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
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
                          cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
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
    # Маршрут ведёт сервер (1.152.0): счётчик, пропуски и сделанное — его забота.
    import importlib
    sys.path.insert(0, str(KIT / "cockpit"))
    rr = importlib.import_module("route_runner")
    p18 = importlib.import_module("cases.part_18")
    sc = p18._route()                      # kb:repair, (человек), kb:lint
    ck = p18.FakePanel(tmp, {"kb:repair": [(0, [])], "kb:lint": [(2, ["упало"])]}, [sc])
    run = rr.RouteRun(ck, str(tmp), sc, True, resume={"skipSigs": ["kb:repair --apply"]})
    run.run()
    notes = [e["s"] for e in run.journal.entries if e["k"] == "note"]
    assert any("шаг 1/2 пропущен" in n for n in notes), (
        "пропущенный шаг не увеличивает счётчик — продолжение после семи сделанных шагов "
        "покажет «шаг 1 из N», и человек прочтёт это как «началось заново»: " + str(notes))
    heads = [e["s"] for e in run.journal.entries if e["k"] == "head"]
    assert heads == [h for h in heads if h.startswith("▸ шаг 2 из 2")], heads
    assert "kb:repair --apply" in ck.route_state["done"], \
        "сделанное прошлой попытки не переносится — на третьей попытке работа первой повторится"

    ui = panel_sources()
    body = ui[ui.index("async function attachRoute("):ui.index("function finishRoute(")]
    assert 'if (!resumed) out.innerHTML = "";' in body, (
        "маршрут стирает консоль безусловно — продолжение уносит вывод прошлой попытки, "
        "ради которого кнопку и нажимают")


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
    `.aurora/runs/<id>/` с полным console.log и events.jsonl по шагам, архив не
    растёт вечно, а id прогона, рождённый в браузере, не становится чужим путём.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)

    root = tmp / "проект"
    root.mkdir()
    assert ck.runs_dir(str(root)) == os.path.join(str(root), ".aurora", "runs")
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
    # Тело функции целиком — до следующей функции верхнего уровня, а не окно в 4000 знаков:
    # окно съезжало всякий раз, когда в начало `start_job` добавляли строку.
    sj = src[at:src.index("\ndef ", at + 1)]
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
    # С 1.152.0 история — одна строка на запуск, раскрытие — весь журнал запуска.
    for token in ("function renderHistory(", "function archiveWhen(", "function rtime(",
                  "function fmtDur(", 'id="exportMd"', "Продолжить маршрут",
                  "const SILENCE_MS = 120000;", '"/api/history?project="',
                  '"/api/history/run?project="', "function drawEntries("):
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
    import importlib
    sys.path.insert(0, str(KIT / "cockpit"))
    rr = importlib.import_module("route_runner")
    p18 = importlib.import_module("cases.part_18")
    sc = p18._route(cycle=True)
    ck = p18.FakePanel(tmp, {"kb:repair": [(1, ["## битые ссылки: 2"])],
                             "agent:build": [(0, ["Источников в плане: 9 → 4"])],
                             "kb:lint": [(0, [])]}, [sc])
    res = rr.RouteRun(ck, str(tmp), sc, True).run()
    assert res["reason"] == "stall" and not res["ok"], \
        "застой не помечен — не отличить его от прохода и не дать «Продолжить»: " + str(res)
    assert ck.route_state and ck.route_state["reason"] == "stall", \
        "застой не оставил состояния — «Продолжить маршрут» не появится"
    assert "kb:repair --apply" not in ck.route_state["done"], \
        "продолжение пропускает шаг с кодом 1 — он «нашёл, что чинить», а не прошёл"
    ui = panel_sources()
    assert "застой" in ui, "в интерфейсе нет «застой» — баннер не честен о причине"
    assert "Продолжить маршрут" in ui, "кнопку продолжения потеряли совсем"


@test
def test_a_stopped_route_survives_a_panel_restart(tmp: Path):
    """Остановленный маршрут переживает перезапуск панели: «Продолжить маршрут» после него.

    Состояние последнего остановленного маршрута лежит в `.aurora/state/last_route.json`,
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
        os.path.join(str(root), ".aurora", "state", "last_route.json")
    assert ck.read_route_state(str(root)) is None, \
        "файла нет — состояние читается как объект вместо None"

    wrote = ck.write_route_state(str(root), {"scId": "update", "title": "Обновить базу",
                                           "at": "2026-08-30T04:12:00"})
    assert wrote.get("ok") is True, f"запись состояния не удалась: {wrote}"
    path = root / ".aurora/state/last_route.json"
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
    # Состояние пишет сам маршрут — после каждого шага и в конце (сервер, 1.152.0).
    rr = (KIT / "cockpit/route_runner.py").read_text(encoding="utf-8")
    assert "ck.write_route_state(self.project, state)" in rr \
        and 'self.ck.write_route_state(self.project, self._state("interrupted"))' in rr, \
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
    fin = ui[ui.index("function finishRoute("):ui.index("async function resumeLastRoute(")]
    assert 'r.reason === "stall"' in fin and 't("route.rc_stalled")' in fin \
        and 'className = "chip warn"' in fin, "плашка застоя не жёлтая или не названа"
    assert "if (bad && S.lastRoute && S.project){" in fin, \
        "кнопка «Продолжить маршрут» не связана с остановкой"
    rr = (KIT / "cockpit/route_runner.py").read_text(encoding="utf-8")
    assert 'if res["rc"] == 0:\n            self.done_keys.add(key)' in rr, \
        "продолжение пропускает не только успешные шаги — код 1 повторять надо"
    assert '"stall" if self.stalled else "offline" if self.offline' in rr \
        and '"stopped" if self.stopped' in rr, "причина остановки смешивает застой и прерывание"
    assert "self.stopped, self.failed = True, steps[k][\"cmd\"]" in rr, \
        "стоп цикла по требованию не выставляет оба флага остановки"


@test
def test_a_route_waits_for_the_network_like_the_engine(tmp: Path):
    """Маршрут ждёт сеть ровно по признакам движка, а не глотает обрыв.

    Признаки офлайна в панели — буквальная копия списка `agent_runner`: обе стороны
    отличают «упала связь» от «источник молчит». Паритет проверяем как инвариант равенства
    множеств: изменение списка на любой стороне — расхождение, которое обязано дойти до
    человека. Дальше — протокол ожидания: шаг с кодом 1 и офлайн-текстом не роняет
    маршрут, а ставит его «ждёт сеть» — без потолка попыток (1.170.0: маршрут доводит дело
    до конца), — и позволяет выйти из ожидания руками («Попробовать сейчас» / «не ждать»).
    """
    engine = (KIT / "scripts/agent_runner.py").read_text(encoding="utf-8")
    # С 1.152.0 маршрут ведёт сервер: признаки офлайна он берёт из движка, а не копией.
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    rr = importlib.import_module("route_runner")
    m = re.search(r"OFFLINE_SIGNS\s*=\s*[\[(](.*?)[\])]", engine, re.S)
    assert m, "в agent_runner не найден список OFFLINE_SIGNS"
    engine_signs = set(re.findall(r"[\"']([^\"']+)[\"']", m.group(1)))
    assert set(rr.offline_signs()) == engine_signs, "офлайн-признаки маршрута и движка разошлись"
    assert rr.OFFLINE_RETRY_S == 15 * 60, "нет паузы ожидания сети (15 минут)"
    assert not hasattr(rr, "OFFLINE_TRIES"), "у ожидания сети снова потолок попыток"
    # Вывод шага — массив строк: проверка обязана принимать его, а не только строку
    # («Починить базу» останавливался на 1/13, 2026-08-30, когда она падала на массиве).
    assert rr.looks_offline(["шаг", "Connection refused"]) and not rr.looks_offline(["ok"])
    ui = panel_sources()
    assert "ждёт сеть (попытка" in ui, "нет текста о состоянии ожидания сети"
    assert "Маршрут ждёт, пока сеть не вернётся" in ui, "не сказано, что маршрут ждёт сеть до конца"
    live = ui[ui.index("async function attachRoute("):ui.index("function finishRoute(")]
    assert '"/api/route/wake"' in live and 't("route.wait_now")' in live, \
        "кнопка «Попробовать сейчас» не будит ожидание сервера"
    assert 't("route.wait_stop")' in live, "нет кнопки выйти из ожидания без сети"
    src = (KIT / "cockpit/route_runner.py").read_text(encoding="utf-8")
    assert 'state["attempts"]' in src, \
        "потолок ожидания не сохраняет число попыток для продолжения"
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
        os.path.join(str(root), ".aurora", "state", "last_route.json"), \
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
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    rr = importlib.import_module("route_runner")
    got = rr.left_by_kind(["Источников в плане: 9 → 4", "тезисы · осталось: 12"])
    # Виды работы считаются РАЗДЕЛЬНО. Сложенные в один максимум, они врали: разбор
    # добавляет карточки, переосмысление их разбирает, суммарный остаток стоит — и цикл
    # объявлял гонку застоем. На живой базе он так и встал на 814.
    assert got == {"источники": 4, "карточки": 12}, \
        "остатки разных видов работы слиты в одно число или считается один вид: " + str(got)
    src = (KIT / "cockpit/route_runner.py").read_text(encoding="utf-8")
    cycle = src[src.index("def _cycle"):src.index("def _finish")]
    assert "moved = [k for k in kinds if k not in prev or kinds[k] < prev[k]]" in cycle, \
        "цикл встаёт, когда не убыл общий остаток, а не когда не сдвинулось ничего"
    assert 't("route.grew"' in cycle, \
        "гонка разбора с переосмыслением не названа человеку числами"
    assert 'self._commit(t("route.commit_lap"' in cycle and "git_commit(self.project, message, None, True)" in src, \
        "оборот не фиксируется или упрётся в храповик — ночной прогон встанет посреди базы"
    assert 't("route.lap_unsaved"' in cycle, \
        "неудачная фиксация проходит молча — человек решит, что работа сохранена"
    assert rr.CYCLE_LIMIT == 12, "у цикла нет предохранителя"

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
                              capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()

    (root / "чисто.md").write_text("маршрут не спотыкается о флаг\n", encoding="utf-8")
    base = commit("основа")

    def scan(old: str, new_: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push"],
            cwd=root, input=f"refs/heads/main {new_} refs/heads/main {old}\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace")

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
    cp = scan(clean, bad)
    assert cp.returncode == 0, "без списка названий хук не должен ломать пуши участников"
    assert "НЕ проверено" in cp.stderr and "private_terms.txt" in cp.stderr, \
        f"хук стоит, а содержимое не проверяется — и молчит об этом:\n{cp.stderr}"


@test
def test_push_guard_checks_names_messages_binaries_and_never_opens_silently(tmp: Path):
    """BL-SEC-14: пуш проверяет не только строки диффа и не открывается при сбое проверки.

    До правки хук читал лишь добавленные строки текстовых диффов: название в имени файла
    при безобидном содержимом, в сообщении коммита (хук commit-msg видит только коммиты,
    созданные на этой машине), в двоичном файле или в сжатом внутри docx проходило; а если
    `git log` падал (неизвестный remote_sha), stdout пуст — и хук возвращал 0, то есть
    открывался именно там, где проверить не вышло. Утечка в историю необратима, поэтому любой
    сбой проверки — отказ (код 2), снимаемый только явным AURORA_PUSH_UNVERIFIED=1.
    Термины синтетические: настоящий список в тесте не читается.
    """
    import zipfile
    root = tmp / "repo"
    (root / "local").mkdir(parents=True)
    (root / "local" / "private_terms.txt").write_text("ТайныйПроект\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)

    def commit(msg: str, email: str = "t@t") -> str:
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "-c", f"user.email={email}", "-c", "user.name=t",
                        "commit", "-qm", msg, "--no-verify", "--allow-empty"], cwd=root, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace").stdout.strip()

    def scan(old: str, new_: str, **env) -> subprocess.CompletedProcess:
        e = {k: v for k, v in os.environ.items() if k != "AURORA_PUSH_UNVERIFIED"}
        e.update(env)
        return subprocess.run(
            [sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push"],
            cwd=root, input=f"refs/heads/main {new_} refs/heads/main {old}\n", env=e,
            capture_output=True, text=True, encoding="utf-8", errors="replace")

    (root / "чисто.md").write_text("ничего лишнего\n", encoding="utf-8")
    base = commit("основа")
    prev = base

    def leaks(label: str, why: str) -> None:
        nonlocal prev
        head = commit(label)
        cp = scan(prev, head)
        assert cp.returncode == 1 and "ТайныйПроект" in cp.stderr, f"{why}:\n{cp.stderr}"
        prev = head

    # 1. Название только в имени файла, содержимое пустое.
    (root / "ТайныйПроект_план.txt").write_text("", encoding="utf-8")
    leaks("файл", "название в имени файла пропущено")
    # 2. Название только в сообщении коммита.
    (root / "b.md").write_text("чисто\n", encoding="utf-8")
    leaks("правка для ТайныйПроект", "название в сообщении коммита пропущено")
    # 3. Название в почте автора.
    (root / "c.md").write_text("чисто\n", encoding="utf-8")
    head = commit("обычная правка", email="dev@ТайныйПроект.example")
    cp = scan(prev, head)
    assert cp.returncode == 1 and "ТайныйПроект" in cp.stderr, f"название в почте автора пропущено:\n{cp.stderr}"
    prev = head
    # 4. Название только в двоичных данных.
    (root / "данные.bin").write_bytes(b"\x00\x01\x02 " + "ТайныйПроект".encode("utf-8") + b" \x00\xff\xfe")
    leaks("двоичный", "название в двоичном файле пропущено")
    # 5. Название внутри сжатого docx: сырые байты его не показывают.
    with zipfile.ZipFile(root / "документ.docx", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:t>отчёт по ТайныйПроект</w:t>" * 50)
    raw = (root / "документ.docx").read_bytes()
    assert "ТайныйПроект".encode("utf-8") not in raw, "тест не проверяет сжатие"
    leaks("docx", "название внутри сжатого docx пропущено")
    # 6. Переименование прячет название из нового имени, но не из старого.
    (root / "ТайныйПроект_второй.md").write_text("текст\n", encoding="utf-8")
    prev2 = commit("заведён", email="t@t")
    (root / "ТайныйПроект_второй.md").rename(root / "новое.md")
    head = commit("переименован")
    cp = scan(prev2, head)
    assert cp.returncode == 1 and "ТайныйПроект" in cp.stderr, f"старое имя при переименовании пропущено:\n{cp.stderr}"
    prev = head

    # 7. Каталог vendor: минифицированное содержимое не сканируем, документы и имена — да.
    (root / "cockpit/vendor").mkdir(parents=True)
    (root / "cockpit/vendor/lib.min.js").write_text("var a='ТайныйПроект';\n", encoding="utf-8")
    head = commit("вендор")
    assert scan(prev, head).returncode == 0, "минифицированный чужой код под vendor больше не пропускается"
    prev = head
    (root / "cockpit/vendor/README.md").write_text("для ТайныйПроект\n", encoding="utf-8")
    leaks("вендор-документ", "документ под vendor ушёл из проверки целиком")

    # 8. Диапазон прочитать не удалось: отказ, а не разрешение.
    clean_root = tmp / "clean"
    clean_root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=clean_root, check=True)
    (clean_root / "a.md").write_text("чисто\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=clean_root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a", "--no-verify"],
                   cwd=clean_root, check=True)
    good = subprocess.run(["git", "rev-parse", "HEAD"], cwd=clean_root, capture_output=True,
                          text=True).stdout.strip()
    (clean_root / "local").mkdir()      # список названий не в истории: он лишь для проверки
    (clean_root / "local" / "private_terms.txt").write_text("ТайныйПроект\n", encoding="utf-8")
    cmd = [sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push"]

    def run_in_clean(line: str, **env):
        e = {k: v for k, v in os.environ.items() if k != "AURORA_PUSH_UNVERIFIED"}
        e.update(env)
        return subprocess.run(cmd, cwd=clean_root, input=line, env=e, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    broken = run_in_clean(f"refs/heads/main {'2' * 40} refs/heads/main {good}\n")
    assert broken.returncode == 2 and "не удалось проверить" in broken.stderr, \
        f"ошибка git log открыла пуш (fail-open): rc={broken.returncode}\n{broken.stderr}"
    allowed = run_in_clean(f"refs/heads/main {'2' * 40} refs/heads/main {good}\n", AURORA_PUSH_UNVERIFIED="1")
    assert allowed.returncode == 0, f"явный AURORA_PUSH_UNVERIFIED=1 не снимает отказ:\n{allowed.stderr}"

    # 9. remote_sha неизвестен локально: проверяются все коммиты, которых нет в remote-ветках.
    unknown = run_in_clean(f"refs/heads/main {good} refs/heads/main {'3' * 40}\n")
    assert unknown.returncode == 0 and "неизвестен локально" in unknown.stderr, \
        f"чистая история при неизвестном remote_sha: rc={unknown.returncode}\n{unknown.stderr}"
    cp = scan("4" * 40, prev)
    assert cp.returncode == 1 and "ТайныйПроект" in cp.stderr, \
        f"неизвестный remote_sha открыл проверку (fail-open):\n{cp.stderr}"


@test
def test_push_hook_blocks_backup_branches_and_stops_when_the_scan_does_not_run(tmp: Path):
    """BL-SEC-14: оболочка pre-push закрывает backup/*, history-*, чужие публичные хосты и сбои скана.

    Раньше защищались две ветки по последнему сегменту имени и два домена; `backup/pre-scrub-*`
    (история до зачистки внутренних названий) ехала наружу, `bitbucket.org` и `codeberg.org`
    хук считал приватными, а нет скрипта проверки или он падает — пуш открывался.
    """
    sh = shutil.which("sh")
    if not sh or os.name == "nt":
        return
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    H = importlib.import_module("aurora_hooks")
    fake = tmp / "bin"
    fake.mkdir()
    (fake / "python3").symlink_to(sys.executable)
    repo = tmp / "repo"
    (repo / "scripts").mkdir(parents=True)
    git = shutil.which("git")
    env = {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp)}
    subprocess.run([git, "init", "-q"], cwd=str(repo), check=True, env=env)
    hook = repo / "pre-push"
    hook.write_text(H.PUSH_HOOK.format(marker=H.PUSH_MARKER, branches=" ".join(H.PRIVATE_BRANCHES)),
                    encoding="utf-8")
    stub = repo / "scripts/aurora_hooks.py"
    sha = "a" * 40

    def push(url: str, local: str, remote: str, name: str = "origin", lsha: str = sha, **extra):
        e = dict(env)
        e.update(extra)
        return subprocess.run([sh, str(hook), name, url],
                              cwd=str(repo), input=f"{local} {lsha} {remote} {'0' * 40}\n",
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=30, env=e)

    stub.write_text("import sys\nsys.stdin.read()\nprint('scanned', *sys.argv[1:])\n", encoding="utf-8")
    gh = "https://github.com/x/y.git"
    for ref in ("refs/heads/backup/pre-scrub-1", "refs/heads/history-master-1.16.2",
                "refs/heads/aurora-backup/1.2.3-0900", "refs/heads/development"):
        cp = push(gh, ref, ref)
        assert cp.returncode == 1 and "наружу не уходит" in cp.stderr, f"{ref}: rc={cp.returncode}\n{cp.stderr}"
    # другое имя на сервере или другая локальная ветка не обходят список
    assert push(gh, "refs/heads/backup/x", "refs/heads/foo").returncode == 1, "локальная backup/* уехала под другим именем"
    assert push(gh, "refs/heads/foo", "refs/heads/backup/x").returncode == 1, "ветка уехала в backup/* на сервере"
    # обычная ветка проходит и сканируется
    cp = push(gh, "refs/heads/main", "refs/heads/main")
    assert cp.returncode == 0 and "--remote origin" in cp.stdout, \
        f"оболочка не назвала скану remote, куда идёт пуш: {cp.stdout}{cp.stderr}"
    # удаление ветки кухни на сервере ничего не публикует: очистка не требует --no-verify
    cp = push(gh, "(delete)", "refs/heads/backup/x", lsha="0" * 40)
    assert cp.returncode == 0, f"удаление ветки кухни заблокировано как публикация: {cp.stderr}"
    # публичные хосты помимо двух старых; приватный сервер кухню принимает
    # Адреса чужих хостов собраны из частей: проверка нейтральных адресов в поставке (part_01) ищет
    # хосты по тексту, и литерал чужого сервера в тесте — это «чужой контур» для неё, как и в коде.
    dot = "."
    for url in ("https://bitbucket.org/x/y.git", f"git@codeberg{dot}org:x/y.git"):
        assert push(url, "refs/heads/backup/x", "refs/heads/backup/x").returncode == 1, f"{url} не считается публичным"
    private = "https://git.example.com/x/y.git"
    assert push(private, "refs/heads/backup/x", "refs/heads/backup/x", name="kitchen").returncode == 0, \
        "приватный сервер перестал принимать ветки кухни"
    # хост разбирается из адреса: схема, пользователь, порт, регистр, поддомен — а не подстрока
    for url in ("ssh://git@github.com:22/x/y.git", f"https://user:tok@GitLab{dot}com/x/y.git",
                f"git@sr{dot}ht:~x/y", "https://api.github.com/x/y.git",
                # алиас `Host github.com-<имя>` из ~/.ssh/config и FQDN с точкой: раньше их ловила подстрока
                "git@github.com-work:x/y.git", "https://github.com./x/y.git", f"git@gitlab{dot}com_work:x/y.git"):
        assert push(url, "refs/heads/backup/x", "refs/heads/backup/x").returncode == 1, f"{url} не считается публичным"
    for url in ("https://gitea.company.example/x/y.git", "https://gitlab.company.example/x/y.git",
                "https://git.example.com/usr.html", f"https://evilgithub{dot}com/x/y.git",
                "/srv/git/github.com/y.git"):
        assert push(url, "refs/heads/backup/x", "refs/heads/backup/x", name="kitchen").returncode == 0, \
            f"{url} принят за публичный по подстроке: приватный сервер не принимает ветки кухни"
    # приватный сервер на известном хосте объявляется явно; публичный по списку сильнее приватного
    subprocess.run([git, "config", "--add", "aurora.privateRemotes", "ghe"], cwd=str(repo), check=True, env=env)
    assert push(gh, "refs/heads/backup/x", "refs/heads/backup/x", name="ghe").returncode == 0, \
        "remote из aurora.privateRemotes всё равно считается публичным"
    subprocess.run([git, "config", "--add", "aurora.publicRemotes", "ghe"], cwd=str(repo), check=True, env=env)
    assert push(gh, "refs/heads/backup/x", "refs/heads/backup/x", name="ghe").returncode == 1, \
        "в сомнении пуш должен проверяться: публичный по списку обязан побеждать приватный"
    subprocess.run([git, "config", "--add", "aurora.publicRemotes", "mirror"], cwd=str(repo), check=True, env=env)
    assert push(private, "refs/heads/backup/x", "refs/heads/backup/x", name="mirror").returncode == 1, \
        "remote из aurora.publicRemotes не считается публичным"
    # скан не отработал: любой код, кроме 0, — пуш остановлен, и об этом сказано
    stub.write_text("import sys\nsys.stdin.read()\nsys.exit(2)\n", encoding="utf-8")
    cp = push(gh, "refs/heads/main", "refs/heads/main")
    assert cp.returncode == 1 and "кодом 2" in cp.stderr, f"сбой скана открыл пуш: rc={cp.returncode}\n{cp.stderr}"
    stub.write_text("import sys\nsys.stdin.read()\nsys.exit(1)\n", encoding="utf-8")
    assert push(gh, "refs/heads/main", "refs/heads/main").returncode == 1
    # скрипта проверки нет: закрыто, снимается явно
    stub.unlink()
    cp = push(gh, "refs/heads/main", "refs/heads/main")
    assert cp.returncode == 1 and "проверить нечем" in cp.stderr, f"нет скрипта — пуш открыт: {cp.stderr}"
    assert push(gh, "refs/heads/main", "refs/heads/main", AURORA_PUSH_UNVERIFIED="1").returncode == 0
    # хук старой версии на машине: статус говорит «устарел», а не «не установлен»
    (repo / ".git/hooks/pre-push").write_text("#!/bin/sh\n# aurora-hook push v2\n", encoding="utf-8")
    st = subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--status"], cwd=str(repo),
                        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    assert "УСТАРЕЛ" in st.stdout, st.stdout + st.stderr


@test
def test_push_guard_reads_content_by_signature_and_normalizes_unicode(tmp: Path):
    """BL-SEC-14: архивы, gzip и UTF-16 узнаются по содержимому, а не по расширению; NFD не прячет название.

    Расширение — не свойство файла: `.xlsm`, `.epub`, zip под именем `.dat`, архив в docx и `.gz`
    несут сжатый текст так же, как docx; UTF-16 с BOM не виден построчному скану (между
    символами нули). Название в декомпозированной форме («й» как «и» и U+0306 — так отдаёт имена
    файловая система macOS) не находилось ни в строках, ни в именах внутри архива. Что проверить
    нельзя (больше предела, зашифровано, вложено слишком глубоко), — не «чисто», а отказ.
    """
    import bz2
    import gzip
    import importlib
    import io
    import lzma
    import unicodedata
    import zipfile
    sys.path.insert(0, str(KIT / "scripts"))
    H = importlib.import_module("aurora_hooks")
    term = "ТайныйПроект"
    nfd = unicodedata.normalize("NFD", term)
    assert nfd != term, "тест не проверяет декомпозицию"

    def pack(name: str, text: str, deflate: bool = True) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED) as z:
            z.writestr(name, text)
        return buf.getvalue()

    secret = f"отчёт по {term} " * 40
    files = {
        "u16.txt": b"\xff\xfe" + f"для {term}\n".encode("utf-16-le"),
        "m.xlsm": pack("xl/s.xml", secret),
        "b.epub": pack("OEBPS/c.xhtml", secret),
        "z.dat": pack("a.txt", secret),
        "log.gz": gzip.compress(secret.encode("utf-8")),
        "nfc_in_nfd.docx": pack("word/document.xml", f"для {nfd}"),
        "nfd_name.docx": pack(f"word/{nfd}.xml", "пусто"),
        # кодировки и упаковка, которых построчный скан не видит
        "cp.csv": f"a;b\r\nдля {term};1\r\n".encode("cp1251"),
        "u16le.txt": f"для {term}\n".encode("utf-16-le"),
        "u16be.txt": f"для {term}\n".encode("utf-16-be"),
        "log.bz2": bz2.compress(secret.encode("utf-8")),
        "log.xz": lzma.compress(secret.encode("utf-8")),
        "data.bin": bz2.compress((secret + "!").encode("utf-8")),        # расширение ничего не значит
    }

    def wrap(inner: bytes) -> bytes:
        """Архив, внутри которого лежит другой архив."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("inner.zip", inner)
        return buf.getvalue()
    files["nest.docx"] = wrap(pack("a.txt", secret))
    files["ok.docx"] = pack("word/document.xml", "ничего лишнего" * 40)
    files["ok.gz"] = gzip.compress("ничего лишнего".encode("utf-8"))
    files["broken.gz"] = b"\x1f\x8b\x08\x00" + "не поток".encode("utf-8")
    files["ok16.txt"] = b"\xff\xfe" + "чисто\n".encode("utf-16-le")
    files["ok.bz2"] = bz2.compress("ничего лишнего".encode("utf-8"))
    files["ok.xz"] = lzma.compress("ничего лишнего".encode("utf-8"))
    files["ok.csv"] = "a;b\r\nничего;1\r\n".encode("cp1251")
    files["ok16le.txt"] = "чисто\n".encode("utf-16-le")
    files["broken.bz2"] = b"BZh9" + "не поток".encode("utf-8")
    files["broken.xz"] = b"\xfd7zXZ\x00" + "не поток".encode("utf-8")
    for name, data in files.items():
        assert term.encode("utf-8") not in data or name == "ok.docx", f"{name}: тест не проверяет сжатие"

    root = tmp / "repo"
    (root / "local").mkdir(parents=True)
    (root / "local" / "private_terms.txt").write_text(term + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    (root / "чисто.md").write_text("ничего\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(git + ["commit", "-qm", "основа", "--no-verify"], cwd=root, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    for name, data in files.items():
        (root / name).write_bytes(data)
    # Название в декомпозированной форме в тексте: строка диффа
    (root / "nfd.md").write_text(f"для {nfd}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(git + ["commit", "-qm", "файлы", "--no-verify"], cwd=root, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    cp = subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push"], cwd=root,
                        input=f"refs/heads/main {head} refs/heads/main {base}\n",
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert cp.returncode == 1, f"rc={cp.returncode}\n{cp.stderr}"
    for name in ("u16.txt", "m.xlsm", "b.epub", "z.dat", "nest.docx", "log.gz",
                 "nfc_in_nfd.docx", "nfd_name.docx", "nfd.md", "cp.csv", "u16le.txt", "u16be.txt",
                 "log.bz2", "log.xz", "data.bin"):
        assert f"  {name}" in cp.stderr, f"{name}: название не найдено:\n{cp.stderr}"
    for name in ("ok.docx", "ok.gz", "broken.gz", "ok16.txt", "ok.bz2", "ok.xz", "ok.csv", "ok16le.txt",
                 "broken.bz2", "broken.xz"):
        assert f"  {name}" not in cp.stderr, f"{name}: ложное срабатывание:\n{cp.stderr}"
    assert "Traceback" not in cp.stderr, cp.stderr
    assert "Файл: уберите" in cp.stderr and "Сообщение, автор или тег" not in cp.stderr, \
        f"подсказка не по виду находки:\n{cp.stderr}"

    # Удаление ветки содержимого не прибавляет, даже когда история грязная.
    gone = subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push"], cwd=root,
                          input=f"(delete) {'0' * 40} refs/heads/main {head}\n",
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert gone.returncode == 0, f"удаление ветки остановлено проверкой содержимого:\n{gone.stderr}"

    # Что проверить нельзя — отказ в errors, а не молчаливое «чисто»; битое — не падение.
    real_private, real_cap = H.private_terms, H.ZIP_CAP
    H.private_terms = lambda: [term]
    try:
        pats = H.term_patterns()

        def run(data: bytes, **kw):
            hits, errors = {}, []
            H._scan_payload(pats, data, "f", hits, errors, **kw)
            return hits, errors
        locked = bytearray(pack("a.txt", secret))
        locked[6] |= 1
        locked[locked.find(b"PK\x01\x02") + 8] |= 1
        hits, errors = run(bytes(locked))
        assert not hits and any("зашифрован" in e for e in errors), f"зашифрованный архив прошёл молча: {errors}"
        deep = pack("a.txt", secret)
        for _ in range(H.ARCHIVE_DEPTH + 1):
            deep = wrap(deep)
        hits, errors = run(deep)
        assert any("глубже" in e for e in errors), f"слишком глубокая вложенность прошла молча: {errors}"
        H.ZIP_CAP = 100
        hits, errors = run(pack("a.txt", "x" * 500, deflate=True))
        assert any("больше" in e for e in errors), f"крупный член архива прошёл молча: {errors}"
        hits, errors = run(gzip.compress(b"x" * 5000))
        assert any("после распаковки" in e for e in errors), f"крупный gzip прошёл молча: {errors}"
        for pack_ in (bz2.compress, lzma.compress):
            hits, errors = run(pack_(b"x" * 5000))
            assert any("после распаковки" in e for e in errors), f"крупный {pack_.__module__} прошёл молча: {errors}"
        H.ZIP_CAP = real_cap
        damaged = bytearray(pack("a.txt", secret))
        damaged[damaged.find(b"a.txt") + 10] ^= 0xFF           # внутри сжатых данных
        run(bytes(damaged))                                    # не должен падать трассировкой
    finally:
        H.private_terms, H.ZIP_CAP = real_private, real_cap


@test
def test_push_guard_reads_added_text_whatever_the_line_breaks_and_prefixes(tmp: Path):
    """BL-SEC-14: добавленный текст сканируется и тогда, когда в строке голый CR, FF, FS-RS, NEL или `++`.

    Вывод `git log -p` читался в текстовом режиме (голый `\\r` становился переводом строки) и резался
    `splitlines()` (он режет ещё по `\\x0c`, `\\x1c`-`\\x1e`, `\\x85`, `\\u2028`): продолжение строки теряло
    префикс `+` и не сканировалось. Добавленная строка, начинающаяся с `++` (в диффе `+++ …`), принималась
    за заголовок файла и отбрасывалась. Файл «CSV (Macintosh)» целиком — одна такая строка.
    """
    term = "ТайныйПроект"
    root = tmp / "repo"
    (root / "local").mkdir(parents=True)
    (root / "local" / "private_terms.txt").write_text(term + "\n", encoding="utf-8")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    t = term.encode("utf-8")

    def commit(msg: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(git + ["commit", "-qm", msg, "--no-verify", "--allow-empty"], cwd=root, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()

    def scan(old: str, new_: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push"], cwd=root,
                              input=f"refs/heads/main {new_} refs/heads/main {old}\n",
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    (root / "чисто.md").write_text("ничего\n", encoding="utf-8")
    base = commit("основа")
    leaking = {
        "cr_only.csv": b"head\r" + "для ".encode() + t + b"\r",
        "ff.txt": b"page\x0c \xd0\xb4\xd0\xbb\xd1\x8f " + t + b"\n",
        "fs_rs.bin": b"\x00\x01\x1c\x1d\x1e " + t + b" \x00",
        "nel.txt": b"a\xc2\x85 " + t + b"\n",
        "ls.txt": b"a\xe2\x80\xa8 " + t + b"\n",
        "plus2.md": b"++ " + t + b"\n",
        "plus3.md": b"+++ " + t + b"\n",
        "plus_both.md": b"++" + t + b"++\n",
    }
    clean = {
        "ok_cr.csv": "head\rничего\r".encode(),
        "ok_plus.md": "++ ничего\n+++ b/ничего\n".encode(),
    }
    for name, data in {**leaking, **clean}.items():
        (root / name).write_bytes(data)
    head = commit("файлы")
    cp = scan(base, head)
    assert cp.returncode == 1, f"rc={cp.returncode}\n{cp.stderr}"
    for name in leaking:
        assert f"  {name} " in cp.stderr, f"{name}: название после необычного разделителя не найдено:\n{cp.stderr}"
    for name in clean:
        assert f"  {name} " not in cp.stderr, f"{name}: ложное срабатывание:\n{cp.stderr}"
    assert "Traceback" not in cp.stderr, cp.stderr

    # Строка `+++ b/…` внутри ханка — добавленный текст и не подменяет имя файла в находке.
    (root / "tricky.md").write_bytes(b"+++ b/other.md\n+++ " + t + b"\n")
    head2 = commit("заголовок внутри ханка")
    cp = scan(head, head2)
    assert cp.returncode == 1 and "  tricky.md " in cp.stderr and "  other.md " not in cp.stderr, \
        f"содержимое, похожее на заголовок файла, подменило путь или скрылось:\n{cp.stderr}"


@test
def test_push_guard_reads_merge_resolutions_and_ref_names(tmp: Path):
    """BL-SEC-14: слияние с названием в разрешении конфликта и имя ветки или тега на сервере не проходят.

    `git log -p` без `--cc` не показывает диффов слияний: строка, которую человек написал при
    разрешении конфликта, нигде больше не лежит и уезжала без проверки. Имя ветки (`fix-<название>`)
    и лёгкого тега не сканировалось — только sha, сообщения и тела аннотированных тегов.
    Содержимое родителей слияния проверяется сам по себе (оно в диапазоне), поэтому у слияния
    смотрим только то, что не совпало ни с одним из родителей.
    """
    term = "ТайныйПроект"
    root = tmp / "repo"
    (root / "local").mkdir(parents=True)
    (root / "local" / "private_terms.txt").write_text(term + "\n", encoding="utf-8")
    (root / ".gitignore").write_text("local/\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def run(*args, **kw):
        return subprocess.run(list(args), cwd=root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, **kw)

    def commit(msg: str) -> str:
        run("git", "add", "-A")
        assert run("git", "commit", "-qm", msg, "--no-verify").returncode == 0, msg
        return run("git", "rev-parse", "HEAD").stdout.strip()

    def scan(old: str, new_: str, local="refs/heads/main", remote="refs/heads/main"):
        return run(sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push",
                   input=f"{local} {new_} {remote} {old}\n")

    run("git", "init", "-q", "-b", "main")
    (root / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    commit("основа")
    run("git", "checkout", "-qb", "side")
    (root / "f.txt").write_text("a\nсторона\nc\n", encoding="utf-8")
    (root / "g.txt").write_text("только сторона\n", encoding="utf-8")
    commit("сторона")
    run("git", "checkout", "-q", "main")
    (root / "f.txt").write_text("a\nглавная\nc\n", encoding="utf-8")
    pushed = commit("главная")

    run("git", "merge", "side", "-q", "--no-commit")
    (root / "f.txt").write_text(f"a\nрешено для {term}\nc\n", encoding="utf-8")
    merged = commit("слияние")
    assert len(run("git", "rev-list", "--parents", "-1", merged).stdout.split()) == 3, "тест не про слияние"
    cp = scan(pushed, merged)
    assert cp.returncode == 1 and term in cp.stderr and "f.txt" in cp.stderr, \
        f"название в разрешении конфликта слияния уехало без проверки: rc={cp.returncode}\n{cp.stderr}"

    # То же слияние, разрешённое честно (берём сторону целиком): ничего нового, ложных срабатываний нет.
    run("git", "reset", "-q", "--hard", pushed)
    run("git", "merge", "side", "-q", "--no-commit")
    (root / "f.txt").write_text("a\nсторона\nc\n", encoding="utf-8")
    honest = commit("слияние без правок")
    cp = scan(pushed, honest)
    assert cp.returncode == 0, f"честное слияние остановлено:\n{cp.stderr}"

    # Имя ветки и лёгкого тега на сервере — публикация.
    cp = scan(pushed, honest, remote=f"refs/heads/fix-{term}")
    assert cp.returncode == 1 and "имя ссылки" in cp.stderr and "переименуйте" in cp.stderr \
        and "Файл: уберите" not in cp.stderr, f"название в имени ветки пропущено:\n{cp.stderr}"
    cp = run(sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push",
             input=f"refs/tags/v1 {honest} refs/tags/v1-{term} {'0' * 40}\n")
    assert cp.returncode == 1 and "имя ссылки" in cp.stderr, f"название в имени тега пропущено:\n{cp.stderr}"
    # Локальное имя наружу не идёт: `git push origin fix-<название>:main` его не публикует.
    cp = scan(pushed, honest, local=f"refs/heads/fix-{term}")
    assert cp.returncode == 0, f"локальное имя ветки принято за публикацию:\n{cp.stderr}"


@test
def test_push_guard_scopes_unknown_remote_sha_to_the_target_remote_and_reads_tags(tmp: Path):
    """BL-SEC-14: неизвестный remote_sha не исключает коммиты чужих remote; аннотированный тег проверяется.

    Раньше при неизвестном локально remote_sha брались коммиты, которых нет в ЛЮБОЙ remote-ветке:
    коммит с названием, уехавший в приватный remote и слитый в master, проходил в публичный.
    Исключаться должно только то, что уже есть в ветках remote, КУДА идёт пуш. Тег с названием в
    сообщении раньше уезжал без проверки: сканировались коммиты и дифф.
    """
    term = "ТайныйПроект"
    root = tmp / "repo"
    (root / "local").mkdir(parents=True)
    (root / "local" / "private_terms.txt").write_text(term + "\n", encoding="utf-8")
    (root / ".gitignore").write_text("local/\n", encoding="utf-8")     # список названий в историю не идёт
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)

    def run(*args, **kw):
        return subprocess.run(list(args), cwd=root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", **kw)

    def commit(name: str, text: str, msg: str) -> str:
        (root / name).write_text(text, encoding="utf-8")
        run("git", "add", "-A")
        assert run(*git, "commit", "-qm", msg, "--no-verify").returncode == 0
        return run("git", "rev-parse", "HEAD").stdout.strip()

    a = commit("a.md", "чисто\n", "основа")
    b = commit("b.md", f"для {term}\n", "правка")
    run("git", "update-ref", "refs/remotes/origin/main", a)      # публичный: знает только основу
    run("git", "update-ref", "refs/remotes/kitchen/dev", b)      # приватный: уже получил коммит b

    def scan(*extra: str, line: str = ""):
        return run(sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push", *extra,
                   input=line or f"refs/heads/main {b} refs/heads/main {'3' * 40}\n")

    cp = scan("--remote", "origin")
    assert cp.returncode == 1 and term in cp.stderr, \
        f"коммит, уехавший в приватный remote, прошёл в публичный: rc={cp.returncode}\n{cp.stderr}"
    assert "неизвестен локально" in cp.stderr and "origin" in cp.stderr, cp.stderr
    cp = scan()
    assert cp.returncode == 1 and "remote не назван" in cp.stderr, \
        f"remote неизвестен — проверка должна быть шире, а не уже:\n{cp.stderr}"
    assert scan("--remote", "kitchen").returncode == 0, \
        "то, что уже лежит в ветках целевого remote, проверять повторно не нужно"

    # Аннотированный тег: название в сообщении тега и в имени тэггера.
    assert run("git", "tag", "-a", "v1", a, "-m", f"выпуск для {term}", env={**os.environ, "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}).returncode == 0
    tag = run("git", "rev-parse", "v1").stdout.strip()
    cp = scan("--remote", "origin", line=f"refs/tags/v1 {tag} refs/tags/v1 {'0' * 40}\n")
    assert cp.returncode == 1 and "тег refs/tags/v1" in cp.stderr, \
        f"название в сообщении тега уехало без проверки: rc={cp.returncode}\n{cp.stderr}"
    assert "Сообщение, автор или тег" in cp.stderr and "Файл: уберите" not in cp.stderr, cp.stderr
    run("git", "tag", "-d", "v1")
    run("git", "tag", "-a", "v2", a, "-m", "выпуск", env={**os.environ, "GIT_COMMITTER_NAME": "t",
                                                       "GIT_COMMITTER_EMAIL": "t@t"})
    tag = run("git", "rev-parse", "v2").stdout.strip()
    cp = scan("--remote", "origin", line=f"refs/tags/v2 {tag} refs/tags/v2 {'0' * 40}\n")
    assert cp.returncode == 0, f"чистый тег остановлен:\n{cp.stderr}"


@test
def test_hooks_install_status_and_uninstall_cover_the_push_hook(tmp: Path):
    """BL-SEC-14: `--uninstall` снимает и pre-push, `--status` называет старый хук устаревшим.

    Раньше снимались два хука из трёх: pre-push оставался на машине, хотя инструкция говорила
    «снять оба». Хук версии 3 не передаёт скану имя remote и разбирает адрес подстрокой — он тоже
    устарел и должен быть назван так.
    """
    root = tmp / "kit"
    root.mkdir()
    (root / "engine_manifest.txt").write_text("scripts/x.py\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    cmd = [sys.executable, str(KIT / "scripts/aurora_hooks.py")]

    def hooks(*args):
        return subprocess.run(cmd + list(args), cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    cp = hooks("--install", "--mode", "warn")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    pre_push = root / ".git/hooks/pre-push"
    text = pre_push.read_text(encoding="utf-8")
    assert "# aurora-hook push v7" in text and '--scan-push --remote "$remote_name"' in text, text
    assert "--hook v7" in text, "хук не называет скану свою версию — устаревший не заметить"
    status = hooks("--status").stdout
    assert "pre-push: хук Авроры ·" in status, status
    assert "НЕ проверяется" in status, f"--status молчит, что списка названий нет:\n{status}"
    assert "НЕ проверяется" in hooks("--install", "--mode", "warn").stdout, "--install молчит, что списка нет"
    (root / "local").mkdir()
    (root / "local" / "private_terms.txt").write_text("ТайныйПроект\n", encoding="utf-8")
    assert "НЕ проверяется" not in hooks("--status").stdout
    pre_push.write_text(text.replace("push v7", "push v4"), encoding="utf-8")
    assert "УСТАРЕЛ" in hooks("--status").stdout, "хук прежней версии назван актуальным"
    # Скрипт новее хука: сам скан говорит об этом, а не молчит
    cp = subprocess.run(cmd + ["--scan-push", "--hook", "v4"], cwd=root, input="", capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    assert "старее скрипта" in cp.stderr, f"устаревший хук живёт молча:\n{cp.stderr}"
    cp = subprocess.run(cmd + ["--scan-push", "--hook", "v7"], cwd=root, input="", capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    assert "старее" not in cp.stderr, cp.stderr
    # Чужой pre-push: без --force не трогаем, с --force — резервная копия, как у остальных хуков
    pre_push.write_text("#!/bin/sh\n# мой собственный хук\n", encoding="utf-8")
    assert "pre-push.bak" in hooks("--install", "--mode", "warn").stderr, "без --force не сказано про --force"
    assert "мой собственный" in pre_push.read_text(encoding="utf-8"), "без --force чужой pre-push перезаписан"
    cp = hooks("--install", "--force", "--mode", "warn")
    bak = root / ".git/hooks/pre-push.bak"
    assert bak.is_file() and "мой собственный" in bak.read_text(encoding="utf-8"), \
        f"--force перезаписал чужой pre-push без резервной копии:\n{cp.stdout}{cp.stderr}"
    assert "pre-push.bak" in cp.stdout
    pre_push.write_text(pre_push.read_text(encoding="utf-8").replace("push v7", "push v4"), encoding="utf-8")
    assert "УСТАРЕЛ" in hooks("--status").stdout
    cp = hooks("--uninstall")
    assert "pre-push" in cp.stdout and not pre_push.exists(), \
        f"--uninstall не снял pre-push:\n{cp.stdout}{cp.stderr}"
    assert not (root / ".git/hooks/pre-commit").exists() and not (root / ".git/hooks/commit-msg").exists()


@test
def test_push_guard_survives_split_runs_escapes_and_user_git_config(tmp: Path):
    """BL-SEC-14: название не прячется в разбитых run-ах, `\\uXXXX` и настройках git пользователя.

    Word режет слово на `run`-ы (проверка орфографии, смена языка), и в XML название стоит как
    `Тайный</w:t></w:r><w:r><w:t>Проект`; `json.dump` без `ensure_ascii=False` пишет кириллицу как
    `\\u0422…`. `diff.noprefix` убирал `b/` из заголовка (путь «?», исключение vendor не действовало),
    а `i18n.logOutputEncoding=cp1251` (наследие Windows) отдавал сообщения и авторов не в UTF-8.
    """
    import importlib
    import io
    import json as _json
    import zipfile
    sys.path.insert(0, str(KIT / "scripts"))
    H = importlib.import_module("aurora_hooks")
    term = "ТайныйПроект"
    root = tmp / "repo"
    (root / "local").mkdir(parents=True)
    (root / "local" / "private_terms.txt").write_text(term + "\n", encoding="utf-8")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)

    def commit(msg: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(git + ["commit", "-qm", msg, "--no-verify", "--allow-empty"], cwd=root, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()

    def scan(old: str, new_: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push", "--hook", "v7"],
                              cwd=root, input=f"refs/heads/main {new_} refs/heads/main {old}\n",
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    (root / "чисто.md").write_text("ничего\n", encoding="utf-8")
    base = commit("основа")

    def docx(body: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("word/document.xml", '<?xml version="1.0"?><w:document><w:body>' + body + "</w:body></w:document>")
        return buf.getvalue()

    def run_(text: str) -> str:
        return f"<w:r><w:t>{text}</w:t></w:r>"

    # 1. Название, разбитое на run-ы; в конце абзаца и перед словом следующего абзаца
    split = {
        "split.docx": f"<w:p>{run_('для Тайный')}{run_('Проект')}</w:p>",
        "tail.docx": f"<w:p>{run_('Тайный')}{run_('Проект')}</w:p><w:p>{run_('Следующий')}</w:p>",
    }
    for name, body in split.items():
        (root / name).write_bytes(docx(body))
        prev = commit("добавил " + name.split(".")[0])
        cp = scan(base, prev)
        assert cp.returncode == 1 and name in cp.stderr and "ТайныйПроект" in cp.stderr, \
            f"{name}: название, разбитое на run-ы, ушло без остановки:\n{cp.returncode} {cp.stderr}"
        subprocess.run(["git", "rm", "-q", "-f", name], cwd=root, check=True)
        base = commit("убрал " + name)
    # граница абзаца остаётся границей: слова из соседних абзацев — не название
    (root / "two.docx").write_bytes(docx(f"<w:p>{run_('Тайный')}</w:p><w:p>{run_('Проект')}</w:p>"))
    two = commit("два абзаца")
    cp = scan(base, two)
    assert cp.returncode == 0, f"слова из соседних абзацев приняты за название:\n{cp.stderr}"
    base = two

    # 2. JSON с кириллицей в виде \uXXXX и числовые сущности
    (root / "state.json").write_text(_json.dumps({"project": term}) + "\n", encoding="utf-8")
    assert "\\u" in (root / "state.json").read_text(encoding="utf-8")
    st = commit("состояние")
    cp = scan(base, st)
    assert cp.returncode == 1 and "state.json" in cp.stderr, \
        f"название в \\uXXXX ушло без остановки:\n{cp.returncode} {cp.stderr}"
    subprocess.run(["git", "rm", "-q", "-f", "state.json"], cwd=root, check=True)
    base = commit("убрал состояние")
    (root / "page.html").write_text("<p>" + "".join(f"&#{ord(c)};" for c in term) + "</p>\n", encoding="utf-8")
    pg = commit("страница")
    assert scan(base, pg).returncode == 1, "название в &#NNNN; ушло без остановки"
    subprocess.run(["git", "rm", "-q", "-f", "page.html"], cwd=root, check=True)
    base = commit("убрал страницу")
    # пара суррогатов и обычный текст с процентом не ломают раскодирование
    assert H._forms("a \\ud83d\\ude00 b")[1] == "a \U0001F600 b"
    assert H._forms("скидка 100% сегодня") == ["скидка 100% сегодня"]

    # 3. Настройки git пользователя: noprefix и кодировка вывода
    subprocess.run(["git", "config", "diff.noprefix", "true"], cwd=root, check=True)
    subprocess.run(["git", "config", "diff.mnemonicPrefix", "true"], cwd=root, check=True)
    subprocess.run(["git", "config", "i18n.logOutputEncoding", "cp1251"], cwd=root, check=True)
    (root / "cockpit/vendor").mkdir(parents=True)
    (root / "cockpit/vendor/lib.min.js").write_text(f"var x='{term}';\n", encoding="utf-8")
    vend = commit("библиотека")
    cp = scan(base, vend)
    assert cp.returncode == 0, f"при diff.noprefix путь потерян, исключение vendor не сработало:\n{cp.stderr}"
    (root / "заметка.txt").write_text(f"для {term}\n", encoding="utf-8")
    note = commit("заметка")
    cp = scan(vend, note)
    assert cp.returncode == 1 and "заметка.txt" in cp.stderr and "? →" not in cp.stderr, \
        f"при diff.noprefix отчёт без имени файла:\n{cp.returncode} {cp.stderr}"
    (root / "ok.txt").write_text("ничего\n", encoding="utf-8")
    msg = commit(f"правка для {term}")
    cp = scan(note, msg)
    assert cp.returncode == 1 and "сообщение или автор" in cp.stderr, \
        f"при i18n.logOutputEncoding=cp1251 название в сообщении ушло без остановки:\n{cp.returncode} {cp.stderr}"
    # i18n.commitEncoding=cp1251 при сообщении, набранном в UTF-8: коммит несёт `encoding cp1251` поверх
    # UTF-8-байтов, и `git log --encoding=UTF-8` отдаёт мусор — читаются сырые байты объекта
    subprocess.run(["git", "config", "i18n.commitEncoding", "cp1251"], cwd=root, check=True)
    (root / "ok2.txt").write_text("ничего\n", encoding="utf-8")
    mis = commit(f"правка: {term} тут")
    obj = subprocess.run(["git", "cat-file", "commit", mis], cwd=root, capture_output=True).stdout
    assert b"encoding cp1251" in obj and term.encode("utf-8") in obj, "сценарий не воспроизвёл метку не той кодировки"
    cp = scan(msg, mis)
    assert cp.returncode == 1 and "сообщение или автор" in cp.stderr, \
        f"при i18n.commitEncoding=cp1251 и UTF-8-сообщении название ушло без остановки:\n{cp.returncode} {cp.stderr}"
    # то же для имени автора
    (root / "ok3.txt").write_text("ничего\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", f"user.name=Иванов {term}", "commit", "-qm", "чисто",
                    "--no-verify"], cwd=root, check=True)
    auth = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    cp = scan(mis, auth)
    assert cp.returncode == 1 and "сообщение или автор" in cp.stderr, \
        f"при commitEncoding=cp1251 название в имени автора ушло без остановки:\n{cp.returncode} {cp.stderr}"
    # настоящий cp1251 под своей меткой по-прежнему находится
    (root / "ok4.txt").write_text("ничего\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    real = root / "msg1251.txt"
    real.write_bytes(f"правка: {term} тут\n".encode("cp1251"))
    subprocess.run(git + ["commit", "-q", "-F", str(real), "--no-verify", "--no-edit"], cwd=root, check=True)
    cp = scan(auth, subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip())
    assert cp.returncode == 1, f"сообщение в настоящем cp1251 ушло без остановки:\n{cp.stderr}"
    subprocess.run(["git", "config", "--unset", "i18n.commitEncoding"], cwd=root, check=True)

    # 4. Теги без закрывающей «>»: разбор линейный (раньше — квадрат от размера, пуш висел)
    t0 = time.monotonic()
    H._xml_text("<a " * 200_000)
    H._xml_text("<br " * 100_000 + "</p" * 100_000)
    assert time.monotonic() - t0 < 5, "разбор тегов без «>» не линеен: большой файл с «<» повесит пуш"
    assert H._xml_text("<w:p><w:r><w:t>Тайный</w:t></w:r><w:r><w:t>Проект</w:t></w:r></w:p>") == "ТайныйПроект\n"


@test
def test_push_scan_decodes_xml_entities_survives_huge_numeric_reference_and_reads_cp1251_tag(tmp: Path):
    """BL-SEC-14: `&amp;` в тексте офисного файла, `&#` с тысячами цифр и аннотированный тег в cp1251.

    Имя с «&» лежит в XML как `&amp;`, и после вырезания тегов оставалось невидимым; `html.unescape`
    на `&#` и 4300+ цифр падает с ValueError (предел перевода в число), и пуш останавливался
    без названия файла; сообщение тега в cp1251 (`git tag -a -F`) читалось с заменой знаков.
    """
    import io
    import zipfile
    term, amp = "ТайныйПроект", "Zorblax & Ko"
    root = tmp / "repo"
    (root / "local").mkdir(parents=True)
    (root / "local" / "private_terms.txt").write_text(f"{term}\n{amp}\n", encoding="utf-8")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)

    def commit(msg: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(git + ["commit", "-qm", msg, "--no-verify", "--allow-empty"], cwd=root, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()

    def scan(ref: str, new_: str, old: str = "0" * 40) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push", "--hook", "v7"],
                              cwd=root, input=f"{ref} {new_} {ref} {old}\n",
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    (root / "чисто.md").write_text("ничего\n", encoding="utf-8")
    base = commit("основа")
    main = "refs/heads/main"

    # 1. «&amp;» в docx
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>Zorblax &amp; Ko</w:t></w:r></w:p>"
                   "</w:body></w:document>")
    (root / "amp.docx").write_bytes(buf.getvalue())
    cp = scan(main, commit("docx"), base)
    assert cp.returncode == 1 and "amp.docx" in cp.stderr, \
        f"имя с «&» (`&amp;` в XML) ушло без остановки:\n{cp.returncode} {cp.stderr}"
    subprocess.run(["git", "rm", "-q", "-f", "amp.docx"], cwd=root, check=True)
    base = commit("убрал docx")

    # 2. `&#` и 5000 цифр: не падение, а обычная проверка (и остальные строки файла проверяются)
    (root / "big.txt").write_text("x &#" + "1" * 5000 + " y\n", encoding="utf-8")
    cp = scan(main, commit("большая ссылка"), base)
    assert cp.returncode == 0 and "Traceback" not in cp.stderr, \
        f"честный файл с `&#`+5000 цифр остановил пуш:\n{cp.returncode} {cp.stderr}"
    base = commit("пауза")
    (root / "big.txt").write_text("x &#" + "1" * 5000 + f" y\nдля {term}\n", encoding="utf-8")
    cp = scan(main, commit("большая ссылка и название"), base)
    assert cp.returncode == 1 and "big.txt" in cp.stderr and "Traceback" not in cp.stderr, \
        f"после строки с огромной ссылкой название не найдено:\n{cp.returncode} {cp.stderr}"
    subprocess.run(["git", "rm", "-q", "-f", "big.txt"], cwd=root, check=True)
    base = commit("убрал big")

    # 3. Аннотированный тег с сообщением в cp1251; тот же текст в UTF-8 и чистый тег
    clean = commit("тут чисто")
    msg = root.parent / "tag1251.txt"

    def tag(name: str, raw: bytes) -> str:
        msg.write_bytes(raw)
        subprocess.run(git + ["tag", "-a", "-F", str(msg), name, clean], cwd=root, check=True)
        return subprocess.run(["git", "rev-parse", name], cwd=root, capture_output=True, text=True).stdout.strip()

    for name, raw, want in (("v1", f"выпуск {term}\n".encode("cp1251"), 1),
                            ("v2", f"выпуск {term}\n".encode("utf-8"), 1),
                            ("v3", "выпуск\n".encode("cp1251"), 0)):
        cp = scan(f"refs/tags/{name}", tag(name, raw), base)
        assert cp.returncode == want and (want == 0 or f"тег refs/tags/{name}" in cp.stderr), \
            f"тег {name} ({raw[:12]!r}): ожидали код {want}, а {cp.returncode}\n{cp.stderr}"


@test
def test_private_terms_list_is_read_by_bytes_and_unreadable_list_stops_the_push(tmp: Path):
    """BL-SEC-14: BOM и UTF-16 списка названий читаются; нечитаемый список — отказ, а не «чисто».

    Файл из «Блокнота» старых Windows несёт BOM (первое название с U+FEFF не совпадало никогда),
    из PowerShell 5.1 — UTF-16 (нули между буквами), «ANSI» русской Windows — cp1251 (кириллическая
    строка молча терялась). Хук считал список непустым и пропускал всё без единого слова.
    """
    term = "ТайныйПроект"
    root = tmp / "repo"
    (root / "local").mkdir(parents=True)
    terms = root / "local" / "private_terms.txt"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)

    def commit(msg: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(git + ["commit", "-qm", msg, "--no-verify", "--allow-empty"], cwd=root, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()

    (root / "чисто.md").write_text("ничего\n", encoding="utf-8")
    base = commit("основа")
    (root / "заметка.txt").write_text(f"для {term}\n", encoding="utf-8")
    head = commit("заметка")

    def scan(**env):
        e = {k: v for k, v in os.environ.items() if k != "AURORA_PUSH_UNVERIFIED"}
        e.update(env)
        return subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push", "--hook", "v7"],
                              cwd=root, env=e, input=f"refs/heads/main {head} refs/heads/main {base}\n",
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    readable = {
        "utf-8-sig": ("# список\r\n" + term + "\r\n").encode("utf-8-sig"),
        "utf-16": ("# список\r\n" + term + "\r\n").encode("utf-16"),            # с BOM, как у PowerShell 5.1
        "utf-8": (term + "\n").encode("utf-8"),
    }
    for kind, raw in readable.items():
        terms.write_bytes(raw)
        cp = scan()
        assert cp.returncode == 1 and "заметка.txt" in cp.stderr, \
            f"список в {kind} не читается, название не найдено:\n{cp.returncode} {cp.stderr}"
    unreadable = {
        "cp1251": ("SecondTerm\n" + term + "\n").encode("cp1251"),
        "utf-16-le без BOM": (term + "\n").encode("utf-16-le"),
        "utf-16-le без BOM и без перевода строки": term.encode("utf-16-le"),     # у кириллицы нулей нет
        "utf-16-be без BOM и без перевода строки": term.encode("utf-16-be"),
        "utf-32": (term + "\n").encode("utf-32"),
        # нулевой ширины пробел (имя скопировано из Confluence): с ним название не совпадёт никогда
        "U+200B внутри названия": (term[:5] + "\u200b" + term[5:] + "\n").encode("utf-8"),
        "U+00AD (мягкий перенос) в названии": (term[:3] + "\u00ad" + term[3:] + "\n").encode("utf-8"),
    }
    for kind, raw in unreadable.items():
        terms.write_bytes(raw)
        cp = scan()
        assert cp.returncode == 2 and "не проверена" in cp.stderr, \
            f"список в {kind} нечитаем, а пуш не остановлен:\n{cp.returncode} {cp.stderr}"
        assert scan(AURORA_PUSH_UNVERIFIED="1").returncode == 0, f"{kind}: явный обход не работает"
        # сообщение коммита: предупреждение, а не молчание; коммит не останавливается
        m = root / "msg.txt"
        m.write_text("чистое сообщение\n", encoding="utf-8")
        cp = subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-msg", str(m)],
                            cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert cp.returncode == 0 and "часть названий не проверяется" in cp.stderr, \
            f"{kind}: commit-msg молчит о нечитаемом списке:\n{cp.returncode} {cp.stderr}"
    st = subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--status"], cwd=root,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "прочитан не целиком" in st.stdout, f"--status не называет проблему со списком:\n{st.stdout}{st.stderr}"


@test
def test_push_hook_resolves_head_aliases_and_old_hooks_are_called_out(tmp: Path):
    """BL-SEC-14: `HEAD:foo`, `@:foo` и `<sha>:foo` с ветки кухни не обходят список; старый хук назван и переставляется.

    Git отдаёт хуку то, что набрал человек. Стоя на `development`, `git push origin HEAD:foo`
    давал `local_ref=HEAD`, и список по именам молчал. Хуки v2–v4 зовут скан без `--hook`
    и потому не получали предупреждения о старой версии никогда; `--refresh` (его зовёт «Обновить
    кит») переставляет только свой устаревший хук.
    """
    sh = shutil.which("sh")
    if not sh or os.name == "nt":
        return
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    H = importlib.import_module("aurora_hooks")
    fake = tmp / "bin"
    fake.mkdir()
    (fake / "python3").symlink_to(sys.executable)
    repo = tmp / "repo"
    (repo / "scripts").mkdir(parents=True)
    git = shutil.which("git")
    env = {"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def g(*a):
        return subprocess.run([git, *a], cwd=str(repo), env=env, check=True, capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout.strip()
    g("init", "-q")
    g("symbolic-ref", "HEAD", "refs/heads/development")
    g("commit", "-q", "--allow-empty", "-m", "x")
    sha = g("rev-parse", "HEAD")
    hook = repo / "pre-push"
    hook.write_text(H.PUSH_HOOK.format(marker=H.PUSH_MARKER, branches=" ".join(H.PRIVATE_BRANCHES)), encoding="utf-8")
    (repo / "scripts/aurora_hooks.py").write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
    gh = "https://github.com/x/y.git"

    def push(local: str, remote: str = "refs/heads/foo", lsha: str = sha):
        return subprocess.run([sh, str(hook), "origin", gh], cwd=str(repo), env=env,
                              input=f"{local} {lsha} {remote} {'0' * 40}\n", capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30)
    for alias in ("HEAD", "@"):
        cp = push(alias)
        assert cp.returncode == 1 and "development" in cp.stderr, \
            f"{alias}:foo со стоянием на development уехал наружу: rc={cp.returncode}\n{cp.stderr}"
    # предок текущей ветки: sha уже не кончик ни одной ветки, и по `--points-at` его не найти
    g("commit", "-q", "--allow-empty", "-m", "y")
    for alias in ("HEAD~1", "HEAD^", "@~1", "@^"):
        cp = push(alias, lsha=sha)
        assert cp.returncode == 1 and "development" in cp.stderr, \
            f"{alias}:foo со стоянием на development уехал наружу: rc={cp.returncode}\n{cp.stderr}"
    # имя ветки с суффиксом и reflog: git отдаёт хуку то, что набрано; у предка `--points-at` пуст
    g("branch", "feature")
    for alias in ("development~1", "development^", "HEAD@{1}"):
        cp = push(alias, lsha=sha)
        assert cp.returncode == 1 and "development" in cp.stderr, \
            f"{alias}:foo со стоянием на development уехал наружу: rc={cp.returncode}\n{cp.stderr}"
    cp = push("feature~1", lsha=sha)
    assert cp.returncode == 0, f"feature~1 (обычная ветка) остановлен: rc={cp.returncode}\n{cp.stderr}"
    g("branch", "-q", "-D", "feature")
    g("update-ref", "refs/heads/development", sha)
    # голый sha указывает на ветку кухни; отсоединённый HEAD — то же
    cp = push(sha)
    assert cp.returncode == 1 and "development" in cp.stderr, f"sha:foo обошёл список: {cp.stderr}"
    g("update-ref", "--no-deref", "HEAD", sha)
    assert push("HEAD").returncode == 1, "отсоединённый HEAD на ветке кухни обошёл список"
    # обычная ветка на том же коммите: HEAD разворачивается в неё, а не во все ветки на коммите
    g("update-ref", "refs/heads/main", sha)
    g("symbolic-ref", "HEAD", "refs/heads/main")
    cp = push("HEAD")
    assert cp.returncode == 0, f"HEAD с main остановлен из-за development на том же коммите:\n{cp.stderr}"
    # явное имя ссылки не сверяется с другими ветками на коммите
    assert push("refs/heads/main").returncode == 0, "явная refs/heads/main остановлена соседней веткой кухни"

    # Запись reflog и отсоединённый HEAD: у `HEAD@{N}` коммит — не обязательно текущая ветка, а у
    # отсоединённого HEAD `rev-parse --symbolic-full-name` отдаёт литерал `HEAD` (не ветку).
    tree = g("rev-parse", sha + "^{tree}")
    other = g("commit-tree", tree, "-p", sha, "-m", "z")
    g("update-ref", "refs/heads/main", other)
    g("checkout", "-q", "development")
    g("checkout", "-q", "main")          # HEAD@{1} — кончик development, стоим на main
    for alias in ("HEAD@{1}", "@{1}"):
        cp = push(alias, lsha=sha)
        assert cp.returncode == 1 and "development" in cp.stderr, \
            f"{alias}:foo на main, где {alias} — кончик development, уехал наружу: rc={cp.returncode}\n{cp.stderr}"
    cp = push("HEAD@{0}", lsha=other)
    assert cp.returncode == 0, f"HEAD@{{0}} на коммите обычной ветки остановлен:\n{cp.stderr}"
    g("checkout", "-q", "--detach", "development")
    for alias in ("HEAD^0", "HEAD~0", "HEAD@{0}", "@^0", "HEAD", "@"):
        cp = push(alias, lsha=sha)
        assert cp.returncode == 1 and "development" in cp.stderr, \
            f"{alias}:foo при отсоединённом HEAD на кончике development уехал наружу: rc={cp.returncode}\n{cp.stderr}"
    g("checkout", "-q", "--detach", "main")
    for alias in ("HEAD^0", "HEAD~0", "@^0"):
        cp = push(alias, lsha=other)
        assert cp.returncode == 0, f"{alias} при отсоединённом HEAD на коммите main остановлен:\n{cp.stderr}"
    g("checkout", "-q", "main")
    # Тег и remote-tracking на кончике development — не ветки: форма с суффиксом проверяется по коммиту
    # (раньше `refs/*` принимал `refs/tags/v1` за найденную ветку, и проверка по коммиту отключалась).
    g("tag", "v1", sha)
    g("tag", "-a", "-m", "t", "v2", sha)
    g("update-ref", "refs/remotes/priv/development", sha)
    for alias in ("v1^0", "v1~0", "v1^{commit}", "v2^0", "priv/development^0", "priv/development~0"):
        cp = push(alias, lsha=sha)
        assert cp.returncode == 1 and "development" in cp.stderr, \
            f"{alias}:foo (тег/remote-tracking на кончике development) уехал наружу: rc={cp.returncode}\n{cp.stderr}"
    # Голые теги и remote-tracking без суффикса git отдаёт хуку полным именем (`refs/tags/v1`,
    # `refs/remotes/priv/development`): они тоже не ветки, и под другим именем кончик development не уходит.
    for alias in ("refs/tags/v1", "refs/remotes/priv/development"):
        cp = push(alias, lsha=sha)
        assert cp.returncode == 1 and "development" in cp.stderr, \
            f"{alias}:foo (голый тег/remote-tracking на кончике development) уехал наружу: rc={cp.returncode}\n{cp.stderr}"
    # тег, публикуемый как тег (обычный выпуск), ветку на публичном remote не создаёт — не останавливается
    for alias in ("refs/tags/v1",):
        cp = push(alias, remote=alias, lsha=sha)
        assert cp.returncode == 0, f"выпуск тега {alias} на кончике development остановлен:\n{cp.stderr}"
    # тот же тег в любую ссылку, кроме `refs/tags/*`, — не выпуск: кончик development уехал бы под другим именем
    for dst in ("refs/custom/x", "refs/remotes/leak/x", "refs/heads/foo"):
        cp = push("refs/tags/v1", remote=dst, lsha=sha)
        assert cp.returncode == 1 and "development" in cp.stderr, \
            f"тег v1 на кончике development уехал в {dst}: rc={cp.returncode}\n{cp.stderr}"
    # Аннотированный тег: git отдаёт хуку sha объекта-тега, а не коммита, и ветки на кончике нашлись бы только
    # по развёрнутому до коммита sha (раньше `v2:refs/custom/x` уходил наружу, и кончик development с ним).
    tagsha = g("rev-parse", "refs/tags/v2")
    assert tagsha != sha, "v2 должен быть аннотированным: у него свой sha объекта"
    for dst in ("refs/custom/x", "refs/remotes/leak/x", "refs/notes/foo"):
        cp = push("refs/tags/v2", remote=dst, lsha=tagsha)
        assert cp.returncode == 1 and "development" in cp.stderr, \
            f"аннотированный тег v2 на кончике development уехал в {dst}: rc={cp.returncode}\n{cp.stderr}"
    cp = push("refs/tags/v2", remote="refs/tags/v2", lsha=tagsha)
    assert cp.returncode == 0, f"выпуск аннотированного тега v2 как тега остановлен:\n{cp.stderr}"
    g("tag", "v3", other)
    cp = push("v3^0", lsha=other)
    assert cp.returncode == 0, f"v3^0 (тег на коммите обычной ветки) остановлен:\n{cp.stderr}"
    cp = push("refs/tags/v3", lsha=other)
    assert cp.returncode == 0, f"refs/tags/v3:foo (тег на коммите обычной ветки) остановлен:\n{cp.stderr}"
    # Полное имя с суффиксом: git отдаёт как набрано, суффикс разбирается как у короткого имени
    for alias in ("refs/heads/development~1", "refs/heads/development^", "refs/heads/development^0",
                  "refs/heads/development~0", "refs/heads/development^{commit}", "refs/heads/development@{0}"):
        cp = push(alias, lsha=sha)
        assert cp.returncode == 1 and "development" in cp.stderr, \
            f"{alias}:foo уехал наружу: rc={cp.returncode}\n{cp.stderr}"
    cp = push("refs/heads/main@{1}", lsha=sha)     # запись reflog — коммит, на нём стоит development
    assert cp.returncode == 1 and "development" in cp.stderr, f"refs/heads/main@{{1}} (кончик development) ушёл:\n{cp.stderr}"
    for alias in ("refs/heads/main^0", "refs/heads/main~0"):
        cp = push(alias, lsha=other)
        assert cp.returncode == 0, f"{alias} (обычная ветка) остановлен:\n{cp.stderr}"

    # То же настоящим `git push`: git сам решает, что отдать хуку в local_ref
    real = tmp / "real"
    bare = tmp / "bare.git"
    for d in (real / ".git/hooks", real / "scripts"):
        d.mkdir(parents=True)
    subprocess.run([git, "init", "-q", "--bare", str(bare)], env=env, check=True, capture_output=True)

    def rg(*a, check=True):
        return subprocess.run([git, *a], cwd=str(real), env=env, check=check, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    rg("init", "-q")
    rg("symbolic-ref", "HEAD", "refs/heads/development")
    rg("commit", "-q", "--allow-empty", "-m", "x")
    rg("remote", "add", "origin", str(bare))
    rg("config", "aurora.publicRemotes", "origin")
    (real / "scripts/aurora_hooks.py").write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
    rhook = real / ".git/hooks/pre-push"
    rhook.write_text(H.PUSH_HOOK.format(marker=H.PUSH_MARKER, branches=" ".join(H.PRIVATE_BRANCHES)), encoding="utf-8")
    rhook.chmod(0o755)
    rg("checkout", "-q", "-b", "main")          # до этого стояли на development: HEAD@{1} — его кончик
    rg("commit", "-q", "--allow-empty", "-m", "y")
    cp = rg("push", "origin", "HEAD@{1}:refs/heads/foo", check=False)
    assert cp.returncode != 0 and "development" in cp.stderr, \
        f"git push HEAD@{{1}}:foo со стояния на main выпустил кончик development: rc={cp.returncode}\n{cp.stderr}"
    rg("checkout", "-q", "--detach", "development")
    for alias in ("HEAD^0", "HEAD~0", "HEAD@{0}", "@^0"):
        cp = rg("push", "origin", alias + ":refs/heads/foo", check=False)
        assert cp.returncode != 0 and "development" in cp.stderr, \
            f"git push {alias}:foo при отсоединённом HEAD на development ушёл наружу: rc={cp.returncode}\n{cp.stderr}"
    rg("tag", "v1", "development")
    rg("tag", "-a", "-m", "t", "v2", "development")
    rg("update-ref", "refs/remotes/priv/development", rg("rev-parse", "development").stdout.strip())
    for alias in ("v1^0", "v1~0", "v1^{commit}", "v2^0", "priv/development^0", "priv/development~0",
                  "refs/heads/development~0", "refs/heads/development^0", "refs/heads/development^{commit}",
                  "refs/heads/development@{0}",
                  # голые теги и remote-tracking: git отдаёт хуку refs/tags/v1, refs/remotes/priv/development
                  # (аннотированный тег в refs/heads/* remote всё равно отвергает: это не коммит)
                  "v1", "refs/tags/v1", "priv/development", "refs/remotes/priv/development"):
        cp = rg("push", "origin", alias + ":refs/heads/foo", check=False)
        assert cp.returncode != 0 and "development" in cp.stderr, \
            f"git push {alias}:foo (кончик development) ушёл наружу: rc={cp.returncode}\n{cp.stderr}"
    assert not rg("ls-remote", "--heads", "origin").stdout.strip(), "в публичный remote что-то уехало"
    # обычный выпуск: тег уходит как тег и не останавливается, даже стоя на кончике development
    cp = rg("push", "origin", "refs/tags/v1", check=False)
    assert cp.returncode == 0, f"выпуск тега v1 на кончике development остановлен:\n{cp.stderr}"
    assert "refs/tags/v1" in rg("ls-remote", "--tags", "origin").stdout, "тег v1 не доехал"
    assert not rg("ls-remote", "--heads", "origin").stdout.strip(), "тег создал ветку на публичном remote"
    # тег в ссылку не из `refs/tags/*` — отказ; `v1:foo` git сам превращает в `refs/tags/foo` — выпуск
    for dst in ("refs/custom/x", "refs/remotes/leak/x"):
        cp = rg("push", "origin", "v1:" + dst, check=False)
        assert cp.returncode != 0 and "development" in cp.stderr, \
            f"git push v1:{dst} (кончик development) ушёл наружу: rc={cp.returncode}\n{cp.stderr}"
    for dst in ("refs/custom/x", "refs/remotes/leak/x", "refs/notes/foo"):
        cp = rg("push", "origin", "v2:" + dst, check=False)
        assert cp.returncode != 0 and "development" in cp.stderr, \
            f"git push v2:{dst} (аннотированный тег на кончике development) ушёл наружу: rc={cp.returncode}\n{cp.stderr}"
    cp = rg("push", "origin", "v2:foo2", check=False)
    assert cp.returncode == 0, f"git push v2:foo2 (аннотированный тег становится тегом) остановлен:\n{cp.stderr}"
    cp = rg("push", "origin", "v1:foo", check=False)
    assert cp.returncode == 0, f"git push v1:foo (становится тегом foo) остановлен:\n{cp.stderr}"
    assert not rg("ls-remote", "--heads", "origin").stdout.strip(), "v1:foo создал ветку на публичном remote"
    assert not [l for l in rg("ls-remote", "origin").stdout.splitlines()
                if "refs/custom" in l or "refs/remotes" in l], "тег уехал в посторонние ссылки"
    cp = rg("push", "origin", "main:refs/heads/foo", check=False)
    assert cp.returncode == 0, f"обычная ветка main остановлена:\n{cp.stderr}"

    # скан без --hook (хуки v2–v4) сам называет хук старым
    cmd = [sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--scan-push"]
    cp = subprocess.run(cmd, cwd=str(repo), input="", capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "старее скрипта" in cp.stderr, f"хук v2–v4 (скан без --hook) живёт молча:\n{cp.stderr}"

    # --refresh: свой устаревший хук переставляется, чужой и актуальный — нет
    kit = tmp / "kit"
    kit.mkdir()
    (kit / "engine_manifest.txt").write_text("scripts/x.py\n", encoding="utf-8")
    subprocess.run([git, "init", "-q"], cwd=str(kit), check=True)
    hooks = kit / ".git/hooks"
    hooks.mkdir(exist_ok=True)
    ours = hooks / "pre-push"

    def refresh() -> str:
        return subprocess.run([sys.executable, str(KIT / "scripts/aurora_hooks.py"), "--refresh"], cwd=str(kit),
                              capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    ours.write_text("#!/bin/sh\n# aurora-hook push v2\n", encoding="utf-8")
    assert H.push_hook_state(kit / ".git") == "outdated"
    assert "переставлен" in refresh() and H.push_hook_state(kit / ".git") == "current", \
        "--refresh не переставил устаревший хук"
    assert not (hooks / "pre-commit").exists() and not (hooks / "commit-msg").exists(), \
        "--refresh тронул не только pre-push"
    assert refresh() == "", "актуальный хук переставлен повторно"
    ours.write_text("#!/bin/sh\n# мой хук\n", encoding="utf-8")
    assert H.push_hook_state(kit / ".git") == "foreign" and refresh() == "" \
        and "мой хук" in ours.read_text(encoding="utf-8"), "--refresh затёр чужой pre-push"
    ours.unlink()
    assert H.push_hook_state(kit / ".git") == "none" and refresh() == "" and not ours.exists()


@test
def test_kit_doctor_and_panel_update_see_the_outdated_own_pre_push(tmp: Path):
    """BL-SEC-14: `check_kit_hooks` (kit:doctor, «Здоровье») и `_refresh_kit_hooks` («Обновить кит»).

    Предупреждение выдаётся только про свой устаревший pre-push; текущий, чужой и отсутствующий — тишина.
    Переустановка трогает тот же единственный случай.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    H = importlib.import_module("aurora_hooks")
    ck = importlib.import_module("aurora_cockpit")
    kit = tmp / "kit"
    shutil.copytree(KIT / "scripts", kit / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    (kit / "engine_manifest.txt").write_text("scripts/x.py\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=kit, check=True)
    hook = kit / ".git/hooks/pre-push"
    hook.parent.mkdir(exist_ok=True)

    def doctor() -> list:
        code = "import sys, json; sys.path.insert(0, 'scripts'); import aurora_doctor as d; print(json.dumps(d.check_kit_hooks()))"
        cp = subprocess.run([sys.executable, "-c", code], cwd=kit, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
        assert cp.returncode == 0, cp.stderr
        return json.loads(cp.stdout.strip().splitlines()[-1])

    assert doctor() == [], "хука нет, а доктор предупреждает"
    hook.write_text("#!/bin/sh\n# мой хук\n", encoding="utf-8")
    assert doctor() == [], "чужой хук назван устаревшим"
    hook.write_text("#!/bin/sh\n" + H.PUSH_MARKER + "\n", encoding="utf-8")
    assert doctor() == [], "актуальный хук назван устаревшим"
    hook.write_text("#!/bin/sh\n# aurora-hook push v2\n", encoding="utf-8")
    warn = doctor()
    assert len(warn) == 1 and "pre-push" in warn[0] and "--install" in warn[0], f"устаревший хук не замечен: {warn}"

    keep = ck.KIT
    ck.KIT = str(kit)
    try:
        note = ck._refresh_kit_hooks()
        assert "переставлен" in note and H.push_hook_state(kit / ".git") == "current", \
            f"панель не переставила устаревший хук: {note!r}"
        assert ck._refresh_kit_hooks() == "", "актуальный хук переставлен повторно"
        assert doctor() == [], "после переустановки предупреждение осталось"
        hook.write_text("#!/bin/sh\n# мой хук\n", encoding="utf-8")
        assert ck._refresh_kit_hooks() == "" and "мой хук" in hook.read_text(encoding="utf-8"), \
            "панель затёрла чужой pre-push"
    finally:
        ck.KIT = keep


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
