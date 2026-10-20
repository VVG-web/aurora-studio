"""Проверки движка Aurora, часть 102. Каркас и помощники — tests/harness.py.

Схемы draw.io и модели BPMN/DMN → текст (`scripts/diagram_text.py`): разбор без модели и без сети, на одной
стандартной библиотеке. Файл приходит снаружи, поэтому проверяется и безопасность: XML с DOCTYPE/ENTITY,
бомба при распаковке, слишком большой или слишком вложенный файл не читаются. Фикстуры нейтральные
(PRJ-A, «Процесс 1», example.com); пределы модуля подменяются на время проверки и возвращаются в `finally`.
"""
from __future__ import annotations

import base64
import importlib
import sys
import urllib.parse
import zlib
from pathlib import Path

from harness import KIT, SCRIPTS, test  # noqa: F401


def _dt():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("diagram_text")


class _Limits:
    """Подмена пределов модуля `diagram_text` на время проверки: порядково-зависимые тесты при --shard."""

    def __init__(self, mod, **limits):
        self.mod, self.limits = mod, limits

    def __enter__(self):
        self.saved = {k: getattr(self.mod, k) for k in self.limits}
        for k, v in self.limits.items():
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.saved.items():
            setattr(self.mod, k, v)


def _write(tmp: Path, name: str, text, raw: bool = False) -> str:
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if raw:
        p.write_bytes(text)
    else:
        p.write_text(text, encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------- draw.io

CELLS = [
    '<mxCell id="pool" value="Подсистема-1" style="swimlane;html=1" vertex="1" parent="1">'
    '<mxGeometry x="40" y="40" width="400" height="200" as="geometry"/></mxCell>',
    '<mxCell id="a" value="Принять &lt;b&gt;заявку&lt;/b&gt;&lt;br&gt;PRJ-A" style="rounded=1;html=1" vertex="1" '
    'parent="pool"><mxGeometry x="20" y="40" width="100" height="40" as="geometry"/></mxCell>',
    '<mxCell id="b" value="Проверить?" style="rhombus;html=1" vertex="1" parent="pool">'
    '<mxGeometry x="160" y="40" width="80" height="60" as="geometry"/></mxCell>',
    '<UserObject label="Выдать результат" link="https://example.com/x" id="c"><mxCell style="rounded=1" '
    'vertex="1" parent="pool"><mxGeometry x="300" y="40" width="80" height="40" as="geometry"/></mxCell></UserObject>',
    '<mxCell id="e1" value="" edge="1" source="a" target="b" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>',
    '<mxCell id="e2" value="да" edge="1" source="b" target="c" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>',
    '<mxCell id="e3" value="" edge="1" source="b" target="a" parent="1"><mxGeometry relative="1" as="geometry"/></mxCell>',
    '<mxCell id="l3" value="нет" style="edgeLabel;html=1" vertex="1" connectable="0" parent="e3">'
    '<mxGeometry as="geometry"/></mxCell>',
    '<mxCell id="z" value="" style="line" vertex="1" parent="1"><mxGeometry x="0" y="300" width="10" height="1" '
    'as="geometry"/></mxCell>',
]


def _model(cells=None) -> str:
    return ('<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>'
            + "".join(CELLS if cells is None else cells) + '</root></mxGraphModel>')


def _packed(model: str) -> str:
    comp = zlib.compressobj(wbits=-15)
    raw = comp.compress(urllib.parse.quote(model, safe="~()*!.'").encode("utf-8")) + comp.flush()
    return base64.b64encode(raw).decode("ascii")


def _file(model: str, name: str = "Страница-1", packed: bool = False) -> str:
    return '<mxfile><diagram id="d1" name="%s">%s</diagram></mxfile>' % (name, _packed(model) if packed else model)


@test
def test_drawio_plain_and_packed_files_give_one_description(tmp: Path):
    """Несжатый и сжатый файлы (base64 → deflate → URL-декодирование) дают тот же текст: узлы, контейнеры,
    подписи без HTML, связи с подписью ребра — своей и поставленной отдельной ячейкой."""
    D = _dt()
    plain, kind = D.describe(_write(tmp, "plain.drawio", _file(_model())))
    packed, kind2 = D.describe(_write(tmp, "packed.drawio", _file(_model(), packed=True)))
    assert kind == kind2 == "drawio" and plain and plain == packed, (plain, packed)
    assert "## Диаграмма 1: Страница-1" in plain, plain
    assert "- «Подсистема-1» (`pool`, дорожка)" in plain, plain
    assert "  - «Принять заявку / PRJ-A» (`a`)" in plain, "HTML подписи не очищен или вложенность потеряна: " + plain
    assert "<b>" not in plain and "&lt;" not in plain and "<br" not in plain, plain
    assert "  - «Проверить?» (`b`, решение)" in plain, plain
    assert "link: https://example.com/x" in plain, "свойство UserObject потеряно: " + plain
    assert "- «Проверить?» → «Выдать результат» (да)" in plain, plain
    assert "- «Проверить?» → «Принять заявку / PRJ-A» (нет)" in plain, "подпись, поставленная ячейкой на ребро: " + plain
    assert "`z`" not in plain, "линия-украшение без подписи попала в описание"


@test
def test_drawio_description_does_not_depend_on_cell_order_in_the_file(tmp: Path):
    """Порядок в файле случаен, а описание — нет: узлы идут по координатам и id, связи — по узлам."""
    D = _dt()
    a, _k = D.describe(_write(tmp, "a.drawio", _file(_model())))
    b, _k = D.describe(_write(tmp, "b.drawio", _file(_model(list(reversed(CELLS))))))
    assert a and a == b, (a, b)
    # тот же результат при повторном разборе
    assert D.describe(str(tmp / "a.drawio"))[0] == a


@test
def test_drawio_pages_come_in_file_order_and_empty_ones_are_skipped(tmp: Path):
    D = _dt()

    def page(name, model):
        return '<diagram id="%s" name="%s">%s</diagram>' % (name, name, model)
    one ='<mxCell id="x" value="Первый" vertex="1" parent="1"><mxGeometry as="geometry"/></mxCell>'
    two = '<mxCell id="y" value="Второй" vertex="1" parent="1"><mxGeometry as="geometry"/></mxCell>'
    body = ('<mxfile>' + page("Первая", _model([one])) + '<diagram id="e" name="Пустая"/>'
            + '<diagram id="p" name="Вторая">' + _packed(_model([two])) + '</diagram></mxfile>')
    text, _k = D.describe(_write(tmp, "multi.drawio", body))
    assert text.index("## Диаграмма 1: Первая") < text.index("«Первый»") < text.index("## Диаграмма 2: Вторая") \
        < text.index("«Второй»"), text
    assert "Пустая" not in text, text


@test
def test_drawio_that_is_not_a_diagram_is_named_and_not_described(tmp: Path):
    D = _dt()
    for name, body, reason in (("empty.drawio", "<mxfile></mxfile>", "нет диаграмм"),
                               ("html.drawio", "<html><body>x</body></html>", "не файл draw.io"),
                               ("broken.drawio", "<mxfile><diagram>", "не разобран")):
        text, why = D.describe(_write(tmp, name, body))
        assert text is None and reason in why, (name, text, why)
    text, why = D.describe(_write(tmp, "plain.xml", "<root><a/></root>"))
    assert text is None and why == "не схема", (text, why)


@test
def test_drawio_refuses_entities_doctype_and_foreign_encodings(tmp: Path):
    """XXE-проба: внешняя сущность не читается — ни файл с диска, ни расширение сущностей в подписи."""
    D = _dt()
    secret = tmp / "secret.txt"
    secret.write_text("ТАЙНА-PRJ-A", encoding="utf-8")
    xxe = ('<?xml version="1.0"?><!DOCTYPE mxfile [<!ENTITY x SYSTEM "file://%s">]>'
           '<mxfile><diagram name="&x;">%s</diagram></mxfile>' % (secret.as_posix(), _model()))
    text, why = D.describe(_write(tmp, "xxe.drawio", xxe))
    assert text is None and "DOCTYPE" in why and "ТАЙНА" not in why, (text, why)
    laughs = ('<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
              '<mxfile><diagram name="&b;"/></mxfile>')
    text, why = D.describe(_write(tmp, "laughs.drawio", laughs))
    assert text is None and "DOCTYPE" in why, (text, why)
    # тот же приём внутри сжатой диаграммы
    inner = ('<!DOCTYPE m [<!ENTITY x SYSTEM "file://%s">]><mxGraphModel><root><mxCell id="0"/>'
             '<mxCell id="1" value="&x;" vertex="1" parent="0"/></root></mxGraphModel>' % secret.as_posix())
    text, why = D.describe(_write(tmp, "packed_xxe.drawio",
                                  '<mxfile><diagram name="p">%s</diagram></mxfile>' % _packed(inner)))
    assert text is None and "DOCTYPE" in why, (text, why)
    text, why = D.describe(_write(tmp, "utf16.drawio", _file(_model()).encode("utf-16"), raw=True))
    assert text is None and "UTF-8" in why, (text, why)


@test
def test_drawio_bomb_oversize_deep_and_huge_files_are_refused(tmp: Path):
    D = _dt()
    # бомба: сжатая диаграмма, которая раздувается сверх предела распакованного XML
    big = _model() + "<!--" + "A" * 20_000 + "-->"
    body = '<mxfile><diagram name="b">%s</diagram></mxfile>' % _packed(big)
    path = _write(tmp, "bomb.drawio", body)
    with _Limits(D, MAX_XML_BYTES=3_000):
        text, why = D.describe(path)
    assert text is None and "бомб" in why, (text, why)
    assert D.describe(path)[0] is not None, "с обычными пределами тот же файл читается"
    with _Limits(D, MAX_FILE_BYTES=500):
        text, why = D.describe(_write(tmp, "big.drawio", _file(_model())))
    assert text is None and "больше предела" in why, (text, why)
    deep = "<mxfile>" + "<g>" * (D.MAX_DEPTH + 5) + "</g>" * (D.MAX_DEPTH + 5) + "</mxfile>"
    text, why = D.describe(_write(tmp, "deep.drawio", deep))
    assert text is None and "вложенность" in why, (text, why)
    wide = "<mxfile>" + "<diagram/>" * 100 + "</mxfile>"
    with _Limits(D, MAX_ELEMENTS=50):
        text, why = D.describe(_write(tmp, "wide.drawio", wide))
    assert text is None and "элементов" in why, (text, why)


@test
def test_drawio_truncates_at_the_node_and_edge_caps_and_says_so(tmp: Path):
    D = _dt()
    cells = ['<mxCell id="n%d" value="Узел %d" vertex="1" parent="1"><mxGeometry x="0" y="%d" as="geometry"/></mxCell>'
             % (i, i, i * 10) for i in range(12)]
    cells += ['<mxCell id="e%d" edge="1" source="n%d" target="n%d" parent="1"><mxGeometry as="geometry"/></mxCell>'
              % (i, i, i + 1) for i in range(6)]
    path = _write(tmp, "many.drawio", _file(_model(cells)))
    with _Limits(D, MAX_NODES=5, MAX_EDGES=3):
        text, _k = D.describe(path)
    assert sum(1 for l in text.splitlines() if l.startswith("- «Узел") and "→" not in l) == 5, text
    assert "усечено: показано 5 узлов из 12" in text and "усечено: показано 3 связей из 6" in text, text
    full, _k = D.describe(path)
    assert "усечено" not in full, full
    assert sum(1 for l in full.splitlines() if l.startswith("- «Узел") and "→" not in l) == 12, full
    assert sum(1 for l in full.splitlines() if "→" in l) == 6, full


# --------------------------------------------------------------------------- BPMN

BPMN_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<bpmn:definitions '
             'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" id="Defs_1">')

