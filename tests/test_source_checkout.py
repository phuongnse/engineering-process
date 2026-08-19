import subprocess
import tempfile
import time
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from engineering_process.contracts import ContractError
from engineering_process.distribution_verify import _copy_tracked_snapshot
from engineering_process.git import remaining_seconds, run_git
from engineering_process.runner import source_state


PROCESS_ROOT = Path(__file__).resolve().parent.parent
PRODUCER_ATTRIBUTES = (
    b"* text=auto eol=lf -working-tree-encoding -filter -ident\n"
)
ATTRIBUTE_NAMES = (
    "text",
    "eol",
    "working-tree-encoding",
    "filter",
    "ident",
)
EXPECTED_ATTRIBUTES = {
    "text": "auto",
    "eol": "lf",
    "working-tree-encoding": "unset",
    "filter": "unset",
    "ident": "unset",
}
CHECK_ATTRIBUTES_ARGUMENTS = ["check-attr", "-z", *ATTRIBUTE_NAMES, "--"]
CHECK_EOL_ARGUMENTS = ["ls-files", "-z", "--eol", "--"]
MAX_GIT_OUTPUT_BYTES = 256_000
MAX_WINDOWS_TARGET_COMMAND_CHARS = 12_000
MAX_POLICY_BYTES = 4_096
TEST_TIMEOUT_SECONDS = 30.0


def _target_command_chars(
    arguments: list[str], paths: list[PurePosixPath]
) -> int:
    return len(
        subprocess.list2cmdline(
            ["git", *arguments, *(path.as_posix() for path in paths)]
        )
    )


def _path_chunks(paths: list[PurePosixPath]) -> list[list[PurePosixPath]]:
    chunks: list[list[PurePosixPath]] = []
    current: list[PurePosixPath] = []
    for path in paths:
        candidate = [*current, path]
        length = max(
            _target_command_chars(CHECK_ATTRIBUTES_ARGUMENTS, candidate),
            _target_command_chars(CHECK_EOL_ARGUMENTS, candidate),
        )
        if length <= MAX_WINDOWS_TARGET_COMMAND_CHARS:
            current = candidate
            continue
        if not current:
            raise ValueError(f"tracked path exceeds the Git argv limit: {path}")
        chunks.append(current)
        current = [path]
        if max(
            _target_command_chars(CHECK_ATTRIBUTES_ARGUMENTS, current),
            _target_command_chars(CHECK_EOL_ARGUMENTS, current),
        ) > MAX_WINDOWS_TARGET_COMMAND_CHARS:
            raise ValueError(f"tracked path exceeds the Git argv limit: {path}")
    if current:
        chunks.append(current)
    return chunks


