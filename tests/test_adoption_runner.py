import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


PROCESS_ROOT = Path(__file__).resolve().parent.parent


def load_runner():
    path = PROCESS_ROOT / "templates" / "adopt-process.py"
    spec = importlib.util.spec_from_file_location("managed_adoption_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load managed adoption runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AdoptionRunnerTests(unittest.TestCase):
    def test_command_output_is_drained_and_bounded(self):
        runner = load_runner()
        runner.MAX_CAPTURE_BYTES = 64
        with tempfile.TemporaryDirectory() as directory:
            output = runner._run(
                [sys.executable, "-c", "print('x' * 10000)"],
                cwd=Path(directory),
            )

        self.assertLess(len(output), 256)
        self.assertIn("output truncated: 10001 bytes", output)
        self.assertRegex(output, r"sha256:[0-9a-f]{64}")

    def test_command_timeout_terminates_the_process_group(self):
        runner = load_runner()
        runner.COMMAND_TIMEOUT_SECONDS = 0.01
        runner.TERMINATION_TIMEOUT_SECONDS = 2
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                runner._run(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    cwd=Path(directory),
                )

    def test_child_environment_does_not_forward_credentials_or_python_paths(self):
        runner = load_runner()
        previous = {
            key: os.environ.get(key)
            for key in ("GH_TOKEN", "PIP_INDEX_URL", "PYTHONPATH")
        }
        try:
            os.environ["GH_TOKEN"] = "secret"
            os.environ["PIP_INDEX_URL"] = "https://secret@example.invalid/simple"
            os.environ["PYTHONPATH"] = "/untrusted"
            environment = runner._child_environment()
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("PIP_INDEX_URL", environment)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertEqual(os.devnull, environment["PIP_CONFIG_FILE"])


if __name__ == "__main__":
    unittest.main()
