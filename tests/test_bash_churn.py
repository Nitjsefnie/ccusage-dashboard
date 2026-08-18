"""bash_churn.py — line churn recovered from Bash command text.

Most editing in a bypass-permissions session never touches Edit/Write:
it goes through a heredoc, an inline patch, or a python one-liner. Only
what is DIRECTLY ENUMERABLE from the command text counts — no execution,
no guessing. A shape we cannot count exactly contributes 0/0 rather than
an estimate.
"""
import warnings

from backend.bash_churn import bash_churn


# --- heredoc bodies redirected into a file --------------------------------

def test_cat_heredoc_into_file_counts_body_lines():
    assert bash_churn("cat > /tmp/f.txt <<'EOF'\nalpha\nbeta\nEOF\n") == (2, 0)


def test_cat_append_heredoc_counts_body_lines():
    """An append adds lines and deletes none — the same shape as Write."""
    assert bash_churn("cat >> notes.md <<'EOF'\na\nb\nc\nEOF\n") == (3, 0)


def test_overwrite_heredoc_reports_zero_deletions():
    """`cat > F` on an existing file destroys its old content, but the
    call does not carry it — the same limitation Write has, counted the
    same way (0 deletions) rather than guessed."""
    assert bash_churn("cat > existing.py <<'EOF'\nnew\nEOF\n") == (1, 0)


def test_tee_heredoc_counts_body_lines():
    assert bash_churn("tee -a /etc/hosts <<'EOF'\n1.2.3.4 host\nEOF\n") == (1, 0)


def test_redirect_written_after_the_opener_still_counts():
    assert bash_churn("cat <<'EOF' > out.txt\nx\ny\nEOF\n") == (2, 0)


def test_empty_heredoc_body_counts_zero():
    assert bash_churn("cat > f <<'EOF'\nEOF\n") == (0, 0)


def test_unquoted_heredoc_tag_is_recognised():
    assert bash_churn("cat > f <<EOF\none\ntwo\nEOF\n") == (2, 0)


def test_dash_suppressed_heredoc_terminator_is_recognised():
    """<<- allows a tab-indented terminator; the body still counts."""
    assert bash_churn("cat > f <<-'EOF'\n\ta\n\tb\n\tEOF\n") == (2, 0)


def test_multiple_file_heredocs_in_one_command_sum():
    cmd = ("cat > a <<'A'\n1\n2\nA\n"
           "cat >> b <<'B'\n3\nB\n")
    assert bash_churn(cmd) == (3, 0)


# --- heredocs that are NOT file content ------------------------------------

def test_commit_message_heredoc_is_not_churn():
    cmd = ("git add x && git commit -q -F - <<'EOF'\n"
           "fix: a thing\n\nbody line\nEOF\n")
    assert bash_churn(cmd) == (0, 0)


def test_commit_message_via_command_substitution_is_not_churn():
    cmd = "git commit -m \"$(cat <<'EOF'\nsubject\n\nbody\nEOF\n)\"\n"
    assert bash_churn(cmd) == (0, 0)


def test_psql_heredoc_is_not_churn():
    assert bash_churn("psql -d db <<'SQL'\nSELECT 1;\nSQL\n") == (0, 0)


def test_output_capture_redirect_is_not_churn():
    """Redirecting a command's OUTPUT to a file is not enumerable churn:
    the bytes are produced by running it, not present in the call."""
    assert bash_churn("python3 -m pytest tests/ -q > /tmp/out.txt") == (0, 0)


def test_plain_command_yields_zero():
    assert bash_churn("ls -la /root && git status --porcelain") == (0, 0)


def test_empty_command_yields_zero():
    assert bash_churn("") == (0, 0)


# --- inline patches ---------------------------------------------------------

def test_inline_git_apply_counts_hunk_lines():
    """+/- hunk lines are exact churn; the +++/--- file headers are not."""
    cmd = ("git apply <<'PATCH'\n"
           "--- a/x.py\n"
           "+++ b/x.py\n"
           "@@ -1,3 +1,4 @@\n"
           " ctx\n"
           "-old one\n"
           "-old two\n"
           "+new one\n"
           "+new two\n"
           "+new three\n"
           "PATCH\n")
    assert bash_churn(cmd) == (3, 2)


def test_inline_patch_command_counts_hunk_lines():
    cmd = ("patch -p1 <<'DIFF'\n"
           "--- a/y\n"
           "+++ b/y\n"
           "@@ -1 +1 @@\n"
           "-a\n"
           "+b\n"
           "DIFF\n")
    assert bash_churn(cmd) == (1, 1)


