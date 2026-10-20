"""Pruebas de la Fase 3 con Terraform falso: nada de esto toca Azure."""

import contextlib
import io
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
from plan_review import analyze_plan  # noqa: E402
from terraform_runner import Result  # noqa: E402

CREATE_PLAN = """Terraform will perform the following actions:

  # azurerm_resource_group.main will be created
  + resource "azurerm_resource_group" "main" {
      + name = "rg-backups-dev"
    }

  # azurerm_storage_account.backups will be created
  + resource "azurerm_storage_account" "backups" {
      + name = (known after apply)
    }

  # random_string.storage_suffix will be created
  + resource "random_string" "storage_suffix" {
      + length = 6
    }

Plan: 3 to add, 0 to change, 0 to destroy.
"""

DESTROY_PLAN = CREATE_PLAN.replace("will be created", "will be destroyed").replace(
    "Plan: 3 to add, 0 to change, 0 to destroy.", "Plan: 0 to add, 0 to change, 3 to destroy.")

CHANGE_PLAN = """  # azurerm_storage_account.backups must be replaced
-/+ resource "azurerm_storage_account" "backups" {
    }

Plan: 1 to add, 0 to change, 1 to destroy.
"""

VM_PLAN = """  # azurerm_virtual_machine.vm will be created
  + resource "azurerm_virtual_machine" "vm" {
    }

Plan: 1 to add, 0 to change, 0 to destroy.
"""


def fake_tf(plan_output=CREATE_PLAN, plan_ok=True, apply_ok=True, destroy_plan_output=DESTROY_PLAN):
    """Terraform falso que registra cada llamada."""
    calls = []

    def plan(out, destroy=False):
        calls.append("plan-destroy" if destroy else "plan")
        return Result(plan_ok, destroy_plan_output if destroy else plan_output, "plan")

    def apply(out):
        calls.append("apply")
        return Result(apply_ok, "Apply complete!", "apply")

    def state_list(out):
        calls.append("state")
        return Result(True, "azurerm_resource_group.main", "state")

    def remove_plan_file(out):
        calls.append("remove")

    tf = types.SimpleNamespace(plan=plan, apply=apply, state_list=state_list,
                               remove_plan_file=remove_plan_file, calls=calls)
    return tf


def answers(*replies):
    """input falso que devuelve respuestas en orden y registra cuántas veces se preguntó."""
    queue = list(replies)
    asked = []

    def input_fn(question):
        asked.append(question)
        return queue.pop(0)

    input_fn.asked = asked
    return input_fn


def run_quiet(func, *args, **kwargs):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        result = func(*args, **kwargs)
    return result, buffer.getvalue()


class TestPlanReview(unittest.TestCase):
    def test_reads_the_summary(self):
        info = analyze_plan(CREATE_PLAN)
        self.assertEqual((info.add, info.change, info.destroy), (3, 0, 0))
        self.assertEqual(info.warnings, [])
        self.assertEqual(info.blocked, [])

    def test_no_changes(self):
        self.assertTrue(analyze_plan("No changes. Your infrastructure matches the configuration.").no_changes)

    def test_unreadable_plan_is_blocked(self):
        self.assertTrue(analyze_plan("salida rara sin resumen").blocked)

    def test_replace_or_destroy_on_a_normal_run_warns(self):
        info = analyze_plan(CHANGE_PLAN)
        self.assertEqual(len(info.warnings), 1)
        self.assertEqual(info.blocked, [])

    def test_destroy_plan_does_not_warn_about_destroying(self):
        self.assertEqual(analyze_plan(DESTROY_PLAN, destroying=True).warnings, [])

    def test_disallowed_resource_in_the_plan_is_blocked(self):
        info = analyze_plan(VM_PLAN)
        self.assertEqual(len(info.blocked), 1)
        self.assertIn("azurerm_virtual_machine", info.blocked[0])

    def test_data_source_in_the_plan_is_blocked(self):
        plan = "  # data.external.x will be read during apply\n\nPlan: 0 to add, 0 to change, 0 to destroy.\n"
        self.assertTrue(analyze_plan(plan).blocked)


