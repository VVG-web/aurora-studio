"""Проверки движка Aurora, часть 71. Каркас и помощники — tests/harness.py.

Шаблоны карточек, фаза 6 «Миграция», пакет CT-19 — `agent:conform`: доводка машинных карточек знания, чья собственная
часть не по шаблону (первая строка — не определение, разделы не из списка типа), дословным переносом строк. Модель
называет только номера строк; скрипт расставляет заголовки, сверяет состав строк и результат с шаблоном и только потом
пишет. Модель подставная (`call=`), сети нет; проекты — только во временных папках.
"""
from __future__ import annotations

import collections
import importlib
import json
import os
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, run, test  # noqa: F401

from cases import part_70 as P70  # noqa: E402 — фикстуры миграции: проект с шаблонами кита, карточка, git

KB = "AuroraKnowledgeDB"
LEAD = P70.LEAD
SECOND = "Срок подтверждения — три рабочих дня."
STEP1 = "- Проверить заявку на полноту."
STEP2 = "- Подтвердить заявку поставщику."


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _cfg():
    return _mod("agent_core").parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})


def _answer(payload) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"ok": True, "text": text, "backend": 1, "model": "m", "tps": 9, "log": []}


class _Here:
    """Процесс стоит в корне проекта (скрипты движка ждут этого); журнал сбоев — во временную папку."""

    def __init__(self, root: Path):
        self.root = root

    def __enter__(self):
        self.was = os.getcwd()
        self.run_dir = os.environ.get("AURORA_RUN_DIR")
        os.environ["AURORA_RUN_DIR"] = str(self.root.parent / "run")
        os.makedirs(os.environ["AURORA_RUN_DIR"], exist_ok=True)
        os.chdir(self.root)
        return self.root

    def __exit__(self, *exc):
        os.chdir(self.was)
        if self.run_dir is None:
            os.environ.pop("AURORA_RUN_DIR", None)
        else:
            os.environ["AURORA_RUN_DIR"] = self.run_dir
        return False


# карточка процесса: первая строка — элемент списка (V-12), определение стоит второй строкой
MESSY = STEP1 + "\n" + LEAD + "\n" + STEP2 + "\n" + SECOND + "\n"
GOOD = {"lead": 2, "body": [4], "sections": {"Шаги": [1, 3]}}


def _process(root: Path, name: str = "Приём", own: str = MESSY, **over) -> Path:
    fm = {"type": "process", "title": f'"{name}"'}
    fm.update(over)
    return P70._card(root, f"Processes/{name}.md", own=own, **fm)


def _queue(R, root: Path) -> list:
    return [os.path.basename(p) for p in R.conform_queue(str(root))]


def _lines(text: str) -> collections.Counter:
    return collections.Counter(ln.strip() for ln in text.split("\n") if ln.strip() and not ln.startswith("## "))


def _own(text: str) -> str:
    C = _mod("aurora_common")
    full = C.body(text)
    return full.split("## Источник (перенесено дословно)")[0]


@test
def test_agent_conform_lays_the_lines_out_and_stamps(tmp: Path):
    """AC-CT-19.1: корректная раскладка — карточка пишется со штампом, множество строк сохранено, история дополнена."""
    R = _mod("agent_runner")
    root = P70._project(tmp)
    path = _process(root)
    P70._commit(root)
    before = path.read_text(encoding="utf-8")
    calls = []

    def model(cfg, role, messages, **kw):
        calls.append((role, messages[0]["content"]))
        return _answer(GOOD)

    with _Here(root):
        assert _queue(R, root) == ["Приём.md"]
        res = R.run_conform(_cfg(), str(root), True, call=model)
    assert [s["status"] for s in res["steps"]] == ["разложена"], res["steps"]
    assert len(calls) == 1 and calls[0][0] == "worker"
    ask = calls[0][1]
    assert "Тип карточки: Процесс" in ask and "Участники, Шаги, Исключения, Результат" in ask, ask[:600]
    assert f"2. {LEAD}" in ask and "1. " + STEP1 in ask, ask
    text = path.read_text(encoding="utf-8")
    assert "template: akdb_process_v1.0.md" in text, text
    own = _own(text)
    assert own.strip().startswith(LEAD + "\n\n" + SECOND + "\n\n## Шаги\n\n" + STEP1 + "\n" + STEP2), own
    assert _lines(own) == _lines(_own(before)), "состав строк собственной части изменился"
    assert ("собственная часть разложена по разделам шаблона `akdb_process_v1.0.md` (строки перенесены без изменения текста)"
            in text), text
    # сверка шапок и хвоста: всё, кроме собственной части, шапки-штампа и истории, прежнее
    C = _mod("aurora_common")
    assert "Текст страницы источника." in text and C.frontmatter(text)["built"] == "machine"
    # повтор: карточка штампована, модель не зовётся, очередь пуста, `kb:conform` остатка не видит
    calls.clear()
    with _Here(root):
        assert _queue(R, root) == []
        res = R.run_conform(_cfg(), str(root), True, call=model)
    assert not calls and res["cards"] == 0
    assert path.read_text(encoding="utf-8") == text


