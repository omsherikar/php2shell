"""Read-only repository tools exposed to the auditing agent.

These three tools are the ONLY way the agent touches the target. They enforce
the harness guardrails (see README):

* **Read-only.** list_dir / read_file / grep. No write, no shell, no code
  execution, no network. The agent can read source; it cannot run or modify it.
* **Rooted + no future-leak.** Every path is resolved under a single target root
  and any path containing ``.git`` is refused, so the model cannot read the fix
  commit, the fixed NEWS entry, or branch history.

The path guard is the security-critical part; it is exercised by tests/test_tools.py.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


class PathGuardError(Exception):
    """Raised when a requested path violates the sandbox guard."""


# Any path whose text (requested or resolved) contains this substring is refused.
# Blocking the substring ".git" also blocks ".github", ".gitignore" and
# ".gitattributes"; that is intentional — the agent audits source, not repo
# metadata, and the wider block removes every avenue to the fix history.
_FORBIDDEN = ".git"

# read_file hard cap: force the model to navigate instead of dumping whole files.
MAX_READ_LINES = 400

# grep caps.
DEFAULT_GREP_MAX = 100
_GREP_MAX_FILE_BYTES = 2_000_000  # skip obviously-large/binary files


class RepoTools:
    """Read-only, path-guarded view of a single repository checkout."""

    def __init__(self, root: "str | os.PathLike[str]") -> None:
        self.root = Path(os.path.realpath(str(root)))
        if not self.root.is_dir():
            raise PathGuardError(f"target root is not a directory: {self.root}")

    # ------------------------------------------------------------------ guard
    def _resolve(self, path: "str | os.PathLike[str] | None") -> Path:
        """Resolve a user-supplied path to an absolute path inside the root.

        Raises PathGuardError on any violation: absolute paths, ``.git`` in the
        requested or resolved path, or escaping the target root (including via
        ``..`` or a symlink that points outside / into ``.git``).
        """
        raw = "." if path is None else str(path)

        if os.path.isabs(raw):
            raise PathGuardError(f"absolute paths are not allowed: {raw!r}")

        if _FORBIDDEN in raw.lower():
            raise PathGuardError(f"paths containing '.git' are blocked: {raw!r}")

        # realpath resolves symlinks and `..`, so the containment check below is
        # the real-filesystem answer, not a lexical guess.
        resolved = Path(os.path.realpath(str(self.root / raw)))

        try:
            rel = resolved.relative_to(self.root)
        except ValueError:
            raise PathGuardError(f"path escapes the target root: {raw!r}")

        # Catches a symlink whose target lives inside .git even though the
        # requested name did not contain ".git".
        if _FORBIDDEN in str(rel).lower():
            raise PathGuardError(f"resolved path hits '.git' metadata: {raw!r}")

        return resolved

    def _rel(self, resolved: Path) -> str:
        rel = resolved.relative_to(self.root).as_posix()
        return rel if rel else "."

    # ------------------------------------------------------------------ tools
    def list_dir(self, path: str = ".") -> str:
        """List names + types in a directory (``.git*`` entries are hidden)."""
        try:
            target = self._resolve(path)
        except PathGuardError as exc:
            return f"ERROR: {exc}"
        if not target.exists():
            return f"ERROR: no such path: {path!r}"
        if not target.is_dir():
            return f"ERROR: not a directory: {path!r}"

        entries = []
        for child in sorted(target.iterdir(), key=lambda p: p.name):
            if _FORBIDDEN in child.name.lower():
                continue  # hide .git / .github / .gitignore from listings
            if child.is_dir():
                kind = "dir"
            elif child.is_symlink():
                kind = "symlink"
            elif child.is_file():
                kind = "file"
            else:
                kind = "other"
            entries.append(f"{kind}\t{child.name}")

        header = f"{self._rel(target)}/ ({len(entries)} entries)"
        if not entries:
            return header + "\n(empty)"
        return header + "\n" + "\n".join(entries)

    def read_file(self, path: str, start_line: int = 1, end_line: "int | None" = None) -> str:
        """Return a line-numbered slice of a file, capped at MAX_READ_LINES."""
        try:
            target = self._resolve(path)
        except PathGuardError as exc:
            return f"ERROR: {exc}"
        if not target.exists():
            return f"ERROR: no such file: {path!r}"
        if not target.is_file():
            return f"ERROR: not a file: {path!r}"

        try:
            start = max(1, int(start_line))
        except (TypeError, ValueError):
            return f"ERROR: start_line must be an integer: {start_line!r}"

        if end_line is None:
            end = start + MAX_READ_LINES - 1
        else:
            try:
                end = int(end_line)
            except (TypeError, ValueError):
                return f"ERROR: end_line must be an integer: {end_line!r}"

        if end < start:
            return f"ERROR: end_line ({end}) is before start_line ({start})"

        capped = False
        if end - start + 1 > MAX_READ_LINES:
            end = start + MAX_READ_LINES - 1
            capped = True

        try:
            with target.open("r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError as exc:
            return f"ERROR: could not read {path!r}: {exc}"

        total = len(lines)
        chosen = lines[start - 1:end]
        last_shown = min(end, total)
        out = [f"{self._rel(target)}  (lines {start}-{last_shown} of {total})"]
        for lineno, line in enumerate(chosen, start=start):
            out.append(f"{lineno:>6}\t{line.rstrip(chr(10)).rstrip(chr(13))}")
        if last_shown < total:
            note = "truncated" + (f" to {MAX_READ_LINES} lines" if capped else "")
            out.append(f"... [{note}; {total - last_shown} more line(s); "
                       f"call again with start_line={last_shown + 1}]")
        return "\n".join(out)

    def grep(self, pattern: str, path: str = ".", max_results: int = DEFAULT_GREP_MAX) -> str:
        """Search files under `path` for a regex, returning file:line:text matches."""
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"ERROR: invalid regex {pattern!r}: {exc}"

        try:
            base = self._resolve(path)
        except PathGuardError as exc:
            return f"ERROR: {exc}"
        if not base.exists():
            return f"ERROR: no such path: {path!r}"

        try:
            cap = max(1, int(max_results))
        except (TypeError, ValueError):
            cap = DEFAULT_GREP_MAX

        candidates = []
        if base.is_file():
            candidates = [base]
        else:
            # followlinks=False: never descend into symlinked dirs (loops/escapes).
            for dirpath, dirnames, filenames in os.walk(base):
                # prune .git / .github / etc. so we never enter metadata dirs
                dirnames[:] = sorted(d for d in dirnames if _FORBIDDEN not in d.lower())
                for name in sorted(filenames):
                    if _FORBIDDEN in name.lower():
                        continue
                    candidates.append(Path(dirpath) / name)

        hits: list[str] = []
        for fpath in candidates:
            # Re-run the guard on every candidate: defends against a symlinked
            # file that points outside the root or into .git.
            try:
                rel = fpath.relative_to(self.root).as_posix()
                self._resolve(rel)
            except (PathGuardError, ValueError):
                continue
            try:
                if fpath.stat().st_size > _GREP_MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            try:
                with fpath.open("r", encoding="utf-8", errors="strict") as fh:
                    for lineno, line in enumerate(fh, start=1):
                        if regex.search(line):
                            text = line.rstrip(chr(10)).rstrip(chr(13))[:300]
                            hits.append(f"{rel}:{lineno}:{text}")
                            if len(hits) >= cap:
                                break
            except (UnicodeDecodeError, OSError):
                continue  # skip binary / unreadable files
            if len(hits) >= cap:
                break

        if not hits:
            return f"(no matches for {pattern!r} under {self._rel(base)})"
        header = f"{len(hits)} match(es) for {pattern!r}"
        if len(hits) >= cap:
            header += "  [capped]"
        return header + "\n" + "\n".join(hits)
