#!/usr/bin/env python3
"""sources_registry.py — какие модули источников установлены и что из них подключено.

Зеркала в `Sources/` наливают подключаемые модули (`connectors/<id>/`). Движок сам
не знает ни про Confluence, ни про Jira: он знает, что модуль объявил о себе в
`connector.json` — вид хранилища (wiki или board), папку зеркала, скрипт запуска,
имя команды и префикс переменных окружения для токена.

Кто спрашивает реестр:
  aurora_doctor.py  — чья папка в `Sources/` и не появилось ли ничьей;
  aurora_update.py  — какие папки зеркал завести в проекте;
  sync_audit.py     — что и по каким правилам проверять;
  панель            — что показать в «Зеркалах» и что предложить подключить.

Проект объявляет подключённое в `aurora.config.yaml`:

    sources:
      - id: Confluence          # он же имя папки в Sources/
        module: confluence-dc
        path: Sources/Confluence

Секцию разбирает один разбор — `declared_chunks`/`split_section`: записи с вложенными списками,
лишними ключами, комментариями и записи без `module:` возвращаются сырым текстом, на нём же
стоят писатели секции.

Секции нет вовсе — работают модули с `legacy_path_key`: ровно те два, что стояли в проектах
до появления реестра. Секция есть, но пуста (заголовок `sources:` без записей), — это «не подключено
ничего»: исторические зеркала из `paths:` в таком проекте не оживают. Настройки продукта живут там, где их ждёт модуль
(`settings_block` манифеста), а не дублируются в реестре: разбирать `sync_roots`
умеет только сам Confluence-модуль.

  python3 .aurora/scripts/sources_registry.py           # что установлено и что подключено
  python3 .aurora/scripts/sources_registry.py --json

Панель: `sync:sources`
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
    # и портят протокол MCP; движок говорит по-русски и пишет UTF-8 везде.
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

CONFIG = "aurora.config.yaml"
PROJECT_DIR = ".aurora/connectors"    # манифесты, скопированные в проект
KIT_DIR = "connectors"                  # манифесты в самом kit'е
KINDS = ("wiki", "board")
# Роль зеркала в доверии. `tasks` — трекер: статус задачи решает, доверять ли артефакту.
# `artifacts` — документы, доверие которых вычисляется по связям с задачами. `raw` —
# первоисточник (договор, ТЗ): правда по определению, доказывать нечего.
#
# Роль объявляется здесь, а не отдельным списком папок в конфиге: у зеркала уже есть
# описание, и второй источник правды о том же самом разошёлся бы с первым на второй
# неделе. Модуль без роли — `artifacts`: это безопасное умолчание, оно ничему не даёт
# доверия само по себе.
ROLES = ("tasks", "artifacts", "raw")
DEFAULT_ROLE = "artifacts"


def kit_root() -> str:
    """Корень kit'а: рядом со скриптом (в kit'е) или по метке, положенной установщиком."""
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(os.path.dirname(here), KIT_DIR)):
        return os.path.dirname(here)
    mark = os.path.join(os.path.dirname(here), "kit_path.txt")   # .aurora/kit_path.txt
    if os.path.isfile(mark):
        path = open(mark, encoding="utf-8").read().strip()
        if os.path.isdir(os.path.join(path, KIT_DIR)):
            return path
    return ""


def manifest_files(root: str = "") -> list:
    """Манифесты модулей: сначала свои, потом kit'овые (в проекте лежит копия)."""
    here = os.path.join(root or ".", PROJECT_DIR)
    if os.path.isdir(here):
        return sorted(os.path.join(here, f) for f in os.listdir(here) if f.endswith(".json"))
    kit = kit_root()
    if not kit:
        return []
    base = os.path.join(kit, KIT_DIR)
    return sorted(os.path.join(base, d, "connector.json") for d in sorted(os.listdir(base))
                  if os.path.isfile(os.path.join(base, d, "connector.json")))


# Сбои чтения манифестов за время жизни процесса: doctor и панель показывают их строкой,
# а не теряют вместе с пропущенным модулем. Одна и та же причина копится один раз —
# `installed()` зовут по нескольку раз за запуск.
_REPORT: list = []


def registry_report() -> list:
    """Строки о манифестах, которые реестр пропустил (для doctor и панели)."""
    return list(_REPORT)


def _note(line: str) -> None:
    if line not in _REPORT:
        _REPORT.append(line)
        print(f"sources_registry: {line}", file=sys.stderr)


