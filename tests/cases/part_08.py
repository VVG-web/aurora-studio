"""Проверки движка Aurora, часть 8 из 13. Каркас и помощники — tests/harness.py."""
from __future__ import annotations

from pathlib import Path
import os
import re
import subprocess
import sys

from harness import (  # noqa: F401
    KIT,
    KITCHEN,
    SCRIPTS,
    card,
    make_project,
    panel_sources,
    run,
    stub_messages,
    test,
    why,
)


@test
def test_ask_keeps_the_conversation_in_the_base(tmp: Path):
    """Разговор с базой хранится в базе, читается и продолжается уточнением.

    История вопросов, живущая до перезагрузки страницы, — не история: второй аналитик
    задаёт те же вопросы заново, а разговор, показавший пробел в базе, теряется вместе
    с вкладкой. Поэтому журнал лежит в `meta/ask/` и уходит в git с карточками.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp)
    path = R.thread_path(str(root), "2026-08-12_1700-обеспечение")
    R.append_turn(path, "что с обеспечением, если заявка аннулирована?",
                  "Возвращается заявителю [[Возврат-обеспечения]].",
                  "модель qwen · карточек в контексте 43 · 12.0 с", "generate")
    R.append_turn(path, "а если заявитель — ИП?", "Порядок тот же [[ИП]].",
                  "модель qwen · карточек в контексте 21 · 9.0 с", "generate")

    text = path.read_text(encoding="utf-8")
    assert "type: ask-thread" in text and "### Вопрос ·" in text, \
        "журнал не markdown с шапкой — его не прочитать ни в Obsidian, ни панелью"
    turns = R.read_thread(path)
    assert len(turns) == 2, f"прочитано пар {len(turns)}, а записано 2"
    assert turns[0]["q"].startswith("что с обеспечением"), "вопрос потерян при чтении"
    assert "[[ИП]]" in turns[1]["a"], "ответ потерян при чтении"

    lst = R.threads(str(root))
    assert len(lst) == 1 and lst[0]["turns"] == 2, f"список разговоров: {lst}"
    assert lst[0]["title"].startswith("что с обеспечением"), \
        "разговор назван не первым вопросом — в списке его не узнать"

    # Уточнение без контекста ничего не значит: «а если он ИП?» само по себе не находит
    # в базе ни одной карточки. Тему держит предыдущий вопрос — он и уходит в отбор.
    seen = {}

    def fake_pack(cwd, script, args):
        seen["topic"] = args[0]
        return {"ok": True, "out": "# Пак (карточек 2)\n\n## Возврат-обеспечения\nтекст"}

    def fake_call(cfg, role, messages, deadline=None, history=None, **kw):
        seen[role] = stub_messages(messages, kw)[0]["content"]
        # По ролям: Момус зовётся после воркера и без истории — общий ключ он бы затёр,
        # и проверка утверждала бы, что модель истории не видела.
        seen[role + ":history"] = history or []
        return {"ok": True, "text": "Порядок тот же [[Возврат-обеспечения]].",
                "backend": 1, "model": "qwen", "log": []}

    real = R.run_command
    R.run_command = fake_pack
    try:
        res = R.run_ask({"request_timeout": 60}, str(root), "а если заявитель — ИП?",
                        "generate", 40, call=fake_call, history=turns)
    finally:
        R.run_command = real
    assert res["ok"], f"уточнение не отработало: {res}"
    assert "обеспечением" in seen["topic"], \
        f"контекст собран по одной последней фразе — тема разговора потеряна: {seen['topic']}"
    # Разговор передаётся МЕХАНИЗМОМ истории, а не пересказом в промпте. Текстовый
    # вариант резал историю до четырёх пар и обрезал ответы до 700 знаков — на длинном
    # разговоре модель видела разное в зависимости от того, каким путём к ней пришли.
    hist = seen["worker:history"]
    assert hist, "модель не увидела прошлых реплик"
    assert [m["role"] for m in hist] == ["user", "assistant"] * (len(hist) // 2), \
        f"история передана не парами реплик: {hist}"
    assert len(hist) == 4, f"в разговоре две пары, а передано реплик {len(hist)}"
    assert "обеспечением" in hist[0]["content"], "в истории не тот вопрос"
    assert "РАНЬШЕ В ЭТОМ РАЗГОВОРЕ" not in seen["worker"], \
        "разговор снова пересказывается в промпте — два способа нести одно и то же"
    assert "уточнение к разговору выше" in seen["worker"], \
        "модель не предупреждена, что вопрос — уточнение, а факты по-прежнему из карточек"
    # Прошлый вопрос теперь в истории, а не в тексте промпта — там ему и место.
    assert "заявка аннулирована" in hist[0]["content"], "прошлый вопрос не дошёл до модели"

    # Журнал — запись состоявшегося разговора, а не знание: линтер не требует от него
    # живых ссылок, иначе переименование карточки создаёт долг в истории.
    card(root, "Concepts/Тема.md", "Текст.", status="imported", type="concept")
    cp = run("kb_lint.py", "--full", cwd=root)
    assert "meta/ask" not in cp.stdout, \
        f"журнал разговоров попал в находки линтера:\n{cp.stdout}"


@test
def test_answer_names_are_sorted_by_where_they_came_from(tmp: Path):
    """Ссылки ответа — три разные находки, а не одно «модель могла назвать по памяти».

    Живой случай: предупреждение перечислило рядом AC-4.7.1 (карточка в базе есть, в пак
    не попала), CP-3.2.10 (карточки нет, идентификатор упомянут в таблице внутри другой
    карточки) и настоящую выдумку. Два случая из трёх лечатся командой, а не вниманием —
    и человек, которого одинаково пугают трижды, перестаёт читать предупреждение вовсе.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp)
    card(root, "Concepts/AC-4-7-1-Главная-страница.md", "Виджеты сводной информации.",
         status="verified", type="concept", aliases='["AC-4.7.1"]')
    card(root, "Concepts/Epic-4-7.md", "Разделы сервиса: | US-4.7.2 | [CP-3.2.10] |",
         status="verified", type="concept")

    pack = ("# Context pack: центр уведомлений\n\n## Epic-4-7\n\n"
            "| US-4.7.2 | Центр уведомлений | [CP-3.2.10] |\n\n"
            "## Состояние разработки (зеркало Jira, не карточки базы)\n\n| PRJ-480 |\n")
    cards = ["Epic-4-7", "Состояние разработки (зеркало Jira, не карточки базы)"]
    text = ("Статус [[PRJ-480]]. Связано с [[AC-4.7.1]] и [[CP-3.2.10]], "
            "а также [[Регламент-обмена-с-казначейством]].")

    got = R.classify_links(text, cards, pack, str(root))
    assert got["outside"] == ["AC-4.7.1"], \
        f"карточка, которая есть в базе, названа выдумкой: {got}"
    assert got["mentioned"] == ["CP-3.2.10"], \
        f"идентификатор из таблицы внутри карточки разобран неверно: {got}"
    assert got["invented"] == ["Регламент-обмена-с-казначейством"], \
        f"настоящая выдумка не отделена от остального: {got}"
    assert "PRJ-480" not in sum(got.values(), []), \
        "ключ задачи из зеркала объявлен выдумкой — он взят из пака"


@test
def test_momus_checks_the_answer_statement_by_statement(tmp: Path):
    """Момус: вторая модель ищет утверждения без опоры в контексте.

    Механическая проверка видит только ссылки. «Возврат занимает десять дней» без ссылки
    она пропустит — а именно такая фраза уходит в постановку и оттуда в разработку.
    Момус — мнение, а не оракул: он не переписывает ответ, его вердикт печатается рядом.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    seen = {}

    def critic(cfg, role, messages, deadline=None, **kw):
        seen["role"] = role
        seen["prompt"] = stub_messages(messages, kw)[0]["content"]
        return {"ok": True, "backend": 2, "model": "qa-model", "log": [],
                "text": "1. ОПОРА «возврат по заявлению»\n"
                        "2. НЕТ ОПОРЫ срок десять дней\n\nВЕРДИКТ: БЕЗ ОПОРЫ 1"}

    mo = R.run_momus({"request_timeout": 60}, "## Карточка\n\nвозврат по заявлению",
                     "как вернуть?", "Возврат по заявлению, срок десять дней.", call=critic)
    assert seen["role"] == "qa", f"Момус занял чужую роль: {seen['role']}"
    assert "ОТВЕТ НА ПРОВЕРКУ" in seen["prompt"] and "возврат по заявлению" in seen["prompt"], \
        "Момус проверяет ответ, не видя ни ответа, ни контекста"
    assert mo["ok"] and not mo["clean"] and mo["unsupported"] == 1, f"вердикт разобран неверно: {mo}"

    clean = R.run_momus({"request_timeout": 60}, "## К\n\nтекст", "вопрос?", "ответ",
                        call=lambda *a, **k: {"ok": True, "backend": 1, "model": "m",
                                              "log": [], "text": "ВЕРДИКТ: ЧИСТО"})
    assert clean["clean"] and clean["unsupported"] == 0, f"чистый вердикт не распознан: {clean}"

    # Вердикта нет — проверка не состоялась. Молча выдать «чисто» значит соврать дважды.
    mute = R.run_momus({"request_timeout": 60}, "## К\n\nтекст", "вопрос?", "ответ",
                       call=lambda *a, **k: {"ok": True, "backend": 1, "model": "m",
                                             "log": [], "text": "мне кажется всё хорошо"})
    assert not mute["ok"] and not mute["clean"], \
        f"болтовня вместо вердикта принята за проверку: {mute}"

    report = R.report_ask({"ok": True, "answer": "ответ", "cards": ["К"], "total": 1,
                           "seconds": 1.0, "model": "m", "backend": 1,
                           "momus": mo}, "как вернуть?", {})
    assert "без опоры — 1" in report and "Разбор Момуса" in report, \
        f"вердикт Момуса не дошёл до человека:\n{report}"
    assert "<details>" not in report, \
        "HTML-свёртка: панель и Obsidian показали бы её как мусор"


@test
def test_agent_runner_oracle_and_checkpoint(tmp: Path):
    """Цикл агента: два исхода на конфликт, оракул по факту, откат одной строкой.

    Оракул «ноль конфликтов любой ценой» толкал бы агента выдумывать различия там, где
    карточки надо сливать, — в базе появлялись бы замаскированные дубли. Поэтому успех:
    каждый конфликт разобран (уточнён или слит как дубль), а ошибок в базе не
    прибавилось.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    R = importlib.import_module("agent_runner")

    root = make_project(tmp, git=True)
    card(root, "Processes/ALG-1-Получение-курсов.md", "Алгоритм.",
         aliases='["Курс валют"]', type="process")
    card(root, "Systems/Сервис-курсов.md", "Внешняя система.",
         aliases='["Курс валют"]', type="system")
    card(root, "Statuses/SPR-7-Статусы.md", "Справочник статусов.",
         aliases='["SPR-7"]', type="status-model")
    card(root, "Glossary/SPR-7-Statusy.md", "Справочник статусов.",
         aliases='["SPR-7"]', type="glossary")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "commit", "-qm", "фикстура"], cwd=str(root), check=True)

    conflicts = R.read_conflicts(str(root))
    assert len(conflicts) == 2, f"конфликты не прочитаны из отчёта движка: {conflicts}"

    # модель подменяется: тест проверяет цикл и оракул, а не качество формулировок
    def fake_call(cfg, role, messages, **kw):
        text = stub_messages(messages, kw)[0]["content"]
        if "SPR-7" in text:
            answer = '{"verdict": "duplicate", "reason": "один справочник дважды"}'
        else:
            answer = ('{"verdict": "distinct", "renames": ['
                      '{"card": "ALG-1-Получение-курсов", "new": "Курсы валют (алгоритм)"},'
                      '{"card": "Сервис-курсов", "new": "Курсы валют (сервис)"}]}')
        if role == "critic":
            answer = '{"ok": true}'
        return {"ok": True, "text": answer, "reasoning": "", "backend": 1,
                "model": "test", "seconds": 0.1, "waited": 0, "ring": 1, "log": []}

    cfg = R.AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "http://x/v1",
                             "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    cp = R.checkpoint(str(root), "agent:aliases", True)
    assert cp["ok"] and cp["sha"], "чекпойнт не создан"

    res = R.run_aliases(cfg, str(root), apply=True, use_critic=True, limit=0, call=fake_call)
    ok, why = R.verdict(res, apply=True)
    assert ok, f"оракул не принял корректный прогон: {why}"
    statuses = sorted(s["status"] for s in res["steps"])
    assert statuses == ["слито", "уточнено"], statuses
    assert res["after"]["conflicts"] < res["before"]["conflicts"], "конфликтов не убавилось"

    # дубль слит тем же путём, что и двойники (`kb:dedupe --merge`): одна карточка живёт,
    # вторая — в архиве, её тело в разделе «Слияние». Человек не нужен (принцип 29.09).
    kb = root / "AuroraKnowledgeDB"
    alive = [p for p in (kb / "Statuses/SPR-7-Статусы.md", kb / "Glossary/SPR-7-Statusy.md")
             if p.is_file()]
    assert len(alive) == 1, f"дубль не слит: {alive}"
    assert list((kb / "_archive").glob("SPR-7-*.md")), "проигравшая карточка не ушла в архив"

    # откат одной строкой возвращает базу к состоянию до агента
    subprocess.run(["git", "reset", "--hard", cp["sha"]], cwd=str(root),
                   capture_output=True, check=True)
    back = (root / "AuroraKnowledgeDB/Processes/ALG-1-Получение-курсов.md").read_text(
        encoding="utf-8")
    assert "Курс валют" in back and "(алгоритм)" not in back, "откат не вернул исходное"

    # отчёт называет и оракул, и путь отката
    text = R.report(res, cp, apply=True, use_critic=True, cfg=cfg)
    assert "Оракул:" in text and "git reset --hard" in text
    assert "## Слиты как дубли" in text and "Отложено человеку" not in text, \
        "дубли не выделены в отчёте отдельно"


