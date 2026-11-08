#!/usr/bin/env python3
"""context_runtime.py - связка бюджета, хранилища и политики стадий.

Модуль держит небольшие решения, которые не должны расползаться по
agent_runner: когда планировщику нужны инструменты и MCP, какой рабочий набор
отдать стадии, как собрать компрессор и лимит входа.
"""
from __future__ import annotations

import re
from pathlib import Path

import context_budget as CB
import context_store as CS

JIRA_REF = re.compile(r"\b[A-Z][A-Z0-9]{1,12}-\d+\b")
SEARCH_WORDS = re.compile(
    r"(?i)(найди|поищи|поиск|проверь|посмотри|jira|confluence|атлассиан|atlassian|"
    r"@файл|@file|@mcp|ссылк|источник)")
MCP_WORDS = re.compile(r"(?i)(jira|confluence|атлассиан|atlassian|@mcp|mcp-atlassian)")


def config(cwd: str | Path = ".") -> dict:
    return CB.read_config(cwd)


def enabled(cwd: str | Path = ".") -> bool:
    return bool(config(cwd).get("enabled"))


def task_text(st: dict) -> str:
    parts = [str(st.get("idea") or "")]
    parts += [str(r.get("answers") or "") for r in st.get("rounds") or []]
    parts += [str(x) for x in st.get("context") or []]
    return "\n".join(parts)


def needs_tools(st: dict, spec: dict | None = None, cfg: dict | None = None) -> bool:
    """Нужны ли планировщику инструменты. По умолчанию нет: пак и шаблон уже в промпте."""
    if cfg is None or not cfg.get("enabled"):
        return True
    text = task_text(st)
    return bool(SEARCH_WORDS.search(text) or JIRA_REF.search(text))


def needs_mcp(st: dict, spec: dict | None = None, req: dict | None = None,
              cfg: dict | None = None) -> bool:
    """MCP только по явной необходимости, а не для каждого вида us."""
    if cfg is None or not cfg.get("enabled"):
        return True
    text = task_text(st)
    if MCP_WORDS.search(text):
        return True
    found = list((req or {}).get("mcp") or [])
    # Явное упоминание сервера в задаче оставляем: человек попросил.
    for name in found:
        if f"@{name}" in text or name.lower() in text.lower():
            return True
    return False


def input_limit(agent_cfg: dict, role: str, cfg: dict | None = None, tools: bool = False) -> int:
    return CB.input_budget(agent_cfg, role, cfg, tools=tools)


def open_make(cwd: str | Path, sid: str) -> CS.ContextStore:
    return CS.open_make_store(cwd, sid)


def open_ask(cwd: str | Path, thread: str = "default") -> CS.ContextStore:
    return CS.open_ask_store(cwd, thread)


def prepare_pack(store: CS.ContextStore, pack: str, cfg: dict, *, level: str = "planner") -> dict:
    """Разложить пак в хранилище и вернуть рабочие наборы по стадиям."""
    ids = store.put_pack(pack)
    planner_full = int(cfg.get("planner_full_cards") or 10)
    worker_full = int(cfg.get("worker_full_cards") or 12)
    planner = store.planner_pack(pack, max_full=planner_full)
    worker = store.planner_pack(pack, max_full=worker_full)
    return {"ids": ids, "planner": planner, "worker": worker, "level": level}


def cited(store: CS.ContextStore, pack: str, draft: str, cfg: dict | None = None) -> str:
    count = int((cfg or {}).get("critic_cards") or 8)
    return store.cited_pack(pack, draft, max_full=count)


def has_compressor(agent_cfg: dict) -> bool:
    chain = ((agent_cfg.get("chains") or {}).get("llm") or {}).get("compressor") or []
    return bool(chain)


COMPRESS_PROMPT = """Сожми фрагмент контекста для следующего шага работы.
Верни JSON: {{"summary": "...", "claims": [{{"text": "...", "sources": ["ID"], "confidence": "high|medium|low"}}], "open_questions": ["..."]}}.
Сохрани все числа, имена карточек, решения и открытые вопросы. Не выдумывай.
Цель: не больше {target} знаков.

{text}
"""


def compressor_callable(call, agent_cfg: dict):
    """Вернуть callable(text, target) или None, если роли compressor нет.

    Роль без собственной цепочки не подменяется писателем: иначе компрессор
    стоил бы как планировщик. Экстрактивный fallback остаётся в хранилище.
    """
    if not has_compressor(agent_cfg):
        return None

    def compress(text: str, target: int):
        r = call(agent_cfg, "compressor", [{"role": "user", "content": COMPRESS_PROMPT.format(
            target=max(256, int(target)), text=text)}], max_tokens=min(2000, max(256, int(target // 2))))
        if not r.get("ok") or not (r.get("text") or "").strip():
            return None
        raw = r["text"].strip()
        import json
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("summary"):
                return data
        except ValueError:
            pass
        return {"summary": raw}
    return compress
