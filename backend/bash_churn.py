"""Line churn recovered from Bash command TEXT.

`Edit` and `Write` are not where most editing happens. In a
bypass-permissions session the model is told to work through the shell,
so a file gets written by a heredoc, patched by an inline diff, or
rewritten by a python one-liner — 197k Bash calls against 41k
Edit+Write in the live corpus. None of that reached the churn panels
before this module existed.

The rule here is DIRECT ENUMERABILITY: a shape counts only when the
number of lines is readable off the call arguments themselves, with no
execution and no inference about the state of the disk. Everything else
contributes 0/0 — an honest undercount, never an estimate dressed as a
measurement. Concretely that means:

  counted    heredoc body redirected into a file (`cat > F`, `cat >> F`,
             `tee F`) — added, 0 deleted, exactly as `Write` is counted;
             inline `git apply` / `patch` diffs, by their +/- hunk lines;
             python read/replace/write bodies (heredoc or `-c`), by the
             string LITERALS passed to `.replace()` / `re.sub()`.

  not        commit messages (`git commit -F -`, `-m "$(cat <<EOF)"`),
  counted    heredocs feeding psql/jq/node/ssh, output-capture redirects
             (`cmd > out.txt` — those bytes come from running it),
             `sed -i` (the occurrence count needs the file), and any
             python replacement whose arguments are variables.
"""
from __future__ import annotations

import ast
import re
import shlex
from collections import Counter

# Commands longer than this are pathological (a base64 blob, a giant
# generated fixture); parsing them buys nothing and costs ingest time.
MAX_COMMAND_CHARS = 1_000_000

_HEREDOC_OPEN = re.compile(
    r"<<(-?)\s*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z_][A-Za-z0-9_]*))"
)

# A redirect to a path, ignoring fd duplication (`2>&1`, `>&2`).
_REDIRECT = re.compile(r"(?<![0-9<>&])>>?\s*(?:'([^']+)'|\"([^\"]+)\"|([^\s'\";&|<>()]+))")

_CAT = re.compile(r"(?:^|[|;&(]|\s)cat\b")
_TEE = re.compile(r"(?:^|[|;&(]|\s)tee\b")
_PATCH = re.compile(r"(?:^|[|;&(]|\s)(?:git\s+apply|patch)\b")
# `python3 -` / `python -` / `python3.13 -`: an interpreter reading stdin.
_PYTHON_STDIN = re.compile(r"(?:^|[|;&(]|\s)python(?:3(?:\.\d+)?)?\s+-(?:\s|$)")
_PYTHON_DASH_C = re.compile(r"(?:^|[|;&(]|\s)python(?:3(?:\.\d+)?)?\s+-c\s")

_NULL_SINKS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")

# Attribute writes that put bytes on disk. `sys.stdout.write` is excluded
# by name below — it is the one common `.write` that is not a file.
_WRITE_ATTRS = ("write_text", "write_bytes", "writelines", "write")
_STD_STREAMS = ("stdout", "stderr")


def count_lines(s: str) -> int:
    """Lines in a payload string, git-style: "a\\nb\\n" and "a\\nb" are
    both 2 lines; "" is 0. Mirrors parse._line_count."""
    if not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def _split_heredocs(command: str) -> tuple[list[tuple[str, str]], str]:
    """Split a command into (context, body) heredoc pairs plus the
    command text with every body removed.

    `context` is the opener's line minus the `<<TAG` tokens themselves,
    so a redirect written on either side of the opener is visible to the
    classifier. The leftover text is what the `python -c` scan runs on:
    a `-c` mentioned INSIDE a heredoc body is that body's business, not
    a second script.
    """
    lines = command.split("\n")
    pairs: list[tuple[str, str]] = []
    leftover: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        openers = list(_HEREDOC_OPEN.finditer(line))
        leftover.append(_HEREDOC_OPEN.sub(" ", line))
        i += 1
        if not openers:
            continue
        context = _HEREDOC_OPEN.sub(" ", line)
        for m in openers:
            dash = m.group(1) == "-"
            tag = m.group(2) or m.group(3) or m.group(4) or ""
            body: list[str] = []
            while i < len(lines):
                cur = lines[i]
                probe = cur.lstrip("\t") if dash else cur
                if probe.strip() == tag:
                    i += 1
                    break
                body.append(cur)
                i += 1
            pairs.append((context, "\n".join(body)))
    return pairs, "\n".join(leftover)


