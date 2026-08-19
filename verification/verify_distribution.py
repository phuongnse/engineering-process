from __future__ import annotations

import argparse
import json
from pathlib import Path

from engineering_process.distribution_verify import verify_distribution


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    print(
        json.dumps(
            verify_distribution(
                Path(__file__).resolve().parent.parent,
                output_root=arguments.output,
            ),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
