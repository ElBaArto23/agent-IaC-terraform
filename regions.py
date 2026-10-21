"""Cambio de región cuando Azure no puede crear un recurso en la que se pidió.

Este módulo solo decide y edita texto: nunca ejecuta Terraform ni cambia nada sin que app.py
pida antes una confirmación escrita. La región nueva sale de una lista controlada por el
código (o por la variable AZURE_FALLBACK_REGIONS), nunca de lo que diga el modelo.
"""

import os
import re

# Orden de preferencia (Brasil y Suecia Central van primero porque suelen aceptar bases de datos). Se puede cambiar en el .env con
# AZURE_FALLBACK_REGIONS=eastus2,centralus,...
DEFAULT_FALLBACK = ("eastus2", "brazilsouth", "swedencentral", "centralus", "westus2", "westus3", "southcentralus")
MAX_REGION_SWITCHES = 2

# Errores del apply que indican un problema de la región (disponibilidad, capacidad,
# cuota o política de la suscripción) y no del código.
_REGION_ERRORS = re.compile(
    "|".join([
        r"RegionDoesNotAllowProvisioning",
        r"ProvisioningDisabled",
        r"is not accepting (?:the )?creation of new",
        r"LocationNotAvailableForResourceType",
        r"SkuNotAvailable",
        r"NotAvailableForSubscription",
        r"RequestDisallowedByAzure",
        r"ResourceQuotaExceeded",
        r"QuotaExceeded",
        r"high demand",
        r"does not have (?:enough )?capacity",
        r"not available in (?:the )?(?:location|region)",
    ]),
    re.IGNORECASE,
)
_LOCATION_LITERAL = re.compile(r'(\blocation\s*=\s*)"([a-z0-9]+)"')
_REGION_NAME = re.compile(r"[a-z0-9]{3,30}")


def fallback_regions() -> list[str]:
    raw = os.environ.get("AZURE_FALLBACK_REGIONS", "")
    names = [r.strip().lower() for r in raw.split(",") if r.strip()]
    valid = [r for r in names if _REGION_NAME.fullmatch(r)]
    return valid or list(DEFAULT_FALLBACK)


def is_region_error(output: str) -> bool:
    """True si la salida del apply parece un problema de la región y no del código."""
    return bool(_REGION_ERRORS.search(output or ""))


def current_region(code: str) -> str | None:
    """La región escrita como texto fijo en el código (la del grupo de recursos), si la hay."""
    found = _LOCATION_LITERAL.search(code)
    return found.group(2) if found else None


def next_region(tried: list[str], candidates: list[str]) -> str | None:
    """La primera región de la lista que todavía no se intentó."""
    for region in candidates:
        if region not in tried:
            return region
    return None


def switch_region(code: str, old: str, new: str) -> str:
    """Cambia todos los `location = "old"` escritos como texto fijo; las referencias no se tocan."""
    if not _REGION_NAME.fullmatch(new):
        raise ValueError(f"Región no válida: {new!r}")
    return _LOCATION_LITERAL.sub(
        lambda m: f'{m.group(1)}"{new}"' if m.group(2) == old else m.group(0), code)