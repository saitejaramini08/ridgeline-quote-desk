"""Ridgeline Quote Desk submission checker.

Copy this file to the root of your repository and run it as the last step of
your recorded session:

    python check_submission.py [--repo PATH]

It checks the format of your deliverables (never whether the answers are
right), prints PASS / FAIL / WARN for each check and writes
ridgeline-quote-desk.zip at the repository root, even when checks fail.
Exit code: 0 when there are no FAIL lines, 1 otherwise.

Python 3.9+, standard library only. Other tools can import run_checks(repo)
and write_zip(repo, results) to re-run the same checks.
"""

VERSION = "1.2.0"

import argparse
import io
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"

ZIP_NAME = "ridgeline-quote-desk.zip"
REPORT_NAME = "check_report.json"
SELF_NAME = "check_submission.py"

MAX_DETAILS = 10                    # detail lines printed per check
MAX_SCAN_BYTES = 5 * 1024 * 1024    # larger files are not scanned for secrets
ZIP_WARN_BYTES = 100 * 1024 * 1024

REQUIRED_PATHS = (
    ("README.md", "file"),
    (".env.example", "file"),
    (".chat-history/", "dir"),
    ("outputs/", "dir"),
    ("evaluation/results.json", "file"),
)

OUTPUT_FILES = (
    "rfq_01_email.json",
    "rfq_02_clearwater.json",
    "rfq_03_tricounty.json",
    "rfq_04_handwritten.json",
    "rfq_05_forwarded_chain.json",
    "rfq_06_messy.json",
)

README_HEADINGS = (
    (1, "Ridgeline Quote Desk"),
    (2, "Repository"),
    (2, "How to Run"),
    (2, "Approach"),
    (2, "Evaluation"),
    (2, "Production"),
)

TOP_LEVEL_KEYS = ("rfq_id", "customer_id", "lines", "customer_name", "non_item_rows")
LINE_REQUIRED = (
    "line", "as_written", "quantity", "unit", "ridgeline_part_number",
    "match_method", "confidence", "needs_review", "flags",
)
LINE_OPTIONAL = ("note",)
LINE_KNOWN = LINE_REQUIRED + LINE_OPTIONAL + ("expected_part_number",)  # the last one has its own message
MATCH_METHODS = ("ridgeline_part_number", "cross_reference", "description", "llm")
STUB_FLAG = "stub"
STUB_ALLOWED = "rfq_04_handwritten"  # the photo
STUB_REJECTED = ("rfq_01_email", "rfq_02_clearwater", "rfq_03_tricounty")
RESULT_NUMBERS = ("threshold", "extraction_accuracy", "match_precision", "match_recall")

# Directories left out of the zip by name, at any depth. "env" is handled
# separately: it is only skipped when it is a virtualenv (has pyvenv.cfg).
EXCLUDED_DIRS = frozenset({
    "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ipynb_checkpoints",
})

# Root paths that never make the working tree "dirty", whatever their status.
# (Untracked files the zip would leave out are skipped in _check_clean_tree.)
IGNORED_STATUS_PATHS = frozenset({ZIP_NAME, SELF_NAME, REPORT_NAME})

# Status codes of a .chat-history/ file that was only modified or added since the
# last commit, staged or not. AA (a merge conflict) and T (a type change) still fail.
LOG_UPDATE_CODES = frozenset({" M", "M ", "MM", "A ", "AM", " A"})


@dataclass
class CheckResult:
    name: str
    status: str
    messages: list = field(default_factory=list)

    def to_dict(self):
        return {"name": self.name, "status": self.status, "messages": list(self.messages)}


def _results(name, fails=(), warns=()):
    """One FAIL result and/or one WARN result for `name`, or a single PASS."""
    out = []
    if fails:
        out.append(CheckResult(name, FAIL, list(fails)))
    if warns:
        out.append(CheckResult(name, WARN, list(warns)))
    return out or [CheckResult(name, PASS)]


def _guard(name, fn):
    """Run one check; a bug inside it becomes a FAIL line instead of a crash."""
    try:
        return list(fn())
    except Exception as exc:  # keep the other checks and the zip going
        return [CheckResult(name, FAIL, [f"checker error: {type(exc).__name__}: {exc}"])]


def _short(text, limit=40):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------

_REPO_ENV_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX",
)
# A submission's own .git/config must never be able to run a program on the
# machine that checks it. An empty core.fsmonitor disables the fsmonitor hook on
# every git version (`false` would be read as a command name before git 2.37).
_GIT_SAFE_ARGS = ("-c", "core.fsmonitor=", "-c", "log.showSignature=false")


