#!/usr/bin/env python3
"""aurora.py — единая точка входа фреймворка Aurora.

Развёртывание и обновление (из клона этого репозитория):

  python3 aurora.py new <target>      развернуть Aurora в новый/существующий проект
                                      (scaffold + интерактивная настройка)
  python3 aurora.py setup <target>    только (пере)настройка проекта
  python3 aurora.py cockpit           панель управления в браузере (все проекты машины)
  python3 aurora.py update <target>   обновить движок в проекте до версии kit
                                      (dry-run; запись — с --apply)

Обслуживание проекта (то же самое можно запускать изнутри проекта из .opencode/scripts/):

  python3 aurora.py list <target>     справочник команд: что есть, чем исполняется
  python3 aurora.py doctor <target>   готовность проекта + сверка структуры папок
  python3 aurora.py stats <target>    дашборд здоровья базы знаний
  python3 aurora.py lint <target>     механические ошибки базы
  python3 aurora.py fix <target>      ремонт: ссылки, гомоглифы, frontmatter (dry-run)
  python3 aurora.py queue <target>    очередь верификации по реальной ценности карточек
  python3 aurora.py remap <target>    перенацелить source: карточек после переезда зеркала
  python3 aurora.py classify <target> артефакты, попавшие в знания; типы карточек
  python3 aurora.py build-plan <target>  план извлечения: партии и возобновление
  python3 aurora.py spec-pack <target> SPEC-NNN  бандл спеки для внешней разработки
  python3 aurora.py index <target>      регенерация _index.md разделов базы
  python3 aurora.py scrub <target>     персональные данные: найти и закрыть маркерами
  python3 aurora.py schema <target>    версия схемы карточек и миграция между версиями
  python3 aurora.py publish <target> <артефакт>  артефакт → generated-страница Confluence
  python3 aurora.py context <target> <тема>   собрать context pack по правилам ретрива
  python3 aurora.py verify <target> <раздел> --owner @x   пакетная верификация
  python3 aurora.py supersede <target> <стар> <нов>       замена знания с историей
  python3 aurora.py impact <target> <карточка>            что зависит от карточки
  python3 aurora.py audit <target>    целостность зеркал Sources/
  python3 aurora.py diff <target>     дрейф: источники против проверенных карточек
  python3 aurora.py release <target> <документ>  заморозить переданную версию
  python3 aurora.py hooks <target>    поставить git pre-commit с линтером
  python3 aurora.py ingest-office <target>  docx/pdf/xlsx из Raw/ → markdown-транскрипты
  python3 aurora.py export <target> <файл>  поставляемый документ → docx/pdf
  python3 aurora.py sync-confluence <target>  детерминированное зеркало Confluence
  python3 aurora.py sync-jira <target>        детерминированное зеркало Jira
  python3 aurora.py jira-status <target>      статусы задач → кандидаты в требованиях

Флаги после команды передаются скрипту как есть:
  python3 aurora.py fix . --all --apply

`new` вызывает install_aurora.py (раскладка файлов), затем aurora_setup.py
(интерактивные вопросы: Confluence base/space/страницы, Jira key/JQL и прочее).
Настройку можно перезапустить когда угодно из самого проекта:
  cd <target> && python3 .opencode/scripts/aurora_setup.py
"""
from __future__ import annotations
import subprocess, sys
from pathlib import Path

KIT = Path(__file__).resolve().parent
SCRIPTS = KIT / "scripts"


def sh(args: list[str]) -> int:
    return subprocess.call([sys.executable, *args])


