#!/usr/bin/env python3
"""part_214 - хранилище контекста, оглавление и инструменты памяти.

Проверяем SQLite-хранилище, пакет карточек, TOC, ctx_get, expand, pin/drop,
цитируемые карточки и кэш конспектов. Сеть и модели не нужны.
"""
from __future__ import annotations

import sys
from pathlib import Path

from harness import KIT, test

sys.path.insert(0, str(KIT / "scripts"))
import context_budget as CB  # noqa: E402
import context_store as CS  # noqa: E402


PACK = """# Context pack: тест

_Собран тест · режим ask · карточек 4_

Вступление пака.

---

## ALG-3.28

**Доверие:** knowledge
**Тип:** algorithm

Название: Перенос средств

## Описание

Перенос средств на актуальный аналитический баланс, срок 10 дней.

## Критерии

- операция 42 выполняется атомарно.
- журнал хранит каждую попытку.

---

## ER.Tran

**Доверие:** knowledge

Название: Транзакция

## Описание

Таблица ER.Tran хранит 17 полей и ссылку на баланс.

---

## FinTransLog

**Доверие:** draft

Название: Журнал платежей

## Описание

Журнал FinTransLog относится к платежам, не к складам.

---

## CP-3.2.10

**Доверие:** draft

Название: Черновик

## Описание

Черновик без опоры, нужен только для проверки.
"""


def _new_store(tmp: Path) -> CS.ContextStore:
    return CS.ContextStore(tmp / "context" / "context.sqlite")


@test
def test_context_store_roundtrip_toc_and_memory_tools(_t):
    tmp = Path(_t)
    with _new_store(tmp) as store:
        ids = store.put_pack(PACK)
        assert ids == ["ALG-3.28", "ER.Tran", "FinTransLog", "CP-3.2.10"], ids

        found = store.search("перенос баланс", limit=3)
        assert found and found[0]["id"] == "ALG-3.28", found
        assert store.search("нет такого слова") == []

        full = store.get("ALG-3.28")
        assert full["ok"] and "срок 10 дней" in full["text"], full
        part = store.get("ALG-3.28", section="Критерии", max_chars=80)
        assert part["ok"] and part["text"].startswith("## Критерии") \
            and "42" in part["text"] and len(part["text"]) <= 80, part
        assert not store.get("missing")["ok"]

        parent = store.put("Parent", "родитель", kind="group")
        child = store.put("Child", "ребёнок", parent_id=parent)
        expanded = store.expand(parent)
        assert expanded["ok"] and [c["id"] for c in expanded["children"]] == [child], expanded

        assert store.pin("ALG-3.28") and store.pin("ER.Tran")
        assert [i["id"] for i in store.working_set()] == ["ALG-3.28", "ER.Tran"]
        assert store.drop("ALG-3.28") and [i["id"] for i in store.working_set()] == ["ER.Tran"]

        toc = store.toc()
        assert "| [[ALG-3.28]] |" in toc and "Перенос средств" in toc, toc

        packed = store.planner_pack(PACK, max_full=1)
        assert "## ALG-3.28" in packed and "## ER.Tran" not in packed
        assert "## Оглавление остальных карточек" in packed
        assert "[[ER.Tran]]" in packed and "ctx_get(id)" in packed

        cited = store.cited_pack(PACK, "Смотри [[ER.Tran]] и [[FinTransLog]]", max_full=2)
        assert "## ER.Tran" in cited and "## FinTransLog" in cited
        assert "## ALG-3.28" not in cited


@test
def test_context_store_summary_cache_and_sources(_t):
    tmp = Path(_t)
    with _new_store(tmp) as store:
        ids = store.put_pack(PACK)
        calls = []

        def compressor(text, target):
            calls.append((len(text), target))
            return {"summary": "Конспект: срок 10 дней, операция 42, [[ALG-3.28]]",
                    "claims": [{"text": "срок 10 дней", "sources": ["ALG-3.28"],
                                "confidence": "high"}],
                    "open_questions": ["нет"]}

        first = store.summarize(ids, compressor=compressor, target_chars=600)
        assert first["cached"] is False and len(calls) == 1, first
        assert "10 дней" in first["summary"] and first["claims"][0]["sources"] == ["ALG-3.28"]
        second = store.summarize(ids, compressor=compressor, target_chars=600)
        assert second["cached"] is True and len(calls) == 1, second

        plain = store.summarize(ids[:2], target_chars=600)
        assert plain["summary"] and "[[ALG-3.28]]" in plain["summary"], plain
        assert any("42" in claim["text"] for claim in plain["claims"]), plain["claims"]

        win = 1000
        cfg = {"enabled": True, "compression": {"enabled": True, "threshold": 0.1,
                                                "threshold_tokens": 0, "target_ratio": 0.25}}
        compressed = store.maybe_compress(ids, win, cfg)
        assert compressed and compressed["summary"] and compressed["tokens"] > 0, compressed
        cfg["compression"]["threshold"] = 0.99
        assert store.maybe_compress(ids, win, cfg) is None


@test
def test_context_budget_threshold_and_role_reserves(_t):
    tmp = Path(_t)
    (tmp / "aurora.config.yaml").write_text(
        "context_architecture:\n"
        "  enabled: true\n"
        "  compression:\n"
        "    enabled: true\n"
        "    threshold: 0.50\n"
        "    threshold_tokens: 100\n"
        "    target_ratio: 0.25\n"
        "  reserves:\n"
        "    planner: 20000\n"
        "    worker: 24000\n"
        "    critic: 10000\n"
        "    qa: 10000\n"
        "    tools: 10000\n"
        "    safety: 10000\n", encoding="utf-8")
    cfg = CB.read_config(tmp)
    assert cfg["enabled"] and cfg["reserves"]["planner"] == 20000, cfg
    assert CB.threshold_tokens(1000, cfg) == 100
    assert CB.should_compress(100, 1000, cfg) and not CB.should_compress(99, 1000, cfg)
    assert CB.target_tokens(1000, cfg) == 256, CB.target_tokens(1000, cfg)
    assert CB.target_chars(1000, cfg) >= 512
    assert CB.input_budget({"backends": [{"context": 100000}]}, "planner", cfg, tools=True) == 60000
    assert CB.input_budget({"backends": [{"context": 100000}]}, "worker", cfg) == 66000

    cfg["compression"]["threshold_tokens"] = 0
    assert CB.threshold_tokens(1000, cfg) == 500
    cfg["compression"]["threshold"] = 0.30
    assert CB.threshold_tokens(1000, cfg) == 300
    cfg["enabled"] = False
    assert not CB.should_compress(999, 1000, cfg)


@test
def test_context_store_indexes_existing_cards_without_rewriting(_t):
    root = Path(_t) / "project"
    db = root / "AuroraKnowledgeDB" / "Concepts"
    db.mkdir(parents=True)
    card = db / "Заявка.md"
    card.write_text("---\ntitle: Заявка\ntrust: knowledge\n---\n\n# Заявка\n\nСрок 10 дней.\n",
                    encoding="utf-8")
    before = card.read_bytes()
    with _new_store(Path(_t)) as store:
        ids = store.index_cards(root)
        assert ids == ["Заявка"], ids
        got = store.get("Заявка")
        assert got["ok"] and "Срок 10 дней" in got["text"], got
    assert card.read_bytes() == before, "миграция переписала исходную карточку"