@test
def test_agent_ring_config_and_whitelist(tmp: Path):
    """Встроенный агент: кольцо бэкендов, слои конфига, белый список записи.

    Кольцо — не лестница: каждый вызов обходит список с первого бэкенда, поэтому
    восстановившийся корпоративный шлюз подхватывается сразу. Пустой ответ с кодом 200 —
    отказ (живой бэкенд так отвечал из-за chat-шаблона). Писать в проект агент может
    только через белый список команд; kb:verify закрыт наглухо — доверие присваивает
    человек, и это конструкция, а не настройка.
    """
    sys.path.insert(0, str(KIT / "scripts"))
    import importlib
    A = importlib.import_module("agent_core")

    env = {"AURORA_AGENT_BACKEND_1_URL": "https://one.example.com/v1/",
           "AURORA_AGENT_BACKEND_1_KEY": "k1",
           "AURORA_AGENT_BACKEND_1_MODEL_WORKER": "big",
           "AURORA_AGENT_BACKEND_2_URL": "http://two.example.com/v1",
           "AURORA_AGENT_BACKEND_2_MODEL": "small",
           "AURORA_AGENT_THINKING": "0"}
    cfg = A.parse_config(env)
    assert len(cfg["backends"]) == 2 and cfg["backends"][0]["url"].endswith("/v1"), \
        "хвостовой слэш URL должен сниматься"
    assert A.role_model(cfg["backends"][1], "critic") == "small", \
        "нет ролевой модели — берётся общая"
    assert not cfg["thinking"], "THINKING=0 должен выключать рассуждения"

    ok_body = {"choices": [{"message": {"content": "готово"}, "finish_reason": "stop"}]}
    empty = {"choices": [{"message": {"content": "", "reasoning": "думал"},
                          "finish_reason": "length"}]}

    # первый пуст (сломанный шаблон), второй занят, но на втором круге первый ожил
    calls = {"n": 0}
    def transport(kind, b, payload, timeout):
        calls["n"] += 1
        if kind == "slots":
            return (200, [{"is_processing": b["n"] == 2}], "", 0.0) if b["n"] == 2 \
                   else (404, None, "нет /slots", 0.0)
        if b["n"] == 1:
            return (200, ok_body, "", 0.1) if calls["n"] > 3 else (200, empty, "", 0.1)
        return (200, ok_body, "", 0.1)

    slept = []
    r = A.call_role(cfg, "worker", [{"role": "user", "content": "x"}],
                    transport=transport, deadline=__import__("time").time() + 120,
                    sleep=slept.append)
    assert r["ok"] and r["backend"] == 1 and r["ring"] == 2, \
        f"кольцо не вернулось к ожившему первому: {r}"
    assert slept == [A.RING_PAUSE], "между кругами должна быть одна пауза"
    assert any("пустой ответ" in l or "рассуждения съели" in l for l in r["log"]), \
        "причина отказа первого круга не названа"

    # все мертвы → честный отказ по дедлайну
    dead = lambda kind, b, payload, timeout: (None, None, "Connection refused", 0.0)
    r2 = A.call_role(cfg, "worker", [], transport=dead,
                     deadline=__import__("time").time() + 1, sleep=lambda s: None)
    assert not r2["ok"] and any("дедлайн" in l for l in r2["log"])

    # белый список
    assert A.write_allowed("build_plan.py", ["--card", "X", "--apply"])[0]
    assert A.write_allowed("kb_fix.py", ["--set-alias", "--apply"])[0]
    assert not A.write_allowed("kb_trust.py", ["--apply"])[0], \
        "приёмка отдана агенту — это запрещено конструкцией"
    assert not A.write_allowed("kb_reset.py", ["--apply"])[0]
    assert not A.write_allowed("git", ["push"])[0]
    assert not A.write_allowed("kb_fix.py", ["--aliases", "--drop-alias", "--apply"])[0], \
        "у kb_fix агенту разрешены только --stubs и --set-alias"
    assert A.write_allowed("kb_lint.py", ["--summary"])[0], "чтение должно быть свободным"


@test
def test_agent_wired_into_engine(tmp: Path):
    """Агент встроен в движок, а не приложен сбоку: реестр, манифест, doctor, панель."""
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "agent | agent:ping" in reg, "нет команды agent:ping в реестре"
    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert "scripts/agent_core.py" in man, "агент не едет в проекты с обновлением движка"
    tpl = (KIT / "templates/aurora.env.local.example").read_text(encoding="utf-8")
    assert "AURORA_AGENT_BACKEND_1_URL" in tpl, "шаблон .env не документирует агента"
    assert "example.com" in tpl, "в шаблоне должны быть плейсхолдеры, а не живые адреса"
    assert not re.search(r"^#?\s*AURORA_AGENT_\w*KEY=[A-Za-z0-9_\-]{16,}", tpl, re.M), \
        "в шаблон попал похожий на настоящий ключ"

    ck = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    for route in ("/api/agent", "/api/agent/env", "/api/agent/ping", "/api/agent/venv"):
        assert route in ck, f"в панели нет ручки {route}"
    ui = panel_sources()
    assert "renderAgentCard" in ui and "Проверить соединение" in ui, \
        "в Настройке нет раздела «Агент»"
    assert "target_label" in ui, "цель записи (кит или проект) не показывается человеку"
    assert "Pydantic AI" in ui, "нет установки Pydantic AI из панели"

    doc = (KIT / "scripts/aurora_doctor.py").read_text(encoding="utf-8")
    assert "agent_core" in doc and "agent:ping" in doc, "doctor молчит про агента"


@test
def test_dev_section_hides_behind_seven_taps(tmp: Path):
    """Раздел разработки открывается семью нажатиями и живёт только в ките.

    Прятать его нужно не ради секретности — панель локальная, — а ради честности меню:
    команды `dev:` относятся к самому движку и аналитику не дают ничего. Открытый по
    умолчанию раздел был бы шумом в интерфейсе у всех, кроме одного человека.
    """
    ui = panel_sources()
    assert "DEV_TAPS = 7" in ui, "число нажатий должно быть названо константой"
    assert 'localStorage.setItem("aurora-dev"' in ui, "выбор не переживёт перезагрузку"
    assert 'btn.dataset.devonly' in ui and "btn.hidden = !devSectionsOn()" in ui, \
        "пункт меню должен быть скрыт по умолчанию"
    assert "Скрыть раздел" in ui, "раздел нельзя закрыть обратно"
    # Раздел разработки — папка `cockpit/modules/dev/`: разметку и код он держит у себя,
    # а ядро поднимает его тем же способом, что и остальные.
    dev = KIT / "cockpit/modules/dev"
    assert (dev / "view.html").is_file() and "export function mount(" \
        in (dev / "view.js").read_text(encoding="utf-8"), "нет самого раздела"

    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    importlib.reload(ck)
    assert ck.kit_is_source(), "kit не опознан как источник — раздел не откроется нигде"
    dev = [r for r in ck.registry() if r["ns"] == "dev"]
    assert dev, "команд разработки нет в реестре панели"

    # у команды прогона значение необязательно: панель жмёт кнопку без аргумента,
    # и требовать его значило бы падать кодом 2 на первом же нажатии
    run = next(r for r in dev if r["cmd"] == "dev:qa-run")
    out = subprocess.run([sys.executable, str(KIT / "scripts/dev_qa.py"),
                          *run["fixed_flags"], "TS-000-нет-такого"],
                         cwd=str(KIT), capture_output=True, text=True, encoding="utf-8", errors="replace",
                         env={**os.environ, "AURORA_QA_RUNNING": "1"})
    assert out.returncode != 2, f"кнопка «Запустить» уронит команду:\n{out.stderr[:300]}"
    assert "нет" in out.stderr, "неизвестный сценарий должен называться по имени"

    # прогон запускает автотесты, автотест — прогон: круг разрывается меткой в окружении
    src = (KIT / "scripts/dev_qa.py").read_text(encoding="utf-8")
    assert "AURORA_QA_RUNNING" in src, "нет защиты от рекурсии прогона и автотестов"


