#!/usr/bin/env python3
"""agent_core.py — встроенный агент, основание: конфиг, цепочка моделей, ping.

Аналитик ведёт базу, не выходя из Авроры: рутинные LLM-шаги выполняет встроенный агент.
Этот скрипт — фаза 1: разобрать настройку, дойти до живой модели по цепочке бэкендов и
честно сказать, что работает. Агентский цикл (задачи, оракулы) строится поверх — фаза 2.

  python3 .opencode/scripts/agent_core.py --ping          # каждый бэкенд: жив, занят, пуст
  python3 .opencode/scripts/agent_core.py --show          # собранная конфигурация (ключи маской)
  python3 .opencode/scripts/agent_core.py --venv-status   # стоит ли Pydantic AI и какой версии
  python3 .opencode/scripts/agent_core.py --venv-install  # поставить/обновить в ~/.aurora/venv

Настройка — только `.env`-файлы, один механизм: глобальный в ките (`.env.aurora.local`),
проект переопределяет любую переменную в своём `.env.aurora.local`. Приоритет:
переменные окружения > проект > кит. Ключи и адреса в git не попадают.

Цепочка бэкендов — кольцо, не лестница: каждый вызов обходит список с верха, поэтому
восстановившийся корпоративный шлюз (починили VPN) подхватывается на следующем же
запросе. К следующему бэкенду ведут: нет соединения за 3 с, `/slots` показывает занятый
слот (llama.cpp честно ставит в очередь и не отдаёт ошибку — выяснено зондом), пустой
или невалидный ответ. Полный неудачный круг → пауза → новый круг, до дедлайна.

Повадки моделей, выученные зондами и обязательные для транспорта:
  • рассуждения приходят в разных полях: `reasoning` (qwen) и `reasoning_content`
    (deepseek) — читать оба;
  • thinking включается `chat_template_kwargs: {"enable_thinking": true}`; при малом
    `max_tokens` рассуждения съедают всё и `content=None` при `finish_reason=length`;
  • формы ошибок две: `{"error": {...}}` и `{"code": ..., "message": ...}`.

Панель: `agent:ping`
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

TODAY = date.today().isoformat()
ROLES = ("worker", "planner", "critic", "qa")
CONNECT_TIMEOUT = 3          # секунд на установку соединения: мёртвый бэкенд не держит кольцо
RING_PAUSE = 10              # пауза между полными кругами по цепочке
VENV = Path.home() / ".aurora" / "venv"
# Какой адаптер выбран и почему пришлось откатиться: заполняется при разборе конфига,
# читается отчётом прогона. Глобальное состояние здесь честнее, чем протаскивать флаг
# через каждый вызов транспорта.
ADAPTER: dict = {"name": "openai_compat", "fallback_why": ""}


# ------------------------------------------------------------------ конфигурация

def _roots() -> tuple:
    """(кит, проект|None) — откуда собирать .env.

    Скрипт живёт либо в ките (`scripts/`), либо в копии движка проекта
    (`.opencode/scripts/`); проектом считается текущая папка с `aurora.config.yaml`.
    """
    here = Path(__file__).resolve().parent
    root = here.parent
    if root.name == ".opencode":
        project = root.parent
        kit_ptr = root / "kit_path.txt"
        kit = Path(kit_ptr.read_text(encoding="utf-8").strip()) if kit_ptr.is_file() else project
        return kit, project
    cwd = Path.cwd()
    project = cwd if (cwd / "aurora.config.yaml").is_file() else None
    return root, project


def load_env(path: Path) -> dict:
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def raw_config() -> dict:
    """Слои настройки: кит < проект < окружение. Побеждает более близкий к запуску."""
    kit, project = _roots()
    merged = dict(load_env(kit / ".env.aurora.local"))
    if project is not None:
        merged.update(load_env(project / ".env.aurora.local"))
    merged.update({k: v for k, v in os.environ.items() if k.startswith("AURORA_AGENT_")})
    return merged


def parse_config(env: dict) -> dict:
    """env-словарь → конфигурация агента. Чистая функция: тесты кормят её напрямую."""
    backends = []
    n = 1
    while env.get(f"AURORA_AGENT_BACKEND_{n}_URL"):
        prefix = f"AURORA_AGENT_BACKEND_{n}_"
        models = {r: env.get(prefix + "MODEL_" + r.upper(), "") for r in ROLES}
        backends.append({
            "n": n,
            "url": env[prefix + "URL"].rstrip("/"),
            "key": env.get(prefix + "KEY", ""),
            "model": env.get(prefix + "MODEL", ""),
            "models": models,
        })
        n += 1
    ADAPTER["name"] = env.get("AURORA_AGENT_ADAPTER", "pydantic_ai")
    ADAPTER["fallback_why"] = ""
    return {
        "adapter": ADAPTER["name"],
        "thinking": env.get("AURORA_AGENT_THINKING", "1") not in ("0", "false", "no"),
        "max_steps": int(env.get("AURORA_AGENT_MAX_STEPS", "15") or 15),
        "budget_min": int(env.get("AURORA_AGENT_BUDGET_MIN", "20") or 20),
        "request_timeout": int(env.get("AURORA_AGENT_REQUEST_TIMEOUT", "300") or 300),
        "debug": env.get("AURORA_AGENT_DEBUG", "0") in ("1", "true", "yes"),
        "backends": backends,
    }


def role_model(backend: dict, role: str) -> str:
    """Модель под роль; нет ролевой — общая. Дома одна модель на всё, это законно."""
    return backend["models"].get(role) or backend["model"]


# ------------------------------------------------------------------ белый список

# Агент пишет в проект ТОЛЬКО через команды движка — у них dry-run, git-guard и журнал.
# Списки — данные, а не код: их читает и раннер фазы 2, и тесты, и человек.
ALLOWED_WRITES = {
    "build_plan.py": {"--card", "--done", "--empty"},
    "kb_fix.py": {"--stubs", "--set-alias"},
    "kb_graph.py": {"--cards"},
}
# Запрещено навсегда, при любых флагах: доверие присваивает человек, снос и поставка —
# тоже человек. Это не настройка, а конструкция.
FORBIDDEN = ("kb_verify.py", "kb_reset.py", "ship_doc.py", "publish_doc.py", "git")


def write_allowed(script: str, args: list) -> tuple:
    """→ (можно ли, причина). Чтение свободно; запись — по белому списку."""
    name = os.path.basename(script)
    if name in FORBIDDEN or name.startswith("git"):
        return False, f"{name} запрещён агенту всегда: это решение человека"
    if "--apply" not in args and not (name == "build_plan.py"
                                      and any(a in ("--done", "--card", "--empty") for a in args)):
        return True, "чтение"
    modes = ALLOWED_WRITES.get(name)
    if not modes:
        return False, f"{name} не входит в белый список записи"
    if not any(a in modes for a in args):
        return False, f"у {name} агенту разрешены только режимы: {', '.join(sorted(modes))}"
    return True, "белый список"


# ------------------------------------------------------------------ транспорт

def http_json(url: str, payload: dict | None, key: str, timeout: float) -> tuple:
    """→ (status|None, body|None, ошибка-строкой, секунд)."""
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers,
                                 data=json.dumps(payload).encode() if payload else None)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read()), "", time.time() - t0
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:  # noqa: BLE001
            body = None
        msg = ""
        if isinstance(body, dict):
            msg = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) \
                else body.get("message") or ""
        return e.code, body, msg or f"HTTP {e.code}", time.time() - t0
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}", time.time() - t0


def adapter_process():
    """Долгоживущий процесс адаптера: старт venv с импортом фреймворка стоит секунд восемь.

    Платить их на каждом шаге агента значило бы минуты пустого ожидания за прогон, поэтому
    процесс поднимается один раз и обслуживает все вызовы построчно.
    """
    proc = ADAPTER.get("proc")
    if proc is not None and proc.poll() is None:
        return proc
    vpy = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    adapter = Path(__file__).resolve().parent / "agents" / "pydantic_ai_adapter.py"
    if not vpy.is_file() or not adapter.is_file():
        return None
    proc = subprocess.Popen([str(vpy), str(adapter)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, bufsize=1)
    ADAPTER["proc"] = proc
    return proc


def pydantic_transport(backend: dict, payload: dict, timeout: float) -> tuple:
    """Тот же контракт, что у прямого вызова, но через Pydantic AI в отдельном venv.

    Подпроцессом, а не импортом: зависимости фреймворка живут в `~/.aurora/venv` и в
    питон движка не попадают. Сломался venv — вызывающий откатится на stdlib-транспорт,
    и работа не встанет.
    """
    proc = adapter_process()
    if proc is None:
        return None, None, "venv с pydantic-ai не установлен", 0.0
    task = {"url": backend["url"], "key": backend["key"], "model": payload["model"],
            "messages": payload["messages"], "timeout": timeout,
            "thinking": (payload.get("chat_template_kwargs") or {}).get("enable_thinking", True)}
    t0 = time.time()
    try:
        proc.stdin.write(json.dumps(task, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            ADAPTER["proc"] = None      # процесс умер — следующий вызов поднимет заново
            return None, None, "адаптер закрылся", time.time() - t0
        out = json.loads(line)
    except Exception as e:  # noqa: BLE001
        ADAPTER["proc"] = None
        return None, None, f"адаптер не ответил: {type(e).__name__}", time.time() - t0
    if not out.get("ok"):
        return None, None, out.get("error", "адаптер вернул ошибку"), time.time() - t0
    body = {"choices": [{"message": {"content": out["text"],
                                     "reasoning": out.get("reasoning", "")},
                         "finish_reason": "stop"}]}
    return 200, body, "", time.time() - t0


def default_transport(kind: str, backend: dict, payload: dict | None, timeout: float) -> tuple:
    """kind: 'slots' | 'chat'. Отделён от логики кольца, чтобы тесты подменяли его целиком."""
    if kind == "slots":
        root = backend["url"].rsplit("/v1", 1)[0]
        return http_json(root + "/slots", None, backend["key"], CONNECT_TIMEOUT)
    if ADAPTER.get("name") == "pydantic_ai":
        st, body, err, dt = pydantic_transport(backend, payload, timeout)
        if st == 200:
            return st, body, err, dt
        # Фолбэк не молчаливый: причина уходит в журнал шага, и в отчёте видно, что
        # работали не тем адаптером, который выбран в конфиге.
        ADAPTER["fallback_why"] = err
    return http_json(backend["url"] + "/chat/completions", payload, backend["key"], timeout)


def answer_of(body: dict) -> tuple:
    """→ (текст, рассуждения). Поля рассуждений у бэкендов называются по-разному."""
    msg = (body.get("choices") or [{}])[0].get("message") or {}
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    return content, reasoning


def busy(backend: dict, transport) -> bool:
    """llama.cpp не отказывает, а молча ставит в очередь — занятость видна только в /slots.
    Шлюз на /slots отвечает ошибкой: это не занятость, а «проверка неприменима»."""
    st, body, _err, _dt = transport("slots", backend, None, CONNECT_TIMEOUT)
    if st != 200 or not isinstance(body, list):
        return False
    return any(s.get("is_processing") for s in body if isinstance(s, dict))


def call_role(cfg: dict, role: str, messages: list, transport=None,
              deadline: float | None = None, sleep=time.sleep,
              thinking: bool | None = None, max_tokens: int | None = None) -> dict:
    """Один вызов модели через кольцо бэкендов.

    → {ok, text, reasoning, backend, model, seconds, waited, log[]} либо {ok: False, log}.
    Кольцо: каждый круг начинается с первого бэкенда — восстановившийся корпоративный
    подхватывается сразу, у него слоты почти не ограничены.
    """
    transport = transport or default_transport
    think = cfg["thinking"] if thinking is None else thinking
    deadline = deadline or (time.time() + cfg["request_timeout"])
    log, waited, ring = [], 0.0, 0

    if not cfg["backends"]:
        return {"ok": False, "log": ["бэкенды не настроены: нет AURORA_AGENT_BACKEND_1_URL"]}

    while time.time() < deadline:
        ring += 1
        for b in cfg["backends"]:
            model = role_model(b, role)
            if not model:
                log.append(f"№{b['n']}: нет модели для роли {role} — пропущен")
                continue
            if busy(b, transport):
                log.append(f"№{b['n']}: слот занят (/slots) — дальше по кольцу")
                continue
            payload = {"model": model, "messages": messages,
                       "chat_template_kwargs": {"enable_thinking": think}}
            if max_tokens:
                payload["max_tokens"] = max_tokens
            left = deadline - time.time()
            st, body, err, dt = transport("chat", b, payload, max(5.0, left))
            if st == 400:
                # бэкенд не знает chat_template_kwargs — повторяем без него
                payload.pop("chat_template_kwargs", None)
                st, body, err, dt = transport("chat", b, payload, max(5.0, deadline - time.time()))
            if st != 200 or not isinstance(body, dict):
                log.append(f"№{b['n']} {model}: {err or f'HTTP {st}'}")
                continue
            text, reasoning = answer_of(body)
            finish = (body.get("choices") or [{}])[0].get("finish_reason")
            if not text:
                why = ("рассуждения съели лимит токенов (finish_reason=length)"
                       if finish == "length" and reasoning else
                       "пустой ответ — вероятно, chat-шаблон на сервере")
                log.append(f"№{b['n']} {model}: {why}")
                continue
            return {"ok": True, "text": text, "reasoning": reasoning, "backend": b["n"],
                    "model": model, "seconds": round(dt, 2), "waited": round(waited, 1),
                    "ring": ring, "log": log}
        if time.time() + RING_PAUSE >= deadline:
            break
        log.append(f"круг {ring} неудачен — пауза {RING_PAUSE} с, снова с первого")
        sleep(RING_PAUSE)
        waited += RING_PAUSE
    log.append("дедлайн исчерпан: ни один бэкенд не ответил осмысленно")
    return {"ok": False, "log": log}


# ------------------------------------------------------------------ команды

def mask(key: str) -> str:
    return (key[:6] + "…") if key else "(нет)"


def cmd_show() -> int:
    cfg = parse_config(raw_config())
    kit, project = _roots()
    print(f"# Агент — собранная конфигурация · {TODAY}\n")
    print(f"Слои: кит {kit / '.env.aurora.local'}"
          + (f" ← проект {project / '.env.aurora.local'}" if project else " (проект не выбран)"))
    print(f"Адаптер: {cfg['adapter']} · thinking: {'вкл' if cfg['thinking'] else 'выкл'} · "
          f"шагов ≤ {cfg['max_steps']} · бюджет {cfg['budget_min']} мин · "
          f"таймаут запроса {cfg['request_timeout']} с\n")
    if not cfg["backends"]:
        print("Бэкенды не настроены. Панель: «Настройка» → «Агент», либо руками в "
              ".env.aurora.local (AURORA_AGENT_BACKEND_1_URL=…).")
        return 1
    for b in cfg["backends"]:
        roles = " · ".join(f"{r}={role_model(b, r) or '—'}" for r in ROLES)
        print(f"№{b['n']} {b['url']} · ключ {mask(b['key'])}\n    {roles}")
    ok, version = venv_status()
    print(f"\nPydantic AI: {'установлен, ' + version if ok else 'не установлен'} ({VENV})"
          + ("" if ok else " — работает stdlib-фолбэк; поставить: --venv-install"))
    return 0


def cmd_ping(as_json: bool) -> int:
    """Каждый бэкенд отдельно: живой ответ, а не код 200.

    Зонд показал бэкенд, отвечающий 200 с пустым текстом (chat-шаблон), — поэтому успех
    здесь только осмысленный непустой ответ. Thinking для ping выключен: это проверка
    связности, а не качества; в рабочих вызовах он включён конфигом.
    """
    cfg = parse_config(raw_config())
    rows = []
    for b in cfg["backends"]:
        model = role_model(b, "worker")
        row = {"n": b["n"], "url": b["url"], "model": model}
        if not model:
            row.update(status="нет модели", ok=False)
            rows.append(row)
            continue
        if busy(b, default_transport):
            row.update(status="занят (слот в работе)", ok=False)
            rows.append(row)
            continue
        r = call_role({**cfg, "backends": [b], "request_timeout": 45}, "worker",
                      [{"role": "user", "content": "Повтори одно слово: готов"}],
                      thinking=False, max_tokens=60,
                      deadline=time.time() + 45, sleep=lambda s: None)
        if r["ok"]:
            row.update(status="ок", ok=True, seconds=r["seconds"], answer=r["text"][:60])
        else:
            fails = [l for l in r["log"] if not l.startswith("дедлайн")]
            reason = fails[-1].split(": ", 1)[-1] if fails else "нет ответа"
            if "Connection refused" in reason:
                reason = "недоступен (connection refused)"
            row.update(status=reason, ok=False)
        rows.append(row)

    alive = [r for r in rows if r.get("ok")]
    if as_json:
        print(json.dumps({"backends": rows, "alive": len(alive),
                          "adapter": cfg["adapter"], "venv": venv_status()[0]},
                         ensure_ascii=False))
        return 0 if alive else 1

    print(f"# Агент — проверка цепочки · {TODAY}\n")
    if not rows:
        print("Бэкенды не настроены: «Настройка» → «Агент» в панели.")
        return 1
    for r in rows:
        tail = (f"{r['seconds']} с · «{r['answer']}»" if r.get("ok") else r["status"])
        print(f"{'✅' if r.get('ok') else '✗'} №{r['n']} {r['url']} · {r.get('model') or '—'} · {tail}")
    print(f"\nЖивых бэкендов: {len(alive)} из {len(rows)}. "
          + ("Кольцо работает: первый живой в списке принимает запросы."
             if alive else "Агент работать не сможет — проверьте адреса, VPN и ключи."))
    return 0 if alive else 1


def venv_status() -> tuple:
    """→ (стоит ли pydantic-ai, версия)."""
    vpy = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not vpy.is_file():
        return False, ""
    try:
        p = subprocess.run([str(vpy), "-c",
                            "from importlib.metadata import version; print(version('pydantic-ai'))"],
                           capture_output=True, text=True, timeout=20)
        return (p.returncode == 0, p.stdout.strip())
    except Exception:  # noqa: BLE001
        return False, ""


def cmd_venv_install() -> int:
    """Поставить или обновить Pydantic AI в отдельном venv.

    Отдельный venv, а не системный pip: ядро кита обязано работать без зависимостей, и
    агентский фреймворк не имеет права протечь в него. Путь одинаков для macOS/Linux и
    Windows (pathlib сам разберётся с разделителями и Scripts/).
    """
    vpy = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not vpy.is_file():
        print(f"Создаю venv: {VENV}")
        VENV.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.run([sys.executable, "-m", "venv", str(VENV)],
                           capture_output=True, text=True)
        if p.returncode != 0:
            print(f"agent: venv не создан: {p.stderr[-400:]}", file=sys.stderr)
            return 1
    print("Ставлю/обновляю pydantic-ai (может занять пару минут)…")
    p = subprocess.run([str(vpy), "-m", "pip", "install", "--upgrade", "--quiet",
                        "pydantic-ai"], capture_output=True, text=True, timeout=900)
    if p.returncode != 0:
        print(f"agent: pip не справился: {(p.stderr or p.stdout)[-500:]}", file=sys.stderr)
        return 1
    ok, version = venv_status()
    print(f"✅ pydantic-ai {version} в {VENV}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Встроенный агент: конфигурация и проверка цепочки")
    ap.add_argument("--ping", action="store_true",
                    help="проверить каждый бэкенд живым запросом (thinking выключен)")
    ap.add_argument("--show", action="store_true", help="собранная конфигурация, ключи маской")
    ap.add_argument("--venv-status", action="store_true", help="стоит ли Pydantic AI")
    ap.add_argument("--venv-install", action="store_true",
                    help="поставить/обновить Pydantic AI в ~/.aurora/venv")
    ap.add_argument("--json", action="store_true", help="машинный вывод (для панели)")
    a = ap.parse_args()

    if a.ping:
        return cmd_ping(a.json)
    if a.venv_status:
        ok, version = venv_status()
        if a.json:
            print(json.dumps({"ok": ok, "version": version, "path": str(VENV)}))
        else:
            print(f"Pydantic AI: {'установлен, ' + version if ok else 'не установлен'} ({VENV})")
        return 0
    if a.venv_install:
        return cmd_venv_install()
    return cmd_show()


if __name__ == "__main__":
    sys.exit(main())
