import tempfile
import time
import unittest
from pathlib import Path, PurePosixPath

from engineering_process.distribution_verify import _tracked_paths
from engineering_process.git import remaining_seconds, run_git


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
MAX_GIT_OUTPUT_BYTES = 256_000
MAX_POLICY_BYTES = 4_096
PATHS_PER_QUERY = 128
TEST_TIMEOUT_SECONDS = 30.0


def _chunks(paths: list[PurePosixPath]) -> list[list[PurePosixPath]]:
    return [
        paths[index : index + PATHS_PER_QUERY]
        for index in range(0, len(paths), PATHS_PER_QUERY)
    ]


class SourceCheckoutTests(unittest.TestCase):
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
        self.assertEqual(
            PRODUCER_ATTRIBUTES,
            self.bounded_bytes(
                PROCESS_ROOT / ".gitattributes", limit=MAX_POLICY_BYTES
            ),
        )
        paths = _tracked_paths(PROCESS_ROOT)
        self.assertIn(PurePosixPath(".gitattributes"), paths)
        deadline = time.monotonic() + TEST_TIMEOUT_SECONDS

        for chunk in _chunks(paths):
            names = [path.as_posix() for path in chunk]
            attributes = self.git(
                PROCESS_ROOT,
                ["check-attr", "-z", *ATTRIBUTE_NAMES, "--", *names],
                label="inspect producer checkout attributes",
                deadline=deadline,
            ).split(b"\0")
            self.assertEqual(b"", attributes.pop())
            self.assertEqual(0, len(attributes) % 3)
            observed: dict[str, dict[str, str]] = {}
            for index in range(0, len(attributes), 3):
                path, attribute, value = (
                    item.decode("utf-8") for item in attributes[index : index + 3]
                )
                observed.setdefault(path, {})[attribute] = value
            self.assertEqual(set(names), set(observed))
            for name in names:
                self.assertEqual(EXPECTED_ATTRIBUTES, observed[name])

            records = self.git(
                PROCESS_ROOT,
                ["ls-files", "-z", "--eol", "--", *names],
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
