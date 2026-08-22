import json
import unittest
from urllib.error import HTTPError

from engineering_process.contracts import ContractError
from verification.check_pypi_publication import inspect_pypi_publication


PACKAGE = "engineering-process"
VERSION = "0.2.0"
EXPECTED = {
    "engineering_process-0.2.0-py3-none-any.whl": (3, "a" * 64),
    "engineering_process-0.2.0.tar.gz": (4, "b" * 64),
}


class Response:
    def __init__(self, document: object):
        self.content = json.dumps(document).encode("utf-8")
        self.headers = {"Content-Length": str(len(self.content))}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_arguments):
        return False

    def read(self, limit: int) -> bytes:
        return self.content[:limit]


def document(files: dict[str, tuple[int, str]] = EXPECTED) -> dict[str, object]:
    return {
        "info": {"name": PACKAGE, "version": VERSION},
        "urls": [
            {
                "digests": {"sha256": digest},
                "filename": name,
                "size": size,
            }
            for name, (size, digest) in files.items()
        ],
    }


def simple_document(
    files: dict[str, tuple[int, str]] = EXPECTED,
) -> dict[str, object]:
    return {
        "files": [
            {"filename": name, "hashes": {"sha256": digest}}
            for name, (_size, digest) in files.items()
        ],
        "meta": {"api-version": "1.4"},
        "name": PACKAGE,
    }


def exact_opener(
    files: dict[str, tuple[int, str]] = EXPECTED,
    simple_files: dict[str, tuple[int, str]] = EXPECTED,
):
    def open_request(request, **_kwargs):
        if "/simple/" in request.full_url:
            return Response(simple_document(simple_files))
        return Response(document(files))

    return open_request


class PyPIPublicationTests(unittest.TestCase):
    def test_missing_version_requires_publication(self):
        def missing(request, *, timeout):
            self.assertEqual(15, timeout)
            self.assertEqual(
                "https://pypi.org/pypi/engineering-process/0.2.0/json",
                request.full_url,
            )
            raise HTTPError(request.full_url, 404, "missing", {}, None)

        result = inspect_pypi_publication(
            PACKAGE,
            VERSION,
            EXPECTED,
            opener=missing,
        )

        self.assertEqual("missing", result["status"])
        self.assertTrue(result["publishRequired"])

    def test_exact_version_is_idempotently_accepted(self):
        result = inspect_pypi_publication(
            PACKAGE,
            VERSION,
            EXPECTED,
            opener=exact_opener(),
        )

        self.assertEqual("published", result["status"])
        self.assertFalse(result["publishRequired"])
        self.assertEqual(sorted(EXPECTED), [item["name"] for item in result["files"]])

    def test_partial_version_fails_closed(self):
        partial = dict(EXPECTED)
        partial.pop("engineering_process-0.2.0.tar.gz")

        with self.assertRaisesRegex(ContractError, "conflicts"):
            inspect_pypi_publication(
                PACKAGE,
                VERSION,
                EXPECTED,
                opener=exact_opener(partial),
            )

    def test_conflicting_hash_fails_closed(self):
        conflicting = dict(EXPECTED)
        conflicting["engineering_process-0.2.0.tar.gz"] = (4, "c" * 64)

        with self.assertRaisesRegex(ContractError, "conflicts"):
            inspect_pypi_publication(
                PACKAGE,
                VERSION,
                EXPECTED,
                opener=exact_opener(conflicting),
            )

    def test_version_json_waits_for_simple_api_propagation(self):
        partial = dict(EXPECTED)
        partial.pop("engineering_process-0.2.0.tar.gz")

        result = inspect_pypi_publication(
            PACKAGE,
            VERSION,
            EXPECTED,
            opener=exact_opener(simple_files=partial),
        )

        self.assertEqual("propagating", result["status"])
        self.assertFalse(result["publishRequired"])

    def test_oversized_response_is_rejected_before_read(self):
        response = Response(document())
        response.headers["Content-Length"] = "2000001"

        with self.assertRaisesRegex(ContractError, "size limit"):
            inspect_pypi_publication(
                PACKAGE,
                VERSION,
                EXPECTED,
                opener=lambda *_args, **_kwargs: response,
            )


if __name__ == "__main__":
    unittest.main()