PROCESS_FIXED = (
    '<bpmn:documentation>Основной сценарий PRJ-A.</bpmn:documentation>'
    '<bpmn:laneSet id="LS_1">'
    '<bpmn:lane id="Lane_1" name="Менеджер"><bpmn:flowNodeRef>Start_1</bpmn:flowNodeRef>'
    '<bpmn:flowNodeRef>Task_1</bpmn:flowNodeRef><bpmn:flowNodeRef>GW_1</bpmn:flowNodeRef></bpmn:lane>'
    '<bpmn:lane id="Lane_2" name="Система"><bpmn:flowNodeRef>Task_2</bpmn:flowNodeRef>'
    '<bpmn:flowNodeRef>Sub_1</bpmn:flowNodeRef><bpmn:flowNodeRef>End_1</bpmn:flowNodeRef></bpmn:lane>'
    '</bpmn:laneSet>')

PROCESS_PARTS = [
    '<bpmn:startEvent id="Start_1" name="Заявка получена"><bpmn:messageEventDefinition messageRef="Msg_1"/>'
    '</bpmn:startEvent>',
    '<bpmn:userTask id="Task_1" name="Проверить заявку"><bpmn:documentation>Менеджер сверяет данные.'
    '</bpmn:documentation></bpmn:userTask>',
    '<bpmn:exclusiveGateway id="GW_1" name="Заявка корректна?" default="F4"/>',
    '<bpmn:serviceTask id="Task_2" name="Отправить в систему"/>',
    '<bpmn:subProcess id="Sub_1" name="Обработка"><bpmn:startEvent id="SS_1"/>'
    '<bpmn:scriptTask id="ST_1" name="Посчитать"><bpmn:script>__import__("os").system("touch SENTINEL")</bpmn:script>'
    '</bpmn:scriptTask><bpmn:endEvent id="SE_1"/>'
    '<bpmn:sequenceFlow id="SF_1" sourceRef="SS_1" targetRef="ST_1"/>'
    '<bpmn:sequenceFlow id="SF_2" sourceRef="ST_1" targetRef="SE_1"/></bpmn:subProcess>',
    '<bpmn:callActivity id="Call_1" name="Уведомить" calledElement="Process_2"/>',
    '<bpmn:endEvent id="End_1" name="Готово"/>',
    '<bpmn:endEvent id="End_2" name="Отклонено"/>',
    '<bpmn:boundaryEvent id="BE_1" attachedToRef="Task_2"><bpmn:timerEventDefinition>'
    '<bpmn:timeDuration>PT1H</bpmn:timeDuration></bpmn:timerEventDefinition></bpmn:boundaryEvent>',
    '<bpmn:sequenceFlow id="F1" sourceRef="Start_1" targetRef="Task_1"/>',
    '<bpmn:sequenceFlow id="F2" sourceRef="Task_1" targetRef="GW_1"/>',
    '<bpmn:sequenceFlow id="F3" name="да" sourceRef="GW_1" targetRef="Task_2"><bpmn:conditionExpression>${ok}'
    '</bpmn:conditionExpression></bpmn:sequenceFlow>',
    '<bpmn:sequenceFlow id="F4" name="нет" sourceRef="GW_1" targetRef="End_2"/>',
    '<bpmn:sequenceFlow id="F5" sourceRef="Task_2" targetRef="Sub_1"/>',
    '<bpmn:sequenceFlow id="F6" sourceRef="Sub_1" targetRef="Call_1"/>',
    '<bpmn:sequenceFlow id="F7" sourceRef="Call_1" targetRef="End_1"/>',
    '<bpmn:sequenceFlow id="F8" sourceRef="BE_1" targetRef="End_2"/>',
    '<bpmn:userTask id="Orphan_1" name="Забытая задача"/>',
    '<bpmn:textAnnotation id="TA_1"><bpmn:text>Только для PRJ-A</bpmn:text></bpmn:textAnnotation>',
    '<bpmn:association id="As_1" sourceRef="TA_1" targetRef="Task_1"/>',
]


