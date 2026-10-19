"""Проверки движка Aurora, часть 82: потерянные обновления карточек (BL-SEC-15).

Каркас и помощники — tests/harness.py. Тезис записывался из снимка, прочитанного минуты назад, до вызова модели:
правка человека в Obsidian за это время затиралась без следа. Теперь запись карточки идёт под замком проекта, сверяет
отпечаток снимка с файлом и при расхождении отказывает (`CardChanged`); прогон не отмечает карточку обработанной и не
откладывает её, а возвращает в очередь. Дописывание в карточку-приёмник и отметка осмотра — чтение-правка-запись под
тем же замком. Модель — заглушка, сеть и настоящий HOME не используются.
"""
from __future__ import annotations

from pathlib import Path
import importlib
import json
import os
import sys
import threading

from harness import KIT, SCRIPTS, card, make_project, run, test  # noqa: F401

from cases import part_42 as P42  # noqa: E402 — исправная карточка и шаблоны первой части
from cases import part_47 as P47  # noqa: E402 — проект с шаблонами и путь карточки


def _mods():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("agent_core"), importlib.import_module("agent_runner"), importlib.import_module("card_types")


@test
def test_card_write_refuses_a_stale_snapshot_and_keeps_the_human_edit(tmp: Path):
    """`expect_sig`: пока карточка такая, какой её видел писавший, запись проходит; правку человека, внесённую после
    чтения, запись по старому снимку не затирает — `CardChanged`, файл остаётся таким, каким его оставил человек."""
    _AG, _R, C = _mods()
    root = P47._project(tmp)
    reg = P47._reg(root)
    p = P47._path(root)
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    base = P42.mk()
    Path(p).write_bytes(base.encode("utf-8"))
    sig = C.file_sig(p)
    assert sig and C.file_sig(str(tmp / "нет-такого.md")) == ""
    assert C.write_card_text(p, base + "\nДобавлено движком.\n", op="body", reg=reg, expect_sig=sig) is True
    human = base + "\nПравка человека в Obsidian.\n"
    Path(p).write_bytes(human.encode("utf-8"))                       # человек правит, пока движок ждёт модель
    before = Path(p).read_bytes()
    try:
        C.write_card_text(p, base + "\nТезис по старому снимку.\n", op="body", reg=reg, expect_sig=sig)
    except C.CardChanged as e:
        assert "Заявка на поставку.md" in str(e), str(e)
    else:
        raise AssertionError("запись по устаревшему снимку принята")
    assert Path(p).read_bytes() == before, "правка человека затёрта"
    assert not [f for f in Path(p).parent.iterdir() if f.name.endswith(".aurora-tmp")]
    ok, why = C.try_write_card(p, human + "ещё\n", op="body", reg=reg)               # без expect_sig запись как прежде
    assert ok and not why, why
    try:
        C.try_write_card(p, human, op="body", reg=reg, expect_sig=sig)
    except C.CardChanged:
        pass
    else:
        raise AssertionError("try_write_card проглотил изменение карточки: оно не отказ шаблона")
    assert (root / ".aurora" / "state" / "cards.lock").is_file(), "запись не шла под замком проекта"


