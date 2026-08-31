# Installation & Rollout Function

## Description

Два документа, которые приводят Аврору в проект с разных сторон. `docs/INSTALL.md` — hands-on гайд развёртывания: prerequisites и точные команды для скелета, настройки и проверки. `docs/IMPLEMENTATION.md` — плейбук для лида / SA / BA, introducing фреймворк: зачем Аврора, инварианты и поэтапный rollout, где на каждом этапе что положить. Оба документа — на английском, язык документа совпадает с его аудиторией (тот, кто разворачивает, а не ежедневный пользователь команды).

## Key Features

- **Prerequisites и deploy** (`docs/INSTALL.md`) — Python 3.9+, права записи на целевой проект, локальный клон Aurora Studio. Рекомендуемый путь из корня кита — `python3 aurora.py new /absolute/path/to/your-project`: раскладывает trust-layer структуру, копирует движок и запускает **интерактивную настройку**, которая спрашивает всё проектное: **название/слаг** проекта (слаг имёнует sync-скиллы), **base URL, space** Confluence и **root-страницы для синка** (page ID), **base URL, project key** Jira и **дефолтный JQL**, bootstrap-порог.
- **Перезапуск настройки** (`docs/INSTALL.md`) — setup копируется в проект (`.opencode/scripts/aurora_setup.py`), поэтому любой может позже поправить или дополнить настройки: текущие значения подставлены, `Enter` сохраняет их.
- **Скриптуемый install** (`docs/INSTALL.md`) — скелетер ходит и flag-driven для CI / без промптов: `python3 scripts/install_aurora.py --target … --name … --jira-key … --confluence-space …` (плюс `--slug`, `--dry-run`, `--force`). Дефолтное поведение — **никогда не перезаписывать** существующие файлы, поэтому повторный запуск безопасен для добивания пропусков.
- **Что появляется в target** (`docs/INSTALL.md`) — `AGENTS.md`, `aurora.config.yaml`, `aurora.env.local.example` (копируется в gitignored `.env.aurora.local`), `.opencode/` (скиллы `aurora-vault` + sync-скиллы `confluence-sync-<Slug>` / `jira-export-<Slug>`, скрипты, `structure_dirs.txt`), зеркала `Sources/{Confluence,JIRA}/`, `Raw/{laws,contract,customer,project,meetings,examples}/`, `AuroraKnowledgeDB/`, `Artifacts/`, `Deliverables/{work,released}/`, `Workspaces/`, `Templates/`, `Prompts/`. Список папок фиксирован движком (`structure_dirs.txt`) и одинаков во всех проектах Авроры.
- **Проверка** (`docs/INSTALL.md`) — `aurora_doctor.py --structure`, `kb_lint.py --summary`, `aurora_hooks.py --install` (pre-commit храповик: текущее число ошибок фиксируется базой, которая может только уменьшаться). Для репозитория, где уже лежат документы, — одноразовая обслуживающая цепочка: `sync_audit.py` → `kb_fix.py --all` (dry-run) → `--apply` → `--dupes` + `--merge` → `aurora_stats.py --queue`.
- **Почему Аврора** (`docs/IMPLEMENTATION.md`) — LLM-агенты галлюцинируют, когда весь markdown выглядит одинаково; Аврора разделяет *доказательства* (`Raw/`, `Sources/` — неизменяемые или владение синка), *знание* (карточки `AuroraKnowledgeDB/` с `status` — фильтр доверия для промптов) и *продукты* (`Artifacts/`, `Deliverables/` — сгенерированное не возвращается в промпты как «истина»). Инварианты («никогда не нарушать») — в `.opencode/skills/aurora-vault/SKILL.md`.
- **Фазы rollout** (`docs/IMPLEMENTATION.md`) — Phase 0 *Install* (30–60 мин: commit скелета, доменные карточки пока не придумывать); Phase 1 *Evidence first* (1–3 дня, только реальные материалы: контракт/SoW/ТЗ → `Raw/contract/`, законы → `Raw/laws/`, транскрипты встреч → `Raw/meetings/`, AS-IS заказчика → `Raw/customer/`, живые заметки → `Raw/project/`); Phase 2 *First cards* (bootstrap через `build`/`ingest`, статус по умолчанию `imported`/`draft`, 5–10 golden questions, когда появятся verified-факты); Phase 3 *Verify gate* (владелец руками ставит `status: verified` + `owner` + `review_by`; `verified` — верхний статус базы, тихого перезаписывания нет); Phase 4 *Artifacts & SDD* (US → `Artifacts/us/`, AC → `Artifacts/ac/`, спеки → `AuroraKnowledgeDB/Specs/` + `spec-pack` для подрядчиков); Phase 5 *Hygiene* (еженедельно `garden` + `kb_lint`, ежемесячно `meta/metrics.md`, после крупных синков — `eval` по golden questions).
- **Миграция накопившейся базы** (`docs/IMPLEMENTATION.md`) — установка без `--force`, mapping легаси-папок (`Laws/` → `Raw/laws/`, `Transcripts/` → `Raw/meetings/`, корневая `JIRA/` → `Sources/JIRA/`, плоские карточки → разделы + апгрейд frontmatter, рабочие черновики → `Workspaces/<task>/`), предложенный mapping статусов, легаси-ID остаются в `aliases:`, чтобы wiki-ссылки продолжали работать.

## Related Documentation

### Source Files
- docs/INSTALL.md — deploy в проект: prerequisites, команды, first-week checklist, troubleshooting
- docs/IMPLEMENTATION.md — плейбук rollout для лида/SA/BA
- docs/readme/02-quickstart.md — вид того же старта глазами первого дня
- scripts/install_aurora.py — скелетер; флаги `--target`, `--name`, `--slug`, `--jira-key`, `--confluence-space`, `--force`, `--dry-run`
- aurora.py — точки входа `new` / `setup` / `update` и команды обслуживания

### Related Functions
- [Docs for Humans](./02-docs-for-humans.md) — quickstart парится с INSTALL: один и тот же старт, разные читатель
- [Engine Structure & Roadmap](./01-engine-structure-roadmap.md) — что deploy копирует в проект, описано там же

## Implementation Notes

`aurora.py new` — это цепочка, а не одна команда: `install_aurora.py` (раскладка) → `aurora_setup.py` (интерактивные вопросы) → `aurora_update.py --apply` (приведение движка к финальному конфигу и проставление версии) → `install_skills.py --apply` (скиллы в общий каталог агента). Без терминала (запуск из скрипта, панели или ассистента) setup идёт с `--non-interactive`, и конфиг добирается позже: `python3 aurora.py setup <target>`.

Те же точки входа фигурируют в таблице **Развёртывание** `docs/commands.md`; любую команду обслуживания можно звать и из кита: `python3 aurora.py <команда> <target> [флаги]`.

Антипаттерны плейбука: класть AI-черновик сразу в базу как `verified`; редактировать `Deliverables/released/` или `Raw/contract/`; кормить `Artifacts/` обратно в промпты как ground truth; удалять карточки вместо `supersede`; синк, пишущий за пределами `Sources/`.

---
*Last updated: 2026-08-28*
*Areas: aurora-studio, engine*
