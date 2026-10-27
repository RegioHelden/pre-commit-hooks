from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class HookTestCase(unittest.TestCase):
    """Runs the hook in a throwaway repository holding one managed file."""

    HOOK = Path(__file__).resolve().parent.parent / "hooks" / "check-managed-files.py"
    MARKER = "@generated"
    BOT = "bot@example.com"
    DEVELOPER = "developer@example.com"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("GIT_", "PRE_COMMIT_", "MANAGED_FILES_HOOK_"))
        }
        # keeps the developer's own git config out of the tests
        self.environment.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_COMMITTER_NAME="committer",
            GIT_COMMITTER_EMAIL="committer@example.com",
        )
        self.git("init", "--quiet", "--initial-branch=main")
        self.commit("Dockerfile", self.managed("FROM base"), author=self.BOT)
        self.commit("README.md", "readme\n", author=self.BOT)

    def run_hook(
        self,
        *arguments: str,
        marker: str | None = MARKER,
        author: str = DEVELOPER,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        marker_arguments = [] if marker is None else ["--marker", marker]
        return subprocess.run(  # noqa: S603
            [sys.executable, str(self.HOOK), *marker_arguments, *arguments],
            cwd=self.root,
            env={**self.author_environment(author), **(environment or {})},
            capture_output=True,
            text=True,
            check=False,
        )

    def assert_rejected(
        self,
        result: subprocess.CompletedProcess[str],
        path: str,
        author: str = DEVELOPER,
    ) -> None:
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            f"{path}: carries the managed-file marker, {author}",
            result.stderr,
        )

    def stage(self, name: str, content: str) -> None:
        (self.root / name).write_text(content)
        self.git("add", "--", name)

    def commit(self, name: str, content: str, *, author: str = DEVELOPER) -> str:
        self.stage(name, content)
        self.git("commit", "--quiet", "--message", f"change {name}", author=author)
        return self.git("rev-parse", "HEAD")

    def git(self, *arguments: str, author: str = DEVELOPER, check: bool = True) -> str:
        result = subprocess.run(  # noqa: S603
            ["git", *arguments],  # noqa: S607
            cwd=self.root,
            env=self.author_environment(author),
            capture_output=True,
            text=True,
            check=check,
        )
        return result.stdout.strip()

    def author_environment(self, author: str) -> dict[str, str]:
        return {
            **self.environment,
            "GIT_AUTHOR_NAME": "author",
            "GIT_AUTHOR_EMAIL": author,
        }

    def managed(self, body: str) -> str:
        return f"# {self.MARKER}\n{body}\n"


class TestStagedCommit(HookTestCase):
    def test_an_edit_to_a_managed_file_is_rejected(self) -> None:
        self.stage("Dockerfile", self.managed("FROM other"))

        self.assert_rejected(self.run_hook(), "Dockerfile")

    def test_an_allowed_author_may_edit_a_managed_file(self) -> None:
        self.stage("Dockerfile", self.managed("FROM other"))

        result = self.run_hook("--allow-author", self.BOT, author=self.BOT)

        self.assertEqual(result.returncode, 0)

    def test_allowed_authors_may_come_from_the_environment(self) -> None:
        self.stage("Dockerfile", self.managed("FROM other"))

        allowed_authors = f"someone@example.com, {self.BOT}"

        result = self.run_hook(
            author=self.BOT,
            environment={"MANAGED_FILES_HOOK_ALLOWED_AUTHORS": allowed_authors},
        )

        self.assertEqual(result.returncode, 0)

    def test_an_edit_to_an_unmanaged_file_passes(self) -> None:
        self.stage("README.md", "changed\n")

        self.assertEqual(self.run_hook().returncode, 0)

    def test_dropping_the_marker_takes_the_file_over(self) -> None:
        self.stage("Dockerfile", "FROM own\n")

        self.assertEqual(self.run_hook().returncode, 0)

    def test_an_unstaged_edit_is_not_checked(self) -> None:
        (self.root / "Dockerfile").write_text(self.managed("FROM other"))

        self.assertEqual(self.run_hook().returncode, 0)

    def test_a_marker_below_the_header_is_ignored(self) -> None:
        self.stage("NOTES.md", "\n" * 5 + f"quotes {self.MARKER}\n")

        self.assertEqual(self.run_hook().returncode, 0)

    def test_header_lines_widens_the_search_for_the_marker(self) -> None:
        self.stage("NOTES.md", "\n" * 5 + f"quotes {self.MARKER}\n")

        self.assert_rejected(self.run_hook("--header-lines", "6"), "NOTES.md")

    def test_the_hint_is_appended_to_a_rejection(self) -> None:
        self.stage("Dockerfile", self.managed("FROM other"))

        result = self.run_hook("--hint", "change its template instead")

        self.assertIn("may not change it; change its template instead", result.stderr)

    def test_a_symlink_to_a_managed_file_is_ignored(self) -> None:
        (self.root / "link").symlink_to("Dockerfile")
        self.git("add", "link")

        self.assertEqual(self.run_hook().returncode, 0)

    def test_unusual_file_names_are_checked(self) -> None:
        for name in ["1:x", "überall.txt", "trailing ", "star*"]:
            with self.subTest(name=name):
                self.stage(name, self.managed("content"))

                self.assert_rejected(self.run_hook(), name)


