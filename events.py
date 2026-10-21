"""Canal de salida del agente.

Todo lo que el agente le cuenta a la persona pasa por emit(). Por defecto se imprime en la
consola igual que siempre; el front se suscribe para recibir lo mismo como eventos
estructurados (tipo, texto y datos).

Un evento sin texto no imprime nada: sirve para avisar de un cambio de estado
(por ejemplo "validate: running") sin ensuciar la consola.
"""

from typing import Any, Callable

Listener = Callable[[str, str, dict[str, Any]], None]

_listeners: list[Listener] = []
_console = True


def subscribe(listener: Listener) -> Callable[[], None]:
    """Registra quién escucha los eventos. Devuelve una función para dejar de escuchar."""
    _listeners.append(listener)

    def stop() -> None:
        if listener in _listeners:
            _listeners.remove(listener)

    return stop


def set_console(enabled: bool) -> None:
    """Activa o apaga la impresión en consola (el front la apaga y usa solo los eventos)."""
    global _console
    _console = enabled


def emit(kind: str, text: str = "", **data: Any) -> None:
    """Cuenta algo a la persona: lo imprime (si hay texto) y se lo pasa a quien escuche."""
    if _console and text:
        print(text)
    for listener in list(_listeners):
        listener(kind, text, data)


def emit_secret(kind: str, console_text: str, **data: Any) -> None:
    """Como emit(), pero para valores secretos (contraseñas).

    La consola ve el texto completo, como siempre. Quien escucha recibe el texto vacío y los
    valores solo en `data`, para que un registro o un front que guarde el texto no los copie
    por accidente.
    """
    if _console and console_text:
        print(console_text)
    for listener in list(_listeners):
        listener(kind, "", data)