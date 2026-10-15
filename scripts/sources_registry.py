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

Секции нет — работают модули с `legacy_path_key`: ровно те два, что стояли в проектах
до появления реестра. Настройки продукта живут там, где их ждёт модуль
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


def declared(root: str = "") -> list:
    """Секция `sources:` конфига → [{id, module, path}]. Порядок сохраняем."""
    cfg = os.path.join(root or ".", CONFIG)
    if not os.path.isfile(cfg):
        return []
    text = open(cfg, encoding="utf-8", errors="ignore").read()
    m = re.search(r"^sources:\s*$(.*?)(?=^\S|\Z)", text, re.M | re.S)
    if not m:
        return []
    out = []
    for chunk in re.split(r"^\s*-\s+", m.group(1), flags=re.M)[1:]:
        chunk = "  " + chunk
        item = {}
        for key in ("id", "module", "path"):
            hit = re.search(rf'^\s*{key}\s*:\s*"?([^"\n#]+?)"?\s*$', chunk, re.M)
            if hit:
                item[key] = hit.group(1).strip()
        if item.get("module"):
            out.append(item)
    return out


def legacy(root: str = "", mods: dict = None) -> list:
    """Чем работал проект до реестра: модули с `legacy_path_key` и путями из `paths:`."""
    mods = installed(root) if mods is None else mods
    cfg = os.path.join(root or ".", CONFIG)
    text = open(cfg, encoding="utf-8", errors="ignore").read() if os.path.isfile(cfg) else ""
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
    """Что подключено: объявленное в конфиге, иначе — исторические два зеркала.

    Каждый элемент: id, module, path, kind, state, command, script, manifest.
    Модуль, которого нет среди установленных, не выбрасывается молча: он остаётся
    в списке с `manifest: None`, чтобы doctor мог сказать, чего не хватает.
    """
    mods = installed(root)
    items = declared(root) or legacy(root, mods)
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
          + ("" if declared() else "  (секции sources: нет — работают исторические зеркала)"))
    for i in inst:
        mark = "" if i["manifest"] else "  ← модуль не установлен"
        exists = "" if os.path.isdir(i["path"]) else "  ← папки нет"
        print(f"  · {i['id']}: {i['module']} → {i['path']}{mark}{exists}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