class TestConflictedMerge(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.git("switch", "--quiet", "--create", "feature")
        self.commit("README.md", "feature readme\n")
        self.git("switch", "--quiet", "main")
        self.commit("README.md", "main readme\n", author=self.BOT)
        self.commit("Dockerfile", self.managed("FROM newer"), author=self.BOT)
        self.git("switch", "--quiet", "feature")
        self.git("merge", "--quiet", "main", check=False)
        self.stage("README.md", "resolved readme\n")

    def test_a_managed_update_from_the_merged_branch_passes(self) -> None:
        self.assertEqual(self.run_hook().returncode, 0)

    def test_a_hand_edit_during_resolution_is_rejected(self) -> None:
        self.stage("Dockerfile", self.managed("FROM hand edited"))

        self.assert_rejected(self.run_hook(), "Dockerfile")


class TestRevisionRange(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = self.git("rev-parse", "HEAD")

    def test_a_commit_by_another_author_is_rejected(self) -> None:
        self.commit("Dockerfile", self.managed("FROM other"))

        result = self.run_hook("--allow-author", self.BOT, "--from-ref", self.base)

        self.assert_rejected(result, "Dockerfile")

    def test_a_later_commit_by_an_allowed_author_does_not_hide_an_edit(self) -> None:
        self.commit("Dockerfile", self.managed("FROM other"))
        self.commit("README.md", "unrelated\n")
        self.commit("Dockerfile", self.managed("FROM synced"), author=self.BOT)

        result = self.run_hook("--allow-author", self.BOT, "--from-ref", self.base)

        self.assert_rejected(result, "Dockerfile")

    def test_a_commit_by_an_allowed_author_passes(self) -> None:
        self.commit("Dockerfile", self.managed("FROM other"), author=self.BOT)

        result = self.run_hook("--allow-author", self.BOT, "--from-ref", self.base)

        self.assertEqual(result.returncode, 0)

    def test_prek_passes_the_refs_on_pre_push(self) -> None:
        tip = self.commit("Dockerfile", self.managed("FROM other"))

        result = self.run_hook(
            environment={"PRE_COMMIT_FROM_REF": self.base, "PRE_COMMIT_TO_REF": tip},
        )

        self.assert_rejected(result, "Dockerfile")

    def test_ci_may_pass_the_range_through_the_environment(self) -> None:
        self.commit("Dockerfile", self.managed("FROM other"))

        result = self.run_hook(
            environment={
                "MANAGED_FILES_HOOK_FROM_REF": self.base,
                "MANAGED_FILES_HOOK_TO_REF": "",
            },
        )

        self.assert_rejected(result, "Dockerfile")

    def test_a_push_to_an_empty_remote_passes(self) -> None:
        tip = self.commit("Dockerfile", self.managed("FROM other"))

        result = self.run_hook(environment={"PRE_COMMIT_TO_REF": tip})

        self.assertEqual(result.returncode, 0)


class TestArguments(HookTestCase):
    # argparse's exit status for a usage error
    USAGE_ERROR = 2

    def test_the_marker_is_required(self) -> None:
        self.assertEqual(self.run_hook(marker=None).returncode, self.USAGE_ERROR)

    def test_invalid_values_are_rejected(self) -> None:
        for arguments in [
            ["--marker", ""],
            ["--marker", self.MARKER, "--header-lines", "0"],
            ["--marker", self.MARKER, "--header-bytes", "many"],
        ]:
            with self.subTest(arguments=arguments):
                result = self.run_hook(*arguments, marker=None)

                self.assertEqual(result.returncode, self.USAGE_ERROR)
