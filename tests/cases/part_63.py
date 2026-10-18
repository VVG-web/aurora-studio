"""Проверки движка Aurora, часть 63. Каркас и помощники — tests/harness.py.

Доработки по просмотру фазы B проверки доступности: блок `source_access:` не теряет ручную правку молча (комментарии, лишние
поля, запись в одну строку, повторный блок — отказ `lossy` до согласия), окончания строк и конец файла без перевода строки,
слепок блока пуст при пустом блоке; `models.json` — вложенные неизвестные поля переживают запись, непарсящийся файл не
перезаписывается без согласия, слепок с солью машины, не строка `expect` равен пустому слепку, записи не мешают друг другу;
имена профилей — метки, а не адреса; метаданные команд устойчивы к кривому файлу, а диагностика не обещает работать без связи.
Сети нет; кит, проект и домашняя папка — временные.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
import threading
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, set_home, test  # noqa: F401

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(KIT / "cockpit"))


def _g():
    return importlib.import_module("conn_gate")


def _project(tmp: Path) -> Path:
    proj = make_project(tmp)
    cfg = proj / "aurora.config.yaml"
    cfg.write_text(cfg.read_text(encoding="utf-8")
                   + "\nsources:\n  - id: Web\n    module: web\n    path: Sources/Web\n"
                     "  - id: Confluence\n    module: confluence-dc\n    path: Sources/Confluence\n", encoding="utf-8")
    return proj


# ------------------------------------------------------------------ блок source_access: не теряет ручную правку молча

@test
def test_source_access_save_refuses_to_drop_hand_edited_content_until_confirmed(tmp: Path):
    """Комментарий, лишнее поле и запись в одну строку внутри блока: сохранение отказывает кодом `lossy` и ничего не пишет."""
    G = _g()
    proj = _project(tmp)
    cfg = proj / "aurora.config.yaml"
    base = cfg.read_text(encoding="utf-8")
    cfg.write_text(base + 'source_access:\n  # keep me\n  Web:\n    mode: "internet"\n    note: "hand"\n', encoding="utf-8")
    before = cfg.read_bytes()
    state = G.access_state(str(proj))
    assert set(state["lossy"]) == {"comments", "skipped"}, f"что потеряется, названо кодами: {state['lossy']}"
    assert state["explicit"] == {"Web": {"mode": "internet"}}, state["explicit"]
    res = G.save_access(str(proj), {"Web": {"mode": "local"}}, state["digest"])
    assert not res["ok"] and res["code"] == "lossy" and set(res["lossy"]) == {"comments", "skipped"}, f"потеря принята молча: {res}"
    assert cfg.read_bytes() == before, "отказ изменил конфиг"
    res = G.save_access(str(proj), {"Web": {"mode": "local"}}, state["digest"], replace_lossy=True)
    assert res["ok"], res
    text = cfg.read_text(encoding="utf-8")
    assert "keep me" not in text and 'note: "hand"' not in text and G.parse_access(text)[0] == {"Web": {"mode": "local"}}, text
    assert G.access_state(str(proj))["lossy"] == [], "после замены блок снова чистый"
    # страница, которая ничего не меняла, не пишет: записи нет — и потери нет
    keep = cfg.read_bytes()
    assert G.save_access(str(proj), {"Web": {"mode": "local"}}, G.access_state(str(proj))["digest"])["ok"] and cfg.read_bytes() == keep
    # запись в одну строку и повторный блок — тоже потери, названные кодами
    cfg.write_text(base + 'source_access: {Web: {mode: vpn}}\n', encoding="utf-8")
    assert G.lossy_kinds(cfg.read_text(encoding="utf-8")) == ["inline"], G.lossy_kinds(cfg.read_text(encoding="utf-8"))
    cfg.write_text(base + 'source_access:\n  Web:\n    mode: "local"\nother:\n  x: 1\nsource_access:\n  Confluence:\n    mode: "local"\n', encoding="utf-8")
    assert G.lossy_kinds(cfg.read_text(encoding="utf-8")) == ["duplicate"]
    assert G.save_access(str(proj), {"Web": {"mode": "internet"}}, G.access_state(str(proj))["digest"], replace_lossy=True)["ok"]
    out = cfg.read_text(encoding="utf-8")
    assert out.count("source_access:") == 1 and "other:\n  x: 1" in out, f"лишний блок не убран или соседний раздел задет: {out}"


@test
def test_source_access_writer_handles_line_endings_quotes_and_odd_shapes(tmp: Path):
    """CRLF без перевода строки в конце, ключ блока в кавычках, имена с обоими видами кавычек, `|+` перед блоком."""
    G = _g()
    access = {"Web": {"mode": "internet"}}
    crlf = "a: 1\r\nb: 2"                                   # CRLF, последняя строка без окончания
    out = G.write_access(crlf, access)
    assert out.startswith("a: 1\r\nb: 2\r\n") and "\n" not in out.replace("\r\n", ""), f"смесь окончаний: {out!r}"
    assert G.write_access(out, {}) == "a: 1\r\nb: 2\r\n", "удаление блока оставило одинокий CR или лишнюю строку"
    tail = "a: 1\r\nsource_access:\r\n  Web:\r\n    mode: \"local\""      # блок — последняя строка, без окончания
    swapped = G.write_access(tail, access)
    assert swapped.endswith('mode: "internet"') and "\n" not in swapped.replace("\r\n", "") and "\r" not in swapped.replace("\r\n", ""), \
        f"конец без перевода строки или окончания испорчены: {swapped!r}"
    assert G.write_access(tail, {}) == "a: 1\r\n", "удаление блока в конце без перевода строки"
    # ключ блока в кавычках — тот же блок: второго не появляется
    quoted = 'x: 1\n"source_access":\n  Web:\n    mode: "local"\n'
    assert G.parse_access(quoted)[0] == {"Web": {"mode": "local"}}, "ключ блока в кавычках не прочитан"
    assert G.write_access(quoted, access).count("source_access") == 1, "блок с ключом в кавычках продублирован"
    # имена источников с кавычками и обратной чертой доходят обратно как есть
    names = {"it's \"odd\"": {"mode": "local"}, "back\\slash": {"mode": "internet"}, "yes": {"mode": "local"}, "Old Wiki (2)": {"mode": "local"}}
    written = G.write_access("p: 1\n", names)
    found, problems = G.parse_access(written)
    assert found == names and not problems, f"имена не прошли туда и обратно: {found} {problems}"
    assert '"yes":' in written, "имя, которое YAML читает как логическое, записано без кавычек"
    # `|+` перед блоком: пустые строки в конце скаляра остаются
    keep = "note: |+\n  text\n\n\n"
    assert G.write_access(keep, access).startswith(keep), "хвост блочного скаляра `|+` съеден"
    # блок пуст — слепок пуст: страница, открытая без блока, и запрос без слепка сходятся
    assert G.block_digest({}) == "" and G.block_digest({"Web": {"mode": "local"}}) != ""
    proj = _project(tmp)
    assert G.access_state(str(proj))["digest"] == "", "слепок пустого блока не пуст"
    assert G.save_access(str(proj), {"Web": {"mode": "local"}}, "")["ok"], "запись при пустом слепке и пустом блоке отклонена"


@test
def test_source_access_save_reports_unreadable_config_and_serialises_concurrent_saves(tmp: Path):
    """Нечитаемый конфиг — отказ с кодом `write`, а не исключение; две записи разом не дают ложной ошибки записи."""
    G = _g()
    broken = tmp / "bad"
    broken.mkdir()
    (broken / "aurora.config.yaml").mkdir()              # вместо файла — папка: чтение падает OSError
    res = G.save_access(str(broken), {"Web": {"mode": "local"}})
    assert not res["ok"] and res["code"] == "write", f"нечитаемый конфиг: {res}"
    proj = _project(tmp)
    digest = G.access_state(str(proj))["digest"]
    results: list = []

    def go(mode):
        results.append(G.save_access(str(proj), {"Web": {"mode": mode}}, digest))
    threads = [threading.Thread(target=go, args=(m,)) for m in ("local", "internet", "local", "internet", "local")]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sum(1 for r in results if r["ok"]) == 1, f"слепок один — записать вправе одна: {results}"
    assert all(r["ok"] or r.get("conflict") for r in results), f"запись разом дала ложную ошибку: {results}"
    assert not list(proj.glob("*.aurora-tmp")), "остался временный файл"
    assert G.parse_access((proj / "aurora.config.yaml").read_text(encoding="utf-8"))[0].get("Web", {}).get("mode") in ("local", "internet")


@test
def test_profile_names_are_labels_not_addresses_and_stale_vpn_is_dropped(tmp: Path):
    """Имя профиля — метка: IP и составное имя хоста не принимаются нигде; `vpn` не остаётся при режиме без VPN."""
    G = _g()
    CN = importlib.import_module("connectivity")
    MC = importlib.import_module("model_config")
    for name in ("VPN-1", "Office", "corp.vpn", "v2"):
        assert CN.valid_name(name), f"годное имя отвергнуто: {name}"
    for name in ("10.1.2.3", "gw.corp.example.com", "a.b.c", "", "-x", "a b", "x" * 41, None):
        assert not CN.valid_name(name), f"адрес принят за имя профиля: {name!r}"
    out, errors = G.clean_access({"Web": {"mode": "vpn", "vpn": "10.1.2.3"}})
    assert errors and errors[0]["code"] == "bad_name", f"IP принят как профиль режима доступа: {out} {errors}"
    clean, notes = MC.normalize({"providers": [{"id": "a", "url": "https://llm.example.com/v1", "vpn": "gw.corp.example.com"}]})
    assert clean["providers"][0]["vpn"] == "" and notes, f"составное имя принято профилем провайдера: {notes}"
    cfg, problems = CN.clean({"enabled": True, "profiles": [{"name": "10.0.0.1"}, {"name": "VPN-1"}], "probes": []})
    assert [p["name"] for p in cfg["profiles"]] == ["VPN-1"] and problems and problems[0]["code"] == "bad_name", problems
    # профиль, оставшийся от прежнего выбора, не пишется при режиме без VPN; без режима (умолчание модуля) — остаётся
    out, errors = G.clean_access({"Web": {"mode": "internet", "vpn": "VPN-1"}, "Wiki": {"vpn": "VPN-2"}})
    assert not errors and out["Web"] == {"mode": "internet"} and out["Wiki"] == {"vpn": "VPN-2"}, out
    panel = (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    assert 'v !== "vpn" && v !== "mixed") delete r.vpn' in panel, "страница оставляет vpn при смене режима"


# ------------------------------------------------------------------ models.json

@test
def test_models_save_carries_nested_unknown_fields_and_not_onto_a_moved_provider(tmp: Path):
    """Лишние поля роли, бэкенда, возможности, настроек и неизвестная возможность переживают запись формы; переезд провайдера их не тянет."""
    MC = importlib.import_module("model_config")
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    path = MC.models_path(kit)
    path.write_text(json.dumps({
        "version": 1,
        "providers": [{"id": "gw", "name": "GW", "type": "openai", "url": "https://llm.example.com/v1", "key": "k", "extra_headers": {"X": "1"}}],
        "capabilities": {"llm": {"x_cap": 3, "roles": [{"id": "worker", "x_role": 5, "backends": [
            {"provider": "gw", "model": "m", "x_backend": 7}]}]}, "reranker": {"roles": [{"id": "r"}]}},
        "settings": {"x_setting": 1}}), encoding="utf-8")
    page = {"providers": [{"id": "gw", "name": "GW", "type": "openai", "url": "https://llm.example.com/v1", "key": "k"}],
            "capabilities": {"llm": {"roles": [{"id": "worker", "backends": [{"provider": "gw", "model": "m"}]}]}}}
    assert MC.save(kit, page, MC.file_stamp(kit))["ok"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    llm = saved["capabilities"]["llm"]
    assert llm["x_cap"] == 3 and llm["roles"][0]["x_role"] == 5 and llm["roles"][0]["backends"][0]["x_backend"] == 7, \
        f"вложенные неизвестные поля стёрты: {llm}"
    assert saved["settings"]["x_setting"] == 1 and "reranker" in saved["capabilities"], "настройки или чужая возможность стёрты"
    assert saved["providers"][0]["extra_headers"] == {"X": "1"}, "поле провайдера стёрто"
    moved = json.loads(json.dumps(page))
    moved["providers"][0]["url"] = "https://other.example.org/v1"
    assert MC.save(kit, moved)["ok"]
    assert "extra_headers" not in json.loads(path.read_text(encoding="utf-8"))["providers"][0], \
        "поля переехавшего провайдера (заголовки, токены) ушли на новый адрес"


@test
def test_models_save_does_not_replace_an_unparseable_file_without_consent(tmp: Path):
    """Файл с опечаткой: запись отказывает кодом `broken`; по согласию заменяет и оставляет копию рядом, права 600."""
    MC = importlib.import_module("model_config")
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    path = MC.models_path(kit)
    path.write_text('{"providers": [ {"id": "gw", "key": "SECRET-1"} ', encoding="utf-8")          # не закрыта скобка
    before = path.read_bytes()
    res = MC.save(kit, {"providers": []})
    assert not res["ok"] and res["code"] == "broken" and path.read_bytes() == before, f"битый файл перезаписан молча: {res}"
    res = MC.save(kit, {"providers": []}, None, True)
    assert res["ok"], res
    copies = list(path.parent.glob("models.json.broken-*"))
    assert len(copies) == 1 and copies[0].read_bytes() == before, f"копия прежнего файла не оставлена: {copies}"
    if sys.platform != "win32":
        assert oct(copies[0].stat().st_mode & 0o777) == "0o600", "копия файла с ключами открыта всем"
    assert json.loads(path.read_text(encoding="utf-8"))["providers"] == []


@test
def test_models_stamp_is_salted_so_it_cannot_be_used_to_guess_keys(tmp: Path):
    """Слепок `models.json` — не хэш содержимого: с солью машины; в разных китах разный; совпадает, пока файл тот же."""
    MC = importlib.import_module("model_config")
    stamps = []
    for name in ("k1", "k2"):
        kit = tmp / name
        (kit / "local").mkdir(parents=True)
        MC.models_path(kit).write_text('{"providers": []}\n', encoding="utf-8")
        s1, s2 = MC.file_stamp(kit), MC.file_stamp(kit)
        assert s1 == s2 and s1, "слепок неустойчив"
        assert s1 != hashlib.sha256(MC.models_path(kit).read_bytes()).hexdigest()[:32], "слепок — голый SHA-256 содержимого"
        salt = kit / "local" / ".stamp-salt"
        assert salt.is_file() and len(salt.read_bytes()) >= 16, "соль слепка не сохранена рядом с файлом"
        if sys.platform != "win32":
            assert oct(salt.stat().st_mode & 0o777) == "0o600", "соль открыта всем"
        stamps.append(s1)
    assert stamps[0] != stamps[1], "одно содержимое в разных китах дало один слепок: соль не работает"
    MC.models_path(tmp / "k1").write_text('{"providers": [{"id": "a"}]}\n', encoding="utf-8")
    assert MC.file_stamp(tmp / "k1") != stamps[0], "слепок не заметил правки файла"
    assert MC.file_stamp(tmp / "nope") == "", "нет файла — слепок пуст"


@test
def test_models_route_treats_a_non_string_expect_as_the_empty_stamp(tmp: Path):
    """`expect: null` или число не обходят защиту от потерянного обновления; ответ 409, файл цел."""
    import http.client
    from http.server import ThreadingHTTPServer
    restore = set_home(tmp / "home")
    ck = importlib.import_module("aurora_cockpit")
    CN = importlib.import_module("connectivity")
    MC = importlib.import_module("model_config")
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    path = kit / "local" / "models.json"
    path.write_text(json.dumps({"version": 1, "providers": [], "capabilities": {}}), encoding="utf-8")
    old_kit = ck.KIT
    ck.KIT = str(kit)
    CN.reset_services()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ck.Handler)
    srv.roots = [str(tmp / "roots")]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(body):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=60)
        c.request("POST", "/api/models/save", body=json.dumps(body).encode("utf-8"),
                  headers={"X-Aurora-Token": ck.TOKEN, "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read().decode("utf-8") or "{}")
    try:
        keep = path.read_bytes()
        models = {"providers": [{"id": "gw", "name": "GW", "type": "openai", "url": "https://llm.example.com/v1", "key": "k"}], "capabilities": {}}
        for bad in (None, 5, [], {}):
            st, r = call({"models": models, "expect": bad})
            assert st == 409 and r.get("conflict") is True and path.read_bytes() == keep, f"expect={bad!r}: {st} {r}"
        path.write_text('{"providers": [ ', encoding="utf-8")
        st, r = call({"models": models, "expect": MC.file_stamp(kit)})
        assert st == 200 and r.get("code") == "broken" and not r.get("ok"), f"битый файл: {st} {r}"
        st, r = call({"models": models, "expect": MC.file_stamp(kit), "replace_broken": True})
        assert st == 200 and r["ok"], f"замена по согласию: {st} {r}"
    finally:
        srv.shutdown()
        srv.server_close()
        CN.reset_services()
        ck.KIT = old_kit
        restore()


@test
def test_models_saves_at_once_do_not_clash(tmp: Path):
    """Пять записей разом с одним слепком: ровно одна проходит, остальные — конфликт, а не ошибка записи; временных файлов нет."""
    MC = importlib.import_module("model_config")
    kit = tmp / "kit"
    (kit / "local").mkdir(parents=True)
    MC.models_path(kit).write_text(json.dumps({"version": 1, "providers": [], "capabilities": {}}), encoding="utf-8")
    stamp = MC.file_stamp(kit)
    results: list = []

    def go(i):
        page = {"providers": [{"id": f"p{i}", "name": f"P{i}", "type": "openai", "url": f"https://llm{i}.example.com/v1", "key": "k"}]}
        results.append(MC.save(kit, page, stamp))
    threads = [threading.Thread(target=go, args=(i,)) for i in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    # На медленной файловой системе (Windows) очередь за замком может не дождаться: «занято» — честный ответ, повтор вправе.
    assert sum(1 for r in results if r.get("ok")) == 1 and all(r.get("ok") or r.get("conflict") or r.get("code") == "busy" for r in results), results
    assert not list((kit / "local").glob("*.tmp")) and not (kit / "local" / "models.json.lock").exists(), "остались временные файлы"


# ------------------------------------------------------------------ метаданные команд и закладки

@test
def test_command_deps_tolerates_a_malformed_file_and_the_check_flags_contradictions(tmp: Path):
    """`commands` не объект — пусто, а не исключение; неизвестный вариант без связи и противоречивая запись видны проверке."""
    G = _g()
    K = importlib.import_module("kit_commands")
    rows = K.read_registry()
    for payload in ('{"commands": ["a"]}', '{"commands": "x"}', '[]', '{"commands": null}'):
        path = tmp / "d.json"
        path.write_text(payload, encoding="utf-8")
        assert G.load_deps(path) == {}, f"кривой файл метаданных не прочитан как пустой: {payload}"
    data = json.loads((KIT / "command_deps.json").read_text(encoding="utf-8"))
    data["commands"]["kb:lint"]["offline_variant"] = "teleport"
    data["commands"]["agent:build"]["offline_capable"] = True                       # а requires_llm — тоже да
    path = tmp / "d2.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    problems = " | ".join(G.check_deps(rows, str(KIT), path))
    assert "teleport" in problems and "agent:build" in problems and "противоречит" in problems, problems
    # диагностика обращается к шлюзам и не обещает работать без связи; замер поиска нужен вектор модели
    cmds = json.loads((KIT / "command_deps.json").read_text(encoding="utf-8"))["commands"]
    for cmd in ("agent:ping", "agent:probe", "agent:width"):
        assert cmds[cmd]["offline_capable"] is False, f"{cmd} обещает работать без связи"
    assert cmds["ops:search-quality"]["requires_llm"] is True and cmds["ops:search-quality"]["offline_capable"] is False


@test
def test_models_and_commands_views_recover_after_conflicts_and_failures(_t):
    """Статика страницы: «Отменить» перечитывает настройку, список проверки перерисовывается, вкладка команд не падает от сбоя."""
    models = (KIT / "cockpit" / "modules" / "models" / "view.js").read_text(encoding="utf-8")
    assert "onclick: async () => { DIRTY = false; await refresh(ctx); }" in models, "«Отменить правки» не перечитывает настройку"
    assert "health.check = check.value; touchFn(ctx, true)" in models, "выбор проверки не перерисовывает путь и подсказку"
    assert 'r.code === "broken"' in models and "replace_broken" in models, "страница не умеет согласие на замену битого файла"
    cmds = (KIT / "cockpit" / "modules" / "commands" / "view.js").read_text(encoding="utf-8")
    assert "refresh(ctx, true)" in cmds and "if (typing && NET_KEY === key) return;" in cmds, "поиск ходит на сервер на каждую букву"
    assert "catch (e)" in cmds[cmds.index("async function loadNet"):cmds.index("export async function refresh")], "сбой зависимостей роняет вкладку"
    assert "n.llm_internet" in cmds, "нет метки «нужен интернет» у команды с облачной моделью"
    ck = (KIT / "cockpit" / "aurora_cockpit.py").read_text(encoding="utf-8")
    assert '"llm_internet": req["llm_internet"]' in ck, "ответ /api/net/commands не несёт llm_internet"
    panel = (KIT / "cockpit" / "ui" / "panel.js").read_text(encoding="utf-8")
    assert "r.code === \"lossy\"" in panel and "replace_lossy" in panel and "access.lossy_title" in panel, \
        "группа режима доступа не показывает потери и не спрашивает согласия"
