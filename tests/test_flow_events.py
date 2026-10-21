"""Pruebas de los eventos que emite el flujo de plan, confirmación y apply.

Usan el Terraform falso de test_deploy: nada de esto toca Azure.
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app  # noqa: E402
import events  # noqa: E402
from test_deploy import VM_PLAN, answers, fake_tf, run_quiet  # noqa: E402


class Recorder:
    """Se suscribe a los eventos y los guarda como (tipo, texto, datos)."""

    def __init__(self):
        self.items = []
        self._stop = events.subscribe(lambda kind, text, data: self.items.append((kind, text, data)))

    def close(self):
        self._stop()

    def kinds(self):
        return [kind for kind, _, _ in self.items]

    def stages(self):
        return [(data["stage"], data["status"]) for kind, _, data in self.items if kind == "stage"]

    def of(self, kind):
        return [(text, data) for k, text, data in self.items if k == kind]


class FlowEventsTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"ARM_SUBSCRIPTION_ID": "sub-id"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.recorder = Recorder()
        self.addCleanup(self.recorder.close)

    def test_apply_emits_the_whole_sequence(self):
        ok, _ = run_quiet(app.review_and_apply, Path("."), False, fake_tf(), answers("yes"))
        self.assertTrue(ok)
        self.assertEqual(self.recorder.kinds(), ["stage", "plan", "confirm", "stage", "output", "stage"])
        self.assertEqual(self.recorder.stages(),
                         [("plan", "running"), ("apply", "running"), ("apply", "ok")])

    def test_plan_event_carries_the_counts(self):
        run_quiet(app.review_and_apply, Path("."), False, fake_tf(), answers("no"))
        (_, data), = self.recorder.of("plan")
        self.assertEqual((data["add"], data["change"], data["to_destroy"]), (3, 0, 0))
        self.assertFalse(data["destroy"])
        self.assertIn("Plan: 3 to add", data["output"])

    def test_confirm_event_names_the_action(self):
        run_quiet(app.review_and_apply, Path("."), False, fake_tf(), answers("no"))
        (_, data), = self.recorder.of("confirm")
        self.assertEqual(data["action"], "apply")
        self.assertIn("Escribe yes para crear", data["question"])

    def test_cancelling_never_reaches_apply(self):
        run_quiet(app.review_and_apply, Path("."), False, fake_tf(), answers("no"))
        self.assertNotIn(("apply", "running"), self.recorder.stages())
        self.assertTrue(any(data.get("cancelled") for _, data in self.recorder.of("info")))

    def test_failed_apply_is_reported_as_a_stage(self):
        run_quiet(app.review_and_apply, Path("."), False, fake_tf(apply_ok=False), answers("yes"))
        self.assertIn(("apply", "failed"), self.recorder.stages())

    def test_blocked_plan_emits_blocked_and_never_asks(self):
        run_quiet(app.review_and_apply, Path("."), False, fake_tf(plan_output=VM_PLAN), answers())
        self.assertEqual(len(self.recorder.of("blocked")), 1)
        self.assertNotIn("confirm", self.recorder.kinds())

    def test_missing_subscription_is_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            run_quiet(app.review_and_apply, Path("."), False, fake_tf(), answers())
        (_, data), = self.recorder.of("rejected")
        self.assertEqual(data["reason"], "missing_subscription")

    def test_destroy_plan_is_flagged(self):
        run_quiet(app.review_and_apply, Path("."), True, fake_tf(), answers("no"))
        (_, data), = self.recorder.of("plan")
        self.assertTrue(data["destroy"])
        (_, confirm_data), = self.recorder.of("confirm")
        self.assertEqual(confirm_data["action"], "destroy")


class SecretsEventsTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"ARM_SUBSCRIPTION_ID": "sub-id"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.recorder = Recorder()
        self.addCleanup(self.recorder.close)

    def test_passwords_travel_only_in_data_never_in_event_text(self):
        tf = fake_tf(passwords={"random_password.sql": "Zq9!secret"})
        _, printed = run_quiet(app.deploy, Path("."), tf, answers("yes", "no"))
        self.assertIn("Zq9!secret", printed)  # la consola sigue mostrándolas
        for _, text, _ in self.recorder.items:
            self.assertNotIn("Zq9!secret", text)
        (_, data), = self.recorder.of("secrets")
        self.assertEqual(data["secrets"], {"random_password.sql": "Zq9!secret"})

    def test_created_resources_are_listed(self):
        run_quiet(app.deploy, Path("."), fake_tf(), answers("yes", "no"))
        (_, data), = self.recorder.of("resources")
        self.assertEqual(data["resources"], ["azurerm_resource_group.main"])


if __name__ == "__main__":
    unittest.main()