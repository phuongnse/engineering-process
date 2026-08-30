from __future__ import annotations

from pathlib import Path
import shutil
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

from engineering_process.contracts import ProcessError, read_json
from engineering_process.release import derive_next_version, validate_release
from verification.normalize_sdist import normalize


ROOT = Path(__file__).resolve().parent.parent


class ReleaseTests(unittest.TestCase):
    def test_current_release_identity_is_consistent(self) -> None:
        result = validate_release(ROOT, ROOT, tag="v0.9.0")
        self.assertEqual("0.9.0", result["version"])
        self.assertEqual(1, result["changeCount"])

    def test_semver_is_derived_from_change_classification(self) -> None:
        self.assertEqual("0.9.1", derive_next_version("0.9.0", ["fix"]))
        self.assertEqual("0.10.0", derive_next_version("0.9.0", ["capability"]))
        self.assertEqual("1.0.0", derive_next_version("0.9.0", ["breaking"]))
        self.assertEqual("3.0.0", derive_next_version("2.4.1", ["fix", "breaking"]))

    def test_simplification_fragment_derives_one_dot_zero(self) -> None:
        fragment = read_json(ROOT / "release-changes" / "simplify-process-core.json")
        self.assertEqual("1.0.0", derive_next_version("0.9.0", [fragment["type"]]))

    def test_release_preparation_materializes_one_dot_zero_in_a_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source"
            shutil.copytree(
                ROOT,
                target,
                ignore=shutil.ignore_patterns(
                    ".git", ".venv", "build", "*.egg-info", "__pycache__"
                ),
            )
            result = subprocess.run(
                [sys.executable, "verification/prepare_release.py", "1.0.0"],
                cwd=target,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            prepared = read_json(target / "release.json")
            self.assertEqual("1.0.0", prepared["version"])
            self.assertEqual("0.9.0", prepared["previousVersion"])
            self.assertFalse(
                (target / "release-changes" / "simplify-process-core.json").exists()
            )
            self.assertIn('version = "1.0.0"', (target / "pyproject.toml").read_text())

    def test_invalid_change_type_fails(self) -> None:
        with self.assertRaises(ProcessError):
            derive_next_version("0.9.0", ["governance"])

    def test_release_files_are_reproducible_across_checkout_mtimes(self) -> None:
        epoch = 1_700_000_000
        built: list[dict[str, str]] = []
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            for index in range(2):
                source = temporary / f"source-{index}"
                output = temporary / f"dist-{index}"
                shutil.copytree(
                    ROOT,
                    source,
                    ignore=shutil.ignore_patterns(
                        ".git", ".venv", "build", "dist", "*.egg-info", "__pycache__"
                    ),
                )
                for path in source.rglob("*"):
                    if path.is_file():
                        os.utime(path, (epoch + index * 10_000,) * 2)
                result = subprocess.run(
                    [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(output)],
                    cwd=source,
                    env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch), "PYTHONHASHSEED": "0"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                sdist = next(output.glob("*.tar.gz"))
                normalize(sdist, epoch)
                built.append(
                    {
                        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in output.iterdir()
                    }
                )
        self.assertEqual(built[0], built[1])


if __name__ == "__main__":
    unittest.main()
