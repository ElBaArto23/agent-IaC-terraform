"""Pruebas del servidor del front con un agente falso: nada de esto toca Azure ni el modelo."""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
except ImportError as error:  # pragma: no cover - solo si faltan las dependencias del servidor
    raise unittest.SkipTest(f"Faltan las dependencias del servidor: {error}")

import events  # noqa: E402
import server  # noqa: E402


def receive_until(socket, kind: str) -> list[dict]:
    """Lee mensajes hasta ver uno del tipo pedido y devuelve todos los leídos."""
    seen = []
    while True:
        message = socket.receive_json()
        seen.append(message)
        if message["kind"] == kind:
            return seen


def wait_for(condition, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.01)
    return False


def make_hub(request=None, destroy=None, secrets=None, has_resources=False) -> server.Hub:
    handlers = server.Handlers(
        request=request or (lambda text, ask: None),
        destroy=destroy or (lambda ask: None),
        secrets=secrets or (lambda: None),
        has_resources=lambda: has_resources,
    )
    return server.Hub(handlers)


def asking_flow(result: list):
    """Agente falso: planifica, pide confirmación y anota la respuesta que recibió."""
    def flow(text, ask):
        events.emit("stage", "Planificando", stage="plan", status="running")
        events.emit("confirm", question="¿Crear?", action="apply")
        result.append(ask("¿Crear?"))
        events.emit("info", f"respuesta: {result[-1]}")
    return flow


class ServerFlowTests(unittest.TestCase):
    def open(self, hub, token=""):
        self.addCleanup(hub.close)
        return TestClient(server.create_app(hub, token))

    def test_request_runs_and_waits_for_the_button(self):
        result = []
        client = self.open(make_hub(request=asking_flow(result)))
        with client.websocket_connect("/ws") as socket:
            self.assertEqual(socket.receive_json()["kind"], "state")
            socket.send_json({"type": "request", "text": "un grupo de recursos"})
            seen = receive_until(socket, "confirm")
            self.assertEqual([m["kind"] for m in seen], ["working", "stage", "confirm"])
            self.assertEqual(seen[-1]["data"]["action"], "apply")
            socket.send_json({"type": "answer", "value": "yes"})
            seen = receive_until(socket, "idle")
        self.assertEqual(result, ["yes"])
        self.assertIn("respuesta: yes", [m["text"] for m in seen])

    def test_state_reports_existing_resources(self):
        client = self.open(make_hub(has_resources=True))
        with client.websocket_connect("/ws") as socket:
            state = socket.receive_json()
        self.assertTrue(state["data"]["has_resources"])
        self.assertFalse(state["data"]["busy"])

    def test_second_request_while_busy_is_refused(self):
        result = []
        client = self.open(make_hub(request=asking_flow(result)))
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "request", "text": "uno"})
            receive_until(socket, "confirm")
            socket.send_json({"type": "request", "text": "dos"})
            seen = receive_until(socket, "busy")
            self.assertIn("Ya hay algo en ejecución", seen[-1]["text"])
            socket.send_json({"type": "answer", "value": "no"})
            receive_until(socket, "idle")
        self.assertEqual(result, ["no"])

    def test_closing_the_page_while_waiting_cancels(self):
        result = []
        done = threading.Event()
        flow = asking_flow(result)

        def request(text, ask):
            try:
                flow(text, ask)
            finally:
                done.set()

        client = self.open(make_hub(request=request))
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "request", "text": "uno"})
            receive_until(socket, "confirm")
        self.assertTrue(done.wait(3), "el agente se quedó esperando una respuesta que nunca llegará")
        self.assertEqual(result, ["no"])

    def test_an_error_in_the_agent_is_reported_and_the_server_keeps_working(self):
        def boom(text, ask):
            raise RuntimeError("Falta la variable de entorno X")

        hub = make_hub(request=boom)
        client = self.open(hub)
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "request", "text": "uno"})
            seen = receive_until(socket, "idle")
        errors = [m for m in seen if m["kind"] == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("Falta la variable de entorno X", errors[0]["text"])
        self.assertTrue(wait_for(lambda: not hub.busy.locked()))

    def test_typed_commands_do_not_go_to_the_model(self):
        calls = []
        hub = make_hub(
            request=lambda text, ask: calls.append(("request", text)),
            destroy=lambda ask: calls.append(("destroy", None)),
            secrets=lambda: calls.append(("secrets", None)),
        )
        client = self.open(hub)
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            for typed in ("destroy", "  Destroy ", "secretos", "SECRETOS", "crea un grupo"):
                socket.send_json({"type": "request", "text": typed})
                receive_until(socket, "idle")
        self.assertEqual(calls, [("destroy", None), ("destroy", None), ("secrets", None),
                                 ("secrets", None), ("request", "crea un grupo")])

    def test_answer_without_a_pending_question_is_ignored(self):
        hub = make_hub()
        client = self.open(hub)
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "answer", "value": "yes"})
            socket.send_json({"type": "request", "text": "algo"})
            receive_until(socket, "idle")
        self.assertTrue(hub.answers.empty())

    def test_garbage_messages_do_not_break_the_connection(self):
        client = self.open(make_hub())
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_text("esto no es json")
            socket.send_text("[1, 2, 3]")
            socket.send_json({"type": "desconocido"})
            socket.send_json({"type": "request", "text": "algo"})
            receive_until(socket, "idle")