@test
def test_agent_conform_verbatim_check_rejects_a_bad_answer(tmp: Path):
    """AC-CT-19.2: потерянная или добавленная строка, повтор номера, чужой раздел, негодное определение — карточка не
    меняется, причина в шаге; отброшенная откладывается до правки файла."""
    R = _mod("agent_runner")
    bad = [
        ({"lead": 2, "body": [4], "sections": {"Шаги": [1]}}, "по одному разу"),                       # потеряна строка 3
        ({"lead": 2, "body": [4, 5], "sections": {"Шаги": [1, 3]}}, "по одному разу"),                  # добавлена строка 5
        ({"lead": 2, "body": [4, 4], "sections": {"Шаги": [1, 3]}}, "по одному разу"),                  # номер дважды
        ({"lead": 2, "body": [4], "sections": {"Выдумка": [1, 3]}}, "не из списка типа"),               # раздел не из списка
        ({"lead": 1, "body": [4], "sections": {"Шаги": [2, 3]}}, "lead — не обычный текст"),           # определение — элемент списка
        ({"lead": "два", "body": [4], "sections": {"Шаги": [1, 3]}}, "номер"),                          # не число
        ({"lead": 2, "body": [4], "sections": []}, "ожидается объект"),
        ("не JSON", "не разобран"),
        ([1, 2, 3], "не разобран"),
    ]
    for k, (payload, expect) in enumerate(bad):
        sub = tmp / f"case{k}"
        sub.mkdir()
        root = P70._project(sub)
        path = _process(root)
        P70._commit(root)
        before = path.read_text(encoding="utf-8")
        calls = []

        def model(cfg, role, messages, payload=payload, calls=calls, **kw):
            calls.append(1)
            return _answer(payload)

        with _Here(root):
            res = R.run_conform(_cfg(), str(root), True, call=model)
            st = res["steps"][0]
            assert st["status"] == "отброшена" and expect in st["note"], (payload, st)
            assert path.read_text(encoding="utf-8") == before, f"файл изменился при ответе {payload!r}"
            # отброшенная откладывается: повторный прогон модель не зовёт
            assert _queue(R, root) == [], "отброшенная карточка осталась в очереди"
            R.run_conform(_cfg(), str(root), True, call=model)
            assert len(calls) == 1, "модель позвана повторно по отложенной карточке"
            # правка файла возвращает карточку в очередь
            path.write_text(before + "\nДобавлено человеком.\n", encoding="utf-8")
            assert _queue(R, root) == ["Приём.md"]


@test
def test_agent_conform_preview_writes_nothing_and_a_model_failure_keeps_the_card(tmp: Path):
    R = _mod("agent_runner")
    root = P70._project(tmp)
    path = _process(root)
    P70._commit(root)
    before = path.read_text(encoding="utf-8")
    with _Here(root):
        res = R.run_conform(_cfg(), str(root), False, call=lambda cfg, role, messages, **kw: _answer(GOOD))
        assert res["steps"][0]["status"] == "разложил бы" and path.read_text(encoding="utf-8") == before
        assert not (root / ".aurora" / "state" / "conform_parked.json").exists()
        # шлюз не ответил: карточка остаётся в очереди, не откладывается
        down = lambda cfg, role, messages, **kw: {"ok": False, "text": "", "backend": 1, "log": ["обрыв"]}  # noqa: E731
        res = R.run_conform(_cfg(), str(root), True, call=down)
        assert res["steps"][0]["status"] == "сбой", res["steps"]
        assert path.read_text(encoding="utf-8") == before
        assert _queue(R, root) == ["Приём.md"]


