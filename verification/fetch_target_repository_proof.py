from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import ssl
import sys
from typing import Any
from urllib.parse import quote
from urllib.error import URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engineering_process.contracts import ContractError, read_json
from verification.build_target_repository_proof import _target, build


MAX_RESPONSE_BYTES = 2_000_000
REQUEST_TIMEOUT_SECONDS = 15


def _fetch(url: str) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "engineering-process-authority-transition",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers, method="GET")
    context = ssl.create_default_context()
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS, context=context) as response:
        if response.geturl() != url or response.status != 200:
            raise ContractError("GitHub repository proof endpoint redirected or failed")
        content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise ContractError("GitHub repository proof response exceeds 2000000 bytes")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContractError("GitHub repository proof response is not UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ContractError("GitHub repository proof response must be an object")
    return value


def fetch(identity: dict[str, Any]) -> dict[str, Any]:
    target = _target(identity)
    repository = target["repository"]
    tag = target["tag"]
    root = f"https://api.github.com/repos/{repository}"
    repository_document = _fetch(root)
    release_document = _fetch(f"{root}/releases/tags/{quote(tag, safe='')}")
    tag_ref = _fetch(f"{root}/git/ref/tags/{quote(tag, safe='')}")
    ref_object = tag_ref.get("object")
    tag_object = None
    if isinstance(ref_object, dict) and ref_object.get("type") == "tag":
        sha = ref_object.get("sha")
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            raise ContractError("GitHub annotated tag id is invalid")
        tag_object = _fetch(f"{root}/git/tags/{sha}")
    return build(
        identity,
        repository_document,
        release_document,
        tag_ref,
        tag_object,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch provider-authenticated target repository proof"
    )
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if re.fullmatch(r"[0-9a-f]{64}", args.nonce) is None:
        raise ContractError("repository proof nonce must be 32 random bytes")
    proof = fetch(read_json(args.identity))
    if os.path.lexists(args.output):
        raise ContractError(f"{args.output}: refusing to replace repository proof envelope")
    envelope = {
        "schemaVersion": 1,
        "kind": "engineering-process-target-repository-proof-envelope",
        "nonce": args.nonce,
        "proof": proof,
    }
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(envelope, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(json.dumps({"nonce": args.nonce, "status": "passed"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ContractError, OSError, TimeoutError, URLError) as error:
        print(f"authenticated target repository proof failed: {error}", file=sys.stderr)
        raise SystemExit(2) from error
