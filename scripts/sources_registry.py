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

  python3 .opencode/scripts/sources_registry.py           # что установлено и что подключено
  python3 .opencode/scripts/sources_registry.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

CONFIG = "aurora.config.yaml"
PROJECT_DIR = ".opencode/connectors"    # манифесты, скопированные в проект
KIT_DIR = "connectors"                  # манифесты в самом kit'е
KINDS = ("wiki", "board")


def kit_root() -> str:
    """Корень kit'а: рядом со скриптом (в kit'е) или по метке, положенной установщиком."""
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(os.path.dirname(here), KIT_DIR)):
        return os.path.dirname(here)
    mark = os.path.join(os.path.dirname(here), "kit_path.txt")   # .opencode/kit_path.txt
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


def load(path: str) -> dict:
    """Манифест + проверка обязательного. Кривой модуль лучше назвать, чем молча пропустить."""
    try:
        m = json.load(open(path, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"sources_registry: {path} не читается: {e}", file=sys.stderr)
        return {}
    for key in ("id", "kind", "mirror", "run"):
        if key not in m:
            print(f"sources_registry: в {path} нет поля {key} — модуль пропущен", file=sys.stderr)
            return {}
    if m["kind"] not in KINDS:
        print(f"sources_registry: {m['id']}: вид «{m['kind']}» движку неизвестен "
              f"(есть {', '.join(KINDS)}) — модуль пропущен", file=sys.stderr)
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
        key = m["mirror"].get("legacy_path_key")
        if not key:
            continue
        hit = re.search(rf'^\s*{key}\s*:\s*"?([^"\n#]+?)"?\s*$', text, re.M)
        path = (hit.group(1).strip() if hit else m["mirror"]["default_path"]).rstrip("/")
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
        path = (item.get("path") or (m["mirror"]["default_path"] if m else "")).rstrip("/")
        out.append({
            "id": item.get("id") or os.path.basename(path),
            "module": item["module"],
            "path": path,
            "kind": m["kind"] if m else "",
            "state": m["mirror"].get("state", "") if m else "",
            "command": m["run"].get("command", "") if m else "",
            "script": m["run"].get("script", "") if m else "",
            "env_prefix": (m.get("auth") or {}).get("env_prefix", "") if m else "",
            "title": m.get("title", item["module"]) if m else item["module"],
            "manifest": m,
        })
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
        print(f"  · {m['id']} — {m.get('title', '')} [{m['kind']}] → {m['mirror']['default_path']}")
    print(f"\nПодключено в проекте: {len(inst)}"
          + ("" if declared() else "  (секции sources: нет — работают исторические зеркала)"))
    for i in inst:
        mark = "" if i["manifest"] else "  ← модуль не установлен"
        exists = "" if os.path.isdir(i["path"]) else "  ← папки нет"
        print(f"  · {i['id']}: {i['module']} → {i['path']}{mark}{exists}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
