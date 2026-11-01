"""Адаптеры извлечения T0: разбор без модели и без внешних зависимостей.

Каждый адаптер получает путь, текст, язык и роль файла и возвращает записи контракта
приложения A: `symbol`, `edge`, `route`, `table`, `column`, `topic`, `config`, `string`,
`test`, `reqtag`. Ошибка разбора не роняет индекс: вызывающий помечает файл `parse_error`.
"""
from __future__ import annotations

__all__ = ["stdlib_py", "stdlib_jvm", "stdlib_web", "stdlib_misc", "sql_files", "spec_files"]
