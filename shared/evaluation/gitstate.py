"""Git facts the experiment workflow needs: is the code clean, and is it the captured code.

Read-only `git` calls; nothing here writes to the repository. Every function fails closed: if Git
cannot answer, the code is reported dirty or changed, never clean.

Experiment data (the capture folder and the repository's `captures/` directory, where the manual
procedure keeps the saved fault evidence and where capture folders are committed) is not code, so
it is ignored. The code's identity is bound by the commit and the registered hashes. Every other
change, including untracked source files, counts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

DATA_DIR = "captures"


def _git_output(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def git_state(exclude: Path | None) -> tuple[str, bool]:
    """(HEAD commit, working tree dirty), ignoring experiment data.

    Ignored: changes under `exclude` (the capture folder) and under `captures/`. Fails closed: if
    Git cannot answer, the commit is "unavailable" and the tree counts as dirty.
    """
    try:
        root = Path(_git_output("rev-parse", "--show-toplevel").strip()).resolve()
        commit = _git_output("rev-parse", "HEAD").strip()
        status = _git_output("status", "--porcelain", "--untracked-files=all")
    except (OSError, subprocess.CalledProcessError):
        return "unavailable", True
    skip = [(root / DATA_DIR).resolve()]
    if exclude is not None:
        skip.append(exclude.resolve())
    for line in status.splitlines():
        path = (root / line[3:].strip().strip('"').split(" -> ")[-1]).resolve()
        if any(path.is_relative_to(folder) for folder in skip):
            continue
        return commit, True
    return commit, False


def code_unchanged_since(commit: str) -> bool:
    """True if HEAD is `commit`, or a descendant of it whose only changes are under `captures/`.

    This is what keeps a frozen capture usable after its folder is committed: the commit moves
    HEAD, but no code changed. A source change in any later commit makes this False.
    """
    if not commit or commit == "unavailable":
        return False
    try:
        if _git_output("rev-parse", "HEAD").strip() == commit:
            return True
        _git_output("merge-base", "--is-ancestor", commit, "HEAD")
        changed = _git_output("diff", "--name-only", commit, "HEAD").splitlines()
    except (OSError, subprocess.CalledProcessError):
        return False
    prefix = DATA_DIR + "/"
    return all(
        p.strip().strip('"').replace("\\", "/").startswith(prefix) for p in changed if p.strip()
    )
