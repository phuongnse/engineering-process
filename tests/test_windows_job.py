import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

from engineering_process import _windows_job


class NativeCall:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments):
        return self.callback(*arguments)


class FakeKernel32:
    def __init__(self):
        self.process_ids = [()]
        self.assigned_process_count = None
        self.wait_results = [_windows_job.WAIT_OBJECT_0]
        self.terminate_result = True
        self.close_failure = None
        self.closed_handles = []
        self.deleted_attribute_lists = 0
        self.termination_calls = 0
        self.creation_flags = None
        self.job_attribute = None
        self.application = None
        self.command_line = None
        self.events = []
        self.root_active_until_process_handle_closed = False

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
        del count, flags
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
        self.asserted_attribute = attribute
        self.job_attribute = ctypes.cast(
            value, ctypes.POINTER(wintypes.HANDLE)
        )[0]
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
        self.events.append(f"wait:{handle}")
        del handle, timeout
        if self.wait_results:
            return self.wait_results.pop(0)
        return _windows_job.WAIT_OBJECT_0

    def _get_exit_code(self, process, exit_code_pointer):
        self.events.append(f"exit-code:{process}")
        del process
        exit_code = ctypes.cast(exit_code_pointer, ctypes.POINTER(wintypes.DWORD))
        exit_code.contents.value = 7
        return True

    def _query_job(self, job, information_class, output, output_size, returned):
        self.events.append("query-job")
        del job, output_size
        if information_class != _windows_job.JOB_OBJECT_BASIC_PROCESS_ID_LIST_CLASS:
            raise AssertionError(
                f"unexpected job information class: {information_class}"
            )
        process_list = ctypes.cast(
            output,
            ctypes.POINTER(_windows_job.JOBOBJECT_BASIC_PROCESS_ID_LIST),
        )
        if self.root_active_until_process_handle_closed and 201 not in self.closed_handles:
            process_ids = (301,)
        elif len(self.process_ids) > 1:
            process_ids = self.process_ids.pop(0)
        else:
            process_ids = self.process_ids[0]
        process_list.contents.NumberOfAssignedProcesses = (
            len(process_ids)
            if self.assigned_process_count is None
            else self.assigned_process_count
        )
        process_list.contents.NumberOfProcessIdsInList = len(process_ids)
        for index, process_id in enumerate(process_ids):
            process_list.contents.ProcessIdList[index] = process_id
        returned_length = ctypes.cast(returned, ctypes.POINTER(wintypes.DWORD))
        returned_length.contents.value = ctypes.sizeof(process_list.contents)
        return True

    def _terminate_job(self, job, exit_code):
        self.events.append("terminate-job")
        del job, exit_code
        self.termination_calls += 1
        return self.terminate_result

    def _close_handle(self, handle):
        self.events.append(f"close:{handle}")
        self.closed_handles.append(handle)
        return handle != self.close_failure


