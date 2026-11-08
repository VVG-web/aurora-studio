#!/usr/bin/env python3
"""part_222 - кэш разбора слов в поиске не меняет результат и не отдаёт общий список."""
from __future__ import annotations

import sys

from harness import KIT, test

sys.path.insert(0, str(KIT / "scripts"))
import ctx_pack as CP  # noqa: E402


@test
def test_cached_words_are_identical_and_callers_get_their_own_list(_t):
    text = "Что сказано в AC-4.4.3 про бизнес-процесс?"
    first = CP.words(text)
    second = CP.words(text)
    assert first == second == [CP.norm("сказано"), "ac-4.4.3", "4.4.3",
                               CP.norm("про"), CP.norm("бизнес-процесс")], first
    first.append("чужое")
    assert "чужое" not in CP.words(text), "кэш отдал общий мутируемый список"


@test
def test_cached_words_keep_identifier_and_stop_word_rules(_t):
    assert CP.words("AC-4.4.30") == ["ac-4.4.30", "4.4.30"]
    assert CP.words("AC-4.4.3") == ["ac-4.4.3", "4.4.3"]
    assert "как" not in CP.words("как вернуть платёж")
    assert CP.words("до 01.09.2026") == ["01.09.2026", "2026"]
