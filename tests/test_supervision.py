import os
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess

from engineering_process._supervisor_posix import PosixProcessSupervisor
from engineering_process._supervisor_windows import resolve_windows_application
from engineering_process.helper_launch import isolated_helper_command
from engineering_process.supervision import process_supervisor


class ProcessSupervisionTests(unittest.TestCase):
    def test_platform_selection_is_confined_to_the_supervision_boundary(self):
        self.assertEqual(
            "PosixProcessSupervisor",
            type(process_supervisor(platform_name="posix")).__name__,
        )
        self.assertEqual(
            "WindowsProcessSupervisor",
            type(process_supervisor(platform_name="nt")).__name__,
        )
        with self.assertRaisesRegex(OSError, "unsupported process supervision"):
            process_supervisor(platform_name="unsupported")

    def test_posix_backend_resolves_the_exact_application(self):
        supervisor = PosixProcessSupervisor()
        application = supervisor.resolve_application(
            sys.executable,
            working_directory=Path.cwd(),
            environment=os.environ,
        )
        self.assertEqual(Path(sys.executable).resolve(), application)

    @unittest.skipUnless(os.name == "posix", "POSIX executable permission semantics")
    def test_posix_relative_path_entries_are_resolved_from_command_working_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            working = root / "work"
            command = working / "bin" / "sample"
            command.parent.mkdir(parents=True)
            command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)

            application = PosixProcessSupervisor().resolve_application(
                "sample",
                working_directory=working,
                environment={"PATH": "bin"},
            )

            self.assertEqual(command.resolve(), application)

    def test_windows_search_excludes_checkout_and_prefers_injected_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "checkout"
            managed = root / "managed"
            system = root / "system"
            checkout.mkdir()
            managed.mkdir()
            system.mkdir()
            (checkout / "sample.exe").write_bytes(b"checkout")
            selected = managed / "sample.exe"
            selected.write_bytes(b"managed")
            (system / "sample.exe").write_bytes(b"system")

            application = resolve_windows_application(
                "sample",
                working_directory=checkout,
                environment={"PATH": os.pathsep.join((str(managed), str(system)))},
                path_separator=os.pathsep,
            )

            self.assertEqual(selected.resolve(), application)

    def test_windows_explicit_project_executable_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / "tools" / "sample.exe"
            command.parent.mkdir()
            command.write_bytes(b"native")

            application = resolve_windows_application(
                "tools/sample.exe",
                working_directory=root,
                environment={"PATH": ""},
                path_separator=os.pathsep,
            )

            self.assertEqual(command.resolve(), application)

    def test_windows_batch_commands_fail_closed_without_a_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(OSError, "requires a shell"):
                resolve_windows_application(
                    "npm.cmd",
                    working_directory=Path(directory),
                    environment={"PATH": directory},
                    path_separator=os.pathsep,
                )

    def test_private_helper_import_cannot_be_shadowed_by_checkout_or_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shadow_root = root / "shadow"
            package = shadow_root / "engineering_process"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            marker = root / "shadowed"
            fake_helper = (
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('shadowed')\n"
            )
            checkout_package = root / "engineering_process"
            checkout_package.mkdir()
            (checkout_package / "__init__.py").write_text("", encoding="utf-8")
            for helper_name in ("_download_worker.py", "_windows_job.py"):
                (package / helper_name).write_text(fake_helper, encoding="utf-8")
                (checkout_package / helper_name).write_text(
                    fake_helper, encoding="utf-8"
                )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(shadow_root)

            for module in (
                "engineering_process._download_worker",
                "engineering_process._windows_job",
            ):
                with self.subTest(module=module):
                    result = subprocess.run(
                        isolated_helper_command(module),
                        cwd=root,
                        env=environment,
                        check=False,
                        input=b"{}",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        timeout=10,
                    )
                    self.assertEqual(125, result.returncode)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