class WindowsJobTests(unittest.TestCase):
    def test_target_is_assigned_atomically_during_process_creation(self):
        kernel32 = FakeKernel32()

        exit_code = _windows_job._run(
            r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
        )

        self.assertEqual(7, exit_code)
        self.assertEqual(
            _windows_job.PROC_THREAD_ATTRIBUTE_JOB_LIST,
            kernel32.asserted_attribute,
        )
        self.assertEqual(101, kernel32.job_attribute)
        self.assertTrue(
            kernel32.creation_flags & _windows_job.EXTENDED_STARTUPINFO_PRESENT
        )
        self.assertTrue(kernel32.startup_attribute_list)
        self.assertEqual(r"C:\Python\python.exe", kernel32.application)
        self.assertEqual("python -V", kernel32.command_line)
        self.assertEqual(1, kernel32.deleted_attribute_lists)
        self.assertEqual([202, 201, 101], kernel32.closed_handles)

    def test_root_handles_are_closed_before_descendant_accounting(self):
        kernel32 = FakeKernel32()
        kernel32.root_active_until_process_handle_closed = True

        exit_code = _windows_job._run(
            r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
        )

        self.assertEqual(7, exit_code)
        self.assertEqual(0, kernel32.termination_calls)
        self.assertLess(
            kernel32.events.index("close:202"),
            kernel32.events.index("wait:201"),
        )
        self.assertLess(
            kernel32.events.index("close:201"),
            kernel32.events.index("query-job"),
        )

    def test_exited_root_identity_drains_without_descendant_termination(self):
        kernel32 = FakeKernel32()
        kernel32.process_ids = [(301,), ()]

        exit_code = _windows_job._run(
            r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
        )

        self.assertEqual(7, exit_code)
        self.assertEqual(0, kernel32.termination_calls)
        self.assertGreaterEqual(kernel32.events.count("query-job"), 2)

    def test_wait_failure_terminates_the_job_and_closes_every_handle(self):
        kernel32 = FakeKernel32()
        kernel32.wait_results = [
            _windows_job.WAIT_FAILED,
            _windows_job.WAIT_OBJECT_0,
        ]
        kernel32.process_ids = [(301,), ()]

        with self.assertRaisesRegex(OSError, "WaitForSingleObject"):
            _windows_job._run(
                r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
            )

        self.assertEqual(1, kernel32.termination_calls)
        self.assertEqual(1, kernel32.deleted_attribute_lists)
        self.assertEqual([202, 201, 101], kernel32.closed_handles)

    def test_cleanup_api_failure_is_completion_blocking(self):
        kernel32 = FakeKernel32()
        kernel32.process_ids = [(401,), ()]
        kernel32.terminate_result = False

        with self.assertRaisesRegex(OSError, "TerminateJobObject"):
            _windows_job._run(
                r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
            )

        self.assertEqual([202, 201, 101], kernel32.closed_handles)

    def test_process_list_overflow_fails_closed(self):
        kernel32 = FakeKernel32()
        kernel32.process_ids = [(401,)]
        kernel32.assigned_process_count = _windows_job.MAX_JOB_PROCESS_IDS + 1

        with self.assertRaisesRegex(OSError, "bounded cleanup capacity"):
            _windows_job._run(
                r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
            )

        self.assertEqual(1, kernel32.termination_calls)
        self.assertEqual([202, 201, 101], kernel32.closed_handles)

    def test_descendant_cleanup_is_reported_as_a_command_failure(self):
        kernel32 = FakeKernel32()
        kernel32.process_ids = [(401,), ()]

        with self.assertRaisesRegex(OSError, "left descendant processes"):
            _windows_job._run(
                r"C:\Python\python.exe", ["python", "-V"], kernel32=kernel32
            )

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
    def test_real_job_repeats_root_only_commands_without_false_descendants(self):
        for iteration in range(32):
            with self.subTest(iteration=iteration):
                self.assertEqual(
                    0,
                    _windows_job._run(
                        sys.executable,
                        [sys.executable, "-c", "raise SystemExit(0)"],
                    ),
                )

    @unittest.skipUnless(os.name == "nt", "Windows Job Object integration")
    def test_real_job_repeats_git_commands_without_false_descendants(self):
        git = shutil.which("git")
        self.assertIsNotNone(git)
        assert git is not None
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                0,
                _windows_job._run(git, [git, "-C", directory, "init", "-q"]),
            )
            for iteration in range(64):
                with self.subTest(iteration=iteration):
                    self.assertEqual(
                        0,
                        _windows_job._run(
                            git,
                            [git, "-C", directory, "status", "--porcelain=v1"],
                        ),
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
                "time.sleep(1); Path(sys.argv[2]).write_text('survived')"
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

            with self.assertRaisesRegex(OSError, "left descendant processes"):
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

            time.sleep(1.25)
            self.assertFalse(survived.exists())


if __name__ == "__main__":
    unittest.main()
