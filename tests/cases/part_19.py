"""Проверки движка Aurora, часть 19: настройка моделей одна на кит (1.153.0).

Каркас и помощники — tests/harness.py. Провайдеры отдельно от возможностей (LLM, OCR,
эмбеддинги), у роли — упорядоченная цепочка бэкендов «провайдер + модель», которые можно
включать и выключать. Настройка живёт в `<кит>/local/models.json`, проект её не перекрывает.
"""
from __future__ import annotations

from pathlib import Path
import ast
import json
import subprocess
import sys

from harness import KIT, SCRIPTS, make_project, run, test  # noqa: F401


def _mods():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import importlib
    return importlib.import_module("model_config"), importlib.import_module("agent_core")


def _setup(**caps):
    return {"providers": [
        {"id": "work", "name": "Рабочий", "url": "http://work.example/v1", "key": "k1",
         "width": 4},
        {"id": "home", "name": "Дома", "url": "http://home.example/v1", "type": "llama.cpp",
         "width": 1, "parallel": False},
        {"id": "tei", "name": "Вектора", "url": "http://tei.example/v1", "type": "tei"}],
        "capabilities": caps}


@test
def test_providers_are_separate_from_roles_and_chains_are_ordered(_t):
    """Провайдер — подключение; роль — использование; бэкенд — «провайдер + модель» в роли.

    Прежде шлюз был номером, и на нём жили сразу адрес, ключ и модели всех ролей; порядок
    запасных был один на все роли. Теперь у каждой роли своя цепочка, и движок идёт по ней
    ровно в показанном порядке, пропуская выключенные.
    """
    MC, AG = _mods()
    data, notes = MC.normalize(_setup(llm={"roles": [
        {"id": "worker", "backends": [{"provider": "work", "model": "flash"},
                                      {"provider": "home", "model": "27b"}]},
        {"id": "critic", "thinking": True, "backends": [
            {"provider": "home", "model": "27b"},
            {"provider": "work", "model": "flash", "enabled": False},
            {"provider": "nope", "model": "x"}]},
        {"id": "summary", "name": "Суммаризация", "backends": [
            {"provider": "work", "model": "122b"}]}]}))
    assert any("nope" in n for n in notes), "бэкенд на неизвестного провайдера прошёл молча"
    roles = {r["id"]: r for r in data["capabilities"]["llm"]["roles"]}
    assert set(roles) >= {"worker", "planner", "critic", "qa", "summary"}, \
        "роли движка пропали или своя роль не сохранилась"
    assert roles["summary"]["builtin"] is False and roles["worker"]["builtin"] is True
    cfg = MC.to_config(data)
    assert [(b["n"], b["model"]) for b in AG.ring_order(cfg, 0, "worker")] == \
        [(1, "flash"), (2, "27b")]
    assert [(b["n"], b["model"]) for b in AG.ring_order(cfg, 0, "critic")] == [(2, "27b")], \
        "выключенный запасной пошёл в работу"
    assert [b["model"] for b in AG.ring_order(cfg, 0, "summary")] == ["122b"], \
        "своя роль не получила своей цепочки"
    assert [b["model"] for b in AG.ring_order(cfg, 0, "planner")] == ["flash", "27b"], \
        "пустая роль движка не идёт по цепочке разбора"
    # слот параллельного прогона: свой провайдер первым, остальные по порядку цепочки
    assert [b["n"] for b in AG.ring_order(cfg, 2, "worker")] == [2, 1]
    # пул: ширина у провайдера, медленный запасной без «в параллель» потока не получает
    cfg["parallel"] = AG.AUTO
    assert AG.pool(cfg) == [1, 1, 1, 1], AG.pool(cfg)