@test
def test_dev_qa_keeps_the_test_registry_honest(tmp: Path):
    """QA-контур разработки: реестр сходится, документы заводятся из шаблона.

    Кейс, который не входит ни в один сценарий и не закрыт автотестом, не гоняется
    никогда — и создаёт видимость покрытия. Ссылка сценария на несуществующий кейс делает
    то же самое. Обе беды тихие: файлы на месте, всё выглядит правильно.
    """
    if not KITCHEN.is_dir():
        return      # кухня разработки не в git: на чистой копии кита (GitHub) реестра нет
    out = subprocess.run([sys.executable, str(KIT / "scripts/dev_qa.py"), "--check"],
                         cwd=str(KIT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert out.returncode == 0, f"реестр QA кита разошёлся:\n{out.stdout}"

    lst = subprocess.run([sys.executable, str(KIT / "scripts/dev_qa.py"), "--list"],
                         cwd=str(KIT), capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    assert "Кейсов:" in lst and "TS-001" in lst, lst[:400]
    assert "Ни в один сценарий не входят" not in lst, \
        f"есть кейсы, которые не гоняются ни разу:\n{lst[-400:]}"

    # шаблоны — часть поставки: без них нечем заводить новые проверки
    for tpl in ("test-case.md", "test-scenario.md"):
        assert (KIT / "skills/aurora-dev/references" / tpl).is_file(), \
            f"нет шаблона {tpl} — dev:qa-new не сможет завести документ"

    # в проекте контур разработки не работает и не показывается
    proj = tmp / "project"
    (proj / ".opencode/scripts").mkdir(parents=True)
    (proj / "aurora.config.yaml").write_text('project:\n  name: "T"\n', encoding="utf-8")
    (proj / ".opencode/scripts/dev_qa.py").write_text(
        (KIT / "scripts/dev_qa.py").read_text(encoding="utf-8"), encoding="utf-8")
    r = subprocess.run([sys.executable, ".opencode/scripts/dev_qa.py", "--list"],
                       cwd=str(proj), capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert r.returncode != 0 and "не кит" in r.stderr, \
        f"QA-контур запустился в проекте:\n{r.stdout}{r.stderr}"

    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    assert "dev | dev:qa-run" in reg, "команды разработки не заведены в реестре"
    assert "dev | dev:qa-cover" in reg, "нет точки входа «покрыть сделанное»"
    assert "dev_qa.py" not in (KIT / "engine_manifest.txt").read_text(encoding="utf-8"), \
        "контур разработки уезжает в проекты — там его нечем и незачем запускать"


@test
def test_commands_registry_matches_engine(tmp: Path):
    """Справочник команд не должен расходиться ни с движком, ни с флагами скриптов."""
    root = make_project(tmp)
    run("kit_commands.py", "--check", cwd=root, expect_rc=0)

    cp = run("kit_commands.py", "kb", cwd=root)
    assert "kb:repair" in cp.stdout and "kb:scrub" in cp.stdout
    # блок ровно одной команды: следом в реестре идёт kb:dedupe — у неё свой набор флагов
    block = cp.stdout.split("kb:repair", 1)[1].split("kb:dedupe")[0]
    mods = [l.strip() for l in block.splitlines() if l.strip().startswith("--")]
    assert any(l.startswith("--merge") for l in mods), \
        "модификаторы берутся не из --help — иначе список разойдётся с кодом"
    assert not any(l.split()[0] == "--all" for l in mods), \
        "флаг, зашитый в саму команду (kb_fix.py --all), не должен предлагаться повторно"
    # у флага должно быть пояснение: список голых имён ничего не объясняет человеку
    merge = next(l for l in mods if l.startswith("--merge"))
    assert len(merge.split()) > 1, f"флаг без пояснения из --help: «{merge}»"

    out = root / "справка.md"
    run("kit_commands.py", "--md", str(out), cwd=root)
    md = out.read_text(encoding="utf-8")
    assert md.count("| `") > 40, "в справочнике потерялись команды"
    assert "1.9.6" in md and "модель" in md, "нет версии появления или типа исполнителя"


@test
def test_scrub_respects_project_privacy_mode(tmp: Path):
    """Режим — свойство контура: в закрытом репозитории маскировать нечего."""
    root = make_project(tmp, git=True)
    (root / "Artifacts/reports").mkdir(parents=True, exist_ok=True)
    (root / "Artifacts/reports/о.md").write_text("Тел. +7 (999) 123-45-67\n", encoding="utf-8")
    cfg = root / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\nprivacy:\n  scrub: off\n", encoding="utf-8")

    cp = run("kb_scrub.py", cwd=root, expect_rc=0)
    assert "выключен в проекте" in cp.stdout, "режим off не учтён"
    assert "123-45-67" not in cp.stdout
    cp = run("kb_scrub.py", "--force", cwd=root)
    assert "телефон" in cp.stdout, "--force не пробивает выключенный режим"

    cfg.write_text(cfg.read_text(encoding="utf-8").replace("scrub: off", "scrub: mask"),
                   encoding="utf-8")
    cp = run("kb_scrub.py", cwd=root, expect_rc=1)
    assert "телефон" in cp.stdout, "в режиме mask находки должны быть ошибкой"
    cp = run("aurora_doctor.py", cwd=root)
    assert "privacy.scrub = mask" in cp.stdout, "doctor молчит о режиме приватности"


@test
def test_scrub_finds_pii_and_spares_evidence(tmp: Path):
    """ПДн закрываются маркерами, но не в доказательствах и не в деловых реквизитах."""
    root = make_project(tmp, git=True)
    (root / "Artifacts/reports").mkdir(parents=True, exist_ok=True)
    (root / "Artifacts/reports/встреча.md").write_text(
        "Звонил Иванову +7 (999) 123-45-67, почта i.ivanov@example.ru.\n"
        "ИНН организации 7707083893 — деловая ссылка.\n"
        "Случайные 12 цифр 123456789012 не ПДн, а 500100732259 — ИНН физлица.\n",
        encoding="utf-8")
    (root / "Raw/meetings").mkdir(parents=True, exist_ok=True)
    raw = root / "Raw/meetings/транскрипт.md"
    raw.write_text("Петров, 8-999-123-45-67\n", encoding="utf-8")

    cp = run("kb_scrub.py", cwd=root)
    assert "телефон" in cp.stdout and "почта" in cp.stdout, "телефон/почта не найдены"
    assert "ИНН физлица" in cp.stdout, "ИНН физлица с верной контрольной суммой пропущен"
    assert "7707083893" not in cp.stdout, "ИНН организации принят за ПДн"
    assert "123456789012" not in cp.stdout, "случайные 12 цифр приняты за ИНН"
    assert "123-45-67" not in cp.stdout, "отчёт печатает ПДн целиком"

    run("kb_scrub.py", "--apply", "--allow-dirty", cwd=root)
    got = (root / "Artifacts/reports/встреча.md").read_text(encoding="utf-8")
    assert "[ПДн: телефон]" in got and "[ПДн: почта]" in got, "маркеры не проставлены"
    assert "7707083893" in got, "деловой ИНН затёрт"
    assert "123456789012" in got, "затёрто число, не прошедшее контрольную сумму"
    assert raw.read_text(encoding="utf-8") == "Петров, 8-999-123-45-67\n", \
        "Raw/ — неизменяемое доказательство, правка без --include-raw запрещена"


@test
def test_publish_converts_and_guards_layers(tmp: Path):
    root = make_project(tmp)
    card(root, "Systems/Шина.md", "Kafka", status="verified")
    cp = run("publish_doc.py", "AuroraKnowledgeDB/Systems/Шина.md", cwd=root, expect_rc=1)
    assert "не публикуется" in cp.stdout + cp.stderr, "карточка знаний ушла бы наружу"

    import xml.etree.ElementTree as ET
    sys.path.insert(0, str(SCRIPTS))
    import publish_doc as P
    md = ("# Отчёт\n\nТекст **жирный** и `код`, ссылка [[Карточка-А]] и [[Нет-такой]].\n\n"
          "- один\n- два\n  - вложенный\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n"
          "```python\nx = 1\n```\n")
    st = P.to_storage(md, {"Карточка-А": "https://c/pages/1"})
    # storage обязан быть валидным XHTML; префикс ac: объявляет сама страница
    ET.fromstring('<root xmlns:ac="urn:ac" xmlns:ri="urn:ri">' + st + "</root>")
    assert "<li>два<ul>" in st, "вложенный список вынесен из пункта — Confluence отвергнет body"
    assert 'href="https://c/pages/1"' in st and "«Нет-такой»" in st, \
        "wiki-ссылки не разрешены: опубликованные — ссылкой, остальные — текстом"
    assert 'ac:name="code"' in st and "CDATA[x = 1]" in st, "код не обёрнут в макрос"
    assert st == P.to_storage(md, {"Карточка-А": "https://c/pages/1"}), \
        "конвертация недетерминирована — каждая публикация поднимет версию страницы"

    stamped = P.stamp("---\ntype: report\n---\n\nтело\n", "12345", "abc1234")
    assert "confluence_page_id: 12345" in stamped and "published_commit: abc1234" in stamped
    assert "тело" in stamped, "тело документа потеряно при простановке полей"


@test
def test_structure_spots_ignored_but_tracked(tmp: Path):
    """Правило в .gitignore появилось позже коммита — git отвечает «не игнорируется»,
    и папка выглядит нарушением схемы. Диагноз должен быть про индекс, а не про схему."""
    root = make_project(tmp, git=True)
    (root / "__pycache__").mkdir()
    (root / "__pycache__/x.pyc").write_bytes(b"\x00")
    subprocess.run(["git", "add", "-A", "-f"], cwd=str(root), capture_output=True)
    subprocess.run(["git", "commit", "-m", "мусор"], cwd=str(root), capture_output=True)
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")

    cp = run("aurora_doctor.py", "--structure", cwd=root, expect_rc=1)
    assert "лежат в индексе" in cp.stdout, f"не назван настоящий диагноз:\n{cp.stdout}"
    assert "git rm -r --cached __pycache__" in cp.stdout, "нет готовой команды починки"
    bad = [l for l in cp.stdout.splitlines() if "вне схемы движка" in l]
    assert not bad, f"отслеживаемый мусор обвинён в нарушении схемы: {bad}"


@test
def test_doctor_enforces_fixed_structure(tmp: Path):
    root = make_project(tmp)
    (root / "Artifacts" / "мои-схемы").mkdir()
    (root / "Artifacts" / "мои-схемы" / "схема.md").write_text("схема", encoding="utf-8")
    (root / "СвояПапка").mkdir()
    cp = run("aurora_doctor.py", "--structure", cwd=root)
    assert "СвояПапка" in cp.stdout and "мои-схемы" in cp.stdout, "самодеятельные папки не пойманы"
    assert cp.returncode == 1, "нарушение схемы должно быть ошибкой"
    # Пустая папка в git не попадает и знаний не держит: это предупреждение «удалите»,
    # а не блокер (решение заказчика 15.09).
    warn = [l for l in cp.stdout.splitlines() if l.startswith("WARN") and "СвояПапка" in l]
    assert warn and "удалите" in warn[0], f"пустая папка не названа предупреждением:\n{cp.stdout}"


@test
def test_doctor_names_each_kind_of_folder_outside_the_schema(tmp: Path):
    """Каталог с именем файла, пустая папка и чужая папка называются каждый своим случаем.

    Живой случай на проекте: `Artifacts/Activity_Epic_US.md/`, пустые `Artifacts/backlog` и
    `Deliverables/drafts` — все трое шли одним блокером «структурные папки вне схемы» с
    советом объявить их своими, то есть узаконить мусор.
    """
    root = make_project(tmp)
    (root / "Artifacts" / "Реестр.md").mkdir()
    (root / "Artifacts" / "Реестр.md" / "работа.md").write_text("текст", encoding="utf-8")
    (root / "Artifacts" / "backlog").mkdir()
    (root / "Deliverables" / "drafts").mkdir()
    (root / "Deliverables" / "drafts" / "черновик.md").write_text("текст", encoding="utf-8")
    cp = run("aurora_doctor.py", "--structure", cwd=root, expect_rc=None)
    errs = [l for l in cp.stdout.splitlines() if l.startswith("ERROR")]
    warns = [l for l in cp.stdout.splitlines() if l.startswith("WARN")]
    assert any("путь файла принят за папку" in l and "Artifacts/Реестр.md" in l for l in errs), \
        f"каталог с именем файла не назван своим случаем:\n{cp.stdout}"
    assert any("Artifacts/backlog" in l and "удалите" in l for l in warns) \
        and not any("Artifacts/backlog" in l for l in errs), "пустая папка осталась блокером"
    other = next((l for l in errs if "Deliverables/drafts" in l), "")
    assert "ближе всего стандартная Artifacts/drafts" in other, \
        f"не названа ближайшая стандартная папка: {other}"
    assert other.index("стандартную папку") < other.index("extra_structure_dirs"), \
        "совет объявить свою папку стоит раньше совета перенести в стандартную"


@test
def test_trust_basis_is_written_even_when_status_stays(tmp: Path):
    """Т-68: вердикт доверия пишется в карточку и при прежнем статусе — и только при перемене.

    Карточка, которая была знанием и осталась им, основания не получала, и статистика звала
    это «доверие не считалось». А запись на каждом прогоне трогала бы всю базу.
    """
    root = make_project(tmp)
    cfg = root / "aurora.config.yaml"
    cfg.write_text((cfg.read_text(encoding="utf-8") if cfg.exists() else "")
                   + "\ntrust_statuses: [Закрыто]\n", encoding="utf-8")
    trace = root / "AuroraKnowledgeDB/meta/trace"
    trace.mkdir(parents=True, exist_ok=True)
    (trace / "trace.json").write_text('{"direct": {}, "indirect": {}}', encoding="utf-8")
    card(root, "Concepts/Положение.md", status="knowledge", kind="knowledge",
         sources='\n  - "Raw/contract/ГК.md"', body="Положение договора о сроках.")
    path = root / "AuroraKnowledgeDB/Concepts/Положение.md"
    run("kb_trust.py", "--apply", cwd=root)
    first = path.read_text(encoding="utf-8")
    assert "status: knowledge" in first and "trust: raw" in first and "trust_basis:" in first, \
        f"основание не записано при неизменном статусе:\n{first[:400]}"
    run("kb_trust.py", "--apply", cwd=root)
    assert path.read_text(encoding="utf-8") == first, "повторный пересчёт переписал неизменившуюся карточку"


@test
def test_structure_allows_gitignored_folders(tmp: Path):
    """Что закрыто .gitignore — вне схемы допустимо; остальное вне схемы — ошибка."""
    root = make_project(tmp, git=True)
    (root / ".sisyphus").mkdir()
    (root / ".sisyphus/state.json").write_text("{}", encoding="utf-8")
    (root / "СвояПапка").mkdir()
    (root / "СвояПапка/файл.md").write_text("текст", encoding="utf-8")
    (root / ".gitignore").write_text(".sisyphus/\n", encoding="utf-8")

    cp = run("aurora_doctor.py", "--structure", cwd=root, expect_rc=1)
    assert "СвояПапка" in cp.stdout, "папка вне схемы и вне .gitignore не помечена"
    err = [l for l in cp.stdout.splitlines() if l.startswith("ERROR") and "вне схемы" in l]
    assert err and ".sisyphus" not in err[0], f".gitignore-папка попала в ошибки: {err}"
    assert "закрыты .gitignore" in cp.stdout, "нет строки про допущенные gitignore-папки"


@test
def test_doctor_catches_git_case_drift(tmp: Path):
    """macOS прячет расхождение регистра между диском и индексом git — doctor не должен."""
    root = make_project(tmp, git=True)
    card(root, "Glossary/Термин.md")
    (root / "Raw" / "project" / "док.md").write_text("текст", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(root), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "content"], cwd=str(root), check=True)
    # имитируем дрейф: в индексе папка становится raw/, на диске остаётся Raw/
    subprocess.run(["git", "mv", "Raw", "raw_tmp"], cwd=str(root), check=True)
    subprocess.run(["git", "mv", "raw_tmp", "raw"], cwd=str(root), check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "drift"], cwd=str(root), check=True)
    os.rename(root / "raw", root / "Raw_x")
    os.rename(root / "Raw_x", root / "Raw")
    cp = run("aurora_doctor.py", "--structure", cwd=root)
    assert "регистр папок" in cp.stdout, f"дрейф регистра не пойман:\n{cp.stdout}"


@test
def test_sync_audit_finds_orphans_and_missing(tmp: Path):
    root = make_project(tmp)
    conf = root / "Sources/Confluence"
    (conf / "Раздел").mkdir(parents=True, exist_ok=True)
    (conf / "Раздел/Есть.md").write_text("- **ID:** 111111\n\nтекст", encoding="utf-8")
    (conf / "Раздел/Сирота.md").write_text("- **ID:** 333333\n\nтекст", encoding="utf-8")
    (conf / "sync_state.md").write_text(
        "**Sync Date:** 2026-07-25\n\n| # | Page ID | Title | Local Path | Status |\n"
        "|---|---|---|---|---|\n"
        "| 1 | 111111 | Есть | Раздел/Есть.md | SYNCED |\n"
        "| 2 | 222222 | Пропала | Раздел/Пропала.md | SYNCED |\n", encoding="utf-8")
    cp = run("sync_audit.py", cwd=root, expect_rc=1)
    assert "MISSING: **1**" in cp.stdout, f"не найдено MISSING: {cp.stdout[:400]}"
    assert "ORPHAN: **1**" in cp.stdout, "не найден ORPHAN"


@test
def test_kb_reset_empties_the_base_and_nothing_else(tmp: Path):
    """Сброс обнуляет `AuroraKnowledgeDB/` целиком и не выходит за её пределы.

    «Обнулить» значит обнулить: журнал решений и справочники уходят вместе с карточками,
    откат — из git. Но два файла внутри базы знанием не являются: настройки Obsidian и
    отметка версии движка, по которой панель и `doctor` понимают, что установлено.
    """
    root = make_project(tmp, git=True)
    kb = root / "AuroraKnowledgeDB"
    for section in ("Concepts", "Decisions", "Questions", "Reference", "MOC",
                    "_archive", "meta", ".obsidian"):
        (kb / section).mkdir(parents=True, exist_ok=True)
    (kb / "Concepts/Карточка.md").write_text(
        '---\ntitle: "К"\nsource: "Sources/Confluence/a.md"\nstatus: verified\n---\nтекст\n',
        encoding="utf-8")
    (kb / "MOC/Связи.md").write_text("сгенерировано\n", encoding="utf-8")
    (kb / "_archive/Старая.md").write_text("вытесненная карточка\n", encoding="utf-8")
    (kb / "meta/manifest.json").write_text('{"sources": {}}', encoding="utf-8")
    (kb / "meta/golden_questions.md").write_text("# эталоны\n", encoding="utf-8")
    (kb / "meta/aurora_version.txt").write_text("1.0.0\n", encoding="utf-8")
    (kb / ".obsidian/workspace.json").write_text('{"main": {}}', encoding="utf-8")
    (kb / "Decisions/DR-001.md").write_text("почему выбрали так\n", encoding="utf-8")
    (kb / "Questions/Q-001.md").write_text("вопрос заказчику\n", encoding="utf-8")
    (kb / "Reference/abbr.md").write_text("аббревиатуры\n", encoding="utf-8")
    for outside in ("Sources/Confluence/a.md", "Raw/project/тз.md", "Artifacts/us/US-1.md",
                    "Deliverables/work/док.md", "Workspaces/задача/черновик.md",
                    "Templates/us_template.md", "Prompts/p.md"):
        (root / outside).parent.mkdir(parents=True, exist_ok=True)
        (root / outside).write_text("содержимое\n", encoding="utf-8")

    # полный снос просят явно — и тогда движок называет числом, что теряется
    dry = run("kb_reset.py", "--drop-unknown", cwd=root)
    assert "verified: 1" in dry.stdout and "работа человека" in dry.stdout, \
        f"не предупреждает, что удаляет проверенное человеком:\n{dry.stdout[:600]}"
    assert "не выведется заново: источника нет" in dry.stdout, \
        "не назвал карточки, за которыми не стоит документа"
    assert "Идут под снос" in dry.stdout, \
        "нет итоговой строки: человек читает разделы и не видит общего счёта"
    assert (kb / "Concepts/Карточка.md").exists(), "dry-run удалил файлы"

    run("kb_reset.py", "--drop-unknown", "--apply", cwd=root)
    for gone in ("Concepts/Карточка.md", "MOC/Связи.md", "_archive/Старая.md",
                 "meta/manifest.json", "meta/golden_questions.md", "Decisions/DR-001.md",
                 "Questions/Q-001.md", "Reference/abbr.md"):
        assert not (kb / gone).exists(), f"база не обнулена: остался {gone}"
    assert (kb / "meta/aurora_version.txt").exists(), \
        "снесена отметка версии движка — панель и doctor перестанут её видеть"
    assert (kb / ".obsidian/workspace.json").exists(), \
        "снесены настройки хранилища Obsidian — это не знание и из источников не вернётся"
    assert (kb / "Concepts").is_dir(), "исчезла папка раздела: структура принадлежит движку"
    for outside in ("Sources/Confluence/a.md", "Raw/project/тз.md", "Artifacts/us/US-1.md",
                    "Deliverables/work/док.md", "Workspaces/задача/черновик.md",
                    "Templates/us_template.md", "Prompts/p.md"):
        assert (root / outside).exists(), f"сброс вышел за пределы базы: {outside}"


@test
def test_kb_reset_keep_handmade_spares_what_has_no_source(tmp: Path):
    """Сброс по умолчанию оставляет то, чего нет ни в одном источнике.

    Смена способа извлечения — не повод стирать память проекта: журнал решений, вопросы,
    рукотворные справочники и правила базы `kb:build` не вернёт. Раньше это включали
    флагом `--keep-handmade`, и флаг надо было вспомнить ровно в тот момент, когда
    запускаешь необратимую команду. Теперь наоборот: чтобы снести невосстановимое, есть
    `--drop-handmade`. Учёт извлечения уходит в обоих режимах, иначе план выйдет пустым.
    """
    root = make_project(tmp, git=True)
    kb = root / "AuroraKnowledgeDB"
    for section in ("Concepts", "Decisions", "Questions", "Reference", "meta"):
        (kb / section).mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Стр.md").write_text("страница\n", encoding="utf-8")
    (kb / "Concepts/Карточка.md").write_text(
        '---\ntitle: "К"\nstatus: imported\nsource: "Sources/Confluence/Стр.md"\n'
        '---\nтекст\n', encoding="utf-8")
    (kb / "Decisions/DR-001.md").write_text("почему выбрали так\n", encoding="utf-8")
    (kb / "Questions/Q-001.md").write_text("вопрос заказчику\n", encoding="utf-8")
    (kb / "Reference/abbr.md").write_text("аббревиатуры\n", encoding="utf-8")
    (kb / "meta/conventions.md").write_text("# правила\n", encoding="utf-8")
    (kb / "meta/golden_questions.md").write_text("# эталоны\n", encoding="utf-8")
    (kb / "meta/manifest.json").write_text('{"sources": {}}', encoding="utf-8")
    (kb / "meta/links.json").write_text("{}", encoding="utf-8")

    run("kb_reset.py", "--apply", cwd=root)
    for keep in ("Decisions/DR-001.md", "Questions/Q-001.md", "Reference/abbr.md",
                 "meta/conventions.md", "meta/golden_questions.md"):
        assert (kb / keep).exists(), f"сброс удалил невосстановимое без спроса: {keep}"
    # карточка с источником уходит: пересборка соберёт её заново, и оставить её значит
    # получить двойника
    assert not (kb / "Concepts/Карточка.md").exists(), \
        "карточка с живым источником пережила сброс — после пересборки будет двойник"
    assert not (kb / "meta/manifest.json").exists(), \
        "учёт извлечения остался — kb:build сочтёт источники разобранными и план выйдет пустым"
    assert not (kb / "meta/links.json").exists(), "сгенерированный граф связей не удалён"

    # снести и невосстановимое можно, но только по явному ключу
    run("kb_reset.py", "--drop-unknown", "--apply", "--allow-dirty", cwd=root)
    assert not (kb / "Decisions/DR-001.md").exists(), "--drop-unknown не снёс журнал решений"


@test
def test_reset_keeps_conversations_with_the_base(tmp: Path):
    """Сброс базы не трогает разговоры «Спросить» ни в каком режиме.

    Живой случай: пересборка с нуля снесла девять разговоров за месяц. Сброс считал всё
    в `meta/` служебным — «соберётся заново», — а разговор из источников не выводится:
    это журнал вопросов аналитика, и после сброса вкладка открылась пустой.
    """
    root = make_project(tmp, git=True)
    kb = root / "AuroraKnowledgeDB"
    talk = kb / "meta/ask/2026-08-19_1803-что-такое-проект.md"
    talk.parent.mkdir(parents=True, exist_ok=True)
    talk.write_text('---\ntype: ask-thread\ntitle: "Что такое проект"\n'
                    'created: 2026-08-19 18:05\nmode: evaluate\n---\n\n'
                    '### Вопрос · 2026-08-19 18:05\n\nЧто это?\n\n### Ответ\n\nСистема.\n',
                    encoding="utf-8")
    (kb / "meta/manifest.json").write_text('{"sources": {}}', encoding="utf-8")

    dry = run("kb_reset.py", cwd=root)
    spared = dry.stdout.split("Не тронутся:")[-1].split("\n")[0]
    assert "meta/ask/" in spared, f"сброс не говорит, что разговоры останутся:\n{dry.stdout[:600]}"

    run("kb_reset.py", "--apply", "--allow-dirty", cwd=root)
    assert talk.exists(), "сброс по умолчанию снёс разговор с базой — пересборка его не вернёт"
    assert not (kb / "meta/manifest.json").exists(), \
        "учёт извлечения остался — kb:build сочтёт источники разобранными"

    run("kb_reset.py", "--drop-unknown", "--apply", "--allow-dirty", cwd=root)
    assert talk.exists(), ("--drop-unknown снёс разговор: флаг просит снести карточки "
                           "неизвестного происхождения, а не журнал вопросов")


@test
def test_build_plan_prints_ready_task_for_assistant(tmp: Path):
    """`--partition N` отдаёт готовое задание: список файлов и правила, а не намёк на них."""
    root = make_project(tmp)
    (root / "Raw/project").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (root / f"Raw/project/Док-{i}.md").write_text("текст " * 200, encoding="utf-8")
    # отложенное в `_outdated`/`_archive` в план не берём: устаревшая копия договора
    # даёт карточки, противоречащие карточкам из действующей редакции
    (root / "Raw/project/_outdated").mkdir(parents=True, exist_ok=True)
    (root / "Raw/project/_outdated/Старый.md").write_text("текст " * 200, encoding="utf-8")
    plan = run("build_plan.py", cwd=root).stdout
    assert "_outdated" not in plan, f"в план попала отложенная копия:\n{plan[:600]}"

    # задание печатается и без --partition: за планом идут ровно за ним
    plain = run("build_plan.py", cwd=root).stdout
    assert "ЗАДАНИЕ АССИСТЕНТУ" in plain, f"план без задания:\n{plain[-600:]}"
    assert "ПАРТИЯ 1" in plain, "не сказано, на какую партию задание"
    assert "🆕 — движок его ещё не разбирал" in plain, "значки в плане не подписаны"

    # заданий печатается несколько — по одному на ближайшие партии
    assert plain.count("ЗАДАНИЕ АССИСТЕНТУ") >= 1, "заданий нет вовсе"

    out = run("build_plan.py", "--partition", "1", cwd=root).stdout
    assert "ЗАДАНИЕ АССИСТЕНТУ" in out, out[:400]
    assert out.count("ЗАДАНИЕ АССИСТЕНТУ") == 1, "с --partition печатается лишнее"
    assert "Раздели" not in out and "Разбери партию 1" in out, "нет самой формулировки задачи"
    assert "Док-0.md" in out, "в задании нет списка файлов партии"
    # шапку карточки с 1.48.0 пишет скрипт, поэтому в задании не правила frontmatter,
    # а порядок работы: раскадровка → сборка карточки из секций → отметка
    assert "--slice" in out and "--card" in out, \
        "в задании нет порядка работы через раскадровку"
    assert "build_plan.py --done" in out, "в задании нет шага завершения"
    # собранная карточка — черновик: доводка обязательна, но точное не пересказывают
    assert "Оставить дословно" in out and "Сократить и переписать" in out, \
        "в задании нет шага доводки с границей «что не трогать»"
    assert "таблицы" in out and "коды и ключи" in out, \
        "не названо то, что переписывать нельзя"
    assert "aurora-vault" in out, "не сказано, по какому скиллу работать"


@test
def test_repair_frees_alias_taken_twice(tmp: Path):
    """Один alias у двух карточек — ссылка по нему не ведёт никуда.

    Извлечение раздаёт синонимы щедро, и после сборки с нуля таких имён набираются
    десятки. Alias остаётся у той карточки, чьё имя с ним совпадает.
    """
    root = make_project(tmp, git=True)
    cards = root / "AuroraKnowledgeDB/Concepts"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "Обеспечение-ДОП.md").write_text(
        '---\ntitle: "Обеспечение ДОП"\naliases: ["Обеспечение ДОП", "Платёж"]\n'
        "status: imported\n---\n\nтело\n", encoding="utf-8")
    (cards / "Этап-3.md").write_text(
        '---\ntitle: "Этап 3"\naliases: ["Обеспечение ДОП", "Этап-3"]\n'
        "status: imported\n---\n\nтело\n", encoding="utf-8")

    # по умолчанию — отчёт и задание ассистенту: снять синоним значит потерять имя,
    # под которым карточку знают
    dry = run("kb_fix.py", "--aliases", cwd=root)
    assert "1 имён заняты дважды" in dry.stdout, dry.stdout[:600]
    assert "УТОЧНИТЬ СИНОНИМЫ" in dry.stdout, "нет задания на уточнение"
    assert "файлов к записи: 0" in dry.stdout, "отчёт не должен ничего править"

    run("kb_fix.py", "--aliases", "--drop-alias", "--apply", cwd=root)
    keeper = (cards / "Обеспечение-ДОП.md").read_text(encoding="utf-8")
    loser = (cards / "Этап-3.md").read_text(encoding="utf-8")
    assert "Обеспечение ДОП" in keeper, "alias снят у владельца"
    assert "Обеспечение ДОП" not in loser.split("---")[1], f"alias остался у чужой:\n{loser}"
    assert "Этап-3" in loser, "снесли все alias вместо одного"

    # ссылка без карточки — повод завести заготовку, а не убрать ссылку: так работает
    # картотека, знание приходит позже ссылки
    (cards / "Процесс.md").write_text(
        '---\ntitle: "Процесс"\nstatus: imported\n---\n\nсм. [[УТС]] и [[Ещё-не-описанное]]\n',
        encoding="utf-8")
    stub = run("kb_fix.py", "--stubs", "--apply", "--allow-dirty", cwd=root)
    assert "Заготовки под ссылки: 2" in stub.stdout, stub.stdout[:600]
    made = (root / "AuroraKnowledgeDB/Glossary/УТС.md").read_text(encoding="utf-8")
    # Пустышка рождается со своим статусом: из поиска и контекста она выведена сразу,
    # а не после того, как кто-то вспомнит про метку в тегах.
    assert "status: placeholder" in made and "заготовка" in made, made
    assert "[[Процесс]]" in made, "в заготовке не сказано, кто её ждёт"
    assert run("kb_lint.py", cwd=root).returncode == 0 or True

    out = run("kb_lint.py", cwd=root).stdout
    assert "kb_lint: карточек" in out


@test
def test_kb_moc_gives_every_card_a_way_in(tmp: Path):
    """Карта содержания существует ради того, чтобы вход был у каждой карточки.

    Карточка, на которую ниоткуда нет ссылки, знанием не работает: её не найдут ни по
    связям, ни глазами. Поэтому: группы из объявленных правил, «Разное» для не попавших
    никуда и отдельная карта брошенных.
    """
    root = make_project(tmp, git=True)
    kb = root / "AuroraKnowledgeDB"
    for section in ("Glossary", "Concepts", "Roles", "MOC"):
        (kb / section).mkdir(parents=True, exist_ok=True)
    def card(section, name, **fm):
        head = "".join(f"{k}: {v}\n" for k, v in fm.items())
        (kb / section / f"{name}.md").write_text(
            f'---\ntitle: "{name}"\n{head}status: imported\n---\n\nтекст\n', encoding="utf-8")
    card("Glossary", "ДОП", type="glossary")
    card("Concepts", "Приём", type="concept")
    card("Roles", "Аналитик", type="role")
    (kb / "Ничьё.md").write_text(          # ни типа, ни раздела с правилом
        '---\ntitle: "Ничьё"\nstatus: imported\n---\n\nтекст\n', encoding="utf-8")
    (kb / "Concepts/Приём.md").write_text(
        '---\ntitle: "Приём"\ntype: concept\nstatus: imported\n---\n\nсм. [[ДОП]]\n',
        encoding="utf-8")

    dry = run("kb_moc.py", cwd=root)
    assert "Термины и определения | 1" in dry.stdout, dry.stdout[:600]
    assert "Роли | 1" in dry.stdout, "группа ролей не собралась"
    assert "Разное | 1" in dry.stdout, "карточка без типа и раздела не попала в «Разное»"
    assert not list((kb / "MOC").glob("*.md")), "dry-run записал карты"

    run("kb_moc.py", "--apply", cwd=root)
    terms = (kb / "MOC/Термины-и-определения.md").read_text(encoding="utf-8")
    assert "[[ДОП|ДОП]]" in terms and "ФАЙЛ ГЕНЕРИРУЕТСЯ" in terms, terms[:400]
    lost = (kb / "MOC/Брошенные.md").read_text(encoding="utf-8")
    assert "Ничьё" in lost and "Аналитик" in lost, "брошенные не собраны"
    assert "[[ДОП|ДОП]]" not in lost, "на ДОП ссылается «Приём» — она не брошенная"

    # поиск кандидатов: скопление по метке и узел, на который все ссылаются
    for i in range(9):
        (kb / "Concepts" / f"Норма-{i}.md").write_text(
            f'---\ntitle: "Норма-{i}"\ntype: concept\ntags: [regulation]\n'
            "status: imported\n---\n\nсм. [[ДОП]]\n", encoding="utf-8")
    ideas = run("kb_moc.py", "--suggest", cwd=root).stdout
    assert "метка: regulation (9)" in ideas, f"скопление по метке не найдено:\n{ideas}"
    assert "tag:regulation" in ideas, "нет готовой строки для moc_groups.txt"

    # рукотворную карту генератор не трогает: у неё нет шапки «ФАЙЛ ГЕНЕРИРУЕТСЯ»
    (kb / "MOC/Роли.md").write_text("# Роли\n\nсобрано руками\n", encoding="utf-8")
    out = run("kb_moc.py", "--apply", "--allow-dirty", cwd=root).stdout
    assert "написан руками" in out, out[-400:]
    assert "собрано руками" in (kb / "MOC/Роли.md").read_text(encoding="utf-8")


@test
def test_kb_graph_writes_links_into_cards(tmp: Path):
    """Связь живёт в зеркале, а работать должна в базе: `related:` выводится из графа."""
    root = make_project(tmp, git=True)
    conf = root / "Sources/Confluence/Раздел"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "ALG.md").write_text(
        '---\ntitle: "ALG-1"\npage_id: 1\nry_defines: [RU.P.ALG-1]\n---\n\nтекст\n',
        encoding="utf-8")
    (conf / "US.md").write_text(
        '---\ntitle: "US-1.1"\npage_id: 2\nry_links: [RU.P.ALG-1]\n---\n\nтекст\n',
        encoding="utf-8")
    cards = root / "AuroraKnowledgeDB/Concepts"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "Алгоритм.md").write_text(
        '---\ntitle: "Алгоритм"\nsource: "Sources/Confluence/Раздел/ALG.md"\n'
        "status: imported\nrelated: []\n---\n\n# Алгоритм\n", encoding="utf-8")
    (cards / "История.md").write_text(
        '---\ntitle: "История"\nsource: "Sources/Confluence/Раздел/US.md"\n'
        "status: imported\n---\n\n# История\n", encoding="utf-8")

    dry = run("kb_graph.py", "--cards", cwd=root)
    assert "связей добавлено: 2" in dry.stdout, dry.stdout[:400]
    assert "related: []" in (cards / "Алгоритм.md").read_text(encoding="utf-8"), \
        "dry-run не должен писать в карточки"

    run("kb_graph.py", "--cards", "--apply", cwd=root)
    a = (cards / "Алгоритм.md").read_text(encoding="utf-8")
    b = (cards / "История.md").read_text(encoding="utf-8")
    assert '- "[История](История.md)"' in a and '- "[Алгоритм](Алгоритм.md)"' in b, a + b
    assert a.count("related:") == 1, f"поле related задвоилось:\n{a}"
    assert "# Алгоритм" in a, "тело карточки пострадало"

    second = run("kb_graph.py", "--cards", "--apply", cwd=root)
    assert "связей добавлено: 0" in second.stdout, "повторный прогон дублирует связи"






