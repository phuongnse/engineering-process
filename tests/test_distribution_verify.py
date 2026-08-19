import tempfile
import unittest
from pathlib import Path

from engineering_process.contracts import ContractError
from engineering_process.distribution_verify import (
    _validate_archive_members,
    verify_distribution,
)


class DistributionVerificationTests(unittest.TestCase):
    def test_archive_contract_requires_release_and_production_assets(self):
        wheel = Path("engineering_process-0.1.1-py3-none-any.whl")
        members = [
            "engineering_process-0.1.1.data/data/share/engineering-process/PRODUCTION_STANDARD.md",
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

    def test_verified_outputs_cannot_be_written_into_the_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ContractError, "outside the checkout"):
                verify_distribution(root, output_root=root / "dist")


if __name__ == "__main__":
    unittest.main()
