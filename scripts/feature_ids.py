#!/usr/bin/env python3
"""feature_ids.py — устойчивые идентификаторы сценариев Gherkin и их связи с критериями (фреймворк «Аврора»).

Шаги тест-кейсов проекта — Gherkin (решение владельца 09.10): сценарии лежат файлами `.feature` в git, мастер —
git, Confluence — витрина, TMS — исполнение. Чтобы сценарий можно было найти, пересчитать покрытие и не потерять
при правке текста, его личность живёт в теге над ним, а не в позиции или названии:

    @RU.PRJ.US-1.1.1                    ← над Feature: ключ Requirement Yogi истории (непрозрачный, из реестра)
    Функционал: Приём заявки

      @id:SC-0001 @covers:RU.PRJ.AC-1.1.1 @positive @auto
      Сценарий: Заявка принята

`@id:` — идентификатор сценария: выдаёт Аврора (`SC-NNNN`, под замком `kb:next-id`) или, после первого импорта в TMS,
ключ теста оттуда — значение непрозрачно, важна уникальность. `@covers:` — ключ критерия приёмки, который
сценарий проверяет (по одному тегу на ключ). Остальные теги (`@positive`, `@negative`, `@auto`…) Аврора не судит.

  python3 .aurora/scripts/feature_ids.py                 # отчёт по Artifacts/tests/**/*.feature
  python3 .aurora/scripts/feature_ids.py путь…           # файлы или папки
  python3 .aurora/scripts/feature_ids.py --assign        # у каких сценариев нет @id (предпросмотр)
  python3 .aurora/scripts/feature_ids.py --assign --apply  # проставить @id: строкой тегов над сценарием
  python3 .aurora/scripts/feature_ids.py --json

Замечания: SC-01 сценарий без @id; SC-02 несколько @id у одного сценария; SC-03 один @id у двух сценариев;
SC-05 нет @covers; SC-06 @covers называет ключ, которого нет ни в артефактах (`ref_key`), ни на страницах
зеркала (`ry_defines`); SC-07 над Feature нет ключа Requirement Yogi. Выход: 0 — замечаний нет, 1 — есть.

Панель: `ops:scenarios`
В отчётах и рекомендациях называйте эту команду так, как она называется в панели
и в реестре, — а не путём к скрипту: человек нажимает кнопку, а не набирает python3.
"""
from __future__ import annotations
import aurora_common  # noqa: F401 — защита версии Python: на старом — понятный текст, а не traceback

import argparse
import json
import os
import re
import sys
for _s in (sys.stdin, sys.stdout, sys.stderr):
    # Windows: консоль и труба в cp1251/cp866 падают на эмодзи и «—» (UnicodeEncodeError)
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from aurora_common import frontmatter, head_text, read_verdict, real_inside, safe_walk  # noqa: E402

FEATURES = "Artifacts/tests"
FEATURE_WORDS = ("feature", "функция", "функциональность", "функционал", "свойство")
SCENARIO_WORDS = ("scenario outline", "scenario", "example", "структура сценария", "сценарий", "пример")
KEYWORD = re.compile(r"^(" + "|".join(sorted((re.escape(w) for w in FEATURE_WORDS + SCENARIO_WORDS),
                                              key=len, reverse=True)) + r")\s*:\s*(.*)$", re.I)
RY_TAG = re.compile(r"^@[A-Za-z]{2}(?:\.[A-Za-z0-9]+)+-\d")      # @RU.PRJ.US-1.1.1


