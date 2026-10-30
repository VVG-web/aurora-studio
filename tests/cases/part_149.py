"""Проверки движка Aurora, часть 149. Каркас и помощники — tests/harness.py, подставной Confluence — part_148.py.

Выпуск B «Confluence»: честный конфликт и повтор без изменений. «Git — истина, Confluence — витрина»: страницу, которую
правили руками, публикация не затирает без явного `--force`; чужую страницу с тем же заголовком берёт только `--adopt`;
свою страницу не на месте пути обновляет на месте и переносит только `--move`. Повтор с теми же входами
(`published_input`) и той же версией страницы — `unchanged`, без PUT и без записи в файл, даже когда сервер дописывает
в storage свои атрибуты. Гонка между проверкой и записью (409) — снова `conflict`, а не запись вслепую; обрыв после POST
не создаёт вторую страницу; разовые 401/403/404/5xx — внятная причина, файл не тронут.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

from cases import part_148 as W
from harness import SCRIPTS, test


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


KIND = ('artifacts:\n'
        '  us:\n    title: "История"\n    template: "Templates/US.md"\n    out: "Artifacts/us"\n'
        '    publish_url: "{root}"\n    publish_path: "{path}"\n')
REL = "Artifacts/us/US-1.md"
HEAD = "type: us\nstatus: ready\nrelease: \"1.0\"\n"


def _setup(tmp: Path, wiki, path: str = "Релиз {release}", head: str = HEAD, sub: str = "") -> tuple:
    rid = W.root_page(wiki)
    root = W.project(tmp, wiki.url, KIND.format(root=rid, path=path), sub=sub)
    return rid, root, W.put(root, REL, W.doc(head))


# ------------------------------------------------------------------ повтор без изменений

@test
def test_a_repeat_without_changes_is_unchanged_and_writes_nothing(tmp: Path):
    """Сервер дописывает в storage свои атрибуты (как настоящий Confluence), поэтому тело страницы никогда не равно нашему.
    Повтор сравнивает входы (`published_input`) и версию страницы: `unchanged`, ни PUT, ни записи в файл. Правка текста —
    обновление; правка шапки (статус) — нет."""
    AC = _mod("aurora_common")
    wiki = W.Wiki()
    try:
        _rid, root, path = _setup(tmp, wiki)
        cp, res = W.publish(root, REL, "--apply")
        assert cp.returncode == 0 and res["outcome"] == "ok", cp.stdout + cp.stderr
        pid = res["page"]["id"]
        assert "ac:schema-version" in wiki.pages[pid]["body"], "подставной сервер не нормализует storage — проверка пуста"
        raw, writes = path.read_bytes(), len(wiki.writes())
        for _ in range(2):
            cp, res = W.publish(root, REL, "--apply")
            assert cp.returncode == 0 and res["outcome"] == "unchanged" and res["stamped"] is False, cp.stdout + cp.stderr
        assert len(wiki.writes()) == writes and path.read_bytes() == raw, "повтор без изменений записал страницу или файл"
        assert wiki.pages[pid]["version"] == 1, "версия страницы выросла на пустом месте"
        # правка шапки — не правка страницы
        path.write_text(path.read_text(encoding="utf-8").replace("status: ready", "status: ready\nreviewer: x"), encoding="utf-8")
        _cp, res = W.publish(root, REL, "--apply")
        assert res["outcome"] == "unchanged", res
        # правка текста — обновление и новый отпечаток входов
        before = AC.frontmatter(path.read_text(encoding="utf-8"))["published_input"]
        path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
        cp, res = W.publish(root, REL, "--apply")
        assert res["outcome"] == "ok" and res["action"] == "update" and wiki.pages[pid]["version"] == 2, cp.stdout + cp.stderr
        fm = AC.frontmatter(path.read_text(encoding="utf-8"))
        assert fm["published_input"] != before and fm["published_version"] == "2", fm
        _cp, res = W.publish(root, REL, "--apply")
        assert res["outcome"] == "unchanged", "после обновления повтор снова пишет"
    finally:
        wiki.close()


# ------------------------------------------------------------------ чужая правка, гонка, чужая страница

@test
def test_someone_elses_edit_is_a_conflict_until_force(tmp: Path):
    """Страницу правили после публикации: исход `conflict` с версиями, автором и датой, ни PUT, ни записи в файл; без
    `--force` повтор тот же. `--force` перезаписывает и штампует новую версию."""
    AC = _mod("aurora_common")
    wiki = W.Wiki()
    try:
        _rid, root, path = _setup(tmp, wiki)
        _cp, res = W.publish(root, REL, "--apply")
        pid = res["page"]["id"]
        wiki.edit(pid, by="Коллега")
        raw, writes = path.read_bytes(), len(wiki.writes())
        for args in (("--apply",), ("--json",)):
            cp, res = W.publish(root, REL, *args)
            assert cp.returncode == 1 and res["outcome"] == "conflict", cp.stdout + cp.stderr
            assert res["conflict"] == {"was": "1", "now": "2", "by": "Коллега", "when": "2026-10-02"}, res["conflict"]
            assert res["url"].endswith(f"pageId={pid}"), res["url"]
        assert len(wiki.writes()) == writes and path.read_bytes() == raw, "чужая правка затёрта без клика"
        assert "правка руками" in wiki.pages[pid]["body"]
        cp, res = W.publish(root, REL, "--apply", "--force")
        assert cp.returncode == 0 and res["outcome"] == "ok" and wiki.pages[pid]["version"] == 3, cp.stdout + cp.stderr
        assert "правка руками" not in wiki.pages[pid]["body"]
        assert AC.frontmatter(path.read_text(encoding="utf-8"))["published_version"] == "3"
    finally:
        wiki.close()


@test
def test_a_race_between_check_and_write_is_a_conflict_not_a_blind_write(tmp: Path):
    """Человек сохранил правку, пока шла публикация: сервер ответил 409 на номер версии — публикация перечитывает страницу и
    отдаёт `conflict` со свежей версией; файл не тронут, правка на странице жива."""
    wiki = W.Wiki()
    try:
        _rid, root, path = _setup(tmp, wiki)
        _cp, res = W.publish(root, REL, "--apply")
        pid = res["page"]["id"]
        path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
        raw = path.read_bytes()
        wiki.race.add(pid)
        cp, res = W.publish(root, REL, "--apply")
        assert cp.returncode == 1 and res["outcome"] == "conflict" and res["conflict"]["now"] == "2", cp.stdout + cp.stderr
        assert path.read_bytes() == raw and "правка руками" in wiki.pages[pid]["body"], "запись вслепую после 409"
        puts = [m for m, _p, _b in wiki.writes() if m == "PUT"]
        assert len(puts) == 1, f"после 409 запись повторена: {puts}"
    finally:
        wiki.close()


@test
def test_a_foreign_page_with_the_same_title_is_taken_only_by_adopt(tmp: Path):
    """В пространстве уже есть страница с таким заголовком, написанная не Авророй: `adopt` без записи; `--adopt` берёт её
    под управление (баннер, штамп), дальше она обновляется как своя."""
    AC = _mod("aurora_common")
    wiki = W.Wiki()
    try:
        rid, root, path = _setup(tmp, wiki, path="")
        foreign = wiki.add("Итог", rid, body="<p>написано человеком</p>")
        raw = path.read_bytes()
        cp, res = W.publish(root, REL, "--apply")
        assert cp.returncode == 1 and res["outcome"] == "adopt" and res["page"]["id"] == foreign, cp.stdout + cp.stderr
        assert not wiki.writes() and path.read_bytes() == raw
        cp, res = W.publish(root, REL, "--apply", "--adopt")
        assert cp.returncode == 0 and res["outcome"] == "ok" and res["page"]["id"] == foreign, cp.stdout + cp.stderr
        assert "aurora-generated" in wiki.pages[foreign]["body"] and len(wiki.by_title("Итог")) == 1
        assert AC.frontmatter(path.read_text(encoding="utf-8"))["confluence_page_id"] == foreign
    finally:
        wiki.close()


# ------------------------------------------------------------------ страница не на месте пути

@test
def test_a_misplaced_page_is_updated_in_place_and_moved_only_by_move(tmp: Path):
    """Путь вида сменили после публикации: страница обновляется на месте (исход `misplaced`, названы оба места), разделы
    нового пути без `--move` не создаются. `--move` создаёт их и переносит свою страницу (`ancestors` в PUT). Чужую —
    без баннера Авроры — не переносит."""
    AC = _mod("aurora_common")
    wiki = W.Wiki()
    try:
        rid, root, path = _setup(tmp, wiki)
        _cp, res = W.publish(root, REL, "--apply")
        pid, old_parent = res["page"]["id"], wiki.pages[res["page"]["id"]]["parent"]
        cfg = root / "aurora.config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8").replace('publish_path: "Релиз {release}"', 'publish_path: "Архив > {release}"'),
                       encoding="utf-8")
        # текст тот же — страница не меняется, исход называет оба места
        cp, res = W.publish(root, REL, "--apply")
        assert cp.returncode == 1 and res["outcome"] == "misplaced" and not wiki.by_title("Архив"), cp.stdout + cp.stderr
        assert res["where"]["now"]["title"] == "Релиз 1.0" and res["where"]["want"]["path"] == ["Истории", "Архив", "1.0"], res["where"]
        # текст изменён — обновление на месте, без переноса
        path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "три дня"), encoding="utf-8")
        cp, res = W.publish(root, REL, "--apply")
        assert res["outcome"] == "misplaced" and res["stamped"] is True and wiki.pages[pid]["parent"] == old_parent, cp.stdout
        put = [b for m, _p, b in wiki.writes() if m == "PUT"][-1]
        assert "ancestors" not in put and not wiki.by_title("Архив"), "без --move страница перенесена или разделы созданы"
        # план показывает обе кнопки: обновить на месте и перенести
        _cp, plan = W.publish(root, REL, "--json")
        assert plan["outcome"] == "misplaced" and [c["state"] for c in plan["chain"]] == ["create", "create"], plan
        # перенос — явным --move
        cp, res = W.publish(root, REL, "--apply", "--move")
        assert cp.returncode == 0 and res["outcome"] == "ok" and res["moved"] is True, cp.stdout + cp.stderr
        new_parent = wiki.by_title("1.0")[0]["id"]
        assert wiki.pages[pid]["parent"] == new_parent and wiki.pages[new_parent]["parent"] == wiki.by_title("Архив")[0]["id"]
        assert AC.frontmatter(path.read_text(encoding="utf-8"))["published_parent"] == new_parent
        _cp, res = W.publish(root, REL, "--apply")
        assert res["outcome"] == "unchanged", f"после переноса повтор не узнал страницу: {res['outcome']}"
        # чужую страницу (баннер с неё сняли руками) не переносим
        wiki.pages[pid]["body"] = "<p>переписано руками</p>"
        cfg.write_text(cfg.read_text(encoding="utf-8").replace('publish_path: "Архив > {release}"', 'publish_path: ""'),
                       encoding="utf-8")
        cp, res = W.publish(root, REL, "--apply", "--move", "--force")
        assert res["outcome"] == "blocked" and res["code"] == "not-ours" and wiki.pages[pid]["parent"] == new_parent, res
    finally:
        wiki.close()


# ------------------------------------------------------------------ обрыв и ошибки сервера

@test
def test_a_drop_after_post_does_not_create_a_second_page(tmp: Path):
    """Страница создана, ответ потерян: штампа нет, исход — сетевой отказ. Повтор находит страницу по заголовку и баннеру
    и обновляет её, а не создаёт вторую."""
    AC = _mod("aurora_common")
    wiki = W.Wiki()
    try:
        _rid, root, path = _setup(tmp, wiki)
        raw = path.read_bytes()
        wiki.drop_after_post.add("Итог")
        cp, res = W.publish(root, REL, "--apply")
        assert cp.returncode == 1 and res["outcome"] == "blocked" and res["code"] == "network", cp.stdout + cp.stderr
        assert path.read_bytes() == raw and len(wiki.by_title("Итог")) == 1, "обрыв испортил файл или страниц две"
        cp, res = W.publish(root, REL, "--apply")
        assert cp.returncode == 0 and res["outcome"] == "ok" and res["action"] == "update", cp.stdout + cp.stderr
        assert len(wiki.by_title("Итог")) == 1 and len(wiki.by_title("Релиз 1.0")) == 1, "повтор создал вторую страницу"
        assert AC.frontmatter(path.read_text(encoding="utf-8"))["confluence_page_id"] == wiki.by_title("Итог")[0]["id"]
    finally:
        wiki.close()


@test
def test_server_errors_once_are_named_and_leave_the_file_intact(tmp: Path):
    """401, 403, 404 корня, 503 на записи — один раз каждая: причина словами и кодом, файл байт в байт; после ошибки
    повтор проходит. Разделы, созданные до сбоя, повтор переиспользует."""
    wiki = W.Wiki()
    try:
        rid, root, path = _setup(tmp, wiki)
        raw = path.read_bytes()
        for method, part, code, want in (("GET", f"/content/{rid}?", 401, "http"), ("GET", "/child/page", 403, "http"),
                                         ("GET", f"/content/{rid}?", 404, "root-missing"),
                                         ("POST", "/rest/api/content", 503, "http")):
            wiki.fail.append((method, part, code))
            cp, res = W.publish(root, REL, "--apply")
            assert cp.returncode == 1 and res["outcome"] == "blocked" and res["code"] == want, (code, cp.stdout + cp.stderr)
            if want == "http":
                assert str(res["status"]) == str(code) and str(code) in res["why"], res
            assert path.read_bytes() == raw, f"после ошибки {code} файл изменён"
            assert "Traceback" not in cp.stderr, cp.stderr
        # 503 пришёл на создание раздела — повтор создаёт его один раз и публикует страницу
        cp, res = W.publish(root, REL, "--apply")
        assert cp.returncode == 0 and res["outcome"] == "ok", cp.stdout + cp.stderr
        assert len(wiki.by_title("Релиз 1.0")) == 1 and len(wiki.by_title("Итог")) == 1
        # 503 посреди записи страницы — раздел уже есть, повтор его переиспользует
        path.write_text(path.read_text(encoding="utf-8").replace("пять дней", "два дня"), encoding="utf-8")
        wiki.fail.append(("PUT", "/rest/api/content/", 502))
        cp, res = W.publish(root, REL, "--apply")
        assert res["outcome"] == "blocked" and str(res["status"]) == "502", res
        cp, res = W.publish(root, REL, "--apply")
        assert res["outcome"] == "ok" and len(wiki.by_title("Релиз 1.0")) == 1, cp.stdout + cp.stderr
    finally:
        wiki.close()
