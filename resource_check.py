"""Barrera 3: lista cerrada de lo que el código generado puede contener.

Todo lo que no esté explícitamente permitido se rechaza. Se revisa ANTES de ejecutar
cualquier comando de Terraform, porque incluso `init` y `validate` leen el código.
"""

import re

ALLOWED_RESOURCES = {"azurerm_resource_group", "azurerm_storage_account", "random_string"}
ALLOWED_TOP_LEVEL = {"terraform", "provider", "resource"}

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_COMMENT = re.compile(r"(#|//).*$")
_TOP_WORD = re.compile(r"^\s*([A-Za-z_][\w-]*)")
_RESOURCE_TYPE = re.compile(r'^\s*resource\s+"([^"]+)"')
_PROVIDER_NAME = re.compile(r'^\s*provider\s+"([^"]+)"')
# Ejecución de comandos, estado remoto y heredocs
_FORBIDDEN_WORDS = re.compile(r"\b(provisioner|local-exec|remote-exec|backend)\b|<<")
# Funciones que leen archivos o rutas de la máquina donde corre el agente
_FORBIDDEN_FUNCS = re.compile(r"\b(file|filebase64|templatefile|fileset|abspath|pathexpand)\s*\(")


def check_code(code: str) -> list[str]:
    """Devuelve la lista de problemas encontrados; vacía si el código es aceptable."""
    problems: list[str] = []
    depth = 0

    for number, raw in enumerate(_BLOCK_COMMENT.sub("", code).splitlines(), start=1):
        # Sin textos ni comentarios, para contar llaves y detectar palabras sin falsos positivos
        line = _COMMENT.sub("", _STRING.sub('""', raw))

        if depth == 0 and line.strip() and not line.strip().startswith("}"):
            match = _TOP_WORD.match(line)
            kind = match.group(1) if match else line.strip()
            if kind not in ALLOWED_TOP_LEVEL:
                problems.append(f"Línea {number}: bloque no permitido '{kind}'.")
            elif kind == "resource":
                found = _RESOURCE_TYPE.match(raw)
                rtype = found.group(1) if found else "(ilegible)"
                if rtype not in ALLOWED_RESOURCES:
                    problems.append(f"Línea {number}: recurso no permitido '{rtype}'.")
            elif kind == "provider":
                found = _PROVIDER_NAME.match(raw)
                if not found or found.group(1) != "azurerm":
                    problems.append(f"Línea {number}: solo se permite el provider azurerm.")

        if _FORBIDDEN_WORDS.search(line):
            problems.append(f"Línea {number}: construcción no permitida (provisioner, backend o heredoc).")
        if _FORBIDDEN_FUNCS.search(raw):
            problems.append(f"Línea {number}: función no permitida (lee archivos de la máquina).")

        depth += line.count("{") - line.count("}")
        if depth < 0:
            problems.append(f"Línea {number}: llaves desbalanceadas.")
            depth = 0

    if depth != 0:
        problems.append("Hay llaves sin cerrar.")
    return problems