# Aurora Studio — repository layout

```
Aurora/
├── README.md
├── LICENSE
├── VERSION                  # версия движка (semver)
├── structure_dirs.txt       # ФИКСИРОВАННАЯ схема папок проекта (единый источник правды)
├── engine_manifest.txt      # что именно обновляет `aurora.py update`
├── aurora.py                # точка входа: new/setup/update + обслуживание
├── scripts/
│   ├── install_aurora.py    # раскладка проекта (папки читает из structure_dirs.txt)
│   ├── aurora_setup.py      # интерактивная перезапускаемая настройка
│   ├── aurora_update.py     # обновление движка по манифесту
│   ├── kb_lint.py           # найти механические ошибки
│   ├── kb_fix.py            # починить: ссылки, гомоглифы, frontmatter, слияние двойников
│   ├── kb_queue.py          # очередь верификации по реальной ценности карточек
│   ├── sync_audit.py        # целостность зеркал Sources/
│   ├── aurora_stats.py      # дашборд здоровья + месячные метрики
│   ├── aurora_hooks.py      # git pre-commit (храповик по числу ошибок)
│   ├── aurora_trace.py      # генератор трассировки требований
│   └── aurora_doctor.py     # готовность проекта + сверка структуры
├── skills/
│   ├── aurora-vault/
│   ├── confluence-sync-template/
│   └── jira-export-template/
├── templates/
│   ├── agents/AGENTS.md.template
│   ├── aurora.config.yaml.template   # project settings (committed in targets)
│   ├── aurora.env.local.example      # → .env.aurora.local (gitignored)
│   ├── meta/conventions.md
│   └── cursor/atlassian.mdc.template
├── scaffold/          # что копируется в проект как стартовый контент
│   ├── Templates/
│   └── Prompts/
├── docs/readme/
├── docs/
└── examples/
```

Target project after install gets `aurora.config.yaml` + trust-layer tree from `skills/aurora-vault/SKILL.md`.
Atlassian space/JQL/skills come from config — not hard-coded in skill bodies.
