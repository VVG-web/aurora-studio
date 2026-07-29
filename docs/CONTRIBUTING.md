# Contributing to Aurora Studio

## Goals

Keep this repository a **project-agnostic installer**, not a client knowledge base.

## Do

- Improve `skills/aurora-vault/` procedures and frontmatter clarity
- Harden `scripts/install_aurora.py` and `kb_lint.py`
- Add generic Templates/Prompts
- Improve HTML guides (no hard-coded client product names)
- Expand docs with real migration lessons (anonymized)

## Don't

- Commit client Raw materials, Jira dumps, or private Confluence content
- Hard-code a single company's space/project keys as defaults
- Break the installer's skip-existing-files safety without a `--force` path

## Dev loop

```bash
python3 scripts/install_aurora.py \
  --target /tmp/aurora-vault-demo \
  --name "Demo" \
  --jira-key DEMO \
  --confluence-space DEMO \
  --force

cd /tmp/aurora-vault-demo
python3 .opencode/scripts/kb_lint.py --summary
```

## Versioning

Tag releases as `vMAJOR.MINOR.PATCH`. Note breaking changes to folder semantics or frontmatter in the tag notes.

## Три уровня проверок

| Уровень | Чем | Что ловит |
|---|---|---|
| Фикстуры | `python3 tests/run_tests.py` | «скрипт делает то, что задумано» — 42 теста на синтетике |
| Золотой корпус | тот же прогон, данные `tests/corpus/` | «движок видит реальные формы так же, как вчера»: гомоглифы, двойники, легаси-шапки, ПДн рядом с реквизитами, NFD-имена, артефакты в знаниях. Числа зафиксированы в `EXPECTED.json` |
| Живая база | `python3 tests/smoke_live.py <проект>` | «на этом проекте ничего не поехало»: снимок чисел и имён в `meta/smoke_snapshot.json`, сверка при каждом запуске |

Корпус пересобирается `python3 tests/make_corpus.py` и лежит в git. Нашли новую
патологию в бою — допишите её в корпус, иначе она вернётся.

Снимок живой базы хранится в самом проекте: это его данные, в репозитории kit им не место.
После осознанной правки базы снимок обновляется: `tests/smoke_live.py <проект> --update`.
