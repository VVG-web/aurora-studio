#!/usr/bin/env python3
"""ext_footprint.py — метрика «файлов ядра на расширение» (BL-ARC-02, срез 1).

Зачем: заявленная модульность («новый коннектор, провайдер, раздел — без правки ядра»)
выполнена не везде, и разрыв рос незаметно. Метрика считает кодом по текущему дереву: сколько
файлов ядра (движок, панель, отчёты) знают существующие расширения каждой точки по имени.
Это эвристика (возможны и пропуски, и случайные совпадения общих слов вроде continue), а не
точный счёт; считаются файлы-объединения, а не число мест-ссылок: файл с одним упоминанием и файл
с двадцатью весят одинаково, а два расширения в одном файле дают один файл. Чем больше
ядро цитирует литералы одних расширений, тем вероятнее, что и следующему понадобится строка
в тех же файлах.

Как считается. Для каждой точки расширения (таблица PROBES) литералы берутся из самих
манифестов и реестров дерева: connectors/*/connector.json, cockpit/modules/*/module.json,
gitproviders/*/provider.json, scripts/make_kinds.py:KNOWN, scripts/model_config.py:PROVIDER_TYPES,
scripts/harnesses.json (пути config и detect, а не голый id: он совпадает с форматами и словами вроде
continue), cockpit/skins/*.css. Файл ядра, где литерал расширения встречается,
считается; собственные файлы расширения (его папка, его скрипт запуска, его реестровый файл)
не считаются. «Ядро» — aurora.py, текстовые файлы корня, scripts/, cockpit/ (без vendor,
modules, skins, каталогов строк), reports/. Тесты, документация и каталоги строк не ядро.

Планка (tests/ext_footprint_baseline.json) — храповик: падает только рост числа; снизилось —
проверка печатает подсказку «обнови планку» (`--write`), чтобы достигнутое не откатилось.

  python3 tests/ext_footprint.py           # таблица
  python3 tests/ext_footprint.py --json    # то же в JSON, с файлами
  python3 tests/ext_footprint.py --write   # записать текущие числа как планку

Модуль не часть кита (в engine_manifest не попадает); только стандартная библиотека.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KIT = HERE.parent
BASELINE = HERE / "ext_footprint_baseline.json"

# Суффиксы текстовых файлов, которые бывают кодом ядра.
CORE_SUFFIXES = {".py", ".js", ".html", ".css", ".txt", ".json", ".command", ".bat"}
# Корневые файлы ядра и каталоги, откуда берутся остальные.
CORE_ROOT_FILES = ("aurora.py", "commands.txt", "engine_manifest.txt", "structure_dirs.txt",
                   "moc_groups.txt")
CORE_DIRS = ("scripts", "cockpit", "reports")
# Части пути, по которым файл не ядро: чужие библиотеки, сами расширения, каталоги строк, кэши.
NON_CORE_PARTS = {"vendor", "modules", "skins", "i18n", "__pycache__"}


def _read(p: Path) -> str:
    """Читает файл как UTF-8, битые байты пропускает: метрика не падает на чужой кодировке."""
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _json(p: Path):
    """JSON-файл или None: битый манифест не должен обнулять метрику остальных."""
    try:
        return json.loads(_read(p))
    except ValueError:
        return None


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


def core_files(root: Path) -> list[str]:
    """Пути (as_posix, от корня) файлов ядра: их и сканирует метрика."""
    out: list[str] = []
    for name in CORE_ROOT_FILES:
        if (root / name).is_file():
            out.append(name)
    for d in CORE_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in CORE_SUFFIXES:
                continue
            rel = _rel(root, p)
            parts = rel.split("/")
            if NON_CORE_PARTS & set(parts[:-1]) or parts[-1].startswith("."):
                continue
            out.append(rel)
    return out


def _quoted(word: str) -> str:
    """Слово в кавычках («github», 'tei'): голое слово в тексте и комментариях не считаем."""
    return r"[\"']" + re.escape(word) + r"[\"']"


def _literal_assign(path: Path, name: str):
    """Значение литерала `name = ...` из .py без импорта (ast): метрика не исполняет код дерева."""
    try:
        tree = ast.parse(_read(path))
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name
                                                for t in node.targets):
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return None
    return None


def _manifest_dirs(root: Path, sub: str, fname: str) -> list[tuple[str, dict]]:
    base = root / sub
    out = []
    if base.is_dir():
        for p in sorted(base.glob("*/" + fname)):
            m = _json(p)
            if isinstance(m, dict):
                out.append((str(m.get("id") or p.parent.name), m))
    return out


# --- таблица проб: точка расширения -> (id расширения -> регулярки литералов, свои файлы) ---
# Каждая проба возвращает ({id: [регулярки]}, функция «это собственный файл расширения?»).

def _probe_connector(root: Path):
    lits: dict[str, list[str]] = {}
    own_scripts: dict[str, str] = {}
    for cid, m in _manifest_dirs(root, "connectors", "connector.json"):
        mirror = (m.get("mirror") or {}) if isinstance(m.get("mirror"), dict) else {}
        run = (m.get("run") or {}) if isinstance(m.get("run"), dict) else {}
        vals = [mirror.get("default_path"), run.get("command"), run.get("script")]
        lits[cid] = [re.escape(v) for v in vals if isinstance(v, str) and v]
        if isinstance(run.get("script"), str) and run["script"]:
            own_scripts[cid] = "scripts/" + run["script"]

    def own(ext: str, rel: str) -> bool:
        return rel.startswith("connectors/") or rel == own_scripts.get(ext)
    return lits, own


def _probe_module(root: Path):
    lits = {mid: [r"modules[/\\]+" + re.escape(mid) + r"\b", r"/api/" + re.escape(mid) + r"\b"]
            for mid, _m in _manifest_dirs(root, "cockpit/modules", "module.json")}
    return lits, lambda ext, rel: rel.startswith("cockpit/modules/")


def _probe_git_provider(root: Path):
    lits = {pid: [_quoted(pid), r"gitproviders[/\\]+" + re.escape(pid) + r"\b"]
            for pid, _m in _manifest_dirs(root, "gitproviders", "provider.json")}
    return lits, lambda ext, rel: rel.startswith("gitproviders/")


def _probe_llm_provider(root: Path):
    types = _literal_assign(root / "scripts" / "model_config.py", "PROVIDER_TYPES") or ()
    return {str(t): [_quoted(str(t))] for t in types}, lambda ext, rel: False


def _probe_artifact_kind(root: Path):
    known = _literal_assign(root / "scripts" / "make_kinds.py", "KNOWN") or {}
    return {str(k): [_quoted(str(k))] for k in known}, lambda ext, rel: False


def _harness_path_literals(h: dict) -> list[str]:
    """Регулярки по путям ассистента: config и detect без префикса-подстановки ({home}, {appdata},
    {vscode}). Голый id (`continue`, `vscode`) совпадает с названиями форматов и обычными словами,
    поэтому не берётся; путь без каталога и не начинающийся с точки (`mcp.json`, `Claude`) слишком
    общ и тоже пропускается: у такого ассистента литералов нет, и ядро его «по имени» не знает."""
    out: list[str] = []
    for raw in [h.get("config")] + list(h.get("detect") or []):
        if not isinstance(raw, str):
            continue
        tail = re.sub(r"^\{\w+\}[/\\]*", "", raw).replace("\\", "/")
        if tail and ("/" in tail or tail.startswith(".")):
            out.append(re.escape(tail).replace("/", r"[/\\]"))
    return out


def _probe_assistant(root: Path):
    d = _json(root / "scripts" / "harnesses.json")
    items = d.get("harnesses") if isinstance(d, dict) else None
    hs = [h for h in items if isinstance(h, dict) and h.get("id")] if isinstance(items, list) else []
    return ({str(h["id"]): _harness_path_literals(h) for h in hs},
            lambda ext, rel: rel == "scripts/harnesses.json")


def _probe_skin(root: Path):
    base = root / "cockpit" / "skins"
    ids = [p.stem for p in sorted(base.glob("*.css"))] if base.is_dir() else []
    return ({i: [_quoted(i), r"skins[/\\]+" + re.escape(i) + r"\b"] for i in ids},
            lambda ext, rel: rel.startswith("cockpit/skins/"))


# Точка -> (функция пробы, считать ли в храповике). Скин — справка: ядро не должно знать скинов,
# но число показываем, не ограничивая.
PROBES = {
    "connector": (_probe_connector, True),
    "panel_module": (_probe_module, True),
    "git_provider": (_probe_git_provider, True),
    "llm_provider": (_probe_llm_provider, True),
    "artifact_kind": (_probe_artifact_kind, True),
    "assistant": (_probe_assistant, True),
    "skin": (_probe_skin, False),
}


def footprint_detail(root: Path | str = KIT) -> dict[str, dict[str, list[str]]]:
    """{точка: {id расширения: [файлы ядра, где оно упомянуто]}}; ids без упоминаний — пустой список."""
    root = Path(root)
    files = core_files(root)
    texts = {rel: _read(root / rel) for rel in files}
    out: dict[str, dict[str, list[str]]] = {}
    for name, (probe, _ratchet) in PROBES.items():
        lits, own = probe(root)
        per: dict[str, list[str]] = {}
        for ext, pats in lits.items():
            rxs = [re.compile(p) for p in pats]
            per[ext] = [rel for rel in files
                        if not own(ext, rel) and any(rx.search(texts[rel]) for rx in rxs)]
        out[name] = per
    return out


def footprint(root: Path | str = KIT) -> dict[str, list[str]]:
    """{точка: [файлы ядра]} — объединение по всем существующим расширениям точки."""
    return {name: sorted({f for fs in per.values() for f in fs})
            for name, per in footprint_detail(root).items()}


def counts(root: Path | str = KIT) -> dict[str, int]:
    return {name: len(files) for name, files in footprint(root).items()}


def load_baseline() -> dict[str, int]:
    d = _json(BASELINE)
    c = d.get("counts") if isinstance(d, dict) else None
    return {str(k): int(v) for k, v in c.items()} if isinstance(c, dict) else {}


def ratcheted() -> list[str]:
    """Точки, на которые распространяется храповик (скин — справка)."""
    return [n for n, (_p, r) in PROBES.items() if r]


def ratchet_verdict(now: dict[str, int], base: dict[str, int]) -> tuple[list[str], list[str]]:
    """(проблемы, подсказки). Проблема — точка без планки или с числом выше планки: тест падает
    только на рост. Подсказка — число ниже планки: достигнутое надо закрепить `--write`, но красным
    это не бывает (снижение — то, к чему стремится метрика, а не поломка)."""
    problems: list[str] = []
    hints: list[str] = []
    for name in ratcheted():
        if name not in base:
            problems.append(f"{name}: нет в планке — python3 tests/ext_footprint.py --write")
        elif now.get(name, 0) > base[name]:
            problems.append(f"{name}: файлов ядра {now.get(name, 0)} > планки {base[name]}")
        elif now.get(name, 0) < base[name]:
            hints.append(f"{name}: файлов ядра {now.get(name, 0)} < планки {base[name]} — хорошо; "
                         f"обнови планку: python3 tests/ext_footprint.py --write")
    return problems, hints


def write_baseline(root: Path | str = KIT) -> dict[str, int]:
    c = counts(root)
    data = {
        "about": "Планка метрики «файлов ядра на расширение» (tests/ext_footprint.py). "
                 "Число не должно расти (рост — красный тест); уменьшили — обновите: python3 tests/ext_footprint.py --write",
        "counts": {n: c[n] for n in ratcheted()},
    }
    BASELINE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data["counts"]


def main(argv: list[str]) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    if "--write" in argv:
        c = write_baseline()
        print("Планка записана:", json.dumps(c, ensure_ascii=False))
        return 0
    fp = footprint()
    base = load_baseline()
    if "--json" in argv:
        print(json.dumps({"counts": {n: len(f) for n, f in fp.items()}, "baseline": base,
                          "files": fp}, ensure_ascii=False, indent=2))
        return 0
    for n, files in fp.items():
        mark = "" if n in base else "  (справка)"
        limit = f" / планка {base[n]}" if n in base else ""
        print(f"{n:<15} {len(files):>3}{limit}{mark}")
    for h in ratchet_verdict({n: len(f) for n, f in fp.items()}, base)[1]:
        print("подсказка:", h)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
