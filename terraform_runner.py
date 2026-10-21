"""Ejecuta Terraform y devuelve su salida y si falló.

Todos los comandos se ejecutan con una lista de argumentos y sin shell=True, así ningún
texto del modelo ni del usuario se interpreta como un comando.

El apply SIEMPRE aplica el plan guardado que la persona revisó (plan.tfplan), nunca uno
nuevo: así lo que se crea es exactamente lo que se leyó. Un plan guardado no vuelve a
preguntar, por eso la confirmación escrita vive en app.py y nunca se usa -auto-approve.
"""

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from config import OUTPUT_DIR

PLAN_FILE = "plan.tfplan"
TIMEOUT_S = 300
PLAN_TIMEOUT_S = 600
APPLY_TIMEOUT_S = 900


@dataclass
class Result:
    ok: bool
    output: str
    stage: str = ""  # fmt, init, validate, plan, apply o state: dónde falló (o terminó)


def _clean_env() -> dict[str, str]:
    """Entorno para Terraform: sin las credenciales del modelo de IA."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AZURE_AI_")}
    env["TF_IN_AUTOMATION"] = "1"
    return env


def run(args: list[str], cwd: Path = OUTPUT_DIR, stage: str = "", timeout: int = TIMEOUT_S) -> Result:
    exe = shutil.which("terraform")
    if exe is None:
        return Result(False, "No se encontró terraform en el PATH. Instálalo y reabre la terminal.", stage)
    try:
        proc = subprocess.run(
            [exe, *args],
            cwd=cwd,
            env=_clean_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Result(False, f"terraform {args[0]} tardó más de {timeout} segundos y se canceló.", stage)
    output = (proc.stdout + proc.stderr).strip()
    return Result(proc.returncode == 0, output, stage)


def check_with_terraform(cwd: Path = OUTPUT_DIR) -> Result:
    """Barrera 4: fmt, init y validate, en ese orden; se detiene en el primer fallo.

    fmt va primero porque detecta errores de sintaxis (que son del código, no de la red)
    y deja el archivo bien formateado. validate no se conecta a Azure.
    """
    steps = (
        ("fmt", ["fmt", "-no-color"]),
        ("init", ["init", "-input=false", "-no-color"]),
        ("validate", ["validate", "-no-color"]),
    )
    for stage, args in steps:
        result = run(args, cwd, stage)
        if not result.ok:
            return result
    return Result(True, "", "validate")


def plan(cwd: Path = OUTPUT_DIR, destroy: bool = False) -> Result:
    """terraform plan guardado en plan.tfplan. Con destroy=True, el plan de destrucción."""
    args = ["plan", "-input=false", "-no-color", f"-out={PLAN_FILE}"]
    if destroy:
        args.append("-destroy")
    return run(args, cwd, "plan", PLAN_TIMEOUT_S)


def apply(cwd: Path = OUTPUT_DIR) -> Result:
    """Aplica el plan guardado. Solo debe llamarse después de la confirmación escrita."""
    return run(["apply", "-input=false", "-no-color", PLAN_FILE], cwd, "apply", APPLY_TIMEOUT_S)


def state_list(cwd: Path = OUTPUT_DIR) -> Result:
    return run(["state", "list"], cwd, "state")


def remove_plan_file(cwd: Path = OUTPUT_DIR) -> None:
    """Borra el plan guardado para que nunca se aplique uno viejo."""
    (Path(cwd) / PLAN_FILE).unlink(missing_ok=True)


def generated_passwords(cwd: Path = OUTPUT_DIR) -> dict[str, str]:
    """{dirección: valor} de los random_password del estado.

    Lee el archivo de estado directamente: no ejecuta nada ni usa un `output`. Estos valores
    solo se muestran a la persona; nunca deben enviarse al modelo.
    """
    try:
        state = json.loads((Path(cwd) / "terraform.tfstate").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found: dict[str, str] = {}
    for resource in state.get("resources", []):
        if resource.get("mode") != "managed" or resource.get("type") != "random_password":
            continue
        instances = resource.get("instances", [])
        for index, instance in enumerate(instances):
            value = (instance.get("attributes") or {}).get("result")
            if value:
                address = f"random_password.{resource.get('name')}"
                found[address if len(instances) == 1 else f"{address}[{index}]"] = value
    return found


def has_resources(cwd: Path = OUTPUT_DIR) -> bool:
    """True si el estado de Terraform registra recursos creados (lee el archivo, no ejecuta nada)."""
    try:
        state = json.loads((Path(cwd) / "terraform.tfstate").read_text(encoding="utf-8"))
        return bool(state.get("resources"))
    except (OSError, ValueError):
        return False