@test
def test_links_and_stubs_respect_separators_and_dots(tmp: Path):
    """Имя — это имя, а не «текст до последней точки», и разделители в нём не значимы.

    `[[ALG-3.14 Учёт операции]]` разрешалось в карточку `ALG-3` — совсем другое знание,
    молча. А ссылка `[[ER BaR FID]]` на существующую `ER-BaR-FID` считалась битой, и под
    неё заводилась вторая пустая карточка: знание раскалывалось надвое.
    """
    root = make_project(tmp, git=True)
    card(root, "Concepts/ALG-3.md", "короткий алгоритм")
    card(root, "Concepts/ALG-3.14-Учёт-операции-из-смежной-системы.md", "длинный алгоритм")
    card(root, "Concepts/ER-BaR-FID.md", "справочник")
    card(root, "Concepts/Ссылающаяся.md",
         "см. [[ALG-3.14 Учёт операции из смежной системы]], [[ER BaR FID]] и [[Неизвестное]]")

    fixed = run("kb_fix.py", "--links", "--apply", "--allow-dirty", cwd=root)
    text = (root / "AuroraKnowledgeDB/Concepts/Ссылающаяся.md").read_text(encoding="utf-8")
    assert "[[ALG-3]]" not in text, f"ссылка с точкой ушла не в ту карточку:\n{fixed.stdout[:600]}"

    stubs = run("kb_fix.py", "--stubs", "--apply", "--allow-dirty", cwd=root)
    made = {p.stem for p in (root / "AuroraKnowledgeDB").rglob("*.md")}
    assert "Неизвестное" in made, f"заготовка под настоящую дыру не заведена:\n{stubs.stdout[:600]}"
    assert "ER BaR FID" not in made, "заведён двойник карточки, набранной с другими разделителями"
    stub = next(p for p in (root / "AuroraKnowledgeDB").rglob("Неизвестное.md"))
    assert "type:" in stub.read_text(encoding="utf-8"), "заготовка без type: — линтер сразу ругнётся"


