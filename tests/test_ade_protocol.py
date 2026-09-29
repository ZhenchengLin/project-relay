from project_relay.relay import ade

FENCE = "`" * 3


def test_parse_task():
    reply = "Plan:\n\nRELAY_TASK\nRun the unit tests in backend/ and print the summary.\nEND_RELAY_TASK\n"
    d = ade.parse_plan(reply)
    assert d.kind == "TASK" and d.text == "Run the unit tests in backend/ and print the summary."


def test_parse_task_tolerates_markdown_emphasis():
    d = ade.parse_plan("**RELAY_TASK**\nDo X\n`END_RELAY_TASK`")
    assert d.kind == "TASK" and d.text == "Do X"


def test_unclosed_or_empty_task_is_not_a_task():
    assert ade.parse_plan("RELAY_TASK\nDo X").kind == "NONE"
    assert ade.parse_plan("RELAY_TASK\n\nEND_RELAY_TASK").kind == "NONE"


def test_parse_done_and_ask_human():
    assert ade.parse_plan("All verified.\n\nRELAY_DONE").kind == "DONE"
    d = ade.parse_plan("RELAY_ASK_HUMAN: need the API key location")
    assert d.kind == "ASK_HUMAN" and d.text == "need the API key location"


def test_task_wins_over_mentions_of_done():
    d = ade.parse_plan("Not RELAY_DONE yet.\nRELAY_TASK\nstep\nEND_RELAY_TASK")
    assert d.kind == "TASK"


def test_parse_review():
    assert ade.parse_review("Looks fine.\nRELAY_APPROVE").kind == "APPROVE"
    d = ade.parse_review("RELAY_REVISE: do not push\nalso keep untracked files")
    assert d.kind == "REVISE" and "do not push" in d.text and "untracked" in d.text
    assert ade.parse_review("ok").kind == "NONE"


def test_review_policy():
    assert ade.review_reasons("pytest -q", "risky") == []
    assert ade.review_reasons("git add a && git commit -m x && git push", "risky") == ["changes Git history"]
    assert "deletes files" in ade.review_reasons("rm build/cache.bin", "risky")
    assert "installs or removes packages" in ade.review_reasons("pip install requests", "risky")
    assert ade.review_reasons("pytest", "always") == ["review policy is 'always'"]
    assert ade.review_reasons("git push", "never") == []
    assert ade.review_reasons("echo x\n" * 250, "risky") == ["longer than 200 lines"]
    assert ade.review_reasons("echo format rm-rf-report", "risky") == []


def test_prompts_carry_protocols():
    k = ade.pm_kickoff(project="P", root="/r", goal="Ship it", rules="no push", git={"branch": "main"})
    assert "Ship it" in k and "no push" in k and ade.TASK_START in k
    w = ade.worker_task(task="Do X", last_result="out")
    assert "Do X" in w and "EXACTLY ONE fenced bash block" in w
    r = ade.pm_review(task="T", command="git push", reasons=["changes Git history"])
    assert ade.APPROVE in r and "git push" in r


def test_review_sees_git_with_global_options():
    cmd = 'git -c user.email=r@t -c user.name=r commit -qm "add hello"'
    assert ade.review_reasons(cmd, "risky") == ["changes Git history"]
    assert ade.review_reasons("git -C repo push origin main", "risky") == ["changes Git history"]
    assert ade.review_reasons("git -C repo status", "risky") == []
