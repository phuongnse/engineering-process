import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from engineering_process import _windows_job
from engineering_process.supervision import (
    WINDOWS_NATURAL_DRAIN_GRACE_MILLISECONDS,
)


class NativeCall:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments):
        return self.callback(*arguments)


class FakeKernel32:
    def __init__(self):
        self.active_processes = [0]
        self.wait_results = [_windows_job.WAIT_OBJECT_0]
        self.terminate_result = True
        self.close_failure = None
        self.closed_handles = []
        self.deleted_attribute_lists = 0
        self.termination_calls = 0
        self.creation_flags = None
        self.job_attribute = None
        self.handle_attribute = None
        self.updated_attributes = []
        self.attribute_counts = []
        self.application = None
        self.command_line = None

        self.CreateJobObjectW = NativeCall(lambda *_: 101)
        self.SetInformationJobObject = NativeCall(lambda *_: True)
        self.InitializeProcThreadAttributeList = NativeCall(
            self._initialize_attribute_list
        )
        self.UpdateProcThreadAttribute = NativeCall(self._update_attribute)
        self.DeleteProcThreadAttributeList = NativeCall(self._delete_attribute_list)
        self.CreateProcessW = NativeCall(self._create_process)
        self.WaitForSingleObject = NativeCall(self._wait)
        self.GetExitCodeProcess = NativeCall(self._get_exit_code)
        self.QueryInformationJobObject = NativeCall(self._query_job)
        self.TerminateJobObject = NativeCall(self._terminate_job)
        self.GetStdHandle = NativeCall(lambda value: value)
        self.CloseHandle = NativeCall(self._close_handle)

    def _initialize_attribute_list(self, attribute_list, count, flags, size_pointer):
        del flags
        self.attribute_counts.append(count)
        size = ctypes.cast(size_pointer, ctypes.POINTER(ctypes.c_size_t))
        if not attribute_list:
            size.contents.value = 64
            return False
        return True

    def _update_attribute(
        self,
        attribute_list,
        flags,
        attribute,
        value,
        value_size,
        previous,
        return_size,
    ):
        del attribute_list, flags, value_size, previous, return_size
        self.updated_attributes.append(attribute)
        if attribute == _windows_job.PROC_THREAD_ATTRIBUTE_JOB_LIST:
            self.job_attribute = ctypes.cast(
                value, ctypes.POINTER(wintypes.HANDLE)
            )[0]
        elif attribute == _windows_job.PROC_THREAD_ATTRIBUTE_HANDLE_LIST:
            handles = ctypes.cast(
                value, ctypes.POINTER(wintypes.HANDLE * 3)
            ).contents
            self.handle_attribute = list(handles)
        else:
            return False
        return True

    def _delete_attribute_list(self, attribute_list):
        del attribute_list
        self.deleted_attribute_lists += 1

    def _create_process(
        self,
        application,
        command_line,
        process_attributes,
        thread_attributes,
        inherit_handles,
        creation_flags,
        environment,
        current_directory,
        startup_pointer,
        process_info_pointer,
    ):
        self.application = application
        self.command_line = command_line.value
        del (
            process_attributes,
            thread_attributes,
            environment,
            current_directory,
        )
        self.creation_flags = creation_flags
        self.inherit_handles = inherit_handles
        startup = ctypes.cast(
            startup_pointer, ctypes.POINTER(_windows_job.STARTUPINFOEXW)
        ).contents
        self.startup_attribute_list = startup.lpAttributeList
        process_info = ctypes.cast(
            process_info_pointer, ctypes.POINTER(_windows_job.PROCESS_INFORMATION)
        ).contents
        process_info.hProcess = 201
        process_info.hThread = 202
        process_info.dwProcessId = 301
        process_info.dwThreadId = 302
        return True

    def _wait(self, handle, timeout):
        del handle, timeout
        if self.wait_results:
            return self.wait_results.pop(0)
        return _windows_job.WAIT_OBJECT_0

    def _get_exit_code(self, process, exit_code_pointer):
        del process
        exit_code = ctypes.cast(exit_code_pointer, ctypes.POINTER(wintypes.DWORD))
        exit_code.contents.value = 7
        return True

    def _query_job(self, job, information_class, output, output_size, returned):
        del job, information_class, output_size
        accounting = ctypes.cast(
            output,
            ctypes.POINTER(_windows_job.JOBOBJECT_BASIC_ACCOUNTING_INFORMATION),
        )
        if len(self.active_processes) > 1:
            active_processes = self.active_processes.pop(0)
        else:
            active_processes = self.active_processes[0]
        accounting.contents.ActiveProcesses = active_processes
        returned_length = ctypes.cast(returned, ctypes.POINTER(wintypes.DWORD))
        returned_length.contents.value = ctypes.sizeof(accounting.contents)
        return True

    def _terminate_job(self, job, exit_code):
        del job, exit_code
        self.termination_calls += 1
        return self.terminate_result

    def _close_handle(self, handle):
        self.closed_handles.append(handle)
        return handle != self.close_failure