@test
def test_agent_conform_takes_only_the_cards_it_is_for(tmp: Path):
    """Очередь: машинное знание без штампа с нарушениями только структуры собственной части; не штампованные-годные,
    документы, словари, заготовки, неизвестное происхождение, длинные, с нарушением другого рода — не в очереди; карточка
    без строки, годной на определение, модель не зовёт."""
    R = _mod("agent_runner")
    root = P70._project(tmp)
    _process(root, "Годная")
    _process(root, "Уже", own=LEAD + "\n")                                                    # проходит: ей нужен штамп, не раскладка
    _process(root, "Документ", kind="document")
    _process(root, "Заготовка", status="placeholder")
    _process(root, "Ручная", built=None, sources=" []")
    _process(root, "Длинная", own=STEP1 + "\n" + ("Слово " * 1100) + "\n")                      # собственная часть > 6000 знаков
    _process(root, "Без даты", created=None)                                                  # другое нарушение (V-02): сначала kb:conform
    _process(root, "Штамп", template="akdb_process_v1.0.md", own=LEAD + "\n")
    _process(root, "Одна строка", own=STEP1 + "\n" + ("Слово " * 105) + "\n")                  # единственная годная по виду строка > 600
    P70._commit(root)
    with _Here(root):
        got = _queue(R, root)
        assert got == ["Годная.md"], got        # «Одна строка»: определение длиннее 600 знаков, переносом строк не лечится
        called = []

        def model(cfg, role, messages, **kw):
            called.append(messages[0]["content"])
            return _answer(GOOD)

        res = R.run_conform(_cfg(), str(root), True, call=model)
    assert [s["card"] for s in res["steps"]] == ["Годная"], res["steps"]
    assert len(called) == 1, "модель позвана по карточке без годной строки"
    # разведка миграции судит так же: «Одна строка», «Длинная» и «Ручная» — остаток, а не очередь агента; в очереди
    # остаётся только «Без даты» (шапку починит `kb:conform`, после чего её возьмёт агент)
    out = json.loads(run("kb_conform.py", "--json", cwd=root).stdout)["outcomes"]
    assert out["queue"] == 1 and out["rest"] == 3, out


@test
def test_agent_conform_keeps_tables_fences_title_and_meeting_mark(tmp: Path):
    """Таблица, ограда кода и строка с продолжением — одна единица; `# Заголовок`, пометка встречи и служебные разделы
    остаются на месте; заголовки прежних групп в состав строк не входят."""
    R = _mod("agent_runner")
    root = P70._project(tmp)
    mark = "> 🎙 Из встречи: 01.10.2026 — сказано в разговоре, а не записано в документе. Стенограмма: `Raw/meetings/a.md`."
    own = ("# Приём\n\n## Прежняя группа\n\n" + STEP1 + "\n  - вложенный пункт\n| Поле | Значение |\n| --- | --- |\n| срок | 3 |\n"
           + LEAD + "\n```\nкод\n```\n" + SECOND + "\n\n" + mark + "\n\n## Не путать\n\n[[Отгрузка]] — другая сущность.\n")
    path = _process(root, own=own)
    P70._commit(root)
    seen = {}

    def model(cfg, role, messages, **kw):
        seen["ask"] = messages[0]["content"]
        return _answer({"lead": 3, "body": [5], "sections": {"Шаги": [1], "Результат": [2, 4]}})

    with _Here(root):
        res = R.run_conform(_cfg(), str(root), True, call=model)
    assert res["steps"][0]["status"] == "разложена", res["steps"]
    ask = seen["ask"]
    assert "1. " + STEP1 + "\n  - вложенный пункт" in ask and "2. | Поле | Значение |\n| --- | --- |\n| срок | 3 |" in ask, ask
    assert "[заголовок прежней группы: ## Прежняя группа" in ask and "# Приём" not in ask.split("Строки пронумерованы:")[1]
    text = path.read_text(encoding="utf-8")
    new_own = _own(text)
    assert new_own.strip().startswith("# Приём\n\n" + LEAD + "\n\n" + SECOND), new_own
    assert "\n\n## Шаги\n\n" + STEP1 + "\n  - вложенный пункт\n\n## Результат\n\n| Поле" in new_own, new_own
    assert new_own.index(mark) > new_own.index("## Не путать") > new_own.index("## Результат"), new_own   # пометка встречи — последней
    assert new_own.rstrip().endswith(mark), new_own
    assert _lines(new_own) == _lines(own)


