#!/usr/bin/env python3
"""code_fixtures.py — маленькие синтетические репозитории кода для проверок папки `Code/` (фреймворк «Аврора»).

Проверкам синхронизации, а потом индекса и проверки критериев по коду нужен настоящий git: ветки, коммиты с
датами, переписанная история, «сервер», с которого клонируют. Двоичных `.git` в ките нет: репозиторий собирается
во временной папке из дерева файлов `tests/corpus/code/<имя>/tree/` по сценарию `commits.json` — список коммитов
с сообщением, датой и файлами. Рядом `expected.json` — разметка для проверок (языки, а для следующих выпусков —
истинные вердикты критериев).

Сценарий коммита: `add` — пути из `tree/` (байт в байт), `write` — файл, которого в дереве нет: текст с явной
кодировкой и концами строк (`{"text": …, "encoding": "cp1251", "eol": "crlf"}`) или байты (`{"hex": …}`), —
так в ките не лежит ни CRLF, ни cp1251, ни двоичное, а репозиторий их получает; `delete` — убрать путь.

Сборка детерминирована: даты автора и коммиттера заданы сценарием, порядок файлов фиксирован, концы строк git не
трогает (`core.autocrlf=false`). Два построения на одной машине дают те же SHA. Автор коммитов — человек из
настройки git машины (в прогоне тестов — его подставляет `tests/harness.py`), никогда не имя программы.

Сети нет: «удалённый» репозиторий — голый клон в соседней папке, адрес — путь или `file://`.

  python3 tests/code_fixtures.py --into /tmp/code          # все фикстуры рабочими репозиториями
  python3 tests/code_fixtures.py --into /tmp/code --bare   # плюс голые «серверы» рядом (<имя>.git)
  python3 tests/code_fixtures.py --into /tmp/code --name order-intake

Модуль не часть кита (в engine_manifest не попадает); только стандартная библиотека и git.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus" / "code"
NAMES = ("order-intake", "py-notify", "ts-portal", "noise")
BRANCH = "main"


def _env(date: str = "") -> dict:
    """Окружение git без чужих влияний: язык вывода, подсказки, даты коммита из сценария."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"))}
    env.update(LC_ALL="C", LANG="C", GIT_TERMINAL_PROMPT="0", GIT_EDITOR="true")
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return env


CFG = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "commit.gpgsign=false",
       "-c", "tag.gpgsign=false", "-c", "core.quotepath=false", "-c", "init.defaultBranch=" + BRANCH]


def git(repo, *args, date: str = "", check: bool = True) -> str:
    """git в папке репозитория → stdout. Сбой — исключение с выводом: заготовка не должна молча собраться криво."""
    r = subprocess.run(["git", *CFG, "-C", str(repo), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=_env(date), stdin=subprocess.DEVNULL)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} в {repo}: код {r.returncode}\n{r.stdout}\n{r.stderr}")
    return r.stdout.strip()


def scenario(name: str) -> dict:
    return json.loads((CORPUS / name / "commits.json").read_text(encoding="utf-8"))


def expected(name: str) -> dict:
    p = CORPUS / name / "expected.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _bytes(spec) -> bytes:
    """Содержимое файла из `write`: строка — UTF-8 как есть; объект — текст с кодировкой и концами строк или байты."""
    if isinstance(spec, str):
        return spec.encode("utf-8")
    if "hex" in spec:
        return bytes.fromhex(spec["hex"])
    text = str(spec.get("text") or "")
    if spec.get("eol") == "crlf":
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    return text.encode(spec.get("encoding") or "utf-8")