class TestConfirmation(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"ARM_SUBSCRIPTION_ID": "sub-id"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_only_the_full_word_yes_applies(self):
        for reply in ("no", "y", "YES", "Yes", "", "sí", "si", "yes please", "yess"):
            tf = fake_tf()
            ok, _ = run_quiet(app.review_and_apply, Path("."), False, tf, answers(reply))
            self.assertFalse(ok, repr(reply))
            self.assertNotIn("apply", tf.calls, repr(reply))

    def test_yes_applies_the_reviewed_plan(self):
        tf = fake_tf()
        ok, _ = run_quiet(app.review_and_apply, Path("."), False, tf, answers("yes"))
        self.assertTrue(ok)
        self.assertEqual(tf.calls, ["plan", "apply", "remove"])

    def test_yes_with_surrounding_spaces_is_accepted(self):
        tf = fake_tf()
        ok, _ = run_quiet(app.review_and_apply, Path("."), False, tf, answers("  yes \n"))
        self.assertTrue(ok)

    def test_plan_is_shown_before_asking(self):
        _, printed = run_quiet(app.review_and_apply, Path("."), False, fake_tf(), answers("no"))
        self.assertIn("rg-backups-dev", printed)
        self.assertIn("Plan: 3 to add", printed)

    def test_plan_failure_never_asks_nor_applies(self):
        tf = fake_tf(plan_output="Error: not logged in", plan_ok=False)
        ask = answers()
        ok, printed = run_quiet(app.review_and_apply, Path("."), False, tf, ask)
        self.assertFalse(ok)
        self.assertEqual(ask.asked, [])
        self.assertNotIn("apply", tf.calls)
        self.assertIn("az login", printed)

    def test_blocked_plan_never_asks_nor_applies(self):
        tf = fake_tf(plan_output=VM_PLAN)
        ask = answers()
        ok, _ = run_quiet(app.review_and_apply, Path("."), False, tf, ask)
        self.assertFalse(ok)
        self.assertEqual(ask.asked, [])
        self.assertNotIn("apply", tf.calls)

    def test_no_changes_never_applies(self):
        tf = fake_tf(plan_output="No changes. Your infrastructure matches the configuration.")
        ok, _ = run_quiet(app.review_and_apply, Path("."), False, tf, answers())
        self.assertFalse(ok)
        self.assertNotIn("apply", tf.calls)

    def test_warning_is_shown_for_changes_to_existing_resources(self):
        tf = fake_tf(plan_output=CHANGE_PLAN)
        _, printed = run_quiet(app.review_and_apply, Path("."), False, tf, answers("no"))
        self.assertIn("MODIFICA", printed)

    def test_missing_subscription_id_runs_nothing(self):
        tf = fake_tf()
        with mock.patch.dict(os.environ, {}, clear=True):
            ok, printed = run_quiet(app.review_and_apply, Path("."), False, tf, answers())
        self.assertFalse(ok)
        self.assertEqual(tf.calls, [])
        self.assertIn("ARM_SUBSCRIPTION_ID", printed)

    def test_apply_failure_returns_false(self):
        tf = fake_tf(apply_ok=False)
        ok, printed = run_quiet(app.review_and_apply, Path("."), False, tf, answers("yes"))
        self.assertFalse(ok)
        self.assertIn("Falló terraform apply", printed)

    def test_saved_plan_file_is_always_removed(self):
        for reply, kwargs in (("yes", {}), ("no", {}), ("yes", {"apply_ok": False}),
                              ("yes", {"plan_ok": False}), ("yes", {"plan_output": VM_PLAN})):
            tf = fake_tf(**kwargs)
            run_quiet(app.review_and_apply, Path("."), False, tf, answers(reply))
            self.assertEqual(tf.calls[-1], "remove", kwargs)


class TestDeployAndDestroy(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"ARM_SUBSCRIPTION_ID": "sub-id"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_deploy_then_decline_destroy_leaves_resources(self):
        tf = fake_tf()
        _, printed = run_quiet(app.deploy, Path("."), tf, answers("yes", "no"))
        self.assertEqual(tf.calls.count("apply"), 1)
        self.assertIn("plan-destroy", tf.calls)
        self.assertIn('escribe "destroy"', printed)

    def test_deploy_then_yes_destroys(self):
        tf = fake_tf()
        run_quiet(app.deploy, Path("."), tf, answers("yes", "yes"))
        self.assertEqual(tf.calls.count("apply"), 2)

    def test_destroy_needs_its_own_written_confirmation(self):
        tf = fake_tf()
        run_quiet(app.deploy, Path("."), tf, answers("yes", "y"))
        self.assertEqual(tf.calls.count("apply"), 1)  # solo el de crear

    def test_declining_the_creation_never_offers_destroy(self):
        tf = fake_tf()
        run_quiet(app.deploy, Path("."), tf, answers("no"))
        self.assertEqual(tf.calls, ["plan", "remove"])

    def test_failed_apply_does_not_offer_destroy(self):
        tf = fake_tf(apply_ok=False)
        run_quiet(app.deploy, Path("."), tf, answers("yes"))
        self.assertNotIn("plan-destroy", tf.calls)

    def test_destroy_command_plans_a_destroy_and_asks(self):
        tf = fake_tf()
        ok, _ = run_quiet(app.review_and_apply, Path("."), True, tf, answers("yes"))
        self.assertTrue(ok)
        self.assertEqual(tf.calls, ["plan-destroy", "apply", "remove"])


if __name__ == "__main__":
    unittest.main()