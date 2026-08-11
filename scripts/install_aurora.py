#!/usr/bin/env python3
"""Aurora framework installer — bootstrap or upgrade a target project repo.

Usage:
  python3 scripts/install_aurora.py --target /path/to/project --name "My Project" \\
      [--jira-key PROJ] [--confluence-space SPACE] [--force] [--dry-run]

What it does:
  1. Creates Aurora trust-layer folders (Sources, Raw, AuroraKnowledgeDB, …)
  2. Copies aurora-vault skill + kb_lint.py + aurora_doctor.py into .opencode/
  3. Writes aurora.config.yaml (project settings) + aurora.env.local.example
  4. Writes AGENTS.md from template (Karpathy + Aurora rules)
  5. Seeds meta (conventions, golden_questions, metrics, releases, manifest, index)
  6. Copies Templates/ and Prompts/; docs stay in the kit (pointer in meta/)
  7. Scaffolds Confluence/Jira sync skills (read config, not hardcode JQL)
  8. Writes thin .cursor/rules/atlassian.mdc and .gitignore entries

Safe by default: never overwrites existing files unless --force.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[1]
TODAY = date.today().isoformat()

def manifest_pairs() -> list:
    """[(файл в kit, путь в проекте)] из engine_manifest.txt — тот же список, по которому
    работает `kit:update`. Спец-правила (connectors/agents/seed) установка не трогает:
    их раскладывает update, а коннекторы — install_connectors ниже."""
    man = KIT_ROOT / "engine_manifest.txt"
    out = []
    if not man.is_file():
        return out
    for line in man.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if "=>" not in line:
            continue
        src, dst = (x.strip() for x in line.split("=>", 1))
        if "(" in dst:            # спец-правило — не наше дело
            continue
        out.append((KIT_ROOT / src, dst))
    return out


def _schema_dirs() -> list[str]:
    """Структура проекта — из structure_dirs.txt (единственный источник правды).

    Список фиксирован движком и одинаков во всех проектах Авроры; новые папки
    появляются только релизом kit'а. См. docs/roadmap.md, раздел 2.
    """
    path = KIT_ROOT / "structure_dirs.txt"
    dirs = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                dirs.append(line.rstrip("/"))
    return dirs


# Служебные папки движка (не часть схемы знаний, поэтому не в structure_dirs.txt).
ENGINE_DIRS = [
    ".opencode/skills/aurora-vault/references",
    ".opencode/scripts",
    ".cursor/rules",
]

TRUST_DIRS = _schema_dirs() + ENGINE_DIRS


class Installer:
    def __init__(self, target: Path, name: str, slug: str | None, jira: str | None,
                 confluence: str | None, force: bool, dry_run: bool):
        self.target = target.resolve()
        self.name = name
        self.jira = jira or "PROJECT"
        self.confluence = confluence or "SPACE"
        self.force = force
        self.dry_run = dry_run
        self.slug = slug or self._slug(name)
        self.actions: list[str] = []

    @staticmethod
    def _slug(name: str) -> str:
        # Prefer CamelCase/Pascal without spaces: "Demo Project" → "DemoProject"
        parts = [p for p in name.replace("_", " ").replace("-", " ").split() if p]
        if not parts:
            return "Project"
        return "".join(p[:1].upper() + p[1:] for p in parts)

    def log(self, msg: str):
        self.actions.append(msg)
        print(msg)

    def write(self, rel: str, content: str):
        path = self.target / rel
        if path.exists() and not self.force:
            self.log(f"  skip (exists): {rel}")
            return
        if self.dry_run:
            self.log(f"  would write: {rel}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.log(f"  wrote: {rel}")

    def copy_file(self, src: Path, rel: str):
        path = self.target / rel
        if path.exists() and not self.force:
            self.log(f"  skip (exists): {rel}")
            return
        if self.dry_run:
            self.log(f"  would copy: {rel}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, path)
        self.log(f"  copied: {rel}")

    def copy_tree_files(self, src_dir: Path, dest_rel: str):
        if not src_dir.exists():
            return
        for src in src_dir.rglob("*"):
            if src.is_file():
                rel = f"{dest_rel}/{src.relative_to(src_dir).as_posix()}"
                self.copy_file(src, rel)

    def ensure_dir(self, rel: str):
        path = self.target / rel
        if path.exists():
            self.log(f"  exists: {rel}/")
            return
        if self.dry_run:
            self.log(f"  would mkdir: {rel}/")
            return
        path.mkdir(parents=True, exist_ok=True)
        # gitkeep only for truly empty leaf folders
        (path / ".gitkeep").touch()
        self.log(f"  mkdir: {rel}/")

    def ensure_dirs(self):
        self.log("== folders ==")
        for d in TRUST_DIRS:
            self.ensure_dir(d)

    def install_skill(self):
        self.log("== aurora-vault skill ==")
        skill_src = KIT_ROOT / "skills/aurora-vault"
        self.copy_file(skill_src / "SKILL.md", ".opencode/skills/aurora-vault/SKILL.md")
        for ref in (skill_src / "references").glob("*.md"):
            self.copy_file(ref, f".opencode/skills/aurora-vault/references/{ref.name}")
        # skill.json берём из kit'а, а не пишем свой: два источника одного файла
        # означают, что update будет вечно предлагать перезапись сразу после установки
        self.copy_file(skill_src / "skill.json", ".opencode/skills/aurora-vault/skill.json")
        # Список инженерных файлов — один на весь kit: `engine_manifest.txt`. Свой
        # хардкод здесь уже расходился с манифестом (три скрипта новый проект не получал
        # до первого `kit:update`), поэтому раскладку ведёт манифест, а не память.
        for src, dst in manifest_pairs():
            if src.name.endswith(".py") and src.parent.name == "scripts":
                self.copy_file(src, dst)
        # схема структуры папок: движок сверяет по ней факт (doctor --structure)
        self.copy_file(KIT_ROOT / "structure_dirs.txt", ".opencode/structure_dirs.txt")
        # реестр команд: справочник kit:list собирается из него
        self.copy_file(KIT_ROOT / "commands.txt", ".opencode/commands.txt")
        # где лежит kit: копии setup/update внутри проекта берут отсюда манифест и версию
        self.write(".opencode/kit_path.txt", str(KIT_ROOT) + "\n")

    def install_project_config(self):
        self.log("== aurora.config.yaml ==")
        tpl = (KIT_ROOT / "templates/aurora.config.yaml.template").read_text(encoding="utf-8")
        content = (
            tpl.replace("{{PROJECT_NAME}}", self.name)
            .replace("{{PROJECT_SLUG}}", self.slug)
            .replace("{{JIRA_KEY}}", self.jira)
            .replace("{{CONFLUENCE_SPACE}}", self.confluence)
        )
        self.write("aurora.config.yaml", content)
        self.copy_file(
            KIT_ROOT / "templates/aurora.env.local.example",
            "aurora.env.local.example",
        )

    def install_agents(self):
        self.log("== AGENTS.md ==")
        tpl = (KIT_ROOT / "templates/agents/AGENTS.md.template").read_text(encoding="utf-8")
        text = (
            tpl.replace("{{PROJECT_NAME}}", self.name)
            .replace("{{PROJECT_SLUG}}", self.slug)
            .replace("{{JIRA_KEY}}", self.jira)
            .replace("{{CONFLUENCE_SPACE}}", self.confluence)
            .replace("{{DATE}}", TODAY)
        )
        self.write("AGENTS.md", text)

    def install_meta(self):
        self.log("== AuroraKnowledgeDB meta ==")
        conv = (KIT_ROOT / "templates/meta/conventions.md").read_text(encoding="utf-8")
        # strip project-specific tag examples into generic ones if still present
        self.write("AuroraKnowledgeDB/meta/conventions.md", conv)

        gq = f"""# Golden questions — регрессионные проверки базы знаний

