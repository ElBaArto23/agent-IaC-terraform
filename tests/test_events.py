"""Pruebas del canal de eventos: nada de esto toca Azure ni Terraform."""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import events  # noqa: E402


def capture(function, *args, **kwargs) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        function(*args, **kwargs)
    return buffer.getvalue()


class EventsTests(unittest.TestCase):
    def tearDown(self):
        events.set_console(True)

    def test_prints_text_to_console_by_default(self):
        self.assertEqual(capture(events.emit, "info", "hola"), "hola\n")

    def test_event_without_text_prints_nothing(self):
        self.assertEqual(capture(events.emit, "stage", stage="validate", status="running"), "")

    def test_listener_receives_kind_text_and_data(self):
        received = []
        stop = events.subscribe(lambda kind, text, data: received.append((kind, text, data)))
        self.addCleanup(stop)
        capture(events.emit, "stage", "validando", stage="validate", status="running")
        self.assertEqual(received, [("stage", "validando", {"stage": "validate", "status": "running"})])

    def test_console_can_be_silenced_and_listener_still_hears(self):
        received = []
        stop = events.subscribe(lambda kind, text, data: received.append(text))
        self.addCleanup(stop)
        events.set_console(False)
        self.assertEqual(capture(events.emit, "info", "solo para el front"), "")
        self.assertEqual(received, ["solo para el front"])

    def test_stop_listening(self):
        received = []
        stop = events.subscribe(lambda kind, text, data: received.append(text))
        stop()
        capture(events.emit, "info", "nadie escucha")
        self.assertEqual(received, [])


if __name__ == "__main__":
    unittest.main()