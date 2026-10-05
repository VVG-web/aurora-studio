"""Проверки движка Aurora, часть 20: Git проекта — сервер, обновление, отправка (1.154.0).

Каркас и помощники — tests/harness.py. У каждого проекта свой сервер и свой вход; секреты —
вне проекта; неудача — причиной, советом и действием; автоматика работает только
включённой и не молчит; провайдеры — модули со своей версией на странице «Установка».
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re
import subprocess
import sys
import time

from harness import KIT, SCRIPTS, run, set_home, test  # noqa: F401


def _gs():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import importlib
    return importlib.import_module("git_sync")


def _git(cwd: Path, *args) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _server(tmp: Path) -> Path:
    bare = tmp / "server.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    return bare


def _code(res: dict) -> str:
    return (res.get("problem") or {}).get("code", "")


@test
def test_each_project_keeps_its_own_git_setup_and_secrets_stay_outside(tmp: Path):
    """Настройка — в `.git/aurora` проекта (не в истории); токен — в `~/.aurora`, правами
    600, наружу маской; ни в адрес сервера, ни в аргументы git он не попадает."""
    GS = _gs()
    restore = set_home(tmp / "home")
    try:
        a, b = _repo(tmp / "a"), _repo(tmp / "b")
        r = GS.save(a, {"provider": "gitea", "instance": "https://git.example.com:3100",
                        "repo": "team/a", "branch": "main"},
                    {"auth": "token", "user": "me", "secret": "tok-abcdef-123"})
        assert r["ok"], r
        assert GS.save(b, {"provider": "generic", "repo": str(tmp / "srv.git")}, None)["ok"]
        assert (a / ".git" / "aurora" / "git.json").is_file()
        assert GS.load(a)["provider"] == "gitea" and GS.load(b)["provider"] == "generic", \
            "настройка проекта перетекла в соседний"
        assert _git(a, "status", "--porcelain") == "", "настройка Git легла в рабочее дерево"
        assert _git(a, "remote", "get-url", "origin") == "https://git.example.com:3100/team/a.git"
        cred = tmp / "home" / ".aurora" / "git" / "credentials.json"
        assert cred.is_file() and oct(cred.stat().st_mode & 0o777) == "0o600"
        for f in (a / ".git" / "config", a / ".git" / "aurora" / "git.json"):
            assert "tok-abcdef-123" not in f.read_text(encoding="utf-8"), f"токен лёг в {f.name}"
        assert GS.cred_masked(a)["secret"] == GS.MASK
        # страница присылает маску обратно — токен прежний; пустой при входе по токену — отказ
        assert GS.save(a, GS.load(a), {"auth": "token", "user": "me", "secret": GS.MASK})["ok"]
        assert GS.cred_load(a)["secret"] == "tok-abcdef-123", "маска затёрла токен"
        bad = GS.save(a, GS.load(a), {"auth": "token", "user": "me", "secret": ""})
        assert not bad["ok"] and {"field": "secret", "code": "required"} in bad["problems"]
        # вход — через помощник учётных данных из окружения, не через аргументы
        env = {}
        args = GS.auth_config(GS.load(a), GS.cred_load(a), env)
        assert "tok-abcdef-123" not in " ".join(args) and env["AURORA_GIT_PASS"] == "tok-abcdef-123"
        assert any(x == "credential.helper=" for x in args), "системный помощник не сброшен"
        assert "tok-abcdef-123" not in GS.redact("fatal: https://me:tok-abcdef-123@host/x")
    finally:
        restore()


@test
def test_git_settings_are_validated_field_by_field(_t):
    """Каждое неверное поле называется само, с причиной: панель подсвечивает именно его."""
    GS = _gs()

    def codes(**kw):
        return {(p["field"], p["code"]) for p in GS.normalize({**kw})[1]}
    assert ("instance", "url") in codes(instance="git.example.com")
    assert ("instance", "required") in codes(provider="gitea", repo="team/a")
    assert ("repo", "repo") in codes(repo="просто имя")
    assert ("branch", "branch") in codes(branch="a..b")
    assert ("author_email", "email") in codes(author_email="кто-то")
    assert ("message", "placeholder") in codes(message="Aurora {нет_такого}")
    assert ("auto_update", "every") in codes(auto_update={"enabled": True, "on": ["interval"], "every_min": 1})
    assert ("auto_push", "no_trigger") in codes(auto_push={"enabled": True, "on": []})
    s, p = GS.normalize({"repo": "https://me:secret@git.example.com/a/b.git"})
    assert ("repo", "creds_in_url") in {(x["field"], x["code"]) for x in p} and "secret" not in s["repo"]
    for ok in ("/srv/git/a.git", "file:///srv/a.git", "git@git.example.com:a/b.git",
               "ssh://git@git.example.com:2222/a/b.git", "https://git.example.com:3100/a/b.git"):
        assert not GS.normalize({"repo": ok})[1], f"законный адрес отвергнут: {ok}"
    assert GS.normalize({"provider": "gitlab", "instance": "https://gitlab.example.com",
                         "repo": "group/sub/proj"})[0]["repo"] == "group/sub/proj"


@test
def test_update_push_and_conflicts_work_against_a_real_server(tmp: Path):
    """Сервер — настоящий репозиторий, клонов два: расхождение, конфликт, выбор версии,
    перенос своих поверх серверных, отказ отправки и «обновить и отправить»."""
    GS = _gs()
    restore = set_home(tmp / "home")
    try:
        bare = _server(tmp)
        a, b = _repo(tmp / "a"), _repo(tmp / "b")
        cfg = {"repo": str(bare), "branch": "main", "message": "Aurora: {project} ({count})"}
        assert GS.save(a, cfg, None)["ok"] and GS.save(b, cfg, None)["ok"]
        (a / "a.md").write_text("one\ntwo\nthree\n", encoding="utf-8")
        r = GS.push(a)
        assert r["ok"] and r["pushed"] == 1, r
        assert _git(a, "rev-parse", "--abbrev-ref", "@{u}") == "origin/main", "ветка не связана"
        r = GS.update(b)
        assert r["ok"] and (b / "a.md").is_file(), f"пустой клон не встал на сервер: {r}"
        assert GS.push(a)["nothing"], "отправка без новых фиксаций сходила на сервер"
        # разошлись
        (a / "a.md").write_text("one\nA\nthree\n", encoding="utf-8")
        assert GS.commit(a, "A")["ok"] and GS.push(a)["ok"]
        (b / "a.md").write_text("one\nB\nthree\n", encoding="utf-8")
        assert GS.commit(b, "B")["ok"]
        r = GS.push(b)
        assert _code(r) == "rejected" and r["problem"]["actions"] == ["update_then_push"], r
        assert _code(GS.update(b)) == "diverged", "только перемотка молча слила истории"
        r = GS.update(b, strategy="merge")
        assert _code(r) == "conflict" and r["problem"]["files"] == ["a.md"], r
        st = GS.status(b)
        assert st["operation"] == "merge" and st["conflicts"] == ["a.md"]
        assert _code(GS.push(b)) == "in_progress", "отправка посреди конфликта не остановлена"
        assert _code(GS.fix(b, "continue")) == "conflict", "конфликт закрыли, не разобрав"
        (b / "a.md").write_text("one\n<<<<<<< HEAD\nB\n=======\nA\n>>>>>>> x\nthree\n", encoding="utf-8")
        assert _code(GS.fix(b, "resolved", "a.md")) == "conflict_markers"
        assert GS.fix(b, "mine", "a.md")["ok"]
        assert (b / "a.md").read_text(encoding="utf-8").splitlines()[1] == "B"
        assert GS.fix(b, "continue")["ok"] and GS.push(b)["ok"]
        assert GS.update(a)["ok"] and (a / "a.md").read_text(encoding="utf-8").splitlines()[1] == "B"
        # перенос своих поверх серверных: «серверная» версия при переносе — у git «наша»
        (a / "a.md").write_text("one\nX-A\nthree\n", encoding="utf-8")
        GS.commit(a, "A2")
        GS.push(a)
        (b / "a.md").write_text("one\nX-B\nthree\n", encoding="utf-8")
        GS.commit(b, "B2")
        assert _code(GS.update(b, strategy="rebase")) == "conflict"
        st = GS.status(b)
        assert st["branch"] == "main" and "detached" not in {p["code"] for p in st["problems"]}, \
            "посреди переноса раздел сказал «не на ветке» вместо «не закончено»"
        assert GS.fix(b, "theirs", "a.md")["ok"]
        assert (b / "a.md").read_text(encoding="utf-8").splitlines()[1] == "X-A"
        assert GS.fix(b, "continue")["ok"] and GS.status(b)["operation"] == ""
        # незафиксированное переживает обновление; «обновить и отправить» — одним действием
        (a / "n.md").write_text("new\n", encoding="utf-8")
        GS.commit(a, "A3")
        GS.push(a)
        (b / "local.md").write_text("mine\n", encoding="utf-8")
        r = GS.push(b, update_first=True)
        assert r["ok"] and (b / "n.md").is_file() and (b / "local.md").is_file(), r
        log = GS.read_log(b, 50)
        assert log and {"update", "push", "commit", "fix"} <= {e["action"] for e in log}
    finally:
        restore()


@test
def test_git_errors_become_plain_reasons_with_fixes(_t):
    """Сырой вывод git не единственное, что видит человек: у отказа есть код, слова,
    совет и действие, и у каждого кода — строки раздела на обоих языках."""
    GS = _gs()
    samples = {
        "fatal: Authentication failed for 'https://git.example.com/a/b.git/'": "auth",
        "remote: HTTP Basic: Access denied": "auth",
        "fatal: could not read Username for 'https://x': terminal prompts disabled": "auth",
        "The requested URL returned error: 403": "forbidden",
        "remote: Repository not found.": "repo_not_found",
        "fatal: unable to access 'https://x/': SSL certificate problem: self-signed certificate": "cert",
        "fatal: unable to access 'https://x/': Could not resolve host: x": "network",
        "git@x: Permission denied (publickey).\nfatal: Could not read from remote repository.": "ssh_key",
        "Host key verification failed.": "ssh_hostkey",
        " ! [rejected]        main -> main (fetch first)\nerror: failed to push some refs": "rejected",
        " ! [remote rejected] main -> main (pre-receive hook declined)": "protected",
        "fatal: Not possible to fast-forward, aborting.": "diverged",
        "CONFLICT (content): Merge conflict in a.md\nAutomatic merge failed": "conflict",
        "error: Your local changes to the following files would be overwritten by merge:": "dirty",
        "fatal: Unable to create '/x/.git/index.lock': File exists.": "locked",
        "fatal: refusing to merge unrelated histories": "unrelated",
        "fatal: couldn't find remote ref main": "remote_branch_missing",
        "fatal: The current branch main has no upstream branch.": "no_upstream",
    }
    for text, code in samples.items():
        assert GS.classify(text) == code, f"{code}: {text!r} → {GS.classify(text)}"
    assert GS.commit_code("отказ ... AURORA_SKIP_RATCHET=1 git commit") == "ratchet"
    view = (KIT / "cockpit/modules/gitsync/view.js").read_text(encoding="utf-8")
    cats = {lang: json.loads((KIT / f"cockpit/modules/gitsync/i18n/{lang}.json").read_text(encoding="utf-8"))
            for lang in ("ru", "en")}
    for code, (title, fix, actions) in GS.PROBLEMS.items():
        assert title and fix, code
        for lang, cat in cats.items():
            assert f"gitsync.p.{code}" in cat and f"gitsync.fix.{code}" in cat, f"{lang}: нет слов для {code}"
        for a in actions:
            assert f"    {a}: " in view, f"действие «{a}» ({code}) панель не умеет выполнить"
            assert all(f"gitsync.a.{a}" in cat for cat in cats.values()), f"у действия {a} нет подписи"


@test
def test_git_automation_runs_only_when_switched_on_and_never_silently(tmp: Path):
    """Автоматика: выключена — ничего не делает; включена — на своих событиях; занятый
    проект не трогает; отказ ставит отметку, удача её снимает."""
    GS = _gs()
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    GA = importlib.import_module("git_auto")
    restore = set_home(tmp / "home")
    try:
        p = _repo(tmp / "p")
        (p / "a.md").write_text("x\n", encoding="utf-8")
        _git(p, "add", "-A")
        _git(p, "commit", "-q", "-m", "init")
        out = run("git_sync.py", "--update", "--auto=interval", "--project", str(p), cwd=p, expect_rc=0)
        assert "выключена" in out.stdout and GS.read_log(p) == [], "выключенная автоматика сработала"

        class Ck:
            busy = False
            jobs = []

            def project_activity(self, project):
                return {"running": self.busy}

            def start_job(self, project, cmd, args, parent=""):
                self.jobs.append((cmd, args))
                return "job"
        ck = Ck()
        auto = GA.GitAuto(ck, lambda: [{"path": str(p)}])
        assert auto.tick() == [], "без сохранённой настройки автоматика пошла сама"
        GS.save(p, {"repo": str(tmp / "srv.git"),
                    "auto_update": {"enabled": True, "on": ["interval", "open"], "every_min": 5},
                    "auto_push": {"enabled": True, "on": ["after_commit"]}}, None)
        assert auto.tick() == [(str(p), "update", "interval")]
        assert auto.tick() == [], "расписание сработало дважды подряд"
        # после фиксации — когда фиксации перестали идти
        GS.write_state(str(p), auto_update_at=time.time())
        assert auto.tick() == [], "отправка без выдержки после фиксации"
        GS.write_state(str(p), seen_at=time.time() - GS.DEBOUNCE_S - 1)
        assert auto.tick() == [(str(p), "push", "after_commit")]
        assert auto.tick() == [], "одну и ту же фиксацию отправляли на каждом тике"
        # открытие проекта: занят — отложено с записью, освободился — выполнено
        ck.busy = True
        r = auto.event(str(p), "open")
        assert r["why"] == "busy" and GS.read_log(p)[0]["skipped"] == "busy"
        assert auto.tick() == [], "занятый проект тронули"
        ck.busy = False
        assert auto.tick() == [(str(p), "update", "open")]
        assert auto.event(str(p), "open")["why"] == "recent", "сервер дёргают на каждом открытии"
        # отказ виден, удача его снимает
        GS.record(p, GS.result("push", False, prob=GS.problem("auth")), "after_commit")
        assert GS.alert(p) and GS.alert(p)["code"] == "auth"
        GS.record(p, GS.result("push", True, "ok"), "after_commit")
        assert GS.alert(p) is None
        assert all(c.startswith("git:") and a[0].startswith("--auto=") for c, a in ck.jobs)
    finally:
        restore()


@test
def test_routes_update_before_and_push_after_when_asked(_t):
    """«Перед маршрутом» и «после маршрута» — шаги самого маршрута: видны в его журнале и
    в истории, отказ маршрут не роняет."""
    src = (KIT / "cockpit/route_runner.py").read_text(encoding="utf-8")
    run_body = src[src.index("    def run(self) -> dict:"):src.index("    def _cycle(")]
    assert run_body.index('self._git("update", "before_route")') < run_body.index("git_head(self.project)"), \
        "обновление перед маршрутом идёт после того, как маршрут запомнил свою отправную точку"
    fin = src[src.index("    def _finish("):src.index("    def _close(")]
    assert 'if reason == "passed":\n            self._git("push", "after_route")' in fin
    git = src[src.index("    def _git("):src.index("    def run(self)")]
    assert "self.exec_step(" in git and '"warn"' in git, "шаг Git идёт мимо журнала или молчит об отказе"
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    GA = importlib.import_module("git_auto")
    on = {"auto_update": {"enabled": True, "on": ["before_route"], "every_min": 60},
          "auto_push": {"enabled": False, "on": ["after_route"], "every_min": 60}}
    assert GA.GitAuto.wanted(on, "update", "before_route")
    assert not GA.GitAuto.wanted(on, "push", "after_route"), "выключенная отправка сработала"
    assert not GA.GitAuto.wanted(None, "update", "before_route")


class _Http:
    """Подставной сервер API: {адрес: (код, json, заголовки)}; что спросили — в `seen`."""

    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def __call__(self, url, headers=None, timeout=20):
        self.seen.append((url, dict(headers or {})))
        for tail, (st, data, hdrs) in self.routes.items():
            if url.endswith(tail):
                return st, data, "" if st == 200 else f"HTTP {st}", hdrs
        return 404, None, "HTTP 404", {}


@test
def test_provider_modules_install_update_and_check_through_api(tmp: Path):
    """Модули Gitea, GitLab, Bitbucket: ставятся из кита, устаревший видно, битый не
    заменяет рабочий; адаптеры отличают «вход не принят», «нет прав» и «нет репозитория»."""
    GS = _gs()
    restore = set_home(tmp / "home")
    try:
        rows = {r["id"]: r for r in GS.module_rows(KIT)}
        assert set(rows) >= {"gitea", "gitlab", "bitbucket"} and all(r["missing"] for r in rows.values())
        assert GS.module_state("generic")["state"] == "builtin"
        assert GS.module_state("gitea", KIT)["state"] == "missing"
        r = GS.install_module("gitea", KIT)
        assert r["ok"] and GS.module_state("gitea", KIT)["state"] == "ok", r
        man = tmp / "home" / ".aurora" / "git-providers" / "gitea" / "provider.json"
        data = json.loads(man.read_text(encoding="utf-8"))
        man.write_text(json.dumps({**data, "version": "0.0.1"}), encoding="utf-8")
        assert GS.module_state("gitea", KIT)["state"] == "outdated"
        assert {r["id"]: r for r in GS.module_rows(KIT)}["gitea"]["update"]
        # битый модуль в ките не заменяет рабочий
        fake = tmp / "kit"
        (fake / "gitproviders" / "gitea").mkdir(parents=True)
        (fake / "gitproviders" / "gitea" / "provider.json").write_text(
            json.dumps({"id": "gitea", "version": "9.9.9"}), encoding="utf-8")
        (fake / "gitproviders" / "gitea" / "adapter.py").write_text("x = 1\n", encoding="utf-8")
        assert not GS.install_module("gitea", fake)["ok"]
        assert json.loads(man.read_text(encoding="utf-8"))["version"] == "0.0.1", "битый модуль заменил рабочий"
        for mid in ("gitea", "gitlab", "bitbucket"):
            assert GS.install_module(mid, KIT)["ok"]
        gitea, gitlab, bb = (GS.load_adapter(m) for m in ("gitea", "gitlab", "bitbucket"))
        cfg = {"instance": "https://git.example.com:3100", "repo": "team/a", "auth": "token",
               "user": "", "secret": "t"}
        http = _Http({"/api/v1/user": (200, {"login": "me"}, {}),
                      "/api/v1/repos/team/a": (200, {"default_branch": "main",
                                                     "permissions": {"push": True},
                                                     "clone_url": "https://git.example.com:3100/team/a.git"}, {})})
        r = gitea.check(cfg, http)
        assert r["ok"] and r["login"] == "me" and r["repo"]["default_branch"] == "main", r
        assert http.seen[0][1]["Authorization"] == "token t"
        r = gitea.check(cfg, _Http({"/api/v1/user": (200, {"login": "me"}, {}),
                                    "/api/v1/repos/team/a": (200, {"permissions": {"push": False}}, {})}))
        assert r["code"] == "forbidden", r
        assert gitea.check(cfg, _Http({"/api/v1/user": (401, None, {})}))["code"] == "auth"
        assert gitea.check(cfg, _Http({"/api/v1/user": (200, {"login": "me"}, {})}))["code"] == "repo_not_found"
        assert gitea.clone_urls(cfg)["ssh"] == "git@git.example.com:team/a.git"
        gl = {**cfg, "repo": "group/sub/proj"}
        http = _Http({"/api/v4/user": (200, {"username": "me"}, {}),
                      "/api/v4/projects/group%2Fsub%2Fproj": (200, {"default_branch": "dev",
                          "permissions": {"project_access": {"access_level": 20}}}, {})})
        r = gitlab.check(gl, http)
        assert r["code"] == "forbidden" and http.seen[0][1]["PRIVATE-TOKEN"] == "t", r
        bbc = {**cfg, "instance": "https://bb.example.com", "repo": "PRJ/repo"}
        http = _Http({"/rest/api/1.0/projects/PRJ/repos/repo": (200, {"links": {"clone": [
            {"name": "http", "href": "https://bb.example.com/scm/prj/repo.git"}]}}, {"x-ausername": "me"}),
            "/default-branch": (200, {"displayId": "main"}, {})})
        r = bb.check(bbc, http)
        assert r["ok"] and r["login"] == "me" and r["repo"]["default_branch"] == "main", r
        assert bb.clone_urls(bbc)["http"] == "https://bb.example.com/scm/prj/repo.git"
        assert bb.clone_urls(bbc)["ssh"].endswith(":7999/prj/repo.git")
        assert bb.clone_urls({**bbc, "instance": "https://bitbucket.org"})["http"] == \
            "https://bitbucket.org/PRJ/repo.git"
        # адрес клонирования по провайдеру: модуль стоит — его правило
        s = GS.normalize({"provider": "bitbucket", "instance": "https://bb.example.com", "repo": "PRJ/repo"})[0]
        assert GS.repo_url(s, {"auth": "token"}) == "https://bb.example.com/scm/prj/repo.git"
        assert GS.repo_url(s, {"auth": "ssh"}).startswith("ssh://git@bb.example.com:7999/")
    finally:
        restore()


@test
def test_trusting_a_certificate_pins_it_instead_of_switching_checks_off(tmp: Path):
    """Свой сертификат сервера: доверие сохраняет именно его (по отпечатку, сверенному
    перед сохранением), проверка остаётся включённой."""
    GS = _gs()
    restore = set_home(tmp / "home")
    saved = GS.cert_info
    try:
        p = _repo(tmp / "p")
        GS.save(p, {"provider": "gitea", "instance": "https://git.example.com:3100", "repo": "a/b"}, None)
        GS.cert_info = lambda url: {"ok": True, "host": "git.example.com", "port": 3100,
                                    "pem": "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n",
                                    "sha256": "AA:BB"}
        assert not GS.trust_cert(p, "https://git.example.com:3100", "CC:DD")["ok"], \
            "доверие выдано сертификату с другим отпечатком"
        r = GS.trust_cert(p, "https://git.example.com:3100", "AA:BB")
        assert r["ok"] and Path(r["path"]).is_file()
        tls = GS.load(p)["tls"]
        assert tls == {"verify": True, "ca_file": r["path"]}, tls
        args = GS.auth_config(GS.load(p), GS.cred_load(p), {})
        assert f"http.sslCAInfo={r['path']}" in args and "http.sslVerify=false" not in args
        off = GS.auth_config({**GS.load(p), "tls": {"verify": False, "ca_file": ""}}, {}, {})
        assert "http.sslVerify=false" in off
    finally:
        GS.cert_info = saved
        restore()


@test
def test_the_git_section_is_native_and_actionable(_t):
    """Раздел «Git»: состояние, кнопки, автоматика, разбор конфликтов, настройка с
    проверкой полей на месте; «Установка» ставит и обновляет модули; меню отмечает отказ."""
    mod = KIT / "cockpit/modules/gitsync"
    meta = json.loads((mod / "module.json").read_text(encoding="utf-8"))
    assert meta["group"] == "project" and meta["needs"] == ["project"]
    assert {"git:update", "git:push"} <= set(meta["commands"])
    view = (mod / "view.js").read_text(encoding="utf-8")
    for need, why in (('["state", "setup"]', "нет вкладок «Состояние / Настройка»"),
                      ('act(ctx, "git:update")', "нет кнопки «Обновить»"),
                      ('act(ctx, "git:push")', "нет кнопки «Отправить»"),
                      ('"git:commit"', "нет фиксации"),
                      ("autoCard(ctx)", "нет автоматики на экране состояния"),
                      ('fx("mine", f)', "нет выбора своей версии в конфликте"),
                      ('fx("theirs", f)', "нет выбора серверной версии"),
                      ('fx("abort")', "конфликт нельзя отменить"),
                      ("function validate()", "поля проверяются только после сохранения"),
                      ('"/api/gitsync/check"', "нет проверки подключения"),
                      ('action: "trust"', "сертификату нельзя довериться"),
                      ("lastFailure(ctx, resolving)", "отказ автоматики не показан на экране")):
        assert need in view, why
    assert not re.search(r'["\'][^"\'\n]*[а-яё][^"\'\n]*["\']', re.sub(r"//[^\n]*|/\*.*?\*/", "", view, flags=re.S)), \
        "русская надпись в коде раздела — мимо каталога строк"
    install = (KIT / "cockpit/modules/install/view.js").read_text(encoding="utf-8")
    assert "gitModsCard(ctx)" in install and '"/api/gitmods/install"' in install
    assert "gitModLine(ctx, p.git_provider)" in install, "устаревший модуль проекта не виден на «Установке»"
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    assert 'setBadge("gitsync"' in panel and '"/api/gitsync/event"' in panel and "renderGitCard(box)" in panel
    ck = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    for route in ('"/api/gitsync"', '"/api/gitmods"', '"/api/gitmods/install"', '"/api/gitsync/settings"',
                  '"/api/gitsync/check"', '"/api/gitsync/event"', '"/api/gitsync/cert"'):
        assert route in ck, f"нет адреса {route}"
    assert "GITAUTO.start()" in ck, "автоматика не запускается вместе с панелью"


@test
def test_git_commands_are_registered_and_the_engine_ships_them(_t):
    """Команды `git:*` в реестре, `git_sync.py` едет в проект; модули провайдеров — нет:
    они ставятся на машину со страницы «Установка»."""
    reg = (KIT / "commands.txt").read_text(encoding="utf-8")
    for cmd in ("git:status", "git:update", "git:push", "git:commit", "git:fix", "git:check"):
        assert f"| {cmd} |" in reg, cmd
    man = (KIT / "engine_manifest.txt").read_text(encoding="utf-8")
    assert "scripts/git_sync.py" in man and "gitproviders" not in man
    for mid in ("gitea", "gitlab", "bitbucket"):
        meta = json.loads((KIT / "gitproviders" / mid / "provider.json").read_text(encoding="utf-8"))
        assert meta["id"] == mid and re.fullmatch(r"\d+\.\d+\.\d+", meta["version"])
        assert (KIT / "gitproviders" / mid / "adapter.py").is_file()


@test
def test_every_flag_the_git_section_sends_is_accepted_by_its_command(_t):
    """Флаги, которые шлёт раздел, принимает реестр команды. 1.154.0 в разработке слал
    `git:fix --fix=…` к команде, которая сама вызывается с `--fix`: панель отказывала
    «флаг не объявлен», и «Взять с сервера» в конфликте не работала."""
    sys.path.insert(0, str(KIT / "cockpit"))
    import importlib
    ck = importlib.import_module("aurora_cockpit")
    view = (KIT / "cockpit/modules/gitsync/view.js").read_text(encoding="utf-8")
    calls = re.findall(r'(?:act\(ctx, |run\()"(git:\w+)",\s*\[([^\]]*)\]', view)
    assert len(calls) >= 12, calls
    for cmd, args in calls:
        row = ck.command_by_name(cmd)
        assert row and row["runnable"], f"нет команды {cmd}"
        for flag in re.findall(r'"(--[\w-]+)', args):
            assert flag in row["flags"] and flag not in row["fixed_flags"], \
                f"{cmd} {flag}: панель откажет «флаг не объявлен»"