def _bpmn(parts=None) -> str:
    return (BPMN_HEAD
            + '<bpmn:collaboration id="Collab_1"><bpmn:participant id="P_A" name="Подсистема-1" processRef="Process_1"/>'
              '<bpmn:participant id="P_B" name="Внешняя система"/>'
              '<bpmn:messageFlow id="MF_1" name="Запрос" sourceRef="Task_2" targetRef="P_B"/></bpmn:collaboration>'
              '<bpmn:message id="Msg_1" name="Заявка получена"/>'
              '<bpmn:process id="Process_1" name="Процесс 1" isExecutable="false">' + PROCESS_FIXED
            + "".join(PROCESS_PARTS if parts is None else parts) + '</bpmn:process>'
              '<bpmn:process id="Process_2" name="Уведомление"><bpmn:startEvent id="N1"/><bpmn:endEvent id="N2"/>'
              '<bpmn:sequenceFlow id="NF" sourceRef="N1" targetRef="N2"/></bpmn:process></bpmn:definitions>')


@test
def test_bpmn_model_is_described_as_a_process_with_summary_first(tmp: Path):
    """Процессы, участники, дорожки, задачи, шлюзы, события и потоки; сводка идёт первой; порядок — от старта
    по потокам, при ветвлении по id; элементы вне пути — отдельным списком."""
    D = _dt()
    import os
    os.chdir(tmp)                      # скрипт-задача лежит в модели текстом: ничего не должно исполниться
    path = _write(tmp, "model.bpmn", _bpmn())
    text, kind = D.describe(path)
    assert kind == "bpmn" and text, (kind, text)
    assert not (tmp / "SENTINEL").exists(), "скрипт из scriptTask был исполнен"
    assert text.index("## Сводка") < text.index("## Процесс: Процесс 1"), text
    for line in ("- Процессов: 2",
                 "- Участники: «Подсистема-1»; «Внешняя система» (внешний, процесса нет)",
                 "- Дорожки: Менеджер; Система",
                 "- Задач: 4 (scriptTask: 1, serviceTask: 1, userTask: 2)",
                 "- Подпроцессов: 1",
                 "- Точки решения: шлюзов 1 (exclusiveGateway: 1)",
                 "вызовов процессов: 1 («Уведомить» → Уведомление); потоков сообщений: 1"):
        assert line in text, (line, text)
    order = [text.index("(`%s`" % i) for i in ("Start_1", "Task_1", "GW_1", "Task_2", "End_2", "Sub_1", "End_1")]
    assert order == sorted(order), "порядок шагов не от старта по потокам: %s" % order
    assert "Участник: «Подсистема-1»" in text and "Описание: Основной сценарий PRJ-A." in text, text
    assert "- Менеджер: «Заявка получена», «Проверить заявку», «Заявка корректна?»" in text, text
    assert "Описание: Менеджер сверяет данные." in text, text
    assert "тип: сообщение «Заявка получена»" in text, text
    assert "Далее: → «Отправить в систему» [да; условие: ${ok}]; → «Отклонено» [нет; по умолчанию]" in text, text
    assert "Граничное событие: [BE_1] — таймер (через PT1H) (`BE_1`)" in text, text
    assert "Внутри подпроцесса:\n     1. Начальное событие (`SS_1`)" in text, text
    assert "Скрипт (текст, не исполняется): __import__" in text, text
    assert "Вызывает процесс: Уведомление" in text, text
    assert "**Элементы вне основного пути**" in text and "Забытая задача" in text, text
    assert "Только для PRJ-A → «Проверить заявку»" in text, text
    assert "«Отправить в систему» → «Внешняя система» (Запрос)" in text, text
    assert "## Процесс: Уведомление" in text, text


