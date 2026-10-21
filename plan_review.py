"""Barrera 5: analiza la salida de `terraform plan` antes de pedir la confirmación.

No reemplaza la lectura humana del plan: solo marca lo que merece atención y bloquea
lo que no debería aparecer nunca.
"""

import re
from dataclasses import dataclass, field

from resource_check import ALLOWED_DATA, ALLOWED_RESOURCES

_SUMMARY = re.compile(r"Plan:\s+(\d+) to add,\s+(\d+) to change,\s+(\d+) to destroy")
_ADDRESS = re.compile(r"^\s*#\s+(\S+)\s+(?:will|must) be", re.M)


# Recursos que cobran mientras existen. No se dan precios (cambian): revisa el vigente en Azure.
COST_NOTES = {
    "azurerm_mssql_database": "La base de datos SQL cobra por hora mientras exista; destrúyela al terminar de probar.",
    "azurerm_linux_virtual_machine": "La máquina virtual cobra por hora mientras exista (aunque esté apagada, el disco y la IP siguen cobrando); destrúyela al terminar.",
    "azurerm_service_plan": "El plan de App Service cobra por hora si no es el nivel gratuito F1; destrúyelo al terminar.",
    "azurerm_cosmosdb_account": "Cosmos DB solo es gratis con free tier, y Azure permite una cuenta gratis por suscripción; si ya tienes otra, esta fallará.",
    "azurerm_log_analytics_workspace": "Log Analytics cobra por los datos que ingiere (el código limita el tope diario).",
    "azurerm_container_app": "Container Apps cobra por uso de CPU y memoria; el agente limita el tamaño y las réplicas.",
    "azurerm_key_vault": "Key Vault cobra por operaciones (centavos). Al destruirlo queda en borrado suave unos días y su nombre sigue reservado.",
}


@dataclass
class PlanInfo:
    add: int = 0
    change: int = 0
    destroy: int = 0
    no_changes: bool = False
    warnings: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    costs: list[str] = field(default_factory=list)


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
        if rtype.startswith("data.") and rtype[5:] in ALLOWED_DATA:
            continue
        if rtype not in ALLOWED_RESOURCES:
            info.blocked.append(f"El plan toca un recurso no permitido: {address}.")
        elif not destroying and rtype in COST_NOTES and COST_NOTES[rtype] not in info.costs:
            info.costs.append(COST_NOTES[rtype])

    if not destroying and (info.change or info.destroy):
        info.warnings.append(
            f"Este plan MODIFICA {info.change} y DESTRUYE {info.destroy} recurso(s) que ya existen "
            "en el estado de Terraform. Una creación nueva normal no debería hacerlo: "
            "lee con cuidado las líneas con ~, - o -/+ antes de confirmar."
        )
    return info