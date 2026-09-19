"""Two journals of one call: the operator's account and the frames behind it.

Both are kept the same way — one file per site, replaced by every call —
because a fault on a line repeats on every call, and one call's worth of
frames is what anybody actually reads.
"""
from backend.services import poll_journal


def lines(*rows):
    return [{"seq": i + 1, "level": level, "message": message}
            for i, (level, message) in enumerate(rows)]


def test_frames_go_only_to_the_technical_journal(tmp_path):
    path = tmp_path / "enterprise-1.log"
    poll_journal.start(path, "Хлібозавод 3")
    poll_journal.append(path, lines(
        ("info", "Набираю: ATDP+380671234567"),
        ("debug", "--> 01 00 00 04 9a 9f 9f"),
        ("debug", "<-- 00 28 01 04 9a  (45 Б за 210 мс)"),
        ("info", "Прочитано годин: 6"),
    ))

    operator = poll_journal.read(path)
    assert "Набираю" in operator and "Прочитано годин" in operator
    assert "9a 9f 9f" not in operator

    technical = poll_journal.read(poll_journal.debug_path(path))
    assert "9a 9f 9f" in technical
    # The account is in there too: frames without it read as a stream of hex
    # with nothing to hang them on.
    assert "Набираю" in technical


def test_a_new_call_replaces_both(tmp_path):
    path = tmp_path / "enterprise-1.log"
    poll_journal.start(path, "перший дзвінок")
    poll_journal.append(path, lines(("debug", "--> старий кадр")))

    poll_journal.start(path, "другий дзвінок")
    poll_journal.append(path, lines(("debug", "--> новий кадр")))

    technical = poll_journal.read(poll_journal.debug_path(path))
    assert "новий кадр" in technical
    assert "старий кадр" not in technical


def test_the_outcome_closes_both(tmp_path):
    path = tmp_path / "enterprise-1.log"
    poll_journal.start(path, "Волана")
    poll_journal.append(path, lines(("debug", "--> 01 41 00 02")))
    poll_journal.finish(path, "error", "Немає носійної", {"hour": 0, "day": 0}, 37000)

    for target in (path, poll_journal.debug_path(path)):
        text = poll_journal.read(target)
        assert "Помилка" in text and "Немає носійної" in text


def test_a_runaway_call_does_not_fill_the_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(poll_journal, "MAX_DEBUG_BYTES", 2048)
    path = tmp_path / "enterprise-1.log"
    poll_journal.start(path, "нескінченний дзвінок")
    for _ in range(200):
        poll_journal.append(path, lines(("debug", "--> " + "aa " * 60)))

    size = poll_journal.debug_path(path).stat().st_size
    assert size < 2048 * 3          # stops soon after the cap, not at it exactly


def test_a_site_never_polled_has_no_journal(tmp_path):
    path = tmp_path / "enterprise-404.log"
    assert poll_journal.read(path) is None
    assert poll_journal.read(poll_journal.debug_path(path)) is None
