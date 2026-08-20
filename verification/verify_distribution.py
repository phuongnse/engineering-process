from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from engineering_process.distribution_verify import verify_distribution


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--attestation", type=Path)
    arguments = parser.parse_args()
    print(
        json.dumps(
            verify_distribution(
                PROJECT_ROOT,
                output_root=arguments.output,
                receipt_path=arguments.receipt,
                attestation_path=arguments.attestation,
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