def parse(text: str) -> dict:
    """Gherkin → {features: [{line, name, tags}], scenarios: [{line, name, tags, feature, outline}]}.

    Теги относятся к ключевому слову сразу под ними; любая другая строка (шаг, таблица, строка документа) их
    сбрасывает. Комментарии и пустые строки между тегами и ключевым словом допустимы."""
    features, scenarios, pending = [], [], []
    # Строки делятся только по «\n» (после «\r\n»): `splitlines` рвёт ещё по U+2028, \x0b, \x0c, \x85, и номера строк
    # здесь и в `assign` разошлись бы — тег встал бы не над своим сценарием.
    for n, raw in enumerate(text.lstrip("\ufeff").replace("\r\n", "\n").split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            pending += [tok for tok in re.split(r"\s+", line.split(" #")[0]) if tok.startswith("@")]
            continue
        m = KEYWORD.match(line)
        if m and m.group(1).lower() in FEATURE_WORDS:
            features.append({"line": n, "name": m.group(2).strip(), "tags": pending})
        elif m:
            scenarios.append({"line": n, "name": m.group(2).strip(), "tags": pending, "feature": len(features) - 1,
                              "outline": m.group(1).lower() in ("scenario outline", "структура сценария")})
        pending = []
    return {"features": features, "scenarios": scenarios}


def tag_values(tags: list, prefix: str) -> list:
    return [t[len(prefix):] for t in tags if t.startswith(prefix) and len(t) > len(prefix)]


def feature_files(root: str, paths) -> list:
    """Файлы `.feature`: переданные (файлы и папки) или `Artifacts/tests`; относительно корня, по порядку."""
    out = []
    for given in (paths or [FEATURES]):
        inside = real_inside(root, given)                     # `..`, абсолютные пути и ссылки наружу не выводят за проект
        if not inside:
            continue
        base = os.path.join(root, given)
        if os.path.isfile(inside) and inside.endswith(".feature") \
                and not read_verdict(base, root, strict=True, every_file=True):
            out.append(base)
        for dirpath, dirs, files in (safe_walk(base) if os.path.isdir(inside) else []):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            out += [os.path.join(dirpath, f) for f in sorted(files) if f.endswith(".feature")]
    return sorted(dict.fromkeys(out))


def known_refs(root: str) -> set:
    """Ключи Requirement Yogi, о которых проект знает: `ref_key` артефактов и `ry_defines` страниц зеркала."""
    keys = set()
    for sub, field in (("Artifacts", "ref_key"), (os.path.join("Sources", "Confluence"), "ry_defines")):
        base = os.path.join(root, sub)
        for dirpath, dirs, files in (safe_walk(base) if os.path.isdir(base) else []):
            dirs.sort()
            for f in sorted(files):
                if not f.endswith(".md"):
                    continue
                value = frontmatter(head_text(os.path.join(dirpath, f))).get(field) or ""     # шапка с сотнями ключей — целиком
                keys.update(k.strip().strip("\"'") for k in value.strip("[] ").split(",") if k.strip())
    return keys


def analyse(root: str = ".", paths=None) -> dict:
    """Разбор всех файлов и замечания: {files: [{path, features, scenarios, findings}], findings: [...], ids: {id: [где]}}."""
    known = known_refs(root)
    seen: dict = {}
    files, findings = [], []
    for path in feature_files(root, paths):
        rel = os.path.relpath(path, root).replace("\\", "/")
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                parsed = parse(f.read())
        except OSError:
            continue
        mine = []

        def add(code: str, line: int, msg: str, _into=mine, _rel=rel) -> None:
            _into.append({"id": code, "path": _rel, "line": line, "msg": msg})

        for ft in parsed["features"]:
            if not any(RY_TAG.match(t) for t in ft["tags"]):
                add("SC-07", ft["line"], f"над Feature «{ft['name']}» нет ключа Requirement Yogi истории (тег вида @RU.PRJ.US-1.1.1)")
        for sc in parsed["scenarios"]:
            ids = tag_values(sc["tags"], "@id:")
            where = f"«{sc['name']}»"
            if not ids:
                add("SC-01", sc["line"], f"у сценария {where} нет тега @id (`--assign --apply` проставит)")
            if len(ids) > 1:
                add("SC-02", sc["line"], f"у сценария {where} несколько @id: {', '.join(ids)}")
            for i in ids[:1]:
                seen.setdefault(i, []).append(f"{rel}:{sc['line']}")
            covers = tag_values(sc["tags"], "@covers:")
            if not covers:
                add("SC-05", sc["line"], f"сценарий {where} не связан с критерием (@covers:<ключ>)")
            for c in covers:
                if known and c not in known:
                    add("SC-06", sc["line"], f"@covers:{c} — такого ключа нет ни в артефактах, ни на страницах зеркала")
        files.append({"path": rel, "features": len(parsed["features"]), "scenarios": len(parsed["scenarios"]),
                      "findings": mine})
        findings += mine
    for ident, where in sorted(seen.items()):
        if len(where) > 1:
            path, line = where[1].rsplit(":", 1)
            findings.append({"id": "SC-03", "path": path, "line": int(line),
                             "msg": f"@id:{ident} носят {len(where)} сценария: {'; '.join(where)}"})
    findings.sort(key=lambda x: (x["path"], x["line"], x["id"]))
    return {"files": files, "findings": findings, "ids": {k: v for k, v in sorted(seen.items())}}


def assign(root: str, paths, apply: bool) -> tuple:
    """Проставить `@id:SC-NNNN` сценариям без идентификатора. → (сколько, [строки вывода]).

    Тег ложится в строку тегов прямо над сценарием, а если её нет — отдельной строкой с тем же отступом.
    Перевод строк файла (`\\n` или `\\r\\n`) сохраняется. Без `apply` номера не выдаются: показано, что будет."""
    import id_registry as IDR
    changed, report = 0, []
    for path in feature_files(root, paths):
        rel = os.path.relpath(path, root).replace("\\", "/")
        with open(path, encoding="utf-8", newline="") as f:
            raw = f.read()
        eol = "\r\n" if "\r\n" in raw else "\n"
        # Смешанные окончания: файл с «\r\n» и одиночными «\n» делился бы по-разному здесь и в `parse`.
        if eol == "\r\n" and raw.replace("\r\n", "").count("\n"):
            report.append(f"{os.path.relpath(path, root).replace(chr(92), '/')}: смешанные окончания строк — файл пропущен, приведите к одному виду")
            continue
        lines = raw.split(eol)
        parsed = parse(raw.replace("\r\n", "\n"))
        todo = [sc for sc in parsed["scenarios"] if not tag_values(sc["tags"], "@id:")]
        if not todo:
            continue
        # номера выдаются в порядке документа (верхний сценарий получает меньший), правка идёт снизу вверх:
        # так номера строк выше не плывут от вставленных
        todo.sort(key=lambda s: s["line"])
        got = IDR.allocate_many("SC", len(todo), root) if apply else ["SC-????"] * len(todo)      # один захват замка на файл
        idents = {sc["line"]: ident for sc, ident in zip(todo, got)}
        for sc in reversed(todo):
            ident = idents[sc["line"]]
            idx = sc["line"] - 1
            above = idx - 1
            while above >= 0 and (not lines[above].strip() or lines[above].strip().startswith("#")):
                above -= 1
            if above >= 0 and lines[above].strip().startswith("@") and sc["tags"]:
                tags_line = lines[above]
                cut = re.search(r"\s+#", tags_line)             # хвостовой комментарий строки тегов остаётся хвостом
                lines[above] = (tags_line[:cut.start()].rstrip() + f" @id:{ident}" + tags_line[cut.start():]) if cut \
                    else tags_line.rstrip() + f" @id:{ident}"
            else:
                lines.insert(idx, re.match(r"\s*", lines[idx]).group(0) + f"@id:{ident}")
            report.append(f"{rel}:{sc['line']}: «{sc['name']}» → @id:{ident}")
            changed += 1
        if apply:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(eol.join(lines))
    return changed, list(reversed(report))


def main() -> int:
    ap = argparse.ArgumentParser(description="Идентификаторы сценариев Gherkin и связи с критериями")
    ap.add_argument("paths", nargs="*", help="файлы .feature или папки; по умолчанию Artifacts/tests")
    ap.add_argument("--assign", action="store_true", help="проставить @id: сценариям без идентификатора")
    ap.add_argument("--apply", action="store_true", help="записать (иначе --assign только показывает)")
    ap.add_argument("--json", action="store_true", help="машинный вывод")
    a = ap.parse_args()
    if a.assign:
        try:
            n, lines = assign(".", a.paths, a.apply)
        except (OSError, RuntimeError) as e:             # замок выдачи номеров занят или отметка недоступна
            print(f"feature_ids: номера не выданы: {e}", file=sys.stderr)
            return 2
        print(("Проставлено" if a.apply else "Будет проставлено (предпросмотр, номера не выданы)") + f": {n}")
        print("\n".join(f"- {x}" for x in lines))
        if not a.apply and n:
            print("\nЗаписать — повторите с --apply.")
        return 0
    res = analyse(".", a.paths)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        total = sum(f["scenarios"] for f in res["files"])
        print(f"# Сценарии Gherkin — файлов {len(res['files'])}, сценариев {total}, с @id {len(res['ids'])}, "
              f"замечаний {len(res['findings'])}\n")
        for it in res["findings"]:
            print(f"- {it['path']}:{it['line']} {it['id']}: {it['msg']}")
        if not res["findings"]:
            print("Замечаний нет.")
    return 1 if res["findings"] else 0


if __name__ == "__main__":
    sys.exit(main())
