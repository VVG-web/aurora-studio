#!/usr/bin/env python3
"""part_205 — C.10: причина readonly переводится вместе с остальными сообщениями (1.211.1)."""
from __future__ import annotations

import sys

from harness import KIT, test

sys.path.insert(0, str(KIT / "cockpit"))
import aurora_cockpit as ck  # noqa: E402

SOURCE = ("карточка выведена из источников: правится корректирующим артефактом, а не здесь — "
          "иначе следующая сборка сотрёт правку")


@test
def test_readonly_reason_is_translated_in_english(_t):
    """`readonly` входит в MESSAGE_KEYS, поэтому английская панель не показывает русскую причину."""
    translated = ck.translate_payload({"readonly": SOURCE}, "en")["readonly"]
    assert translated.startswith("the card is derived from sources"), translated
    assert ck.translate_payload({"readonly": SOURCE}, "ru")["readonly"] == SOURCE