@test
def test_bpmn_description_does_not_depend_on_element_order_in_the_file(tmp: Path):
    D = _dt()
    a, _k = D.describe(_write(tmp, "a.bpmn", _bpmn()))
    b, _k = D.describe(_write(tmp, "b.bpmn", _bpmn(list(reversed(PROCESS_PARTS)))))
    assert a and a == b, "порядок элементов в файле изменил описание"


@test
def test_bpmn_and_xml_with_bpmn_root_are_recognised_and_plain_xml_is_not(tmp: Path):
    D = _dt()
    assert D.kind_of(_write(tmp, "m.bpmn", _bpmn())) == "bpmn"
    assert D.kind_of(_write(tmp, "m.xml", _bpmn())) == "bpmn", "xml с корнем BPMN не узнан"
    assert D.kind_of(_write(tmp, "other.xml", '<?xml version="1.0"?><root><a/></root>')) == ""
    assert D.kind_of(_write(tmp, "wsdl.xml", '<definitions xmlns="http://example.com/wsdl/"/>')) == "", \
        "definitions чужого пространства имён принят за BPMN"
    assert D.kind_of(_write(tmp, "d.xml", _file(_model()))) == "drawio"
    text, why = D.describe(str(tmp / "other.xml"))
    assert text is None and why == "не схема", (text, why)
    assert D.kind_of(str(tmp / "нет.xml")) == "" and D.kind_of(str(tmp / "a.docx")) == ""