def _common_fn(name: str):
    """Функция `name` из `aurora_common` — той, что лежит рядом с этим файлом.

    `update` может идти из копии старого выпуска в проекте: её `aurora_common` уже в памяти
    и загрузчика конверта не знает, а реестр берётся из нового кита. Тогда читаем
    `aurora_common.py` соседа по файлу, а не падаем на `AttributeError`.
    """
    fn = getattr(aurora_common, name, None)
    if fn:
        return fn
    import importlib.util
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aurora_common.py")
    spec = importlib.util.spec_from_file_location("aurora_common_envelope", here)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, name)


def _loader():
    """`load_manifest` общего загрузчика конверта (см. `_common_fn`)."""
    return _common_fn("load_manifest")


def safe_relpath(value) -> bool:
    """Годится ли путь зеркала: простой относительный путь внутри проекта (`ext_safe_relpath`)."""
    return _common_fn("ext_safe_relpath")(value)


def load(path: str) -> dict:
    """Манифест + проверка обязательного. Кривой модуль лучше назвать, чем молча пропустить.

    Чтение и проверка формы — общий загрузчик конверта (`aurora_common.load_manifest`):
    он не бросает исключений, поэтому один неполный `connector.json` не обрушивает
    `update` всех проектов. Любой отказ — `{}` и строка в `registry_report()`.
    """
    m, rep = _loader()(path, "connector")
    for line in rep:
        _note(line)
    if m is None:
        return {}
    role = m.get("role", DEFAULT_ROLE)
    if role not in ROLES:
        _note(f"{m['id']}: роль «{role}» движку неизвестна (есть: {', '.join(ROLES)}) — модуль пропущен")
        return {}
    if m["kind"] not in KINDS:
        _note(f"{m['id']}: вид «{m['kind']}» движку неизвестен "
              f"(есть {', '.join(KINDS)}) — модуль пропущен")
        return {}
    # Путь зеркала из манифеста уходит в `mkdir` обновления и в строку конфига без экранирования
    # (форма настроек): «../outside» создал бы папку вне проекта, перевод строки — чужие строки YAML.
    # Проверка стоит здесь, у единственного читателя проектных манифестов, а не в каждом потребителе.
    default_path = (m.get("mirror") or {}).get("default_path")
    if default_path and not safe_relpath(default_path):
        _note(f"{m['id']}: mirror.default_path не годится в путь внутри проекта "
              "(простые сегменты через «/», без «..», диска и управляющих знаков) — модуль пропущен")
        return {}
    m["_path"] = path
    return m


def installed(root: str = "") -> dict:
    """{id модуля: манифест} — что вообще доступно этому проекту."""
    out = {}
    for path in manifest_files(root):
        m = load(path)
        if m:
            out[m["id"]] = m
    return out


# Заголовок секции: `sources:` и ничего после него в строке. Пустой заголовок — тоже секция.
_HEADER = re.compile(r"^sources:[ \t\r]*(?:\n|\Z)", re.M)
_ITEM0 = re.compile(r"-(?:[ \t]|$)")        # пункт списка на нулевом отступе: `sources:` + `- id: …` без сдвига (стиль PyYAML)


def section_span(text: str):
    """Границы секции `sources:` → (начало заголовка, начало тела, конец тела) или None.

    Единственное место, где решается, где секция кончается: реестр, панель и форма настроек берут границы
    отсюда. Секцию продолжают строки с отступом и пункты списка на нулевом отступе (`- id: …`); пустые строки
    и комментарии на нулевом отступе внутри списка остаются в секции, если за ними идёт ещё что-то из секции.
    Секцию кончает первая строка нулевого отступа, не пункт списка и не комментарий. Пустые строки перед ней
    остаются в теле (как было всегда), а комментарии перед следующим ключом и всё после них — нет: они его.
    """
    m = _HEADER.search(text or "")
    if not m:
        return None
    pos = lead = m.end()     # lead — конец подтверждённой части секции и идущих сразу за ней пустых строк
    comment = False          # после последней строки секции уже был комментарий на нулевом отступе
    for line in re.findall(r"[^\n]*\n|[^\n]+", text[m.end():]):
        bare = line.rstrip("\r\n")
        pos += len(line)
        if not bare.strip():
            if not comment:
                lead = pos
        elif bare[0] in " \t" or _ITEM0.match(bare):
            lead, comment = pos, False
        elif bare.startswith("#"):
            comment = True                       # чей он, решит следующая строка: секции или следующего ключа
        else:
            break                                # новый верхний ключ
    return m.start(), m.end(), lead