@test
def test_conform_add_history_places_the_line(tmp: Path):
    K = _mod("kb_conform")
    line = "- 2026-10-19: собственная часть разложена"
    base = "---\ntitle: x\n---\n\nТекст.\n\n## Источник (перенесено дословно)\n\nИсточник.\n"
    got = K.add_history(base, line)
    assert got == base + "\n## История изменений\n\n" + line + "\n", got
    # раздел есть: строка встаёт в конец раздела, следующий раздел не сдвигается
    with_footer = base + "\n## История изменений\n\n- 2026-09-01: тезис написан.\n\n## Не путать\n\n[[A]]\n"
    got = K.add_history(with_footer, line)
    assert got == base + "\n## История изменений\n\n- 2026-09-01: тезис написан.\n" + line + "\n\n## Не путать\n\n[[A]]\n", got
    # слияние: история встаёт перед ним
    merged = base + "\n## Слияние\n\nТело донора.\n"
    got = K.add_history(merged, line)
    assert got.index("## История изменений") < got.index("## Слияние") and got.endswith("Тело донора.\n"), got
    # пустой раздел
    empty = base + "\n## История изменений\n"
    assert K.add_history(empty, line) == base + "\n## История изменений\n\n" + line + "\n"


@test
def test_agent_conform_keeps_the_paragraph_structure(tmp: Path):
    """Перенос не склеивает абзацы и не делает абзац продолжением пункта списка или строку перед чертой — заголовком:
    рядом стоят только то, что стояло вплотную, и пункты списка."""
    R = _mod("agent_runner")
    root = P70._project(tmp)
    own = ("- пункт, с которого карточка начинается\n\n" + LEAD + "\n\n- a\n- b\n\nАбзац после списка.\n\n---\n\n"
           "Абзац после черты.\n\n> цитата\n\nАбзац после цитаты.\n")
    path = _process(root, own=own)
    P70._commit(root)
    with _Here(root):
        res = R.run_conform(_cfg(), str(root), True, call=lambda *a, **kw: _answer(
            {"lead": 2, "body": [3, 4, 5, 6, 7, 8, 9], "sections": {"Шаги": [1]}}))
    assert res["steps"][0]["status"] == "разложена", res["steps"]
    new_own = _own(path.read_text(encoding="utf-8"))
    assert LEAD + "\n\n- a\n- b\n\nАбзац после списка.\n\n---\n\nАбзац после черты.\n\n> цитата\n\nАбзац после цитаты." in new_own, new_own
    assert "\n\n## Шаги\n\n- пункт, с которого карточка начинается" in new_own, new_own
    assert _lines(new_own) == _lines(own)
    # связанные в исходнике строки остаются вместе
    glued = _process(root, "Склеенные", own="- первый шаг\n" + LEAD + "\nВторая фраза.\nТретья фраза.\n")
    P70._commit(root)
    with _Here(root):
        R.run_conform(_cfg(), str(root), True, call=lambda *a, **kw: _answer(
            {"lead": 2, "body": [3, 4], "sections": {"Шаги": [1]}}))
    assert _own(glued.read_text(encoding="utf-8")).strip().startswith(LEAD + "\nВторая фраза.\nТретья фраза.\n\n## Шаги")


