"""Pruebas de la Fase 3 con Terraform falso: nada de esto toca Azure."""

import contextlib
import io
import os
import sys
import tempfile
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


def fake_tf(plan_output=CREATE_PLAN, plan_ok=True, apply_ok=True, destroy_plan_output=DESTROY_PLAN,
            apply_sequence=None, state_output="azurerm_resource_group.main", state_ok=True,
            apply_error="Error: boom", passwords=None):
    """Terraform falso que registra cada llamada.

    apply_sequence: resultados sucesivos de apply (True/False); si no se da, siempre apply_ok.
    """
    calls = []
    sequence = list(apply_sequence) if apply_sequence is not None else None

    def plan(out, destroy=False):
        calls.append("plan-destroy" if destroy else "plan")
        return Result(plan_ok, destroy_plan_output if destroy else plan_output, "plan")

    def apply(out):
        calls.append("apply")
        ok = sequence.pop(0) if sequence is not None else apply_ok
        return Result(ok, "Apply complete!" if ok else apply_error, "apply")

    def state_list(out):
        calls.append("state")
        return Result(state_ok, state_output, "state")

    def remove_plan_file(out):
        calls.append("remove")

    def generated_passwords(out):
        calls.append("secrets")
        return dict(passwords or {})

    tf = types.SimpleNamespace(plan=plan, apply=apply, state_list=state_list,
                               remove_plan_file=remove_plan_file,
                               generated_passwords=generated_passwords, calls=calls)
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

    # --- Apply que falla a medias ---------------------------------------------------
    def test_failed_apply_with_leftovers_offers_cleanup_and_yes_destroys(self):
        tf = fake_tf(apply_sequence=[False, True])
        _, printed = run_quiet(app.deploy, Path("."), tf, answers("yes", "yes"))
        self.assertEqual(tf.calls, ["plan", "apply", "remove", "state", "plan-destroy", "apply", "remove"])
        self.assertIn("quedó creado", printed)

    def test_failed_apply_cleanup_needs_its_own_yes(self):
        tf = fake_tf(apply_sequence=[False])
        _, printed = run_quiet(app.deploy, Path("."), tf, answers("yes", "no"))
        self.assertEqual(tf.calls.count("apply"), 1)  # el de destruir no se ejecutó
        self.assertIn('escribe "destroy"', printed)

    def test_failed_apply_with_nothing_left_does_not_offer_destroy(self):
        tf = fake_tf(apply_ok=False, state_output="")
        ask = answers("yes")
        _, printed = run_quiet(app.deploy, Path("."), tf, ask)
        self.assertNotIn("plan-destroy", tf.calls)
        self.assertEqual(len(ask.asked), 1)
        self.assertIn("No quedó nada", printed)

    def test_failed_apply_when_state_cannot_be_read_does_not_offer_destroy(self):
        tf = fake_tf(apply_ok=False, state_ok=False, state_output="Error")
        run_quiet(app.deploy, Path("."), tf, answers("yes"))
        self.assertNotIn("plan-destroy", tf.calls)

    def test_cleanup_is_only_offered_after_a_failed_apply(self):
        for kwargs, reply in (({}, "no"), ({"plan_ok": False}, "yes"), ({"plan_output": VM_PLAN}, "yes")):
            tf = fake_tf(**kwargs)
            run_quiet(app.deploy, Path("."), tf, answers(reply))
            self.assertNotIn("state", tf.calls, kwargs)
            self.assertNotIn("plan-destroy", tf.calls, kwargs)

    def test_failed_destroy_never_loops_into_another_cleanup(self):
        tf = fake_tf(apply_sequence=[True, False])
        run_quiet(app.deploy, Path("."), tf, answers("yes", "yes"))
        self.assertEqual(tf.calls.count("plan-destroy"), 1)

    def test_destroy_command_plans_a_destroy_and_asks(self):
        tf = fake_tf()
        ok, _ = run_quiet(app.review_and_apply, Path("."), True, tf, answers("yes"))
        self.assertTrue(ok)
        self.assertEqual(tf.calls, ["plan-destroy", "apply", "remove"])


REGION_ERR = "Error: creating Server: RegionDoesNotAllowProvisioning: Location 'eastus2' is not accepting creation"


