#!/usr/bin/env python3
"""agent_probe.py — живая проверка связи со шлюзами. Без кеша, без карантина.

Панель: `agent:probe`

`agent:ping` показывает состояние кольца, и если бэкенд помечен недоступным, печатает
метку: «не отвечал, вернёмся через 865 с». Это сообщение о **пропуске**, а не результат
опроса: пока метка жива, к бэкенду никто не обращается. Проверка связи, показывающая
кеш, бесполезна ровно тогда, когда нужна.

Здесь каждый бэкенд опрашивается **сейчас**, и печатается то, что ответил сервер.

Главное отличие от `ping` — различаем три разных случая, которые тот сводит в один:

  сеть        адрес не отвечает, соединение отвергнуто, таймаут — вот это «нет связи»
  доступ      сервер ответил 401/403: ключ неверен или истёк. Ждать бессмысленно
  модель      сервер ответил 404 или «model not found»: адрес и ключ верны, а имени
              модели у него нет. Тогда печатаем список моделей, которые у него ЕСТЬ

Различать их важно: первое лечится ожиданием, второе и третье — правкой настройки, и
пятнадцатиминутный карантин там только мешает.

  python3 scripts/agent_probe.py                 # из корня проекта или кита
  python3 scripts/agent_probe.py --models        # ещё и полный список моделей шлюза
  python3 scripts/agent_probe.py --timeout 30    # медленный шлюз

Зависимостей нет: urllib из стандартной библиотеки.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent_core as AG                                           # noqa: E402


def backends() -> list:
    """[{n, url, model, key}] — тем же чтением настроек, каким живёт сам движок.

    Своё чтение .env здесь уже было, и оно врало: движок складывает настройку слоями
    (кит < проект < окружение) и бэкенды держит в файле **кита**, а копия смотрела
    только в папку проекта — «бэкенды не настроены» там, где движок видел три. Два пути
    к одной настройке расходятся всегда; остался один, общий с движком.
    """
    cfg = AG.parse_config(AG.raw_config())
    out = []
    for b in cfg["backends"]:
        # Общей модели может не быть вовсе: контур, где у каждой роли своя. Движок в
        # таком случае берёт ролевую (`role_model`), и проверка обязана спрашивать тем
        # же именем — иначе она шлёт пустое поле и получает «Missing model field»,
        # объявляя живой шлюз сломанным. Так и вышло на живом контуре.
        # У ролей могут быть РАЗНЫЕ модели на одном шлюзе, и «бэкенд недоступен» тогда
        # ничего не значит: планировщик ходит к одной, работник к другой, и упасть может
        # любая. На живом контуре отчёт назвал `deepseek-v4-flash` — модель роли qa, — а
        # проверка спрашивала работника и говорила «жив». Спрашиваем каждую.
        models = []
        for role in AG.ROLES:
            m = AG.role_model(b, role)
            if m and m not in [x["model"] for x in models]:
                models.append({"model": m,
                               "roles": [r for r in AG.ROLES if AG.role_model(b, r) == m]})
        for spec in models or [{"model": b["model"], "roles": []}]:
            out.append({"n": b["n"], "url": b["url"], "key": b["key"],
                        "model": spec["model"], "roles": spec["roles"],
                        "by_role": bool(spec["model"] and not b["model"])})
    return out


def ask(url: str, key: str, payload, timeout: float, path: str) -> tuple:
    """Один запрос. → (код, тело, ошибка, секунды). Код None — до сервера не дошли."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), "", time.time() - t0
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
        return e.code, body, "", time.time() - t0
    except Exception as e:                                     # noqa: BLE001
        return None, "", f"{type(e).__name__}: {e}", time.time() - t0


def models_of(url: str, key: str, timeout: float) -> list:
    """Какие модели у шлюза на самом деле. [] — не спросить."""
    code, body, _err, _dt = ask(url, key, None, timeout, "/models")
    if code != 200:
        return []
    try:
        return [str(m.get("id")) for m in (json.loads(body).get("data") or []) if m.get("id")]
    except ValueError:
        return []


def verdict(code, body: str, err: str) -> tuple:
    """→ (значок, разряд, что сказать человеку). Разряд: сеть | доступ | модель | ок | иное."""
    if code is None:
        return "✗", "сеть", f"до сервера не дошли — {err}"
    if code == 200:
        return "✅", "ок", "ответил"
    low = (body or "").lower()
    if code in (401, 403):
        return "✗", "доступ", f"HTTP {code}: ключ неверен или истёк — ожидание не поможет"
    if code == 404 or "model" in low and ("not found" in low or "not exist" in low
                                          or "unknown" in low or "не найдена" in low):
        return "✗", "модель", f"HTTP {code}: шлюз не знает такой модели"
    if code == 429:
        return "⚠", "иное", "HTTP 429: шлюз просит подождать — это перегрузка, не поломка"
    short = " ".join((body or "").split())[:140]
    return "✗", "иное", f"HTTP {code}: {short or 'тело ответа пустое'}"


