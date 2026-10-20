"""Проверки движка Aurora, часть 108. Каркас и помощники — tests/harness.py.

Изображения вложений (необязательный срез, по умолчанию выключен): jpg, png, webp, gif читает зрячая модель кольца
распознавания — только если в настройках проекта включено «Распознавать изображения моделью»; svg читается как
текст без модели. Значки не отправляются, слишком большие не принимаются, нет кольца — предупреждение, а не сбой.
Кольцо и шлюз подменяются в процессе на время проверки; сети и настоящей модели нет.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import os
import sys
import types
from pathlib import Path

from harness import SCRIPTS, make_project, test  # noqa: F401

ASSETS = "Sources/Confluence/Страница верхнего уровня_assets"
OUT = "Raw/Extracted/Confluence/Страница верхнего уровня"
PNG = b"\x89PNG\r\n\x1a\n"


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


class _Ring:
    """Подмена `agent_core`: кольцо распознавания из одного бэкенда и шлюз, записывающий запросы."""

    def __init__(self, answer=("Текст со снимка экрана PRJ-A: заявка принята и ушла в обработку подсистемы, "
                               "после проверки правил статус заявки меняется на «принята»."), ring=True):
        self.calls = []
        self.answer, self.ring = answer, ring

    def module(self):
        m = types.ModuleType("agent_core")
        m.config = lambda: {"ocr": {"model": "vision-1", "dpi": 100, "max_pages": 5}, "request_timeout": 5}
        m.ocr_ring = lambda cfg: [{"url": "http://gw.example/v1", "key": "k", "model": "vision-1"}] if self.ring else []

        def http_json(url, payload, key, timeout):
            self.calls.append((url, payload))
            return 200, {"choices": [{"message": {"content": self.answer}}]}, "", 0.0
        m.http_json = http_json
        return m

    def __enter__(self):
        self.saved = sys.modules.get("agent_core")
        sys.modules["agent_core"] = self.module()
        return self

    def __exit__(self, *a):
        if self.saved is None:
            sys.modules.pop("agent_core", None)
        else:
            sys.modules["agent_core"] = self.saved


def _run(OI, root: Path, *args) -> tuple:
    """Запуск `office_ingest.main()` в процессе: (код, вывод). Рабочая папка и argv возвращаются."""
    cwd, argv = os.getcwd(), sys.argv
    os.chdir(root)
    sys.argv = ["office_ingest.py", *args]
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = OI.main()
    finally:
        os.chdir(cwd)
        sys.argv = argv
    return code, out.getvalue()


def _project(tmp: Path, images: bool) -> Path:
    root = make_project(tmp)
    page = root / "Sources/Confluence/Страница верхнего уровня.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntitle: Страница\n---\n\nОписание страницы PRJ-A, достаточно длинное для источника. " * 4, encoding="utf-8")
    (root / ASSETS).mkdir(parents=True, exist_ok=True)
    (root / ASSETS / "снимок.png").write_bytes(PNG + os.urandom(20_000))
    (root / ASSETS / "значок.png").write_bytes(PNG + os.urandom(500))
    (root / ASSETS / "схема.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><title>Схема PRJ-A</title><desc>Приём заявки подсистемой</desc>'
        '<g><text x="1" y="1">Принять заявку</text><text x="1" y="9"><tspan>Проверить</tspan> данные</text>'
        '<text x="1" y="19">Выдать результат заказчику</text><text x="1" y="29">Записать итог в журнал</text></g></svg>',
        encoding="utf-8")
    if images:
        cfg = root / "aurora.config.yaml"
        cfg.write_text(cfg.read_text(encoding="utf-8") + "\ningest:\n  images: true\n", encoding="utf-8")
    return root


@test
def test_images_are_left_alone_by_default_and_read_by_the_vision_ring_when_enabled(tmp: Path):
    OI = _mod("office_ingest")
    (tmp / "off").mkdir()
    (tmp / "on").mkdir()
    root = _project(tmp / "off", images=False)
    with _Ring() as ring:
        code, out = _run(OI, root)
        assert code == 0 and not ring.calls, "изображения отправлены модели без настройки"
    assert not list((root / "Raw/Extracted").rglob("снимок*")) and not list((root / "Raw/Extracted").rglob("*.svg.md")), \
        "по умолчанию изображения и svg попали в транскрипты"
    root = _project(tmp / "on", images=True)
    with _Ring() as ring:
        code, out = _run(OI, root)
        assert code == 0, out
        assert len(ring.calls) == 1, "модель позвана не один раз (значок отправлять нельзя): %d" % len(ring.calls)
        url, payload = ring.calls[0]
        part = payload["messages"][0]["content"]
        assert url == "http://gw.example/v1/chat/completions" and payload["model"] == "vision-1", (url, payload["model"])
        assert part[0]["text"] == OI.IMAGE_PROMPT and part[1]["image_url"]["url"].startswith("data:image/png;base64,"), part
    shot = (root / OUT / "снимок.png.md").read_text(encoding="utf-8")
    assert "converter: ocr-vision-1" in shot and "Распознано моделью" in shot and "это изображение" in shot, shot[:600]
    assert "Текст со снимка экрана PRJ-A" in shot and 'page: "Sources/Confluence/Страница верхнего уровня.md"' in shot
    assert not (root / OUT / "значок.png.md").exists(), "значок получил транскрипт"
    assert "Без транскрипта (текста слишком мало" in out, out
    # повтор ничего не отправляет и ничего не пишет
    with _Ring() as ring:
        code, out2 = _run(OI, root)
        assert not ring.calls and "собрано: 0" in out2, (ring.calls, out2)


@test
def test_svg_is_read_as_text_without_a_model_and_a_hostile_svg_is_refused(tmp: Path):
    OI = _mod("office_ingest")
    root = _project(tmp, images=True)
    with _Ring(ring=False) as ring:
        code, out = _run(OI, root)
        assert not ring.calls, "svg ушёл модели"
    svg = (root / OUT / "схема.svg.md").read_text(encoding="utf-8")
    assert "converter: svg" in svg and "- Схема PRJ-A" in svg and "- Приём заявки подсистемой" in svg, svg
    assert "- Принять заявку" in svg and "- Проверить данные" in svg or "- Проверить" in svg, svg
    bad = root / ASSETS / "чужая.svg"
    bad.write_text('<?xml version="1.0"?><!DOCTYPE s [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg><text>&x;</text></svg>',
                   encoding="utf-8")
    text, conv = OI.convert(str(bad), "auto")
    assert text is None and "DOCTYPE" in OI.LAST_WHY, (text, OI.LAST_WHY)
    empty = root / ASSETS / "пустая.svg"
    empty.write_text('<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>', encoding="utf-8")
    assert OI.convert(str(empty), "auto") == (None, None) and "нет подписей" in OI.LAST_WHY


@test
def test_enabled_images_without_a_recognition_ring_are_a_warning_not_a_failure(tmp: Path):
    OI = _mod("office_ingest")
    root = _project(tmp, images=True)
    with _Ring(ring=False) as ring:
        code, out = _run(OI, root)
        assert code == 0, "незаконченная настройка распознавания покрасила шаг маршрута: " + out
        assert not ring.calls
    assert "Изображения без текста: распознавание не настроено (раздел «Модели» → OCR) — 1" in out, out
    assert not (root / OUT / "снимок.png.md").exists()
    os.chdir(root)
    assert any("снимок.png" in p for p in OI.inventory()["loose"]), "изображение без текста не названо в инвентаре"
    # --no-ocr: без зрячей модели изображения не обходятся вовсе (ни предупреждения, ни вызовов)
    with _Ring() as ring:
        code, out = _run(OI, root, "--no-ocr")
        assert code == 0 and not ring.calls and "Изображения без текста" not in out, out


@test
def test_a_too_large_image_is_refused_and_the_shared_gateway_call_is_the_only_one(tmp: Path):
    OI = _mod("office_ingest")
    root = _project(tmp, images=True)
    big = root / ASSETS / "огромный.png"
    big.write_bytes(PNG + b"\0" * (OI.MAX_IMAGE_BYTES + 10))
    with _Ring() as ring:
        code, out = _run(OI, root)
        assert code == 1 and "огромный.png" in out and "изображение больше предела" in out, (code, out)
        assert len(ring.calls) == 1, "огромный файл отправлен модели"
    # единственное место шлюза: страница pdf и изображение зовут один `ocr_page`
    src = (SCRIPTS / "office_ingest.py").read_text(encoding="utf-8")
    assert src.count('"/chat/completions"') == 1 and "def ocr_page(" in src, "распознавание ходит на шлюз не из одного места"
    LC = _mod("llm_calls")
    found = LC.scan()
    assert found["office_ingest.py:ocr_page"]["http"] and "office_ingest.py:conv_pdf_ocr" not in found