class TestRegionFallback(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"ARM_SUBSCRIPTION_ID": "sub-id"}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        (self.dir / "main.tf").write_text('location = "eastus2"\n', encoding="utf-8")

    def region(self):
        return (self.dir / "main.tf").read_text(encoding="utf-8").strip()

    def test_region_error_cleanup_then_yes_switches_and_retries(self):
        tf = fake_tf(apply_sequence=[False, True, True, True], state_output="azurerm_resource_group.main",
                     apply_error=REGION_ERR)
        ask = answers("yes", "yes", "yes", "yes", "no")
        run_quiet(app.deploy, self.dir, tf, ask)
        self.assertEqual(self.region(), 'location = "brazilsouth"')
        # crear (falla) -> limpiar -> cambiar región -> crear de nuevo (plan + yes nuevos)
        self.assertEqual(tf.calls.count("plan"), 2)

    def test_declining_the_switch_keeps_the_region(self):
        tf = fake_tf(apply_ok=False, state_output="", apply_error=REGION_ERR)
        _, printed = run_quiet(app.deploy, self.dir, tf, answers("yes", "no"))
        self.assertEqual(self.region(), 'location = "eastus2"')
        self.assertEqual(tf.calls.count("plan"), 1)

    def test_switch_needs_the_full_word_yes(self):
        tf = fake_tf(apply_ok=False, state_output="", apply_error=REGION_ERR)
        run_quiet(app.deploy, self.dir, tf, answers("yes", "y"))
        self.assertEqual(self.region(), 'location = "eastus2"')

    def test_non_region_error_never_offers_a_switch(self):
        tf = fake_tf(apply_ok=False, state_output="", apply_error="Error: name already taken")
        ask = answers("yes")
        run_quiet(app.deploy, self.dir, tf, ask)
        self.assertEqual(len(ask.asked), 1)
        self.assertEqual(self.region(), 'location = "eastus2"')

    def test_no_switch_when_cleanup_was_declined(self):
        tf = fake_tf(apply_ok=False, apply_error=REGION_ERR)
        ask = answers("yes", "no")
        run_quiet(app.deploy, self.dir, tf, ask)
        self.assertEqual(len(ask.asked), 2)
        self.assertEqual(self.region(), 'location = "eastus2"')

    def test_no_switch_when_state_cannot_be_read(self):
        tf = fake_tf(apply_ok=False, state_ok=False, state_output="Error", apply_error=REGION_ERR)
        ask = answers("yes")
        run_quiet(app.deploy, self.dir, tf, ask)
        self.assertEqual(len(ask.asked), 1)

    def test_stops_after_max_switches(self):
        tf = fake_tf(apply_ok=False, state_output="", apply_error=REGION_ERR)
        ask = answers(*(["yes"] * 10))
        run_quiet(app.deploy, self.dir, tf, ask)
        self.assertEqual(tf.calls.count("apply"), 3)  # el original + 2 cambios
        self.assertEqual(self.region(), 'location = "swedencentral"')

    def test_without_literal_region_it_stops(self):
        (self.dir / "main.tf").write_text("location = var.x\n", encoding="utf-8")
        tf = fake_tf(apply_ok=False, state_output="", apply_error=REGION_ERR)
        ask = answers("yes")
        run_quiet(app.deploy, self.dir, tf, ask)
        self.assertEqual(len(ask.asked), 1)


class TestSecrets(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"ARM_SUBSCRIPTION_ID": "sub-id"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_passwords_are_shown_after_a_successful_apply(self):
        tf = fake_tf(passwords={"random_password.sql_admin": "Zq9!secret"})
        _, printed = run_quiet(app.deploy, Path("."), tf, answers("yes", "no"))
        self.assertIn("Zq9!secret", printed)
        self.assertIn("sqladmin", printed)

    def test_no_passwords_nothing_printed(self):
        _, printed = run_quiet(app.deploy, Path("."), fake_tf(), answers("yes", "no"))
        self.assertNotIn("🔑", printed)

    def test_passwords_not_shown_when_apply_fails_or_is_cancelled(self):
        for reply, kwargs in (("no", {}), ("yes", {"apply_ok": False, "state_output": ""})):
            tf = fake_tf(passwords={"random_password.sql_admin": "Zq9!secret"}, **kwargs)
            _, printed = run_quiet(app.deploy, Path("."), tf, answers(reply))
            self.assertNotIn("Zq9!secret", printed)

    def test_cost_note_is_printed_before_asking(self):
        plan = ("  # azurerm_mssql_database.main will be created\n\nPlan: 1 to add, 0 to change, 0 to destroy.\n")
        _, printed = run_quiet(app.review_and_apply, Path("."), False, fake_tf(plan_output=plan), answers("no"))
        self.assertIn("💲", printed)


if __name__ == "__main__":
    unittest.main()