@test
def test_parallel_auto_and_gateway_width_follow_the_roles(_t):
    """«Одновременно = авто» — не один поток; шлюз только в цепочке qa держит свою ширину.

    Раннер сравнивал потолок с единицей, а AUTO это -1: настройка по умолчанию гнала
    разбор, связывание и тезисы по одному. Семафор шлюза считал слоты по цепочке worker,
    и провайдер, стоящий лишь в `qa`, получал «ноль слотов», то есть один запрос за раз."""
    MC, AG = _mods()
    import importlib
    R = importlib.import_module("agent_runner")
    data, _ = MC.normalize({"providers": [
        {"id": "work", "name": "Рабочий", "url": "http://work.example/v1", "key": "k1",
         "width": 2},
        {"id": "strong", "name": "Сильный", "url": "http://strong.example/v1", "key": "k2",
         "width": 8}],
        "capabilities": {"llm": {"roles": [
            {"id": "worker", "backends": [{"provider": "work", "model": "flash"}]},
            {"id": "qa", "backends": [{"provider": "strong", "model": "big"}]}]}}})
    cfg = MC.to_config(data)
    work = next(b["n"] for b in AG.role_chain(cfg, "worker"))
    strong = next(b["n"] for b in AG.role_chain(cfg, "qa"))
    assert work != strong
    # авто: ширина берётся из объявленных ширин цепочки роли, а не схлопывается в 1
    cfg["parallel"] = AG.AUTO
    assert AG.parallel_on(cfg) and AG.effective_width(cfg) == 2
    assert AG.effective_width(cfg, "qa") == 8
    slots, width = R.parallel_width(cfg, 5)
    assert width == 2 and slots == [work, work], (slots, width)
    assert R.parallel_width(cfg, 1)[1] == 1, "работы на одного — слот один"
    assert "потоков: 2" in R.threads_line(cfg, width)
    # перепроверка «без опоры» ходит в qa — потоков по ширине шлюза qa, а не worker
    qslots, qwidth = R.parallel_width(cfg, 20, "qa")
    assert qwidth == 8 and qslots == [strong] * 8, (qslots, qwidth)
    assert R.parallel_width(cfg, 20)[1] == 2
    # оборот и дистилляция берут ширину пула, когда она больше max_steps: ширина 20 при
    # max_steps 15 — равенства, а не «не меньше», иначе откат к `parallel > 1` незаметен
    wide = {**data, "providers": [{**data["providers"][0], "width": 20}, data["providers"][1]]}
    cw = MC.to_config(wide)
    cw["parallel"] = AG.AUTO
    assert cw["max_steps"] == 15
    assert R.lap_steps(cw) == 20 and R.distill_every(cw) == 80, \
        (R.lap_steps(cw), R.distill_every(cw))
    cw["parallel"] = 1
    assert R.lap_steps(cw) == cw["max_steps"] and R.distill_every(cw) == cw["max_steps"]
    # число обрезает; 1 и пустое — последовательно
    cfg["parallel"] = 1
    assert not AG.parallel_on(cfg) and AG.effective_width(cfg) == 1
    assert R.parallel_width(cfg, 5) == ([], 1)
    assert "= 1 в настройке" in R.threads_line(cfg, 1)
    cfg["parallel"] = 3
    assert AG.parallel_on(cfg) and AG.effective_width(cfg) == 2
    cfg["parallel"] = 1
    # семафор: ширина шлюза не зависит от роли, по которой смотрим
    assert AG.pool(cfg, "qa").count(strong) == 1, "потолок 1 режет и ширину шлюза qa"
    cfg["parallel"] = AG.AUTO
    assert AG.pool(cfg).count(strong) == 0, "в пуле worker шлюза qa нет — отсюда был «один»"
    assert AG.gateway_width(cfg, strong) == 8, AG.gateway_width(cfg, strong)
    assert AG.gateway_width(cfg, work) == 2
    cfg["parallel"] = 4
    assert AG.gateway_width(cfg, strong) == 4, "потолок прогона шлюз не поднимает, но режет"
    # авто без объявленных ширин: по слоту на шлюз — безопасное умолчание
    for p in data["providers"]:
        p["width"] = 0
    cfg2 = MC.to_config(data)
    cfg2["parallel"] = AG.AUTO
    assert AG.effective_width(cfg2) == 1 and "авто" in R.threads_line(cfg2, 1)
    # ширину объявил один шлюз, а другой нет: необъявленному один слот в любой роли, даже
    # если в своей цепочке он единственный и потолок 99 отдал бы ему всё (llama.cpp, 22.09)
    mixed, _ = MC.normalize({"providers": [
        {"id": "big", "name": "Большой", "url": "http://big.example/v1", "key": "k1",
         "width": 16},
        {"id": "home", "name": "Домашний", "url": "http://home.example/v1", "key": "k2"}],
        "capabilities": {"llm": {"roles": [
            {"id": "worker", "backends": [{"provider": "big", "model": "a"},
                                          {"provider": "home", "model": "b"}]},
            {"id": "qa", "backends": [{"provider": "home", "model": "b"}]}]}}})
    cm = MC.to_config(mixed)
    cm["parallel"] = 99
    big = next(b["n"] for b in AG.role_chain(cm, "worker") if b.get("width"))
    home = next(b["n"] for b in AG.role_chain(cm, "qa"))
    assert AG.pool(cm, "qa").count(home) == 1, "необъявленному один слот и в пуле qa"
    assert AG.gateway_width(cm, home) == 1, AG.gateway_width(cm, home)
    assert AG.gateway_width(cm, big) == 16
    assert R.parallel_width(cm, 50, "qa")[1] == 1, "потоков qa столько же, сколько слотов"
    # правило одно: слоты пула и семафор шлюза совпадают в каждой роли и для каждого шлюза
    for role in ("worker", "qa"):
        for n in set(AG.pool(cm, role)):
            assert AG.pool(cm, role).count(n) <= AG.gateway_width(cm, n), (role, n)
    # необъявленный в worker, ширину объявил шлюз только в qa; число потоков worker не
    # обгоняет семафор (иначе лишние потоки ждут слот до срока и карточки падают)
    split, _ = MC.normalize({"providers": [
        {"id": "a", "name": "А", "url": "http://a.example/v1", "key": "k1"},
        {"id": "b", "name": "Б", "url": "http://b.example/v1", "key": "k2", "width": 8}],
        "capabilities": {"llm": {"roles": [
            {"id": "worker", "backends": [{"provider": "a", "model": "m"}]},
            {"id": "qa", "backends": [{"provider": "b", "model": "m"}]}]}}})
    cs = MC.to_config(split)
    cs["parallel"] = 6
    a_n = next(b["n"] for b in AG.role_chain(cs, "worker"))
    assert AG.pool(cs).count(a_n) == AG.gateway_width(cs, a_n) == 1, \
        (AG.pool(cs), AG.gateway_width(cs, a_n))
    # старая конфигурация без цепочек: ширину не объявил никто — потолок делится как прежде
    old = AG.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u", "AURORA_AGENT_BACKEND_1_MODEL": "m",
                           "AURORA_AGENT_PARALLEL": "4"})
    assert old.get("chains") is None and AG.pool(old) == [1] * 4 and AG.gateway_width(old, 1) == 4


