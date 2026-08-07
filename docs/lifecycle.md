# Цикл работы с Aurora Studio

Один оборот: источник → зеркало → знание → доверие → артефакт → наружу → обратная связь.
Здесь — карта целиком и ритм работы. Путь одной карточки по шагам, с условиями перехода и
ценой пропуска каждого этапа, — в [card-path.md](card-path.md).
Каждая стрелка — команда движка, а не пожелание. Схема рендерится прямо на GitHub.

## Общий цикл

```mermaid
flowchart LR
  subgraph SRC["1 · Источники"]
    CONF["Confluence\nwiki-хранилище"]
    JIRA["Jira\nдоска задач"]
    RAW["Raw/\nдоговоры, встречи,\nдокументы заказчика"]
  end

  subgraph MIR["2 · Зеркала — Sources/"]
    MCONF["Sources/Confluence\nдетерминированная выгрузка"]
    MJIRA["Sources/JIRA\nзадачи по JQL"]
  end

  subgraph KB["3 · База знаний — AuroraKnowledgeDB/"]
    IMP["imported\nмашина принесла"]
    VER["verified\nчеловек проверил"]
    MOC["MOC\nкарты содержания"]
  end

  subgraph OUT["4 · Продукт"]
    ART["Artifacts/\nUS · AC · спеки"]
    DEL["Deliverables/released/\nчто сдали"]
  end

  CONF -->|"sync:confluence"| MCONF
  JIRA -->|"sync:jira"| MJIRA
  RAW -->|"kb:ingest-office"| IMP
  MCONF -->|"kb:build + ассистент"| IMP
  MJIRA -->|"kb:build + ассистент"| IMP
  IMP -->|"kb:links --cards\nkb:moc"| MOC
  MOC -.->|"вход у каждой карточки"| IMP
  IMP -->|"kb:queue → человек → kb:verify"| VER
  VER -->|"ctx:context\nтолько проверенное"| ART
  ART -->|"make:review\nship:export · ship:publish"| DEL
  DEL -->|"ship:release"| CONF
  MJIRA -->|"sync:jira-status\nстатусы задач"| ART
  MCONF -.->|"sync:diff — дрейф:\nисточник изменился"| VER

  classDef src fill:#E7DCC5,stroke:#16150F,stroke-width:2px,color:#16150F
  classDef mir fill:#F8F1E2,stroke:#16150F,stroke-width:2px,color:#16150F
  classDef imp fill:#8A8272,stroke:#16150F,stroke-width:2px,color:#FFFFFF
  classDef ver fill:#1E8A46,stroke:#16150F,stroke-width:2px,color:#FFFFFF
  classDef moc fill:#2E6FC8,stroke:#16150F,stroke-width:2px,color:#FFFFFF
  classDef out fill:#D98A00,stroke:#16150F,stroke-width:2px,color:#16150F
  class CONF,JIRA,RAW src
  class MCONF,MJIRA mir
  class IMP imp
  class VER ver
  class MOC moc
  class ART,DEL out
```

## Жизнь одной карточки

```mermaid
stateDiagram-v2
  [*] --> imported: kb#58;build — извлёк ассистент
  imported --> draft: человек начал править
  draft --> in_review: отдал на проверку
  in_review --> verified: kb#58;verify — сверено с источником
  imported --> verified: kb#58;verify --source-older-than\n(пакетно, основание в карточке)
  verified --> imported: sync#58;diff — источник изменился
  verified --> deprecated: kb#58;supersede — знание заменено
  deprecated --> [*]: _archive/ — только история

  note right of verified
    Верхний статус базы.
    В ctx:context режимов generate и review
    попадает только verified.
  end note
```

## Суточный ритм

| Когда | Что | Команды |
|---|---|---|
| каждый день, 10 минут | утренний обход | `sync:confluence` → `sync:jira` → `sync:audit` → `sync:diff` → `ops:stats` |
| после синка | пополнить базу | `kb:build --partition N` → ассистент → `kb:links --cards` → `kb:moc` → `kb:lint` |
| раз в неделю | верификация | `kb:queue` → человек → `kb:verify` |
| раз в месяц | навигация | `kb:moc --suggest` → правило в `moc_groups.txt` → `kb:moc --apply` |
| пятница, 30 минут | прополка | `kb:garden` → `kb:dedupe` → `kb:index` → `kb:schema` |
| когда идёт разработка | трассировка | `sync:jira-status` → `ops:trace` → `make:spec-pack` |
| когда артефакт готов | наружу | `make:review` → `ship:export` → `ship:publish` → `ship:release` |

Два правила, из которых вырастает всё остальное: **зеркало детерминировано** (один и тот
же источник даёт один и тот же файл — иначе git-diff перестаёт быть уликой) и **доверие
присваивает человек** (`verified` ставит тот, кто сверил карточку с источником и отвечает
за это).