class SourceCheckoutTests(unittest.TestCase):
    def test_git_environment_cannot_redirect_checkpoint_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            other = base / "other"
            root.mkdir()
            other.mkdir()
            self._initialize_repository(root)
            self._initialize_repository(other)
            with mock.patch.dict(
                "os.environ", {"GIT_DIR": str(other / ".git")}, clear=False
            ):
                result = run_git(
                    root,
                    ["rev-parse", "--show-toplevel"],
                    label="sanitized Git environment",
                    timeout_seconds=5,
                    max_stdout_bytes=1_024,
                )
            self.assertEqual(0, result.returncode)
            self.assertEqual(str(root.resolve()), result.stdout.decode().strip())

    def _initialize_repository(self, root: Path) -> None:
        subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "tests@example.invalid"],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Tests"], cwd=root, check=True
        )
        (root / "tracked.txt").write_text("checkpoint\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "checkpoint"],
            cwd=root,
            check=True,
        )

    def test_hidden_index_flags_are_rejected_by_fingerprint_and_snapshot(self):
        for flag in ("--assume-unchanged", "--skip-worktree"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._initialize_repository(root)
                tracked = root / "tracked.txt"
                tracked.write_text("changed behind Git's back\n", encoding="utf-8")
                subprocess.run(
                    ["git", "update-index", flag, "--", "tracked.txt"],
                    cwd=root,
                    check=True,
                )

                with self.assertRaisesRegex(ContractError, "hidden index flag"):
                    source_state(root)
                with tempfile.TemporaryDirectory() as snapshot_directory:
                    with self.assertRaisesRegex(ContractError, "hidden index flag"):
                        _copy_tracked_snapshot(root, Path(snapshot_directory))

    def bounded_bytes(self, path: Path, *, limit: int) -> bytes:
        self.assertLessEqual(path.stat().st_size, limit)
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        self.assertLessEqual(len(data), limit)
        return data

    def git(
        self,
        root: Path,
        arguments: list[str],
        *,
        label: str,
        deadline: float,
    ) -> bytes:
        result = run_git(
            root,
            arguments,
            label=label,
            timeout_seconds=remaining_seconds(deadline, label=label),
            max_stdout_bytes=MAX_GIT_OUTPUT_BYTES,
        )
        self.assertEqual(
            0,
            result.returncode,
            result.stderr.decode(errors="replace"),
        )
        return result.stdout

    def test_all_tracked_sources_have_byte_stable_checkout_attributes(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "source"
            snapshot.mkdir()
            paths = _copy_tracked_snapshot(PROCESS_ROOT, snapshot)
            self.assertIn(PurePosixPath(".gitattributes"), paths)
            self.assertEqual(
                PRODUCER_ATTRIBUTES,
                self.bounded_bytes(
                    snapshot / ".gitattributes", limit=MAX_POLICY_BYTES
                ),
            )
            deadline = time.monotonic() + TEST_TIMEOUT_SECONDS
            self.git(
                snapshot,
                ["init", "--quiet"],
                label="initialize source snapshot Git",
                deadline=deadline,
            )
            self.git(
                snapshot,
                ["config", "core.autocrlf", "true"],
                label="configure source snapshot Git",
                deadline=deadline,
            )
            self.git(
                snapshot,
                ["add", "--all"],
                label="stage bounded source snapshot",
                deadline=deadline,
            )

            for chunk in _path_chunks(paths):
                names = [path.as_posix() for path in chunk]
                attributes = self.git(
                    snapshot,
                    [*CHECK_ATTRIBUTES_ARGUMENTS, *names],
                    label="inspect producer checkout attributes",
                    deadline=deadline,
                ).split(b"\0")
                self.assertEqual(b"", attributes.pop())
                self.assertEqual(0, len(attributes) % 3)
                observed: dict[str, dict[str, str]] = {}
                for index in range(0, len(attributes), 3):
                    path, attribute, value = (
                        item.decode("utf-8")
                        for item in attributes[index : index + 3]
                    )
                    observed.setdefault(path, {})[attribute] = value
                self.assertEqual(set(names), set(observed))
                for name in names:
                    self.assertEqual(EXPECTED_ATTRIBUTES, observed[name])

                records = self.git(
                    snapshot,
                    [*CHECK_EOL_ARGUMENTS, *names],
                    label="inspect producer checkout line endings",
                    deadline=deadline,
                ).split(b"\0")
                self.assertEqual(b"", records.pop())
                self.assertEqual(len(names), len(records))
                for record in records:
                    metadata, separator, encoded_path = record.partition(b"\t")
                    self.assertEqual(b"\t", separator)
                    self.assertIn(encoded_path.decode("utf-8"), names)
                    fields = metadata.split()
                    self.assertGreaterEqual(len(fields), 2)
                    self.assertIn(
                        (fields[0], fields[1]),
                        {
                            (b"i/lf", b"w/lf"),
                            (b"i/-text", b"w/-text"),
                            (b"i/none", b"w/none"),
                        },
                    )

    def test_path_chunks_bound_windows_command_line_characters(self):
        paths = [
            PurePosixPath(f"directory-{index:03}/{'x' * 240}.txt")
            for index in range(128)
        ]
        chunks = _path_chunks(paths)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(paths, [path for chunk in chunks for path in chunk])
        for chunk in chunks:
            self.assertLessEqual(
                _target_command_chars(CHECK_ATTRIBUTES_ARGUMENTS, chunk),
                MAX_WINDOWS_TARGET_COMMAND_CHARS,
            )
            self.assertLessEqual(
                _target_command_chars(CHECK_EOL_ARGUMENTS, chunk),
                MAX_WINDOWS_TARGET_COMMAND_CHARS,
            )

    def test_binary_checkout_bytes_are_not_normalized(self):
        binary = b"\x00process-binary\r\n\xff"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".gitattributes").write_bytes(PRODUCER_ATTRIBUTES)
            target = root / "asset.bin"
            target.write_bytes(binary)
            deadline = time.monotonic() + TEST_TIMEOUT_SECONDS
            self.git(
                root,
                ["init", "--quiet"],
                label="initialize Git",
                deadline=deadline,
            )
            self.git(
                root,
                ["config", "core.autocrlf", "true"],
                label="configure Git newline conversion",
                deadline=deadline,
            )
            self.git(
                root,
                ["add", ".gitattributes", "asset.bin"],
                label="stage binary fixture",
                deadline=deadline,
            )
            line_endings = self.git(
                root,
                ["ls-files", "--eol", "--", "asset.bin"],
                label="inspect binary classification",
                deadline=deadline,
            )
            self.assertIn(b"i/-text", line_endings)
            self.assertIn(b"w/-text", line_endings)
            target.unlink()
            self.git(
                root,
                ["checkout", "--", "asset.bin"],
                label="restore binary fixture",
                deadline=deadline,
            )
            self.assertEqual(
                binary,
                self.bounded_bytes(target, limit=MAX_POLICY_BYTES),
            )
