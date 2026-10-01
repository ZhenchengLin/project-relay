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
    assert "bash syntax error" in incomplete_reason("echo 'oops")


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


from project_relay.relay.shell import script_problems


def test_clean_scripts_have_no_problems():
    clean = [
        "echo hi",
        "bash <<'BASH'\nset -euo pipefail\npython3 - <<'PY'\nx = ...\nprint(x)\nPY\necho done\nBASH",
        'echo "完成：“一步”"',                         # curly quotes inside a string are fine
        "cat <<EOF > out.txt\nvalue=$HOME\nEOF",       # unquoted heredoc: not compiled as Python
        "python3 - <<PY\nprint($x)\nPY",               # unquoted delimiter: shell expands, not checked
        "sort < input.txt > output.txt",
        "# the evaluation takes ... about 5 minutes\necho ok",
    ]
    for script in clean:
        assert script_problems(script) == [], script


def test_bash_syntax_error_names_the_line():
    problems = script_problems("echo one\nif true; then\necho two\n")
    assert problems and problems[0].startswith("line ") and "bash syntax error" in problems[0]


def test_python_heredoc_syntax_error_is_found_with_script_line():
    script = "echo start\npython3 - <<'PY'\nimport os\nprint(os.getcwd()\nPY\necho end"
    problems = script_problems(script)
    assert len(problems) == 1 and "Python syntax error in the PY block" in problems[0]
    assert problems[0].startswith("line ")


def test_python_block_inside_outer_bash_heredoc():
    script = "bash <<'BASH'\npython3 -B - <<'PY'\ndef f(:\n    pass\nPY\nBASH"
    assert any("Python syntax error" in p for p in script_problems(script))


def test_chat_debris_is_reported_per_line():
    script = "echo a\n```\necho\u00a0b\n# ... rest unchanged\ncp x /path/to/dest\nmkdir <your-folder>"
    problems = script_problems(script)
    text = " | ".join(problems)
    assert "line 2: a Markdown code fence" in text
    assert "line 3: contains a non-breaking space" in text
    assert "line 4: part of the script is left out" in text
    assert "line 5: contains a placeholder" in text and "line 6: contains a placeholder" in text


def test_windows_line_endings_and_bare_ellipsis():
    assert "Windows line endings" in script_problems("echo a\r\necho b\r\n")[0]
    assert any("left out" in p for p in script_problems("echo a\n...\necho b"))


def test_cut_off_script_still_reported_first():
    assert script_problems("bash <<'BASH'\necho a")[0].startswith("heredoc 'BASH' is never closed")
