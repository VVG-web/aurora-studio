"""folder_guides.py — описания папок проекта: `README.md` в каждой папке схемы кита.

Человек и агент, открывшие папку, должны с первого взгляда понять: для чего она, что
сюда класть и чего нет, кто пишет и можно ли заводить вложенные папки — так, чтобы не
нарушить правила кита (`kit:doctor`). Тексты лежат в ките (`templates/folder_guides.md`),
в проект их кладёт `aurora.py update`.

Описание — блок между метками: его ведёт кит и обновляет вместе с движком. Всё, что
проект допишет ниже метки конца, остаётся как есть. Чужой `README.md` без меток не
теряется: блок кита встаёт над ним.

Движок такие файлы за содержимое не считает: `README.md` — служебное имя
(`aurora_common.is_service`, `is_folder_guide`), его не разбирают в источники, не
показывают шаблоном или артефактом и не считают карточкой.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

GUIDE = "README.md"
BEGIN = ("<!-- aurora:folder:begin — этот блок ведёт кит Авроры и обновляет вместе с движком; "
         "свои заметки пишите ниже метки конца -->")
END = "<!-- aurora:folder:end -->"
BLOCK_RE = re.compile(r"<!-- aurora:folder:begin.*?-->.*?<!-- aurora:folder:end -->\n?", re.S)
FIELDS = (("what", ""), ("put", "Что класть"), ("not", "Чего не класть"), ("who", "Кто пишет"),
          ("nested", "Вложенные папки"))
# Корни, у которых второй уровень задаёт кит (`aurora_doctor.MANAGED_ROOTS`).
MANAGED_ROOTS = ("Artifacts", "AuroraKnowledgeDB", "Raw", "Sources", "Deliverables")


def source(kit) -> Path:
    return Path(kit) / "templates" / "folder_guides.md"


def load(kit) -> dict:
    """{путь папки: {поле: текст}} из файла кита; порядок — как в файле."""
    out, cur = {}, None
    path = source(kit)
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            cur = out.setdefault(line[3:].strip().strip("/"), {})
            continue
        m = re.match(r"^(title|what|put|not|who|nested):\s*(.*)$", line)
        if cur is not None and m:
            cur[m.group(1)] = m.group(2).strip()
    return out


def default_nested(path: str) -> str:
    """Правило вложенных папок по схеме, когда в описании оно не задано."""
    top, _, rest = path.partition("/")
    if top in MANAGED_ROOTS and not rest:
        return ("Папки этого уровня задаёт кит (`structure_dirs.txt`): свою рядом заводить "
                "нельзя — `kit:doctor` назовёт её ошибкой. Папка только для этого проекта — "
                "`paths.extra_structure_dirs` в `aurora.config.yaml`; общая для всех проектов — "
                "только выпуском кита.")
    if top == "Raw":
        return ("Можно: движок обходит папку целиком. Папка с `_` в начале имени "
                "(`_outdated`, `_archive`) — отложенное, её не разбирают.")
    if top == "AuroraKnowledgeDB":
        return "Заводит движок; руками — нет: раскладкой базы управляет сборка."
    if top == "Artifacts":
        return ("Можно, но панель показывает документы вида только из самой папки: подпапки — "
                "для вложений и архива.")
    return "Можно."


def render(path: str, g: dict) -> str:
    lines = [BEGIN, f"# {g.get('title') or path} (`{path}/`)", ""]
    if g.get("what"):
        lines += [g["what"], ""]
    for key, label in FIELDS[1:]:
        text = g.get(key) or (default_nested(path) if key == "nested" else "")
        if text:
            lines.append(f"- **{label}:** {text}")
    return "\n".join(lines + [END]) + "\n"


def plan(target, kit) -> list:
    """[(путь README от корня проекта, новый текст, что будет: create|update)] — только то,
    что надо записать. Папки, которых в проекте нет, пропускаются: схему заводит update."""
    target = Path(target)
    out = []
    for path, g in load(kit).items():
        folder = target / path
        if not folder.is_dir():
            continue
        readme = folder / GUIDE
        block = render(path, g)
        if not readme.is_file():
            out.append((f"{path}/{GUIDE}", block, "create"))
            continue
        old = readme.read_text(encoding="utf-8", errors="replace")
        if BLOCK_RE.search(old):
            m = BLOCK_RE.search(old)
            new = old[:m.start()] + block + old[m.end():]
        else:
            new = block + "\n" + old            # своё описание проекта остаётся под блоком
        if new != old:
            out.append((f"{path}/{GUIDE}", new, "update"))
    return out


def apply(target, changes: list) -> None:
    for rel, text, _kind in changes:
        p = Path(target) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".aurora-tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