class WindowsJobTests(unittest.TestCase):
    def test_status_record_is_bounded_typed_and_written_once(self):
        stream = io.BytesIO()

        _windows_job._write_status(
            stream,
            descendants_found=True,
            cleanup_error=None,
        )
        document = json.loads(stream.getvalue())

        self.assertEqual(
            {
                "schemaVersion": 1,
                "descendantsFound": True,
                "cleanupError": None,
            },
            document,
        )
        self.assertLessEqual(
            len(stream.getvalue()), _windows_job.MAX_STATUS_BYTES
        )

    def test_status_record_bounds_cleanup_error(self):
        content = _windows_job._status_bytes(
            descendants_found=False,
            cleanup_error="x" * 10_000,
        )

        self.assertEqual(
            _windows_job.MAX_STATUS_ERROR_CHARACTERS,
            len(json.loads(content)["cleanupError"]),
        )

    def test_main_preserves_target_exit_code_when_descendants_are_reported(self):
        read_fd, write_fd = os.pipe()
        arguments = [
            "_windows_job.py",
            "--status-handle",
            str(write_fd),
            "--application",
            r"C:\Python\python.exe",
            "--",
            "python",
            "-V",
        ]
        descendant = _windows_job.DescendantsFoundError(
            "descendants stopped", target_exit_code=7
        )
        try:
            with (
                patch.object(_windows_job.os, "name", "nt"),
                patch.object(_windows_job.sys, "argv", arguments),
                patch.dict(
                    sys.modules,
                    {
                        "msvcrt": SimpleNamespace(
                            open_osfhandle=lambda handle, _flags: handle
                        )
                    },
                ),
                patch.object(_windows_job, "_run", side_effect=descendant),
                patch.object(_windows_job.sys, "stderr", io.StringIO()),
            ):
                exit_code = _windows_job.main()
            status = os.read(read_fd, _windows_job.MAX_STATUS_BYTES)
        finally:
            os.close(read_fd)

        self.assertEqual(7, exit_code)
        self.assertTrue(json.loads(status)["descendantsFound"])

    def test_natural_drain_uses_the_windows_supervision_bound(self):
        self.assertEqual(
            WINDOWS_NATURAL_DRAIN_GRACE_MILLISECONDS,
            _windows_job.NATURAL_DRAIN_GRACE_MILLISECONDS,
        )

    def test_target_is_assigned_atomically_during_process_creation(self):
        kernel32 = FakeKernel32()

        exit_code = _windows_job._run(
            r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
        )

        self.assertEqual(7, exit_code)
        self.assertEqual(
            [
                _windows_job.PROC_THREAD_ATTRIBUTE_JOB_LIST,
                _windows_job.PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            ],
            kernel32.updated_attributes,
        )
        self.assertEqual(101, kernel32.job_attribute)
        self.assertEqual(
            [
                _windows_job.STD_INPUT_HANDLE,
                _windows_job.STD_OUTPUT_HANDLE,
                _windows_job.STD_ERROR_HANDLE,
            ],
            kernel32.handle_attribute,
        )
        self.assertEqual([2, 2], kernel32.attribute_counts)
        self.assertTrue(
            kernel32.creation_flags & _windows_job.EXTENDED_STARTUPINFO_PRESENT
        )
        self.assertTrue(kernel32.startup_attribute_list)
        self.assertEqual(r"C:\Python\python.exe", kernel32.application)
        self.assertEqual("python -V", kernel32.command_line)
        self.assertEqual(1, kernel32.deleted_attribute_lists)
        self.assertEqual([202, 201, 101], kernel32.closed_handles)

    def test_wait_failure_terminates_the_job_and_closes_every_handle(self):
        kernel32 = FakeKernel32()
        kernel32.wait_results = [
            _windows_job.WAIT_FAILED,
            _windows_job.WAIT_OBJECT_0,
        ]
        kernel32.active_processes = [1, 0]

        with self.assertRaisesRegex(OSError, "WaitForSingleObject"):
            _windows_job._run(
                r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
            )

        self.assertEqual(1, kernel32.termination_calls)
        self.assertEqual(1, kernel32.deleted_attribute_lists)
        self.assertEqual([202, 201, 101], kernel32.closed_handles)

    def test_cleanup_api_failure_is_completion_blocking(self):
        kernel32 = FakeKernel32()
        kernel32.active_processes = [1, 0]
        kernel32.terminate_result = False

        with patch.object(_windows_job, "NATURAL_DRAIN_GRACE_MILLISECONDS", 0):
            with self.assertRaisesRegex(OSError, "TerminateJobObject"):
                _windows_job._run(
                    r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
                )

        self.assertEqual([202, 201, 101], kernel32.closed_handles)

    def test_completed_target_allows_job_accounting_to_drain(self):
        kernel32 = FakeKernel32()
        kernel32.active_processes = [1, 0]

        exit_code = _windows_job._run(
            r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
        )

        self.assertEqual(7, exit_code)
        self.assertEqual(0, kernel32.termination_calls)
        self.assertEqual([202, 201, 101], kernel32.closed_handles)

    def test_descendant_cleanup_is_reported_as_a_command_failure(self):
        kernel32 = FakeKernel32()
        kernel32.active_processes = [1, 0]

        with patch.object(_windows_job, "NATURAL_DRAIN_GRACE_MILLISECONDS", 0):
            with self.assertRaisesRegex(
                _windows_job.DescendantsFoundError,
                "left descendant processes",
            ) as raised:
                _windows_job._run(
                    r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
                )

        self.assertEqual(7, raised.exception.target_exit_code)
        self.assertEqual(1, kernel32.termination_calls)
        self.assertEqual([202, 201, 101], kernel32.closed_handles)

    def test_handle_close_failure_is_completion_blocking(self):
        kernel32 = FakeKernel32()
        kernel32.close_failure = 101

        with self.assertRaisesRegex(OSError, r"CloseHandle\(job\)"):
            _windows_job._run(
                r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
            )

        self.assertEqual([202, 201, 101], kernel32.closed_handles)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object integration")
    def test_real_job_allows_a_short_lived_descendant_to_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = root / "started"
            completed = root / "completed"
            child = (
                "from pathlib import Path; import sys, time; "
                "Path(sys.argv[1]).write_text('started'); "
                "time.sleep(1); Path(sys.argv[2]).write_text('completed')"
            )
            parent = (
                "from pathlib import Path; import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], "
                "sys.argv[2], sys.argv[3]]); "
                "deadline=time.monotonic()+5; marker=Path(sys.argv[2]); "
                "\nwhile not marker.exists() and time.monotonic() < deadline: "
                "time.sleep(0.01)\n"
                "raise SystemExit(0 if marker.exists() else 2)"
            )

            started_at = time.monotonic()
            exit_code = _windows_job._run(
                sys.executable,
                [
                    sys.executable,
                    "-c",
                    parent,
                    child,
                    str(started),
                    str(completed),
                ],
            )
            elapsed = time.monotonic() - started_at

            self.assertEqual(0, exit_code)
            self.assertTrue(completed.exists())
            self.assertGreaterEqual(elapsed, 0.75)
            self.assertLess(
                elapsed,
                WINDOWS_NATURAL_DRAIN_GRACE_MILLISECONDS / 1000 + 5,
            )

    @unittest.skipUnless(os.name == "nt", "Windows Job Object integration")
    def test_real_job_terminates_a_descendant_left_by_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            started = root / "started"
            survived = root / "survived"
            child = (
                "from pathlib import Path; import sys, time; "
                "Path(sys.argv[1]).write_text('started'); "
                "time.sleep(30); Path(sys.argv[2]).write_text('survived')"
            )
            parent = (
                "from pathlib import Path; import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', sys.argv[1], "
                "sys.argv[2], sys.argv[3]]); "
                "deadline=time.monotonic()+5; marker=Path(sys.argv[2]); "
                "\nwhile not marker.exists() and time.monotonic() < deadline: "
                "time.sleep(0.01)\n"
                "raise SystemExit(0 if marker.exists() else 2)"
            )

            with self.assertRaisesRegex(
                _windows_job.DescendantsFoundError,
                "left descendant processes",
            ):
                _windows_job._run(
                    sys.executable,
                    [
                        sys.executable,
                        "-c",
                        parent,
                        child,
                        str(started),
                        str(survived),
                    ]
                )

            time.sleep(0.25)
            self.assertFalse(survived.exists())


if __name__ == "__main__":
    unittest.main()
