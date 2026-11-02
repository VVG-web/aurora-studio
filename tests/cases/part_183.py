"""Проверки движка Aurora, часть 183. Шаблон AC: статус не спрашивается вручную, «Как» — только роли из «Ролевой модели».

Статус карточки в базе знаний вычисляет `kb:trust` по источнику и задачам Jira, человек его не выбирает.
Старый вопрос «draft / in_progress / verified / done» из шаблона AC смешивал жизненный цикл истории с доверием
и заставлял модель спрашивать то, что должно быть производным. Роли в «Как» тоже не выдумываются: шаблон
отправляет модель к разделу «Ролевая модель» проекта.
"""
from __future__ import annotations

from pathlib import Path

from harness import KIT, test


@test
def test_ac_template_has_no_manual_status_and_points_how_to_role_model(tmp: Path):
    """Шаблон AC не содержит вопроса про статус, а поле «Как» ограничено ролями из «Ролевой модели»."""
    text = (KIT / "scaffold/Templates/AC_template.md").read_text(encoding="utf-8")
    assert "Укажите статус User Story" not in text
    assert "{{Статус User Story" not in text
    assert "status: {{" not in text, "в шаблоне остался ручной status-плейсхолдер"
    assert "Ролевая модель" in text, "шаблон не отправляет к ролевой модели проекта"
    assert "Не придумывайте роли" in text, "нет запрета на выдуманные роли"
    assert "системн" in text.lower(), "не сказано, как поступать с системным поведением"
