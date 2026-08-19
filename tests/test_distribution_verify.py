import tempfile
import tarfile
import subprocess
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from engineering_process.contracts import ContractError
from engineering_process.distribution_verify import (
    _tracked_paths,
    _validate_archive_members,
    _validate_tar_archive,
    _validate_zip_archive,
    verify_distribution,
)


class DistributionVerificationTests(unittest.TestCase):
    def test_tracked_distribution_paths_reject_windows_hostile_names(self):
        for name in ("AUX.txt", "trailing. ", "control\x01.txt"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                subprocess.run(["git", "init", "-q"], cwd=root, check=True)
                (root / name).write_text("hostile\n", encoding="utf-8")
                subprocess.run(["git", "add", "--", name], cwd=root, check=True)
                with self.assertRaisesRegex(ContractError, "non-portable path"):
                    _tracked_paths(root)

    def test_archive_contract_requires_release_and_production_assets(self):
        wheel = Path("engineering_process-0.1.1-py3-none-any.whl")
        members = [
            "engineering_process-0.1.1.data/data/share/engineering-process/PRODUCTION_STANDARD.md",
            "engineering_process/requirements-release.txt",
            "engineering_process-0.1.1.data/data/share/engineering-process/release.json",
            "engineering_process-0.1.1.data/data/share/engineering-process/schemas/change.schema.json",
            "engineering_process-0.1.1.data/data/share/engineering-process/schemas/evidence-receipt.schema.json",
            "engineering_process-0.1.1.data/data/share/engineering-process/schemas/release.schema.json",
        ]

        _validate_archive_members(wheel, members)

        with self.assertRaisesRegex(ContractError, "missing required distribution assets"):
            _validate_archive_members(wheel, members[:-1])

    def test_archive_contract_rejects_managed_or_generated_state(self):
        wheel = Path("engineering_process-0.1.1-py3-none-any.whl")
        with self.assertRaisesRegex(ContractError, "forbidden generated or managed"):
            _validate_archive_members(wheel, [".process/runs/change/state.json"])

    def test_archive_contract_rejects_windows_hostile_member_names(self):
        required = [
            "PRODUCTION_STANDARD.md",
            "engineering_process/requirements-release.txt",
            "release.json",
            "schemas/change.schema.json",
            "schemas/evidence-receipt.schema.json",
            "schemas/release.schema.json",
        ]
        cases = (
            (
                Path("engineering_process-0.1.1-py3-none-any.whl"),
                required,
                "CON.txt",
            ),
            (
                Path("engineering_process-0.1.1-py3-none-any.whl"),
                required,
                "package/trailing.",
            ),
            (
                Path("engineering_process-0.1.1.tar.gz"),
                [f"engineering_process-0.1.1/{name}" for name in required],
                "engineering_process-0.1.1/CON.txt",
            ),
            (
                Path("engineering_process-0.1.1.tar.gz"),
                [f"engineering_process-0.1.1/{name}" for name in required],
                "engineering_process-0.1.1/package/trailing.",
            ),
        )
        for archive, members, hostile in cases:
            with self.subTest(archive=archive, hostile=hostile), self.assertRaisesRegex(
                ContractError, "forbidden generated or managed"
            ):
                _validate_archive_members(archive, [*members, hostile])

    def test_wheel_expansion_and_duplicate_names_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "engineering_process-0.1.1-py3-none-any.whl"
            required = [
                "PRODUCTION_STANDARD.md",
                "engineering_process/requirements-release.txt",
                "release.json",
                "schemas/change.schema.json",
                "schemas/evidence-receipt.schema.json",
                "schemas/release.schema.json",
            ]
            with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name in required:
                    archive.writestr(name, b"ok")
                archive.writestr("payload.bin", b"x" * 101)
            with (
                patch(
                    "engineering_process.distribution_verify.MAX_ARCHIVE_MEMBER_BYTES",
                    100,
                ),
                self.assertRaisesRegex(ContractError, "expanded bytes"),
            ):
                _validate_zip_archive(wheel)

            duplicate = Path(directory) / "duplicate.whl"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(duplicate, "w") as archive:
                    for name in required:
                        archive.writestr(name, b"ok")
                    archive.writestr("duplicate.txt", b"one")
                    archive.writestr("duplicate.txt", b"two")
            with self.assertRaisesRegex(ContractError, "duplicate member"):
                _validate_zip_archive(duplicate)

    def test_sdist_special_members_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            sdist = Path(directory) / "engineering_process-0.1.1.tar.gz"
            root = "engineering_process-0.1.1"
            with tarfile.open(sdist, "w:gz") as archive:
                fifo = tarfile.TarInfo(f"{root}/unexpected.fifo")
                fifo.type = tarfile.FIFOTYPE
                archive.addfile(fifo)
            with self.assertRaisesRegex(ContractError, "non-regular member"):
                _validate_tar_archive(sdist)

    def test_verified_outputs_cannot_be_written_into_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ContractError, "outside the checkout"):
                verify_distribution(root, output_root=root / "dist")


if __name__ == "__main__":
    unittest.main()
