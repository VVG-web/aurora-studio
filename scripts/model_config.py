#!/usr/bin/env python3
"""model_config.py — настройка моделей Авроры: одна на кит, общая для всех проектов.

До 1.153.0 модели настраивались переменными `.env.aurora.local` — в ките и, поверх него, в
каждом проекте. Шлюз был номером (`AURORA_AGENT_BACKEND_2_*`), и на одном номере жили
сразу адрес, ключ, модель под каждую роль, модель векторов и модель распознавания. Проект
мог молча перекрыть любую из них, порядок запасных был один на все роли, а «добавить второй
запасной для критика» значило завести шлюзу номер и вписать ему модели всех ролей.

Теперь три понятия разведены:

- **провайдер** — подключение к API: имя, тип, адрес, ключ, сколько запросов держит;
- **возможность** — LLM, OCR, эмбеддинги; в каждой — **роли** (использование: разбор,
  критик, распознавание документов, индекс базы);
- **бэкенд** — «провайдер + модель» в роли. Бэкенды роли упорядочены: первый основной,
  остальные запасные по порядку; каждый можно выключить, не удаляя.

Хранится в `<кит>/local/models.json` — рядом с `local/mcp.json`, тем же правилом: ключи внутри
файла, наружу только маска. Проектной настройки моделей нет: все проекты машины работают
одной. Файла нет — он собирается один раз из `.env.aurora.local` кита (прежняя настройка);
сами `.env`-файлы не правятся никогда.

  python3 scripts/model_config.py --show       # что настроено (ключи маской)
  python3 scripts/model_config.py --migrate    # собрать из .env кита, если файла ещё нет
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

VERSION = 1
CAPABILITIES = ("llm", "ocr", "embeddings")
MODELS_FILE = ("local", "models.json")
MASK = "••••••"
PROVIDER_TYPES = ("openai", "llama.cpp", "vllm", "sglang", "ollama", "tei")

# Роли, которые зовёт сам движок. Их нельзя удалить (иначе задача останется без модели), но
# можно переименовать на свой лад. Свои роли человек добавляет рядом — по мере нужды.
ENGINE_ROLES = {
    "llm": (("worker", "Разбор и тезисы"), ("planner", "Планировщик и вынос"),
            ("critic", "Критик (Момус)"), ("qa", "Ответы на вопросы")),
    "ocr": (("document", "Сканы документов"),),
    "embeddings": (("index", "Индекс базы знаний"),),
}
# Роль, которой отвечает возможность, когда задача зовёт роль без бэкендов.
DEFAULT_ROLE = {"llm": "worker", "ocr": "document", "embeddings": "index"}
SETTINGS_DEFAULT = {"adapter": "pydantic_ai", "max_steps": 15, "budget_min": 20,
                    "request_timeout": 300, "parallel": "auto", "debug": False}
OCR_DEFAULT = {"dpi": 130, "max_pages": 60}


def models_path(kit) -> Path:
    return Path(kit).joinpath(*MODELS_FILE)


def slug(text: str, taken=()) -> str:
    """Короткое имя для ссылок: латиница, цифры и дефис; занятое — с номером."""
    base = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "provider"
    out, i = base, 2
    while out in taken:
        out, i = f"{base}-{i}", i + 1
    return out


def empty() -> dict:
    """Настройка без провайдеров: роли движка на месте и ждут бэкендов."""
    caps = {c: {"roles": [{"id": rid, "name": name, "builtin": True, "backends": [],
                           **({"thinking": True} if c == "llm" else {})}
                          for rid, name in ENGINE_ROLES[c]]}
            for c in CAPABILITIES}
    caps["ocr"].update(OCR_DEFAULT)
    return {"version": VERSION, "providers": [], "capabilities": caps,
            "settings": dict(SETTINGS_DEFAULT)}


# ------------------------------------------------------------------ проверка

def _int(v, default: int = 0, low: int = 0) -> int:
    try:
        return max(low, int(v))
    except (TypeError, ValueError):
        return default


def normalize(data) -> tuple:
    """Привести настройку к схеме. → (настройка, [замечания]).

    Замечания — то, что пришлось поправить или выбросить: бэкенд на несуществующего
    провайдера, роль без имени. Настройку они не роняют — панель их показывает.
    """
    notes = []
    data = data if isinstance(data, dict) else {}
    out = empty()
    ids = set()
    for p in data.get("providers") or []:
        if not isinstance(p, dict) or not str(p.get("url") or "").strip():
            notes.append("провайдер без адреса пропущен")
            continue
        pid = str(p.get("id") or "").strip() or slug(p.get("name") or p.get("url"), ids)
        if pid in ids:
            pid = slug(pid, ids)
        ids.add(pid)
        template = p.get("template") if isinstance(p.get("template"), dict) else {}
        out["providers"].append({
            "id": pid,
            "name": " ".join(str(p.get("name") or pid).split())[:80],
            "type": p.get("type") if p.get("type") in PROVIDER_TYPES else "openai",
            "url": str(p["url"]).strip().rstrip("/"),
            "key": str(p.get("key") or ""),
            # Сколько запросов держит сервер: корпоративный — десяток, домашняя llama.cpp —
            # один. 0 — не объявлено, тогда провайдер делит общий потолок прогона.
            "width": _int(p.get("width"), 0),
            # Берётся ли провайдер в параллельный прогон ради пропускной способности.
            "parallel": p.get("parallel", True) is not False,
            # Свои поля chat-шаблона (как extraBody у opencode): {"reasoning_effort": "xhigh"}.
            "template": {k: v for k, v in template.items() if k != "enable_thinking"},
        })
    caps_in = data.get("capabilities") if isinstance(data.get("capabilities"), dict) else {}
    for cap in CAPABILITIES:
        src = caps_in.get(cap) if isinstance(caps_in.get(cap), dict) else {}
        roles, seen = [], set()
        for r in src.get("roles") or []:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "").strip() or slug(r.get("name"), seen)
            if rid in seen:
                continue
            seen.add(rid)
            backends = []
            for b in r.get("backends") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("provider") not in ids:
                    notes.append(f"{cap}/{rid}: бэкенд на неизвестного провайдера "
                                 f"«{b.get('provider')}» пропущен")
                    continue
                if not str(b.get("model") or "").strip():
                    notes.append(f"{cap}/{rid}: бэкенд без модели пропущен")
                    continue
                backends.append({"provider": b["provider"],
                                 "model": str(b["model"]).strip(),
                                 "enabled": b.get("enabled", True) is not False,
                                 # Окно модели НА ЭТОМ провайдере: одна модель у разных
                                 # провайдеров порезана по-разному. 0 — не объявлено.
                                 "context": _int(b.get("context"), 0),
                                 # Свой адрес сервиса (TEI векторов, отдельный порт OCR).
                                 "url": str(b.get("url") or "").strip().rstrip("/")})
            role = {"id": rid, "name": " ".join(str(r.get("name") or rid).split())[:80],
                    "builtin": rid in dict(ENGINE_ROLES[cap]), "backends": backends}
            if cap == "llm":
                role["thinking"] = r.get("thinking", True) is not False
            roles.append(role)
        # Роли движка есть всегда: удалённую возвращаем пустой — задача без роли упала бы.
        for rid, name in ENGINE_ROLES[cap]:
            if rid not in seen:
                roles.insert(len([x for x in roles if x["builtin"]]),
                             {"id": rid, "name": name, "builtin": True, "backends": [],
                              **({"thinking": True} if cap == "llm" else {})})
        out["capabilities"][cap] = {"roles": roles}
        if cap == "ocr":
            out["capabilities"]["ocr"]["dpi"] = max(72, _int(src.get("dpi"), 130))
            out["capabilities"]["ocr"]["max_pages"] = max(1, _int(src.get("max_pages"), 60))
    st = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    s = dict(SETTINGS_DEFAULT)
    s["adapter"] = st.get("adapter") if st.get("adapter") in ("pydantic_ai", "openai_compat") \
        else "pydantic_ai"
    for k in ("max_steps", "budget_min", "request_timeout"):
        s[k] = _int(st.get(k), SETTINGS_DEFAULT[k], 1)
    par = str(st.get("parallel", "auto")).strip().lower()
    s["parallel"] = "auto" if par in ("auto", "авто", "0", "", "all") else _int(par, 1, 1)
    s["debug"] = bool(st.get("debug"))
    out["settings"] = s
    return out, notes


# ------------------------------------------------------------- файл и ключи

def load(kit, migrate_env=None) -> dict:
    """Настройка кита. Файла нет — собрать из прежнего `.env` (`migrate_env`) и записать.

    Битый файл не переписывается: человек мог править его руками, и затереть правку из-за
    одной запятой — хуже, чем работать без моделей и сказать об этом.
    """
    path = models_path(kit)
    from aurora_common import personal_kit_file
    if personal_kit_file(path):
        return empty()          # прогон тестов: личная настройка машины в него не попадает
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            out = empty()
            out["error"] = f"{path}: не разобран ({e}) — исправьте файл или удалите его"
            return out
        return normalize(data)[0]
    data = from_env(migrate_env or {}) if migrate_env else empty()
    if data["providers"]:
        save(kit, data)
    return data


def save(kit, data: dict) -> dict:
    """Записать неделимо и закрыть правами 600: внутри ключи."""
    clean, notes = normalize(data)
    path = models_path(kit)
    from aurora_common import personal_kit_file
    if personal_kit_file(path):
        return {"ok": False, "error": "прогон тестов не пишет личную настройку машины"}
    # Копия движка без kit_path.txt считает китом сам проект. Писать туда нельзя: `local/`
    # проекта не закрыт .gitignore, и ключи провайдеров ушли бы в его git. Такая копия
    # работает настройкой из памяти, собранной из .env, а файл живёт только в ките.
    root = Path(kit)
    if (root / "aurora.config.yaml").is_file() and not (root / "scripts" / "model_config.py").is_file():
        return {"ok": False, "error": f"{root} — папка проекта, а не кита: настройка моделей "
                                      "пишется только в кит (нет .aurora/kit_path.txt?)"}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return {"ok": True, "notes": notes, "path": str(path)}


def masked(data: dict) -> dict:
    """Наружу — без ключей: «заполнен» маской, пусто — пусто."""
    out = json.loads(json.dumps(data))
    for p in out.get("providers") or []:
        p["key"] = MASK if p.get("key") else ""
    return out


def unmask(new: dict, old: dict) -> dict:
    """Маска в пришедшей настройке — «ключ не трогали»: подставить прежний."""
    keys = {p.get("id"): p.get("key", "") for p in (old.get("providers") or [])}
    out = json.loads(json.dumps(new if isinstance(new, dict) else {}))
    for p in out.get("providers") or []:
        if p.get("key") == MASK:
            p["key"] = keys.get(p.get("id"), "")
    return out


# --------------------------------------------------------- перенос из .env

def from_env(env: dict) -> dict:
    """Прежняя настройка (`AURORA_AGENT_BACKEND_<n>_*`, `AURORA_EMBED_*`, `AURORA_OCR_*`) →
    новая. Шлюз становится провайдером, его модели — бэкендами ролей; порядок запасных —
    прежний порядок номеров, флаг FALLBACK=0 — выключенный запасной."""
    import agent_core as AG
    cfg = AG.parse_config(env)
    out = empty()
    pid = {}
    for b in cfg["backends"]:
        p = {"id": f"gw{b['n']}", "name": f"Шлюз №{b['n']}", "type": "openai",
             "url": b["url"], "key": b.get("key", ""), "width": b.get("width", 0),
             "parallel": b.get("parallel", True), "template": b.get("template") or {}}
        pid[b["n"]] = p["id"]
        out["providers"].append(p)
    roles = {c: {r["id"]: r for r in out["capabilities"][c]["roles"]} for c in CAPABILITIES}
    for r in AG.ROLES:
        roles["llm"][r]["thinking"] = AG.role_thinks(cfg, r)
        for b in cfg["backends"]:
            model = AG.role_model(b, r)
            if b.get("chat", True) and model:
                roles["llm"][r]["backends"].append(
                    {"provider": pid[b["n"]], "model": model, "context": b.get("context", 0),
                     "enabled": b["n"] == cfg["backends"][0]["n"] or b.get("fallback", True),
                     "url": ""})
    e = cfg.get("embed") or {}
    if e.get("url"):
        p = {"id": "embed", "name": "Сервис векторов", "type": "tei", "url": e["url"],
             "key": e.get("key", ""), "width": 0, "parallel": True, "template": {}}
        out["providers"].append(p)
        roles["embeddings"]["index"]["backends"].append(
            {"provider": "embed", "model": e.get("model") or "bge-m3", "enabled": True,
             "context": 0, "url": ""})
    for b in cfg["backends"]:
        if b.get("embed_model"):
            roles["embeddings"]["index"]["backends"].append(
                {"provider": pid[b["n"]], "model": b["embed_model"],
                 "enabled": bool(e.get("fallback")) or not roles["embeddings"]["index"]["backends"],
                 "context": 0, "url": b.get("embed_url") if b.get("embed_url") != b["url"] else ""})
    o = cfg.get("ocr") or {}
    if o.get("model"):
        if o.get("url"):
            out["providers"].append({"id": "ocr", "name": "Сервис распознавания",
                                     "type": "openai", "url": o["url"], "key": o.get("key", ""),
                                     "width": 0, "parallel": True, "template": {}})
            roles["ocr"]["document"]["backends"].append(
                {"provider": "ocr", "model": o["model"], "enabled": True, "context": 0, "url": ""})
        for b in cfg["backends"]:
            if (b.get("ocr_model") or "").strip().lower() == o["model"].strip().lower():
                roles["ocr"]["document"]["backends"].append(
                    {"provider": pid[b["n"]], "model": b["ocr_model"],
                     "enabled": bool(o.get("fallback")) or not roles["ocr"]["document"]["backends"],
                     "context": 0, "url": b.get("ocr_url") if b.get("ocr_url") != b["url"] else ""})
        out["capabilities"]["ocr"]["dpi"] = o.get("dpi", 130)
        out["capabilities"]["ocr"]["max_pages"] = o.get("max_pages", 60)
    out["settings"] = {"adapter": cfg.get("adapter", "pydantic_ai"),
                       "max_steps": cfg["max_steps"], "budget_min": cfg["budget_min"],
                       "request_timeout": cfg["request_timeout"],
                       "parallel": "auto" if cfg.get("parallel") == AG.AUTO
                       else cfg.get("parallel", 1),
                       "debug": cfg.get("debug", False)}
    return normalize(out)[0]


# ---------------------------------------------------- во внутренний вид движка

def chain(data: dict, cap: str, role: str, enabled_only: bool = True) -> list:
    """Бэкенды роли по порядку; роли нет или она пуста — роль движка этой возможности."""
    roles = {r["id"]: r for r in data["capabilities"][cap]["roles"]}
    r = roles.get(role)
    if not r or not [b for b in r["backends"] if b["enabled"] or not enabled_only]:
        r = roles.get(DEFAULT_ROLE[cap]) or {"backends": []}
    return [b for b in r["backends"] if b["enabled"] or not enabled_only]


def to_config(data: dict) -> dict:
    """Настройка кита → конфигурация, которой живёт движок (`agent_core`).

    Внутри движка провайдер — «шлюз» с номером: по номеру держатся карантин, ширина и
    слоты параллельного прогона. Номер — место провайдера в списке. Роль получает своё
    кольцо — `chains`: бэкенды роли по порядку, каждый со своей моделью и окном.
    """
    provs = data["providers"]
    num = {p["id"]: i for i, p in enumerate(provs, 1)}

    def elem(cap: str, b: dict) -> dict:
        p = provs[num[b["provider"]] - 1]
        return {"n": num[b["provider"]], "provider": p["id"], "name": p["name"],
                "url": b.get("url") or p["url"], "key": p["key"], "model": b["model"],
                "models": {}, "context": b.get("context", 0), "width": p["width"],
                "parallel": p["parallel"], "fallback": True, "chat": cap == "llm",
                "template": p.get("template") or {}, "template_error": "",
                "embed_model": b["model"] if cap == "embeddings" else "",
                "embed_url": b.get("url") or p["url"],
                "ocr_model": b["model"] if cap == "ocr" else "",
                "ocr_url": b.get("url") or p["url"]}

    chains = {cap: {r["id"]: [elem(cap, b) for b in r["backends"] if b["enabled"]]
                    for r in data["capabilities"][cap]["roles"]}
              for cap in CAPABILITIES}
    # Провайдер глазами прежних мест движка (ширина, пул, показ): одна запись на провайдера,
    # с моделью под каждую роль движка — первой в её цепочке на этом провайдере.
    backends = []
    for p in provs:
        n = num[p["id"]]
        models = {}
        for rid, _name in ENGINE_ROLES["llm"]:
            hit = next((b for b in chain(data, "llm", rid) if b["provider"] == p["id"]), None)
            if hit:
                models[rid] = hit["model"]
        embed = next((b for b in chain(data, "embeddings", "index") if b["provider"] == p["id"]),
                     None)
        ocr = next((b for b in chain(data, "ocr", "document") if b["provider"] == p["id"]), None)
        llm_in = any(b["provider"] == p["id"] for r in data["capabilities"]["llm"]["roles"]
                     for b in r["backends"] if b["enabled"])
        ctx = max([b.get("context", 0) for r in data["capabilities"]["llm"]["roles"]
                   for b in r["backends"] if b["provider"] == p["id"] and b["enabled"]] or [0])
        backends.append({"n": n, "provider": p["id"], "name": p["name"], "url": p["url"],
                         "key": p["key"], "model": "", "models": models, "context": ctx,
                         "parallel": p["parallel"], "fallback": True, "width": p["width"],
                         "embed_model": embed["model"] if embed else "",
                         "embed_url": (embed.get("url") or p["url"]) if embed else p["url"],
                         "ocr_model": ocr["model"] if ocr else "",
                         "ocr_url": (ocr.get("url") or p["url"]) if ocr else p["url"],
                         "template": p.get("template") or {}, "template_error": "",
                         "chat": llm_in})
    first_e = (chain(data, "embeddings", "index") or [None])[0]
    first_o = (chain(data, "ocr", "document") or [None])[0]
    s = data["settings"]
    import agent_core as AG
    return {
        "source": "models",
        "chains": chains,
        "embed": {"url": "", "key": "", "model": first_e["model"] if first_e else "bge-m3",
                  "fallback": len(chain(data, "embeddings", "index")) > 1},
        "ocr": {"url": "", "key": "", "model": first_o["model"] if first_o else "",
                "dpi": data["capabilities"]["ocr"].get("dpi", 130),
                "max_pages": data["capabilities"]["ocr"].get("max_pages", 60),
                "fallback": len(chain(data, "ocr", "document")) > 1},
        "adapter": s["adapter"],
        "thinking": True,
        "thinking_roles": {r["id"]: ("1" if r.get("thinking", True) else "0")
                           for r in data["capabilities"]["llm"]["roles"]},
        "max_steps": s["max_steps"], "budget_min": s["budget_min"],
        "request_timeout": s["request_timeout"],
        "parallel": AG.parallel_cap(s["parallel"]),
        "debug": s["debug"],
        "backends": backends,
    }


# --------------------------------------------------------------- команды

def main(argv=None) -> int:
    import argparse
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import agent_core as AG
    ap = argparse.ArgumentParser(description="Настройка моделей кита")
    ap.add_argument("--show", action="store_true", help="что настроено (ключи маской)")
    ap.add_argument("--migrate", action="store_true",
                    help="собрать из .env.aurora.local кита, если файла ещё нет")
    a = ap.parse_args(argv)
    kit, _project = AG._roots()
    path = models_path(kit)
    if a.migrate and path.is_file():
        print(f"{path} уже есть — переносить нечего")
        return 0
    data = load(kit, AG.kit_env(kit))
    if data.get("error"):
        print(data["error"], file=sys.stderr)
        return 2
    print(f"# Модели кита — {path}\n")
    for p in data["providers"]:
        print(f"- провайдер {p['id']} «{p['name']}» · {p['type']} · {p['url']} · ключ "
              f"{'есть' if p['key'] else 'нет'} · ширина {p['width'] or '—'}")
    for cap in CAPABILITIES:
        print(f"\n## {cap}")
        for r in data["capabilities"][cap]["roles"]:
            chain_txt = " → ".join(f"{b['provider']}/{b['model']}"
                                   + ("" if b["enabled"] else " (выкл.)")
                                   for b in r["backends"]) or "— нет бэкендов"
            print(f"- {r['id']} «{r['name']}»: {chain_txt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
