#!/usr/bin/env python3
from __future__ import annotations

import abc
import argparse
import os
import subprocess
import sys
from pathlib import Path


def required(value: str) -> str:
    """An argparse type that rejects an empty value."""
    if not value:
        raise argparse.ArgumentTypeError("needs a value")
    return value


def positive(value: str) -> int:
    """An argparse type that rejects anything but a whole number above zero."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("needs a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("needs a number above zero")
    return number


class ManagedFilesCheck:
    """Rejects changes to managed files by authors not on the allowlist."""

    # reading past the header could match docs that quote the marker
    HEADER_LINES = 5
    # bounds the read for binary files without line breaks
    HEADER_BYTES = 4096

    def main(self, argv: list[str]) -> int:
        parser = argparse.ArgumentParser()
        self.add_arguments(parser)
        options = parser.parse_args(argv[1:])
        try:
            return self.run(**vars(options))
        except subprocess.CalledProcessError as error:
            # git has already printed its own error
            return error.returncode

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--marker",
            type=required,
            required=True,
        )
        parser.add_argument(
            "--hint",
            type=required,
            help="how a project changes or takes over a managed file",
        )
        parser.add_argument("--header-lines", type=positive, default=self.HEADER_LINES)
        parser.add_argument("--header-bytes", type=positive, default=self.HEADER_BYTES)
        parser.add_argument(
            "--allow-author",
            type=required,
            action="extend",
            nargs="+",
            # comma-separated, so CI can set the list without touching the config
            default=[
                author.strip()
                for author in os.environ.get(
                    "MANAGED_FILES_HOOK_ALLOWED_AUTHORS",
                    "",
                ).split(",")
                if author.strip()
            ],
            dest="allowed_authors",
            metavar="EMAIL",
        )
        # prek sets PRE_COMMIT_* on pre-push, CI sets MANAGED_FILES_HOOK_* for any run
        parser.add_argument(
            "--from-ref",
            default=os.environ.get("PRE_COMMIT_FROM_REF")
            or os.environ.get("MANAGED_FILES_HOOK_FROM_REF"),
        )
        parser.add_argument(
            "--to-ref",
            default=os.environ.get("PRE_COMMIT_TO_REF")
            or os.environ.get("MANAGED_FILES_HOOK_TO_REF"),
        )

    def run(
        self,
        *,
        marker: str,
        hint: str | None,
        header_lines: int,
        header_bytes: int,
        allowed_authors: list[str],
        from_ref: str | None,
        to_ref: str | None,
    ) -> int:
        change = ProposedChange.from_refs(from_ref, to_ref)
        if change is None:
            return 0
        self.marker = marker.encode()
        self.hint = hint
        self.header_lines = header_lines
        self.header_bytes = header_bytes
        allowed = set(allowed_authors)
        violations = [
            violation
            for path in change.changed_files()
            for violation in self.check(path, change, allowed)
        ]
        for violation in violations:
            sys.stderr.write(f"{violation}\n")
        return 1 if violations else 0

    def check(
        self,
        path: Path,
        change: ProposedChange,
        allowed_authors: set[str],
    ) -> list[str]:
        # dropping the marker is how a project takes a file over
        if not self._carries_marker(path, change):
            return []
        suffix = f"; {self.hint}" if self.hint else ""
        return [
            f"{path}: carries the managed-file marker,"
            f" {author} may not change it{suffix}"
            for author in sorted(change.authors_of(path) - allowed_authors)
        ]

    def _carries_marker(self, path: Path, change: ProposedChange) -> bool:
        header = change.header_of(path, self.header_bytes)
        if header is None:
            return False
        lines = header.split(b"\n")[: self.header_lines]
        return any(self.marker in line for line in lines)


class ProposedChange(abc.ABC):
    """What a commit or a push is about to add, as git records it."""

    # file names are never glob patterns, even when they contain `*`
    GIT = ("git", "--literal-pathspecs")

    @classmethod
    def from_refs(
        cls,
        from_ref: str | None,
        to_ref: str | None,
    ) -> ProposedChange | None:
        if from_ref:
            # a base alone reaches up to what is checked out
            revision_range = RevisionRange(from_ref, to_ref or "HEAD")
            revision_range.fetch_missing_history()
            return revision_range
        # the first push to an empty remote has no base to compare against
        if to_ref:
            return None
        return StagedCommit()

    @abc.abstractmethod
    def changed_files(self) -> list[Path]: ...

    @abc.abstractmethod
    def authors_of(self, path: Path) -> set[str]: ...

    def header_of(self, path: Path, size: int) -> bytes | None:
        """The start of path in this change, or None for symlinks and submodules."""
        # only regular files, mode 100644 or 100755, have a header of their own
        if not self._mode_of(path).startswith("100"):
            return None
        command = [*self.GIT, "cat-file", "blob", self._object_name(path)]
        with subprocess.Popen(command, stdout=subprocess.PIPE) as process:  # noqa: S603
            header = process.stdout.read(size) if process.stdout else b""
            # spares streaming the rest of a large binary
            process.kill()
        return header

    @abc.abstractmethod
    def _mode_of(self, path: Path) -> str: ...

    @abc.abstractmethod
    def _object_name(self, path: Path) -> str: ...

    def _diff(self, *revisions: str) -> list[Path]:
        output = self._git("diff", "--name-only", "-z", "--diff-filter=d", *revisions)
        return [Path(name) for name in output.split("\0") if name]

    def _git(self, *arguments: str) -> str:
        command = [*self.GIT, *arguments]
        result = subprocess.run(  # noqa: S603
            command,
            stdout=subprocess.PIPE,
            check=True,
            encoding="utf-8",
            # paths that are not UTF-8 reach Path byte for byte
            errors="surrogateescape",
        )
        # file names may end in whitespace, so only git's own newline goes
        return result.stdout.removesuffix("\n")

    def _git_succeeds(self, *arguments: str) -> bool:
        command = [*self.GIT, *arguments]
        result = subprocess.run(  # noqa: S603
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0


class StagedCommit(ProposedChange):
    """The commit being made, checked by the pre-commit stage."""

    def changed_files(self) -> list[Path]:
        staged = self._diff("--cached")
        if not self._git_succeeds("rev-parse", "--quiet", "--verify", "MERGE_HEAD"):
            return staged
        # files matching either parent came in with the merge, not from a hand edit
        differing_from_merged_branch = set(self._diff("--cached", "MERGE_HEAD"))
        return [path for path in staged if path in differing_from_merged_branch]

    def authors_of(self, path: Path) -> set[str]:
        # whoever commits is the author of every staged change
        ident = self._git("var", "GIT_AUTHOR_IDENT")
        return {ident.partition("<")[2].partition(">")[0]}

    def _mode_of(self, path: Path) -> str:
        entry = self._git("ls-files", "--stage", "-z", "--", str(path))
        return entry.partition(" ")[0]

    def _object_name(self, path: Path) -> str:
        # stage 0 spelled out, or a name like `1:x` reads as stage 1 of `x`
        return f":0:{path}"


class RevisionRange(ProposedChange):
    """Commits on to_ref since from_ref, checked on pre-push and in CI."""

    FETCH_DEPTH_STEP = 100

    def __init__(self, from_ref: str, to_ref: str) -> None:
        self.from_ref = from_ref
        self.to_ref = to_ref

    def fetch_missing_history(self) -> None:
        """Deepens a shallow clone, as CI makes, until both refs share history."""
        while self._git("rev-parse", "--is-shallow-repository") == "true":
            if self._git_succeeds("merge-base", self.from_ref, self.to_ref):
                return
            self._git("fetch", "--quiet", f"--deepen={self.FETCH_DEPTH_STEP}", "origin")

    def changed_files(self) -> list[Path]:
        return self._diff(f"{self.from_ref}...{self.to_ref}")

    def authors_of(self, path: Path) -> set[str]:
        log = self._git(
            "log",
            "--format=%ae",
            f"{self.from_ref}..{self.to_ref}",
            "--",
            str(path),
        )
        return set(log.splitlines())

    def _mode_of(self, path: Path) -> str:
        entry = self._git("ls-tree", "-z", self.to_ref, "--", str(path))
        return entry.partition(" ")[0]

    def _object_name(self, path: Path) -> str:
        return f"{self.to_ref}:{path}"


if __name__ == "__main__":
    sys.exit(ManagedFilesCheck().main(sys.argv))
