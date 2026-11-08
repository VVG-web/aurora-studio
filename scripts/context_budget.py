#!/usr/bin/env python3
"""context_budget.py - бюджеты контекста и порог сжатия.

Настройка живёт в `aurora.config.yaml` проекта:

    context_architecture:
      enabled: true
      compression:
        enabled: true
        threshold: 0.50
        threshold_tokens: 0
        target_ratio: 0.50
      reserves:
        planner: 24000
        worker: 24000
        critic: 10000
        qa: 10000
        tools: 24000
        safety: 10000

Модуль не зависит от agent_core: его можно проверять отдельно и использовать
в тестах без сети и моделей.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

DEFAULTS = {
    "enabled": False,
    "compression": {
        "enabled": True,
        "threshold": 0.50,
        "threshold_tokens": 0,
        "target_ratio": 0.50,
    },
    "reserves": {
        "planner": 24000,
        "worker": 24000,
        "critic": 10000,
        "qa": 10000,
        "compressor": 4000,
        "tools": 24000,
        "safety": 10000,
    },
    "planner_full_cards": 10,
    "ask_full_cards": 10,
    "worker_full_cards": 12,
    "critic_cards": 8,
}

TRUE = {"1", "true", "yes", "on", "да", "вкл"}
FALSE = {"0", "false", "no", "off", "нет", "выкл"}


def _scalar(raw: str):
    value = raw.strip().strip('"').strip("'")
    low = value.lower()
    if low in TRUE:
        return True
    if low in FALSE:
        return False
    try:
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        if re.fullmatch(r"-?\d+[.,]\d+", value):
            return float(value.replace(",", "."))
    except ValueError:
        pass
    return value


def _nested(text: str, section: str) -> dict:
    """Разобрать один блок `section:` с двумя уровнями вложенности без PyYAML."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(section)}\s*:\s*$", line):
            start = i + 1
            break
    if start is None:
        return {}
    out: dict = {}
    current = None
    for line in lines[start:]:
        if line.strip() and not line.startswith((" ", "\t")):
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key, sep, value = line.strip().partition(":")
        if not sep:
            continue
        if indent <= 2:
            current = key.strip()
            out.setdefault(current, {})
            if value.strip():
                out[current] = _scalar(value)
        elif current and isinstance(out.get(current), dict):
            out[current][key.strip()] = _scalar(value)
    return out


def read_config(cwd: str | os.PathLike = ".") -> dict:
    """Настройка контекста проекта, слитая с безопасными умолчаниями."""
    cfg = {
        "enabled": DEFAULTS["enabled"],
        "compression": dict(DEFAULTS["compression"]),
        "reserves": dict(DEFAULTS["reserves"]),
        "planner_full_cards": DEFAULTS["planner_full_cards"],
        "ask_full_cards": DEFAULTS["ask_full_cards"],
        "worker_full_cards": DEFAULTS["worker_full_cards"],
        "critic_cards": DEFAULTS["critic_cards"],
    }
    path = Path(cwd) / "aurora.config.yaml"
    if not path.is_file():
        return cfg
    text = path.read_text(encoding="utf-8", errors="replace")
    section = _nested(text, "context_architecture")
    if not section:
        return cfg
    if "enabled" in section:
        cfg["enabled"] = bool(section["enabled"])
    comp = section.get("compression")
    if isinstance(comp, dict):
        for key in ("enabled", "threshold", "threshold_tokens", "target_ratio"):
            if key in comp:
                cfg["compression"][key] = comp[key]
    res = section.get("reserves")
    if isinstance(res, dict):
        for key in DEFAULTS["reserves"]:
            if key in res:
                cfg["reserves"][key] = res[key]
    for key in ("planner_full_cards", "ask_full_cards", "worker_full_cards", "critic_cards"):
        if key in section:
            cfg[key] = section[key]
    # Числа после разбора могут быть строками: приводим к безопасным значениям.
    for key in ("planner", "worker", "critic", "qa", "compressor", "tools", "safety"):
        try:
            cfg["reserves"][key] = max(0, int(cfg["reserves"][key]))
        except (TypeError, ValueError):
            cfg["reserves"][key] = DEFAULTS["reserves"][key]
    for key in ("planner_full_cards", "ask_full_cards", "worker_full_cards", "critic_cards"):
        try:
            cfg[key] = max(1, int(cfg[key]))
        except (TypeError, ValueError):
            cfg[key] = DEFAULTS[key]
    try:
        cfg["compression"]["threshold"] = min(1.0, max(0.0, float(cfg["compression"]["threshold"])))
    except (TypeError, ValueError):
        cfg["compression"]["threshold"] = DEFAULTS["compression"]["threshold"]
    try:
        cfg["compression"]["threshold_tokens"] = max(0, int(cfg["compression"]["threshold_tokens"]))
    except (TypeError, ValueError):
        cfg["compression"]["threshold_tokens"] = 0
    try:
        cfg["compression"]["target_ratio"] = min(1.0, max(0.05, float(cfg["compression"]["target_ratio"])))
    except (TypeError, ValueError):
        cfg["compression"]["target_ratio"] = DEFAULTS["compression"]["target_ratio"]
    cfg["compression"]["enabled"] = bool(cfg["compression"]["enabled"])
    return cfg