Контрольные вопросы с эталонными ответами. Прогоняются командой `/aurora-vault eval`.
Проект: **{self.name}**. Bootstrap: {TODAY}.

| # | Вопрос | Эталон (кратко) | Карточка-источник |
|---|---|---|---|
| 1 | _(добавьте первый доменный вопрос после verify)_ | | |

_Мета-вопросы:_

| # | Вопрос | Эталон |
|---|---|---|
| M1 | Спроси про несуществующее в базе | Ответ «нет данных», без выдумок |
| M2 | Почему структура папок такая? | Ссылка на фреймворк Аврора / AGENTS.md |
"""
        self.write("AuroraKnowledgeDB/meta/golden_questions.md", gq)

        metrics = f"""# Метрики пользы базы знаний

Замер раз в месяц (последняя пятница, после garden).

| Метрика | Как считать | Зачем |
|---|---|---|
| % verified | `status` | Здоровье базы |
| Доля артефактов с based_on | frontmatter в Artifacts/ | Использование базы |
| Замечания на артефакт | ревью US/AC | Качество на выходе |

## Журнал замеров

| Месяц | % verified | Артефактов с based_on | Замечаний на артефакт | Eval score | Комментарий |
|---|---|---|---|---|---|
| {TODAY[:7]} (базовая точка) | 0% (0/0) | — | — | — | Установка Аврора; bootstrap |
"""
        self.write("AuroraKnowledgeDB/meta/metrics.md", metrics)

        releases = """# Релизы проекта