# Слои запроса — от того, что шлёт любой клиент, до того, что шлёт Аврора. Проверяем
# по одному: первый упавший слой и есть причина. Так отличается «шлюз недоступен» от
# «шлюз не принимает наш запрос» — снаружи они выглядят одинаково, а лечатся по-разному.
LAYERS = (
    ("голый запрос", {}),
    ("+ chat_template_kwargs", {"chat_template_kwargs": {"enable_thinking": False}}),
    ("+ _slots", {"_slots": 8}),
    ("+ guard/role", {"guard": {"grams": [], "gram": 3, "max_words": 12, "ready": False},
                      "role": "worker"}),
)


def why_failing(b: dict, timeout: float) -> None:
    """Послойно: какой именно кусок запроса шлюз не принимает."""
    body_base = {"model": b["model"], "messages": [{"role": "user", "content": "ping"}],
                 "max_tokens": 1}
    payload = dict(body_base)
    print("    послойная проверка запроса:")
    for name, extra in LAYERS:
        payload = {**payload, **extra}
        code, body, err, dt = ask(b["url"], b["key"], payload, timeout, "/chat/completions")
        mark, _kind, say = verdict(code, body, err)
        print(f"      {mark} {name:24} {say[:96]}")
        if code != 200:
            fields = ", ".join(extra) or "—"
            print(f"      ↳ ломается здесь. Лишние поля этого слоя: {fields}")
            return
    print("      все слои прошли — запрос шлюз принимает целиком")


def main() -> int:
    ap = argparse.ArgumentParser(description="Живая проверка связи со шлюзами")
    ap.add_argument("--timeout", type=float, default=20.0, help="секунд на запрос")
    ap.add_argument("--models", action="store_true", help="печатать список моделей шлюза")
    ap.add_argument("--why", action="store_true",
                    help="послойно: какой кусок запроса шлюз не принимает")
    a = ap.parse_args()

    bs = backends()
    if not bs:
        print("Бэкенды не настроены: нет AURORA_AGENT_BACKEND_1_URL.\n"
              "Настройка читается слоями: .env.aurora.local кита, затем проекта, затем "
              "окружение.", file=sys.stderr)
        return 1

    print(f"# Живая проверка связи — {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    print("Опрашиваю каждый шлюз сейчас. Карантин `agent:ping` здесь не действует:\n"
          "эта проверка не читает отметок, она спрашивает сервер.\n")

    alive = 0
    for b in bs:
        code, body, err, dt = ask(b["url"], b["key"],
                                  {"model": b["model"],
                                   "messages": [{"role": "user", "content": "ping"}],
                                   "max_tokens": 1},
                                  a.timeout, "/chat/completions")
        mark, kind, say = verdict(code, body, err)
        keyed = "ключ есть" if b["key"] else "ключа нет"
        roles = b.get("roles") or []
        named = (b["model"] or "—") + (f" · роли: {', '.join(roles)}"
                                       if b.get("by_role") and roles else "")
        print(f"{mark} №{b['n']} {b['url']} · {named} · {dt:.2f} с · {keyed}")
        print(f"    {say}")
        if kind == "ок":
            alive += 1
        elif kind in ("модель", "доступ"):
            # Сервер отвечает — значит связь есть, и спросить его о моделях можно.
            got = models_of(b["url"], b["key"], a.timeout)
            if got:
                print(f"    у шлюза есть модели ({len(got)}): "
                      + ", ".join(got[:12]) + ("…" if len(got) > 12 else ""))
                if b["model"] and b["model"] not in got:
                    near = [m for m in got if b["model"].split("-")[0].lower() in m.lower()]
                    print(f"    ⚠ «{b['model']}» среди них НЕТ"
                          + (f" · похожие: {', '.join(near[:5])}" if near else ""))
            elif kind == "доступ":
                print("    список моделей тоже под ключом — проверьте сам ключ")
        # Слои гоняем ВСЕГДА, а не только при отказе. Базовый запрос здесь минимальный —
        # такой же, как у любого клиента, — и шлюз его принимает. Ровно этим и опасен
        # случай «у других работает, у нас нет»: проверка сказала бы «ответил» и умолкла,
        # а ломается запрос Авроры, который богаче.
        if a.why:
            why_failing(b, a.timeout)
        if a.models and kind == "ок":
            got = models_of(b["url"], b["key"], a.timeout)
            if got:
                print(f"    моделей у шлюза: {len(got)} — " + ", ".join(got[:12])
                      + ("…" if len(got) > 12 else ""))
        print()

    print(f"Отвечают сейчас: {alive} из {len(bs)} "
          "(шлюз считается по каждой своей модели отдельно: у ролей они могут "
          "отличаться,\nи упасть может одна из них).")
    if alive < len(bs):
        print("\nОтвет «HTTP …» означает, что сервер жив и что-то сказал — это не «нет связи».\n"
              "Ключ и имя модели ожиданием не чинятся: `agent:ping` сажает такой шлюз в\n"
              "карантин на 15 минут, и снять его можно кнопкой «Вернуться на основного».")
    return 0 if alive else 2


if __name__ == "__main__":
    sys.exit(main())