@test
def test_distill_does_not_overwrite_an_edit_made_while_the_model_thinks(tmp: Path):
    """Сквозной случай AR2-03: во время вызова модели человек дописал в карточку строку. Тезис не записан, строка цела,
    карточка не отмечена тезисом и не отложена; со следующего прогона (без правок) тезис пишется, и строка человека
    остаётся в тексте."""
    AG, R, C = _mods()
    root = make_project(tmp)
    (root / "Sources/Confluence").mkdir(parents=True, exist_ok=True)
    (root / "Sources/Confluence/Стр.md").write_text("Возврат за 10 дней. Правило действует для всех заявок.", encoding="utf-8")
    cp = run("build_plan.py", "--card", "Возврат", "--source", "Sources/Confluence/Стр.md", "--paras", "1",
             "--to", "Concepts", "--apply", cwd=root)
    path = root / "AuroraKnowledgeDB/Concepts/Возврат.md"
    assert path.exists(), f"карточка не собрана:\n{cp.stdout}{cp.stderr}"
    path.write_text(path.read_text(encoding="utf-8").replace("built: machine", "built: machine\nkind: knowledge"),
                    encoding="utf-8")
    edit = {"on": True}

    def model(cfg_, role, messages, **kw):
        if edit["on"]:
            path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + "\n\nВажная правка человека: срок считать в рабочих днях.\n",
                            encoding="utf-8")
        return {"ok": True, "text": "ТЕЗИС:\nВозврат занимает десять дней.\n\nИЗМЕНИЛОСЬ:\nпервый тезис",
                "backend": 1, "model": "m", "tps": 9, "log": []}

    cfg = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m"})
    res = R.run_distill(cfg, str(root), apply=True, limit=3, momus=False, call=model)
    now = path.read_text(encoding="utf-8")
    assert "Важная правка человека" in now, "правка человека затёрта тезисом из старого снимка"
    assert "distilled:" not in now, "карточка отмечена тезисом, который не записан"
    assert [s["status"] for s in res["steps"]] == [R.CHANGED], res["steps"]
    assert not R.parked_cards(str(root)), "изменённая карточка отложена как отказ шаблона или «человеку»"
    edit["on"] = False
    res = R.run_distill(cfg, str(root), apply=True, limit=3, momus=False, call=model)
    done = path.read_text(encoding="utf-8")
    assert "distilled:" in done and "Важная правка человека" in done, done
    assert [s["status"] for s in res["steps"]] != [R.CHANGED], res["steps"]
    assert C.file_sig(str(path)), "карточка пропала"


@test
def test_appends_to_one_card_from_many_threads_lose_nothing(tmp: Path):
    """Карточка-приёмник: определения дописывают потоки (и процессы, где `threading.Lock` уже не защищает). Чтение
    карточки и запись идут под замком проекта — ни одно определение не пропало."""
    _AG, R, _C = _mods()
    root = make_project(tmp)
    card(root, "Concepts/ФЦОД.md", "Центр обработки данных.", kind="knowledge", status="draft",
         created="2026-07-01", updated="2026-07-01", sources='\n  - "Sources/Confluence/Центр.md"')
    errors = []

    def put(k):
        try:
            for n in range(6):
                R.place_definition(str(root), "ФЦОД", f"Определение номер {k}-{n} вынесено из карточки-донора.", f"Донор-{k}",
                                   donor_sources=[f"Sources/Confluence/Донор-{k}.md"])
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=put, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(180)
    assert not errors, errors[:2]
    text = (root / "AuroraKnowledgeDB/Concepts/ФЦОД.md").read_text(encoding="utf-8")
    lost = [f"{k}-{n}" for k in range(4) for n in range(6) if f"Определение номер {k}-{n} " not in text]
    assert not lost, f"потерянные дописывания: {lost[:6]} ({len(lost)} из 24)"


@test
def test_split_of_a_changed_card_is_refused_before_any_part_is_written(tmp: Path):
    """Разрез карточки по границам, построенным на старом тексте: карточка изменилась — ни одна часть и ни карта не
    записаны (`CardChanged`), правка человека цела."""
    _AG, R, C = _mods()
    root = make_project(tmp)
    folder = root / "AuroraKnowledgeDB/Reference"
    folder.mkdir(parents=True, exist_ok=True)
    body = "\n\n".join(f"Термин {i}. Определение термина {i} и правила применения. " * 6 for i in range(1, 13))
    src = folder / "Справочник.md"
    src.write_text(f'---\nid: X\ntitle: "Справочник"\nkind: dictionary\nstatus: knowledge\ntype: reference\n'
                   f'source: "Sources/C/S.md"\n---\n\n{R.QUOTES}\n{body}\n', encoding="utf-8")
    sig = C.file_sig(str(src))
    src.write_text(src.read_text(encoding="utf-8") + "\nДобавил человек.\n", encoding="utf-8")
    human = src.read_bytes()
    parts = [("Термины 1-6", "первая половина"), ("Термины 7-12", "вторая половина")]
    try:
        R.apply_split_parts(str(src), parts, expect_sig=sig)
    except C.CardChanged:
        pass
    else:
        raise AssertionError("разрез по устаревшему снимку принят")
    assert src.read_bytes() == human, "карта документа записана поверх правки человека"
    assert sorted(p.name for p in folder.iterdir()) == ["Справочник.md"], "часть записана до проверки снимка"


