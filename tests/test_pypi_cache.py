from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

from tests.test_adoption import load_managed_adopter


def fixture_wheel(version: str) -> bytes:
    metadata = f"engineering_process-{version}.dist-info"
    files = {
        "engineering_process/__init__.py": f'VERSION = "{version}"\n'.encode(),
        f"{metadata}/METADATA": (
            f"Metadata-Version: 2.1\nName: engineering-process\nVersion: {version}\n"
        ).encode(),
        f"{metadata}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: cache-regression\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ).encode(),
    }
    records = []
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
        records.append(f"{name},sha256={digest.decode()},{len(content)}\n")
    files[f"{metadata}/RECORD"] = (
        "".join(records) + f"{metadata}/RECORD,,\n"
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


class PipCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.adopter = load_managed_adopter("cache_test_adopter")
        self.adopter.COMMAND_TIMEOUT_SECONDS = 45

    def test_refresh_uses_supported_cli_option_and_preserves_old_pip(self) -> None:
        for help_text, expected in (
            ("  --refresh-package <refresh_package>\n", ["--refresh-package", "engineering-process"]),
            ("  --no-cache-dir  Disable the cache.\n", ["--no-cache-dir"]),
        ):
            with self.subTest(help_text=help_text), mock.patch.object(
                self.adopter, "_run", return_value=help_text
            ) as run:
                self.assertEqual(
                    expected,
                    self.adopter._pip_refresh_arguments(Path(sys.executable), cwd=self.root),
                )
                run.assert_called_once_with(
                    [sys.executable, "-I", "-m", "pip", "install", "--help"], cwd=self.root
                )

    def test_refresh_probe_failure_is_not_retried_or_silenced(self) -> None:
        with mock.patch.object(self.adopter, "_run", side_effect=RuntimeError("timeout")) as run:
            with self.assertRaisesRegex(RuntimeError, "timeout"):
                self.adopter._pip_refresh_arguments(Path(sys.executable), cwd=self.root)
            run.assert_called_once()

    def test_real_warm_cache_refresh_preserves_exact_version_and_hash(self) -> None:
        wheels = {
            f"engineering_process-{version}-py3-none-any.whl": fixture_wheel(version)
            for version in ("0.0.1", "0.0.2")
        }
        visible = ["0.0.1"]
        index_requests = []

        class Index(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/simple/engineering-process/":
                    index_requests.append(self.headers.get("Cache-Control"))
                    body = json.dumps({
                        "meta": {"api-version": "1.0"},
                        "name": "engineering-process",
                        "files": [
                            {
                                "filename": name,
                                "url": f"/files/{name}",
                                "hashes": {"sha256": hashlib.sha256(content).hexdigest()},
                            }
                            for name, content in wheels.items()
                            if any(f"-{version}-" in name for version in visible)
                        ],
                    }).encode()
                    content_type = "application/vnd.pypi.simple.v1+json"
                elif self.path.startswith("/files/") and self.path[7:] in wheels:
                    body = wheels[self.path[7:]]
                    content_type = "application/octet-stream"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Index)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.addCleanup(stop_server)
        requirements = self.root / "requirements.txt"
        report = self.root / "report.json"
        command = [
            sys.executable, "-I", "-m", "pip", "install", "--isolated",
            "--disable-pip-version-check", "--no-input", "--require-hashes",
            "--only-binary", ":all:", "--ignore-installed", "--dry-run",
            "--report", str(report), "--cache-dir", str(self.root / "cache"),
            "--index-url", f"http://127.0.0.1:{server.server_port}/simple/",
            "--trusted-host", "127.0.0.1", "--retries", "0", "--timeout", "5",
            "-r", str(requirements),
        ]

        def pin(version: str, digest: str | None = None) -> None:
            if digest is None:
                digest = hashlib.sha256(
                    wheels[f"engineering_process-{version}-py3-none-any.whl"]
                ).hexdigest()
            requirements.write_text(
                f"engineering-process=={version} --hash=sha256:{digest}\n", encoding="utf-8"
            )
            report.unlink(missing_ok=True)

        refresh = self.adopter._pip_refresh_arguments(Path(sys.executable), cwd=self.root)
        pin("0.0.1")
        self.adopter._run(command, cwd=self.root)
        self.assertEqual(1, len(index_requests))
        visible.append("0.0.2")
        pin("0.0.2")
        if "--refresh-package" in refresh:
            with self.assertRaisesRegex(RuntimeError, "exit status 1"):
                self.adopter._run(command, cwd=self.root)
            self.assertEqual(1, len(index_requests), "control must reuse the unexpired index")
            self.assertFalse(report.exists())
            self.adopter._run(command + ["--no-cache-dir"], cwd=self.root)
            fallback = json.loads(report.read_text(encoding="utf-8"))["install"]
            self.assertEqual("0.0.2", fallback[0]["metadata"]["version"])

        self.adopter._run(command + refresh, cwd=self.root)
        installed = json.loads(report.read_text(encoding="utf-8"))["install"]
        self.assertEqual(["0.0.2"], [item["metadata"]["version"] for item in installed])
        self.assertEqual(
            hashlib.sha256(wheels["engineering_process-0.0.2-py3-none-any.whl"]).hexdigest(),
            installed[0]["download_info"]["archive_info"]["hashes"]["sha256"],
        )
        if "--refresh-package" in refresh:
            self.assertEqual("max-age=0", index_requests[-1])

        for version in ("0.0.2", "0.0.3"):
            with self.subTest(rejected_version=version):
                pin(version, "0" * 64)
                with self.assertRaisesRegex(RuntimeError, "exit status 1"):
                    self.adopter._run(command + refresh, cwd=self.root)
                self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
