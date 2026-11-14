#!/usr/bin/env python3
"""agent_schemas.py — формы ответов модели, общие для движка и адаптера Pydantic AI.

Зачем отдельный файл. Движок обязан работать без зависимостей, а Pydantic AI живёт
только в venv адаптера. Здесь лежат две вещи:

  • Pydantic-модели для `output_type` — их читает адаптер в venv;
  • `validate()` без Pydantic — им пользуется движок, если ответ пришёл прямым HTTP.

Один перечень полей на оба пути: иначе адаптер принимал бы одно, а движок молча
терял другое. Модели намеренно снисходительны к лишним полям: ответ модели — данные,
а не повод уронить разбор из-за нового пояснения рядом.
"""
from __future__ import annotations

try:  # в venv адаптера Pydantic есть; в питоне движка его может не быть
    from pydantic import BaseModel, ConfigDict, Field
    HAVE_PYDANTIC = True
except ImportError:  # noqa: BLE001 — движок работает без Pydantic
    BaseModel = object  # type: ignore[assignment]
    ConfigDict = dict  # type: ignore[assignment]
    Field = lambda *a, **k: None  # type: ignore[assignment]
    HAVE_PYDANTIC = False


if HAVE_PYDANTIC:
    class BuildCard(BaseModel):
        model_config = ConfigDict(extra="allow")
        title: str = ""
        into: str = ""
        # Раздел базы и одна фраза о сути: их просит PROMPT_BUILD. Без полей в
        # Pydantic-схеме модель не видит их в описании ответа и заполняет как придётся.
        to: str = ""
        summary: str = ""
        sections: str = ""

    class BuildPlan(BaseModel):
        model_config = ConfigDict(extra="allow")
        cards: list[BuildCard] = Field(default_factory=list)
        empty: str = ""

    class CriticVerdict(BaseModel):
        model_config = ConfigDict(extra="allow")
        ok: bool = True
        why: str = ""
        issues: list[str] = Field(default_factory=list)

    class ExtractItem(BaseModel):
        model_config = ConfigDict(extra="allow")
        term: str = ""
        definition: str = ""
        keep: str = ""

    class ExtractResult(BaseModel):
        model_config = ConfigDict(extra="allow")
        extract: list[ExtractItem] = Field(default_factory=list)

    class RelinkItem(BaseModel):
        model_config = ConfigDict(extra="allow")
        phrase: str = ""
        card: str = ""

    class RelinkResult(BaseModel):
        model_config = ConfigDict(extra="allow")
        links: list[RelinkItem] = Field(default_factory=list)

    class DistillResult(BaseModel):
        model_config = ConfigDict(extra="allow")
        thesis: str = ""
        changed: str = ""
        empty: str = ""

    class TranslitItem(BaseModel):
        model_config = ConfigDict(extra="allow")
        id: int = 0
        cyrillic: str = ""
        is_translit: bool = True
        not_translit: bool = False

    class TranslitBatch(BaseModel):
        model_config = ConfigDict(extra="allow")
        items: list[TranslitItem] = Field(default_factory=list)

    class TwinsGroup(BaseModel):
        model_config = ConfigDict(extra="allow")
        id: int = 0
        same: bool = False
        keep: str = ""
        why: str = ""

    class TwinsBatch(BaseModel):
        model_config = ConfigDict(extra="allow")
        groups: list[TwinsGroup] = Field(default_factory=list)

    MODELS = {
        "BuildPlan": BuildPlan,
        "CriticVerdict": CriticVerdict,
        "ExtractResult": ExtractResult,
        "RelinkResult": RelinkResult,
        "DistillResult": DistillResult,
        "TranslitBatch": TranslitBatch,
        "TwinsBatch": TwinsBatch,
    }
    for _model in MODELS.values():
        _model.model_rebuild()
else:
    MODELS = {}


def _str(value) -> bool:
    return isinstance(value, str)


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def _objects(value) -> bool:
    return isinstance(value, list) and all(isinstance(x, dict) for x in value)


def _valid_build(data: dict) -> tuple[bool, str]:
    if not isinstance(data.get("cards", []), list):
        return False, "cards не список"
    cards = data.get("cards") or []
    if not cards and not _str(data.get("empty", "")):
        return False, "нет ни cards, ни empty"
    for card in cards:
        if not isinstance(card, dict):
            return False, "карточка не объект"
        if not _str(card.get("title", "")) or not _str(card.get("into", "")):
            return False, "title/into не строки"
        if not card.get("title") and not card.get("into"):
            return False, "у карточки нет ни title, ни into"
        if not _str(card.get("sections", "")):
            return False, "sections не строка"
        for field in ("to", "summary"):
            if not _str(card.get(field, "")):
                return False, f"{field} не строка"
    return True, ""


def _valid_critic(data: dict) -> tuple[bool, str]:
    if not isinstance(data.get("ok"), bool):
        return False, "ok не bool"
    if not _str(data.get("why", "")):
        return False, "why не строка"
    if not _strings(data.get("issues", [])):
        return False, "issues не список строк"
    return True, ""


def _valid_extract(data: dict) -> tuple[bool, str]:
    if not _objects(data.get("extract")):
        return False, "extract не список объектов"
    for item in data["extract"]:
        if not _str(item.get("term", "")) or not _str(item.get("definition", "")):
            return False, "term/definition не строки"
    return True, ""


def _valid_relink(data: dict) -> tuple[bool, str]:
    if not _objects(data.get("links")):
        return False, "links не список объектов"
    for item in data["links"]:
        if not _str(item.get("phrase", "")) or not _str(item.get("card", "")):
            return False, "phrase/card не строки"
    return True, ""


def _valid_distill(data: dict) -> tuple[bool, str]:
    if not _str(data.get("thesis", "")):
        return False, "thesis не строка"
    return True, ""


def _valid_translit(data: dict) -> tuple[bool, str]:
    if not _objects(data.get("items")):
        return False, "items не список объектов"
    for item in data["items"]:
        if not isinstance(item.get("id"), int):
            return False, "id не число"
        if "cyrillic" in item and not _str(item.get("cyrillic")):
            return False, "cyrillic не строка"
        if "not_translit" in item and not isinstance(item.get("not_translit"), bool):
            return False, "not_translit не bool"
    return True, ""


def _valid_twins(data: dict) -> tuple[bool, str]:
    if not _objects(data.get("groups")):
        return False, "groups не список объектов"
    for item in data["groups"]:
        if not isinstance(item.get("id"), int) or not isinstance(item.get("same"), bool):
            return False, "id/same неверного типа"
        if not _str(item.get("keep", "")):
            return False, "keep не строка"
    return True, ""


_VALIDATORS = {
    "BuildPlan": _valid_build,
    "CriticVerdict": _valid_critic,
    "ExtractResult": _valid_extract,
    "RelinkResult": _valid_relink,
    "DistillResult": _valid_distill,
    "TranslitBatch": _valid_translit,
    "TwinsBatch": _valid_twins,
}


def validate(name: str, data) -> tuple[bool, str]:
    """Проверить ответ по имени формы. → (годен, причина отказа).

    Проверяются только поля, без которых движок не может продолжить. Лишние поля
    разрешены, пустые строки разрешены: модель вправе не заполнять необязательное.
    """
    if not isinstance(data, dict):
        return False, "ответ не JSON-объект"
    check = _VALIDATORS.get(name)
    return check(data) if check else (True, "")