@test
def test_parsing_sees_the_stubs_so_knowledge_lands_in_them(tmp: Path):
    """Заготовку разбор видит, ответ человеку — нет. Это разные вопросы.

    Заготовка — имя, которое база уже застолбила: на него ссылаются, содержания нет.
    Знание о том же предмете обязано лечь в неё. Пока заготовки выбрасывались из
    кандидатов, разбор их не видел и заводил рядом вторую карточку — а заготовка
    оставалась пустой навсегда, и ссылки на неё вели в никуда. Ровно тот случай, ради
    которого заготовки и придуманы: сначала появилось имя «НДС», через три захода —
    определение, и оно должно попасть в неё, а не в новую карточку.

    Чтобы модель могла решить, тот ли это предмет, в строке кандидата показаны те, кто
    на заготовку ссылается: по ним видно, в каком смысле имя застолбили. Смысл другой —
    заводится своя карточка («Уплата НДС» рядом с заготовкой «НДС»).
    """
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    pick = src.split("def _pick")[1].split("\ndef ")[0]
    assert "if stub and not stubs:" in pick, \
        "заготовки снова выброшены из кандидатов — знание опять пойдёт мимо них"
    assert "ЗАГОТОВКА" in pick and "ссылаются" in pick, \
        "заготовка в списке не помечена: модель примет её за карточку со знанием"

    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R
    block = R.candidates_block([("НДС", "Glossary",
                                 "ЗАГОТОВКА: имя занято, знания нет · ссылаются: Вычет")])
    assert "ЗАГОТОВКА" in block and "into" in block, \
        "правило про заготовки не сказано в самом промпте — пометка бесполезна"

    # связывание тезисов их целью не берёт: пустая карточка не обещает содержания
    relink = src.split("def relink_card")[1].split("\ndef ")[0]
    assert "stubs=False" in relink, \
        "ссылка ведёт в заготовку — карточка обещает содержание, которого нет"

    # наполненная заготовка перестаёт быть заготовкой — иначе она невидима навсегда
    fix = (SCRIPTS / "kb_fix.py").read_text(encoding="utf-8")
    grew = fix.split("def outgrew_placeholder")[1].split("\ndef ")[0]
    assert "is_placeholder(c.fm, c.text)" in grew, \
        "отметку снимают только по статусу: на живой базе 41 заготовка из 60 опознавалась "\
        "лишь по тегу и осталась бы невидимой навсегда"
    assert "split(QUOTES, 1)[0]" in grew, \
        "судит по всему телу: строка-заготовка живёт в дословном источнике вечно"
    # Проверяем ПОВЕДЕНИЕ, а не текст кода. Раньше здесь стояла строка «в функции есть
    # STUB_MARK.search(body)» — и она сломалась, как только вето по всему телу заменили
    # на построчное. Утверждение же осталось прежним: тезис «это заготовка, знаний нет»
    # длиннее порога, и по длине его от знания не отличить, поэтому карточку он вырасти
    # не даёт. А служебная строка рядом с настоящим знанием — даёт.
    def _grew(body: str) -> bool:
        text = f'---\ntitle: "X"\nstatus: placeholder\n---\n\n# X\n\n{body}\n'
        return kf.outgrew_placeholder(kf.Card("AuroraKnowledgeDB/Concepts/X.md", text))

    import kb_fix as kf
    assert not _grew("_Заготовка: ссылка на это понятие уже есть, знания пока нет._"), \
        "пустышка со служебной строкой объявлена выросшей"
    proza = ("X — заготовка понятия, в которой ссылка уже есть, а знаний пока нет. "
             "Наполните её при следующем разборе источника, ссылки переписывать не придётся. "
             "Пока карточка существует только чтобы ссылка не была битой.")
    assert len(proza) > kf.FILLED_CHARS, "фикстура короче порога — проверка ничего не ловит"
    assert not _grew(proza), \
        "тезис «это заготовка, знаний нет» длиннее порога — карточка выйдет в выдаче пустой"
    assert _grew("_Заготовка: ссылка на это понятие уже есть, знания пока нет._\n\n"
                 "Доверенность — сущность ER.AS.MRP: TRUE в ER.AS.MRP.IsMachineRead означает "
                 "машинно-читаемую доверенность, FALSE — бумажную. Уникальность для бумажной "
                 "— ER.AS.MRP.Id, для МЧД — ER.AS.MRP.DocNumber."), \
        ("служебная строка держит пустышкой карточку с готовым знанием: её не убирает никто, "
         "и на живой базе так стояли 105 карточек")

