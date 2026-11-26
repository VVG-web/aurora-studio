#!/usr/bin/env python3
"""local_paths.py — единый реестр локальных путей Авроры.

Локальный путь — служебный файл или каталог этой машины: журнал запусков панели,
снимки сети, личное состояние Obsidian, штамп версии, клоны репозиториев кода. Такой
путь объявлен один раз здесь и дальше одинаково понимается установщиком (правила
`.gitignore`), обновлением (снятие с индекса), авто-коммитом панели, чекпойнтом
агента, `commit_result`, doctor и тестами.

Почему отдельный модуль, а не список в каждом скрипте: правила были разбросаны —
`install_aurora.GITIGNORE_BLOCK`, `aurora_update.LOCAL_STATE_RELS`, `tracked_but_ignored`.
`add -A` возвращал снятый с индекса файл обратно, потому что `.gitignore` на уже
отслеживаемое не действует; а списки расходились молча.

Модуль на стандартной библиотеке и без побочных эффектов при импорте: его читают и
установщик, и копия `git_sync` из проекта. Потребители подключают его лениво (`_lp()`):
оборванное обновление не должно ронять панель. Установщик и обновление имеют собственные
литералы блока правил, а коммит панели, чекпойнт агента и `commit_result` без реестра
отказывают: без него неоткуда взять список локальных путей, и угадывать его нельзя.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------- виды правил

LOCAL_PATH = "local_path"     # явный файл: снимается с индекса и закрыт .gitignore
LOCAL_DIR = "local_dir"       # явный каталог: снимается рекурсивно
CLONE_GLOB = "clone_glob"     # шаблон с отрицаниями (`Code/*`): снимаются каталоги-клоны
IGNORE_ONLY = "ignore_only"   # правило есть в .gitignore, индекс не трогаем

# Сегодняшний блок правил — дословно. Правка списка локального — это правка RULES и
# этого блока вместе: `validate()` сверяет, что у каждого снимаемого пути есть строка.
GITIGNORE_BLOCK = """
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
# Временный файл атомарной записи: остаётся только после убитого процесса
*.aurora-tmp
# Повреждённое состояние движка, отложенное в сторону (aurora_fs.read_state), — для разбора, не для git
*.corrupt
*.corrupt-[0-9]*

