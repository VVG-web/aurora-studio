"""Проверки движка Aurora, часть 110. Каркас и помощники — tests/harness.py.

Новый проект и доверие сертификату. Два пути создания не смешиваются: локальный (каркас в папке, git подключают
потом) и из git (репозиторий клонируется, настройка Git проекта пишется сразу). Кнопка «Доверять сертификату» не
врёт: доверие запоминается на машине по адресу сервера, действует и у проекта без `.git`, и у быстрого подключения,
и при клонировании, а переменная окружения с общим набором сертификатов его не отменяет. Сеть настоящая только
в петле 127.0.0.1; домашняя папка подменена.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import ssl
import subprocess
import sys
import threading
import types
import urllib.request
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from harness import KIT, SCRIPTS, set_home, test  # noqa: F401


def _gs():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("git_sync")


def _ck():
    for p in (str(SCRIPTS), str(KIT / "cockpit")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("aurora_cockpit")


def _git(cwd: Path, *args) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def _bare(path: Path, files: dict | None = None) -> Path:
    """Голый репозиторий «сервера»; с файлами — в нём уже есть коммит на ветке main."""
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True)
    if files:
        work = path.parent / (path.name + "-work")
        subprocess.run(["git", "clone", "-q", str(path), str(work)], check=True, capture_output=True)
        for k, v in (("user.name", "Test"), ("user.email", "test@example.com"), ("commit.gpgsign", "false")):
            _git(work, "config", k, v)
        for rel, text in files.items():
            f = work / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        _git(work, "add", ".")
        _git(work, "commit", "-q", "-m", "init")
        _git(work, "push", "-q", "origin", "HEAD:main")
    return path


def _settings(repo, **extra) -> dict:
    return {"provider": "generic", "repo": str(repo), **extra}


# ------------------------------------------------------------------------------------------------ сертификат

@test
def test_trusting_a_certificate_is_kept_for_the_machine_and_reaches_a_project_without_git(tmp: Path):
    """Прежде доверие записывалось только в `.git/aurora/git.json`: у проекта ещё без git (папку создали, git
    подключают потом) записывать было некуда, кнопка отвечала «сертификат сохранён», а ничего не менялось."""
    GS = _gs()
    restore = set_home(tmp / "home")
    saved = GS.cert_info
    url = "https://git.example.com:3100/team/proj.git"
    try:
        GS.cert_info = lambda u: {"ok": True, "host": "Git.Example.com", "port": 3100,
                                  "pem": "-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----\n",
                                  "sha256": "AA:BB"}
        plain = tmp / "plain"
        plain.mkdir()                                              # папка проекта, git ещё нет
        assert not GS.trusted_ca(url)
        assert not GS.trust_cert(plain, url, "CC:DD")["ok"], "доверие выдано сертификату с другим отпечатком"
        assert not GS.trusted_ca(url), "отказ оставил файл сертификата"
        r = GS.trust_cert(plain, url, "AA:BB")
        assert r["ok"] and r["scope"] == "machine" and Path(r["path"]).is_file(), r
        assert GS.trusted_ca(url) == r["path"] and GS.trusted_ca(url.replace("proj", "other")) == r["path"]
        assert not GS.trusted_ca("https://git.example.com:3101/x.git"), "доверие одного порта чужому серверу"
        assert not GS.trusted_ca("git@git.example.com:team/proj.git"), "ssh-адрес не про сертификат"
        assert ":" not in GS.cert_file("::1", 8943).name, "в имени файла остался двоеточие адреса IPv6"
        # доверие действует там, где настройка своего файла не называет
        s = {"provider": "generic", "repo": url, "tls": {"verify": True, "ca_file": ""}}
        flags = GS.auth_config(s, {"auth": "system"}, {})
        assert f"http.sslCAInfo={r['path']}" in flags and "http.sslVerify=false" not in flags, flags
        assert GS.effective_tls(s["tls"], url) == {"verify": True, "ca_file": r["path"]}
        # а выбор человека сильнее: проверка выключена или назван другой файл — как есть
        assert GS.effective_tls({"verify": False, "ca_file": ""}, url)["verify"] is False
        assert GS.effective_tls({"verify": True, "ca_file": "/x/own.pem"}, url)["ca_file"] == "/x/own.pem"
        assert GS.effective_tls(None, "https://other.example.com/x.git") == {"verify": True, "ca_file": ""}
        # общий набор сертификатов из окружения отменял бы выбранный сертификат сервера (GIT_SSL_CAINFO сильнее -c)
        env = {"GIT_SSL_CAINFO": "/corp/bundle.crt", "KEEP": "1"}
        GS.auth_config(s, {"auth": "system"}, env)
        assert "GIT_SSL_CAINFO" not in env and env["KEEP"] == "1", env
        env2 = {"GIT_SSL_CAINFO": "/corp/bundle.crt"}
        GS.auth_config({**s, "repo": "https://other.example.com/x.git"}, {"auth": "system"}, env2)
        assert env2["GIT_SSL_CAINFO"] == "/corp/bundle.crt", "чужой сервер остался без общего набора"
        # проект под git: файл ещё и в его настройке, чтобы вкладка Git его показала
        proj = tmp / "proj"
        proj.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(proj)], check=True)
        r2 = GS.trust_cert(proj, url, "AA:BB")
        assert r2["ok"] and r2["scope"] == "project", r2
        assert GS.load(proj)["tls"] == {"verify": True, "ca_file": r2["path"]}
    finally:
        GS.cert_info = saved
        restore()


def _openssl() -> str:
    """openssl для сертификата на лету: в PATH, а на Windows — тот, что лежит в Git for Windows."""
    found = shutil.which("openssl")
    if found:
        return found
    git = shutil.which("git")
    if git:
        root = Path(git).resolve().parents[1]
        for rel in ("usr/bin/openssl.exe", "mingw64/bin/openssl.exe", "../usr/bin/openssl.exe"):
            if (root / rel).is_file():
                return str((root / rel).resolve())
    return ""


def _self_signed(folder: Path) -> tuple:
    """Самоподписанный сертификат для 127.0.0.1 → (файл сертификата, файл ключа) или ничего, если нет openssl."""
    exe = _openssl()
    if not exe:
        return None
    conf = folder / "openssl.cnf"
    conf.write_text("[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=127.0.0.1\n"
                    "[v3]\nsubjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\n", encoding="utf-8")
    cert, key = folder / "cert.pem", folder / "key.pem"
    r = subprocess.run([exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-config", str(conf),
                        "-keyout", str(key), "-out", str(cert)], capture_output=True, text=True)
    return (cert, key) if r.returncode == 0 and cert.is_file() and key.is_file() else None


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def _serve_tls(folder: Path, cert: Path, key: Path):
    """Каталог голого репозитория по https (простой «глупый» протокол git: ls-remote и clone его понимают)."""
    srv = HTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(folder)))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@test
def test_a_self_signed_server_is_reached_after_trust_through_real_tls(tmp: Path):
    """Настоящий TLS в петле: до доверия проверка подключения называет сертификат, после — проходит (и у API, и у git);
    проект создаётся из git по этому адресу; сертификат, выпущенный другим, не проходит."""
    GS = _gs()
    pair = _self_signed(tmp)
    if not pair:
        return          # нет openssl — сертификат на лету не сделать; прочее покрыто соседними проверками
    cert, key = pair
    server_dir = tmp / "www"
    server_dir.mkdir()
    bare = _bare(server_dir / "team.git", {"aurora.config.yaml": "name: Demo\n", "AuroraKnowledgeDB/.keep": "x\n"})
    _git(bare, "update-server-info")
    srv = _serve_tls(server_dir, cert, key)
    restore = set_home(tmp / "home")
    saved_env = os.environ.pop("GIT_SSL_CAINFO", None)
    try:
        url = f"https://127.0.0.1:{srv.server_address[1]}/team.git"
        info = GS.cert_info(url)
        der = ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="ascii"))
        want = hashlib.sha256(der).hexdigest().upper()
        assert info["ok"] and info["sha256"].replace(":", "") == want, "отпечаток не тот, что у сертификата сервера"
        settings = {"provider": "generic", "repo": url, "instance": url.rsplit("/", 1)[0]}
        before = GS.check_remote(settings, {"auth": "system"})
        assert not before["ok"] and (before["problem"] or {}).get("code") == "cert", before
        # Python-сторона: тот же сертификат пропускает запрос провайдера
        ctx_plain = GS.tls_context({"verify": True, "ca_file": ""})
        try:
            urllib.request.urlopen(url + "/HEAD", timeout=10, context=ctx_plain)
            reached = True
        except Exception:                                          # noqa: BLE001 — ждём отказ проверки
            reached = False
        assert not reached, "системное хранилище приняло чужой самоподписанный сертификат"
        assert GS.trust_cert(tmp / "nothing", url, "00:11")["ok"] is False
        got = GS.save_cert(url, info["sha256"])
        assert got["ok"], got
        ctx = GS.tls_context(GS.effective_tls({"verify": True, "ca_file": ""}, url))
        with urllib.request.urlopen(url + "/HEAD", timeout=10, context=ctx) as resp:
            assert resp.status == 200
        if sys.platform.startswith("linux"):     # git на Windows и macOS ходит в сеть через schannel и Secure Transport:
                                                 # читает ли он файл сертификата, зависит от сборки, — там проверен только Python
            after = GS.check_remote(settings, {"auth": "system"})
            assert after["ok"], after
            target = tmp / "home" / "projects" / "team"
            made = GS.clone_project(target, settings, {"auth": "system"})
            assert made["ok"] and (target / "aurora.config.yaml").is_file(), made
            assert GS.load_saved(target)["repo"] == url
    finally:
        srv.shutdown()
        srv.server_close()
        if saved_env is not None:
            os.environ["GIT_SSL_CAINFO"] = saved_env
        restore()


# ------------------------------------------------------------------------------------------------ клон проекта

@test
def test_clone_project_writes_the_git_setup_and_fails_without_leaving_anything(tmp: Path):
    GS = _gs()
    restore = set_home(tmp / "home")
    try:
        # репозиторий с проектом: настройка Git записана, имя сервера в git — какое назвали, ветка — серверная
        full = _bare(tmp / "full.git", {"aurora.config.yaml": "name: Demo\n", "notes.md": "n\n"})
        a = tmp / "a"
        r = GS.clone_project(a, _settings(full, remote="upstream"), {"auth": "system"})
        assert r["ok"] and r["branch"] == "main" and r["empty"] is False, r
        assert (a / "notes.md").is_file()
        assert _git(a, "remote") == "upstream" and _git(a, "remote", "get-url", "upstream") == str(full)
        s = GS.load_saved(a)
        assert s and s["repo"] == str(full) and s["remote"] == "upstream" and s["tls"]["verify"] is True, s
        # пустой репозиторий: ветка, какую назвали, станет текущей и дождётся первой отправки
        empty = _bare(tmp / "empty.git")
        b = tmp / "b"
        rb = GS.clone_project(b, _settings(empty, branch="work"), {"auth": "system"})
        assert rb["ok"] and rb["empty"] is True and rb["branch"] == "work", rb
        assert _git(b, "symbolic-ref", "HEAD") == "refs/heads/work"
        assert GS.load_saved(b)["branch"] == "work"
        # ветка из репозитория с коммитами
        c = tmp / "c"
        full_dev = _bare(tmp / "dev.git", {"README.md": "r\n"})
        _git(full_dev, "branch", "dev", "main")
        rc = GS.clone_project(c, _settings(full_dev, branch="dev"), {"auth": "system"})
        assert rc["ok"] and rc["branch"] == "dev", rc
        # такой ветки на сервере нет — отказ по полю, папка не создана
        d = tmp / "d"
        rd = GS.clone_project(d, _settings(full, branch="nope"), {"auth": "system"})
        assert not rd["ok"] and rd["problems"] == [{"field": "branch", "code": "absent"}] and not d.exists(), rd
        # адреса нет — отказ по полю
        rn = GS.clone_project(tmp / "n", _settings(""), None)
        assert not rn["ok"] and {"field": "repo", "code": "required"} in rn["problems"], rn
        # сервер не отвечает: причина кодом, созданная папка убрана, своего не затронуто
        e = tmp / "deep" / "e"
        re_ = GS.clone_project(e, _settings(tmp / "no-such.git"), {"auth": "system"})
        assert not re_["ok"] and re_["problem"]["code"] in ("repo_not_found", "unknown", "bad_url"), re_
        assert not e.exists(), "после неудачи осталась созданная папка"
        # занятая папка: отказ, чужие файлы целы
        busy = tmp / "busy"
        busy.mkdir()
        (busy / "mine.txt").write_text("мои\n", encoding="utf-8")
        rbusy = GS.clone_project(busy, _settings(full), {"auth": "system"})
        assert not rbusy["ok"] and "занята" in rbusy["error"], rbusy
        assert (busy / "mine.txt").read_text(encoding="utf-8") == "мои\n" and not (busy / ".git").exists()
        # пустая существующая папка годится и после неудачи остаётся на месте
        hollow = tmp / "hollow"
        hollow.mkdir()
        assert GS.clone_project(hollow, _settings(tmp / "no-such.git"), {"auth": "system"})["ok"] is False
        assert hollow.is_dir() and not any(hollow.iterdir())
        assert GS.clone_project(hollow, _settings(full), {"auth": "system"})["ok"]
        # вставленный ключ после неудачи не остаётся на диске
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n"
        rk = GS.clone_project(tmp / "k", _settings(tmp / "no-such.git"), {"auth": "ssh", "ssh_key_text": key})
        assert not rk["ok"]
        keys = tmp / "home" / ".aurora" / "git" / "keys"
        assert not keys.is_dir() or not list(keys.iterdir()), "ключ остался после неудачи"
    finally:
        restore()


def _handler(ck, roots: list):
    """Обработчик панели без сервера: из него нужны только корни поиска."""
    h = object.__new__(ck.Handler)
    h.server = types.SimpleNamespace(roots=list(roots))
    return h


class _Roots:
    """Файл корней поиска панели — во временную папку: настоящий лежит в домашней папке человека."""

    def __init__(self, ck, path: Path):
        self.ck, self.path = ck, path

    def __enter__(self):
        self.saved = self.ck.ROOTS_FILE
        self.ck.ROOTS_FILE = str(self.path)
        return self

    def __exit__(self, *exc):
        self.ck.ROOTS_FILE = self.saved


@test
def test_a_new_project_comes_either_from_a_local_folder_or_from_git_never_a_mix(tmp: Path):
    ck = _ck()
    restore = set_home(tmp / "home")
    prepared = []
    saved_prepare = ck.engine_prepare_action
    ck.engine_prepare_action = lambda p: prepared.append(p) or {"ok": True}
    try:
        with _Roots(ck, tmp / "roots.txt"):
            work = tmp / "work"
            work.mkdir()
            h = _handler(ck, [str(work)])
            # локальный путь: каркас, без git; повторное создание в той же папке отказывает
            loc = work / "local"
            r = h._create_project({"path": str(loc), "name": "Local one"})
            assert r.get("ok") and (loc / "aurora.config.yaml").is_file() and not (loc / ".git").exists(), r
            assert "уже есть проект" in h._create_project({"path": str(loc), "name": "Local one"})["error"]
            assert h._create_project({"path": str(work / "x")})["error"], "локальному пути нужно название"
            # из git, репозиторий с проектом Авроры: он и есть проект, каркас поверх не кладётся
            srv_a = _bare(tmp / "a.git", {"aurora.config.yaml": "name: From git\n",
                                          "AuroraKnowledgeDB/.keep": "x\n", "Raw/note.md": "n\n"})
            outside = tmp / "elsewhere" / "from-git"
            ra = h._create_project({"path": str(outside), "name": "ignored",
                                    "git": {"settings": _settings(srv_a, branch="main", author_name="Иван Петров",
                                                                  author_email="ivan@example.com"),
                                            "credentials": {"auth": "system"}}})
            assert ra["ok"] and ra["mode"] == "existing" and ra["engine"] == {"ok": True, "error": ""}, ra
            assert (outside / "aurora.config.yaml").read_text(encoding="utf-8") == "name: From git\n"
            assert prepared == [str(outside)], prepared
            gs = _gs()
            assert gs.load_saved(outside)["repo"] == str(srv_a), "настройка Git не записана при создании"
            assert _git(outside, "config", "--local", "user.email") == "ivan@example.com", "автор фиксаций не записан"
            assert gs.load_saved(outside)["author_name"] == "Иван Петров"
            assert ra["added_root"] == str(outside.parent) and str(outside.parent) in h.server.roots
            assert str(outside) in {p["path"] for p in ck.find_projects(h.server.roots)}, "проект не виден панели"
            assert _git(outside, "status", "--porcelain") == "", "клон сразу грязный"
            # из git, репозиторий пуст: структура Авроры разворачивается в нём, название по умолчанию — имя папки
            srv_e = _bare(tmp / "e.git")
            emp = work / "fresh"
            re_ = h._create_project({"path": str(emp), "git": {"settings": _settings(srv_e),
                                                                 "credentials": {"auth": "system"}}})
            assert re_["ok"] and re_["mode"] == "deployed" and re_["empty"] is True, re_
            assert (emp / "aurora.config.yaml").is_file() and (emp / ".git").is_dir()
            assert "fresh" in (emp / "aurora.config.yaml").read_text(encoding="utf-8")
            assert gs.load_saved(emp)["repo"] == str(srv_e)
            # из git, в репозитории чужое: файлы целы, структура — поверх
            srv_o = _bare(tmp / "o.git", {"README.md": "чужой\n"})
            oth = work / "other"
            ro = h._create_project({"path": str(oth), "name": "Other",
                                    "git": {"settings": _settings(srv_o), "credentials": {"auth": "system"}}})
            assert ro["ok"] and ro["mode"] == "deployed", ro
            assert (oth / "README.md").read_text(encoding="utf-8") == "чужой\n" and (oth / "aurora.config.yaml").is_file()
            # отказы: нет папки; в папке уже проект; репозиторий не отвечает — ничего не создано
            assert h._create_project({"path": "", "git": {"settings": _settings(srv_a)}})["error"]
            dup = h._create_project({"path": str(loc), "git": {"settings": _settings(srv_a)}})
            assert "уже есть проект" in dup["error"], dup
            wrong = work / "wrong"
            rw = h._create_project({"path": str(wrong), "git": {"settings": _settings(srv_a, author_email="не почта"),
                                                                "credentials": {"auth": "system"}}})
            assert not rw["ok"] and {"field": "author_email", "code": "email"} in rw["problems"] and not wrong.exists(), rw
            missing = work / "missing"
            rm = h._create_project({"path": str(missing), "git": {"settings": _settings(tmp / "none.git"),
                                                                  "credentials": {"auth": "system"}}})
            assert not rm["ok"] and rm["problem"]["code"] and not missing.exists(), rm
    finally:
        ck.engine_prepare_action = saved_prepare
        restore()


@test
def test_the_panel_offers_both_ways_and_the_trust_button_works_in_every_place_that_needs_it(_t):
    """Разметка и код панели: два режима создания, адрес и вход из строки git clone, проверка, доверие и повтор;
    у вкладки Git — быстрое подключение тоже получает кнопку и само повторяет подключение."""
    panel = (KIT / "cockpit/ui/panel.js").read_text(encoding="utf-8")
    for needle in ('"setup.mode_" + m', '"/api/project/git/check"', '"/api/project/git/cert"',
                   "git: {settings: newGitSettings(g), credentials: g.cred}", "author_email: g.author_email.trim()",
                   'g.lastAction === "create" ? createNewGit() : checkNewGit()'):
        assert needle in panel, needle
    view = (KIT / "cockpit/modules/gitsync/view.js").read_text(encoding="utf-8")
    for needle in ('action === "quick" ? quickCertShow(ctx)', 'scope: "machine"', "await connectQuick(ctx, false)",
                   "await load(ctx, true)", "await runCheck(ctx, CHECK.remote)"):
        assert needle in view, needle
    ck = (KIT / "cockpit/aurora_cockpit.py").read_text(encoding="utf-8")
    for route in ('"/api/project/git/check"', '"/api/project/git/cert"', '"/api/project/new"'):
        assert route in ck, route
    assert "GS.check_remote(" in ck and "GS.clone_project(" in ck