Реестр релизов, по которому фильтруется контекст: карточка с `applies_to`, не содержащим
текущий релиз, в пак фактов не попадает (см. `references/retrieval.md`). Пустой
`applies_to` = карточка верна для всех релизов.

Держите строку `current` актуальной — по ней `ctx_pack.py` определяет релиз задачи.
`kb_lint` ругается, если карточка ссылается на релиз, которого здесь нет.

| Релиз | Период | Состояние |
|---|---|---|
| R1 | — | current |

Знание изменилось от релиза к релизу — это ДВЕ карточки (`applies_to: [R1]` и
`applies_to: [R2+]`) со взаимными ссылками, а не `deprecated`: старая верна для своего
релиза. `deprecated` — только когда знание неверно для всех релизов.
"""
        self.write("AuroraKnowledgeDB/meta/releases.md", releases)

        manifest = {
            "version": 1,
            "project": self.name,
            "framework": "Aurora",
            "updated": TODAY,
            "cards": [],
            "note": "Empty bootstrap manifest. Populate via /aurora-vault build.",
        }
        self.write(
            "AuroraKnowledgeDB/meta/manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )

        # версия-штамп движка (для aurora.py update / doctor)
        vf = KIT_ROOT / "VERSION"
        self.write(
            "AuroraKnowledgeDB/meta/aurora_version.txt",
            (vf.read_text(encoding="utf-8").strip() if vf.exists() else "1.0.0") + "\n",
        )

        index = f"""# AuroraKnowledgeDB

Главная точка входа в базу знаний проекта **{self.name}**.
Фреймворк «Аврора», установка: {TODAY}. Режим: **bootstrap**.

## Concepts
- Индекс: [[Concepts/_index|Concepts]]

## Processes
- Индекс: [[Processes/_index|Processes]]

## Glossary
- Индекс: [[Glossary/_index|Glossary]]

## Systems
- Индекс: [[Systems/_index|Systems]]

## Roles
- Индекс: [[Roles/_index|Roles]]

## Statuses
- Индекс: [[Statuses/_index|Statuses]]

## Reference
- Индекс: [[Reference/_index|Reference]]

## Requirements
- Индекс: [[Requirements/_index|Requirements]]

## Specs
- Индекс: [[Specs/_index|Specs]]

## Decisions
- Индекс: [[Decisions/_index|Decisions]]

## MOC
- Индекс: [[MOC/_index|MOC]]
"""
        self.write("AuroraKnowledgeDB/index.md", index)

        for folder in [
            "Concepts", "Processes", "Glossary", "Systems", "Roles", "Statuses",
            "Decisions", "Requirements", "Reference", "Specs", "MOC",
        ]:
            self.write(
                f"AuroraKnowledgeDB/{folder}/_index.md",
                f"# {folder}\n\nИндекс раздела. Карточек: 0 (на {TODAY}).\n",
            )

        self.write(
            "AuroraKnowledgeDB/meta/releases.md",
            f"""# Releases

Список релизов проекта и маркер текущего. Используется полем `applies_to` карточек.

| Релиз | Описание | current |
|---|---|---|
| R1 | _(первичный / bootstrap)_ | yes |

Обновлено: {TODAY}.
""",
        )

        self.write(
            "Workspaces/README.md",
            f"""# Workspaces — рабочие пространства больших задач

Одна большая задача = одна папка. Содержимое — **не знание**.
Результат уезжает в Deliverables / Artifacts / AuroraKnowledgeDB / Raw; завершённое → `_archive/`.

## Активные воркспейсы

| Папка | Задача |
|---|---|
| `_archive/` | Завершённые задачи |