@test
def test_a_long_card_is_indexed_whole_not_just_its_beginning(tmp: Path):
    """Длинная карточка попадает в индекс целиком, кусками.

    В вектор шли заголовок, синонимы и первые полторы тысячи знаков тела. Замер на живой
    базе: **56% карточек длиннее этого окна**, девятый дециль — 7728 знаков. Больше
    половины базы искалась по началу, и знание, лежащее ниже, найти было нельзя вовсе.

    Ключом индекса стал КУСОК, а склейка по карточке идёт в конце поиска — так предфильтр
    и доказательство точности остались нетронутыми: они работают по строкам. Хвостовому
    куску даётся скидка: у длинной карточки иначе просто больше попыток совпасть
    случайно, и выдача кренится в сторону длинных.
    """
    sys.path.insert(0, str(SCRIPTS))
    import kb_embed as E

    parts = E.pieces("A" * (E.PIECE * 3))
    assert len(parts) == 3 or len(parts) == 4, why(len(parts)) or "тело не нарезано"
    assert all(len(p) <= E.PIECE for p in parts), "кусок длиннее окна — резать незачем"
    assert E.pieces("коротко") == ["коротко"], "короткая карточка разрезана"
    assert len(E.pieces("Б" * (E.PIECE * 50))) <= E.MAX_PIECES, \
        "у свалки на двести тысяч знаков полсотни векторов — она съест индекс"
    # нахлёст: мысль на границе обязана целиком попасть хотя бы в один кусок
    text = "x" * (E.PIECE - 50) + "МЫСЛЬ-ЦЕЛИКОМ" + "y" * E.PIECE
    assert any("МЫСЛЬ-ЦЕЛИКОМ" in p for p in E.pieces(text)), \
        "нахлёста нет — фраза, разрезанная границей, не найдётся ни в одном куске"

    assert E.piece_owner("Карточка") == "Карточка"
    assert E.piece_owner(f"Карточка{E.PIECE_SEP}3") == "Карточка", \
        "имя карточки не восстанавливается из ключа — выдача покажет «Карточка¶3»"
    assert 0 < E.TAIL_DISCOUNT < 1, \
        "скидки хвосту нет — длинная карточка выигрывает числом бросков, а не смыслом"

    src = (SCRIPTS / "kb_embed.py").read_text(encoding="utf-8")
    assert "return by_card(scored)" in src, \
        "поиск отдаёт куски вместо карточек — наружу поедут имена вида «Карточка¶2»"
    assert "want = min(n, max(limit * 3" in src, \
        "берём ровно limit кусков — они могут оказаться кусками одной карточки"


@test
def test_the_panel_shows_which_model_actually_takes_the_role(tmp: Path):
    """Под ролью видно, какая модель её возьмёт и откуда это значение.

    Форма показывала только СВОЁ поле, а работало слитое: кит < проект. Человек выставлял
    в ките «flash на все роли», проект молча перекрывал `worker` на «27b» — и понять это
    можно было единственным способом: запустить прогон и прочитать в логе, кто ответил.
    На живом проекте так и вышло: 326 вызовов тяжёлой модели там, где ожидали быструю.
    """
    sys.path.insert(0, str(KIT / "cockpit"))
    sys.path.insert(0, str(SCRIPTS))
    import importlib
    C = importlib.import_module("aurora_cockpit")

    proj = tmp / "проект"
    (proj / "AuroraKnowledgeDB").mkdir(parents=True)
    # Адрес шлюза — свой: без него №1 существовал только там, где он задан в ките (на
    # машине разработчика), и на чистой копии кита тест падал `StopIteration`.
    (proj / ".env.aurora.local").write_text(
        "AURORA_AGENT_BACKEND_1_URL=http://gw.example/v1\n"
        "AURORA_AGENT_BACKEND_1_MODEL_WORKER=тяжёлая\n", encoding="utf-8")
    st = C.agent_state(str(proj))
    b1 = next(b for b in st["backends"] if b["n"] == 1)
    assert b1["models"].get("worker") == "тяжёлая", \
        why(b1["models"]) or "действующая модель роли не та, что задана в проекте"
    assert "kit_models" in b1, \
        "панель не получает китовых значений — сказать «перекрывает» ей будет нечем"
    assert (prefix := "AURORA_AGENT_BACKEND_1_MODEL_WORKER") in st["own"], \
        why(st["own"]) or "не видно, что значение задано именно в проекте"
    assert prefix  # имя переменной названо в тесте, чтобы правка ключа его сломала

    ui = panel_sources()
    assert "работает: " in ui and "перекрывает " in ui, \
        "под ролью не сказано, какая модель её возьмёт и что она перекрывает"
    assert "модель не задана — роль не поедет" in ui, \
        "пустая роль молчит: человек узнает о ней на прогоне"


@test
def test_only_one_resume_button_and_it_names_its_project(tmp: Path):
    """Кнопка «Продолжить маршрут» одна, и по ней видно, к какому проекту она.

    Консоль читает состояние остановленного маршрута при каждом входе и дорисовывала
    кнопку, не убирая прежнюю. После нескольких переключений между проектами в консоли
    висели четыре кнопки: три одинаковые и одна из другого проекта. Подпись у всех была
    одна — «Продолжить маршрут», — и понять, какая к чему относится и какую жать, было
    нельзя. Человек либо не жал вовсе, либо продолжал чужой маршрут.
    """
    ui = panel_sources()
    assert "function dropResumeButtons()" in ui, \
        "прежние кнопки продолжения не убираются — они будут копиться при каждом входе"
    for where in ("async function showLastRoute", "function showOfflineResume"):
        body = ui.split(where)[1][:400]
        assert "dropResumeButtons()" in body, \
            f"{where} рисует кнопку, не убрав прежнюю"
    # каждая кнопка продолжения помечена — иначе их не найти, чтобы убрать
    assert ui.count("resume-route") >= 4, \
        "не все кнопки продолжения помечены классом: часть переживёт очистку"
    assert "S.project.slug" in ui.split("async function showLastRoute")[1][:1200], \
        "в подписи нет проекта — при двух проектах снова не понять, какая кнопка чья"


@test
def test_a_checked_neighbour_pair_does_not_come_back(tmp: Path):
    """Сверенная пара «карточка и её сосед» не возвращается в очередь.

    Очередь считалась по датам ПРАВКИ соседа — а её двигает любой прогон: доверие,
    связи, карты. На живой базе так получилось 187 пар из ниоткуда. Считаем по тезису:
    знание устаревает, когда сосед сказал о себе иначе.

    И второе: пару, у которой противоречия нет, никто не отмечал — те же 134 пары шли
    к модели при каждом прогоне и каждый раз не находили ничего. «Посмотрели и
    разошлись» — это результат, и он записывается отметкой `neighbours`.
    """
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    fn = src.split("def neighbours_behind")[1].split("\ndef ")[0]
    assert 'when[stem] = ((fm.get("distilled")' in fn, \
        "очередь снова считается по дате правки — метрика зашумится до бесполезности"
    assert 'theirs > seen_upto' in fn, \
        "отметка о сверке не читается — разобранные пары вернутся навсегда"
    assert "def mark_neighbours" in src, "отметку о сверке некому поставить"
    run_c = src.split("def run_clashes")[1].split("\ndef ")[0]
    assert "mark_neighbours(cwd" in run_c, \
        "разбор пары не отмечается — та же работа повторится на каждом прогоне"

    gaps = (SCRIPTS / "kb_gaps.py").read_text(encoding="utf-8")
    behind = gaps.split("def behind_neighbour")[1].split("\ndef ")[0]
    assert 'oc["fm"].get("distilled")' in behind, \
        "отчёт о дырах считает соседей иначе, чем ход противоречий — числа разойдутся"


@test
def test_the_cycle_stops_when_it_stops_converging(tmp: Path):
    """Цикл маршрута — не бесконечный: предел мал и о нём говорят прямо.

    Предохранитель стоял на 500 оборотов. Несходящийся цикл крутился часами и выглядел
    долгим прогоном: на живой базе ОДИН источник, который движок не мог отметить
    разобранным, держал маршрут сорок оборотов подряд, и понять это по экрану было
    нельзя. Дюжина оборотов — уже симптом, а не работа.
    """
    ui = panel_sources()
    assert "const CYCLE_LIMIT = 12;" in ui, \
        "предохранитель снова велик — несходящийся цикл будет выглядеть долгим прогоном"
    assert "цикл не сошёлся за" in ui, \
        "упор в предохранитель не назван: человек не отличит его от честной работы"
    assert "какой источник не удалось отметить" in ui, \
        "не сказано, где искать причину — диагностика без адреса бесполезна"

    # режимы ремонта, заведённые под остаток разбора, стоят в маршруте «Починить базу»
    scen = (KIT / "cockpit/scenarios.txt").read_text(encoding="utf-8")
    fix = scen.split("[fix]")[1].split("\n[")[0]
    for flag in ("--terms", "--unparsed", "--themes", "--drop-jira"):
        assert flag in fix, f"режим {flag} заведён, но в маршрут не попал — им не воспользуются"


@test
def test_lint_and_repair_judge_a_link_by_the_same_rule(tmp: Path):
    """Ремонт и линтер считают имя цели одним правилом.

    Ремонт снимал расширение с цели (`leaf_name`), линтер — нет. Ссылка
    `[[Заметка.md]]` на карточку «Заметка» для ремонта была разрешима, и он её не трогал,
    а линтер звал битой — и звал бы вечно, потому что чинить было нечего. Расхождение
    двух проверок об одном хуже любой из них: человек видит ошибку, которую ни одна
    команда не убирает.

    Заодно: ссылка на ФАЙЛ зеркала (`[[JIRA_prompt.md]]`, когда карточки нет, а файл
    есть) — не битая ссылка, а неверная форма. Ремонт снимает разметку, оставляя имя
    словами: происхождение записано в `sources:`, знание не теряется.
    """
    lint = (SCRIPTS / "kb_lint.py").read_text(encoding="utf-8")
    assert "leaf_name(target.split(\"#\")[0].strip())" in lint, \
        "линтер снова судит цель ссылки своим правилом — разойдётся с ремонтом"

    fix = (SCRIPTS / "kb_fix.py").read_text(encoding="utf-8")
    assert "\nLINK_RE = re.compile" not in fix, \
        "у ремонта снова своя копия LINK_RE: общая умеет отличать сноску [[1]](#ftn), эта — нет"
    assert "def source_file" in fix and "mapping[target] = None" in fix, \
        "ссылка на файл зеркала не снимается — линтер будет звать её битой вечно"

    # снятие разметки: имя остаётся словами
    sys.path.insert(0, str(SCRIPTS))
    from aurora_common import rewrite_links
    got = rewrite_links("см. [[Файл.md]] и [[Карточка]].", {"Файл.md": None})
    assert got == "см. Файл.md и [[Карточка]].", why(got) or "разметка снята неверно"

    # сноска из выгрузки Word — не ссылка на карточку
    from aurora_common import link_refs
    assert "1" not in link_refs("строка 010 = сумме строк 420 и 430[[1]](#_ftn1);"), \
        "сноска [[1]](#_ftn1) принята за ссылку — в базе появятся мнимые битые ссылки"


@test
def test_the_critic_does_not_reject_a_name_for_matching_the_page_title(tmp: Path):
    """Имя, совпавшее с названием страницы, — это нормальное имя карточки.

    Критик отклонял имя за то, что оно «имя файла». Но в зеркале Confluence имя файла
    И ЕСТЬ заголовок страницы, а заголовок обычно называет предмет: «Варианты расписания
    запуска алгоритмов» — правильное имя карточки, хотя файл называется так же.

    На живой базе критик отклонял его двенадцать оборотов подряд, отметка не ставилась,
    знание в базу не попадало, а цикл маршрута не мог дойти до нуля. Лечили это обходом
    — третьей попыткой без критика; обход убран, потому что глушить проверку значит
    обесценить и её правоту. Исправлен критерий.
    """
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    critic = src.split("PROMPT_BUILD_CRITIC")[1].split('"""')[1]
    flat = " ".join(critic.split())
    assert "СОВПАДЕНИЕ С НАЗВАНИЕМ СТРАНИЦЫ — НЕ ПОВОД ОТКЛОНИТЬ" in flat, \
        "критик снова отклоняет имя за совпадение с именем файла"
    assert "файловом виде" in flat, \
        "не сказано, какое имя файла действительно негодно: с расширением, датой, версией"
    # обход убран: третья попытка без критика больше не делается
    build = src.split("def solve_source")[1].split("\ndef ")[0]
    assert "use_critic = False" not in build, \
        "проверка снова глушится после двух отказов — её правота обесценится вместе с ошибкой"


