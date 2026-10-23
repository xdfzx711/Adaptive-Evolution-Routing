"""Parse textual unified Git diffs into path-aware changed-line multisets."""

from collections import Counter
from pathlib import PurePosixPath
import re
import shlex


def patch_changes(patch: str):
    if not isinstance(patch, str):
        raise ValueError("patch must be text")
    changes = Counter()
    files = set()
    path, remaining_old, remaining_new, saw_hunk = None, 0, 0, False
    for line in patch.splitlines():
        if line == "\\ No newline at end of file":
            continue
        if remaining_old or remaining_new:
            if not line or line[0] not in " +-":
                raise ValueError("invalid unified diff hunk")
            kind = line[0]
            remaining_old -= kind in " -"
            remaining_new -= kind in " +"
            if min(remaining_old, remaining_new) < 0:
                raise ValueError("hunk line counts do not match")
            if kind in "+-":
                # Keep indentation and literal contents; only trailing space
                # and diff position metadata are excluded from comparison.
                changes[(path, kind, line[1:].rstrip())] += 1
                files.add(path)
            continue
        if line.startswith("diff --git "):
            if path is not None and not saw_hunk:
                raise ValueError("diff contains no textual hunk")
            names = shlex.split(line[len("diff --git "):])
            if len(names) != 2 or not names[1].startswith("b/"):
                raise ValueError("unsupported diff paths")
            path = names[1][2:]
            candidate = PurePosixPath(path)
            if candidate.is_absolute() or ".." in candidate.parts or ".git" in candidate.parts:
                raise ValueError("patch path must stay in the source tree")
            saw_hunk = False
        elif line.startswith("@@ "):
            match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*", line)
            if path is None or match is None:
                raise ValueError("invalid hunk header")
            remaining_old = int(match[2]) if match[2] is not None else 1
            remaining_new = int(match[4]) if match[4] is not None else 1
            saw_hunk = True
        elif line.startswith(("index ", "--- ", "+++ ", "new file mode ", "deleted file mode ")):
            if path is None:
                raise ValueError("missing diff header")
        elif line:
            raise ValueError("unsupported non-text or malformed diff")
    if remaining_old or remaining_new or (path is not None and not saw_hunk):
        raise ValueError("incomplete unified diff")
    return changes, files