_DASH = re.compile(r"^([ \t]*)-(?:[ \t]+|$)")
# Значение верхнего ключа записи: в кавычках или без, хвостовой `# комментарий` не значение.
_VALUE = r'"?([^"\n#]+?)"?[ \t\r]*(?:\s#.*)?$'


def has_section(text: str) -> bool:
    """Есть ли в конфиге заголовок `sources:` — в том числе пустой (без единой записи)."""
    return section_span(text) is not None


def _entry_key(raw: str, key: str):
    """Значение верхнего ключа `key` записи секции или None.

    Верхний уровень — отступ первого ключа после «- » (`  - id: X` → ключи на отступе 4):
    пункты вложенного списка (`exclude:` и под ним `- drafts`) и ключи глубже в него не входят.
    """
    lines = raw.split("\n")
    indent = len(_DASH.match(lines[0]).group(0))
    rows = [(indent, lines[0][indent:])]
    rest = [(len(ln) - len(ln.lstrip(" \t")), ln.lstrip(" \t")) for ln in lines[1:]]
    if not rows[0][1].strip():      # «-» одна в строке: ключи начинаются со следующей значимой строки
        for ind, body in rest:
            if body.strip() and not body.startswith("#"):
                indent = ind
                break
    for ind, body in rows + rest:
        hit = ind == indent and re.match(rf"{key}\s*:\s*{_VALUE}", body)
        if hit:
            return hit.group(1).strip()
    return None


def split_section(text: str) -> tuple:
    """Тело секции `sources:` → (голова, [(module, id, path, сырой текст записи)]).

    Единственный разбор секции: реестр, панель и форма настроек читают её только им.
    Новая запись начинается с «- » на отступе первой записи секции или левее; всё, что
    глубже (вложенный список `exclude:`, лишние ключи), комментарии и пустые строки внутри
    записи и после неё до следующей — остаются её сырым текстом. Голова — всё до первой записи
    (комментарии над ней). Склейка `голова + "".join(raw)` возвращает тело секции байт в байт.
    Запись без `module:` не выбрасывается: у неё module=None; id и path — None, если не заданы.
    Секции нет — ("", []); пустой заголовок — ("", []) тоже, их различает `has_section`.
    """
    span = section_span(text)
    if not span:
        return "", []
    head, chunks, first = [], [], None
    for line in re.findall(r"[^\n]*\n|[^\n]+", text[span[1]:span[2]]):
        dash = _DASH.match(line)
        if dash and (first is None or len(dash.group(1)) <= first):
            first = len(dash.group(1)) if first is None else first
            chunks.append([line])
        elif chunks:
            chunks[-1].append(line)
        else:
            head.append(line)
    out = []
    for lines in chunks:
        raw = "".join(lines)
        out.append((_entry_key(raw, "module"), _entry_key(raw, "id"), _entry_key(raw, "path"), raw))
    return "".join(head), out


def declared_chunks(text: str) -> list:
    """Записи секции `sources:` как [(module|None, id, path, сырой текст записи)] — см. `split_section`."""
    return split_section(text)[1]


def config_text(root: str = "") -> str:
    """Текст `aurora.config.yaml` проекта ("" — конфига нет)."""
    cfg = os.path.join(root or ".", CONFIG)
    return open(cfg, encoding="utf-8", errors="ignore").read() if os.path.isfile(cfg) else ""


def declared(root: str = "") -> list:
    """Секция `sources:` конфига → [{id, module, path}]. Порядок сохраняем.

    Записи без `module:` не выдаются (модуль не назван — реестру нечего подключать), но
    остаются в `declared_chunks`: писатели секции не вправе их терять.
    """
    out = []
    for module, eid, path, _raw in declared_chunks(config_text(root)):
        if module:
            out.append({k: v for k, v in (("id", eid), ("module", module), ("path", path)) if v})
    return out


