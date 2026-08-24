import io
import subprocess
import tempfile
import time
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from engineering_process.contracts import ContractError
from engineering_process.distribution_verify import _copy_tracked_snapshot
from engineering_process.git import GIT_STDIN_LIMIT, remaining_seconds, run_git
from engineering_process.runner import source_state
from engineering_process.supervision import CleanupOutcome


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
    def test_exit_zero_git_diagnostic_fails_closed_without_raw_text(self):
        process = mock.Mock()
        process.stdout = io.BytesIO(b"")
        process.stderr = io.BytesIO(b"WARNING: secret-shaped=value\n")
        process.returncode = 0
        process.wait.return_value = 0
        process.poll.return_value = 0
        supervisor = mock.Mock()
        supervisor.spawn.return_value = process
        supervisor.finalize.return_value = CleanupOutcome(bounded=True)

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "engineering_process.git.process_supervisor",
                return_value=supervisor,
            ),
            self.assertRaisesRegex(
                ContractError, "Git emitted forbidden warning/error diagnostics"
            ) as raised,
        ):
            run_git(
                Path(directory),
                ["status"],
                label="diagnostic Git",
                timeout_seconds=5,
                max_stdout_bytes=1_024,
            )

        self.assertNotIn("secret-shaped", str(raised.exception))

    def test_git_stdout_protocol_payload_is_not_classified_as_diagnostic(self):
        process = mock.Mock()
        process.stdout = io.BytesIO(b"ValidationError: tracked source payload\n")
        process.stderr = io.BytesIO(b"")
        process.returncode = 0
        process.wait.return_value = 0
        process.poll.return_value = 0
        supervisor = mock.Mock()
        supervisor.spawn.return_value = process
        supervisor.finalize.return_value = CleanupOutcome(bounded=True)

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "engineering_process.git.process_supervisor",
                return_value=supervisor,
            ),
        ):
            result = run_git(
                Path(directory),
                ["cat-file", "--batch"],
                label="Git protocol payload",
                timeout_seconds=5,
                max_stdout_bytes=1_024,
            )

        self.assertEqual(0, result.returncode)
        self.assertIn(b"ValidationError", result.stdout)

    def test_git_input_is_bounded_before_process_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            with self.assertRaisesRegex(ContractError, "stdin exceeds"):
                run_git(
                    root,
                    ["cat-file", "--batch"],
                    label="bounded Git input",
                    timeout_seconds=5,
                    max_stdout_bytes=1_024,
                    input_bytes=b"x" * (GIT_STDIN_LIMIT + 1),
                )

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
            self.assertEqual(
                root.resolve(), Path(result.stdout.decode().strip()).resolve()
            )

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

    def test_snapshot_materializes_head_objects_not_live_worktree_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            (root / "tracked.txt").write_text(
                "transient unreviewed bytes\n", encoding="utf-8"
            )
            with tempfile.TemporaryDirectory() as snapshot_directory:
                snapshot = Path(snapshot_directory)
                _copy_tracked_snapshot(root, snapshot)
                self.assertEqual(
                    "checkpoint\n",
                    (snapshot / "tracked.txt").read_text(encoding="utf-8"),
                )

    def test_snapshot_preserves_exact_binary_blob_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            expected = b"binary\x00line\nwindows\r\nend\xff"
            binary = root / "payload.bin"
            binary.write_bytes(expected)
            subprocess.run(["git", "add", "payload.bin"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "binary"],
                cwd=root,
                check=True,
            )

            with tempfile.TemporaryDirectory() as snapshot_directory:
                snapshot = Path(snapshot_directory)
                _copy_tracked_snapshot(root, snapshot)
                self.assertEqual(expected, (snapshot / "payload.bin").read_bytes())

    def test_snapshot_ignores_archive_attributes_and_preserves_raw_blobs(self):
        cases = {
            "export-subst": "tracked.txt export-subst\n",
            "export-ignore": "tracked.txt export-ignore\n",
        }
        for name, attributes in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._initialize_repository(root)
                (root / "tracked.txt").write_text(
                    "$Format:%H$\n", encoding="utf-8"
                )
                (root / ".gitattributes").write_text(
                    attributes, encoding="utf-8"
                )
                subprocess.run(
                    ["git", "add", ".gitattributes", "tracked.txt"],
                    cwd=root,
                    check=True,
                )
                subprocess.run(
                    ["git", "commit", "--quiet", "-m", name],
                    cwd=root,
                    check=True,
                )
                expected = subprocess.check_output(
                    ["git", "cat-file", "blob", "HEAD:tracked.txt"],
                    cwd=root,
                )

                with tempfile.TemporaryDirectory() as snapshot_directory:
                    snapshot = Path(snapshot_directory)
                    _copy_tracked_snapshot(root, snapshot)
                    self.assertEqual(
                        expected, (snapshot / "tracked.txt").read_bytes()
                    )

    def test_snapshot_can_pin_the_checkpoint_across_a_ref_move(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._initialize_repository(root)
            checkpoint = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            (root / "tracked.txt").write_text("later commit\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "--quiet", "-m", "later"], cwd=root, check=True
            )

            with tempfile.TemporaryDirectory() as snapshot_directory:
                snapshot = Path(snapshot_directory)
                _copy_tracked_snapshot(root, snapshot, checkpoint=checkpoint)
                self.assertEqual(
                    "checkpoint\n",
                    (snapshot / "tracked.txt").read_text(encoding="utf-8"),
                )

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
