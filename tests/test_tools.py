"""Path-guard unit tests for harness/tools.py.

The guard is the security-critical boundary of this study (README guardrails
#1 and #3). These tests prove it rejects, and never leaks content through:

  * reads of the .git directory (and .github / .gitignore / .gitattributes),
  * directory-escape attempts via ``..``,
  * absolute paths,
  * symlinks that point outside the root or into .git.

Run from the repo root:  python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make the repo root importable when run via `python -m unittest` from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.tools import RepoTools, PathGuardError  # noqa: E402


# Distinctive tokens that must NEVER appear in any tool output.
GIT_SECRET = "COMMIT_FIX_SECRET_do_not_leak"
GITHUB_SECRET = "WORKFLOW_SECRET_do_not_leak"
GITIGNORE_SECRET = "GITIGNORE_SECRET_do_not_leak"
OUTSIDE_SECRET = "OUTSIDE_ROOT_SECRET_do_not_leak"
# A token that lives in a legitimate source file and SHOULD be reachable.
SOURCE_MARKER = "marker_alpha_reachable"


class PathGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)

        # The target root the tools are rooted at.
        self.root = tmp / "php-src"
        (self.root / "main").mkdir(parents=True)
        (self.root / "ext" / "standard").mkdir(parents=True)

        # A legitimate source file (reachable).
        (self.root / "main" / "php.h").write_text(
            "line one\nint " + SOURCE_MARKER + "(void);\nline three\n"
        )
        (self.root / "ext" / "standard" / "string.c").write_text(
            "/* normal source, nothing secret */\nvoid f(void) {}\n"
        )

        # Git metadata that must be fully blocked.
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text(GIT_SECRET + "\n")
        (self.root / ".github" / "workflows").mkdir(parents=True)
        (self.root / ".github" / "workflows" / "ci.yml").write_text(GITHUB_SECRET + "\n")
        (self.root / ".gitignore").write_text(GITIGNORE_SECRET + "\n")

        # A secret OUTSIDE the root (escape target).
        self.outside = tmp / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text(OUTSIDE_SECRET + "\n")

        # A large file for the read cap test (1000 numbered lines).
        self.root.joinpath("big.txt").write_text(
            "".join(f"bigline_{i:04d}\n" for i in range(1, 1001))
        )

        # Symlinks (skipped individually if the platform refuses them).
        self.symlink_escape = self.root / "escape_link"
        self.symlink_to_git = self.root / "git_link"
        try:
            self.symlink_escape.symlink_to(self.outside / "secret.txt")
            self.symlink_to_git.symlink_to(self.root / ".git")
            self.symlinks_ok = True
        except (OSError, NotImplementedError):
            self.symlinks_ok = False

        self.tools = RepoTools(self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # -- positive controls ---------------------------------------------------
    def test_valid_read_returns_content_with_line_numbers(self):
        out = self.tools.read_file("main/php.h")
        self.assertIn(SOURCE_MARKER, out)
        self.assertIn("     2\t", out)  # line numbering present

    def test_valid_list_shows_source_dirs(self):
        out = self.tools.list_dir(".")
        self.assertIn("main", out)
        self.assertIn("ext", out)

    def test_grep_finds_source_marker(self):
        out = self.tools.grep(SOURCE_MARKER, ".")
        self.assertIn("main/php.h", out)
        self.assertIn(SOURCE_MARKER, out)

    # -- .git / .github / .gitignore are blocked -----------------------------
    def test_listing_hides_git_entries(self):
        out = self.tools.list_dir(".")
        self.assertNotIn(".git", out)
        self.assertNotIn(".github", out)
        self.assertNotIn(".gitignore", out)

    def test_read_dotgit_config_blocked_and_no_leak(self):
        out = self.tools.read_file(".git/config")
        self.assertTrue(out.startswith("ERROR"))
        self.assertNotIn(GIT_SECRET, out)

    def test_read_nested_dotgit_blocked(self):
        out = self.tools.read_file("main/../.git/config")
        self.assertTrue(out.startswith("ERROR"))
        self.assertNotIn(GIT_SECRET, out)

    def test_read_gitignore_blocked_and_no_leak(self):
        out = self.tools.read_file(".gitignore")
        self.assertTrue(out.startswith("ERROR"))
        self.assertNotIn(GITIGNORE_SECRET, out)

    def test_read_github_workflow_blocked_and_no_leak(self):
        out = self.tools.read_file(".github/workflows/ci.yml")
        self.assertTrue(out.startswith("ERROR"))
        self.assertNotIn(GITHUB_SECRET, out)

    def test_resolve_raises_on_dotgit(self):
        with self.assertRaises(PathGuardError):
            self.tools._resolve(".git/config")

    # -- directory escape ----------------------------------------------------
    def test_parent_escape_blocked_and_no_leak(self):
        out = self.tools.read_file("../outside/secret.txt")
        self.assertTrue(out.startswith("ERROR"))
        self.assertNotIn(OUTSIDE_SECRET, out)

    def test_deep_parent_escape_blocked(self):
        out = self.tools.read_file("main/../../outside/secret.txt")
        self.assertTrue(out.startswith("ERROR"))
        self.assertNotIn(OUTSIDE_SECRET, out)

    def test_resolve_raises_on_escape(self):
        with self.assertRaises(PathGuardError):
            self.tools._resolve("../outside/secret.txt")

    # -- absolute paths ------------------------------------------------------
    def test_absolute_path_blocked(self):
        abs_secret = str(self.outside / "secret.txt")
        out = self.tools.read_file(abs_secret)
        self.assertTrue(out.startswith("ERROR"))
        self.assertNotIn(OUTSIDE_SECRET, out)

    def test_resolve_raises_on_absolute(self):
        with self.assertRaises(PathGuardError):
            self.tools._resolve("/etc/hostname")

    # -- symlink escapes -----------------------------------------------------
    def test_symlink_escape_blocked_and_no_leak(self):
        if not self.symlinks_ok:
            self.skipTest("symlinks not supported on this platform")
        out = self.tools.read_file("escape_link")
        self.assertTrue(out.startswith("ERROR"))
        self.assertNotIn(OUTSIDE_SECRET, out)

    def test_symlink_into_git_blocked_and_no_leak(self):
        if not self.symlinks_ok:
            self.skipTest("symlinks not supported on this platform")
        out = self.tools.read_file("git_link/config")
        self.assertTrue(out.startswith("ERROR"))
        self.assertNotIn(GIT_SECRET, out)

    # -- grep never descends into blocked areas ------------------------------
    def test_grep_never_leaks_git_or_outside_secrets(self):
        out = self.tools.grep("_do_not_leak", ".")
        for secret in (GIT_SECRET, GITHUB_SECRET, GITIGNORE_SECRET, OUTSIDE_SECRET):
            self.assertNotIn(secret, out)

    # -- read cap ------------------------------------------------------------
    def test_read_cap_truncates(self):
        out = self.tools.read_file("big.txt", start_line=1, end_line=1000)
        self.assertIn("[truncated", out)
        self.assertIn("bigline_0001", out)
        self.assertIn("bigline_0400", out)
        self.assertNotIn("bigline_0401", out)

    def test_read_default_cap(self):
        out = self.tools.read_file("big.txt")
        self.assertIn("[truncated", out)
        self.assertNotIn("bigline_0401", out)


if __name__ == "__main__":
    unittest.main()