def cmd_new(target: str, extra: list[str]) -> int:
    tgt = Path(target).expanduser().resolve()
    # Диалог настройки требует терминала. Когда его нет — запуск из скрипта, из панели,
    # из ассистента — вопросы задавать некому, и раньше команда падала на первом же
    # `input()` с EOFError, оставляя развёрнутую, но ненастроенную папку.
    quiet = "--non-interactive" in extra or not sys.stdin.isatty()
    extra = [x for x in extra if x != "--non-interactive"]

    print(f"→ Разворачиваю Aurora в {tgt}\n", flush=True)
    # 1. scaffold (флаги --name/--jira-key/--confluence-space опциональны — setup их уточнит)
    rc = sh([str(SCRIPTS / "install_aurora.py"), "--target", str(tgt),
             "--name", _guess_name(tgt), *extra])
    if rc != 0:
        return rc
    # 2. настройка проекта
    print("\n→ Настройка проекта" + (" (без вопросов: нет терминала)" if quiet else "")
          + "\n", flush=True)
    rc = sh([str(tgt / ".opencode/scripts/aurora_setup.py"), "--target", str(tgt)]
            + (["--non-interactive"] if quiet else []))
    if rc != 0:
        return rc
    # 3. привести AGENTS.md / тела sync-скиллов к финальному конфигу + проставить версию
    print("\n→ Приведение движка к настройкам\n", flush=True)
    rc = sh([str(SCRIPTS / "aurora_update.py"), str(tgt), "--apply"])
    if rc == 0 and quiet:
        print(f"\nНастройка пропущена — вопросы задавать было некому. Заполните конфиг "
              f"позже: python3 aurora.py setup {tgt}")
    return rc


def cmd_setup(target: str, extra: list[str]) -> int:
    tgt = Path(target).expanduser().resolve()
    setup = tgt / ".opencode/scripts/aurora_setup.py"
    if not setup.is_file():
        setup = SCRIPTS / "aurora_setup.py"
    return sh([str(setup), "--target", str(tgt), *extra])


# Команды обслуживания: имя → скрипт. Запускаются в корне проекта (скрипты
# работают с относительными путями), предпочитая копию движка внутри проекта.
TOOLS = {
    "doctor": "aurora_doctor.py",
    "stats": "aurora_stats.py",
    "lint": "kb_lint.py",
    "fix": "kb_fix.py",
    "queue": "aurora_stats.py --queue",
    "structure": "aurora_doctor.py --structure",
    "remap": "kb_remap.py",
    "build-plan": "build_plan.py",
    "spec-pack": "spec_pack.py",
    "index": "kb_index.py",
    "scrub": "kb_scrub.py",
    "schema": "kb_schema.py",
    "publish": "publish_doc.py",
    "verify": "kb_verify.py",
    "supersede": "kb_supersede.py",
    "impact": "kb_trace.py --impact",
    "context": "ctx_pack.py",
    "audit": "sync_audit.py",
    "diff": "sync_audit.py --drift",
    "release": "ship_doc.py --release",
    "hooks": "aurora_hooks.py",
    "ingest-office": "office_ingest.py",
    "export": "ship_doc.py --export docx",
    "sync-confluence": "confluence_export.py",
    "sync-jira": "jira_export.py",
    "jira-status": "jira_status.py",
    "trace": "kb_trace.py --requirements",
    "list": "kit_commands.py",
}


def cmd_tool(name: str, target: str, extra: list[str]) -> int:
    tgt = Path(target).expanduser().resolve()
    # В реестре у команды может стоять фиксированный флаг («queue» — это
    # `aurora_stats.py --queue`): один скрипт, несколько именованных входов.
    file, *fixed = TOOLS[name].split()
    script = tgt / ".opencode/scripts" / file
    if not script.is_file():
        script = SCRIPTS / file
    return subprocess.call([sys.executable, str(script), *fixed, *extra], cwd=str(tgt))


def cmd_update(target: str, extra: list[str]) -> int:
    tgt = Path(target).expanduser().resolve()
    return sh([str(SCRIPTS / "aurora_update.py"), str(tgt), *extra])


def _guess_name(tgt: Path) -> str:
    return tgt.name.replace("-", " ").replace("_", " ").strip() or "Project"


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd = sys.argv[1]
    rest = sys.argv[2:]
    if cmd == "new":
        if not rest:
            print("Укажите путь: python3 aurora.py new <target>", file=sys.stderr)
            return 1
        return cmd_new(rest[0], rest[1:])
    if cmd == "cockpit":
        return sh([str(KIT / "cockpit" / "aurora_cockpit.py"), *rest])
    if cmd == "setup":
        return cmd_setup(rest[0] if rest else ".", rest[1:])
    if cmd == "update":
        return cmd_update(rest[0] if rest else ".", rest[1:])
    if cmd in TOOLS:
        # первый аргумент — путь к проекту, если это не флаг
        if rest and not rest[0].startswith("-"):
            return cmd_tool(cmd, rest[0], rest[1:])
        return cmd_tool(cmd, ".", rest)
    print(f"Неизвестная команда: {cmd}\n{__doc__}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