Проект: {self.name}. Обновлено: {TODAY}.
""",
        )

    def install_templates_prompts(self):
        self.log("== Templates & Prompts ==")
        self.copy_tree_files(KIT_ROOT / "scaffold/Templates", "Templates")
        self.copy_tree_files(KIT_ROOT / "scaffold/Prompts", "Prompts")

    def install_launchers(self):
        """Пусковые файлы в корне проекта: не всем удобно ходить в терминал.

        `start-aurora.command` (macOS/Linux, двойной щелчок) и `start-aurora.bat`
        (Windows) дают
        меню: проверка готовности, здоровье базы, настройка, панель, справочник команд.
        Путь к kit'у подставляется при установке, но launcher умеет искать его сам.
        """
        self.log("== пусковые файлы ==")
        for name in ("start-aurora.command", "start-aurora.bat"):
            src = KIT_ROOT / "templates/launchers" / name
            if not src.is_file():
                continue
            text = src.read_text(encoding="utf-8").replace("{{KIT_PATH}}", str(KIT_ROOT))
            path = self.target / name
            if path.exists() and not self.force:
                self.log(f"  skip (exists): {name}")
                continue
            if self.dry_run:
                self.log(f"  would write: {name}")
                continue
            path.write_text(text, encoding="utf-8")
            if name.endswith(".command"):
                path.chmod(0o755)
            self.log(f"  wrote: {name}")

    def install_docs(self):
        """Документация для людей — ссылкой, а не копией.

        Раньше в проект копировались HTML-гайды, и у каждого проекта заводилась своя
        стареющая копия описания фреймворка. Описание принадлежит киту: там оно живёт
        в одном экземпляре и обновляется вместе с движком.
        """
        self.log("== документация ==")
        self.write("AuroraKnowledgeDB/meta/where_to_read.md",
                   "# Где читать про фреймворк\n\n"
                   "Документация живёт в ките, а не в проекте — чтобы у каждого проекта\n"
                   "не заводилась своя стареющая копия.\n\n"
                   f"- набор для людей: `<kit>/docs/readme/` — обзор, лёгкий старт,\n"
                   "  регламент, практика, уход за базой, спецификации;\n"
                   "- справочник команд: `python3 .opencode/scripts/kit_commands.py`;\n"
                   "- панель управления: `python3 <kit>/aurora.py cockpit`.\n\n"
                   "Процедуры, по которым работает ассистент, лежат в проекте:\n"
                   "`.opencode/skills/aurora-vault/`.\n")

    def install_connectors(self):
        """Модули источников: манифест, скрипт запуска, папка зеркала и sync-скилл.

        Поимённо здесь ничего не перечислено: сколько модулей лежит в `connectors/`,
        столько и встанет. Так доустановленный модуль приезжает в проект сам.
        """
        self.log("== модули источников ==")
        for man in sorted((KIT_ROOT / "connectors").glob("*/connector.json")):
            m = json.loads(man.read_text(encoding="utf-8"))
            self.copy_file(man, f".opencode/connectors/{m['id']}.json")
            script = (m.get("run") or {}).get("script", "")
            if script:
                # скрипт может лежать рядом с манифестом (доустановленный модуль) или
                # в scripts/ kit'а (встроенный — его же импортируют панель и publish_doc)
                src = man.parent / script
                self.copy_file(src if src.is_file() else KIT_ROOT / "scripts" / script,
                               f".opencode/scripts/{script}")
            mirror = (m.get("mirror") or {}).get("default_path", "")
            if mirror:
                self.ensure_dir(mirror)
            skill, tpl = (m.get("run") or {}).get("skill", ""), man.parent / "SKILL.md"
            if not (skill and tpl.is_file()):
                continue
            name = f"{skill}-{self.slug}"
            body = (tpl.read_text(encoding="utf-8")
                    .replace("{{PROJECT_SLUG}}", self.slug)
                    .replace("{{PROJECT_NAME}}", self.name)
                    .replace("{{CONFLUENCE_SPACE}}", self.confluence)
                    .replace("{{JIRA_KEY}}", self.jira))
            self.write(f".opencode/skills/{name}/SKILL.md", body)
            self.write(
                f".opencode/skills/{name}/skill.json",
                json.dumps({"name": name,
                            "description": f"{m.get('title', m['id'])} → {mirror}",
                            "entrypoint": "SKILL.md"}, indent=2, ensure_ascii=False) + "\n",
            )

    def install_cursor_rules(self):
        self.log("== .cursor/rules ==")
        self.write(
            ".cursor/rules/atlassian.mdc",
            """---
alwaysApply: true
---

# Atlassian MCP tool

Project Atlassian settings live in **`aurora.config.yaml`** (`atlassian.*`).

- Confluence space / sync roots: `atlassian.confluence`
- Jira project / default JQL: `atlassian.jira`
- Auth: **your** Cursor MCP login (`atlassian.auth.mode: mcp_user`). Never commit tokens.

Do not hardcode another teammate's credentials in skills or rules.
""",
        )

    def install_gitignore(self):
        self.log("== .gitignore ==")
        block = """
# Aurora / local
.DS_Store
.env
.env.*
!.env.aurora.local.example
aurora.env.local
.env.aurora.local
.mcp.json
__pycache__/
*.pyc
.ruff_cache/
~$*
*.log