@test
def test_bpmn_refuses_entities_oversize_and_models_without_processes(tmp: Path):
    D = _dt()
    secret = tmp / "secret.txt"
    secret.write_text("ТАЙНА-PRJ-A", encoding="utf-8")
    xxe = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file://%s">]>'
           '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"><bpmn:process id="P" name="&x;"/>'
           '</bpmn:definitions>' % secret.as_posix())
    text, why = D.describe(_write(tmp, "xxe.bpmn", xxe))
    assert text is None and "DOCTYPE" in why and "ТАЙНА" not in why, (text, why)
    with _Limits(D, MAX_FILE_BYTES=500):
        text, why = D.describe(_write(tmp, "big.bpmn", _bpmn()))
    assert text is None and "больше предела" in why, (text, why)
    text, why = D.describe(_write(tmp, "empty.bpmn", BPMN_HEAD + "</bpmn:definitions>"))
    assert text is None and "нет процессов" in why, (text, why)
    text, why = D.describe(_write(tmp, "foreign.bpmn", "<root/>"))
    assert text is None and "не модель BPMN" in why, (text, why)


@test
def test_bpmn_truncates_a_long_process_at_the_cap_and_says_so(tmp: Path):
    D = _dt()
    n = 12
    parts = ['<bpmn:startEvent id="S"/>'] + ['<bpmn:task id="T%02d" name="Шаг %d"/>' % (i, i) for i in range(n)]
    parts += ['<bpmn:sequenceFlow id="F00" sourceRef="S" targetRef="T00"/>']
    parts += ['<bpmn:sequenceFlow id="F%02d" sourceRef="T%02d" targetRef="T%02d"/>' % (i + 1, i, i + 1)
              for i in range(n - 1)]
    path = _write(tmp, "long.bpmn", BPMN_HEAD + '<bpmn:process id="P" name="Процесс 1">' + "".join(parts)
                  + '</bpmn:process></bpmn:definitions>')
    with _Limits(D, MAX_STEPS=5):
        text, _k = D.describe(path)
    assert "усечено: описание процесса обрезано на 5 элементах" in text, text
    assert "(`T03`" in text and "(`T08`" not in text, text
    full, _k = D.describe(path)
    assert "усечено" not in full and "(`T11`" in full, full