def _writes_to_a_file(context: str) -> bool:
    """True when this heredoc's body lands in a file verbatim."""
    if _TEE.search(context):
        return True
    if not _CAT.search(context):
        return False
    targets = [m.group(1) or m.group(2) or m.group(3)
               for m in _REDIRECT.finditer(context)]
    return any(t and t not in _NULL_SINKS for t in targets)


def _diff_churn(body: str) -> tuple[int, int]:
    """+/- hunk lines of a unified diff. The `+++`/`---` file headers
    name files, they do not change lines."""
    added = deleted = 0
    for line in body.split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted


def _const_str(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _const_bindings(tree: ast.AST) -> dict[str, str]:
    """Names bound EXACTLY ONCE to a string literal.

    `old = "..."` / `new = "..."` above a `replace(old, new)` is the
    shape most real edits take, and those literals are as enumerable as
    inline ones. A name bound more than once — reassigned, or a loop
    target — holds whatever the run produced, so it is excluded.
    """
    stores: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            stores[node.id] += 1
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = _const_str(node.value)
        if (isinstance(target, ast.Name) and value is not None
                and stores[target.id] == 1):
            out[target.id] = value
    return out


def _resolve_str(node: ast.expr, consts: dict[str, str]) -> str | None:
    """A string literal, directly or through a single-assignment name."""
    literal = _const_str(node)
    if literal is not None:
        return literal
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _is_std_stream_write(func: ast.Attribute) -> bool:
    """`sys.stdout.write(...)` / `sys.stderr.write(...)` — not a file."""
    value = func.value
    if isinstance(value, ast.Attribute) and value.attr in _STD_STREAMS:
        return True
    return isinstance(value, ast.Name) and value.id in _STD_STREAMS


def _opens_for_writing(node: ast.Call) -> bool:
    """`open(path, 'w')` / `open(path, mode='a')` with a literal mode."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else "")
    if name != "open":
        return False
    modes = [_const_str(a) for a in node.args[1:2]]
    modes += [_const_str(kw.value) for kw in node.keywords
              if kw.arg == "mode"]
    return any(m and any(ch in m for ch in "wax") for m in modes)


def _python_churn(src: str) -> tuple[int, int]:
    """Churn enumerable from a python script's own source.

    Two questions, in order. Does this script write a file at all? If
    not it is a reader — a `.replace()` on the way to stdout changes
    nothing on disk. If it does, every LITERAL replacement it performs
    is a countable edit; a replacement built from variables is only
    knowable by running it, so it counts 0.
    """
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return 0, 0

    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    writes = False
    for node in calls:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _WRITE_ATTRS:
            if not (func.attr == "write" and _is_std_stream_write(func)):
                writes = True
        elif _opens_for_writing(node):
            writes = True
    if not writes:
        return 0, 0

    consts = _const_bindings(tree)
    added = deleted = 0
    for node in calls:
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        is_re_sub = (func.attr == "sub" and isinstance(func.value, ast.Name)
                     and func.value.id == "re")
        if (func.attr == "replace" or is_re_sub) and len(node.args) >= 2:
            old = _resolve_str(node.args[0], consts)
            new = _resolve_str(node.args[1], consts)
            if old is not None and new is not None:
                deleted += count_lines(old)
                added += count_lines(new)
        elif func.attr in ("write_text", "write") and node.args:
            if func.attr == "write" and _is_std_stream_write(func):
                continue
            literal = _resolve_str(node.args[0], consts)
            if literal is not None:
                added += count_lines(literal)
    return added, deleted


def _dash_c_sources(text: str) -> list[str]:
    """Script bodies passed as `python3 -c '<code>'`."""
    out: list[str] = []
    for m in _PYTHON_DASH_C.finditer(text):
        try:
            tokens = shlex.split(text[m.end():], comments=False, posix=True)
        except ValueError:
            continue
        if tokens:
            out.append(tokens[0])
    return out


def bash_churn(command: str) -> tuple[int, int]:
    """(lines_added, lines_deleted) enumerable from one Bash call."""
    if not command or len(command) > MAX_COMMAND_CHARS:
        return 0, 0

    added = deleted = 0
    pairs, outside = _split_heredocs(command)
    for context, body in pairs:
        if _PATCH.search(context):
            a, d = _diff_churn(body)
        elif _PYTHON_STDIN.search(context):
            a, d = _python_churn(body)
        elif _writes_to_a_file(context):
            a, d = (count_lines(body + "\n") if body else 0), 0
        else:
            a, d = 0, 0
        added += a
        deleted += d

    for src in _dash_c_sources(outside):
        a, d = _python_churn(src)
        added += a
        deleted += d
    return added, deleted
