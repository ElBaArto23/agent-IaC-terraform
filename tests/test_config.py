"""Pruebas de la carpeta de trabajo configurable y de su comprobación al arrancar."""

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

try:
    import server  # noqa: E402
except ImportError as error:  # pragma: no cover - solo si faltan las dependencias del servidor
    raise unittest.SkipTest(f"Faltan las dependencias del servidor: {error}")


class OutputDirTests(unittest.TestCase):
    def tearDown(self):
        importlib.reload(config)  # deja el módulo como estaba, sin la variable de prueba

    def test_default_is_the_output_folder_next_to_the_code(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("AGENT_OUTPUT_DIR", None)
            importlib.reload(config)
            self.assertEqual(config.OUTPUT_DIR, Path(config.__file__).parent / "output")

    def test_environment_variable_overrides_it(self):
        with mock.patch.dict(os.environ, {"AGENT_OUTPUT_DIR": "/data/output"}):
            importlib.reload(config)
            self.assertEqual(config.OUTPUT_DIR, Path("/data/output"))

    def test_empty_variable_falls_back_to_the_default(self):
        with mock.patch.dict(os.environ, {"AGENT_OUTPUT_DIR": ""}):
            importlib.reload(config)
            self.assertEqual(config.OUTPUT_DIR, Path(config.__file__).parent / "output")

    def test_app_and_runner_share_the_same_folder(self):
        import app
        import terraform_runner
        self.assertEqual(app.OUTPUT_DIR, terraform_runner.OUTPUT_DIR)


class EnsureOutputDirTests(unittest.TestCase):
    def test_creates_a_missing_folder_and_leaves_no_probe_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "a" / "b"
            self.assertIsNone(server.ensure_output_dir(target))
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])

    def test_reports_a_folder_that_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "es-un-archivo"
            blocker.write_text("x", encoding="utf-8")
            problem = server.ensure_output_dir(blocker / "output")
            self.assertIn("No puedo escribir en la carpeta de trabajo", problem)
            self.assertIn("despliegue-container-apps", problem)

    def test_server_refuses_to_start_when_the_folder_is_not_writable(self):
        with mock.patch.object(server, "ensure_output_dir", return_value="sin permiso"):
            with mock.patch.dict(os.environ, {"HOST": "127.0.0.1"}):
                with self.assertRaises(SystemExit) as stop:
                    server.main()
        self.assertEqual(str(stop.exception), "sin permiso")


if __name__ == "__main__":
    unittest.main()