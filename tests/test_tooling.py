import hashlib
import io
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import urllib.request

from engineering_process.contracts import ContractError, ManagedTool, ManagedToolArtifact
from engineering_process.tooling import (
    _HTTPSOnlyRedirectHandler,
    MARKER_NAME,
    install_managed_tool,
    extract_artifact,
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
        commands={"sample": "bin/sample"},
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
            self.assertTrue((first["sample"].parents[1] / MARKER_NAME).is_file())
            self.assertEqual(
                (first["sample"].parent,),
                managed_path_entries(
                    [tool],
                    current_platform=artifact.platform,
                    tools_root=tools_root,
                ),
            )

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
                commands={"sample": "bin/sample"},
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
                commands={"sample": "sample"},
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
