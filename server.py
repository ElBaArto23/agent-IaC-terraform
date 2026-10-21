"""Servidor del front: ejecuta el agente y le cuenta a la página lo que pasa por dentro.

Un WebSocket (/ws) lleva dos cosas:
  servidor -> página: cada evento del agente  {"seq", "kind", "text", "data"}
  página -> servidor: {"type": "request", "text": ...}, {"type": "answer", "value": "yes"|"no"},
                      {"type": "destroy"} y {"type": "secrets"}

El agente corre en un hilo aparte (Terraform bloquea mientras trabaja). Cada confirmación que
antes era un input() espera aquí la respuesta de la página; solo un "yes" exacto cuenta, igual
que en la consola.

Seguridad: escucha solo en 127.0.0.1, rechaza orígenes que no sean la propia página (un sitio
cualquiera abierto en tu navegador no puede hablar con el agente), admite un token opcional
(ACCESS_TOKEN), ejecuta una sola cosa a la vez y no guarda las contraseñas en el historial.
"""

import asyncio
import hmac
import json
import os
import queue
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import app as agent
import events

ANSWER_TIMEOUT_S = 600  # si nadie responde una confirmación, se cancela (equivale a "no")
HISTORY_LIMIT = 500
MAX_REQUEST_CHARS = 2000
STATIC_DIR = Path(__file__).parent / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

AskFn = Callable[[str], str]


@dataclass
class Handlers:
    """Lo que el servidor sabe hacer. Se puede reemplazar en las pruebas."""

    request: Callable[[str, AskFn], None]
    destroy: Callable[[AskFn], None]
    secrets: Callable[[], None]
    has_resources: Callable[[], bool]


def default_handlers() -> Handlers:
    def request(text: str, input_fn: AskFn) -> None:
        if agent.handle_request(text) is not None:
            agent.deploy(input_fn=input_fn)

    def destroy(input_fn: AskFn) -> None:
        agent.review_and_apply(agent.OUTPUT_DIR, destroy=True, input_fn=input_fn)

    def secrets() -> None:
        if not agent.show_secrets(agent.OUTPUT_DIR, agent.terraform_runner):
            events.emit("info", "No hay contraseñas generadas en el estado.")

    def has_resources() -> bool:
        return agent.terraform_runner.has_resources(agent.OUTPUT_DIR)

    return Handlers(request, destroy, secrets, has_resources)


def dumps(message: dict[str, Any]) -> str:
    return json.dumps(message, default=str, ensure_ascii=False)


def origin_allowed(headers) -> bool:
    """Solo la propia página puede abrir el WebSocket (evita que otro sitio del navegador lo use)."""
    origin = headers.get("origin")
    if not origin:  # no es un navegador (una prueba o una herramienta): ningún sitio ajeno lo usa
        return True
    return urlparse(origin).netloc == headers.get("host", "")


def token_ok(expected: str, given: str) -> bool:
    return not expected or hmac.compare_digest(expected, given)


