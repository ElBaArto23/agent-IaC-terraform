"""Orquestador del agente: coordina los demás archivos.

Fase 1: pedir la frase, llamar al modelo, limpiar la respuesta y guardar output/main.tf.
Fase 2: lista cerrada de recursos, fmt/init/validate y reintento con el error.
Fase 3: terraform plan, confirmación escrita ("yes") y apply; después ofrece destruir.
"""

import os
import re
import types
from pathlib import Path
from typing import Callable

import terraform_runner
from llm import ask_model
from plan_review import analyze_plan
from prompt_template import OUT_OF_SCOPE, SYSTEM_PROMPT
from resource_check import check_code
from terraform_runner import Result, check_with_terraform

OUTPUT_DIR = Path(__file__).parent / "output"
MAX_REQUEST_CHARS = 500
MAX_RETRIES = 2

Ask = Callable[[str, str], str]
Check = Callable[[Path], Result]

# Marcas de markdown que el modelo a veces pone alrededor del código (```hcl ... ```)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n|\n?```\s*$")


def clean_response(text: str) -> str:
    """Barrera 2: quita las marcas de markdown alrededor del código."""
    return _FENCE_RE.sub("", text.strip()).strip()


def is_out_of_scope(text: str) -> bool:
    """Barrera 1: el modelo respondió que la petición está fuera del alcance."""
    return text.strip().strip("\"'. ") == OUT_OF_SCOPE


def generate_tf(request: str, ask: Ask = ask_model) -> str | None:
    """Devuelve el código HCL limpio, o None si la petición está fuera de alcance."""
    cleaned = clean_response(ask(SYSTEM_PROMPT, request))
    if not cleaned or is_out_of_scope(cleaned):
        return None
    return cleaned


def save_tf(code: str, output_dir: Path = OUTPUT_DIR) -> Path:
    output_dir.mkdir(exist_ok=True)
    path = output_dir / "main.tf"
    path.write_text(code + "\n", encoding="utf-8")
    return path


def build_fix_request(request: str, code: str, error: str) -> str:
    return (
        f"Petición original: {request}\n\n"
        f"El main.tf que devolviste:\n{code}\n\n"
        f"Terraform devolvió este error:\n{error}\n\n"
        "Corrige el código y devuelve el main.tf completo, siguiendo las mismas reglas."
    )


def _report_problems(problems: list[str]) -> None:
    print("El código generado no cumple la lista de recursos permitidos y se descartó:")
    for problem in problems:
        print(f"  - {problem}")


def validate_with_retries(request: str, code: str, output_dir: Path, ask: Ask, check: Check) -> Path | None:
    """Guarda el código, lo valida con Terraform y, si falla, devuelve el error al modelo.

    Devuelve la ruta de main.tf si pasó la validación, o None si el agente se detuvo.
    """
    retries = 0
    while True:
        path = save_tf(code, output_dir)
        result = check(output_dir)
        code = path.read_text(encoding="utf-8").strip()  # fmt puede haber reformateado el archivo

        if result.ok:
            print("\n✔ Pasó terraform fmt y validate.")
            return path

        print(f"\n✘ Falló terraform {result.stage}:\n{result.output}")
        if result.stage == "init":
            print("No es un error del código (suele ser la red o los providers); no se reintenta.")
            return None
        if retries >= MAX_RETRIES:
            print(f"\nNo se pudo corregir tras {MAX_RETRIES} reintentos. El agente se detiene; "
                  f"el último intento quedó en {path} para que lo revises.")
            return None

        retries += 1
        print(f"\nLe devuelvo el error al modelo (reintento {retries} de {MAX_RETRIES})...")
        fixed = generate_tf(build_fix_request(request, code, result.output), ask)
        if fixed is None:
            print("El modelo no devolvió código corregido. El agente se detiene.")
            return None
        problems = check_code(fixed)
        if problems:
            _report_problems(problems)
            return None
        code = fixed


