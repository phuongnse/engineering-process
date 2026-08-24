from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError
from engineering_process.evidence_transport import decode_completion_evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        size = decode_completion_evidence(arguments.input, arguments.output)
    except ContractError as error:
        parser.error(str(error))
    print(f"decoded completion evidence: {size} bytes")