@test
def test_a_role_answers_through_its_own_fallback_chain(_t):
    """Не ответил основной — запрос уходит следующему бэкенду роли, со своей моделью."""
    MC, AG = _mods()
    data, _ = MC.normalize(_setup(llm={"roles": [{"id": "worker", "backends": [
        {"provider": "work", "model": "flash"}, {"provider": "home", "model": "27b"}]}]}))
    cfg = MC.to_config(data)
    sent = []

    def transport(kind, b, payload, timeout):
        if kind != "chat":
            return 404, None, "", 0.0
        sent.append((b["url"], payload["model"]))
        if b["url"].startswith("http://work"):
            return 404, {"error": {"message": "no model"}}, "HTTP 404", 0.1
        return 200, {"choices": [{"message": {"content": "готов"}}]}, "", 0.1

    AG.DOWN.clear()
    r = AG.call_role(cfg, "worker", [{"role": "user", "content": "привет"}],
                     transport=transport, sleep=lambda s: None)
    assert r["ok"] and r["model"] == "27b", r
    assert sent == [("http://work.example/v1", "flash"), ("http://home.example/v1", "27b")], sent


@test
def test_embeddings_and_ocr_follow_the_same_pattern(_t):
    """OCR и эмбеддинги — те же роли и цепочки. Запасной векторов — только той же модели,
    запасной распознавания — со своей моделью."""
    MC, AG = _mods()
    data, _ = MC.normalize(_setup(
        embeddings={"roles": [{"id": "index", "backends": [
            {"provider": "tei", "model": "bge-m3"},
            {"provider": "work", "model": "e5-large"},
            {"provider": "home", "model": "bge-m3", "url": "http://home.example:8082/v1"}]}]},
        ocr={"dpi": 150, "max_pages": 10, "roles": [{"id": "document", "backends": [
            {"provider": "home", "model": "glm-ocr"}, {"provider": "work", "model": "qwen-vl"}]}]}))
    cfg = MC.to_config(data)
    assert [(r["url"], r["model"]) for r in AG.embed_ring(cfg)] == \
        [("http://tei.example/v1", "bge-m3"), ("http://home.example:8082/v1", "bge-m3")], \
        "в кольцо векторов вошла другая модель или потерян свой адрес сервиса"
    assert cfg["embed"]["model"] == "bge-m3"
    assert [(r["url"], r["model"]) for r in AG.ocr_ring(cfg)] == \
        [("http://home.example/v1", "glm-ocr"), ("http://work.example/v1", "qwen-vl")]
    assert cfg["ocr"]["dpi"] == 150 and cfg["ocr"]["max_pages"] == 10
    src = (SCRIPTS / "office_ingest.py").read_text(encoding="utf-8")
    assert 'backend.get("model") or model' in src, "запасной распознавания шлёт чужую модель"