def legacy(root: str = "", mods: dict = None) -> list:
    """Чем работал проект до реестра: модули с `legacy_path_key` и путями из `paths:`."""
    mods = installed(root) if mods is None else mods
    text = config_text(root)
    out = []
    for mid, m in sorted(mods.items()):
        key = (m.get("mirror") or {}).get("legacy_path_key")
        if not key:
            continue
        hit = re.search(rf'^\s*{re.escape(key)}\s*:\s*"?([^"\n#]+?)"?\s*$', text, re.M)
        path = (hit.group(1).strip() if hit else (m.get("mirror") or {}).get("default_path", "")).rstrip("/")
        if not path:    # у модуля нет ни пути в конфиге, ни `mirror.default_path` — зеркалу негде жить
            continue
        out.append({"id": os.path.basename(path), "module": mid, "path": path})
    return out


def instances(root: str = "") -> list:
    """Что подключено: объявленное в конфиге; секции `sources:` нет вовсе — исторические два зеркала.

    Секция есть, а записей с модулем в ней нет (пустой заголовок), — подключено ничего:
    легаси-возврат к `paths:` только когда секции нет совсем.

    Каждый элемент: id, module, path, kind, state, command, script, manifest.
    Модуль, которого нет среди установленных, не выбрасывается молча: он остаётся
    в списке с `manifest: None`, чтобы doctor мог сказать, чего не хватает.
    """
    mods = installed(root)
    items = declared(root) if has_section(config_text(root)) else legacy(root, mods)
    out = []
    for item in items:
        m = mods.get(item["module"])
        # Путь из `sources:`/`paths:` конфига пишет сам владелец проекта (`Sources/Wiki (old)`,
        # `../Shared/Confluence`): его не ограничиваем. Проверка пути стоит только на пути из
        # МАНИФЕСТА (`mirror.default_path`) — она в `load()`, до сюда плохой манифест не доходит.
        raw = item.get("path") or (m["mirror"].get("default_path", "") if m else "")
        path = raw.rstrip("/")
        out.append({
            "id": item.get("id") or os.path.basename(path),
            "module": item["module"],
            "path": path,
            "kind": m["kind"] if m else "",
            # Роль отдаём как есть: пусто — значит модуль её не объявил. Умолчание
            # подставляет тот, кто читает, и делает это осознанно; тихая подстановка
            # здесь превращает зеркало задач в зеркало артефактов.
            "role": (m.get("role", "") if m else ""),
            "state": m["mirror"].get("state", "") if m else "",
            "command": m["run"].get("command", "") if m else "",
            "script": m["run"].get("script", "") if m else "",
            "env_prefix": (m.get("auth") or {}).get("env_prefix", "") if m else "",
            "title": m.get("title", item["module"]) if m else item["module"],
            "manifest": m,
        })
    return out


def roles(root: str = "") -> dict:
    """{путь зеркала: роль} — кто здесь задачи, кто артефакты, кто первоисточник.

    Трассировка и извлечение спрашивают именно эту карту: она собрана из описаний
    коннекторов, поэтому новое зеркало получает своё место в модели доверия сразу, без
    правок в коде и без второго списка папок в конфиге проекта.
    """
    out = {}
    for it in instances(root):
        path = (it.get("path") or "").rstrip("/")
        # Роль отдаём только объявленную. Умолчание здесь молчаливо превратило бы зеркало
        # задач в зеркало артефактов — а на проекте со старым описанием коннектора это
        # значит «связей с задачами нет», то есть вся база разом становится недоверенной.
        if path and it.get("role"):
            out[path] = it["role"]
    return out


def mirror_paths(root: str = "") -> dict:
    """{папка зеркала: id модуля} — чем занята `Sources/` по мнению конфига."""
    return {i["path"]: i["module"] for i in instances(root) if i["path"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="Модули источников: что установлено и подключено")
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    a = ap.parse_args()

    mods, inst = installed(), instances()
    if a.json:
        print(json.dumps({"installed": list(mods.values()), "instances":
                          [{k: v for k, v in i.items() if k != "manifest"} for i in inst]},
                         ensure_ascii=False, indent=2))
        return 0

    print(f"Установлено модулей: {len(mods)}")
    for m in mods.values():
        print(f"  · {m['id']} — {m.get('title', '')} [{m['kind']} · "
              f"{m.get('role', DEFAULT_ROLE)}] → {m['mirror'].get('default_path', '—')}")
    print(f"\nПодключено в проекте: {len(inst)}"
          + ("" if has_section(config_text()) else "  (секции sources: нет — работают исторические зеркала)"))
    for i in inst:
        mark = "" if i["manifest"] else "  ← модуль не установлен"
        exists = "" if os.path.isdir(i["path"]) else "  ← папки нет"
        print(f"  · {i['id']}: {i['module']} → {i['path']}{mark}{exists}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