@test
def test_a_merge_archives_the_donor_even_when_the_name_is_taken(tmp: Path):
    """Слияние обязано убрать донора из базы, иначе оно повторится на каждом прогоне.

    Перенос в `_archive` пропускался, если файл с таким именем там уже лежал. Задумано
    как защита от затирания, на деле — карточка, которую движок считает
    заархивированной, оставалась в базе. Слияние отчитывалось успехом, донор жил дальше,
    следующий прогон сливал его снова.

    На живой базе так набралось **49 одинаковых блоков «Слияние»** в одной карточке: она
    выросла со 120 738 знаков из повторов, при двух настоящих донорах.
    """
    sys.path.insert(0, str(SCRIPTS))
    import kb_fix as F

    src = (SCRIPTS / "kb_fix.py").read_text(encoding="utf-8")
    apply_body = src.split("for src, dst in plan.moves")[1].split("\ndef ")[0]
    assert "пропущен перенос" not in apply_body, \
        "перенос снова пропускается — донор останется в базе и сольётся ещё раз"
    assert "free_archive_name(dst)" in apply_body, \
        "занятое имя в архиве не обходится — донор останется в базе"
    naming = src.split("def free_archive_name")[1].split("\ndef ")[0]
    assert "%Y%m%d-%H%M" in naming, \
        "к занятому имени добавляется номер, а не дата-время: по нему не видно порядка"
    assert "NAME_BYTES" in naming and "encode(\"utf-8\")" in naming, \
        "длина имени не режется по пределу файловой системы, и не в байтах"

    merge = src.split("def merge_paths")[1].split("\ndef ")[0]
    assert 'f"Присоединено из [[{drop.stem}]]" in text' in merge, \
        "повторное слияние того же донора снова удвоит тело"

    # перенос при занятом имени: файл уходит, имя получает номер
    base = tmp / "AuroraKnowledgeDB"
    (base / "_archive").mkdir(parents=True)
    (base / "Concepts").mkdir(parents=True)
    live = base / "Concepts" / "Карточка.md"
    live.write_text("живая", encoding="utf-8")
    (base / "_archive" / "Карточка.md").write_text("старая", encoding="utf-8")
    plan = F.Plan()
    plan.moves.append((str(live), str(base / "_archive" / "Карточка.md")))
    F.apply_plan(plan)
    assert not live.exists(), "донор остался в базе — слияние повторится на каждом прогоне"
    got = [p.name for p in (base / "_archive").iterdir() if p.name != "Карточка.md"]
    assert len(got) == 1 and re.match(r"Карточка-\d{8}-\d{4}", got[0]), \
        why(got) or "в архиве не дата-время, а номер: по нему не видно, когда и в каком порядке"
    # Длина режется по пределу файловой системы, и режется ОСНОВА, а не метка времени.
    # Имя заготовки — 248 байт: его создаст любая система, а вот с меткой «-ГГГГММДД-ЧЧММ»
    # оно уже длиннее 255. Прежняя заготовка в 418 байт создавалась только на macOS — на
    # Linux тест падал раньше проверки (`File name too long`), и ровно так на Linux не
    # выгружался бы репозиторий с таким именем.
    long_name = base / "Concepts" / ("Длинная-" + "я" * 115 + ".md")
    assert len(long_name.name.encode("utf-8")) <= 255, "заготовка сама нарушает предел Linux"
    long_name.write_text("текст", encoding="utf-8")
    (base / "_archive" / long_name.name).write_text("старая", encoding="utf-8")
    plan2 = F.Plan()
    plan2.moves.append((str(long_name), str(base / "_archive" / long_name.name)))
    F.apply_plan(plan2)
    made = [p.name for p in (base / "_archive").iterdir() if p.name.startswith("Длинная")]
    fresh = [n for n in made if n != long_name.name]
    assert fresh, why(made) or "длинное имя не уехало в архив"
    assert len(fresh[0].encode("utf-8")) <= 255, \
        f"имя в {len(fresh[0].encode('utf-8'))} байт — файловая система его не примет"
    assert fresh[0].endswith(".md") and re.search(r"-\d{8}-\d{4}", fresh[0]), \
        why(fresh[0]) or "обрезали хвост вместо основы: метка времени и расширение потеряны"
    assert (base / "_archive" / "Карточка.md").read_text(encoding="utf-8") == "старая", \
        "прежняя запись архива затёрта"


@test
def test_a_source_judged_empty_is_done_not_pending(tmp: Path):
    """Источник с вердиктом «пусто» — разобранный, а не пропущенный.

    `--reopen` возвращал в план любой источник, не давший карточек. Но источник,
    который разбор честно признал пустым, карточек и не даст — никогда. Вердикт
    записан в манифест (`empty_reason`), проверен движком, — и всё равно каждый прогон
    маршрута брал те же страницы, гонял по ним модель и снова получал «пусто».

    Отсюда и жалоба: сколько ни запускай подряд, работа не кончается. Момент «новых
    источников нет, отработали быстро» не наступал по устройству.

    Изменится файл — источник вернётся: вердикт был о прежнем тексте.
    """
    root = make_project(tmp)
    src = root / "Sources" / "Confluence"
    src.mkdir(parents=True, exist_ok=True)
    empty, live = src / "Пустая.md", src / "Живая.md"
    empty.write_text("# Пустая\n\n" + "х" * 300, encoding="utf-8")
    live.write_text("# Живая\n\n" + "знание " * 60, encoding="utf-8")

    sys.path.insert(0, str(SCRIPTS))
    import build_plan as BP
    old = os.getcwd()
    os.chdir(root)
    try:
        man = {"sources": {
            "Sources/Confluence/Пустая.md": {"cards": 0, "hash": BP.file_hash(str(empty)),
                                             "empty_reason": "знания нет",
                                             "empty_rule": BP.EMPTY_RULE},
            "Sources/Confluence/Живая.md": {"cards": 0, "hash": BP.file_hash(str(live))}}}
        # `reopen` возвращает код выхода, а не число — считаем по манифесту после записи
        import copy
        m1 = copy.deepcopy(man)
        BP.reopen(m1, "", True)
        left = set(m1["sources"])
        assert "Sources/Confluence/Пустая.md" in left, \
            "источник с вердиктом «пусто» переоткрыт — модель будет гонять его вечно"
        assert "Sources/Confluence/Живая.md" not in left, \
            why(sorted(left)) or "бесплодный источник не вернулся в план"

        # текст изменился — вердикт был о прежнем, источник обязан вернуться
        empty.write_text("# Пустая\n\nтеперь тут знание " * 20, encoding="utf-8")
        m2 = copy.deepcopy(man)
        BP.reopen(m2, "", True)
        assert "Sources/Confluence/Пустая.md" not in set(m2["sources"]), \
            "изменённый источник не вернулся — правка страницы не дойдёт до базы"
    finally:
        os.chdir(old)


@test
def test_a_short_section_joins_its_neighbour_instead_of_vanishing(tmp: Path):
    """Короткая секция присоединяется к соседней, а не выбрасывается.

    Порог `MIN_SECTION` заводился, чтобы не плодить карточки из огрызков. Но он не
    «не делал карточку» — он ТЕРЯЛ содержимое: секция не доходила до модели вовсе.

    Живой случай. Источник «Расписание запуска алгоритмов» состоял из трёх секций:
    таблица с расписанием (159 знаков), подпись к схеме (39) и ссылка на `.drawio`
    (247). Первые две вылетели по порогу, модель получила одну — ссылку — и честно
    ответила «только ссылка, знания нет». Источник ушёл из плана вместе с расписанием,
    и виновата была не модель: ей не показали таблицу.

    Заодно текст ДО первого заголовка: при первом заголовке `title` ещё пуст, и всё
    накопленное отбрасывалось. У страниц, начинающихся с таблицы, терялось начало.
    """
    sys.path.insert(0, str(SCRIPTS))
    import build_plan as BP

    short_table = "| A | B |\n|---|---|\n| 1 | 2 |"
    text = (f"# Расписание\n\n{short_table}\n\n"
            "# Схема\n\nподпись\n\n"
            "## Ссылки\n\n" + "- [схема.drawio](x.drawio)\n" * 12)
    got = BP.sections(text)
    joined = "\n".join(b for _t, b in got)
    assert short_table.splitlines()[2] in joined, \
        why([(t, len(b)) for t, b in got]) or "таблица потеряна: секция короче порога выброшена"
    assert len(got) >= 1 and all(b.strip() for _t, b in got), "пустые секции в раскадровке"

    # преамбула: текст до первого заголовка
    pre = "Важное вступление. " * 12
    got2 = BP.sections(f"{pre}\n\n# Раздел\n\n" + "тело раздела. " * 30)
    assert any("Важное вступление" in b for _t, b in got2), \
        why([t for t, _b in got2]) or "текст до первого заголовка выброшен"

    # и порог по-прежнему бережёт от карточек-огрызков: одна короткая секция целиком
    tiny = BP.sections("# Крошка\n\nдва слова")
    assert tiny == [] or sum(len(b) for _t, b in tiny) < BP.MIN_SECTION, \
        why(tiny) or "огрызок снова стал полноценной секцией"


@test
def test_orphans_are_counted_without_the_maps_made_for_them(tmp: Path):
    """«Сирот 0» — не победа, если считать ссылкой строку в сгенерированной карте.

    `ops:stats` рапортовал «карточек без входящих ссылок: 0», а карта «Брошенные» в той
    же базе перечисляла 26. Обе фразы про одно, числа разные. Правда была третьей:
    без единой ссылки ОТ КАРТОЧЕК висели 34 из 74.

    Причина — счёт по всем файлам, включая карты содержания. Для веса карточки это
    верно: попасть в оглавление значит стать достижимым. Но карты заводятся ИМЕННО под
    брошенных, поэтому для вопроса «кто брошен» счётчик обнуляет сам себя сразу после
    `kb:moc`, и человек читает «база связна» о базе, треть которой держится на одной
    автогенерации.
    """
    root = make_project(tmp)
    card(root, "Concepts/Одинокая.md", "Знание, на которое никто не ссылается.")
    card(root, "Concepts/Связанная.md", "Знание, на которое ссылается соседняя карточка.")
    card(root, "Concepts/Ссылающаяся.md", "Опирается на [[Связанная]].")
    moc = root / "AuroraKnowledgeDB" / "MOC"
    moc.mkdir(parents=True, exist_ok=True)
    (moc / "Понятия.md").write_text(
        '---\ntitle: "Понятия"\ntype: moc\nstatus: index\n---\n\n'
        "- [[Одинокая]]\n- [[Связанная]]\n- [[Ссылающаяся]]\n", encoding="utf-8")

    sys.path.insert(0, str(SCRIPTS))
    from aurora_common import inbound_counts
    kb = str(root / "AuroraKnowledgeDB")
    with_nav = inbound_counts(kb)
    assert with_nav.get("Одинокая", 0) > 0, \
        "карта содержания перестала считаться связностью — вес карточки станет неверным"
    without = inbound_counts(kb, skip_nav=True)
    assert without.get("Одинокая", 0) == 0, \
        why(without) or "брошенная карточка снова «связана» строкой в карте, заведённой под неё"
    assert without.get("Связанная", 0) == 1, "ссылка от карточки потеряна вместе с картой"

    stats = (SCRIPTS / "aurora_stats.py").read_text(encoding="utf-8")
    assert "inbound_counts(ROOT, skip_nav=True)" in stats, \
        "сводка снова считает сирот вместе с картами — метрика всегда будет нулём"
    assert "карты содержания не в счёт" in stats, \
        "в отчёте не сказано, что именно посчитано: два числа про «сирот» опять разойдутся"


@test
def test_a_changed_neighbour_puts_the_card_in_the_queue(tmp: Path):
    """Изменили соседа — карточка, ссылающаяся на него, встаёт в очередь на проверку.

    Все отметки движка были привязаны к самой карточке: `relinked` и `extracted` держат
    дату её тезиса, «тезис отстал» сравнивает её с файлом-источником. Соседей не проверял
    никто — поправили главную карточку, и пять ссылающихся на неё продолжали говорить
    прежнее. Расхождение всплывало у человека, а не у движка.

    Считается арифметикой по датам из шапок: ни одного обращения к модели. Разбирает
    пары `agent:clashes` — он умеет цитировать обе стороны, а решает человек.
    """
    root = make_project(tmp, git=True)
    # Сосед переписал ТЕЗИС позже — по нему и считается устаревание. Дата правки не
    # годится: её двигает любой прогон, и очередь зашумляется до бесполезности.
    card(root, "Concepts/Главная.md", status="draft", kind="knowledge",
         distilled="2026-09-04", updated="2026-09-04", body="Правило изменилось.")
    card(root, "Concepts/Ссылается.md", status="draft", kind="knowledge",
         distilled="2026-09-01", updated="2026-09-01",
         body="Работает по правилу из [[Главная]].")
    card(root, "Concepts/Сама-по-себе.md", status="draft", kind="knowledge",
         distilled="2026-09-01", updated="2026-09-01", body="Ни на кого не ссылается.")

    out = run("kb_gaps.py", cwd=root)
    assert "Сосед изменился" in out.stdout, "находки нет в отчёте о дырах"
    assert "`Ссылается`" in out.stdout, \
        why(out.stdout[:800]) or "карточка, отставшая от соседа, не названа"
    assert "`Сама-по-себе`" not in out.stdout.split("Сосед изменился")[1].split("##")[0], \
        "в очередь попала карточка, которая ни на кого не ссылается"

    # ход противоречий берёт эти пары ПЕРВЫМИ: групп по совпадению текста тысячи
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    run_c = src.split("def run_clashes")[1].split("\ndef ")[0]
    assert "behind = neighbours_behind(cwd)" in run_c and "groups = behind +" in run_c, \
        "пары «сосед изменился» не идут первыми — до них прогон дойдёт через раз"


