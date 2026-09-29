from project_relay.core import aggregate_votes, Vote


def test_no_loop_vote_continues():
    d = aggregate_votes([
        Vote("det", "UNCERTAIN", ""),
        Vote("llm", "UNCERTAIN", ""),
        Vote("logic", "PROGRESS", ""),
    ])
    assert d.status == "CONTINUE"


def test_one_loop_vote_is_uncertain():
    d = aggregate_votes([
        Vote("det", "LOOP", ""),
        Vote("llm", "UNCERTAIN", ""),
        Vote("logic", "PROGRESS", ""),
    ])
    assert d.status == "UNCERTAIN"
