#!/usr/bin/env python3
"""install_skills.py — скиллы Авроры в общий каталог агента (фреймворк «Аврора»).

Скиллы живут в репозитории кита, а агент ищет их у себя: `~/.claude/skills/`. Пока копии
там нет, `/aurora-vault` и `/aurora-dev` не находятся ни в одном диалоге — ни в Claude
Code, ни в Cursor, ни в opencode.

  python3 scripts/install_skills.py            # что появится и что обновится
  python3 scripts/install_skills.py --apply
  python3 scripts/install_skills.py --status   # что стоит и не отстало ли от кита

Каталог один — `~/.claude/skills/`. Остальные harness получают на него символьную ссылку:
две копии одного скилла расходятся на первой же правке, и потом невозможно понять, какая
из них отвечала в диалоге.

Копия, а не ссылка на репозиторий: kit переезжает вместе с папкой проектов, а ссылка на
переехавший путь молча перестаёт разворачиваться. Поэтому после правок скиллов установку
надо повторять — `kit:update` делает это сам.

Панель: `kit:skills`
"""
from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
SRC = KIT / "skills"
HOME_SKILLS = Path.home() / ".claude" / "skills"
# Куда положить ссылку на общий каталог: harness ищет скиллы у себя, а держать копии
# в каждом — значит разводить их по разным версиям.
LINK_DIRS = (Path.home() / ".config" / "opencode" / "skills",)


def sources() -> list:
    return sorted(p for p in SRC.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def same(a: Path, b: Path) -> bool:
    """Совпадают ли деревья: сравниваем содержимое, а не даты."""
    if not b.is_dir():
        return False
    cmp = filecmp.dircmp(str(a), str(b))
    if cmp.left_only or cmp.right_only or cmp.diff_files:
        return False
    return all(same(a / d, b / d) for d in cmp.common_dirs)


def plan() -> list:
    """[(имя, состояние)] — что произойдёт с каждым скиллом."""
    out = []
    for src in sources():
        dst = HOME_SKILLS / src.name
        if not dst.exists():
            out.append((src.name, "новый"))
        elif same(src, dst):
            out.append((src.name, "совпадает"))
        else:
            out.append((src.name, "обновится"))
    return out


def cmd_status() -> int:
    rows = plan()
    print(f"# Скиллы Авроры — {HOME_SKILLS}\n")
    if not rows:
        print("В ките нет ни одного скилла — проверьте, что запускаете из корня kit'а.")
        return 1
    for name, state in rows:
        print(f"- {name}: {state}")
    stale = [n for n, s in rows if s != "совпадает"]
    for link in LINK_DIRS:
        target = link / (rows[0][0] if rows else "")
        mark = "ссылка есть" if link.is_dir() and target.exists() else "нет ссылки"
        print(f"\n{link}: {mark}")
    if stale:
        print(f"\nОтстали от кита: {', '.join(stale)} — обновить: "
              f"python3 scripts/install_skills.py --apply")
    else:
        print("\nВсё на месте и совпадает с китом.")
    return 0


def cmd_install(apply: bool) -> int:
    rows = plan()
    if not rows:
        print("install_skills: в ките нет скиллов", file=sys.stderr)
        return 1

    print(f"# Установка скиллов — {HOME_SKILLS}\n")
    todo = [(n, s) for n, s in rows if s != "совпадает"]
    for name, state in rows:
        print(f"- {name}: {state}")
    if not todo:
        print("\nВсё уже совпадает с китом — делать нечего.")
        return 0
    if not apply:
        print(f"\n(dry-run) К установке: {len(todo)}. Повторите с --apply.")
        return 0

    HOME_SKILLS.mkdir(parents=True, exist_ok=True)
    for name, _ in todo:
        dst = HOME_SKILLS / name
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst)
        shutil.copytree(SRC / name, dst)
        print(f"✅ {dst}")

    # ссылки для остальных harness: один источник правды вместо копий
    for link_dir in LINK_DIRS:
        if not link_dir.parent.is_dir():
            continue
        link_dir.mkdir(parents=True, exist_ok=True)
        for name, _ in rows:
            link = link_dir / name
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                continue          # чужая настоящая папка — не трогаем
            link.symlink_to(HOME_SKILLS / name, target_is_directory=True)
            print(f"↪ {link} → {HOME_SKILLS / name}")

    print(f"\nГотово: {len(todo)}. Скиллы доступны из любого диалога — /aurora-vault, "
          f"/aurora-dev.\nПосле правок скиллов в ките установку надо повторить: это копия.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Скиллы Авроры в общий каталог агента")
    ap.add_argument("--status", action="store_true", help="что стоит и не отстало ли")
    ap.add_argument("--apply", action="store_true", help="записать (иначе только показ)")
    a = ap.parse_args()
    if not SRC.is_dir():
        print(f"install_skills: нет {SRC} — запускайте из kit'а", file=sys.stderr)
        return 1
    return cmd_status() if a.status else cmd_install(a.apply)


if __name__ == "__main__":
    sys.exit(main())
