"""Проверки движка Aurora, часть 163: потоки параллельной работы в пульсе шага.

Каркас и помощники — tests/harness.py. Шаг с потоками кладёт в свой пульс (`pulse-<pid>.json` папки прогона), сколько
у него слотов и сколько из них выпало: провайдер в карантине теряет все свои слоты, суженный после отказов 529 —
слоты сверх выученного предела тяжёлых. По этому панель рисует кнопку в шапке «20 (−4)». Сеть и модели не
используются; папка прогона — временная.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from cases import part_36 as P36
from harness import test

CFG = {"backends": [{"n": 1, "name": "Рабочий шлюз"}, {"n": 2, "name": "Запасной"}]}


class _Pulse:
    """Пульс процесса — в папку теста; состояние кольца и пульса возвращается на место."""

    def __init__(self, AG, folder: Path):
        self.AG, self.folder = AG, folder

    def __enter__(self):
        AG = self.AG
        self.keep = (os.environ.get("AURORA_RUN_DIR"), json.loads(json.dumps({k: v for k, v in AG._PULSE.items()
                                                                              if k != "calls"})), dict(AG._PULSE["calls"]))
        os.environ["AURORA_RUN_DIR"] = str(self.folder)
        AG._PULSE.update(seq=0, calls={}, answered=0, failed=0, last_answer=0.0, last_fail=0.0, why="", path=None,
                         rings=0, threads=None)
        return self

    def read(self) -> dict:
        return json.loads((self.folder / f"pulse-{os.getpid()}.json").read_text(encoding="utf-8"))

    def __exit__(self, *a):
        AG = self.AG
        AG._pulse_drop()
        if self.keep[0] is None:
            os.environ.pop("AURORA_RUN_DIR", None)
        else:
            os.environ["AURORA_RUN_DIR"] = self.keep[0]
        AG._PULSE.clear()
        AG._PULSE.update(self.keep[1], calls=self.keep[2])
        AG._PULSE.pop("threads", None)
        for d in (AG.DOWN, AG.DOWN_WHY, AG.HEAVY_LEARNED, AG.SIZES):
            d.clear()


@test
def test_the_step_pulse_names_its_threads_and_what_fell_out(tmp: Path):
    """Шаг на 12 потоках (8 у рабочего шлюза, 4 у запасного): пока всё работает — «12 из 12», без строк. Рабочий
    сужен после 529 до двух тяжёлых, запасной в карантине после трёх 5xx — работают 2: строка на каждого, с причиной
    и сроком карантина. Карантин вышел — его потоки вернулись. Без шага с потоками пульс о потоках молчит."""
    P36._path()
    import agent_core as AG
    with _Pulse(AG, tmp) as pulse:
        AG.pulse_begin({"n": 1, "name": "Рабочий шлюз"}, {"model": "m"}, 60)
        assert "threads" not in pulse.read(), "пульс без шага с потоками заговорил о потоках"
        AG.pulse_threads(CFG, [1] * 8 + [2] * 4)
        th = pulse.read()["threads"]
        assert (th["slots"], th["now"], th["rows"]) == (12, 12, []), th
        now = time.time()
        AG.HEAVY_LEARNED[1] = {"cap": 2, "from": 20000, "ok": 0}
        AG.DOWN[2] = now + 600
        AG.DOWN_WHY[2] = (AG.DOWN[2], AG.link_why("server_error", 2, "m", 503))
        AG.pulse_begin({"n": 1, "name": "Рабочий шлюз"}, {"model": "m"}, 60)       # любой запрос обновляет пульс
        th = pulse.read()["threads"]
        assert (th["slots"], th["now"]) == (12, 2), th
        rows = {r["n"]: r for r in th["rows"]}
        assert rows[1]["kind"] == "heavy" and (rows[1]["cut"], rows[1]["cap"], rows[1]["name"]) == (6, 2, "Рабочий шлюз"), rows
        assert rows[2]["kind"] == "down" and rows[2]["cut"] == 4 and rows[2]["why"] == "server_error", rows
        assert abs(rows[2]["until"] - (now + 600)) < 1, rows[2]
        later = AG.threads_now(AG._PULSE["threads"], now=now + 601)
        assert later["now"] == 6 and [r["n"] for r in later["rows"]] == [1], later
        AG.HEAVY_LEARNED[1]["cap"] = 8
        assert AG.threads_now(AG._PULSE["threads"], now=now + 601)["now"] == 12, "предел, доросший до ширины, всё ещё вычитается"


@test
def test_every_long_step_puts_its_threads_into_the_pulse(tmp: Path):
    """Строка «потоков: N» шага (`threads_line`) — одна на все длинные шаги; ровно те слоты, что пойдут в работу,
    уходят и в пульс. Шаг в один поток — один слот."""
    P36._path()
    import agent_core as AG
    import agent_runner as ar
    env = {}
    for i, w in ((1, 8), (2, 4)):
        env.update({f"AURORA_AGENT_BACKEND_{i}_URL": f"https://llm{i}.example.com/v1",
                    f"AURORA_AGENT_BACKEND_{i}_MODEL": "m", f"AURORA_AGENT_BACKEND_{i}_WIDTH": str(w)})
    env["AURORA_AGENT_PARALLEL"] = "auto"
    cfg = AG.parse_config(env)
    with _Pulse(AG, tmp) as pulse:
        line = ar.threads_line(cfg, 10)
        th = pulse.read()["threads"]
        assert th["slots"] == 10 and "потоков: 10" in line, (th, line)
        ar.threads_line(cfg, 1, "карточка одна")
        assert pulse.read()["threads"]["slots"] == 1, pulse.read()["threads"]