# --- python bodies ----------------------------------------------------------

def test_python_heredoc_literal_replace_counts_both_sides():
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "p = pathlib.Path('backend/db.py')\n"
           "s = p.read_text()\n"
           "s = s.replace('a\\nb', 'x\\ny\\nz')\n"
           "p.write_text(s)\n"
           "PY\n")
    assert bash_churn(cmd) == (3, 2)


def test_python_heredoc_re_sub_literals_counted():
    cmd = ("python3 - <<'PY'\n"
           "import re, pathlib\n"
           "p = pathlib.Path('f')\n"
           "p.write_text(re.sub('old', 'new\\nnew2', p.read_text()))\n"
           "PY\n")
    assert bash_churn(cmd) == (2, 1)


def test_python_heredoc_that_never_writes_is_not_churn():
    """A read-and-print script mutates nothing on disk, however many
    replace() calls it makes on the way to stdout."""
    cmd = ("python3 - <<'PY'\n"
           "s = open('f').read()\n"
           "print(s.replace('a', 'b\\nc'))\n"
           "PY\n")
    assert bash_churn(cmd) == (0, 0)


def test_python_replace_with_non_literal_arguments_is_not_counted():
    """Variables are only knowable by running the script — 0, not a guess."""
    cmd = ("python3 - <<'PY'\n"
           "import pathlib, sys\n"
           "old, new = sys.argv[1], sys.argv[2]\n"
           "p = pathlib.Path('f')\n"
           "p.write_text(p.read_text().replace(old, new))\n"
           "PY\n")
    assert bash_churn(cmd) == (0, 0)


def test_python_write_text_of_a_string_literal_counts_as_added():
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "pathlib.Path('f').write_text('one\\ntwo\\nthree\\n')\n"
           "PY\n")
    assert bash_churn(cmd) == (3, 0)


def test_python_dash_c_literal_replace_is_counted():
    cmd = ("python3 -c 'import pathlib\np=pathlib.Path(\"f\")\n"
           "p.write_text(p.read_text().replace(\"a\", \"b\\nc\"))'")
    assert bash_churn(cmd) == (2, 1)


def test_syntactically_invalid_python_yields_zero():
    cmd = ("python3 - <<'PY'\n"
           "def broken(:\n"
           "PY\n")
    assert bash_churn(cmd) == (0, 0)


def test_non_python_interpreter_heredoc_yields_zero():
    cmd = ("node - <<'JS'\n"
           "require('fs').writeFileSync('f', 'a\\nb')\n"
           "JS\n")
    assert bash_churn(cmd) == (0, 0)


def test_python_replace_through_single_assignment_literals_is_counted():
    """The shape most edits actually take: bind old/new to literals,
    assert the match is unique, then write back. The literals are right
    there in the script — binding them to a name does not make them
    unknowable."""
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "p = pathlib.Path('enroll.go')\n"
           "old = 'a\\nb\\n'\n"
           "new = 'x\\n'\n"
           "s = p.read_text()\n"
           "assert s.count(old) == 1\n"
           "p.write_text(s.replace(old, new))\n"
           "PY\n")
    assert bash_churn(cmd) == (1, 2)


def test_python_replace_through_a_rebound_name_is_not_counted():
    """A name assigned more than once holds whichever value the run
    produced — not enumerable from the text."""
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "p = pathlib.Path('f')\n"
           "new = 'one'\n"
           "new = new + open('other').read()\n"
           "p.write_text(p.read_text().replace('old', new))\n"
           "PY\n")
    assert bash_churn(cmd) == (0, 0)


def test_python_replace_through_a_loop_variable_is_not_counted():
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "p = pathlib.Path('f')\n"
           "s = p.read_text()\n"
           "for new in ('a', 'b'):\n"
           "    s = s.replace('old', new)\n"
           "p.write_text(s)\n"
           "PY\n")
    assert bash_churn(cmd) == (0, 0)


def test_parsing_a_body_with_a_bad_escape_emits_no_warning():
    """Transcript scripts are arbitrary third-party text — `\\$` in a
    shell-ish literal is legal enough to run and warned about by the
    compiler. Ingest parses ~200k of them, so a leaked SyntaxWarning is
    thousands of journald lines per run, from files nobody will edit."""
    cmd = ("python3 - <<'PY'\n"
           "import pathlib\n"
           "pathlib.Path('f').write_text('cost: \\$5')\n"
           "PY\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        bash_churn(cmd)
    assert [str(w.message) for w in caught] == []
