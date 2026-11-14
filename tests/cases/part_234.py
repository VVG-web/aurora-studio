"""Проверки движка: отбор кандидатов по инвертированному индексу (часть 234).

Живой замер на базе в 2661 карточку: первый `fuse` стоил 2,18 с, потому что звал
`score` (и разбор тела) для КАЖДОЙ карточки, хотя совпадение хотя бы одного слова
запроса есть у сотен. `measure_rarity` и так обходит все слова всех карточек —
рядом строится указатель «слово → номера карточек», и `fuse` сверяет только тех,
у кого есть хоть одно слово. Выдача обязана остаться той же: карточка без
совпадения и раньше получала ноль и в слияние не входила.
"""
import sys

from harness import SCRIPTS, test


def _fake(stem: str, text: str):
    class Fake:
        def __init__(self):
            self.stem = self.title = stem
            self.path = f"AuroraKnowledgeDB/Concepts/{stem}.md"
            self.text = text
            self.summary, self.aliases, self.tags, self.fm = "", [], "", {}

    return Fake()


@test
def test_fuse_scans_only_cards_with_a_query_word(tmp):
    """Индекс сужает перебор, а не меняет выдачу: лишние карточки и так давали ноль."""
    sys.path.insert(0, str(SCRIPTS))
    import ctx_pack as P

    cards = {f"Карта-{i:02d}": _fake(f"Карта-{i:02d}", "общие слова про процесс")
             for i in range(60)}
    cards["КПП"] = _fake("КПП", "код причины постановки на учёт")
    P.measure_rarity(cards)

    got = [c.stem for _w, c in P.fuse(cards, "код причины постановки", {})]
    assert got and got[0] == "КПП", f"редкое слово не подняло карточку: {got[:5]}"
    assert P.LAST_FUSE["scanned"] < P.LAST_FUSE["total"], \
        f"перебрана вся база: {P.LAST_FUSE}"
    assert P.LAST_FUSE["scanned"] <= 5, \
        f"указатель отдал почти всю базу на редкий запрос: {P.LAST_FUSE}"

    # Общий запрос — кандидатов больше, но всё равно меньше базы.
    got_common = [c.stem for _w, c in P.fuse(cards, "процесс", {})]
    assert len(got_common) == 60 and P.LAST_FUSE["scanned"] == 60, \
        f"общее слово потеряло карточки: {len(got_common)} {P.LAST_FUSE}"


@test
def test_fuse_ranking_is_the_same_as_a_full_scan(tmp):
    """Порядок и веса не меняются: индекс — только способ не перебирать пустые."""
    sys.path.insert(0, str(SCRIPTS))
    import ctx_pack as P

    texts = {
        "Заявка": "заявка проходит статусы, заявка создаётся",
        "Платёж": "обеспечительный платёж возвращается по заявлению",
        "Справочник": "общие слова без совпадений",
        "Возврат": "возврат платежа после аннулирования заявки",
    }
    cards = {name: _fake(name, text) for name, text in texts.items()}
    P.measure_rarity(cards)
    ask = P.query_words("вернуть обеспечительный платёж")
    brute = sorted(((P.score(c, "вернуть обеспечительный платёж", ask=ask), c.stem)
                    for c in cards.values() if P.score(c, "вернуть обеспечительный платёж", ask=ask) > 0),
                   key=lambda x: (-x[0], x[1]))
    old_rank = P.RETRIEVAL["pagerank"]
    P.RETRIEVAL["pagerank"] = 0
    try:
        got = [(round(w * max(s for s, _ in brute) or 1, 6), c.stem)
               for w, c in P.fuse(cards, "вернуть обеспечительный платёж", {})]
    finally:
        P.RETRIEVAL["pagerank"] = old_rank
    assert [stem for _w, stem in got] == [stem for _s, stem in brute], \
        f"индекс переставил выдачу:\n{got}\n{brute}"


@test
def test_fuse_rebuilds_the_index_for_another_cards_dict(tmp):
    """Новый словарь — новый индекс: старые номера карточек не должны на него влиять."""
    sys.path.insert(0, str(SCRIPTS))
    import ctx_pack as P

    first = {"Старая": _fake("Старая", "старое слово")}
    P.measure_rarity(first)
    assert P.fuse(first, "старое", {})
    second = {"Новая": _fake("Новая", "новое слово")}
    P.measure_rarity(second)
    got = [c.stem for _w, c in P.fuse(second, "новое", {})]
    assert got == ["Новая"], f"индекс остался от прежнего словаря: {got}"