@test
def test_the_old_env_setup_moves_into_the_kit_once(tmp: Path):
    """Прежняя настройка `.env` кита переносится в `local/models.json` один раз и без потерь;
    сам `.env` не трогается, битый `models.json` не перезаписывается."""
    MC, AG = _mods()
    env = {"AURORA_AGENT_BACKEND_1_URL": "http://gw.example/v1",
           "AURORA_AGENT_BACKEND_1_KEY": "секрет",
           "AURORA_AGENT_BACKEND_1_MODEL": "flash",
           "AURORA_AGENT_BACKEND_1_MODEL_QA": "122b",
           "AURORA_AGENT_BACKEND_1_WIDTH": "8",
           "AURORA_AGENT_BACKEND_1_EMBED_MODEL": "bge-m3",
           "AURORA_AGENT_BACKEND_1_OCR_MODEL": "glm-ocr",
           "AURORA_OCR_MODEL": "glm-ocr",
           "AURORA_AGENT_BACKEND_3_URL": "http://local.example/v1",
           "AURORA_AGENT_BACKEND_3_MODEL": "27b",
           "AURORA_AGENT_BACKEND_3_FALLBACK": "0",
           "AURORA_AGENT_BACKEND_3_PARALLEL": "0",
           "AURORA_AGENT_THINKING_WORKER": "0",
           "AURORA_AGENT_PARALLEL": "auto"}
    kit = tmp / "kit"
    data = MC.load(kit, env)
    assert (kit / "local" / "models.json").is_file(), "перенос не записал настройку кита"
    provs = {p["id"]: p for p in data["providers"]}
    assert provs["gw1"]["key"] == "секрет" and provs["gw1"]["width"] == 8
    assert provs["gw3"]["parallel"] is False
    roles = {r["id"]: r for r in data["capabilities"]["llm"]["roles"]}
    assert [(b["provider"], b["model"], b["enabled"]) for b in roles["qa"]["backends"]] == \
        [("gw1", "122b", True), ("gw3", "27b", False)], roles["qa"]["backends"]
    assert roles["worker"]["thinking"] is False and roles["critic"]["thinking"] is True
    ocr = data["capabilities"]["ocr"]["roles"][0]["backends"]
    emb = data["capabilities"]["embeddings"]["roles"][0]["backends"]
    assert ocr[0]["model"] == "glm-ocr" and emb[0]["model"] == "bge-m3"
    assert data["settings"]["parallel"] == "auto"
    # второй запуск читает файл, а не .env: правка человека не затирается переносом
    (kit / "local" / "models.json").write_text(json.dumps({"providers": []}), encoding="utf-8")
    assert MC.load(kit, env)["providers"] == [], "перенос повторился поверх настройки"
    (kit / "local" / "models.json").write_text("{битый", encoding="utf-8")
    broken = MC.load(kit, env)
    assert broken.get("error") and (kit / "local" / "models.json").read_text(
        encoding="utf-8") == "{битый", "битый файл перезаписан"
    # личная настройка настоящего кита в прогоне тестов не читается и не пишется
    assert MC.load(KIT, env)["providers"] == [] and not MC.save(KIT, data)["ok"]
    # копия движка без указателя на кит считает китом проект: ключи в его git не пишутся,
    # настройка собирается в памяти
    (tmp / "p").mkdir()
    proj = make_project(tmp / "p")
    got = MC.load(proj, env)
    assert got["providers"] and not (proj / "local" / "models.json").exists(), \
        "перенос записал ключи провайдеров в папку проекта — под git"