def _put(repo: Path, rel: str, data: bytes) -> None:
    p = repo.joinpath(*rel.split("/"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def init(repo: Path, branch: str = BRANCH) -> Path:
    """Пустой репозиторий с веткой `branch` и без хуков (свой шаблон пуст)."""
    repo = Path(repo).resolve()
    if repo.exists() and any(repo.iterdir()):
        raise RuntimeError(f"{repo}: папка занята — заготовка собирается в новой или пустой")
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "--template=")
    git(repo, "symbolic-ref", "HEAD", "refs/heads/" + branch)
    return repo


def commit(repo, message: str, date: str, files: dict | None = None, delete=(), tree: Path | None = None,
           add=()) -> str:
    """Один коммит: `files` — {путь: содержимое (строка, байты или объект `write`)}, `add` — пути из `tree`,
    `delete` — убрать. → SHA. Пустой коммит (ничего не изменилось) — ошибка сценария."""
    repo = Path(repo)
    for rel in add:
        src = (tree or Path()).joinpath(*rel.split("/"))
        if not src.is_file():
            raise RuntimeError(f"в дереве фикстуры нет {rel}")
        _put(repo, rel, src.read_bytes())
    for rel, spec in sorted((files or {}).items()):
        _put(repo, rel, spec if isinstance(spec, bytes) else _bytes(spec))
    for rel in delete:
        git(repo, "rm", "-q", "--", rel)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--no-verify", "-m", message, date=date)
    return head(repo)


def head(repo, ref: str = "HEAD") -> str:
    return git(repo, "rev-parse", ref)


def build(name: str, dest) -> Path:
    """Рабочий репозиторий фикстуры `name` в папке `dest` (новой или пустой). → путь."""
    sc = scenario(name)
    tree = CORPUS / name / "tree"
    used = set()
    repo = init(Path(dest), sc.get("branch") or BRANCH)
    for c in sc["commits"]:
        commit(repo, c["message"], c["date"], files=c.get("write"), delete=c.get("delete") or (),
               tree=tree, add=c.get("add") or ())
        used |= set(c.get("add") or ())
    # Файл дерева, который ни один коммит не берёт, — забытый в сценарии: репозиторий вышел бы не таким, как задуман.
    # Байт-код (`compileall` в CI проходит и по `tests/`) и служебные файлы системы (.DS_Store) — не дерево фикстуры.
    lying = sorted(p.relative_to(tree).as_posix() for p in tree.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and not p.name.startswith(".")) \
        if tree.is_dir() else []
    lost = [r for r in lying if r not in used]
    if lost:
        raise RuntimeError(f"{name}: в tree/ есть файлы вне сценария: {lost[:5]}")
    return repo


def bare(repo, dest) -> Path:
    """Голый клон — «сервер» для `code:sync`: адрес — путь к нему (или `file://`)."""
    dest = Path(dest).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(dest.parent, "clone", "-q", "--bare", "--", str(Path(repo).resolve()), dest.name)
    return dest


def server(name: str, base) -> tuple:
    """(рабочий репозиторий, голый «сервер») фикстуры в папке `base`: правки — в рабочий, затем `push`."""
    base = Path(base).resolve()
    work = build(name, base / name)
    remote = bare(work, base / f"{name}.git")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "fetch", "-q", "origin")
    git(work, "branch", "-q", "--set-upstream-to=origin/" + (scenario(name).get("branch") or BRANCH))
    return work, remote


def push(repo, branch: str = BRANCH, force: bool = False) -> None:
    git(repo, "push", "-q", *(["--force"] if force else []), "origin", f"HEAD:refs/heads/{branch}")


def url_of(path) -> str:
    """Адрес `file://` для голого репозитория — как его записал бы человек (на Windows — `file:///C:/…`)."""
    return Path(path).resolve().as_uri()


def gen_mono(dest, n: int = 2000, seed: int = 7) -> Path:
    """Большой синтетический монорепозиторий из `n` файлов разных языков одним коммитом — для пределов и скорости.
    Содержимое детерминировано `seed`."""
    rnd = random.Random(seed)
    repo = init(Path(dest))
    langs = (("java", "class C{n} {{ int v{n}() {{ return {n}; }} }}\n"),
             ("py", "def f{n}():\n    return {n}\n"),
             ("ts", "export const v{n} = {n};\n"),
             ("go", "package m\n\nfunc F{n}() int {{ return {n} }}\n"),
             ("sql", "SELECT {n} FROM dual;\n"))
    files = {}
    for i in range(n):
        ext, body = langs[i % len(langs)]
        mod = f"mod{rnd.randrange(40):02d}"
        files[f"{mod}/f{i:05d}.{ext}"] = body.format(n=i).encode("utf-8")
    commit(repo, "generated monorepo", "2026-05-01T00:00:00+00:00", files=files)
    return repo


def build_all(dest, with_bare: bool = False, names=NAMES) -> dict:
    """{имя: путь} всех фикстур в папке `dest`; `with_bare` — рядом голые «серверы» `<имя>.git`."""
    dest = Path(dest).resolve()
    out = {}
    for name in names:
        if with_bare:
            out[name] = server(name, dest)[0]
        else:
            out[name] = build(name, dest / name)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Синтетические репозитории кода для проверок папки Code/")
    ap.add_argument("--into", required=True, metavar="ПАПКА", help="куда собрать (папки фикстур — новые)")
    ap.add_argument("--name", action="append", choices=NAMES, help="только эта фикстура (можно несколько)")
    ap.add_argument("--bare", action="store_true", help="рядом — голые «серверы» <имя>.git для code:sync")
    ap.add_argument("--mono", type=int, default=0, metavar="N", help="ещё монорепозиторий gen-mono из N файлов")
    a = ap.parse_args(argv)
    got = build_all(a.into, a.bare, tuple(a.name or NAMES))
    if a.mono:
        got["gen-mono"] = gen_mono(Path(a.into) / "gen-mono", a.mono)
    for name, path in got.items():
        print(f"{name}: {path}" + (f"  (сервер: {url_of(Path(a.into) / (name + '.git'))})"
                                   if a.bare and name in NAMES else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