@test
def test_prefilter_is_not_rebuilt_for_a_small_top_up(tmp):
    """Досборка трёх кусков не пересобирает слой, который уже признан бесполезным.

    Живой замер: `kb:embed --apply` с тремя устаревшими кусками тратил 10,15 с на
    `build_prefilter`, после чего вердикт был «кандидатов 100 % базы» — работа
    выбрасывалась. Слой нужен только при большой досборке или по `--all`.
    """
    sys.path.insert(0, str(SCRIPTS))
    import kb_embed as E

    assert E.prefilter_due(False, 3, 0, 5000, False) is False, \
        "малая досборка снова строит бесполезный предфильтр"
    assert E.prefilter_due(False, 900, 0, 5000, False) is True, \
        "большая досборка не пересчитывает слой"
    assert E.prefilter_due(True, 1, 0, 5000, True) is True, \
        "--all обязан пересчитать предфильтр"
    assert E.prefilter_due(False, 3, 0, 5000, True) is False, \
        "годный прежний слой пересобирается без причины"


@test
def test_build_batch_takes_two_waves_when_the_plan_is_longer(tmp):
    """Две волны подряд дешевле двух полных кругов тезисов, выноса и связей."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_core as A
    import agent_runner as R

    cfg = A.parse_config({"AURORA_AGENT_BACKEND_1_URL": "u",
                          "AURORA_AGENT_BACKEND_1_MODEL": "m",
                          "AURORA_AGENT_PARALLEL": "24"})
    assert R.lap_steps(cfg) == 24, "прежний вызов без очереди изменил ответ"
    assert R.lap_steps(cfg, 10) == 24, "короткий план не должен растягивать партию"
    assert R.lap_steps(cfg, 1000) == 48, \
        f"длинный план не растянул партию до двух волн: {R.lap_steps(cfg, 1000)}"


@test
def test_critic_new_checks_only_new_cards(tmp):
    """Дописывание в существующую карточку проверяет арифметика, а не вторая модель."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    cards = [{"title": "Новая-сущность", "sections": "1"},
             {"into": "Известная-сущность", "sections": "2"}]
    assert R.critic_cards(cards, True) == [cards[0]], \
        "критик снова получает дописывание в существующую карточку"
    assert R.critic_cards(cards, False) == cards, "прежний --critic изменил поведение"
    assert R.critic_cards([cards[1]], True) == [], \
        "чистое дописывание всё ещё идёт к критику"


@test
def test_base_overview_lives_until_the_next_batch(tmp):
    """Обзор базы не пересобирается каждые две минуты и снимается между партиями."""
    sys.path.insert(0, str(SCRIPTS))
    import agent_runner as R

    assert R.HYB_TTL >= 1800, f"обзор снова живёт {R.HYB_TTL} с"
    R._HYB["/tmp/проект"] = (0.0, {})
    R.drop_hyb_cache()
    assert R._HYB == {}, "сброс обзора не очистил кеш"


@test
def test_a_model_that_missed_its_timeout_stays_last_for_the_run(tmp):
    """Не ответившая по сроку модель уходит в конец кольца до конца прогона.

    Живой прогон 08.11.2026: Момус девять раз ждал `qwen3.5-122b` по 90 с, а запасной
    `deepseek-v4-flash` отвечал за 26–38 с. Пятнадцатиминутной отлучки хватало,
    чтобы модель снова получила шанс и снова не уложилась.
    """
    sys.path.insert(0, str(SCRIPTS))
    import agent_core as A

    assert A.SLOW_TIMEOUT_FOR >= 3600, f"отлучка снова короткая: {A.SLOW_TIMEOUT_FOR}"
    src = (SCRIPTS / "agent_core.py").read_text(encoding="utf-8")
    assert 'SLOW[b["n"]] = time.time() + SLOW_TIMEOUT_FOR' in src, \
        "таймаут снова отправляет модель в конец кольца только на 15 минут"