@test
def test_no_project_has_its_own_model_setup(tmp: Path):
    """Ни слоя проекта в движке, ни карточки проекта в панели; doctor называет остатки."""
    MC, AG = _mods()
    src = (SCRIPTS / "agent_core.py").read_text(encoding="utf-8")
    raw = src[src.index("def raw_config"):src.index("def template_kwargs_of")]
    assert "project /" not in raw, "движок снова наслаивает .env проекта на модели"
    cfg_fn = src[src.index("def config()"):src.index("def raw_config")]
    assert "MC.load(kit" in cfg_fn, "движок читает модели не из настройки кита"
    for rel in ("agent_runner.py", "kb_embed.py", "ctx_pack.py", "office_ingest.py",
                "review_run.py", "agent_probe.py", "kb_search_quality.py", "aurora_doctor.py"):
        text = (SCRIPTS / rel).read_text(encoding="utf-8")
        assert "AG.parse_config(AG.raw_config())" not in text and \
            "_ag.parse_config(_ag.raw_config())" not in text, f"{rel} читает модели мимо кита"
    ck = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    assert "def agent_write_env(" not in ck, "панель снова пишет модели в .env"
    root = make_project(tmp)
    from aurora_common import ENV_FILE
    (root / ENV_FILE).write_text("AURORA_AGENT_BACKEND_1_MODEL=тяжёлая\n", encoding="utf-8")
    out = run("aurora_doctor.py", cwd=root, expect_rc=None).stdout
    assert "переменные моделей больше не действуют" in out and "тяжёлая" not in out, out[-600:]


@test
def test_the_models_section_is_additive_and_native(_t):
    """Раздел «Модели»: вкладки, «+» для запасного, перетаскивание, провайдер прямо из роли."""
    mod = KIT / "cockpit/modules/models"
    meta = json.loads((mod / "module.json").read_text(encoding="utf-8"))
    assert meta["group"] == "machine" and "needs" in meta and not meta["needs"], \
        "раздел моделей привязан к проекту — а настройка одна на кит"
    view = (mod / "view.js").read_text(encoding="utf-8")
    for need, why in (('["providers", ...CAPS, "calls"]',
                       "нет вкладок «Провайдеры / LLM / OCR / Эмбеддинги / Где работают»"),
                      ('t("models.add_fallback")', "нет «+» для запасного"),
                      ('draggable: "true"', "цепочку нельзя перетащить"),
                      ('"__new__"', "провайдера нельзя завести прямо из роли"),
                      ('"/api/models/list"', "модели провайдера нельзя спросить у него"),
                      ("b.enabled = e.target.checked", "запасной нельзя выключить, не удаляя"),
                      ('t("models.add_role")', "свою роль добавить нельзя")):
        assert need in view, why
    assert '"embeddings"' in view and '"ocr"' in view, "OCR и эмбеддинги устроены иначе, чем LLM"


@test
def test_every_module_a_shipped_script_imports_is_shipped(_t):
    """Скрипт, уезжающий в проект, не импортирует модуль кита, который туда не едет.

    1.153.0 вышел с `agent_core`, импортирующим новый `model_config`, — а в манифест движка
    модуль не попал. В ките всё работало (тесты идут из `scripts/`), в каждом проекте
    любая команда агента падала на `ModuleNotFoundError`. Проверка — по разбору импортов,
    в том числе ленивых внутри функций и через `importlib.import_module`.
    """
    shipped = set()
    for line in (KIT / "engine_manifest.txt").read_text(encoding="utf-8").splitlines():
        if "=>" in line and not line.lstrip().startswith("#"):
            src = line.split("=>", 1)[0].strip()
            if src.startswith("scripts/") and src.endswith(".py"):
                shipped.add(Path(src).stem)
    # модули источников едут правилом (connectors): скрипт каждого — в .aurora/scripts/
    for man in (KIT / "connectors").glob("*/connector.json"):
        meta = json.loads(man.read_text(encoding="utf-8"))
        script = (meta.get("run") or {}).get("script") or meta.get("script") or ""
        if script.endswith(".py"):
            shipped.add(Path(script).stem)
    # берётся из самого кита: aurora_update кладёт KIT/scripts в sys.path перед импортом
    from_kit = {"install_aurora"}
    kit_mods = {p.stem for p in (KIT / "scripts").glob("*.py")}
    missing = {}
    for stem in sorted(shipped):
        src = KIT / "scripts" / f"{stem}.py"
        if not src.is_file():
            continue
        for n in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
            names = []
            if isinstance(n, ast.Import):
                names = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                names = [n.module.split(".")[0]]
            elif isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "import_module" \
                    and n.args and isinstance(n.args[0], ast.Constant) \
                    and isinstance(n.args[0].value, str):
                names = [n.args[0].value.split(".")[0]]
            for m in names:
                if m in kit_mods and m not in shipped and m not in from_kit:
                    missing.setdefault(m, set()).add(stem)
    assert len(shipped) > 40, "манифест движка не прочитан"
    assert not missing, ("модули кита не едут в проект, а их импортируют уезжающие скрипты — "
                         f"впишите в engine_manifest.txt: {dict((k, sorted(v)) for k, v in missing.items())}")