@test
def test_relinking_does_not_overwrite_an_edit_made_while_the_model_thinks(tmp: Path):
    """Связывание берёт снимок карточки до вызова модели: правка человека за это время не затирается, шаг —
    «изменена», связи не записаны, карточка не отмечена связанной и вернётся в очередь."""
    _AG, R, C = _mods()
    root = make_project(tmp)
    thesis = ("Аналитический баланс получает данные из ФЦОД по расписанию и обновляет профиль абонента. Остатки "
              "хранятся за расчётный период. Сверка проводится ежедневно и фиксируется в журнале операций по каждому счёту.")
    card(root, "Concepts/Журнал-учёта-операций.md", status="draft", kind="knowledge", body="Журнал и его назначение. " * 3)
    path = card(root, "Concepts/Аналитический-баланс.md", status="draft", kind="knowledge", distilled="2026-09-01", body=thesis)
    cfg = {"request_timeout": 60, "budget_min": 5, "backends": [], "thinking": False, "thinking_roles": {},
           "embed": {"model": "m"}}

    def model(c, role, messages, **kw):
        path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + "\n\nПравка человека: баланс считается ночью.\n", encoding="utf-8")
        return {"ok": True, "backend": 1, "model": "m", "log": [],
                "text": '{"links": [{"phrase": "журнале операций", "card": "Журнал-учёта-операций"}]}'}

    was, here = R.candidates_for, os.getcwd()
    try:
        os.chdir(root)
        R.candidates_for = lambda *a, **k: [("Журнал-учёта-операций", "Concepts", "")]
        st = R.relink_card(cfg, str(path), model, apply=True)
    finally:
        R.candidates_for = was
        os.chdir(here)
    now = path.read_text(encoding="utf-8")
    assert "Правка человека: баланс считается ночью." in now, "правка человека затёрта связыванием"
    assert st["status"] == R.CHANGED and st["added"] == 0, st
    assert "[[Журнал-учёта-операций" not in now, "связи записаны поверх чужой правки"
    assert "relinked:" not in now, "карточка отмечена связанной"


@test
def test_a_split_verdict_about_an_edited_card_is_not_parked(tmp: Path):
    """Планировщик резал карточку, а человек правил её за время вызова: разрез отменён, вердикт к правленому тексту не
    приклеен — карточка не числится отложенной и вернётся в очередь."""
    AG, R, C = _mods()
    kb = tmp / "AuroraKnowledgeDB/Reference"
    kb.mkdir(parents=True)
    para = lambda i: f"Термин {i}. Определение термина {i} и правила применения. " * 6
    body = "\n\n".join(para(i) for i in range(1, 31))
    card_path = kb / "Справочник.md"
    card_path.write_text(f'---\nid: X\ntitle: "Справочник"\nkind: dictionary\nstatus: knowledge\ntype: reference\n'
                         f'source: "Sources/C/S.md"\n---\n\n{R.QUOTES}\n{body}\n', encoding="utf-8")

    def model(cfg_, role, messages, **kw):
        card_path.write_text(card_path.read_text(encoding="utf-8").rstrip("\n") + "\n\nДобавил человек.\n", encoding="utf-8")
        rows = {"parts": [{"title": f"Термины {k*10+1}-{k*10+10}", "from": 1 + k * 10, "to": 10 + k * 10} for k in range(3)]}
        return {"ok": True, "text": json.dumps(rows, ensure_ascii=False), "backend": 1, "model": "p", "tps": 9, "log": []}

    cfg = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                           "AURORA_AGENT_BACKEND_1_CONTEXT": "3000"})
    res = R.run_distill(cfg, str(tmp), apply=True, limit=5, momus=False, call=model)
    assert [s["status"] for s in res["steps"]] == [R.CHANGED], res["steps"]
    assert res["left"] == 1, "изменённая карточка засчитана обработанной"
    assert "Добавил человек." in card_path.read_text(encoding="utf-8")
    assert sorted(p.name for p in kb.iterdir()) == ["Справочник.md"], "части записаны поверх правки"
    assert not R.still_parked(str(tmp), str(card_path), R.parked_cards(str(tmp))), "правленая карточка осталась отложенной"
    assert "изменены во время прогона" in R.report_distill(res, True)