class Hub:
    """Conecta los eventos del agente (hilo de trabajo) con las páginas abiertas (asyncio)."""

    def __init__(self, handlers: Handlers):
        self.handlers = handlers
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subscribers: list[asyncio.Queue] = []
        self.history: list[dict[str, Any]] = []
        self.pending: dict[str, Any] | None = None  # confirmación que espera respuesta
        self.answers: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self.busy = threading.Lock()
        self.seq = 0
        self._stop_listening = events.subscribe(self.publish)

    def close(self) -> None:
        self._stop_listening()

    # --- eventos del agente -> páginas -------------------------------------------------
    def publish(self, kind: str, text: str, data: dict[str, Any]) -> None:
        with self.lock:
            self.seq += 1
            message = {"seq": self.seq, "kind": kind, "text": text, "data": data}
            if kind == "confirm":
                self.pending = message
            if kind != "secrets":  # las contraseñas nunca se guardan para repetirlas después
                self.history.append(message)
                del self.history[:-HISTORY_LIMIT]
            targets = list(self.subscribers)
        if self.loop is not None:
            for inbox in targets:
                self.loop.call_soon_threadsafe(inbox.put_nowait, message)

    def join(self, inbox: asyncio.Queue) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        with self.lock:
            self.subscribers.append(inbox)
            return list(self.history), self.pending

    def leave(self, inbox: asyncio.Queue) -> None:
        with self.lock:
            if inbox in self.subscribers:
                self.subscribers.remove(inbox)
            nobody_left = not self.subscribers
            waiting = self.pending is not None
        if nobody_left and waiting:
            self.answers.put("no")  # nadie puede responder: se cancela, nunca se aplica solo

    def state(self) -> dict[str, Any]:
        with self.lock:
            pending = self.pending
        return {"busy": self.busy.locked(), "pending": pending, "has_resources": self.handlers.has_resources()}

    # --- páginas -> agente --------------------------------------------------------------
    def ask(self, question: str) -> str:
        """input_fn del agente: espera la respuesta que llega de la página."""
        try:
            return self.answers.get(timeout=ANSWER_TIMEOUT_S)
        except queue.Empty:
            return "no"
        finally:
            with self.lock:
                self.pending = None

    def handle(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "answer":
            if self.pending is not None:
                self.answers.put(str(message.get("value", "")).strip())
        elif kind == "request":
            text = str(message.get("text", ""))[:MAX_REQUEST_CHARS]
            command = text.strip().lower()
            if command == "destroy":  # igual que en la consola: no es una petición para el modelo
                self._run(lambda: self.handlers.destroy(self.ask))
            elif command == "secretos":
                self._run(self.handlers.secrets)
            else:
                self._run(lambda: self.handlers.request(text, self.ask))
        elif kind == "destroy":
            self._run(lambda: self.handlers.destroy(self.ask))
        elif kind == "secrets":
            self._run(self.handlers.secrets)

    def _run(self, job: Callable[[], None]) -> None:
        if not self.busy.acquire(blocking=False):
            self.publish("busy", "Ya hay algo en ejecución; espera a que termine.", {})
            return
        while not self.answers.empty():  # respuestas viejas no deben contestar una pregunta nueva
            self.answers.get_nowait()

        def work() -> None:
            try:
                events.emit("working")
                job()
            except Exception as error:  # el agente no debe tumbar el servidor
                events.emit("error", f"Error inesperado: {error}", error=type(error).__name__)
            finally:
                events.emit("idle")
                self.busy.release()

        threading.Thread(target=work, daemon=True).start()


def create_app(hub: Hub, token: str = ""):
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse

    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @api.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    @api.websocket("/ws")
    async def websocket_endpoint(socket: WebSocket) -> None:
        if not origin_allowed(socket.headers) or not token_ok(token, socket.query_params.get("token", "")):
            await socket.close(code=1008)
            return
        await socket.accept()
        hub.loop = asyncio.get_running_loop()
        inbox: asyncio.Queue = asyncio.Queue()
        history, _ = hub.join(inbox)
        for message in history:
            await socket.send_text(dumps({**message, "replay": True}))
        # "state" va al final del historial: le dice a la página que ya está al día y qué falta por responder
        await socket.send_text(dumps({"kind": "state", "text": "", "data": hub.state()}))

        async def pump() -> None:
            while True:
                await socket.send_text(dumps(await inbox.get()))

        sender = asyncio.create_task(pump())
        try:
            while True:
                try:
                    message = json.loads(await socket.receive_text())
                except ValueError:
                    continue
                if isinstance(message, dict):
                    hub.handle(message)
        except WebSocketDisconnect:
            pass
        finally:
            sender.cancel()
            hub.leave(inbox)

    return api


def ensure_output_dir(path: Path) -> str | None:
    """Crea la carpeta de trabajo y comprueba que se puede escribir. Devuelve el problema, o None."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as error:
        return (f"No puedo escribir en la carpeta de trabajo {path}: {error}. "
                "Si es un volumen montado, revisa sus permisos (ver docs/despliegue-container-apps.md).")
    return None


def main() -> None:
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    token = os.environ.get("ACCESS_TOKEN", "")
    if host not in LOCAL_HOSTS and not token:
        raise SystemExit("Para escuchar fuera de 127.0.0.1 define ACCESS_TOKEN (y pon un login delante).")
    problem = ensure_output_dir(agent.OUTPUT_DIR)
    if problem:
        raise SystemExit(problem)
    events.set_console(False)  # el servidor habla con la página, no con la consola
    hub = Hub(default_handlers())
    # El token no se imprime nunca: en un contenedor esta línea acaba en los registros.
    hint = "  (abre la página añadiendo ?token=<tu ACCESS_TOKEN>)" if token else ""
    print(f"Agente en http://{host}:{port}{hint}  (Ctrl+C para salir)")
    uvicorn.run(create_app(hub, token), host=host, port=port, access_log=False)


if __name__ == "__main__":
    main()