@test
def test_agent_conform_skips_what_a_layout_cannot_cure(tmp: Path):
    """Карточка в чужом разделе штамп не получит — модель не зовут; посторонний заголовок после служебных разделов
    раскладке не поддаётся; негодные «определения» (черта, подчёркивание, код с отступом, комментарий) не годятся."""
    R = _mod("agent_runner")
    K = _mod("kb_conform")
    C = _mod("card_types")
    root = P70._project(tmp)
    _process(root, "Своя")
    P70._card(root, "Concepts/Чужая.md", own=MESSY, type="process", title='"Чужая"')       # процесс в разделе понятий
    _process(root, "Хвост", own=MESSY + "\n## Не путать\n\n[[Отгрузка]] — другая.\n\n## Заметки\n\nПосторонний раздел.\n")
    P70._commit(root)
    with _Here(root):
        assert _queue(R, root) == ["Своя.md"]
    reg = C.registry(str(root), kit_root=str(KIT))
    tail = K.plan_conform((root / KB / "Processes" / "Хвост.md").read_text(encoding="utf-8"), reg)
    assert isinstance(tail, str) and "посторонний заголовок" in tail, tail
    for bad in ("---", "===", "    код с отступом", "\tкод", "<!-- комментарий -->", "```", "- пункт", "> цитата", "| a |", "# Заголовок"):
        assert not K.lead_ok(bad), bad
    assert K.lead_ok("Обычное предложение.") and not K.lead_ok("Слово " * 120)


@test
def test_agent_conform_does_not_overwrite_an_edit_made_during_the_call(tmp: Path):
    """Вызов модели идёт минуты: правку человека, сделанную за это время, запись не затирает; карточка не отложена."""
    R = _mod("agent_runner")
    root = P70._project(tmp)
    path = _process(root)
    P70._commit(root)
    edited = {}

    def model(cfg, role, messages, **kw):
        edited["text"] = path.read_text(encoding="utf-8").replace("Подтвердить", "Принять")
        path.write_bytes(edited["text"].encode("utf-8"))
        return _answer(GOOD)

    with _Here(root):
        res = R.run_conform(_cfg(), str(root), True, call=model)
        assert res["steps"][0]["status"] == "пропущена" and "изменился" in res["steps"][0]["note"], res["steps"]
        assert path.read_text(encoding="utf-8") == edited["text"], "правка человека затёрта"
        assert not (root / ".aurora" / "state" / "conform_parked.json").exists()
        assert _queue(R, root) == ["Приём.md"]


@test
def test_agent_conform_accepts_numbers_as_strings_and_trims_section_names(tmp: Path):
    R = _mod("agent_runner")
    root = P70._project(tmp)
    path = _process(root)
    P70._commit(root)
    with _Here(root):
        res = R.run_conform(_cfg(), str(root), True, call=lambda *a, **kw: _answer(
            {"lead": "2", "body": ["4"], "sections": {" Шаги ": ["1", 3]}}))
    assert res["steps"][0]["status"] == "разложена", res["steps"]
    assert "template: akdb_process_v1.0.md" in path.read_text(encoding="utf-8")


@test
def test_agent_conform_parked_memory_is_atomic_and_shared_with_the_scout(tmp: Path):
    """Память отложенного пишется атомарно и читается тем же кодом, что у разведки: отложенная карточка не в очереди ни
    у агента, ни у `kb:conform`; смена реестра типов возвращает её."""
    R = _mod("agent_runner")
    K = _mod("kb_conform")
    root = P70._project(tmp)
    path = _process(root)
    P70._commit(root)
    assert R.CONFORM_PARKED == K.PARKED_REL
    with _Here(root):
        R.run_conform(_cfg(), str(root), True, call=lambda *a, **kw: _answer("не JSON"))
        memory = K.parked_memory(str(root))
        assert list(memory) == ["AuroraKnowledgeDB/Processes/Приём.md"], memory
        assert K.is_parked(str(root), str(path)) and _queue(R, root) == []
        assert not [p for p in (root / ".aurora" / "state").iterdir() if p.name.endswith(".aurora-tmp")]
        # реестр типов изменился (выключили другой тип) — отложенная карточка возвращается в очередь
        cfg = root / "aurora.config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8").rstrip("\n") + '\ncards:\n  system:\n    enabled: "false"\n', encoding="utf-8")
        assert _queue(R, root) == ["Приём.md"]
