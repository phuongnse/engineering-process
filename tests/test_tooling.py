import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import urllib.request
from dataclasses import replace

from engineering_process.contracts import (
    ContractError,
    ManagedCommand,
    ManagedTool,
    ManagedToolArtifact,
)
from engineering_process.tooling import (
    _HTTPSOnlyRedirectHandler,
    _download_artifact_direct,
    MARKER_NAME,
    download_artifact,
    install_managed_tool,
    extract_artifact,
    installed_command_bindings,
    installed_commands,
    managed_path_entries,
    platform_identifier,
)


def artifact_for(path: Path, *, checksum: str | None = None) -> ManagedToolArtifact:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return ManagedToolArtifact(
        platform="linux-glibc-x64",
        url="https://downloads.example.test/sample.tar.gz",
        checksum=checksum or f"sha256:{digest}",
        archive_format="tar.gz",
        strip_components=1,
        max_download_bytes=1_000_000,
        max_extracted_bytes=1_000_000,
        max_files=100,
        commands={"sample": ManagedCommand("bin/sample", None)},
    )


def tool_for(artifact: ManagedToolArtifact) -> ManagedTool:
    return ManagedTool(
        identifier="sample",
        version="1.2.3",
        artifacts={artifact.platform: artifact},
    )


class ManagedToolTests(unittest.TestCase):
    def test_redirect_handler_rejects_every_non_https_hop(self):
        handler = _HTTPSOnlyRedirectHandler()
        request = urllib.request.Request("https://downloads.example.test/tool")
        for target in (
            "http://mirror.example.test/tool",
            "ftp://mirror.example.test/tool",
        ):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ContractError, "not a safe HTTPS URL"):
                    handler.redirect_request(
                        request,
                        None,
                        302,
                        "Found",
                        {},
                        target,
                    )

    def test_redirect_handler_rejects_ambiguous_or_fragmented_https_targets(self):
        handler = _HTTPSOnlyRedirectHandler()
        request = urllib.request.Request("https://downloads.example.test/tool")
        for target in (
            "https://downloads.example.test:0/tool",
            "https://downloads.example.test/tool#ignored",
            "https://downloads.example.test/bad path",
            "https://downloads.example.test\\@mirror.example.test/tool",
        ):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ContractError, "redirect"):
                    handler.redirect_request(
                        request,
                        None,
                        302,
                        "Found",
                        {},
                        target,
                    )

    def test_download_reapplies_remaining_deadline_before_every_read(self):
        class Socket:
            def __init__(self):
                self.timeouts = []

            def settimeout(self, value):
                self.timeouts.append(value)

        class Raw:
            def __init__(self, active_socket):
                self._sock = active_socket

        class Stream:
            def __init__(self, active_socket):
                self.raw = Raw(active_socket)

        class Response:
            def __init__(self):
                self.socket = Socket()
                self.fp = Stream(self.socket)
                self.headers = {}
                self.blocks = [b"payload", b""]

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

            def geturl(self):
                return "https://downloads.example.test/sample"

            def read(self, size):
                del size
                return self.blocks.pop(0)

        artifact = ManagedToolArtifact(
            platform="linux-glibc-x64",
            url="https://downloads.example.test/sample",
            checksum=f"sha256:{'0' * 64}",
            archive_format="file",
            strip_components=0,
            max_download_bytes=100,
            max_extracted_bytes=100,
            max_files=1,
            commands={"sample": ManagedCommand("sample", None)},
        )
        response = Response()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact"
            with (
                patch(
                    "engineering_process.tooling._HTTPS_OPENER.open",
                    return_value=response,
                ),
                patch(
                    "engineering_process.tooling._remaining",
                    side_effect=[10.0, 0.25, 0.1],
                ),
            ):
                _download_artifact_direct(artifact, destination, deadline=123.0)

            self.assertEqual(b"payload", destination.read_bytes())
            self.assertEqual([0.25, 0.1], response.socket.timeouts)

    def test_download_does_not_touch_socket_after_content_length_closes_response(self):
        class Socket:
            def __init__(self):
                self.timeouts = []

            def settimeout(self, value):
                self.timeouts.append(value)

        class Raw:
            def __init__(self, active_socket):
                self._sock = active_socket

        class Stream:
            def __init__(self, active_socket):
                self.raw = Raw(active_socket)

        class Response:
            def __init__(self):
                self.socket = Socket()
                self.fp = Stream(self.socket)
                self.headers = {"Content-Length": "7"}

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

            def geturl(self):
                return "https://downloads.example.test/sample"

            def isclosed(self):
                return self.fp is None

            def read(self, size):
                del size
                self.fp = None
                return b"payload"

        artifact = ManagedToolArtifact(
            platform="linux-glibc-x64",
            url="https://downloads.example.test/sample",
            checksum=f"sha256:{'0' * 64}",
            archive_format="file",
            strip_components=0,
            max_download_bytes=100,
            max_extracted_bytes=100,
            max_files=1,
            commands={"sample": ManagedCommand("sample", None)},
        )
        response = Response()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact"
            with (
                patch(
                    "engineering_process.tooling._HTTPS_OPENER.open",
                    return_value=response,
                ),
                patch(
                    "engineering_process.tooling._remaining",
                    side_effect=[10.0, 0.25],
                ),
            ):
                _download_artifact_direct(artifact, destination, deadline=123.0)

            self.assertEqual(b"payload", destination.read_bytes())
            self.assertEqual([0.25], response.socket.timeouts)

    def test_download_worker_is_force_terminated_at_the_wall_clock_deadline(self):
        artifact = ManagedToolArtifact(
            platform="linux-glibc-x64",
            url="https://downloads.example.test/sample",
            checksum=f"sha256:{'0' * 64}",
            archive_format="file",
            strip_components=0,
            max_download_bytes=100,
            max_extracted_bytes=100,
            max_files=1,
            commands={"sample": ManagedCommand("sample", None)},
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "artifact"
            destination.write_bytes(b"partial")
            with (
                patch("engineering_process.tooling._remaining", return_value=0.25),
                patch(
                    "engineering_process.tooling.subprocess.run",
                    side_effect=subprocess.TimeoutExpired("download", 0.25),
                ) as run,
            ):
                with self.assertRaisesRegex(ContractError, "exceeded its timeout"):
                    download_artifact(artifact, destination, deadline=123.0)

            self.assertFalse(destination.exists())
            self.assertEqual(0.25, run.call_args.kwargs["timeout"])

    def write_archive(self, root: Path, *, unsafe: bool = False) -> Path:
        archive = root / "sample.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            content = b"#!/bin/sh\necho sample 1.2.3\n"
            member = tarfile.TarInfo(
                "../escape" if unsafe else "sample-1.2.3/bin/sample"
            )
            member.mode = 0o755
            member.size = len(content)
            handle.addfile(member, io.BytesIO(content))
        return archive

    def test_platform_identifier_is_explicit_about_linux_libc(self):
        self.assertEqual(
            "linux-glibc-x64",
            platform_identifier(system="Linux", machine="x86_64", libc="glibc"),
        )
        self.assertEqual(
            "linux-musl-arm64",
            platform_identifier(system="Linux", machine="aarch64", libc="musl"),
        )

    def test_verified_archive_install_is_owned_atomic_and_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self.write_archive(root)
            artifact = artifact_for(archive)
            tool = tool_for(artifact)
            tools_root = root / "tools"

            def downloader(selected, destination, deadline):
                self.assertEqual(artifact, selected)
                shutil.copyfile(archive, destination)

            first = install_managed_tool(
                tool,
                timeout_seconds=30,
                current_platform=artifact.platform,
                tools_root=tools_root,
                downloader=downloader,
            )
            second = install_managed_tool(
                tool,
                timeout_seconds=30,
                current_platform=artifact.platform,
                tools_root=tools_root,
                downloader=lambda *_: self.fail("installed tool must be reused"),
            )

            self.assertEqual(first, second)
            self.assertTrue(first["sample"].is_file())
            marker_path = first["sample"].parents[1] / MARKER_NAME
            self.assertTrue(marker_path.is_file())
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(1, marker["schemaVersion"])
            self.assertEqual({"sample": "bin/sample"}, marker["commands"])
            self.assertEqual(
                (first["sample"].parent,),
                managed_path_entries(
                    [tool],
                    current_platform=artifact.platform,
                    tools_root=tools_root,
                ),
            )

    def test_script_binding_is_contained_and_uses_marker_schema_two(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "script-tool.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                for name, content, mode in (
                    ("sample-1.2.3/bin/runtime", b"runtime", 0o755),
                    ("sample-1.2.3/lib/cli.py", b"print('sample')", 0o644),
                ):
                    member = tarfile.TarInfo(name)
                    member.mode = mode
                    member.size = len(content)
                    handle.addfile(member, io.BytesIO(content))
            artifact = artifact_for(archive)
            artifact = replace(
                artifact,
                commands={
                    "sample": ManagedCommand("bin/runtime", "lib/cli.py")
                },
            )
            tool = tool_for(artifact)

            install_managed_tool(
                tool,
                timeout_seconds=30,
                current_platform=artifact.platform,
                tools_root=root / "tools",
                downloader=lambda selected, destination, deadline: shutil.copyfile(
                    archive, destination
                ),
            )
            binding = installed_command_bindings(
                tool,
                current_platform=artifact.platform,
                tools_root=root / "tools",
            )["sample"]

            self.assertEqual("runtime", binding.application.name)
            self.assertEqual("cli.py", Path(binding.prefix_arguments[0]).name)
            marker = json.loads(
                (binding.application.parents[1] / MARKER_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(2, marker["schemaVersion"])

    def test_checksum_failure_never_publishes_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self.write_archive(root)
            artifact = artifact_for(archive, checksum=f"sha256:{'0' * 64}")
            tool = tool_for(artifact)
            tools_root = root / "tools"

            with self.assertRaisesRegex(ContractError, "checksum mismatch"):
                install_managed_tool(
                    tool,
                    timeout_seconds=30,
                    current_platform=artifact.platform,
                    tools_root=tools_root,
                    downloader=lambda selected, destination, deadline: shutil.copyfile(
                        archive, destination
                    ),
                )

            self.assertEqual(
                {},
                installed_commands(
                    tool,
                    current_platform=artifact.platform,
                    tools_root=tools_root,
                ),
            )

    def test_archive_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = self.write_archive(root, unsafe=True)
            artifact = artifact_for(archive)
            tool = tool_for(artifact)

            with self.assertRaisesRegex(ContractError, "unsafe archive member"):
                install_managed_tool(
                    tool,
                    timeout_seconds=30,
                    current_platform=artifact.platform,
                    tools_root=root / "tools",
                    downloader=lambda selected, destination, deadline: shutil.copyfile(
                        archive, destination
                    ),
                )
            self.assertFalse((root / "escape").exists())

    def test_zip_extraction_checks_deadline_between_output_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sample.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("sample/bin/sample", b"x" * 2_500_000)
            artifact = ManagedToolArtifact(
                platform="linux-glibc-x64",
                url="https://downloads.example.test/sample.zip",
                checksum=f"sha256:{'0' * 64}",
                archive_format="zip",
                strip_components=1,
                max_download_bytes=1_000_000,
                max_extracted_bytes=3_000_000,
                max_files=10,
                commands={"sample": ManagedCommand("bin/sample", None)},
            )

            with patch(
                "engineering_process.tooling._remaining",
                side_effect=[1.0, 1.0, 1.0, ContractError("deadline reached")],
            ):
                with self.assertRaisesRegex(ContractError, "deadline reached"):
                    extract_artifact(
                        archive,
                        root / "payload",
                        artifact,
                        deadline=1.0,
                    )

    def test_zip_member_limit_is_enforced_before_zipfile_materializes_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sample.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("sample/one", b"one")
                handle.writestr("sample/two", b"two")
            artifact = ManagedToolArtifact(
                platform="linux-glibc-x64",
                url="https://downloads.example.test/sample.zip",
                checksum=f"sha256:{'0' * 64}",
                archive_format="zip",
                strip_components=1,
                max_download_bytes=1_000_000,
                max_extracted_bytes=1_000_000,
                max_files=1,
                commands={"sample": ManagedCommand("sample", None)},
            )

            with patch(
                "engineering_process.tooling.zipfile.ZipFile",
                side_effect=AssertionError("ZipFile must not be constructed"),
            ):
                with self.assertRaisesRegex(ContractError, "maxFiles"):
                    extract_artifact(
                        archive,
                        root / "payload",
                        artifact,
                        deadline=time.monotonic() + 30,
                    )

    @unittest.skipIf(os.name == "nt", "POSIX archive mode regression")
    def test_zip_directory_modes_are_applied_after_child_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sample.zip"
            directory_member = zipfile.ZipInfo("sample/bin/")
            directory_member.external_attr = (stat.S_IFDIR | 0o555) << 16
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr(directory_member, b"")
                handle.writestr("sample/bin/sample", b"sample")
            artifact = ManagedToolArtifact(
                platform="linux-glibc-x64",
                url="https://downloads.example.test/sample.zip",
                checksum=f"sha256:{'0' * 64}",
                archive_format="zip",
                strip_components=1,
                max_download_bytes=1_000_000,
                max_extracted_bytes=1_000_000,
                max_files=10,
                commands={"sample": ManagedCommand("bin/sample", None)},
            )
            payload = root / "payload"

            try:
                extract_artifact(
                    archive,
                    payload,
                    artifact,
                    deadline=time.monotonic() + 30,
                )
                self.assertEqual(
                    b"sample", (payload / "sample" / "bin" / "sample").read_bytes()
                )
            finally:
                extracted_directory = payload / "sample" / "bin"
                if extracted_directory.is_dir():
                    extracted_directory.chmod(0o755)

    def test_file_install_checks_deadline_between_copy_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample"
            source.write_bytes(b"x" * 2_500_000)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            artifact = ManagedToolArtifact(
                platform="linux-glibc-x64",
                url="https://downloads.example.test/sample",
                checksum=f"sha256:{digest}",
                archive_format="file",
                strip_components=0,
                max_download_bytes=3_000_000,
                max_extracted_bytes=3_000_000,
                max_files=1,
                commands={"sample": ManagedCommand("sample", None)},
            )
            calls = 0

            def bounded_remaining(deadline):
                nonlocal calls
                calls += 1
                if calls == 5:
                    raise ContractError("deadline reached")
                return 1.0

            with patch(
                "engineering_process.tooling._remaining",
                side_effect=bounded_remaining,
            ):
                with self.assertRaisesRegex(ContractError, "deadline reached"):
                    install_managed_tool(
                        tool_for(artifact),
                        timeout_seconds=30,
                        current_platform=artifact.platform,
                        tools_root=root / "tools",
                        downloader=lambda selected, destination, deadline: shutil.copyfile(
                            source, destination
                        ),
                    )
            self.assertEqual(
                {},
                installed_commands(
                    tool_for(artifact),
                    current_platform=artifact.platform,
                    tools_root=root / "tools",
                ),
            )


if __name__ == "__main__":
    unittest.main()