# --------------------------------------------------------------------------- DMN и mermaid

@test
def test_dmn_decision_table_becomes_a_markdown_table(tmp: Path):
    D = _dt()
    dmn = ('<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/" id="D1">'
           '<decision id="dec1" name="Категория заявки"><decisionTable hitPolicy="FIRST">'
           '<input id="i1" label="Сумма"><inputExpression><text>amount</text></inputExpression></input>'
           '<input id="i2" label="Клиент"><inputExpression><text>client</text></inputExpression></input>'
           '<output id="o1" label="Категория" name="category"/>'
           '<rule id="r1"><inputEntry><text>&lt; 1000</text></inputEntry><inputEntry><text>"PRJ-A"</text></inputEntry>'
           '<outputEntry><text>"малая"</text></outputEntry><description>для малых сумм</description></rule>'
           '<rule id="r2"><inputEntry><text>&gt;= 1000</text></inputEntry><inputEntry><text></text></inputEntry>'
           '<outputEntry><text>"крупная"</text></outputEntry></rule></decisionTable></decision></definitions>')
    path = _write(tmp, "rules.dmn", dmn)
    assert D.kind_of(path) == "dmn" and D.kind_of(_write(tmp, "rules.xml", dmn)) == "dmn"
    text, kind = D.describe(path)
    assert kind == "dmn" and "## Решение: Категория заявки" in text, text
    assert "Политика попадания: FIRST" in text, text
    assert "| Вход: Сумма | Вход: Клиент | Выход: Категория | Описание |" in text, text
    assert '| < 1000 | "PRJ-A" | "малая" | для малых сумм |' in text, text
    assert '| >= 1000 | — | "крупная" |  |' in text, text