def handle_request(request: str, output_dir: Path = OUTPUT_DIR, ask: Ask = ask_model,
                   check: Check = check_with_terraform) -> Path | None:
    """Flujo de una petición. Devuelve la ruta de main.tf si quedó validado, o None."""
    request = request.strip()
    if not request:
        return None
    if len(request) > MAX_REQUEST_CHARS:
        print(f"La petición es demasiado larga (máximo {MAX_REQUEST_CHARS} caracteres).")
        return None

    code = generate_tf(request, ask)
    if code is None:
        print("Esa petición está fuera del alcance: solo creo un grupo de recursos "
              "y una cuenta de almacenamiento. No se guardó nada.")
        return None

    problems = check_code(code)
    if problems:
        _report_problems(problems)
        return None

    path = validate_with_retries(request, code, output_dir, ask, check)
    if path is not None:
        print(f"\nCódigo validado en {path}:\n")
        print(path.read_text(encoding="utf-8"))
    return path


def confirm(question: str, input_fn: Callable[[str], str] = input) -> bool:
    """Barrera 6: solo la palabra 'yes' completa cuenta como confirmación."""
    return input_fn(question).strip() == "yes"


def review_and_apply(output_dir: Path = OUTPUT_DIR, destroy: bool = False,
                     tf: types.ModuleType = terraform_runner,
                     input_fn: Callable[[str], str] = input) -> bool:
    """terraform plan -> la persona lo lee -> 'yes' -> apply del MISMO plan.

    Con destroy=True hace lo mismo para destruir lo creado. Devuelve True solo si
    el apply terminó bien.
    """
    verb = "destruir" if destroy else "crear"

    if not os.environ.get("ARM_SUBSCRIPTION_ID"):
        print("Falta ARM_SUBSCRIPTION_ID en el .env (el ID de tu suscripción de Azure). "
              "No se ejecutó nada.")
        return False

    try:
        print(f"\nGenerando el plan para {verb} (no crea nada todavía)...")
        plan = tf.plan(output_dir, destroy=destroy)
        if not plan.ok:
            print(f"\n✘ Falló terraform plan:\n{plan.output}")
            print("\nRevisa que hayas ejecutado `az login` y que ARM_SUBSCRIPTION_ID sea el de tu suscripción.")
            return False

        info = analyze_plan(plan.output, destroying=destroy)
        print(f"\n{plan.output}\n")

        if info.blocked:
            for problem in info.blocked:
                print(f"⛔ {problem}")
            print("No se continúa. No se creó ni se destruyó nada.")
            return False
        if info.no_changes:
            print("No hay cambios: no hay nada que " + verb + ".")
            return False
        for warning in info.warnings:
            print(f"⚠ {warning}\n")

        question = (f"Lee el plan de arriba (puedes revisar el portal antes de responder).\n"
                    f"Escribe yes para {verb}; cualquier otra cosa cancela: ")
        if not confirm(question, input_fn):
            print("Cancelado. No se creó ni se destruyó nada.")
            return False

        result = tf.apply(output_dir)
        print(result.output)
        if not result.ok:
            print("\n✘ Falló terraform apply. Revisa el error de arriba; puede haber quedado algo a medias.")
        return result.ok
    finally:
        tf.remove_plan_file(output_dir)


def deploy(output_dir: Path = OUTPUT_DIR, tf: types.ModuleType = terraform_runner,
           input_fn: Callable[[str], str] = input) -> None:
    """Plan -> confirmación -> apply, y después ofrece destruir lo creado."""
    if not review_and_apply(output_dir, destroy=False, tf=tf, input_fn=input_fn):
        return

    created = tf.state_list(output_dir)
    if created.ok and created.output:
        print("\nRecursos creados:\n" + created.output)
    print("\nPara no gastar crédito, destruye lo que creaste cuando termines de revisarlo.")
    if not review_and_apply(output_dir, destroy=True, tf=tf, input_fn=input_fn):
        print('Los recursos siguen creados. Para destruirlos más tarde, escribe "destroy" en el agente.')


def main() -> None:
    print("Agente de Terraform para Azure. Describe qué quieres crear (vacío para salir).")
    print('Escribe "destroy" para destruir lo que se haya creado.')
    if terraform_runner.has_resources(OUTPUT_DIR):
        print('\n⚠ Hay recursos creados por este agente que siguen existiendo. '
              'Escribe "destroy" para destruirlos y no gastar crédito.')
    while True:
        try:
            request = input("\n> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not request.strip():
            break
        if request.strip().lower() == "destroy":
            review_and_apply(OUTPUT_DIR, destroy=True)
            continue
        if handle_request(request) is not None:
            deploy()


if __name__ == "__main__":
    main()