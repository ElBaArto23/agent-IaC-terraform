"""Pruebas del runner con subprocess falso: no necesitan Terraform instalado."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import terraform_runner as tr  # noqa: E402


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.cwd = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(tr.shutil, "which", return_value="terraform")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_command_is_a_list_without_shell(self):
        with mock.patch.object(tr.subprocess, "run", return_value=completed()) as run:
            tr.run(["validate", "-no-color"], self.cwd, "validate")
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["terraform", "validate", "-no-color"])
        self.assertFalse(kwargs.get("shell", False))
        self.assertEqual(kwargs["cwd"], self.cwd)

    def test_model_credentials_are_not_passed_to_terraform(self):
        fake_env = {"PATH": "x", "AZURE_AI_API_KEY": "secreto", "AZURE_AI_ENDPOINT": "e", "ARM_SUBSCRIPTION_ID": "s"}
        with mock.patch.dict(os.environ, fake_env, clear=True), \
                mock.patch.object(tr.subprocess, "run", return_value=completed()) as run:
            tr.run(["fmt"], self.cwd)
        env = run.call_args.kwargs["env"]
        self.assertNotIn("AZURE_AI_API_KEY", env)
        self.assertNotIn("AZURE_AI_ENDPOINT", env)
        self.assertEqual(env["ARM_SUBSCRIPTION_ID"], "s")

    def test_failure_returns_output_and_stage(self):
        with mock.patch.object(tr.subprocess, "run", return_value=completed(1, "", "Error: algo")):
            result = tr.run(["validate"], self.cwd, "validate")
        self.assertFalse(result.ok)
        self.assertIn("Error: algo", result.output)
        self.assertEqual(result.stage, "validate")

    def test_missing_terraform_is_reported(self):
        with mock.patch.object(tr.shutil, "which", return_value=None):
            result = tr.run(["fmt"], self.cwd, "fmt")
        self.assertFalse(result.ok)
        self.assertIn("No se encontró terraform", result.output)

    def test_timeout_is_reported(self):
        with mock.patch.object(tr.subprocess, "run", side_effect=subprocess.TimeoutExpired("terraform", 1)):
            result = tr.run(["init"], self.cwd, "init")
        self.assertFalse(result.ok)
        self.assertIn("se canceló", result.output)

    def test_check_runs_fmt_init_validate_in_order(self):
        with mock.patch.object(tr.subprocess, "run", return_value=completed()) as run:
            result = tr.check_with_terraform(self.cwd)
        self.assertTrue(result.ok)
        self.assertEqual([c.args[0][1] for c in run.call_args_list], ["fmt", "init", "validate"])

    def test_check_stops_at_the_first_failure(self):
        replies = [completed(0), completed(1, "", "Error: Invalid provider")]
        with mock.patch.object(tr.subprocess, "run", side_effect=replies) as run:
            result = tr.check_with_terraform(self.cwd)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "init")
        self.assertEqual(run.call_count, 2)  # validate nunca se ejecutó

    def test_plan_is_saved_and_destroy_plan_adds_the_flag(self):
        with mock.patch.object(tr.subprocess, "run", return_value=completed()) as run:
            tr.plan(self.cwd)
            tr.plan(self.cwd, destroy=True)
        normal, destroy = (c.args[0] for c in run.call_args_list)
        self.assertIn("-out=plan.tfplan", normal)
        self.assertNotIn("-destroy", normal)
        self.assertIn("-destroy", destroy)
        self.assertIn("-input=false", normal)

    def test_apply_uses_the_saved_plan_and_never_auto_approve(self):
        with mock.patch.object(tr.subprocess, "run", return_value=completed()) as run:
            tr.apply(self.cwd)
        command = run.call_args.args[0]
        self.assertEqual(command[1:], ["apply", "-input=false", "-no-color", "plan.tfplan"])
        self.assertNotIn("-auto-approve", command)

    def test_remove_plan_file(self):
        (self.cwd / tr.PLAN_FILE).write_text("x")
        tr.remove_plan_file(self.cwd)
        self.assertFalse((self.cwd / tr.PLAN_FILE).exists())
        tr.remove_plan_file(self.cwd)  # sin archivo tampoco falla

    def test_has_resources_reads_the_state_file(self):
        self.assertFalse(tr.has_resources(self.cwd))  # sin archivo
        (self.cwd / "terraform.tfstate").write_text('{"resources": []}')
        self.assertFalse(tr.has_resources(self.cwd))
        (self.cwd / "terraform.tfstate").write_text('{"resources": [{"type": "random_string"}]}')
        self.assertTrue(tr.has_resources(self.cwd))
        (self.cwd / "terraform.tfstate").write_text("no es json")
        self.assertFalse(tr.has_resources(self.cwd))


if __name__ == "__main__":
    unittest.main()