@test
def test_mermaid_diagram_is_kept_as_a_code_block_and_long_ones_are_cut(tmp: Path):
    D = _dt()
    src = "graph TD\n  A[Заявка PRJ-A] --> B{Проверка}\n  B -->|да| C[Выдача]\n"
    text, kind = D.describe(_write(tmp, "flow.mmd", src))
    assert kind == "mermaid" and "```mermaid\ngraph TD" in text and text.rstrip().endswith("```"), text
    assert "A[Заявка PRJ-A] --> B{Проверка}" in text, text
    with _Limits(D, MAX_MERMAID=20):
        cut, _k = D.describe(str(tmp / "flow.mmd"))
    assert "усечено: показаны первые 20 знаков" in cut, cut
    fenced, _k = D.describe(_write(tmp, "fence.mmd", "graph TD\n  A[\"```\"] --> B\n"))
    assert fenced.startswith("## Диаграмма mermaid\n\n````mermaid"), fenced
    text, why = D.describe(_write(tmp, "empty.mmd", "  \n"))
    assert text is None and "пустая" in why, (text, why)


@test
def test_sync_keeps_bpmn_and_dmn_attachments_under_their_own_extension(tmp: Path):
    """`.bpmn` и `.dmn` — известные расширения вложений: у длинного имени они не теряются при сокращении."""
    sys.path.insert(0, str(SCRIPTS))
    CE = importlib.import_module("confluence_export")
    assert ".bpmn" in CE.ASSET_EXTS and ".dmn" in CE.ASSET_EXTS
    exp = CE.Exporter(None, str(tmp / "m"), "https://wiki.example.com", "S", False)
    folder = "П" * 100 + "_assets"
    long = "Очень длинное название процесса согласования заявки за 2026 год и ещё много слов"
    for ext in (".bpmn", ".dmn"):
        got = exp.asset_name(long + ext, folder)
        assert got.endswith(ext), got
    assert exp.asset_name("Процесс 1.bpmn", "X_assets") == "Процесс 1.bpmn"
