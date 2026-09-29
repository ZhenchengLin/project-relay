import pytest

from project_relay.relay.shell import dangerous_reason, extract_shell_blocks

BLOCKED = [
    "git stash",
    "git stash push -m wip",
    "git stash pop",
    "git checkout -- src/app.py",
    "git checkout .",
    "git checkout -f main",
    "git restore src/app.py",
    "git restore .",
    "git switch --discard-changes main",
    "git branch -D feature",
    "git branch --delete --force feature",
    "git push origin +main",
    "rm -rf ~",
    "rm -rf $HOME/",
    "git reset --hard HEAD",
    "git clean -fdx",
    "sudo rm x",
]

ALLOWED = [
    "git stash list",
    "git stash show -p",
    "git checkout -b feature/x",
    "git checkout main",
    "git switch -c feature/y",
    "git restore --staged src/app.py",
    "git branch -d merged",
    "git push origin feature/x",
    "rm -rf build/",
    "pytest -q && git commit -m 'x'",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_blocked(command):
    assert dangerous_reason(command), command


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed(command):
    assert dangerous_reason(command) is None, (command, dangerous_reason(command))


FENCE = "`" * 3


def test_extract_one_bash_block():
    assert extract_shell_blocks(f"Do this:\n{FENCE}bash\necho hello\n{FENCE}") == ["echo hello"]


def test_non_shell_block_is_ignored():
    assert extract_shell_blocks(f"{FENCE}python\nprint('hi')\n{FENCE}") == []


def test_unlabeled_block_needs_shell_shape():
    assert extract_shell_blocks(f"{FENCE}\ngit status\n{FENCE}") == ["git status"]
    assert extract_shell_blocks(f"{FENCE}\nimport os\nprint(os)\n{FENCE}") == []


def test_multiple_shell_blocks_remain_multiple():
    text = f"{FENCE}bash\necho one\n{FENCE}\n{FENCE}sh\necho two\n{FENCE}"
    assert len(extract_shell_blocks(text)) == 2


from project_relay.relay.shell import incomplete_reason, unterminated_heredoc

TRUNCATED = """bash <<'BASH'
set -euo pipefail
python3 - <<'PY'
print("x")
if True:
    raise SystemExit(
        "STOP: unexpected"""


def test_truncated_script_is_detected():
    assert unterminated_heredoc(TRUNCATED) == "BASH"
    assert "never closed" in incomplete_reason(TRUNCATED)


def test_complete_nested_heredocs_pass():
    script = "bash <<'BASH'\nset -e\npython3 - <<'PY'\nprint(1)\nPY\necho done\nBASH"
    assert unterminated_heredoc(script) is None
    assert incomplete_reason(script) is None


def test_tab_stripped_heredoc_and_herestring():
    assert unterminated_heredoc("cat <<-EOF\n\tx\n\tEOF\nread x <<< 'y'") is None


def test_unterminated_quote_fails_bash_n():
    assert incomplete_reason("echo 'oops").startswith("bash -n")


@pytest.mark.parametrize("command", [
    "git -C /repo reset --hard",
    "git -c core.pager=cat reset --hard HEAD~1",
    "git --git-dir=/r/.git --work-tree=/r clean -fdx",
    "git -c user.name=x push --force origin main",
    "git -C repo stash push",
    "git --no-pager checkout -- .",
    "git -C repo branch -D old",
])
def test_git_global_options_do_not_bypass_the_guard(command):
    assert dangerous_reason(command), command


def test_git_global_options_still_allow_safe_commands():
    assert dangerous_reason("git -C repo status --short") is None
    assert dangerous_reason("git -c color.ui=never log -1") is None