# Служебное состояние инструментов. Правило Авроры: что закрыто .gitignore,
# то допустимо вне схемы папок — doctor такие пути не считает нарушением.
.sisyphus/
.opencode/node_modules/
node_modules/
.venv/
# Браузерные MCP-инструменты сбрасывают логи консоли и слепки страниц в текущую
# рабочую папку. Открыли дашборд, стоя в корне проекта, — получили блокер doctor и
# слепок чужой страницы в истории git.
.playwright-mcp/
.puppeteer/
# Семантический индекс — производная от карточек: бинарь на мегабайты, меняется
# целиком при смене модели и пересобирается за минуты (`kb:embed --apply`).
AuroraKnowledgeDB/meta/embeddings.bin
AuroraKnowledgeDB/meta/embeddings.json
"""
        gi = self.target / ".gitignore"
        if self.dry_run:
            self.log("  would update: .gitignore")
            return
        if gi.exists():
            cur = gi.read_text(encoding="utf-8")
            if "aurora.config.yaml" in cur and ".env.aurora.local" in cur:
                self.log("  skip (exists): .gitignore aurora entries")
            elif ".mcp.json" in cur and "__pycache__" in cur:
                if ".env.aurora.local" not in cur:
                    gi.write_text(
                        cur.rstrip()
                        + "\n\n# Aurora local secrets\n.env.aurora.local\naurora.env.local\n",
                        encoding="utf-8",
                    )
                    self.log("  appended: .gitignore aurora local secrets")
                else:
                    self.log("  skip (exists): .gitignore entries")
                return
            else:
                gi.write_text(cur.rstrip() + "\n" + block, encoding="utf-8")
                self.log("  appended: .gitignore")
        else:
            gi.write_text(block.lstrip(), encoding="utf-8")
            self.log("  wrote: .gitignore")

    def write_report(self):
        report = f"""---
title: "Aurora install report — {self.name}"
type: report
created: {TODAY}
---

# Aurora install report

- Project: {self.name}
- Target: `{self.target}`
- Jira: {self.jira}
- Confluence: {self.confluence}
- Config: `aurora.config.yaml`
- Mode: {"dry-run" if self.dry_run else "apply"}{" (force)" if self.force else ""}

## Actions

""" + "\n".join(f"- {a}" for a in self.actions) + f"""

## Next steps

1. Open `aurora.config.yaml` — set Confluence `sync_roots` and verify Jira JQL.
2. Copy `aurora.env.local.example` → `.env.aurora.local` (optional; personal only).
3. Authenticate **your** Atlassian account in Cursor MCP (mcp-atlassian).
4. Run: `python3 .opencode/scripts/aurora_doctor.py`
5. Put evidence into `Raw/`; then `/aurora-vault build` or ingest.
6. Read HTML guides in `Artifacts/drafts/`.
"""
        self.write(f"Artifacts/reports/{TODAY}_report_aurora-install.md", report)

    def run(self):
        if not self.target.exists():
            if self.dry_run:
                self.log(f"would create target dir: {self.target}")
            else:
                self.target.mkdir(parents=True, exist_ok=True)
        print(f"Aurora install → {self.target}")
        print(f"  name={self.name!r} slug={self.slug} jira={self.jira} confluence={self.confluence}")
        print(f"  force={self.force} dry_run={self.dry_run}\n")
        self.ensure_dirs()
        self.install_skill()
        self.install_project_config()
        self.install_agents()
        self.install_meta()
        self.install_templates_prompts()
        self.install_launchers()
        self.install_docs()
        self.install_connectors()
        self.install_cursor_rules()
        self.install_gitignore()
        self.write_report()
        print("\nDone." + (" (dry-run — no files written)" if self.dry_run else ""))
        return 0


def main():
    p = argparse.ArgumentParser(description="Install Aurora knowledge framework into a project")
    p.add_argument("--target", required=True, help="Path to target project root")
    p.add_argument("--name", required=True, help='Human project name, e.g. "Northwind"')
    p.add_argument("--slug", default=None, help='Short id for skill names, e.g. "Northwind" (default: from --name)')
    p.add_argument("--jira-key", default=None, help="Jira project key (default: PROJECT)")
    p.add_argument("--confluence-space", default=None, help="Confluence space key")
    p.add_argument("--force", action="store_true", help="Overwrite existing files")
    p.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    args = p.parse_args()
    inst = Installer(
        target=Path(args.target),
        name=args.name,
        slug=args.slug,
        jira=args.jira_key,
        confluence=args.confluence_space,
        force=args.force,
        dry_run=args.dry_run,
    )
    sys.exit(inst.run())


if __name__ == "__main__":
    main()
