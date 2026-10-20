"""Pruebas del flujo con modelo y Terraform falsos: no consumen crédito ni llaman a Azure."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
from terraform_runner import Result  # noqa: E402

GOOD = 'terraform {\n  required_version = ">= 1.6.0"\n}'
BAD = 'terraform {\n  required_version = ">= 1.6.0"\n  oops = \n}'
EXTRA_RESOURCE = 'resource "azurerm_virtual_machine" "vm" {\n}'


def fake_model(*replies):
    """Modelo falso que devuelve las respuestas en orden y guarda lo que recibió."""
    calls = []
    queue = list(replies)

    def ask(system_prompt, user_text):
        calls.append(user_text)
        return queue.pop(0)

    ask.calls = calls
    return ask


def fake_check(*results):
    queue = list(results)
    calls = []

    def check(output_dir):
        calls.append(output_dir)
        return queue.pop(0)

    check.calls = calls
    return check


OK = Result(True, "", "validate")


def fail(stage="validate", output="Error: Argument or block definition required"):
    return Result(False, output, stage)


class TestFlow(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "output"

    def tearDown(self):
        self._tmp.cleanup()

    # --- Fase 1 ---------------------------------------------------------------
    def test_normal_case_saves_main_tf(self):
        path = app.handle_request("un grupo y un storage", self.out, fake_model(GOOD), fake_check(OK))
        self.assertEqual(path, self.out / "main.tf")
        self.assertEqual(path.read_text(encoding="utf-8").strip(), GOOD)

    def test_markdown_fences_are_removed(self):
        for wrapped in (f"```hcl\n{GOOD}\n```", f"```\n{GOOD}\n```", f"```terraform\n{GOOD}\n```"):
            self.assertEqual(app.clean_response(wrapped), GOOD)

    def test_out_of_scope_saves_nothing(self):
        for reply in ("FUERA_DE_ALCANCE", " FUERA_DE_ALCANCE\n"):
            check = fake_check()
            path = app.handle_request("crea una máquina virtual", self.out, fake_model(reply), check)
            self.assertIsNone(path)
            self.assertFalse((self.out / "main.tf").exists())
            self.assertEqual(check.calls, [])

    def test_empty_model_reply_saves_nothing(self):
        self.assertIsNone(app.handle_request("algo", self.out, fake_model("   "), fake_check()))
        self.assertFalse((self.out / "main.tf").exists())

    def test_empty_and_too_long_requests_never_call_the_model(self):
        def boom(*_):
            raise AssertionError("no debía llamar al modelo")

        self.assertIsNone(app.handle_request("   ", self.out, boom))
        self.assertIsNone(app.handle_request("x" * (app.MAX_REQUEST_CHARS + 1), self.out, boom))

    # --- Fase 2 ---------------------------------------------------------------
    def test_disallowed_resource_is_rejected_before_terraform(self):
        check = fake_check()
        path = app.handle_request("algo", self.out, fake_model(EXTRA_RESOURCE), check)
        self.assertIsNone(path)
        self.assertEqual(check.calls, [])  # terraform nunca se ejecutó
        self.assertFalse((self.out / "main.tf").exists())  # y no se guardó nada

    def test_validate_error_goes_back_to_the_model_and_is_fixed(self):
        ask = fake_model(BAD, GOOD)
        check = fake_check(fail(output="ERROR-DE-TERRAFORM"), OK)
        path = app.handle_request("un storage", self.out, ask, check)
        self.assertEqual(len(ask.calls), 2)
        self.assertIn("ERROR-DE-TERRAFORM", ask.calls[1])  # el error volvió al modelo
        self.assertIn("un storage", ask.calls[1])
        self.assertEqual(path.read_text(encoding="utf-8").strip(), GOOD)

    def test_stops_after_two_failed_retries(self):
        ask = fake_model(BAD, BAD, BAD)
        check = fake_check(fail(), fail(), fail())
        path = app.handle_request("un storage", self.out, ask, check)
        self.assertIsNone(path)
        self.assertEqual(len(ask.calls), 1 + app.MAX_RETRIES)  # nunca un tercer reintento
        self.assertEqual(len(check.calls), 1 + app.MAX_RETRIES)

    def test_init_failure_is_not_retried(self):
        ask = fake_model(GOOD)
        path = app.handle_request("un storage", self.out, ask, fake_check(fail("init", "no hay red")))
        self.assertIsNone(path)
        self.assertEqual(len(ask.calls), 1)

    def test_fixed_code_with_disallowed_resource_is_rejected(self):
        ask = fake_model(BAD, EXTRA_RESOURCE)
        check = fake_check(fail())
        self.assertIsNone(app.handle_request("un storage", self.out, ask, check))
        self.assertEqual(len(check.calls), 1)  # el código corregido no llegó a Terraform

    def test_out_of_scope_during_retry_stops(self):
        ask = fake_model(BAD, "FUERA_DE_ALCANCE")
        check = fake_check(fail())
        self.assertIsNone(app.handle_request("un storage", self.out, ask, check))
        self.assertEqual(len(check.calls), 1)


if __name__ == "__main__":
    unittest.main()