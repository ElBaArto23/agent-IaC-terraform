"""Barrera 5: analiza la salida de `terraform plan` antes de pedir la confirmación.

No reemplaza la lectura humana del plan: solo marca lo que merece atención y bloquea
lo que no debería aparecer nunca.
"""

import re
from dataclasses import dataclass, field

from resource_check import ALLOWED_RESOURCES

_SUMMARY = re.compile(r"Plan:\s+(\d+) to add,\s+(\d+) to change,\s+(\d+) to destroy")
_ADDRESS = re.compile(r"^\s*#\s+(\S+)\s+(?:will|must) be", re.M)


@dataclass
class PlanInfo:
    add: int = 0
    change: int = 0
    destroy: int = 0
    no_changes: bool = False
    warnings: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


def _resource_type(address: str) -> str:
    parts = address.split(".")
    return f"data.{parts[1]}" if parts[0] == "data" and len(parts) > 1 else parts[0]


def analyze_plan(output: str, destroying: bool = False) -> PlanInfo:
    info = PlanInfo()

    if "No changes." in output:
        info.no_changes = True
        return info

    match = _SUMMARY.search(output)
    if not match:
        info.blocked.append("No pude leer el resumen del plan; por seguridad no se continúa.")
        return info
    info.add, info.change, info.destroy = (int(g) for g in match.groups())

    for address in _ADDRESS.findall(output):
        rtype = _resource_type(address)
        if rtype not in ALLOWED_RESOURCES:
            info.blocked.append(f"El plan toca un recurso no permitido: {address}.")

    if not destroying and (info.change or info.destroy):
        info.warnings.append(
            f"Este plan MODIFICA {info.change} y DESTRUYE {info.destroy} recurso(s) que ya existen "
            "en el estado de Terraform. Una creación nueva normal no debería hacerlo: "
            "lee con cuidado las líneas con ~, - o -/+ antes de confirmar."
        )
    return info