def _git_env():
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV_VARS}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _run_git(repo, *args, config=(), timeout=300):
    """Run git in `repo`; None when git is missing or could not be run."""
    exe = shutil.which("git")
    if exe is None:
        return None
    try:
        return subprocess.run(
            [exe, *_GIT_SAFE_ARGS, *config, *args], cwd=str(repo), env=_git_env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _filter_overrides(repo):
    """-c options that blank every filter driver configured for the repository.

    `git status` runs a driver's clean filter when it has to re-hash a file,
    so a hostile .git/config could otherwise execute a command here.
    """
    proc = _run_git(repo, "config", "--name-only", "--get-regexp", r"^filter\..+\.(clean|smudge|process)$")
    if proc is None or proc.returncode != 0:
        return []
    drivers = sorted({key.rsplit(".", 1)[0] for key in _text(proc.stdout).splitlines() if key})
    args = []
    for driver in drivers:
        for variable, value in (("clean", ""), ("smudge", ""), ("process", ""), ("required", "false")):
            args += ["-c", f"{driver}.{variable}={value}"]
    return args


def _text(data):
    return data.decode("utf-8", errors="replace").strip()


def _first_line(data):
    lines = _text(data).splitlines()
    return _short(lines[0], 120) if lines else "no details"


# --------------------------------------------------------------------------
# The file set: what goes into the zip, and what the scans look at
# --------------------------------------------------------------------------

@dataclass
class Entry:
    path: str       # absolute path on disk
    arcname: str    # forward-slash path inside the zip
    is_dir: bool
    size: int


@dataclass
class Entries:
    mode: str       # "git" (tracked files + .git/) or "walk" (filesystem fallback)
    items: list
    files: set      # arcnames of the file entries


def _is_excluded_file(name, at_root):
    low = name.lower()
    if low == ".ds_store":
        return True
    if low == ".env" or (low.startswith(".env.") and low != ".env.example"):
        return True
    return at_root and low.endswith(".zip")


def _is_excluded_dir(name, path):
    if name in EXCLUDED_DIRS:
        return True
    return name == "env" and os.path.isfile(os.path.join(path, "pyvenv.cfg"))


def _visit(items, abs_dir, rel_dir, exclusions):
    """Append the entries under `abs_dir` (sorted; symlinks are never followed)."""
    try:
        with os.scandir(abs_dir) as it:
            children = sorted(it, key=lambda c: c.name)
    except OSError:
        return
    for child in children:
        rel = rel_dir + "/" + child.name if rel_dir else child.name
        try:
            if child.is_symlink():
                continue
            if child.is_dir(follow_symlinks=False):
                # the exclusion rules never apply inside .git/
                rules = exclusions and not (not rel_dir and child.name == ".git")
                if rules and _is_excluded_dir(child.name, child.path):
                    continue
                items.append(Entry(child.path, rel, True, 0))
                _visit(items, child.path, rel, rules)
            elif child.is_file(follow_symlinks=False):
                if exclusions and _is_excluded_file(child.name, not rel_dir):
                    continue
                items.append(Entry(child.path, rel, False, child.stat(follow_symlinks=False).st_size))
        except OSError:
            continue


def _tracked_paths(repo):
    """Tracked files from `git ls-files`, or None when git can't tell us."""
    if not (repo / ".git").is_dir():
        return None
    proc = _run_git(repo, "ls-files", "-z")
    if proc is None or proc.returncode != 0:
        return None
    return sorted({os.fsdecode(p) for p in proc.stdout.split(b"\0") if p})


def _under_excluded_dir(repo, parts, cache):
    """True when a directory above the last component of `parts` is left out of the zip."""
    for i, part in enumerate(parts[:-1]):
        if part in EXCLUDED_DIRS:
            return True
        if part == "env":
            key = "/".join(parts[: i + 1])
            if key not in cache:
                cache[key] = os.path.isfile(os.path.join(str(repo), *parts[: i + 1], "pyvenv.cfg"))
            if cache[key]:
                return True
    return False


def collect_entries(repo):
    """The files that go into the zip. Zip, chat-history check and secret scan share this list.

    With git available the zip matches the repository: tracked files plus the
    .git/ directory. Without git it falls back to walking the working tree.
    """
    repo = Path(repo)
    tracked = _tracked_paths(repo)
    items = []
    if tracked is None:
        mode = "walk"
        _visit(items, str(repo), "", True)
    else:
        mode = "git"
        cache = {}
        for rel in tracked:
            parts = rel.split("/")
            if _under_excluded_dir(repo, parts, cache) or _is_excluded_file(parts[-1], len(parts) == 1):
                continue
            full = os.path.join(str(repo), *parts)
            try:
                st = os.lstat(full)
            except OSError:
                continue  # tracked but deleted from disk
            if stat.S_ISREG(st.st_mode):  # skips symlinks and submodule directories
                items.append(Entry(full, rel, False, st.st_size))
        git_dir = repo / ".git"
        items.append(Entry(str(git_dir), ".git", True, 0))
        _visit(items, str(git_dir), ".git", False)
    items.sort(key=lambda e: e.arcname)
    return Entries(mode, items, {e.arcname for e in items if not e.is_dir})


def _locate(repo, entries, rel, kind):
    """Find `rel` (forward slashes) with exact letter case, as a Linux grader would.

    Returns (absolute path, None) or (None, reason). A file must also be part of
    the zip, otherwise the checks would pass for something the grader never sees.
    """
    cur = str(repo)
    parts = rel.split("/")
    for i, part in enumerate(parts):
        try:
            names = os.listdir(cur)
        except OSError:
            return None, "not found"
        if part in names:
            cur = os.path.join(cur, part)
            continue
        alt = [n for n in names if n.lower() == part.lower()]
        if alt:
            shown = "/".join(parts[:i] + [alt[0]])
            return None, f'found only as "{shown}" (names are case-sensitive)'
        return None, "not found"
    if kind == "dir":
        return (cur, None) if os.path.isdir(cur) else (None, "not a directory")
    if not os.path.isfile(cur):
        return None, "not a file"
    if rel not in entries.files:
        if entries.mode == "git":
            return None, "not tracked by git, so it is not in the zip"
        return None, "is not included in the zip (symlinks are skipped)"
    return cur, None


# --------------------------------------------------------------------------
# Check 1: repository
# --------------------------------------------------------------------------

def check_git(repo):
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return [CheckResult("Git repository", FAIL,
                            [".git/ not found" if not git_dir.exists() else ".git is not a directory"])]
    results = [CheckResult("Git repository", PASS)]
    if shutil.which("git") is None:
        results.append(CheckResult("Git not installed", WARN,
                                   ["the working tree, upstream and history checks were skipped"]))
        return results
    results.extend(_check_clean_tree(repo))
    results.extend(_check_unpushed(repo))
    return results


def _check_clean_tree(repo):
    name = "Clean working tree"
    proc = _run_git(repo, "status", "--porcelain", "-z", "--untracked-files=all",
                    config=_filter_overrides(repo))
    if proc is None or proc.returncode != 0:
        why = _first_line(proc.stderr) if proc is not None else "could not run git"
        return [CheckResult(name, FAIL, [f"git status failed: {why}"])]
    tokens = proc.stdout.split(b"\0")
    dirty, warns, cache = [], [], {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if len(token) < 4:
            continue
        xy = token[:2].decode("ascii", errors="replace")
        path = os.fsdecode(token[3:])
        if "R" in xy or "C" in xy:
            i += 1  # renames and copies are followed by the original path
        if path in IGNORED_STATUS_PATHS:
            continue
        if path.startswith(".chat-history/") and xy in LOG_UPDATE_CODES \
                and _zip_packs(repo, path.split("/"), cache):
            # The assistant logs the "commit and push" exchange too, so the log always
            # changes after the last commit; the zip packs the file as it is on disk.
            warns.append(f"{path}: changed after your last commit; the zip includes the current version")
            continue
        if xy == "??":
            # Untracked files the zip leaves out anyway don't dirty the tree: a FAIL
            # would push people towards `git add -A`, which commits a .env file.
            parts = path.split("/")
            in_excluded_dir = _under_excluded_dir(repo, parts, cache)
            if in_excluded_dir or _is_excluded_file(parts[-1], len(parts) == 1):
                if not in_excluded_dir and parts[-1].lower() == ".env":
                    warns.append(f"{path} is not in .gitignore: add it there and never commit it")
                continue
        dirty.append(f"{xy.strip()} {path}")
    return _results(name, dirty, warns)


def _zip_packs(repo, parts, cache):
    """True when collect_entries would zip this tracked path from disk (a regular file, not excluded)."""
    if _under_excluded_dir(repo, parts, cache) or _is_excluded_file(parts[-1], len(parts) == 1):
        return False
    try:
        return stat.S_ISREG(os.lstat(os.path.join(str(repo), *parts)).st_mode)
    except OSError:
        return False


def _check_unpushed(repo):
    name = "Unpushed commits on current branch"
    head = _run_git(repo, "rev-parse", "--verify", "-q", "HEAD")
    if head is None or head.returncode != 0:
        return [CheckResult(name, WARN, ["no commits yet"])]
    branch = _run_git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    branch_name = _text(branch.stdout) if branch is not None and branch.returncode == 0 else ""
    upstream = _run_git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if upstream is None or upstream.returncode != 0:
        why = f'no upstream configured for branch "{branch_name}"' if branch_name \
            else "HEAD is detached, so there is no upstream to compare with"
        return [CheckResult(name, WARN, [why])]
    count = _run_git(repo, "rev-list", "--count", "@{upstream}..HEAD")
    try:
        ahead = int(_text(count.stdout))
    except (AttributeError, ValueError):
        return [CheckResult(name, WARN, ["could not compare with the upstream branch"])]
    if ahead:
        plural = "" if ahead == 1 else "s"
        return [CheckResult(name, WARN, [f"{ahead} commit{plural} not pushed to {_text(upstream.stdout)}"])]
    return [CheckResult(name, PASS)]


# --------------------------------------------------------------------------
# Checks 2 and 3: required paths, chat history
# --------------------------------------------------------------------------

def check_required_paths(repo, entries):
    fails = []
    for display, kind in REQUIRED_PATHS:
        located, problem = _locate(repo, entries, display.rstrip("/"), kind)
        if located is None:
            fails.append(f"{display}: {problem}")
    return _results("Required paths", fails)


def check_chat_history(repo, entries):
    located, _ = _locate(repo, entries, ".chat-history", "dir")
    if located is None:
        return []  # already reported by "Required paths"
    if any(not e.is_dir and e.size > 0 and e.arcname.startswith(".chat-history/") for e in entries.items):
        return [CheckResult("Chat history", PASS)]
    kind = "tracked file" if entries.mode == "git" else "file"
    return [CheckResult("Chat history", FAIL, [f"no non-empty {kind} in .chat-history/"])]


# --------------------------------------------------------------------------
# Checks 4 and 5: JSON deliverables
# --------------------------------------------------------------------------

def _reject_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def _load_json(path, rel):
    """Return (value, fails, warns). A leading UTF-8 BOM is tolerated with a WARN."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        return None, [f"cannot read file: {exc.strerror or exc}"], []
    warns = []
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        warns.append(f"{rel}: starts with a UTF-8 BOM")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, ["not valid UTF-8"], warns
    try:
        return json.loads(text, parse_constant=_reject_constant), [], warns
    except (ValueError, RecursionError) as exc:
        return None, [f"invalid JSON: {exc}"], warns


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    if isinstance(v, bool):
        return False
    return isinstance(v, int) or (isinstance(v, float) and math.isfinite(v))


def _is_str_or_null(v):
    return v is None or isinstance(v, str)


def _is_str_list(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _validate_lines(lines, fails, warns):
    sequence_broken = False
    for i, item in enumerate(lines):
        where = f"lines[{i}]"
        if not isinstance(item, dict):
            fails.append(f"{where}: must be an object")
            continue
        for key in LINE_REQUIRED:
            if key not in item:
                fails.append(f'{where}: missing field "{key}"')
        if "expected_part_number" in item:
            fails.append(f"{where}: use ridgeline_part_number instead of expected_part_number")
        if "line" in item:
            if not _is_int(item["line"]):
                fails.append(f"{where}.line: must be an integer")
            elif item["line"] != i + 1 and not sequence_broken:
                sequence_broken = True  # later lines would only repeat the same problem
                fails.append(f"{where}.line: value {_short(item['line'])} breaks the 1, 2, 3... sequence")
        if "as_written" in item and not (isinstance(item["as_written"], str) and item["as_written"]):
            fails.append(f"{where}.as_written: must be a non-empty string")
        if "quantity" in item and not (item["quantity"] is None or _is_num(item["quantity"])):
            fails.append(f"{where}.quantity: must be a number or null")
        for key in ("unit", "ridgeline_part_number"):
            if key in item and not _is_str_or_null(item[key]):
                fails.append(f"{where}.{key}: must be a string or null")
        if "match_method" in item:
            method = item["match_method"]
            if method is not None and not (isinstance(method, str) and method in MATCH_METHODS):
                fails.append(f"{where}.match_method: must be {', '.join(MATCH_METHODS)} or null")
        if "confidence" in item:
            conf = item["confidence"]
            if not (conf is None or _is_num(conf)):
                fails.append(f"{where}.confidence: must be a number or null")
            elif conf is not None and not 0 <= conf <= 1:
                fails.append(f"{where}.confidence: value {_short(conf)} is outside the range 0 to 1")
        if "needs_review" in item and not isinstance(item["needs_review"], bool):
            fails.append(f"{where}.needs_review: must be true or false")
        if "flags" in item and not _is_str_list(item["flags"]):
            fails.append(f"{where}.flags: must be an array of strings")
        if "note" in item and not _is_str_or_null(item["note"]):
            fails.append(f"{where}.note: must be a string or null")
        for key in item:
            if key not in LINE_KNOWN:
                warns.append(f'{where}: unexpected key "{_short(key)}"')


def _validate_stub(stem, lines, fails, warns):
    """The shape of a stubbed RFQ, and which RFQs may be stubbed. Never judges the content."""
    stub_at = []
    for i, item in enumerate(lines):
        flags = item.get("flags") if isinstance(item, dict) else None
        flags = [f for f in flags if isinstance(f, str)] if isinstance(flags, list) else []
        if STUB_FLAG in flags:
            stub_at.append(i)
        if any(f != STUB_FLAG and f.strip().lower().startswith(STUB_FLAG) for f in flags):
            warns.append(f'lines[{i}].flags: use the flag "{STUB_FLAG}" exactly')
    if not stub_at:
        return
    if stem in STUB_REJECTED:
        fails.append("this RFQ can't be stubbed: rfq_01 to rfq_03 must be fully extracted")
    elif stem != STUB_ALLOWED:
        warns.append(f"only {STUB_ALLOWED} may be stubbed; run your pipeline on this RFQ as it is")
    if len(lines) != 1:
        fails.append("a stubbed RFQ has a single line")
    for i in stub_at:
        if lines[i].get("needs_review") is False:  # missing or non-boolean: reported by _validate_lines
            fails.append(f"lines[{i}].needs_review: must be true on a stub")
        note = lines[i].get("note")
        if not (isinstance(note, str) and note.strip()):
            fails.append(f"lines[{i}].note: a stub needs the reason in note")


def _validate_output(data, stem):
    fails, warns = [], []
    if not isinstance(data, dict):
        return ["top-level value must be a JSON object"], warns
    for key in ("rfq_id", "customer_id", "lines"):
        if key not in data:
            fails.append(f'missing field "{key}"')
    if "rfq_id" in data:
        if not isinstance(data["rfq_id"], str):
            fails.append("rfq_id: must be a string")
        elif data["rfq_id"] != stem:
            fails.append("rfq_id: does not match the file name")
    if "customer_id" in data and not _is_str_or_null(data["customer_id"]):
        fails.append("customer_id: must be a string or null")
    if "customer_name" in data and not _is_str_or_null(data["customer_name"]):
        fails.append("customer_name: must be a string or null")
    if "non_item_rows" in data and not _is_str_list(data["non_item_rows"]):
        fails.append("non_item_rows: must be an array of strings")
    if "lines" in data:
        lines = data["lines"]
        if not isinstance(lines, list) or not lines:
            fails.append("lines: must be a non-empty array")
        else:
            _validate_lines(lines, fails, warns)
            _validate_stub(stem, lines, fails, warns)
    for key in data:
        if key not in TOP_LEVEL_KEYS:
            warns.append(f'unexpected top-level key "{_short(key)}"')
    return fails, warns


def check_outputs(repo, entries):
    results = []
    for fname in OUTPUT_FILES:
        rel = f"outputs/{fname}"
        located, problem = _locate(repo, entries, rel, "file")
        if located is None:
            results.append(CheckResult(rel, FAIL, [problem]))
            continue
        data, fails, warns = _load_json(located, rel)
        if not fails:
            more_fails, more_warns = _validate_output(data, fname[: -len(".json")])
            fails, warns = fails + more_fails, warns + more_warns
        results.extend(_results(rel, fails, warns))
    extras = sorted(
        e for e in entries.files
        if e.startswith("outputs/") and "/" not in e[len("outputs/"):]
        and e.lower().endswith(".json") and e[len("outputs/"):] not in OUTPUT_FILES
    )
    if extras:
        results.append(CheckResult("Extra .json files in outputs/", WARN, extras))
    return results


def check_results_json(repo, entries):
    rel = "evaluation/results.json"
    located, _ = _locate(repo, entries, rel, "file")
    if located is None:
        return []  # already reported by "Required paths"
    data, fails, warns = _load_json(located, rel)
    if not fails:
        if not isinstance(data, dict):
            fails.append("top-level value must be a JSON object")
        else:
            for key in RESULT_NUMBERS:
                if key not in data:
                    fails.append(f'missing field "{key}"')
                elif not _is_num(data[key]):
                    fails.append(f"{key}: must be a number")
                elif not 0 <= data[key] <= 1:
                    fails.append(f"{key}: value {_short(data[key])} is outside the range 0 to 1")
            if "additional_metrics" not in data:
                fails.append('missing field "additional_metrics"')
            elif not isinstance(data["additional_metrics"], dict):
                fails.append("additional_metrics: must be an object")
    return _results(rel, fails, warns)


# --------------------------------------------------------------------------
# Check 6: README.md
# --------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*(.*?)[ \t]*$")
_GITHUB_URL_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+", re.IGNORECASE)
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")


def _parse_markdown(lines):
    """ATX headings and mermaid fences outside/inside fenced code blocks.

    Returns (headings, mermaid_lines): headings are (line number, level, text)
    for lines that are not inside a fence; mermaid_lines are the line numbers
    of fences whose info string is `mermaid`.
    """
    headings, mermaid_lines = [], []
    fence = None  # (marker character, marker length) while inside a fenced block
    for number, line in enumerate(lines, 1):
        m = _FENCE_RE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1] and not m.group(2):
                fence = None
            continue
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)))
            words = m.group(2).split()
            if words and words[0].lower() == "mermaid":
                mermaid_lines.append(number)
            continue
        h = _HEADING_RE.match(line)
        if h:
            headings.append((number, len(h.group(1)), (h.group(2) or "").strip()))
    return headings, mermaid_lines


def _check_readme_text(text):
    lines = _LINE_BREAK_RE.split(text)
    headings, mermaid_lines = _parse_markdown(lines)
    required = {key: [] for key in README_HEADINGS}
    boundaries = [number for number, level, _ in headings if level <= 2]
    fails = []
    for number, level, title in headings:
        if (level, title) in required:
            required[(level, title)].append(number)
        elif level <= 2:
            fails.append(f'line {number}: unexpected heading "{"#" * level} {_short(title, 50)}"')
    for level, title in README_HEADINGS:
        found = required[(level, title)]
        label = f'"{"#" * level} {title}"'
        if not found:
            fails.append(f"missing heading {label}")
        elif len(found) > 1:
            shown = ", ".join(str(n) for n in found[:5])
            fails.append(f"heading {label} appears {len(found)} times (lines {shown})")
    # Order: keep the longest run of headings that is already in order and report
    # the others, so one misplaced heading is named instead of everything after it.
    order = [position for _, position in sorted(
        (found[0], position) for position, key in enumerate(README_HEADINGS)
        for found in [required[key]] if found)]
    best, previous = [1] * len(order), [-1] * len(order)
    for i in range(len(order)):
        for j in range(i):
            if order[j] < order[i] and best[j] + 1 > best[i]:
                best[i], previous[i] = best[j] + 1, j
    in_order = set()
    i = max(range(len(order)), key=lambda k: best[k]) if order else -1
    while i != -1:
        in_order.add(i)
        i = previous[i]
    for i, position in enumerate(order):
        if i not in in_order:
            level, title = README_HEADINGS[position]
            fails.append(f'heading "{"#" * level} {title}" is out of order')
    for (level, title), found in required.items():
        if level == 1 or not found:
            continue  # the title needs no text of its own
        start = found[0]
        end = next((b for b in boundaries if b > start), len(lines) + 1)
        body = lines[start: end - 1]
        label = f'"## {title}"'
        if not any(line.strip() for line in body):
            fails.append(f"section {label} is empty")
        elif title == "Repository" and not _GITHUB_URL_RE.search("\n".join(body)):
            fails.append(f"section {label}: no https://github.com/ URL")
        elif title == "Production" and not any(start < n < end for n in mermaid_lines):
            fails.append(f"section {label}: no fenced code block with the info string mermaid")
    return fails


def check_readme(repo, entries):
    located, _ = _locate(repo, entries, "README.md", "file")
    if located is None:
        return []  # already reported by "Required paths"
    try:
        with open(located, "rb") as fh:
            text = fh.read().decode("utf-8-sig", errors="replace")
    except OSError as exc:
        return [CheckResult("README.md", FAIL, [f"cannot read file: {exc.strerror or exc}"])]
    return _results("README.md", _check_readme_text(text))


# --------------------------------------------------------------------------
# Check 7: secrets
# --------------------------------------------------------------------------

_LEFT = r"(?<![A-Za-z0-9_-])"
_PREFIXED_PATTERNS = (
    ("sk- token", re.compile(_LEFT + r"sk-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(_LEFT + r"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(_LEFT + r"gh[po]_[A-Za-z0-9]{20,}")),
    ("GitHub token", re.compile(_LEFT + r"github_pat_[A-Za-z0-9_]{20,}")),
    ("Google API key", re.compile(_LEFT + r"AIza[0-9A-Za-z_-]{20,}")),
    ("Slack token", re.compile(_LEFT + r"xox[abprs]-[A-Za-z0-9-]{10,}")),
    ("private key", re.compile(r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----")),
)
# NAME=value / NAME: "value" where NAME ends in api_key, secret or token and the
# value is one unbroken run of 16+ characters (so function calls, attribute
# lookups and plural names like input_tokens are not matched).
_ASSIGNMENT_RE = re.compile(
    r"(?:api[_-]?key|secret(?:[_-]?access)?(?:[_-]?key)?|token)"
    r"""(?:\\?["'])?[ \t]*[:=](?!=)[ \t]*(?:\\?["'])?"""
    r"(?P<value>[A-Za-z0-9_+/=-]{16,})",
    re.IGNORECASE,
)
_ENV_LINE_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_.-]*[ \t]*=(?P<value>.*)$")


def _mask(value):
    return value[:4] + "***"


def scan_line(line, assignments=True):
    """Return [(label, masked value)] for one line, one entry per distinct value."""
    found = {}
    for label, pattern in _PREFIXED_PATTERNS:
        for m in pattern.finditer(line):
            found.setdefault(_mask(m.group(0)), label)
    if assignments:
        for m in _ASSIGNMENT_RE.finditer(line):
            found.setdefault(_mask(m.group("value")), "key assignment")
    return [(label, masked) for masked, label in found.items()]


def _env_line_has_value(line):
    m = _ENV_LINE_RE.match(line)
    if not m:
        return False
    raw = m.group("value").strip()
    if raw[:1] in ("'", '"'):
        end = raw.find(raw[0], 1)
        value = raw[1:end] if end != -1 else raw[1:]
    elif raw.startswith("#"):
        value = ""
    else:
        value = re.split(r"\s#", raw, maxsplit=1)[0]
    return bool(value.strip())


def _is_env_example(path):
    return bool(path) and path.rsplit("/", 1)[-1] == ".env.example"


def _read_text_file(path):
    """File content as text, or None for unreadable files and binaries."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_SCAN_BYTES + 1)
    except OSError:
        return None
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # e.g. written by PowerShell's ">"
        return data.decode("utf-16", errors="replace")
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _scan_entries(entries):
    fails, warns = [], []
    for e in entries.items:
        if e.is_dir or e.size == 0 or e.size > MAX_SCAN_BYTES or e.arcname.startswith(".git/objects/"):
            continue
        text = _read_text_file(e.path)
        if text is None:
            continue
        env_example = _is_env_example(e.arcname)
        for number, line in enumerate(text.split("\n"), 1):
            hits = scan_line(line, assignments=not env_example)
            for label, masked in hits:
                fails.append(f"{e.arcname}:{number}: {label} {masked}")
            if env_example and not hits and _env_line_has_value(line):
                warns.append(f"{e.arcname}:{number}: has a value; list variable names only")
    return fails, warns


_COMMIT_RE = re.compile(r"^commit ([0-9a-f]{40,64})(?: .*)?$")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _scan_history(repo):
    """Scan `git log -p --all`: added lines and commit messages, streamed."""
    exe = shutil.which("git")
    if exe is None or not (repo / ".git").is_dir():
        return []
    cmd = [exe, *_GIT_SAFE_ARGS, "-c", "core.quotePath=false", "log", "-p", "--all",
           "--no-color", "--no-decorate", "--no-ext-diff", "--no-textconv", "-U0",
           "--src-prefix=a/", "--dst-prefix=b/"]
    try:
        proc = subprocess.Popen(cmd, cwd=str(repo), env=_git_env(), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return []
    found, seen = [], set()

    def emit(where, where_key, text, assignments=True):
        for label, masked in scan_line(text, assignments=assignments):
            key = (label, masked, where_key)  # the same value in the same file is reported once
            if key not in seen:
                seen.add(key)
                found.append(f"{where}: {label} {masked}")

    commit, path, zone, in_hunk, new_no = "", None, "head", False, 0
    stream = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace", newline="\n")
    try:
        for raw in stream:
            line = raw.rstrip("\r\n")
            m = _COMMIT_RE.match(line)
            if m:
                commit, zone, path, in_hunk = m.group(1)[:8], "head", None, False
                continue
            if line.startswith("diff --git "):
                zone, path, in_hunk = "diff", None, False
                continue
            if zone == "head":
                emit(f"git history: commit {commit} (commit message)", "(commit message)", line)
            elif line.startswith("@@"):
                hunk = _HUNK_RE.match(line)
                in_hunk, new_no = True, int(hunk.group(1)) if hunk else 0
            elif not in_hunk:
                if line.startswith("+++ "):
                    target = line[4:].rstrip("\t")
                    path = target[2:] if target.startswith("b/") else None
            elif line.startswith("+"):
                where = f"git history: commit {commit} {path or '?'}:{new_no}"
                emit(where, path, line[1:], assignments=not _is_env_example(path))
                new_no += 1
            elif not line.startswith(("-", "\\")):
                new_no += 1
    finally:
        stream.close()  # also closes proc.stdout
        proc.wait()
    return found


SECRETS_GUIDANCE = (
    "Remove every secret listed below before you push, including from your local git history, "
    "and revoke the key. A secret in the submitted repository or its git history disqualifies "
    "the submission."
)


def check_secrets(repo, entries):
    fails, warns = _scan_entries(entries)
    fails.extend(_scan_history(repo))
    if fails:
        fails.insert(0, SECRETS_GUIDANCE)  # first, so the detail cap never hides it
    return _results("Secrets", fails, warns)


# --------------------------------------------------------------------------
# Running the checks, writing the zip
# --------------------------------------------------------------------------

def run_checks(repo):
    """Run every check on `repo` and return the results in report order."""
    repo = Path(repo)
    results = []
    try:
        entries = collect_entries(repo)
    except Exception as exc:  # keep checking with an empty file list
        entries = Entries("walk", [], set())
        results.append(CheckResult("Repository files", FAIL, [f"checker error: {type(exc).__name__}: {exc}"]))
    steps = (
        ("Git repository", lambda: check_git(repo)),
        ("Required paths", lambda: check_required_paths(repo, entries)),
        ("Chat history", lambda: check_chat_history(repo, entries)),
        ("outputs/", lambda: check_outputs(repo, entries)),
        ("evaluation/results.json", lambda: check_results_json(repo, entries)),
        ("README.md", lambda: check_readme(repo, entries)),
        ("Secrets", lambda: check_secrets(repo, entries)),
    )
    for name, fn in steps:
        results.extend(_guard(name, fn))
    return results


def _git_head(repo):
    if not (repo / ".git").is_dir():
        return None
    proc = _run_git(repo, "rev-parse", "HEAD")
    if proc is None or proc.returncode != 0:
        return None
    return _text(proc.stdout) or None


def write_zip(repo, results):
    """Write ridgeline-quote-desk.zip at the repo root and return its path."""
    repo = Path(repo)
    entries = collect_entries(repo)
    report = {
        "script_version": VERSION,
        "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_head": _git_head(repo),
        "zip_source": entries.mode,
        "checks": [r.to_dict() for r in results],
    }
    target = repo / ZIP_NAME
    partial = repo / (ZIP_NAME + ".tmp")
    try:
        with zipfile.ZipFile(str(partial), "w", zipfile.ZIP_DEFLATED, strict_timestamps=False) as zf:
            for e in entries.items:
                if e.arcname == REPORT_NAME and not e.is_dir:
                    continue  # ours replaces any file of that name
                zf.write(e.path, e.arcname + "/" if e.is_dir else e.arcname)
            zf.writestr(REPORT_NAME, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        os.replace(str(partial), str(target))
    except BaseException:
        try:
            os.remove(str(partial))
        except OSError:
            pass
        raise
    return target


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _format_size(size):
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{max(1, round(size / 1024))} KB"


def _format_result(result):
    lines = [f"{result.status}  {result.name}"]
    for message in result.messages[:MAX_DETAILS]:
        lines.append("        " + " ".join(str(message).split()))
    if len(result.messages) > MAX_DETAILS:
        lines.append(f"        ... and {len(result.messages) - MAX_DETAILS} more")
    return lines


def _make_output_safe():
    """Never crash on a file name the console can't encode."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=SELF_NAME,
        description="Check the format of a Ridgeline Quote Desk submission and write "
                    + ZIP_NAME + " at the repository root.",
    )
    parser.add_argument("--repo", default=".", metavar="PATH",
                        help="repository to check (default: current directory)")
    args = parser.parse_args(argv)
    _make_output_safe()

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"error: {args.repo} is not a directory")
        return 1

    results = run_checks(repo)
    zip_size = None
    try:
        zip_size = write_zip(repo, results).stat().st_size
    except Exception as exc:
        results.append(CheckResult("Zip", FAIL, [f"could not write {ZIP_NAME}: {exc}"]))
    if zip_size is not None and zip_size > ZIP_WARN_BYTES:
        results.append(CheckResult("Zip size", WARN, [f"{ZIP_NAME} is {_format_size(zip_size)}"]))

    for result in results:
        print("\n".join(_format_result(result)))
    fails = sum(r.status == FAIL for r in results)
    warns = sum(r.status == WARN for r in results)
    print()
    print(f"Result: {fails} FAIL, {warns} WARN. "
          + ("Fix every FAIL and run again." if fails else "Format checks passed."))
    if zip_size is not None:
        print(f"Zip written: {ZIP_NAME} ({_format_size(zip_size)})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