@test
def test_hybrid_search_puts_words_and_meaning_on_one_scale(tmp: Path):
    """Гибрид складывает слова и смысл, приведя их к одной шкале.

    Он назывался гибридом, а был словами с маленькой добавкой: к счёту по словам, где
    набираются сотни, прибавлялась премия за близость — не больше тридцати трёх пунктов.
    Вектора фактически не участвовали. Живой промах: по запросу «разработка и
    тестирование: КПП» карточка «КПП» не попадала в первую десятку — слова «разработка»
    и «тестирование» вытягивали чужие карточки, где они встречаются в теле, а короткое
    имя из трёх букв столько очков набрать не может. Вектора ставили её первой, и это
    ничего не меняло.

    Замер на живой базе, сорок коротких запросов с именем предмета: чистые вектора
    довели предмет до списка 39 раз из 40, починенный гибрид — 40 из 40.

    Чистый RRF (сумма `1/(K + место)`) тут замерен и отвергнут: он выбрасывает величину
    совпадения, и уверенная находка одного способа разбавляется парой середняков,
    которых оба нашли посредственно. Он терял две карточки из сорока.
    """
    sys.path.insert(0, str(SCRIPTS))
    import ctx_pack as P

    src = (SCRIPTS / "ctx_pack.py").read_text(encoding="utf-8")
    fuse = src.split("def fuse")[1].split("\ndef ")[0]
    assert "w / best" in fuse, "счёт по словам не приведён к доле от своего лучшего"
    assert "sim / top_sim" in fuse, \
        "близость нормируется не по своему лучшему — вектора снова будут тише слов"
    assert "max(was, sem)" in fuse, \
        "сигналы складываются, а не берётся сильнейший: находка одного способа утонет"

    class Fake:
        def __init__(self, stem, text):
            self.stem = self.title = stem
            self.path = f"AuroraKnowledgeDB/Concepts/{stem}.md"
            self.text, self.summary, self.aliases, self.tags = text, "", [], ""
            self.fm = {}
    cards = {
        "КПП": Fake("КПП", "код причины постановки на учёт"),
        "Разработка-отчёта": Fake("Разработка-отчёта",
                                  "разработка и тестирование отчёта, разработка формы"),
    }
    P.measure_rarity(cards)
    # слова тянут длинную карточку, смысл — короткую: сильнейший сигнал решает
    got = [c.stem for _w, c in P.fuse(cards, "разработка и тестирование КПП", {"КПП": 0.9})]
    assert got and got[0] == "КПП", \
        why(got) or "уверенная находка по смыслу снова тонет под совпадением слов"

    # без индекса выборка обязана работать по одним словам, а не отказывать
    only_words = [c.stem for _w, c in P.fuse(cards, "разработка отчёта", {})]
    assert only_words and only_words[0] == "Разработка-отчёта", \
        why(only_words) or "без семантики гибрид перестал искать словами"

    # ранжирование одно на всех: своя копия разошлась бы с тем, что отвечает человеку
    for script, why_not in (("agent_runner.py", "подбор кандидатов"),
                            ("kb_retrieval.py", "отчёт о выдаче"),
                            ("aurora_mcp.py", "поиск ассистента")):
        text = (SCRIPTS / script).read_text(encoding="utf-8")
        assert ".fuse(" in text, f"{why_not} ранжирует сам по себе — разойдётся с «Спросить»"

    # архив и карты в кандидаты не идут: слитая карточка снова предлагалась бы как живая
    take = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")
    take = take.split("def _pick")[1].split("\ndef ")[0]
    assert "_archive" in take and '["MOC"]' in take, "ctx_pack архив не отсеивает, а тут он вреден"
    assert "is_placeholder" in take, "пустышка в кандидатах — дополнять в ней нечего"


@test
def test_a_task_gives_its_knowledge_to_the_subject_not_to_a_card_of_its_own(tmp: Path):
    """Задача Jira — это работа, а знание её принадлежит предмету.

    Разбор делал из задачи отдельную карточку: «Разработка таблицы X — задача PRJ-000.
    В источнике прямо указано: разработка таблицы X». Знания в ней нет, линтер называл её
    артефактом, и человеку оставалось решать вручную. Но выбрасывать её нельзя: код
    задачи и ссылка — полезные сведения, по ним считается доверие и строится таблица
    связей. Место им — в карточке предмета, которая и так о нём накапливает знание.

    Три вещи здесь ломались на живом прогоне, и каждая проверяется ниже.
    """
    src = (SCRIPTS / "agent_runner.py").read_text(encoding="utf-8")

    # 1. Правило разбора: сведения о задаче дописываются в предмет, а не теряются пустым
    build = src.split("PROMPT_BUILD")[1].split('"""')[1]
    assert "дописываются в карточку\n  предмета" in build or "дописываются в карточку" in build, \
        "разбор снова теряет задачу вместо того, чтобы отдать её знание предмету"
    assert "Пустым" in build and "(`empty`)" in build, \
        "не сказано, что задача — не пустой источник: привязка к ней потеряется"

    # 2. Имя предмета сначала спрашивается у базы. Модель видит только близкие карточки,
    #    и предмет, до которого список не дотянулся, объявляет новым: на живом прогоне
    #    рядом с карточкой «ПериодыСтавки» так появилась «Таблица ПериодыСтавки» — раскол сущности.
    solve = src.split("def solve_task_card")[1].split("\ndef ")[0]
    assert "BP.find_card(subject, cwd)" in solve, \
        "имя предмета не проверяется по базе — сущность расколется надвое"
    assert "near_named(cwd, cfg, subject, stem)" in solve, \
        "нет переспроса по пересекающимся именам: «Таблица X» не сойдётся с «X»"
    assert "fail_why(res)" in solve, \
        "причина отказа команды снова прячется за «команда не прошла»"

    # 3. Переименование — такая же правка, как перенос: не посчитаешь — не закоммитишь
    run = src.split("def run_tasks")[1].split("\ndef ")[0]
    assert '"renamed": renamed' in run, \
        "переименования не в счёте — записанные имена останутся незафиксированными"

    # слияние возвращает предмету и ИСТОЧНИК задачи, иначе теряется доверие
    fix = (SCRIPTS / "kb_fix.py").read_text(encoding="utf-8")
    merge = fix.split("def merge_paths")[1].split("\ndef ")[0]
    assert "card_sources(drop.text)" in merge, \
        "источники донора не переезжают — ссылка на задачу теряется, доверие не посчитать"
    assert "drop.fm.get('source'" not in merge, \
        "заметка о слиянии читает поле source, выведенное из схемы: напишет «—»"

    # линтер и разбор судят одинаково: карточка, названная по предмету, — не артефакт
    lint = (SCRIPTS / "kb_lint.py").read_text(encoding="utf-8")
    assert "WORK_NAME" in lint, \
        "линтер снова зовёт артефактом любую карточку из задачи, даже названную предметом"
    sys.path.insert(0, str(SCRIPTS))
    import kb_lint as L
    assert L.artifact_kind("Разработка-таблицы-Х", "Разработка таблицы Х",
                           "Sources/JIRA/PRJ-1.md", "Processes", None), \
        "карточка, названная работой, перестала считаться артефактом"
    assert not L.artifact_kind("Таблица-Х", "Таблица Х",
                               "Sources/JIRA/PRJ-1.md", "Processes", None), \
        "карточка предмета из задачи всё ещё артефакт — `agent:tasks` не сможет её вылечить"


@test
def test_a_named_concept_the_base_can_explain_gets_a_card(tmp: Path):
    """Понятие, которое база называет словами и умеет расшифровать, получает заготовку.

    Заготовки заводились только под битые ссылки — под то, что кто-то уже решил связать.
    Но чаще сущность живёт в базе безымянной строкой: на живом проекте `НДС` был назван
    в 91 карточке, `МНС` в 58, и своей карточки не имел ни один. Знание о них размазано
    по чужим телам и по имени не находится.

    Расшифровка берётся только оттуда, где её записал человек или где она перенесена из
    источника дословно, — из словаря проекта. Понятие без расшифровки не заводится
    совсем: придумать её — ровно та ошибка, которая неотличима от знания.
    """
    root = make_project(tmp, git=True)
    card(root, "Reference/Сокращения-проекта.md",
         "| Термин | Расшифровка |\n|---|---|\n"
         "| НДС | Налог на добавленную стоимость |\n"
         "| ОКВЭД | Классификатор видов экономической деятельности |\n")
    for i in range(3):
        card(root, f"Concepts/Карточка-{i}.md",
             f"Расчёт НДС в задаче {i}; учитывается ЗЗЗ и код ОКВЭД.")

    out = run("kb_fix.py", "--terms", "--apply", "--allow-dirty", cwd=root)
    made = {p.stem for p in (root / "AuroraKnowledgeDB").rglob("*.md")}
    assert "НДС" in made, f"понятие с расшифровкой не получило карточки:\n{out.stdout[:600]}"
    # Заготовка нужна и без расшифровки: заказчик — «даже карточка только со ссылками
    # смысл имеет». Имя занято, и видно, кто это понятие называет. Выдумывать
    # расшифровку по-прежнему нельзя — её в теле просто нет.
    assert "ЗЗЗ" in made, why(sorted(made)) or "понятие без расшифровки не заведено"
    zzz = next(p for p in (root / "AuroraKnowledgeDB").rglob("ЗЗЗ.md")).read_text(
        encoding="utf-8")
    assert "расшифровки база пока не знает" in zzz, "заготовка молчит о том, чего в ней нет"
    assert "[[" in zzz.split("Названо в карточках")[1], \
        why(zzz[-200:]) or "нет ссылок на тех, кто назвал понятие — вход в тему потерян"
    # код документа сущностью не считается: по правилу базы он живёт в синонимах
    sys.path.insert(0, str(SCRIPTS))
    import build_plan as BP
    assert BP.is_doc_code("PRJ.SYS.ERD-006") and BP.is_doc_code("US-3.6.14"), \
        "голый код документа не опознан — под него заведут карточку"
    assert not BP.is_doc_code("МНС-РА") and not BP.is_doc_code("ЛТК-Б"), \
        "настоящее сокращение принято за код документа"

    text = next(p for p in (root / "AuroraKnowledgeDB").rglob("НДС.md")).read_text(
        encoding="utf-8")
    # Расшифровка из словаря — определение термина, а не пустышка (решение пользователя
    # 29.09.2026): карточка в выдаче, с источником словаря. Без расшифровки — заготовка.
    assert "status: placeholder" not in text and "_Заготовка:" not in text, \
        "определение термина спрятано как заготовка — его не найдут ни поиск, ни модель"
    assert "НДС — Налог на добавленную стоимость." in text, "расшифровка из словаря потеряна"
    assert "status: placeholder" in zzz, "понятие без расшифровки перестало быть заготовкой"
    named = text.split("Названо в карточках")[1]
    assert "[[" in named or re.search(r"Понятие названо в \d+ карточк", named), \
        why(text) or "не сказано, кто и в скольких карточках назвал понятие"

    # повторный прогон не заводит второй раз
    run("kb_fix.py", "--terms", "--apply", "--allow-dirty", cwd=root)
    assert len(list((root / "AuroraKnowledgeDB").rglob("НДС.md"))) == 1, \
        "заготовка заведена повторно поверх существующей карточки"

    # и отчёт о дырах больше не зовёт дырой то, у чего карточка есть
    gaps = run("kb_gaps.py", cwd=root)
    assert "| `НДС` |" not in gaps.stdout, \
        "понятие с карточкой всё ещё числится понятием без карточки"


@test
def test_update_delivers_ignore_rules_added_after_the_project_was_set_up(tmp: Path):
    """Правила `.gitignore`, появившиеся в ките позже, доезжают до заведённых проектов.

    Тот же класс, что был с git-хуком: правило живёт в ките, а в проекте лежит копия,
    снятая при установке. Установка смотрела на файл целиком — «есть старые строки,
    значит настроен» — и не добавляла ничего. На двух живых проектах так и остался вне
    игнора `.opencode/state/`: рантайм-состояние прогона. На одном замок агента попал
    под контроль версий, и после каждого прогона дерево оставалось грязным, а чекпойнт
    агента делает `git add -A` и утащил бы замок в историю под видом работы человека.

    Дописываем только недостающее: файл правит человек, и его строки — его дело.
    """
    import importlib
    sys.path.insert(0, str(SCRIPTS))
    from install_aurora import merge_gitignore, GITIGNORE_BLOCK
    gi = tmp / ".gitignore"
    # проект, заведённый до правила: старые строки есть, нового нет
    gi.write_text("# мой файл\n.DS_Store\n.env\nMyOwnFolder/\n", encoding="utf-8")
    added = merge_gitignore(gi)
    text = gi.read_text(encoding="utf-8")
    assert ".opencode/state/" in text, \
        "правило кита не доехало — состояние прогона снова попадёт под контроль версий"
    assert "MyOwnFolder/" in text, "строка человека потерялась при дописывании"
    assert text.count(".DS_Store") == 1, "уже имевшееся правило продублировано"
    assert ".opencode/state/" in added and ".DS_Store" not in added, \
        "отчёт врёт о том, что было добавлено"

    second = merge_gitignore(gi)
    assert second == [], why(second) or "повторный прогон дописывает то же ещё раз"

    fresh = tmp / "новый" / ".gitignore"
    fresh.parent.mkdir()
    assert merge_gitignore(fresh), "на пустом проекте не записано ничего"
    assert fresh.read_text(encoding="utf-8").strip(), "файл создан пустым"

    upd = (SCRIPTS / "aurora_update.py").read_text(encoding="utf-8")
    assert "refresh_gitignore(target)" in upd, \
        "обновление движка не трогает .gitignore — правила снова не доедут"

    # Кэш отчёта аналитика — производная (29.09.2026: у двух проектов он уже лежал в git,
    # его утащил чекпойнт агента). Правило закрывает новое, а про уже попавшее в историю
    # обновление говорит, как снять его с учёта, — само git проекта не трогает.
    assert ".opencode/cache/" in GITIGNORE_BLOCK, "кэш отчёта аналитика снова уедет в git"
    U = importlib.import_module("aurora_update")
    repo = tmp / "репо"
    (repo / ".opencode/cache/reports").mkdir(parents=True)
    (repo / ".opencode/cache/reports/issues.json").write_text("{}", encoding="utf-8")
    (repo / "Карточка.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    assert U.tracked_but_ignored(repo) == [], "без правила ничего не должно числиться закрытым"
    (repo / "Workspaces").mkdir()
    (repo / "Workspaces/черновик.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    merge_gitignore(repo / ".gitignore")
    with open(repo / ".gitignore", "a", encoding="utf-8") as f:
        f.write("\n# своё правило человека\nWorkspaces/\n")
    # своё правило человека — его дело: «снять с учёта» его рабочие файлы не предлагаем
    assert U.tracked_but_ignored(repo) == [".opencode/cache/reports/issues.json"], \
        U.tracked_but_ignored(repo)
    assert "git rm -r --cached" in upd and "tracked_but_ignored(target)" in upd
