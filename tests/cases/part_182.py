"""Проверки движка Aurora, часть 182. Каркас — tests/harness.py.

Найдено на живом проекте: производство (`agent:make`) с большим паком знаний ронялось на «запрос длиннее окна модели»
и «artifact not created». Пак больше окна нарезается по карточкам, каждая часть получает свой конспект моделью,
и дальше идёт короткий пак; для возобновлённой сессии то же самое, но не дважды.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from harness import KIT, make_project, test


def _runner():
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_runner as R
    return R


def card(root: Path, name: str, text: str) -> None:
    path = root / "AuroraKnowledgeDB" / "Concepts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {name}\nstatus: knowledge\n---\n\n{text}\n", encoding="utf-8")


@test
def test_pack_is_split_by_cards_and_a_single_big_card_by_lines(_t):
    """Части пака не длиннее бюджета; разрез по заголовкам карточек, одна большая карточка — по строкам; весь текст
    сохраняется (конкатенация частей равна исходному паку, без потерь и дублей)."""
    R = _runner()
    body = lambda i: "строка текста по делу\n" * (i + 3)
    pack = "".join(f"## Карточка {i}\n\n{body(i)}" for i in range(6))
    budget = 400
    parts = R.split_pack_for_budget(pack, budget)
    assert len(parts) > 1, parts
    assert all(len(p) <= budget for p in parts), [len(p) for p in parts]
    assert all(p.startswith("## Карточка") for p in parts), [p[:20] for p in parts]
    assert "".join(parts) == pack, "части не складываются в исходный пак"
    huge = "## Большая\n\n" + "строка длинного тела\n" * 60
    pieces = R.split_pack_for_budget(huge, 300)
    assert len(pieces) > 1 and all(len(p) <= 300 for p in pieces), [len(p) for p in pieces]
    assert "".join(pieces) == huge, "большая карточка потеряла текст при нарезке"
    assert R.split_pack_for_budget("маленький пак", 1000) == ["маленький пак"]


@test
def test_compact_pack_calls_model_once_per_chunk_and_keeps_a_small_result(_t):
    """Пак больше бюджета — по одному вызову на часть, каждый конспект ограничен капом, итог сильно меньше исходника;
    пак поменьше возвращается без изменений и без вызовов; отказ части — текст с её номером, а не молчание."""
    R = _runner()
    facts = "факт по делу.\n" * 200
    pack = "".join(f"## Карточка {i}\n\n{facts}" for i in range(8))
    calls = []
    deadlines = []

    def fake(cfg, role, messages, **kw):
        calls.append(messages[0]["content"])
        deadlines.append(kw.get("deadline") or 0)
        return {"ok": True, "text": f"конспект с источником [[Карточка {len(calls)}]]", "log": []}

    packed, n, why = R.compact_make_pack({}, pack, "новая история", 2000, fake)
    assert not why, why
    assert n == len(calls) == len(R.split_pack_for_budget(pack, 2000)), (n, len(calls))
    # У каждой части свой срок: общий дедлайн прогона к этому месту мог уже истечь.
    import time as _t
    assert all(d > _t.time() for d in deadlines), deadlines
    assert all("конспект" in c for c in calls), calls[0][:60]
    assert len(packed) < len(pack) / 4, (len(packed), len(pack))
    assert all(len(c) <= 2000 + 1200 for c in calls), "часть не помещается в бюджет"
    small, n2, _ = R.compact_make_pack({}, pack, "новая история", len(pack) + 100, fake)
    assert small == pack and n2 == 0 and len(calls) == n, "маленький пак не должен был конспектироваться"
    bad, _, why_bad = R.compact_make_pack({}, pack, "новая история", 2000,
                                         lambda cfg, role, messages, **kw: {"ok": False, "text": ""})
    assert not bad and why_bad and "части 1" in why_bad, why_bad


@test
def test_run_make_with_a_large_pack_compacts_it_and_continues_without_recompacting(tmp: Path):
    """Найдено на живом проекте: пак ~686 000 знаков уходил воркеру целиком, и производство ронялось на «запрос длиннее
    окна». Возобновлённая сессия с таким паком нарезается на конспекты; производство идёт дальше, и повторной
    нарезки при следующем заходе нет."""
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Критерии приёмки\n", encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "AC"\n'
                          '    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n', encoding="utf-8")
    d = R.make_session_dir(str(root), "ac")
    spec = R.make_spec(str(root), "ac")
    pack = "".join(f"## Заявка-{i}\n\n" + "Срок десять дней, проверка по отчёту. " * 900 for i in range(10))
    assert len(pack) > 200000, len(pack)
    R.save_session(str(root), d.name, {"sid": d.name, "kind": "ac", "idea": "срок заявки и проверка",
                                      "spec": spec, "pack": pack, "stages": {"enriched": "2026-11-01T08:00:00Z"},
                                      "rounds": [], "plan": ""})
    calls = []

    def fake(cfg, role, messages, **kw):
        content = messages[0]["content"]
        calls.append((role, content))
        if "ЧАСТЬ пака знаний" in content:
            return {"ok": True, "text": "конспект: срок десять дней [[Заявка-1]]", "log": []}
        if role == "planner":
            return {"ok": True, "text": json.dumps({"questions": [], "assumptions": [], "plan": "1. Критерии"},
                                                   ensure_ascii=False), "log": []}
        if role == "critic":
            return {"ok": True, "text": json.dumps({"ok": True, "issues": [], "coverage": {}}, ensure_ascii=False),
                    "log": []}
        return {"ok": True, "text": "# AC\n\nСрок ([[Заявка-1]]).", "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "15000"})
    r = R.run_make(cfg, str(root), "", "", d.name, "", False, call=fake, momus=False)
    assert r.get("ok"), f"производство упало: {r.get('why')}"
    compacts = [c for c in calls if "ЧАСТЬ пака знаний" in c[1]]
    assert compacts, "нарезки большого пака не было"
    assert len(compacts) == len(R.split_pack_for_budget(pack, 39000 - 8000)) or len(compacts) > 1, len(compacts)
    st = R.load_session(str(root), d.name)
    assert st.get("compacted"), "флаг конспектирования не записан"
    assert len(st.get("pack") or "") < 40000, f"пак остался гигантским: {len(st.get('pack') or '')}"
    before = len(calls)
    r2 = R.run_make(cfg, str(root), "", "", d.name, "", False, call=fake, momus=False)
    assert r2.get("ok"), f"возобновление упало: {r2.get('why')}"
    assert len(calls) == before, "возобновлённая сессия конспектировалась повторно"


@test
def test_cyrillic_is_measured_safely_so_the_live_pack_actually_compacts(tmp: Path):
    """Найдено на живом проекте: пак 686 839 знаков оценивался по три знака на токен (229 тыс.) и проходил проверку
    окна 262 144, а шлюз насчитал 284 781 токен и отказал. Теперь кириллица считается по 2,2 знака на токен, такой
    пак заранее виден как слишком большой и нарезается; после конспектов запрос влезает в окно."""
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R
    text = "Срок десять дней, проверка по отчёту. " * 19000
    assert len(text) > 680000, len(text)
    old = len(text) / A.CHARS_PER_TOKEN
    new = A.rough_tokens([{"content": text}])
    assert old < 262144 < new, (old, new)
    assert new > old * 1.15, "кириллица считается не строже латиницы"
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "262144"})
    assert A.prompt_budget_safe(cfg) == A.prompt_budget(cfg), "оба бюджета должны быть осторожными"
    budget = A.prompt_budget_safe(cfg, reserve_chars=21000)
    assert len(text) > budget
    calls = []

    def fake(cfg, role, messages, **kw):
        calls.append(messages[0]["content"])
        return {"ok": True, "text": "конспект: срок десять дней [[Заявка-1]]", "log": []}

    packed, n, why = R.compact_make_pack(cfg, text, "срок заявки", budget, fake)
    assert not why and n > 1, (n, why)
    assert len(packed) <= budget, (len(packed), budget)
    need = A.rough_tokens([{"content": packed}]) + A.ANSWER_ROOM
    assert need < 262144, f"даже после конспектов запрос не влезает: {need}"


@test
def test_the_trigger_counts_the_planner_prompt_and_the_grill_method(tmp: Path):
    """Найдено на живом проекте: пак 686 839 знаков без промпта планировщика и метода разбора выглядел как 261 471 токен
    при окне 262 144 — нарезка не просыпалась, а шлюз получал 284 781 токен и отказывал. Теперь в оценку входят
    `PROMPT_MAKE_PLAN` и метод разбора: тот же пак виден как слишком большой."""
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R
    pack = "Срок десять дней, проверка по отчёту. " * 19000
    template = "# AC\n\n## Критерии приёмки\n" * 500
    idea = "помоги сделать новую историю по постановке"
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "262144"})
    plan_base = R.PROMPT_MAKE_PLAN + R.grill_method(str(KIT))
    assert len(plan_base) > 3000, len(plan_base)
    need = A.rough_tokens([{"content": plan_base + template + idea + pack}]) + A.ANSWER_ROOM
    assert need > 262144, f"без промпта планировщика нарезка не проснётся: {need}"
    without = A.rough_tokens([{"content": template + idea + pack}]) + A.ANSWER_ROOM
    assert without < need, "промпт планировщика не учтён в оценке"


@test
def test_large_budget_splits_into_half_window_parts_and_each_gets_its_own_deadline(tmp: Path):
    """Части нарезки берутся вдвое мельче окна: одна часть на полокна отвечала дольше общего срока прогона, и вторая
    получала «дедлайн исчерпан» (живой прогон 01.11). Каждая часть получает свой срок, а не остаток общего."""
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R
    facts = "факт по делу, срок десять дней.\n" * 900      # ~30 тыс. знаков
    pack = "".join(f"## Карточка {i}\n\n{facts}" for i in range(4))   # ~120 тыс.
    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "30000"})
    budget = 60000
    seen = []

    def fake(cfg_, role, messages, **kw):
        seen.append(kw.get("deadline") or 0)
        return {"ok": True, "text": "конспект [[Карточка 1]]", "log": []}

    packed, n, why = R.compact_make_pack(cfg, pack, "идея", budget, fake, deadline=0)
    assert not why, why
    assert n == len(R.split_pack_for_budget(pack, budget // 2)), n
    assert n > len(R.split_pack_for_budget(pack, budget)), "части не стали мельче окна"
    assert len(packed) <= budget
    assert all(d > 0 for d in seen), seen


# ------------------------------------------------------------------ стенка проверок проекта (75043bf)

@test
def test_installer_lays_the_project_check_wall_and_update_delivers_it(tmp: Path):
    """Параллельный коммит 75043bf добавлял `templates/scripts/check.sh`, но установщик его не копировал и манифест
    не доставлял — проект оставался без стенки. Теперь `aurora.py install` кладёт `Scripts/check.sh` с битом
    исполнения, а `update` приносит его существующим проектам строкой манифеста `(launcher)`."""
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import install_aurora as IA
    tpl = (KIT / "templates/scripts/check.sh").read_text(encoding="utf-8")
    assert tpl.startswith("#!/usr/bin/env bash") and "check.sh: PASS" in tpl
    assert "out=$(eval" not in tpl and '"${cmd[@]}"' in tpl, "строка команды снова уходит в eval"
    inst = IA.Installer(target=tmp / "proj", name="Proj", slug=None, jira=None, confluence=None,
                        force=False, dry_run=False)
    (tmp / "proj").mkdir(parents=True, exist_ok=True)
    inst.install_check_script()
    path = tmp / "proj" / "Scripts" / "check.sh"
    assert path.is_file(), "Scripts/check.sh не скопирован"
    if os.name != "nt":                     # на Windows бита исполнения нет — chmod там только «только чтение»
        assert path.stat().st_mode & 0o111, "Scripts/check.sh без +x"
    assert "check.sh: PASS" in path.read_text(encoding="utf-8")
    manifest = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert "templates/scripts/check.sh => (launcher) Scripts/check.sh" in manifest, "update не доставит стенку"
    # повторный запуск не перезаписывает правку проекта
    path.write_text("#!/usr/bin/env bash\necho моё\n", encoding="utf-8")
    inst.install_check_script()
    assert "моё" in path.read_text(encoding="utf-8"), "установщик затёр стенку, доработанную проектом"
    # скрипт запускается и перечисляет проверки на проекте с деревом движка
    import shutil, subprocess
    proj = tmp / "p2"
    (proj / ".aurora").mkdir(parents=True)
    (proj / ".aurora/structure_dirs.txt").write_text("", encoding="utf-8")
    (proj / "Scripts").mkdir()
    script = proj / "Scripts/check.sh"
    shutil.copy2(KIT / "templates/scripts/check.sh", script)
    script.chmod(0o755)
    bash = shutil.which("bash")
    if bash:                                # на Windows без Git Bash проверять нечем — файл уже проверен
        for args, want in ((["--list"], "готовность проекта"), (["--help"], "Режимы")):
            # Windows-консоль декодирует вывод в cp1251: скрипт говорит по-русски в UTF-8 — задаём явно.
            cp = subprocess.run([bash, str(script), *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
            assert cp.returncode == 0 and want in cp.stdout, (args, cp.returncode, cp.stdout[-300:], cp.stderr[-300:])


@test
def test_a_failed_chunk_is_split_and_retried_instead_of_losing_the_whole_pack(tmp: Path):
    """Живой прогон 01.11: вторая часть конспекта упала на таймауте шлюза, и производство потеряло уже готовую первую.
    Теперь неудачный кусок режется надвое и пробуется мельче (до двух уровней); отказ остаётся отказом только когда
    не вышло и мельче."""
    import sys
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_runner as R
    facts = "факт по делу, срок десять дней.\n" * 400
    pack = "".join(f"## Карточка {i}\n\n{facts}" for i in range(8))
    calls = {"n": 0, "first": []}

    def fake(cfg, role, messages, **kw):
        calls["n"] += 1
        content = messages[0]["content"]
        if "ЧАСТЬ пака знаний" in content and len(content) > 12000:
            calls["first"].append(len(content))
            return {"ok": False, "text": "", "log": ["таймаут"]}
        return {"ok": True, "text": "конспект [[Карточка 1]]", "log": []}

    notes, parts, why = R.compact_make_pack({}, pack, "идея", 60000, fake)
    assert not why, why
    assert parts >= 2 and calls["first"], (parts, calls)
    assert notes.count("## Конспект части") == parts
    assert calls["n"] > len(calls["first"]), "неудачная часть не переспрошена мельче"
    # всё мельче тоже не вышло — отказ называет часть, а не молчит
    bad, _, why_bad = R.compact_make_pack({}, pack, "идея", 60000,
                                         lambda cfg, role, messages, **kw: {"ok": False, "text": "", "log": []})
    assert not bad and why_bad and "не получен" in why_bad, why_bad


@test
def test_compaction_time_does_not_eat_the_deadline_of_the_next_step(tmp: Path):
    """Живой прогон 01.11: конспект трёх частей занял двенадцать минут, и планировщик получил «дедлайн исчерпан» —
    общий срок шага истёк, пока шла нарезка. Время нарезки сдвигает срок, а не съедает его."""
    import sys, time as _t
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Критерии приёмки\n", encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "AC"\n    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n',
                        encoding="utf-8")
    d = R.make_session_dir(str(root), "ac")
    pack = "".join(f"## Заявка-{i}\n\n" + "Срок десять дней, проверка по отчёту. " * 900 for i in range(6))
    R.save_session(str(root), d.name, {"sid": d.name, "kind": "ac", "idea": "срок заявки",
                                      "spec": R.make_spec(str(root), "ac"), "pack": pack,
                                      "stages": {"enriched": "2026-11-01T08:00:00Z"}, "rounds": [], "plan": ""})
    plan_deadline = {"v": 0}

    def fake(cfg, role, messages, **kw):
        content = messages[0]["content"]
        if "ЧАСТЬ пака знаний" in content:
            _t.sleep(1.2)                       # нарезка небыстрая — срок шага тикает
            return {"ok": True, "text": "конспект [[Заявка-1]]", "log": []}
        if role == "planner":
            plan_deadline["v"] = kw.get("deadline") or 0
            return {"ok": True, "text": json.dumps({"questions": [], "assumptions": [], "plan": "1. Критерии"},
                                                   ensure_ascii=False), "log": []}
        if role == "critic":
            return {"ok": True, "text": json.dumps({"ok": True, "issues": [], "coverage": {}}, ensure_ascii=False),
                    "log": []}
        return {"ok": True, "text": "# AC\n\nСрок.", "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "15000", "AURORA_AGENT_REQUEST_TIMEOUT": "1"})
    r = R.run_make(cfg, str(root), "", "", d.name, "", False, call=fake, momus=False)
    assert r.get("ok"), f"производство упало: {r.get('why')}"
    assert plan_deadline["v"] > _t.time(), "срок планировщика истёк за время нарезки"


@test
def test_every_make_stage_has_its_own_deadline_not_the_remainder_of_the_run(tmp: Path):
    """Живой прогон 01.11: нарезка и план прошли, воркер писал документ минутами, и критик получил «дедлайн исчерпан:
    ни один бэкенд не ответил» — общий срок шага был уже израсходован. Теперь у планировщика, воркера и критика
    свой срок вызова роли, и долгий шаг не отменяет следующий."""
    import sys, time as _t
    sys.path.insert(0, str(KIT / "scripts"))
    import agent_core as A, agent_runner as R
    root = make_project(tmp)
    (root / "Templates").mkdir(exist_ok=True)
    (root / "Templates/AC.md").write_text("# AC\n\n## Критерии приёмки\n", encoding="utf-8")
    cfg_path = root / "aurora.config.yaml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8").rstrip()
                        + '\nartifacts:\n  ac:\n    title: "AC"\n    template: "Templates/AC.md"\n    out: "Artifacts/ac"\n',
                        encoding="utf-8")
    d = R.make_session_dir(str(root), "ac")
    pack = "".join(f"## Заявка-{i}\n\n" + "Срок десять дней, проверка по отчёту. " * 900 for i in range(6))
    R.save_session(str(root), d.name, {"sid": d.name, "kind": "ac", "idea": "срок заявки",
                                      "spec": R.make_spec(str(root), "ac"), "pack": pack,
                                      "stages": {"enriched": "2026-11-01T08:00:00Z"}, "rounds": [], "plan": ""})
    roles = []

    def fake(cfg, role, messages, **kw):
        roles.append(role)
        content = messages[0]["content"]
        _t.sleep(1.2)                            # каждый шаг дольше общего срока прогона (1 с)
        if "ЧАСТЬ пака знаний" in content:
            return {"ok": True, "text": "конспект [[Заявка-1]]", "log": []}
        if role == "planner":
            return {"ok": True, "text": json.dumps({"questions": [], "assumptions": [], "plan": "1. Критерии"},
                                                   ensure_ascii=False), "log": []}
        if role == "critic":
            return {"ok": True, "text": json.dumps({"ok": True, "issues": [], "coverage": {}}, ensure_ascii=False),
                    "log": []}
        return {"ok": True, "text": "# AC\n\nСрок.", "log": []}

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_BACKEND_1_CONTEXT": "15000", "AURORA_AGENT_REQUEST_TIMEOUT": "1"})
    orig = A.call_budget
    A.call_budget = lambda cfg_, role: 4.0       # свой срок шага есть у каждой роли
    try:
        r = R.run_make(cfg, str(root), "", "", d.name, "", False, call=fake, momus=False)
    finally:
        A.call_budget = orig
    assert r.get("ok"), f"производство упало на общем сроке: {r.get('why')}"
    assert "critic" in roles, roles
