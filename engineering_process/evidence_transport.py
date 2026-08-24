from __future__ import annotations

import base64
import binascii
import gzip
from pathlib import Path
import zlib

from .contracts import ContractError
from .evidence import (
    MAX_RECEIPT_BYTES,
    validate_bootstrap_authorization,
    validate_receipt,
)


MAX_ENCODED_COMPLETION_EVIDENCE_BYTES = 60_000
COMPLETION_EVIDENCE_KINDS = ("receipt", "bootstrap-authorization")


def _validate(path: Path, kind: str) -> dict[str, object]:
    if kind == "receipt":
        return validate_receipt(path)
    if kind == "bootstrap-authorization":
        return validate_bootstrap_authorization(path)
    raise ContractError(f"unsupported completion evidence kind: {kind}")


def encode_completion_evidence(
    evidence: Path,
    output: Path,
    *,
    kind: str,
) -> dict[str, object]:
    details = _validate(evidence, kind)
    try:
        content = evidence.read_bytes()
    except OSError as error:
        raise ContractError(f"cannot read completion evidence: {error}") from error
    encoded = base64.b64encode(gzip.compress(content, mtime=0))
    if len(encoded) > MAX_ENCODED_COMPLETION_EVIDENCE_BYTES:
        raise ContractError(
            "encoded completion evidence exceeds the publication transport limit: "
            f"{len(encoded)} > {MAX_ENCODED_COMPLETION_EVIDENCE_BYTES}"
        )
    if output.exists():
        raise ContractError(f"{output}: refusing to replace encoded evidence")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(encoded)
    except OSError as error:
        raise ContractError(f"cannot write encoded completion evidence: {error}") from error
    return {
        **details,
        "evidenceKind": kind,
        "encodedBytes": len(encoded),
        "output": str(output.resolve()),
    }


def decode_completion_evidence(source: Path, output: Path) -> int:
    try:
        encoded = source.read_bytes()
    except OSError as error:
        raise ContractError(f"cannot read encoded completion evidence: {error}") from error
    if not encoded or len(encoded) > MAX_ENCODED_COMPLETION_EVIDENCE_BYTES:
        raise ContractError(
            "encoded completion evidence must contain between 1 and "
            f"{MAX_ENCODED_COMPLETION_EVIDENCE_BYTES} bytes"
        )
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ContractError("completion evidence is not canonical base64") from error
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoded = decoder.decompress(compressed, MAX_RECEIPT_BYTES + 1)
        decoded += decoder.flush()
    except zlib.error as error:
        raise ContractError("completion evidence is not a valid gzip stream") from error
    if (
        not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
        or len(decoded) > MAX_RECEIPT_BYTES
    ):
        raise ContractError("completion evidence exceeds or violates its gzip boundary")
    if output.exists():
        raise ContractError(f"{output}: refusing to replace completion evidence")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(decoded)
    except OSError as error:
        raise ContractError(f"cannot write decoded completion evidence: {error}") from error
    return len(decoded)