class SecretsTests(unittest.TestCase):
    def test_secrets_reach_the_page_but_are_never_replayed(self):
        def secrets():
            events.emit_secret("secrets", "  random_password.sql = Zq9!secret",
                               secrets={"random_password.sql": "Zq9!secret"})

        hub = make_hub(secrets=secrets)
        self.addCleanup(hub.close)
        client = TestClient(server.create_app(hub))
        with client.websocket_connect("/ws") as socket:
            socket.receive_json()
            socket.send_json({"type": "secrets"})
            seen = receive_until(socket, "idle")
        shown = [m for m in seen if m["kind"] == "secrets"]
        self.assertEqual(shown[0]["data"]["secrets"], {"random_password.sql": "Zq9!secret"})
        self.assertEqual(shown[0]["text"], "")
        self.assertNotIn("secrets", [m["kind"] for m in hub.history])
        self.assertNotIn("Zq9!secret", str(hub.history))

        with client.websocket_connect("/ws") as socket:
            replayed = [socket.receive_json() for _ in hub.history]
            self.assertEqual(socket.receive_json()["kind"], "state")  # cierra el historial
        self.assertTrue(all(m["replay"] for m in replayed))
        self.assertNotIn("secrets", [m["kind"] for m in replayed])
        self.assertNotIn("Zq9!secret", str(replayed))


class SecurityTests(unittest.TestCase):
    def test_foreign_origin_is_rejected(self):
        hub = make_hub()
        self.addCleanup(hub.close)
        client = TestClient(server.create_app(hub))
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect("/ws", headers={"origin": "http://evil.example"}):
                pass

    def test_same_origin_is_accepted(self):
        hub = make_hub()
        self.addCleanup(hub.close)
        client = TestClient(server.create_app(hub))
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as socket:
            self.assertEqual(socket.receive_json()["kind"], "state")

    def test_token_is_required_when_configured(self):
        hub = make_hub()
        self.addCleanup(hub.close)
        client = TestClient(server.create_app(hub, token="s3cret"))
        for url in ("/ws", "/ws?token=otro"):
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect(url):
                    pass
        with client.websocket_connect("/ws?token=s3cret") as socket:
            self.assertEqual(socket.receive_json()["kind"], "state")

    def test_origin_and_token_helpers(self):
        self.assertTrue(server.origin_allowed({"host": "127.0.0.1:8000"}))
        self.assertTrue(server.origin_allowed({"origin": "http://127.0.0.1:8000", "host": "127.0.0.1:8000"}))
        self.assertFalse(server.origin_allowed({"origin": "http://evil.example", "host": "127.0.0.1:8000"}))
        self.assertTrue(server.token_ok("", ""))
        self.assertFalse(server.token_ok("abc", ""))
        self.assertTrue(server.token_ok("abc", "abc"))

    def test_index_page_is_served(self):
        hub = make_hub()
        self.addCleanup(hub.close)
        response = TestClient(server.create_app(hub)).get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("WebSocket", response.text)

    def test_api_docs_are_not_exposed(self):
        hub = make_hub()
        self.addCleanup(hub.close)
        client = TestClient(server.create_app(hub))
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(client.get(path).status_code, 404, path)

    def test_startup_never_prints_the_access_token(self):
        import contextlib
        import io
        buffer = io.StringIO()
        self.addCleanup(events.set_console, True)
        with mock.patch.dict("os.environ", {"HOST": "0.0.0.0", "ACCESS_TOKEN": "secreto-123"}), \
                mock.patch.object(server, "ensure_output_dir", return_value=None), \
                mock.patch("uvicorn.run") as run, \
                contextlib.redirect_stdout(buffer):
            server.main()
        run.assert_called_once()
        self.assertNotIn("secreto-123", buffer.getvalue())
        self.assertIn("?token=", buffer.getvalue())

    def test_listening_outside_localhost_requires_a_token(self):
        with mock.patch.dict("os.environ", {"HOST": "0.0.0.0", "ACCESS_TOKEN": ""}):
            with self.assertRaises(SystemExit):
                server.main()


class DefaultHandlersTests(unittest.TestCase):
    """Los manejadores reales llaman al agente; aquí se reemplaza el agente para no tocar nada."""

    def test_request_deploys_only_when_the_code_was_validated(self):
        handlers = server.default_handlers()
        with mock.patch.object(server.agent, "handle_request", return_value=Path("main.tf")) as handle, \
                mock.patch.object(server.agent, "deploy") as deploy:
            ask = lambda question: "yes"  # noqa: E731
            handlers.request("un grupo", ask)
        handle.assert_called_once_with("un grupo")
        deploy.assert_called_once_with(input_fn=ask)

    def test_request_does_not_deploy_when_validation_fails(self):
        handlers = server.default_handlers()
        with mock.patch.object(server.agent, "handle_request", return_value=None), \
                mock.patch.object(server.agent, "deploy") as deploy:
            handlers.request("algo raro", lambda question: "no")
        deploy.assert_not_called()

    def test_destroy_goes_through_the_review(self):
        handlers = server.default_handlers()
        ask = lambda question: "no"  # noqa: E731
        with mock.patch.object(server.agent, "review_and_apply") as review:
            handlers.destroy(ask)
        review.assert_called_once_with(server.agent.OUTPUT_DIR, destroy=True, input_fn=ask)


if __name__ == "__main__":
    unittest.main()