"""Проверки движка Aurora, часть 154. Каркас и помощники — tests/harness.py.

Реестр репозиториев кода (CR-04): блок `code_repos:` в aurora.config.yaml — разбор и запись блока целиком (соседние
разделы байт в байт), слепок `expect` и расхождение 409, проверка значений (имена по правилам трёх систем, пароль в
адресе, ветка, ревизия, маски, связь), вход — тем же механизмом, что у «Git проекта» (секрет вне проекта, ключ — путь
клона), API раздела «Код», разрешение «Моделям». Сеть не используется: «сервер» — голый репозиторий в папке.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from harness import KIT, SCRIPTS, make_project, set_home, test

sys.path.insert(0, str(KIT / "tests"))
import code_fixtures as CF  # noqa: E402

CONFIG = ('﻿project:\r\n  name: "T"\r\n  slug: "T"\r\n\r\n# мои виды\r\nartifacts:\r\n  ac:\r\n'
          '    title: "Критерии"\r\n\r\nreports:\r\n  week: 1\r\n')


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.reload(importlib.import_module(name))


def _cockpit():
    sys.path.insert(0, str(KIT / "cockpit"))
    return importlib.import_module("aurora_cockpit")


@test
def test_code_repos_round_trip_and_validation_by_the_rules_of_three_systems(_t):
    """Запись и чтение блока — одно и то же; имена, адрес, ветка, ревизия, маски и связь проверяются кодами."""
    C = _mod("code_repos")
    clean, probs = C.check({
        "order-intake": {"url": "https://name@git.example.com/team/order-intake.git", "branch": "main", "models": "true"},
        "py": {"url": "/srv/py.git", "pin": "a1b2c3d", "exclude": "**/gen/**, *.min.js", "access": "vpn:VPN-1"}})
    assert probs == [], probs
    assert clean["order-intake"]["url"] == "https://git.example.com/team/order-intake.git", "логин из адреса не убран"
    assert clean["order-intake"]["models"] is True and clean["py"]["models"] is False and clean["py"]["publish"] is False
    assert clean["order-intake"]["provider"] == "generic"
    assert C.check({"g": {"url": "git@github.com:team/g.git"}})[0]["g"]["provider"] == "github"
    text = C.write_block("project:\n  name: T\n", clean)
    assert C.parse(text) == clean and C.write_block(text, clean) == text, "запись не идемпотентна"
    _, probs = C.check({
        "con": {"url": "/x"}, "Aux.git": {"url": "/x"}, "bad name": {"url": "/x"}, "x" * 65: {"url": "/x"},
        "README.md": {"url": "/x"}, "dup": {"url": "/x"}, "DUP": {"url": "/x"},
        "pwd": {"url": "https://u:secret@git.example.com/a.git"}, "nourl": {},
        "br": {"url": "/x", "branch": "a..b"}, "pin": {"url": "/x", "pin": "HEAD~1"},
        "mask": {"url": "/x", "exclude": "../outside"}, "acc": {"url": "/x", "access": "vpn:"},
        "flag": {"url": "/x", "models": "может быть"}, "nl": {"url": "/x", "branch": "a\nb"}})
    got = {(p["name"], p["field"], p["code"]) for p in probs}
    for want in (("con", "name", "name_reserved"), ("Aux.git", "name", "name_reserved"),
                 ("bad name", "name", "name_chars"), ("x" * 65, "name", "name_long"),
                 ("README.md", "name", "name_reserved"), ("DUP", "name", "name_duplicate"),
                 ("pwd", "url", "creds_in_url"), ("nourl", "url", "required"), ("br", "branch", "branch"),
                 ("pin", "pin", "pin"), ("mask", "exclude", "mask"), ("acc", "access", "access"),
                 ("flag", "models", "bool"), ("nl", "branch", "multiline")):
        assert want in got, f"нет замечания {want}: {sorted(got)}"
    assert "secret" not in json.dumps(C.check({"pwd": {"url": "https://u:secret@h.example.com/a.git"}})[0]), \
        "пароль из адреса попал в нормализованную запись"


@test
def test_code_repos_save_keeps_neighbours_byte_for_byte_and_answers_a_stale_snapshot_with_conflict(tmp: Path):
    """Запись меняет только блок `code_repos:` (метка порядка байтов, CRLF, комментарии вне блока на месте); слепок,
    устаревший после правки руками, — расхождение без записи; пустой реестр снимает блок, файл — как был."""
    C = _mod("code_repos")
    root = tmp / "p"
    root.mkdir()
    cfg = root / "aurora.config.yaml"
    cfg.write_bytes(CONFIG.encode("utf-8"))
    st = C.state(root)
    assert st["repos"] == [] and st["digest"] == C.digest({})
    r = C.save(root, {"app": {"url": "/srv/app.git", "branch": "main"}}, st["digest"])
    assert r.get("ok"), r
    raw = cfg.read_bytes().decode("utf-8")
    assert raw.startswith(CONFIG), "соседние разделы конфига изменились"
    assert "\r\ncode_repos:\r\n  app:\r\n" in raw and "\n" not in raw.replace("\r\n", ""), "окончания строк смешались"
    digest = r["digest"]
    cfg.write_bytes(raw.replace('branch: "main"', 'branch: "dev"').encode("utf-8"))      # правка руками
    r2 = C.save(root, {"app": {"url": "/srv/app.git", "branch": "release"}}, digest)
    assert r2.get("conflict") and 'branch: "dev"' in cfg.read_text(encoding="utf-8"), "чужая правка затёрта"
    assert r2["repos"][0]["branch"] == "dev", "в ответе на расхождение нет свежего состояния"
    r3 = C.save(root, {"app": {"url": "/srv/app.git", "branch": "release"}}, "")
    assert r3.get("ok") and C.read_repos(root)["app"]["branch"] == "release", "«Записать моё поверх» не записало"
    bad = C.save(root, {"app": {"url": "https://u:p@h.example.com/a.git"}}, r3["digest"])
    assert bad.get("problems") and C.read_repos(root)["app"]["url"] == "/srv/app.git", "запись с замечаниями прошла"
    assert C.save(root, {}, r3["digest"]).get("ok")
    assert cfg.read_bytes().decode("utf-8") == CONFIG, "пустой реестр оставил след в конфиге"
    assert C.save(root, {}, None).get("error") == "bad_expect", "слепок не строкой принят"


@test
def test_code_repos_survive_the_settings_form(tmp: Path):
    """Форма настроек собирает конфиг заново по шаблону: блок `code_repos:` она не знает и обязана перенести целиком."""
    C = _mod("code_repos")
    S = _mod("aurora_setup")
    cfg = tmp / "aurora.config.yaml"
    cfg.write_text('project:\n  name: "Demo"\n', encoding="utf-8")
    clean, _ = C.check({"order-intake": {"url": "https://git.example.com/team/order-intake.git", "pin": "a1b2c3d4",
                                         "models": True, "publish": True, "access": "vpn:VPN-1"}})
    cfg.write_text(C.write_block(cfg.read_text(encoding="utf-8"), clean), encoding="utf-8")
    c = {"name": "Demo", "slug": "demo", "conf_url": "https://wiki.example.com", "conf_space": "S", "jira_url": "",
         "jira_key": "D", "jira_jql": "project = D", "trust_statuses": "Done", "assumption_statuses": "Open",
         "scrub": "off", "sync_roots": [], "trusted_sources": "", "trusted_branches": "", "threshold": "70"}
    S.write_config(cfg, dict(c))
    first = cfg.read_text(encoding="utf-8")
    assert C.parse(first) == clean, f"форма настроек стёрла или исказила реестр кода:\n{first[-700:]}"
    S.write_config(cfg, dict(c))
    assert cfg.read_text(encoding="utf-8") == first, "повторное сохранение формы изменило конфиг"


@test
def test_code_repo_secret_lives_outside_the_project_and_does_not_follow_a_new_host(tmp: Path):
    """Вход репозитория — в файле секретов «Git проекта» под ключом-путём клона; в конфиг и наружу — только маска.
    Сменился хост адреса — сохранённый токен не переезжает, его вводят заново."""
    restore = set_home(tmp / "home")
    try:
        C = _mod("code_repos")
        GS = _mod("git_sync")
        root = tmp / "p"
        root.mkdir()
        (root / "aurora.config.yaml").write_text('project:\n  name: "T"\n', encoding="utf-8")
        token = "tok-" + "7" * 24
        r = C.save(root, {"app": {"url": "https://git.example.com/team/app.git"}}, "",
                   {"app": {"auth": "token", "user": "reader", "secret": token}})
        assert r.get("ok"), r
        store = tmp / "home/.aurora/git/credentials.json"
        data = json.loads(store.read_text(encoding="utf-8"))
        key = str((root / "Code" / "app").resolve())
        assert data[key]["secret"] == token and data[key]["user"] == "reader", data.keys()
        assert token not in (root / "aurora.config.yaml").read_text(encoding="utf-8"), "токен в конфиге проекта"
        st = C.state(root)
        assert st["repos"][0]["credentials"]["secret"] == GS.MASK and token not in json.dumps(st), "токен ушёл наружу"
        same = C.save(root, {"app": {"url": "https://git.example.com/team/app2.git"}}, st["digest"],
                      {"app": {"auth": "token", "user": "reader", "secret": GS.MASK}})
        assert same.get("ok") and C.creds_of(root, "app")["secret"] == token, "токен потерян при смене пути на том же сервере"
        moved = C.save(root, {"app": {"url": "https://other.example.com/team/app.git"}}, same["digest"],
                       {"app": {"auth": "token", "user": "reader", "secret": GS.MASK}})
        assert {"name": "app", "field": "secret", "code": "required"} in (moved.get("problems") or []), moved
        assert C.creds_of(root, "app")["secret"] == token, "отказ всё-таки переписал вход"
        gone = C.save(root, {}, same["digest"])
        assert gone.get("ok") and key not in json.loads(store.read_text(encoding="utf-8")), \
            "вход убранного из реестра репозитория остался на машине"
    finally:
        restore()


@test
def test_code_api_reads_saves_parses_and_checks_a_local_server(tmp: Path):
    """API раздела «Код»: состояние, запись со слепком (200 / 409 / 400), разбор строки `git clone` с именем папки,
    проверка подключения `ls-remote` к голому репозиторию в папке (ветка есть / ветки нет)."""
    restore = set_home(tmp / "home")
    try:
        ck = _cockpit()
        C = _mod("code_repos")
        root = make_project(tmp)
        _work, remote = CF.server("ts-portal", tmp / "srv")
        st = ck.code_state(str(root))
        assert st["repos"] == [] and "options" in st and isinstance(st["profiles"], list)
        res, code = ck.code_action("repos", str(root), {"repos": {"ts-portal": {"url": str(remote)}},
                                                       "expect": st["digest"]})
        assert code == 200 and res["ok"] and res["state"]["repos"][0]["name"] == "ts-portal", (code, res)
        res, code = ck.code_action("repos", str(root), {"repos": {}, "expect": st["digest"]})
        assert code == 409 and res["conflict"], (code, res)
        res, code = ck.code_action("repos", str(root), {"repos": {"bad name": {"url": "/x"}}, "expect": ""})
        assert code == 400 and res["problems"], (code, res)
        parsed = C.parse_clone("git clone -b develop https://name@bitbucket.org/team/Order-Intake.git")
        assert parsed["ok"] and parsed["provider"] == "bitbucket" and parsed["branch"] == "develop"
        assert parsed["name"] == "Order-Intake" and parsed["url"] == "https://bitbucket.org/team/Order-Intake.git"
        assert C.parse_clone("git clone")["ok"] is False
        ok, code = ck.code_action("check", str(root), {"name": "ts-portal", "repo": {"url": CF.url_of(remote)}})
        assert code == 200 and ok["ok"] and ok["branches"] == ["main"], ok
        miss, _ = ck.code_action("check", str(root), {"name": "ts-portal",
                                                     "repo": {"url": str(remote), "branch": "release"}})
        assert not miss["ok"] and miss["problem"]["code"] == "remote_branch_missing", miss
        gone, _ = ck.code_action("check", str(root), {"name": "x", "repo": {"url": str(tmp / "nowhere.git")}})
        assert not gone["ok"] and gone["problem"]["code"] in ("repo_not_found", "unknown"), gone
    finally:
        restore()


@test
def test_doctor_names_unclaimed_code_folders_and_registered_repositories_without_a_clone(tmp: Path):
    """Папка в `Code/` без записи реестра — замечание doctor; репозиторий в реестре без клона — замечание с командой."""
    C = _mod("code_repos")
    root = make_project(tmp)
    assert C.save(root, {"app": {"url": "/srv/app.git"}}, "").get("ok")
    (root / "Code/stray/src").mkdir(parents=True)
    (root / "Code/stray/src/x.py").write_text("x = 1\n", encoding="utf-8")
    import subprocess
    cp = subprocess.run([sys.executable, str(root / ".aurora/scripts/aurora_doctor.py")], cwd=str(root),
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert "Code/: папки без записи в реестре репозиториев: stray" in cp.stdout, cp.stdout[-1500:]
    assert "репозитории кода в реестре без клона в Code/: app → выполните code:sync --apply" in cp.stdout, \
        cp.stdout[-1500:]
    assert C.stray_dirs(root) == ["stray"]


@test
def test_model_tools_and_task_attachments_see_code_only_with_the_repository_permission(tmp: Path):
    """Файл клона модели и вложениям задания — только при «Моделям: можно» у репозитория; переключатель снят — снова
    нельзя. Описание папки `Code/README.md` клоном не считается."""
    C = _mod("code_repos")
    RC = _mod("request_context")
    sys.path.insert(0, str(SCRIPTS / "agents"))
    PA = importlib.import_module("pydantic_ai_adapter")
    root = make_project(tmp)
    (root / "Code/README.md").write_text("# Код\n", encoding="utf-8")
    CF.build("py-notify", root / "Code/py-notify")

    def model_reads(rel: str) -> bool:
        try:
            PA.resolve_project_path(str(root), rel)
            return True
        except ValueError:
            return False

    assert model_reads("Code/README.md"), "описание папки Code/ закрыто, как клон"
    assert not model_reads("Code/py-notify/app/main.py"), "клон без записи в реестре открыт модели"
    assert C.save(root, {"py-notify": {"url": "/srv/py.git"}}, "").get("ok")
    assert not model_reads("Code/py-notify/app/main.py"), "по умолчанию «Моделям» должно быть нельзя"
    digest = C.state(root)["digest"]
    assert C.save(root, {"py-notify": {"url": "/srv/py.git", "models": True}}, digest).get("ok")
    assert model_reads("Code/py-notify/app/main.py") and model_reads("code/PY-NOTIFY/app/main.py"), \
        "разрешение репозитория не действует"
    block, _notes = RC.read_context(str(root), ["Code/py-notify/app/consumer.py"])
    assert "orders.accepted" in block, "вложение из разрешённого клона не прочитано"
    digest = C.state(root)["digest"]
    assert C.save(root, {"py-notify": {"url": "/srv/py.git", "models": False}}, digest).get("ok")
    block, notes = RC.read_context(str(root), ["Code/py-notify/app/consumer.py"])
    assert "orders.accepted" not in block and any("«py-notify»" in n for n in notes), notes
