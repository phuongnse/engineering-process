import hashlib
import unittest

from engineering_process.diagnostics import classify_diagnostics


class DiagnosticClassificationTests(unittest.TestCase):
    def test_classifies_portable_warning_and_error_conventions(self):
        cases = (
            (" WARN: regex validation may be inaccurate\n", "warning"),
            ("npm warn rebuild script policy is incomplete\n", "warning"),
            ("module.py:12: DeprecationWarning: old path\n", "warning"),
            ("source.c:7:3: warning: unused value\n", "warning"),
            ("##[warning] runner degraded\n", "warning"),
            ('{"level":"warn","message":"degraded"}\n', "warning"),
            ("ERROR: validation engine unavailable\n", "error"),
            ("ValidationError: invalid evidence\n", "error"),
            ("source.c:8:4: error: invalid value\n", "error"),
            ("[error] action failed closed\n", "error"),
            ('{"severity":"error","message":"invalid"}\n', "error"),
        )
        for output, severity in cases:
            with self.subTest(output=output):
                report = classify_diagnostics(
                    stdout=output.encode("utf-8"), stderr=b""
                )

                self.assertEqual("failed", report["status"])
                self.assertEqual(1, report["count"])
                self.assertEqual(severity, report["matches"][0]["severity"])
                self.assertEqual("stdout", report["matches"][0]["stream"])
                self.assertEqual(1, report["matches"][0]["line"])
                self.assertEqual(
                    hashlib.sha256(output.rstrip("\n").encode("utf-8")).hexdigest(),
                    report["matches"][0]["lineSha256"],
                )

    def test_benign_prose_and_success_output_remain_clean(self):
        report = classify_diagnostics(
            stdout=(
                b"verified error handling and warning recovery\n"
                b"all warning fixtures were asserted inside the test process\n"
                b"INFO: validation completed successfully\n"
            ),
            stderr=b"",
        )

        self.assertEqual(
            {
                "policy": "forbid-warning-error",
                "status": "clean",
                "count": 0,
                "matches": [],
                "matchesTruncated": False,
            },
            report,
        )

    def test_records_stream_and_redacted_digest_without_raw_text(self):
        report = classify_diagnostics(
            stdout=b"", stderr=b"WARNING: secret-shaped=value\n"
        )

        self.assertEqual("stderr", report["matches"][0]["stream"])
        self.assertNotIn("secret-shaped", str(report))


if __name__ == "__main__":
    unittest.main()
