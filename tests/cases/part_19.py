"""Проверки движка Aurora, часть 19: настройка моделей одна на кит (1.153.0).

Каркас и помощники — tests/harness.py. Провайдеры отдельно от возможностей (LLM, OCR,
эмбеддинги), у роли — упорядоченная цепочка бэкендов «провайдер + модель», которые можно
включать и выключать. Настройка живёт в `<кит>/local/models.json`, проект её не перекрывает.
"""
from __future__ import annotations

from pathlib import Path
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
    for need, why in (('["providers", ...CAPS]', "нет вкладок «Провайдеры / LLM / OCR / Эмбеддинги»"),
                      ('t("models.add_fallback")', "нет «+» для запасного"),
                      ('draggable: "true"', "цепочку нельзя перетащить"),
                      ('"__new__"', "провайдера нельзя завести прямо из роли"),
                      ('"/api/models/list"', "модели провайдера нельзя спросить у него"),
                      ("b.enabled = e.target.checked", "запасной нельзя выключить, не удаляя"),
                      ('t("models.add_role")', "свою роль добавить нельзя")):
        assert need in view, why
    assert '"embeddings"' in view and '"ocr"' in view, "OCR и эмбеддинги устроены иначе, чем LLM"