def window(cfg: dict) -> int:
    """Самое узкое объявленное окно кольца. 0 - окна не объявлены."""
    values = [b.get("context") or 0 for b in cfg.get("backends") or [] if b.get("context")]
    for chain in ((cfg.get("chains") or {}).get("llm") or {}).values():
        values += [b.get("context") or 0 for b in chain or [] if b.get("context")]
    return min(values) if values else 0


def reserve(role: str, config: dict | None = None) -> int:
    cfg = config or DEFAULTS
    return int((cfg.get("reserves") or {}).get(role, DEFAULTS["reserves"].get(role, 0)))


def input_budget(cfg: dict, role: str, config: dict | None = None, tools: bool = False) -> int:
    """Сколько токенов можно отдать в запрос этой роли.

    `tools=True` вычитает резерв на схемы инструментов и MCP. Для ролей без
    инструментов этот резерв не нужен.
    """
    c = config or DEFAULTS
    win = window(cfg)
    if win <= 0:
        return 0
    used_tools = reserve("tools", c) if tools else 0
    return max(0, win - reserve(role, c) - used_tools - reserve("safety", c))


def threshold_tokens(win: int, config: dict) -> int:
    """Порог сжатия в токенах: нижнее из процентного и абсолютного."""
    comp = config.get("compression") or {}
    pct = int(max(0, win) * float(comp.get("threshold", 0.5)))
    absolute = int(comp.get("threshold_tokens") or 0)
    return min(pct, absolute) if absolute > 0 else pct


def should_compress(used_tokens: int, win: int, config: dict) -> bool:
    """Срабатывает ли сжатие на этом объёме."""
    comp = config.get("compression") or {}
    if not config.get("enabled") or not comp.get("enabled", True):
        return False
    if win <= 0:
        return False
    return used_tokens >= threshold_tokens(win, config)


def target_tokens(win: int, config: dict) -> int:
    comp = config.get("compression") or {}
    return max(256, int(max(0, win) * float(comp.get("target_ratio", 0.5))))


def target_chars(win: int, config: dict) -> int:
    """Целевой объём конспекта в знаках, с осторожной кириллической меркой."""
    return max(512, int(target_tokens(win, config) * 2.2))


def estimate_tokens(text: str) -> int:
    """Оценка токенов без токенизатора: кириллица 2.2 знака, прочее 3.5."""
    cyr = sum(1 for ch in str(text or "") if "\u0400" <= ch <= "\u04ff")
    other = len(str(text or "")) - cyr
    return int(cyr / 2.2 + other / 3.5)


def compression_metrics(used_tokens: int, win: int, config: dict) -> dict:
    """Поля для отчёта: порог, сработало ли, целевой размер."""
    threshold = threshold_tokens(win, config)
    return {
        "window": win,
        "used_tokens": int(used_tokens),
        "threshold_tokens": threshold,
        "threshold": float((config.get("compression") or {}).get("threshold", 0.5)),
        "target_tokens": target_tokens(win, config),
        "triggered": should_compress(used_tokens, win, config),
    }