# Движок Авроры — копия кита: `aurora.py update` ставит её из кита, версия записана в
# AuroraKnowledgeDB/meta/aurora_version.txt. Внутри — и состояние, прогоны, вложения, кэш.
.aurora/
# Папки харнессов — агентов и редакторов — у каждого свои: настройки, планы, кэш. Общие
# у проекта только навыки: `.claude/skills/` остаётся в git.
.cursor/
.opencode/
.codex/
.gemini/
.windsurf/
.claude/*
!.claude/skills/

# Служебное состояние инструментов. Правило Авроры: что закрыто .gitignore,
# то допустимо вне схемы папок — doctor такие пути не считает нарушением.
.sisyphus/
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
# Граф базы наружу (`kb:graph-export`, `kb:code-graph`) — производная: graph.json для
# MCP-сервера графа, страница и скрипт Neo4j от graphify на мегабайты, пересобираются
# каждым прогоном. Заметки тем в `MOC/Сообщества/` — навигация базы, они в git.
AuroraKnowledgeDB/meta/graphify/
# Журнал запусков панели — по строке на команду, меняется каждым запуском: в git он давал
# каждый восьмой коммит и вечно грязное дерево. Дата обновления базы для команды — в
# AuroraKnowledgeDB/meta/kb_updated.d/, она в git по частям участников.
AuroraKnowledgeDB/meta/run_log.md
# Снимки сети за прогон (`net:report`) — локальная телеметрия этой машины, а не знание
# проекта: панель читает их с диска. В git они не едут, иначе каждый push или pull
# оставлял бы незакоммиченный файл, а история росла бы на файл за прогон.
AuroraKnowledgeDB/meta/net-runs.d/
# Последние открытые в панели файлы: личная история на этой машине, не общее состояние проекта.
AuroraKnowledgeDB/meta/recent-files.json
# Штамп версии движка, записанный этой машиной при update: у каждого участника свой
# шаг обновления, а панель берёт штамп из копии движка в .aurora/ (не в git).
AuroraKnowledgeDB/meta/aurora_version.txt
# Пусковые файлы этой машины: движок ставит их при установке, они не исходник проекта.
start-aurora.bat
start-aurora.command
# Локальная точка сравнения выдачи (`kb:retrieval --save`), не общий эталон проекта.
AuroraKnowledgeDB/meta/retrieval-last.json
# Производный граф карточек: пересобирается из базы командой kb:graph.
AuroraKnowledgeDB/meta/graph.json
# Персональное состояние Obsidian. Состав плагинов, их код и темы остаются общими.
AuroraKnowledgeDB/.obsidian/workspace.json
AuroraKnowledgeDB/.obsidian/workspace-mobile.json
AuroraKnowledgeDB/.obsidian/app.json
AuroraKnowledgeDB/.obsidian/appearance.json
AuroraKnowledgeDB/.obsidian/graph.json
AuroraKnowledgeDB/.obsidian/hotkeys.json
AuroraKnowledgeDB/.obsidian/bookmarks.json
AuroraKnowledgeDB/.obsidian/starred.json
AuroraKnowledgeDB/.obsidian/plugins/recent-files-obsidian/data.json
# Клоны репозиториев кода (`code:sync`): чужой код в историю проекта не едет — у каждого свой клон, состав
# записан в aurora.config.yaml (`code_repos:`). В git остаются только описание папки и её пометка.
Code/*
!Code/.gitkeep
!Code/README.md
"""

# Клоны лежат каталогами верхнего уровня под Code/; описание папки и её пометка — общие.
CLONE_KEEP = ("README.md", ".gitkeep")

# Маркеры корня проекта: по ним пишущая операция отличает проект от его подпапки и видит
# вложенный проект в чужом репозитории. `.aurora/` маркером не считается: это копия
# движка, она бывает и в домашней папке пользователя (`~/.aurora`), и тогда любой
# репозиторий под домом выглядел бы вложенным. Нет маркеров — корнем считается сам
# каталог, и вложенность остаётся неразрешённой границей (пишущие операции откажут).
PROJECT_MARKERS = ("aurora.config.yaml", "AuroraKnowledgeDB")


def _project_root(path: Path, limit: Path | None) -> Path:
    """Ближайший вверх корень проекта, не выходя за `limit` (верх git или сам путь)."""
    for folder in (path, *path.parents):
        if any((folder / marker).exists() for marker in PROJECT_MARKERS):
            return folder
        if limit is not None and folder == limit:
            break
    return path


def project_root(root) -> Path:
    """Ближайший вверх корень проекта Авроры в границах его git-репозитория."""
    path = Path(root).resolve()
    top = _toplevel(path)
    return _project_root(path, top.resolve() if top else path)


@dataclass(frozen=True)
class Rule:
    """Одно правило реестра: путь, вид, снимать ли с индекса и почему он локальный."""
    path: str
    kind: str
    untrack: bool
    why: str = ""


_LOCAL: tuple[Rule, ...] = (
    Rule("AuroraKnowledgeDB/meta/run_log.md", LOCAL_PATH, True, "журнал запусков панели"),
    Rule("AuroraKnowledgeDB/meta/recent-files.json", LOCAL_PATH, True, "личная история открытий"),
    Rule("AuroraKnowledgeDB/meta/net-runs.d", LOCAL_DIR, True, "снимки сети этой машины"),
    Rule("AuroraKnowledgeDB/meta/retrieval-last.json", LOCAL_PATH, True, "точка сравнения выдачи"),
    Rule("AuroraKnowledgeDB/meta/graph.json", LOCAL_PATH, True, "производный граф карточек"),
    Rule("AuroraKnowledgeDB/.obsidian/workspace.json", LOCAL_PATH, True, "личное состояние Obsidian"),
    Rule("AuroraKnowledgeDB/.obsidian/workspace-mobile.json", LOCAL_PATH, True, "личное состояние Obsidian"),
    Rule("AuroraKnowledgeDB/.obsidian/app.json", LOCAL_PATH, True, "личное состояние Obsidian"),
    Rule("AuroraKnowledgeDB/.obsidian/appearance.json", LOCAL_PATH, True, "личное состояние Obsidian"),
    Rule("AuroraKnowledgeDB/.obsidian/graph.json", LOCAL_PATH, True, "личное состояние Obsidian"),
    Rule("AuroraKnowledgeDB/.obsidian/hotkeys.json", LOCAL_PATH, True, "личное состояние Obsidian"),
    Rule("AuroraKnowledgeDB/.obsidian/bookmarks.json", LOCAL_PATH, True, "личное состояние Obsidian"),
    Rule("AuroraKnowledgeDB/.obsidian/starred.json", LOCAL_PATH, True, "личное состояние Obsidian"),
    Rule("AuroraKnowledgeDB/.obsidian/plugins/recent-files-obsidian/data.json", LOCAL_PATH, True,
         "личная история открытий"),
    Rule("AuroraKnowledgeDB/meta/aurora_version.txt", LOCAL_PATH, True, "штамп версии этой машины"),
    Rule("start-aurora.bat", LOCAL_PATH, True, "пусковой файл этой машины"),
    Rule("start-aurora.command", LOCAL_PATH, True, "пусковой файл этой машины"),
    # Производные от карточек: пересобираются командами движка, у каждого участника свои.
    Rule("AuroraKnowledgeDB/meta/embeddings.bin", LOCAL_PATH, True, "семантический индекс"),
    Rule("AuroraKnowledgeDB/meta/embeddings.json", LOCAL_PATH, True, "семантический индекс"),
    Rule("AuroraKnowledgeDB/meta/graphify", LOCAL_DIR, True, "граф базы наружу"),
)

_CLONE = Rule("Code/*", CLONE_GLOB, True, "клоны репозиториев кода")

# Остальные строки блока — правила .gitignore, которые индекс не трогают: снимать чужой
# кэш, окружение или harness-папки с учёта движок права не имеет.
_EXPLICIT = {r.path.rstrip("/") for r in _LOCAL}
RULES: tuple[Rule, ...] = _LOCAL + (_CLONE,) + tuple(
    Rule(line.strip(), IGNORE_ONLY, False)
    for line in GITIGNORE_BLOCK.splitlines()
    if line.strip() and not line.strip().startswith("#")
    and line.strip().rstrip("/") not in _EXPLICIT
    and line.strip() != _CLONE.path
)

# Стабильный порядок без шаблонов: part_184 создаёт каждый путь буквально.
LOCAL_STATE_RELS: tuple[str, ...] = tuple(r.path for r in _LOCAL)
LOCAL_STATE_DIRS: tuple[str, ...] = tuple(r.path for r in _LOCAL if r.kind == LOCAL_DIR)


def gitignore_block() -> str:
    """Блок правил `.gitignore` дословно: те же строки, порядок и комментарии."""
    return GITIGNORE_BLOCK


def _patterns() -> tuple[str, ...]:
    """Шаблоны, объявленные реестром, — ровно те строки, что git увидит в `.gitignore`."""
    return tuple(line for line in gitignore_block().splitlines() if line and not line.startswith("#"))


def validate() -> list:
    """Сверка реестра с блоком: у каждого снимаемого пути есть своя строка. → расхождения.

    Вызывается тестом; побочных эффектов при импорте модуль не имеет.
    """
    lines = set(GITIGNORE_BLOCK.splitlines())
    return [r.path for r in RULES
            if r.untrack and r.path not in lines and (r.path + "/") not in lines]


def rules_within(scope: str) -> tuple:
    """Пути политики внутри каталога `scope`: их нельзя коммитить ни при каком `.gitignore`.

    Коммит результата агента собирается из временного индекса, а не из настоящего: в HEAD
    локальный путь мог остаться отслеживаемым (чекпойнт снял его только с индекса). Список
    берётся из реестра, а не из `untrack_local`: тот смотрит на настоящий индекс и о записи
    в HEAD ничего не знает. `scope` — путь от корня репозитория без завершающего слэша.
    """
    scope = scope.rstrip("/")
    return tuple(r.path for r in RULES
                 if r.untrack and (r.path == scope or r.path.startswith(scope + "/")))


# ------------------------------------------------------------------ запись правил

def _gitignore_lines(text: str) -> set[str]:
    # Сравниваем строки, а не слова: начальный и экранированный конечный пробелы значимы.
    return set(text.replace("\r\n", "\n").split("\n"))


def _read_gitignore(path: Path) -> str | None:
    """Прочитать `.gitignore` так, чтобы запись вернула байты прежними. → None, если нельзя.

    Строгое чтение `utf-8` падало на файле не в UTF-8 (блокнот Windows, cp1251), и ошибку
    принимали за пустой файл: блок кита затирал чужие правила целиком. `surrogateescape`
    держит непонятные байты как есть — построчный merge их не двигает, а запись возвращает
    байт в байт. `None` — файл не прочитать (нет прав, на месте файла каталог): писать в
    него тем более нельзя, иначе потеряется ровно то, что не удалось прочитать.
    """
    try:
        return path.read_bytes().decode("utf-8", "surrogateescape")
    except FileNotFoundError:
        return ""
    except OSError:
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    """Записать целиком, подменив файл: оборванная запись не оставит половину правил.

    Прямой перезаписи при сбое нет: `path.write_bytes` на том же месте оставил бы
    половину файла, а `os.replace` подменяет его целиком. Временный файл получает права
    прежнего, а симлинк не разыменовывается — подменяется сама запись каталога.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gitignore.", suffix=".aurora-tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            want = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            want = None
        if want is not None:
            try:
                os.chmod(tmp, want)
            except OSError:
                pass
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)


def _write_rules(path: Path, text: str) -> None:
    """Записать правила через общий atomic-write движка; без него — своим запасным.

    `part_40` подменяет `aurora_fs.write_atomic` и должен видеть запись `.gitignore`, иначе
    обрыв обновления на этой записи нечем проверить. `local_paths` при этом остаётся
    stdlib-импортируемым: движок подключается только в момент записи.

    Байтами и с `surrogateescape`: текст, прочитанный из `.gitignore` не в UTF-8, содержит
    суррогаты, и запись обязана вернуть их прежними байтами. `write_text_atomic` строгая
    (`strict=True`) и такой текст отвергает — зовём `aurora_fs.write_atomic` напрямую.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8", "surrogateescape")
    try:
        from aurora_fs import write_atomic
    except ImportError:
        # Движок прежнего выпуска без общего atomic-write: пишем своим запасным.
        _write_atomic(path, data)
    else:
        try:
            # `follow_links=False`: правила не должны дописываться в файл за пределами
            # проекта, на который указывает `.gitignore`-симлинк.
            write_atomic(path, data, follow_links=False)
        except TypeError:
            # Прежний `write_atomic` без параметра: симлинк уже отсечён в `ensure_gitignore`.
            write_atomic(path, data)


def _merge_gitignore(path: Path, *, allow_write: bool = True) -> dict:
    """Одна операция чтения—записи—проверки правил. → `{added, ok, detail}`.

    Построчный merge: комментарий едет со своим правилом, чужие строки не двигаются и
    не переставляются, повторный прогон ничего не дописывает. Нечитаемый файл не
    переписывается: чужие правила дороже недостающих правил кита.

    Успех — только после перечитывания файла: `[]` значит и «уже стоят», и «симлинк», и
    «не прочитать», и «запись оборвалась». Установщик обязан различать эти состояния до
    первой служебной записи, поэтому `ok` выводится из проверки, а не из пустого списка.
    OSError записи не глотается: `ensure_gitignore` пробрасывает его вызывающему.
    """
    if path.is_symlink():
        # За ссылкой файл вне проекта: правила туда не пишутся и «уже стоят» не значат.
        return {"added": [], "ok": False, "detail": "на месте .gitignore симлинк"}
    cur = _read_gitignore(path)
    if cur is None:
        return {"added": [], "ok": False, "detail": REASONS["gitignore_unreadable"]}
    have = _gitignore_lines(cur)
    add, notes = [], []
    for ln in gitignore_block().splitlines():
        if not ln:
            notes = []
        elif ln.startswith("#"):
            notes.append(ln)          # комментарий поедет вместе со своим правилом
        elif ln not in have:
            add += notes + [ln]
            notes = []
        else:
            notes = []
    added = [ln for ln in add if not ln.startswith("#")]
    if add:
        if not allow_write:
            return {"added": [], "ok": False,
                    "detail": "в .gitignore есть проиндексированные правки; сначала проверьте их"}
        # Исходные байты сохраняются целиком: `rstrip()` съедал значимый экранированный
        # пробел последнего правила (`secret\ `). Добавляем только разделитель.
        head = "" if not cur else cur + ("" if cur.endswith("\n") else "\n") + "\n"
        text = head + "# Aurora — правила движка\n" + "\n".join(add) + "\n"
        _write_rules(path, text)
        after = _read_gitignore(path)
        if after is None:
            return {"added": added, "ok": False, "detail": REASONS["gitignore_unreadable"]}
        missing = sorted(set(_patterns()) - _gitignore_lines(after))
        if missing:
            return {"added": added, "ok": False,
                    "detail": "правила не записались: " + ", ".join(missing[:3])}
    return {"added": added, "ok": True, "detail": ""}


def ensure_gitignore(path) -> list:
    """Дописать в `.gitignore` недостающие правила кита. → добавленные строки.

    Отказ записи (OSError) пробрасывается: вызывающий должен узнать, что правила не легли.
    """
    return _merge_gitignore(Path(path))["added"]


def ensure_gitignore_report(path, *, allow_write: bool = True) -> dict:
    """Установить правила и честно сказать, установились ли они. → `{added, ok, detail}`.

    Отказ (`ok=False`) — повод остановиться до первой служебной записи и не трогать чужой
    файл или ссылку. Проверка одна на чтение, запись и перечитывание: второго чтения, из
    которого успех выводился бы отдельно, больше нет.
    """
    try:
        return _merge_gitignore(Path(path), allow_write=allow_write)
    except OSError as e:
        return {"added": [], "ok": False, "detail": f"правила не записаны: {e}"}


# ------------------------------------------------------------------------- git

# Переменные, которые уводят git в другой репозиторий, дерево или индекс. Коммит панели
# (`git_sync._env`) их уже выбрасывает; реестр обязан смотреть ровно туда же, иначе очистка
# проверит один индекс, а `add`/`commit` возьмут другой.
GIT_CONTEXT_VARS = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")
READ_ONLY_GIT = {"GIT_OPTIONAL_LOCKS": "0"}
GIT_ERRORS = (OSError, subprocess.SubprocessError)


def _git_env(extra: dict | None = None) -> dict:
    """Окружение git без подмены контекста; `extra` — только намеренная подмена (временный индекс)."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_CONTEXT_VARS}
    if extra:
        env.update(extra)
    return env


def _git(root, *args: str, data: bytes | None = None,
         env: dict | None = None) -> subprocess.CompletedProcess:
    """git из корня, аргументами списком и байтами на вводе: оболочки и локали не касаемся.

    `env` — только намеренная подмена (частный индекс для подготовки); переменные, уводящие
    git в другой репозиторий, дерево или индекс, вычищаются всегда.
    """
    return subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=off", *args],
                          input=data, capture_output=True, timeout=60, env=_git_env(env))


def _z(data: bytes) -> list:
    # `surrogateescape`, не `replace`: имя файла из непонятных байтов должно вернуться в git
    # теми же байтами, иначе снятие с индекса ищет несуществующий путь.
    return [x for x in data.decode("utf-8", "surrogateescape").split("\0") if x]


def _toplevel(root, env: dict | None = None) -> Path | None:
    """Корень репозитория: `add` и `rm --cached` считают пути от него, не от подпапки.

    `None` — git не ответил или это не репозиторий. Возвращать в этом случае `root` значило
    бы выдать «не удалось определить» за корень и смотреть не туда.
    """
    p = _git(root, "rev-parse", "--show-toplevel", env=env)
    if p.returncode == 0:
        out = p.stdout.decode("utf-8", "surrogateescape").strip()
        if out:
            return Path(out)
    return None


@dataclass(frozen=True)
class _Scope:
    """Граница проекта внутри git: git-корень, корень проекта и префикс проекта от него."""
    base: Path
    project: Path
    prefix: str
    ok: bool
    detail: str = ""


def _scope(root) -> _Scope:
    """Определить git-корень, корень проекта и префикс. → `ok=False`, если не разрешается.

    `root` — путь, с которого начали (проект, его подпапка или чужой каталог). Корень
    проекта ищется вверх по маркерам Авроры: `kit:update` вложенного проекта
    (`repo/projects/demo` без своего `.git`) пишет правила в `demo/.gitignore`, а индекс
    принадлежит родителю. Политика обязана применяться к `projects/demo/...`, а не к
    `repo/Code/*`: иначе обновление demo снимает с индекса исходники родителя. Запуск из
    подпапки своего проекта корнем считает проект, а не подпапку. Неразрешимая граница —
    отказ, а не «нарушений нет».
    """
    base = _toplevel(root)
    project = _project_root(Path(root).resolve(), base.resolve() if base else Path(root).resolve())
    if base is None:
        # Репозитория нет вовсе: это не «непонятная граница», а нечитаемый индекс.
        return _Scope(project, project, "", False, REASONS["index_unreadable"])
    try:
        top = base.resolve()
        rel = project.relative_to(top)
    except (OSError, ValueError):
        return _Scope(base, project, "", False, REASONS["scope_unresolved"])
    prefix = "" if rel == Path(".") else rel.as_posix()
    return _Scope(top, project, prefix, True, "")


def boundary(root) -> dict:
    """Граница пишущей git-операции → `{ok, project, repo, prefix, nested, detail}`.

    Пишущие операции (коммит, обновление, отправка и чекпойнт) не отождествляют проект с
    корнем репозитория: вложенный проект в чужом репозитории они не трогают вовсе. Отказ до
    мутации лучше коммита, который снимает с индекса исходники родителя.
    """
    scope = _scope(root)
    if not scope.ok:
        return {"ok": False, "project": str(scope.project), "repo": str(scope.base),
                "prefix": "", "nested": False,
                "detail": scope.detail or REASONS["scope_unresolved"]}
    nested = bool(scope.prefix)
    return {"ok": True, "project": str(scope.project), "repo": str(scope.base),
            "prefix": scope.prefix, "nested": nested,
            "detail": (f"проект вложен в чужой git-репозиторий ({scope.prefix}): "
                       "операция Git затронула бы ветку или пути родителя; "
                       "перенесите .git в корень проекта" if nested else "")}


def _repo_rel(prefix: str, rel: str) -> str:
    """Путь политики (от проекта) → путь индекса (от корня репозитория)."""
    return f"{prefix}/{rel}" if prefix else rel


def _ls_files(root, env: dict | None = None) -> list:
    p = _git(root, "ls-files", "-z", env=env)
    return _z(p.stdout) if p.returncode == 0 else []


def _index_entries(root, env: dict | None = None) -> list | None:
    """Записи индекса (режим, путь) — как их видит git. → None, если индекс не читается.

    Режим нужен, чтобы отличить клон от файла человека: `Code/acme` бывает gitlink'ом
    (160000) настоящего клона, каталогом (040000) и обычным файлом (100644). Одинарная
    запись без режима превращала `Code/notes.md` в клон и снимала его с индекса.
    """
    p = _git(root, "ls-files", "-s", "-z", env=env)
    if p.returncode != 0:
        return None
    out = []
    for rec in p.stdout.split(b"\0"):
        if not rec:
            continue
        meta, _, path = rec.partition(b"\t")
        parts = meta.split()
        if len(parts) < 3 or not path:
            continue
        out.append((parts[0].decode("ascii", "replace"),
                    path.decode("utf-8", "surrogateescape")))
    return out


class IndexRefusal(Exception):
    """Невозможно доказать, что операция сохранит индекс человека."""

    def __init__(self, detail: str, *, lock: bool = False, step: str = "index"):
        super().__init__(detail)
        self.lock = lock
        self.step = step


INDEX_ERRORS = (*GIT_ERRORS, IndexRefusal)


def _index_git(root, *args: str, env: dict | None = None) -> bytes:
    p = _git(root, *args, env={**READ_ONLY_GIT, **(env or {})})
    if p.returncode:
        raise IndexRefusal(f"{args[0]}: " + p.stderr.decode("utf-8", "replace").strip())
    return p.stdout


def _index_identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns, info.st_ctime_ns


def _read_index(path: Path) -> tuple[tuple, bytes] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None  # Не путать отсутствующий индекс с непригодным или нечитаемым.
    if not stat.S_ISREG(info.st_mode):
        raise IndexRefusal("индекс git не является обычным файлом")
    identity = _index_identity(info)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                 | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as stream:
        data = stream.read()
        if identity != _index_identity(os.fstat(stream.fileno())) or identity != _index_identity(path.lstat()):
            raise IndexRefusal("индекс git изменился во время чтения")
    return identity, data


def _head_position(root) -> tuple[bytes, str]:
    ref = _git(root, "symbolic-ref", "-q", "HEAD", env=READ_ONLY_GIT)
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", env=READ_ONLY_GIT)
    if ref.returncode not in (0, 1) or head.returncode not in (0, 1):
        raise IndexRefusal("не удалось определить HEAD")
    if head.returncode:
        # ref отсутствует в новом репозитории; существующая, но битая ссылка не unborn.
        name = ref.stdout.rstrip(b"\r\n").decode("utf-8", "surrogateescape")
        if ref.returncode or not name or _index_git(root, "for-each-ref", "--format=%(refname)", name):
            raise IndexRefusal("не удалось прочитать HEAD")
    return ref.stdout, head.stdout.decode("ascii").strip()


def _take_index_lock(path: Path, pause, retries: int) -> os.stat_result:
    for n in range(retries + 1):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        except FileExistsError:
            if n == retries:
                raise IndexRefusal("index.lock занят (File exists)", lock=True) from None
            pause(n + 1)
        else:
            try:
                return os.fstat(fd)
            finally:
                os.close(fd)
    raise AssertionError("index.lock retries must be nonnegative")


@dataclass(frozen=True)
class CommitAttempt:
    sha: str = ""
    error: str = ""
    warning: str = ""


@dataclass(frozen=True)
class IndexTransaction:
    root: str
    path: Path
    lock: Path
    owner: os.stat_result
    original: tuple[tuple, bytes] | None
    position: tuple[bytes, str]
    temp: Path

    @property
    def head(self) -> str:
        return self.position[1]

    def env(self, name: str = "index") -> dict:
        return {**READ_ONLY_GIT, "GIT_INDEX_FILE": str(self.temp / name)}

    def owns_lock(self) -> bool:
        try:
            current = self.lock.lstat()
        except FileNotFoundError:
            return False
        return stat.S_ISREG(current.st_mode) and os.path.samestat(self.owner, current)

    def verify(self, position: tuple[bytes, str]) -> None:
        if not self.owns_lock() or _read_index(self.path) != self.original:
            raise IndexRefusal("индекс git изменился параллельно; чужой stage оставлен как есть")
        if _head_position(self.root) != position:
            raise IndexRefusal("HEAD изменился параллельно")

    def _publish(self, candidate: Path, position: tuple[bytes, str]) -> None:
        self.verify(position)
        data = _read_index(candidate)
        if data is None:
            raise IndexRefusal("подготовленный индекс отсутствует")
        fd = os.open(self.lock, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "wb") as stream:
            if not os.path.samestat(self.owner, os.fstat(stream.fileno())):
                raise IndexRefusal("владелец index.lock изменился; исходный индекс не заменён")
            stream.write(data[1])
            stream.flush()
            os.fsync(stream.fileno())
        if not self.owns_lock():
            raise IndexRefusal("владелец index.lock изменился; исходный индекс не заменён")
        if self.original is not None:
            os.chmod(self.lock, stat.S_IMODE(self.original[0][2]))
        os.utime(self.lock, ns=(data[0][3], data[0][3]))
        os.replace(self.lock, self.path)

    def commit(self, invoke, finish) -> CommitAttempt:
        """Git сам пишет ref и reflog; индекс публикуем только после успешного Git commit.

        После запуска commit нельзя обещать отказ без нового коммита, пока не прочитан
        HEAD. Если commit уже состоялся, ошибка публикации не является отказом коммита.
        """
        self.verify(self.position)
        try:
            rc, out, err = invoke()
            detail = (out + "\n" + err).strip()
        except GIT_ERRORS as exc:
            rc, detail = 1, str(exc)
        try:
            after = _head_position(self.root)
        except INDEX_ERRORS as exc:
            return CommitAttempt(warning=f"исход git commit не проверен: {exc}; проверьте HEAD и индекс")
        if after == self.position:
            return CommitAttempt(error=detail or "git commit не изменил HEAD")
        if rc or not after[1] or after[0] != self.position[0]:
            return CommitAttempt(warning="HEAD изменился, исход git commit не подтверждён; "
                                 "исходный индекс не заменён: " + detail)
        try:
            parents = _index_git(self.root, "rev-list", "--parents", "-n", "1", after[1]).decode("ascii").split()
            if parents != [after[1], *([self.head] if self.head else [])]:
                raise IndexRefusal("новый HEAD не является ожидаемым потомком")
        except INDEX_ERRORS as exc:
            return CommitAttempt(warning=f"исход git commit не проверен: {exc}; проверьте HEAD и индекс")
        try:
            self._publish(finish(), after)
        except INDEX_ERRORS as exc:
            return CommitAttempt(sha=after[1], warning=f"коммит создан, исходный индекс не заменён: {exc}")
        return CommitAttempt(sha=after[1])


@contextmanager
def index_transaction(root: str, *, pause=None, retries: int = 0):
    """Удержать Git index.lock от проверки версии до публикации либо отказа."""
    name = _index_git(root, "rev-parse", "--git-path", "index").rstrip(b"\r\n")
    if not name:
        raise IndexRefusal("путь к индексу git не определён")
    path = Path(root) / os.fsdecode(name)
    lock = path.with_name(path.name + ".lock")
    original, position = _read_index(path), _head_position(root)
    owner = _take_index_lock(lock, pause, retries)
    tx = IndexTransaction(root, path, lock, owner, original, position, path.parent)
    try:
        tx.verify(position)
        split = _git(root, "config", "--bool", "--get", "core.splitIndex", env=READ_ONLY_GIT)
        if split.returncode not in (0, 1):
            raise IndexRefusal("не удалось проверить настройку core.splitIndex")
        if split.stdout.strip() == b"true" or _index_git(root, "rev-parse", "--shared-index-path").strip():
            raise IndexRefusal("split index: сохранность общего индекса не доказана")
        markers = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer")
        if any((path.parent / marker).exists() for marker in markers):
            raise IndexRefusal("git-операция уже начата; завершите её перед автоматическим коммитом")
        with tempfile.TemporaryDirectory(prefix="aurora-index-", dir=path.parent, ignore_cleanup_errors=True) as temp:
            tx = IndexTransaction(root, path, lock, owner, original, position, Path(temp))
            if original is not None:
                candidate = tx.temp / "index"
                candidate.write_bytes(original[1])
                # Git читает mtime самого индекса для racy-clean: новая метка копии
                # могла скрыть правку того же размера, сделанную в ту же секунду.
                os.utime(candidate, ns=(original[0][3], original[0][3]))
            yield tx
    finally:
        if tx.owns_lock():
            lock.unlink()


def policy_hits(entries: list, scope: str = "") -> list:
    """Пути индекса, которые политика реестра объявляет локальными.

    Решение принимается по записи индекса, а не по выигравшему шаблону `.gitignore`:
    пользовательское отрицание `!AuroraKnowledgeDB/meta/run_log.md` после блока кита
    перекрывает правило, и `check-ignore` честно говорит «не игнорируется». Но политика
    реестра от этого не меняется — путь всё равно локальный. Порядок: явные правила
    (`LOCAL_STATE_RELS`), затем клоны `Code/*` по алфавиту.

    Клон — это gitlink (160000), дерево (040000) или каталог с потомками; обычный файл
    `Code/<имя>` — работа человека, его не трогаем. `scope` — префикс проекта от корня
    репозитория: для вложенного проекта правила ищутся под ним, а не во всём родителе.
    Возвращаются пути от проекта, а не от корня репозитория.
    """
    prefix = scope.rstrip("/")
    paths = {p for _mode, p in entries}
    out: list[str] = []
    for rule in _LOCAL:
        target = _repo_rel(prefix, rule.path)
        if any(p == target or p.startswith(target + "/") for p in paths):
            out.append(rule.path)
    clones = set()
    code_prefix = _repo_rel(prefix, "Code") + "/"
    for mode, path in entries:
        if not path.startswith(code_prefix):
            continue
        parts = path[len(code_prefix):].split("/")
        if not parts or not parts[0] or parts[0] in CLONE_KEEP:
            continue
        name = "Code/" + parts[0]
        # Одинарная запись `Code/<имя>` — клон только по режиму: gitlink или дерево.
        if len(parts) > 1 or mode in ("160000", "040000"):
            clones.add(name)
    out.extend(sorted(clones))
    return out


def _in_paths(path: str, roots) -> bool:
    return any(path == p or path.startswith(p + "/") for p in roots)


def _policy_within(entries, prefix: str, within: str) -> list:
    hits = policy_hits(entries, prefix)
    return [p for p in hits if _in_paths(p, [within])] if within else hits


def _rm_cached(root, rel: str, env: dict) -> bool:
    """Снять путь с индекса, оставив файлы на диске. → снято ли что-нибудь."""
    # Только в частном индексе: перед очисткой исходные staged-версии уже проверены.
    # `:(literal)` — имя со `*` или `[` не должно читаться как шаблон.
    return _git(root, "rm", "-r", "-f", "--cached", "-q", "--",
                f":(literal){rel}", env=env).returncode == 0


def code_clones(root) -> list:
    """Каталоги клонов верхнего уровня под `Code/` → `Code/<имя>`, в порядке сортировки.

    Настоящий `git clone` (и `code:sync`) попадает в `ls-files` одной записью-ссылкой
    `Code/<имя>` без второго слэша — это gitlink, а не файлы. Требовать слэш внутри имени
    значило не видеть самый обычный клон; поэтому имя берётся так же, как в doctor.
    Обычный файл `Code/notes.md` клоном не считается: его режим — 100644, а не 160000.
    """
    scope = _scope(root)
    if not scope.ok:
        return []
    entries = _index_entries(scope.base)
    if entries is None:
        return []
    return [p for p in policy_hits(entries, scope.prefix) if p.startswith("Code/")]


def untrack_local(root, env: dict | None = None, *, within: str = "") -> list:
    """Снять с индекса всё объявленное локальным. → пути в порядке `LOCAL_STATE_RELS` + клоны.

    Файлы на диске не трогаются. Повторный вызов возвращает `[]`: снимать больше нечего.
    Список берётся из фактических записей индекса, а не из выигравших шаблонов: правило,
    перекрытое пользовательским отрицанием, всё равно не делает путь общим. `env` — частный
    индекс подготовки: настоящий индекс остаётся индексом человека.
    """
    if not (env and env.get("GIT_INDEX_FILE")):
        return enforce_index(root, within=within)["removed"]
    scope = _scope(root)
    if not scope.ok:
        return []
    entries = _index_entries(scope.base, env)
    if entries is None:
        return []
    return [rel for rel in _policy_within(entries, scope.prefix, within)
            if _rm_cached(scope.base, _repo_rel(scope.prefix, rel), env)]


# Причины, по которым проверка индекса и правил не выполнена (0003). Ключ — для кода,
# текст — для человека: подставлять в сообщение ключ значило бы показать его на экране.
# Тексты русские, как у всех сообщений движка; английские лежат в каталоге
# `cockpit/i18n/data/en.json` → `messages`, и панель переводит вложенную причину на
# границе вывода (`translate_message`). `kit_i18n.message_skeletons` собирает эти строки
# из таблицы — иначе перевод причины считался бы лишним и `kit:i18n --check` краснел бы.
REASONS = {
    "index_unreadable": "индекс git не читается",
    "index_not_reread": "индекс git не перечитан после очистки",
    "scope_unresolved": "граница проекта и git не определена",
    "gitignore_unreadable": ".gitignore не читается",
    "check_ignore_failed": "git check-ignore не отработал",
}


def enforce_index(root, env: dict | None = None, *, within: str = "") -> dict:
    """Снять локальные пути с индекса, не меняя файлы в рабочем дереве.

    → `{"removed", "remaining", "checked", "detail"}`. `checked=False` — индекс не прочитался
    или не перечитался: это не «нарушений нет», и коммитить по такому ответу нельзя.
    `remaining` — пути политики, оставшиеся в индексе после снятия: тоже отказ, а не успех.
    `env` — частный индекс подготовки, который проверяется и чистится вместо настоящего.
    """
    try:
        scope = _scope(root)
        if not scope.ok:
            raise IndexRefusal(scope.detail or REASONS["scope_unresolved"])
        if env and env.get("GIT_INDEX_FILE"):
            return _enforce_index(scope, env, within)
        entries = _index_entries(scope.base, READ_ONLY_GIT)
        if entries is None:
            raise IndexRefusal(REASONS["index_unreadable"])
        if not _policy_within(entries, scope.prefix, within):
            return {"removed": [], "remaining": [], "checked": True, "detail": ""}
        # При необходимости записи перечитываем под замком, а не чистим по старому снимку.
        with index_transaction(str(scope.base)) as tx:
            report = _enforce_index(scope, tx.env(), within, tx=tx)
            if report["checked"] and not report["remaining"] and report["removed"]:
                tx._publish(tx.temp / "index", tx.position)
            else:
                report["removed"] = []  # частичная очистка не опубликована
            return report
    except INDEX_ERRORS as exc:
        return {"removed": [], "remaining": [], "checked": False, "detail": str(exc)}


def _enforce_index(scope: _Scope, env: dict, within: str, *, tx: IndexTransaction | None = None) -> dict:
    entries = _index_entries(scope.base, env)
    if entries is None:
        return {"removed": [], "remaining": [], "checked": False, "detail": REASONS["index_unreadable"]}
    local = _policy_within(entries, scope.prefix, within)
    if tx is not None and local:
        paths = [_repo_rel(scope.prefix, rel) for rel in local]
        staged = _staged_images(tx, _repo_rel(scope.prefix, within).rstrip("/"))
        protected = {p: row for p, row in staged.items() if not row.endswith(" D") and _in_paths(p, paths)}
        if protected:
            _check_stage_worktree(tx, protected, _index_tags(tx))
    removed = [rel for rel in local
               if _rm_cached(scope.base, _repo_rel(scope.prefix, rel), env)]
    after = _index_entries(scope.base, env)
    if after is None:
        return {"removed": removed, "remaining": [], "checked": False,
                "detail": REASONS["index_not_reread"]}
    return {"removed": removed, "remaining": _policy_within(after, scope.prefix, within),
            "checked": True, "detail": ""}


def run_index_step(run, args, env: dict) -> str:
    rc, out, err = run(args, env)
    if rc:
        raise IndexRefusal((out + "\n" + err).strip() or f"git завершился кодом {rc}",
                           lock="index.lock" in err, step=args[0])
    return out


_DIFF_INDEX = ("--no-renames", "--no-ext-diff", "--no-textconv", "--ignore-submodules=none")


def _staged_images(tx: IndexTransaction, within: str) -> dict:
    pathspec = ("--", f":(literal){within}") if within else ("--",)
    raw = _index_git(tx.root, "diff", "--cached", "--raw", "--no-abbrev", "--ita-invisible-in-index",
                     "-z", *_DIFF_INDEX, *pathspec, env=tx.env())
    fields = _z(raw)
    if len(fields) % 2 or any(not row.startswith(":") or row.endswith(" U") for row in fields[::2]):
        raise IndexRefusal("не удалось полностью прочитать staged-версии")
    return dict(zip(fields[1::2], fields[::2]))


def _check_stage(paths) -> None:
    if paths:
        raise IndexRefusal("проиндексированная версия отличается от рабочей; "
                           "stage сохранён: " + ", ".join(sorted(paths)[:5]))


def _index_tags(tx: IndexTransaction) -> list:
    tags = _z(_index_git(tx.root, "ls-files", "-v", "-z", env=tx.env()))
    if any(len(tag) < 3 or tag[1] != " " for tag in tags):
        raise IndexRefusal("не удалось полностью прочитать флаги индекса")
    return tags


def _check_stage_worktree(tx: IndexTransaction, staged: dict, tags: list) -> None:
    if not staged:
        return
    dirty = set(_z(_index_git(tx.root, "diff", "--name-only", "-z", *_DIFF_INDEX, "--", env=tx.env())))
    # skip-worktree и assume-unchanged могут скрыть несовпадение от diff.
    dirty.update(tag[2:] for tag in tags if tag[0] == "S" or tag[0].islower())
    # Для staged-удаления восстановленный на диске файл уже untracked: diff его не покажет.
    dirty.update(p for p, row in staged.items() if row.endswith(" D") and os.path.lexists(Path(tx.root) / p))
    _check_stage(staged.keys() & dirty)


def _clean_index(tx: IndexTransaction, within: str) -> list:
    enforced = enforce_index(tx.root, env=tx.env(), within=within)
    if not enforced["checked"] or enforced["remaining"]:
        raise IndexRefusal(enforced["detail"] or ", ".join(enforced["remaining"][:5]), step="policy")
    return enforced["removed"]


def _check_new_ignored(tx: IndexTransaction, within: str) -> None:
    """Новый кандидат для add должен быть исключён, если его закрывает блок кита."""
    args = ("ls-files", "--others", "-z")
    scope = ("--", f":(literal){within}") if within else ("--",)
    visible = set(_z(_index_git(tx.root, *args, "--exclude-standard", *scope, env=tx.env())))
    if not visible:
        return
    # Пусть Git применит те же шаблоны без правил проекта. Учитываем порядок и отрицания
    # самого блока, а отслеживаемые файлы не считаем новыми даже при смене их содержимого.
    patterns = tuple(f"--exclude={line}" for line in _patterns())
    canonical = _z(_index_git(tx.root, *args, *patterns, *scope, env=tx.env()))
    if exposed := visible.difference(canonical):
        raise IndexRefusal(".gitignore: новые локальные пути не исключены: " + ", ".join(sorted(exposed)[:5]))


def prepare_stage(tx: IndexTransaction, run, *, within: str = "") -> list:
    """Стадия существует только в частном индексе. Полностью staged-файлы допустимы.

    Обычный `add` не должен заменить отличающуюся версию, ранее отобранную человеком.
    Локальные пути самого кита снимаются по прежней политике; не по шаблону человека.
    """
    env = tx.env()
    tags = _index_tags(tx)
    if _index_git(tx.root, "ls-files", "-u", "-z", env=env):
        raise IndexRefusal("индекс содержит неразрешённые конфликты")
    entries = _index_entries(tx.root, env)
    if entries is None:
        raise IndexRefusal(REASONS["index_unreadable"])
    staged = _staged_images(tx, within)
    # Удаление локального пути, подготовленное прошлым успешным чекпойнтом, уже
    # отсутствует в ls-files. Старый режим из diff сохраняет различие файла и gitlink.
    removed = [(row[1:].split(" ", 1)[0], p) for p, row in staged.items() if row.endswith(" D")]
    policy = _policy_within([*entries, *removed], "", within)
    shared = {p: row for p, row in staged.items() if not _in_paths(p, policy)}
    # Снятие локального пути не должно уничтожить его единственную staged-версию.
    # Снятие, уже сделанное раньше, такой версии не содержит.
    _check_stage_worktree(tx, {p: row for p, row in staged.items() if p in shared or not row.endswith(" D")}, tags)

    # .gitignore вне scope коммита базы тоже может быть частью stage человека.
    rules_staged = ".gitignore" in staged or (within and _staged_images(tx, ".gitignore"))
    rules = ensure_gitignore_report(Path(tx.root) / ".gitignore", allow_write=not rules_staged)
    if not rules["ok"]:
        raise IndexRefusal(f".gitignore: {rules['detail']}")
    _check_new_ignored(tx, within)

    local = untrack_local(tx.root, env=env, within=within)
    args = ["add", "--", within] if within else ["add", "-A"]
    run_index_step(run, args, env)
    local += [p for p in _clean_index(tx, within) if p not in local]
    after = _staged_images(tx, within)
    _check_stage([p for p, row in shared.items() if after.get(p) != row])
    return local


def exclude_local_deletions(tx: IndexTransaction, run, files: list, local: list) -> list:
    """Не называть локальное в коммите панели/чекпойнта, даже как удаление."""
    gone = [p for p in files if _in_paths(p, local)]
    kept = [p for p in files if p not in gone]
    if kept and gone and tx.head:
        run_index_step(run, ["restore", "--staged", f"--source={tx.head}", "--",
                             *(f":(literal){p}" for p in gone)], tx.env())
    return kept


def finished_index(tx: IndexTransaction, local: list) -> Path:
    # Git мог обновить частный индекс штатным хуком (например, lint_baseline.txt).
    if local:
        _clean_index(tx, "")
    return tx.temp / "index"


def tracked_local(root, untrack_only: bool = False) -> list:
    """Отслеживаемые файлы, которые закрывает правило кита. Нет git — пусто.

    Решение принимает git: `check-ignore` называет сработавший шаблон, и путь попадает
    в ответ, только если этот шаблон дословно объявлен реестром. Своё правило человека
    (`Workspaces/`) в ответ не попадает: его файлы — его работа.

    `untrack_only=True` — только пути, которые `untrack_local` действительно снимает:
    так doctor не считает блокером `ignore_only`-папки (`.cursor/`, `.opencode/`),
    которые обновление не трогает.
    """
    scope = _scope(root)
    if not scope.ok:
        return []
    if untrack_only:
        # Явная политика реестра, а не `check-ignore`: отрицание `!` перекрывает шаблон,
        # и путь, который агент обязан не коммитить, выглядел бы общим.
        entries = _index_entries(scope.base)
        return policy_hits(entries, scope.prefix) if entries is not None else []
    # Из корня проекта, а не репозитория: у вложенного проекта свои относительные пути,
    # и его `.gitignore` действует именно на них. Подпапка проекта корнем не считается.
    paths = _ls_files(scope.project)
    if not paths:
        return []
    data = b"\0".join(x.encode("utf-8", "surrogateescape") for x in paths) + b"\0"
    v = _git(scope.project, "check-ignore", "-v", "--no-index", "-z", "--stdin", data=data)
    if v.returncode not in (0, 1):
        return []
    recs = _z(v.stdout)
    known = set(_patterns())
    out: list[str] = []
    # `-z` печатает по четыре поля на запись: источник, номер строки, шаблон, путь.
    for i in range(0, len(recs) - 3, 4):
        source, _line, pattern, path = recs[i:i + 4]
        if not path or path in out:
            continue
        # Сработавший шаблон обязан быть из `.gitignore` самого проекта. Иначе чужое
        # `Code/*` во вложенном `.gitignore` (например, `Artifacts/foo/.gitignore`) делало
        # бы локальным чужой файл и звало снять его с учёта.
        if not _is_project_gitignore(scope.base, source, scope.project):
            continue
        # `-v` печатает и сработавшее отрицание (`!Code/README.md`), но такой путь не игнорируется.
        if not pattern or pattern.startswith("!") or pattern not in known:
            continue
        out.append(path)
    return out


def _is_project_gitignore(base: Path, source: str, project) -> bool:
    """Источник правила — `.gitignore` самого проекта (а не чужой вложенный файл)."""
    if not source:
        return False
    path = Path(source)
    if not path.is_absolute():
        path = base / path
    try:
        return path.resolve() == (Path(project) / ".gitignore").resolve()
    except OSError:
        return False


def missing_rules(root) -> list:
    """Локальные пути, правила для которых в `.gitignore` проекта нет. → пути реестра."""
    scope = _scope(root)
    expected = [r.path for r in RULES if r.untrack]
    if not scope.ok:
        return expected
    path = scope.project / ".gitignore"
    cur = _read_gitignore(path)
    have = set() if cur is None else _gitignore_lines(cur)
    return [rel for rel in expected if rel not in have and (rel + "/") not in have]


def _probe_path(rule: Rule) -> str:
    """Путь для `check-ignore`: каталог и шаблон проверяются потомком, файл — собой."""
    if rule.kind == CLONE_GLOB:
        return rule.path.replace("*", ".aurora-probe")
    if rule.kind == LOCAL_DIR:
        return rule.path + "/.aurora-probe"
    return rule.path


def _probe_rules(root, rules: tuple) -> dict | None:
    """Какой шаблон фактически решает судьбу каждого снимаемого пути. → {путь: шаблон}.

    `check-ignore -v --no-index` называет выигравший шаблон и его источник, поэтому
    отрицание `!` видно даже там, где текст правила в файле есть: файл `.gitignore`
    мог быть дописан человеком после блока кита. `None` — git не ответил: это не
    «правила на месте», и doctor должен сказать «проверка не выполнена».
    """
    probes = []
    owner: dict = {}
    for rule in rules:
        probe = _probe_path(rule)
        probes.append(probe)
        owner[probe] = rule.path
    data = b"\0".join(p.encode("utf-8", "surrogateescape") for p in probes) + b"\0"
    p = _git(root, "check-ignore", "-v", "--no-index", "-z", "--stdin", data=data)
    if p.returncode not in (0, 1):
        return None
    found: dict = {}
    recs = _z(p.stdout)
    for i in range(0, len(recs) - 3, 4):
        _source, _line, pattern, path = recs[i:i + 4]
        if path in owner:
            found[owner[path]] = pattern
    return {rule.path: found.get(rule.path, "") for rule in rules}


def findings(root) -> dict:
    """Что doctor говорит о локальных путях: только чтение.

    → `{"tracked", "missing_rules", "overridden", "checked", "detail", "problems"}`.
    Три состояния различаются явно: нарушений нет (`tracked`/`overridden` пусты), правило
    перекрыто (`overridden` — текст есть, но выиграло отрицание) и проверка не выполнена
    (`checked=False`: индекс или `.gitignore` не читаются, git не ответил).

    Доказанная находка в индексе не зависит от проверки правил: если `.gitignore` не
    читается, `tracked` всё равно называет путь, а `checked=False` говорит только о
    правилах. `problems` сводит причины по одному пути, чтобы doctor не печатал его
    дважды: `tracked`, `missing_rule`, `overridden`.
    """
    scope = _scope(root)
    out = {"tracked": [], "missing_rules": [], "overridden": [], "checked": True, "detail": "",
           "problems": {}}
    if not scope.ok:
        out.update(checked=False, detail=scope.detail or REASONS["scope_unresolved"])
        return out
    entries = _index_entries(scope.base)
    if entries is None:
        out.update(checked=False, detail=REASONS["index_unreadable"])
        return out
    out["tracked"] = policy_hits(entries, scope.prefix)
    text = _read_gitignore(scope.project / ".gitignore")
    if text is None:
        out.update(checked=False, detail=REASONS["gitignore_unreadable"])
        return out
    have = _gitignore_lines(text)
    expected = tuple(r for r in RULES if r.untrack)
    missing = [r.path for r in expected if r.path not in have and (r.path + "/") not in have]
    out["missing_rules"] = missing
    probe = _probe_rules(scope.project, expected)
    if probe is None:
        out.update(checked=False, detail=REASONS["check_ignore_failed"])
        return out
    out["overridden"] = [r.path for r in expected
                         if r.path not in missing
                         and (not probe.get(r.path) or probe[r.path].startswith("!"))]
    problems: dict = {}
    for rel in out["tracked"]:
        problems.setdefault(rel, []).append("tracked")
    for rel in missing:
        problems.setdefault(rel, []).append("missing_rule")
    for rel in out["overridden"]:
        problems.setdefault(rel, []).append("overridden")
    out["problems"] = problems
    return out
