#!/usr/bin/env python3
"""smoke_live.py — снимок живой базы: числа и имена, которые не должны меняться сами.

Фикстуры и золотой корпус проверяют поведение скриптов. Они не отвечают на другой
вопрос: не изменилось ли поведение движка **на конкретной базе** после обновления. На
живом проекте это видно сразу — было 684 ошибки, стало 690, и надо понять почему.

Скрипт запускает только команды наблюдения (никаких `--apply`), складывает результат в
`AuroraKnowledgeDB/meta/smoke_snapshot.json` внутри самого проекта и при следующем
запуске сравнивает. Снимок хранит не только числа, но и **имена**: если пропала история
US-3.1.11 или появился новый битый источник, отчёт назовёт их, а не скажет «-1».

  python3 tests/smoke_live.py <путь-к-проекту> [ещё-путь …]   # сверить со снимком
  python3 tests/smoke_live.py <путь> --update                 # записать новый снимок

Снимок живёт в проекте, а не в ките: это данные проекта, и в чужом репозитории им делать
нечего. Код возврата 1, если что-то разошлось, — годится для CI и для хука.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join("AuroraKnowledgeDB", "meta", "smoke_snapshot.json")
SAMPLE = 12          # сколько имён держать в снимке: достаточно, чтобы назвать виновника


def run(project: str, script: str, args: list) -> str:
    path = os.path.join(project, ".opencode", "scripts", script)
    if not os.path.isfile(path):
        path = os.path.join(KIT, "scripts", script)
    try:
        p = subprocess.run([sys.executable, path, *args], cwd=project,
                           capture_output=True, text=True, timeout=600)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return f"ОШИБКА ЗАПУСКА: {e}"


def take(project: str) -> dict:
    """Снимок: числа плюс поимённые образцы того, что стоит за числом."""
    snap: dict = {}

    stats_raw = run(project, "aurora_stats.py", ["--json"])
    try:
        st = json.loads(stats_raw[stats_raw.index("{"):stats_raw.rindex("}") + 1])
    except Exception:
        st = {}
    snap["карточек"] = st.get("total")
    snap["проверено"] = st.get("trusted")
    snap["процент проверенного"] = st.get("pct_verified")
    snap["статусы"] = st.get("statuses")
    snap["разделы"] = st.get("sections")
    snap["сироты"] = st.get("orphans_count")
    snap["протухшие"] = st.get("expired_count")
    snap["без владельца"] = st.get("no_owner_count")
    snap["битые источники"] = st.get("missing_source_count")
    snap["битые источники (образцы)"] = sorted(
        n for n, _p in (st.get("missing_source") or [])[:SAMPLE])
    snap["требований"] = st.get("req_total")
    snap["вопросов открыто"] = st.get("questions_open")

    lint = run(project, "kb_lint.py", ["--summary"])
    m = re.search(r"карточек (\d+), ошибок (\d+)", lint)
    snap["ошибок линтера"] = int(m.group(2)) if m else None

    dupes = run(project, "kb_fix.py", ["--dupes"])
    m = re.search(r"Двойники: групп (\d+)", dupes)
    snap["групп двойников"] = int(m.group(1)) if m else None

    doctor = run(project, "aurora_doctor.py", [])
    snap["блокеры doctor"] = sorted(l[7:].strip()[:80] for l in doctor.splitlines()
                                    if l.startswith("ERROR:"))

    audit = run(project, "sync_audit.py", [])
    nums = re.findall(r"MISSING: \*\*(\d+)\*\*.*?ORPHAN: \*\*(\d+)\*\*", audit)
    if nums:
        snap["зеркало confluence"] = {"missing": int(nums[0][0]), "orphan": int(nums[0][1])}
    if len(nums) > 1:
        snap["зеркало jira"] = {"missing": int(nums[1][0]), "orphan": int(nums[1][1])}

    schema = run(project, "kb_schema.py", [])
    snap["схема: к переводу"] = int(m.group(1)) if (m := re.search(r"К переводу: (\d+)", schema)) else None

    jira = run(project, "jira_status.py", [])
    for label, key in (("совпали по названию", "историй совпало"),
                       ("Истории без задачи в Jira:", "историй без задач"),
                       ("которой нет в", "задач без историй")):
        m = re.search(re.escape(label) + r"[^\d]*(\d+)", jira)
        snap[key] = int(m.group(1)) if m else None
    snap["истории без задач (образцы)"] = sorted(set(re.findall(
        r"US-\d+(?:\.\d+)+", jira.split("Истории без задачи")[1][:400]))) [:SAMPLE] \
        if "Истории без задачи" in jira else []
    return snap


def diff(old: dict, new: dict) -> list:
    out = []
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        if isinstance(a, list) and isinstance(b, list):
            gone, came = sorted(set(a) - set(b)), sorted(set(b) - set(a))
            bits = []
            if gone:
                bits.append("пропало: " + ", ".join(gone[:6]))
            if came:
                bits.append("появилось: " + ", ".join(came[:6]))
            out.append(f"  {key}: " + "; ".join(bits))
        else:
            out.append(f"  {key}: было {a} → стало {b}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Снимок живой базы и сверка с ним")
    ap.add_argument("projects", nargs="+")
    ap.add_argument("--update", action="store_true", help="перезаписать снимок")
    a = ap.parse_args()

    bad = 0
    for raw in a.projects:
        project = os.path.abspath(os.path.expanduser(raw))
        name = os.path.basename(project)
        if not os.path.isfile(os.path.join(project, "aurora.config.yaml")):
            print(f"❌ {name}: не проект Авроры (нет aurora.config.yaml)", file=sys.stderr)
            bad += 1
            continue
        print(f"\n=== {name} ===")
        new = take(project)
        path = os.path.join(project, SNAP)
        if a.update or not os.path.isfile(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(new, f, ensure_ascii=False, indent=2, sort_keys=True)
            print(f"✅ снимок записан: {SNAP}")
            print(f"   карточек {new.get('карточек')}, ошибок линтера {new.get('ошибок линтера')}, "
                  f"двойников {new.get('групп двойников')}")
            continue
        old = json.load(open(path, encoding="utf-8"))
        d = diff(old, new)
        if not d:
            print(f"✅ без изменений: карточек {new.get('карточек')}, "
                  f"ошибок линтера {new.get('ошибок линтера')}")
            continue
        bad += 1
        print(f"⚠️  разошлось со снимком ({len(d)}):")
        print("\n".join(d))
        print("\n   Это ожидаемо после правки базы — тогда обновите снимок: --update.")
        print("   Если базу не трогали, изменилось поведение движка: смотрите последний коммит kit.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
