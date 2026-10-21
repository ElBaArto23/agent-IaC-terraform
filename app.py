"""Orquestador del agente: coordina los demás archivos.

Fase 1: pedir la frase, llamar al modelo, limpiar la respuesta y guardar output/main.tf.
Fase 2: lista cerrada de recursos, fmt/init/validate y reintento con el error.
Fase 3: terraform plan, confirmación escrita ("yes") y apply; después ofrece destruir.
Extras: cambio de región si Azure no puede crear en la pedida y contraseñas generadas por Terraform.
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
from regions import MAX_REGION_SWITCHES, current_region, fallback_regions, is_region_error, next_region, switch_region
from resource_check import check_code, check_names
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


def check_everything(output_dir: Path) -> Result:
    """Revisa los nombres y después fmt, init y validate sobre el main.tf guardado.

    Los problemas de nombres se tratan como un error más de validación: vuelven al modelo.
    """
    code = (output_dir / "main.tf").read_text(encoding="utf-8")
    problems = check_names(code)
    if problems:
        return Result(False, "\n".join(f"- {p}" for p in problems), "names")
    return check_with_terraform(output_dir)


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
            print("\n✔ Pasó la revisión de nombres y terraform fmt y validate.")
            return path

        what = "la revisión de nombres" if result.stage == "names" else f"terraform {result.stage}"
        print(f"\n✘ Falló {what}:\n{result.output}")
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
                   check: Check = check_everything) -> Path | None:
    """Flujo de una petición. Devuelve la ruta de main.tf si quedó validado, o None."""
    request = request.strip()
    if not request:
        return None
    if len(request) > MAX_REQUEST_CHARS:
        print(f"La petición es demasiado larga (máximo {MAX_REQUEST_CHARS} caracteres).")
        return None

    code = generate_tf(request, ask)
    if code is None:
        print("Esa petición está fuera del alcance: solo creo un grupo de recursos, una cuenta de "
              "almacenamiento, una red básica, Azure SQL, Cosmos DB, Key Vault, una "
              "máquina virtual Linux, App Service y Container Apps. No se guardó nada.")
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


APPLIED = "applied"
APPLY_FAILED = "apply_failed"
NOT_APPLIED = "not_applied"  # no se pidió, se canceló, el plan falló o se bloqueó: no se tocó Azure


def _review(output_dir: Path, destroy: bool, tf: types.ModuleType,
            input_fn: Callable[[str], str]) -> tuple[str, str]:
    """terraform plan -> la persona lo lee -> 'yes' -> apply del MISMO plan.

    Con destroy=True hace lo mismo para destruir lo creado. Devuelve (estado, salida del apply):
    APPLIED, APPLY_FAILED (el apply se ejecutó y falló: puede haber quedado algo a medias) o
    NOT_APPLIED.
    """
    verb = "destruir" if destroy else "crear"

    if not os.environ.get("ARM_SUBSCRIPTION_ID"):
        print("Falta ARM_SUBSCRIPTION_ID en el .env (el ID de tu suscripción de Azure). "
              "No se ejecutó nada.")
        return NOT_APPLIED, ""

    try:
        print(f"\nGenerando el plan para {verb} (no crea nada todavía)...")
        plan = tf.plan(output_dir, destroy=destroy)
        if not plan.ok:
            print(f"\n✘ Falló terraform plan:\n{plan.output}")
            print("\nRevisa que hayas ejecutado `az login` y que ARM_SUBSCRIPTION_ID sea el de tu suscripción.")
            return NOT_APPLIED, ""

        info = analyze_plan(plan.output, destroying=destroy)
        print(f"\n{plan.output}\n")

        if info.blocked:
            for problem in info.blocked:
                print(f"⛔ {problem}")
            print("No se continúa. No se creó ni se destruyó nada.")
            return NOT_APPLIED, ""
        if info.no_changes:
            print("No hay cambios: no hay nada que " + verb + ".")
            return NOT_APPLIED, ""
        for warning in info.warnings:
            print(f"⚠ {warning}\n")
        for cost in info.costs:
            print(f"💲 {cost}\n")

        question = (f"Lee el plan de arriba (puedes revisar el portal antes de responder).\n"
                    f"Escribe yes para {verb}; cualquier otra cosa cancela: ")
        if not confirm(question, input_fn):
            print("Cancelado. No se creó ni se destruyó nada.")
            return NOT_APPLIED, ""

        result = tf.apply(output_dir)
        print(result.output)
        if not result.ok:
            print("\n✘ Falló terraform apply. Revisa el error de arriba.")
            return APPLY_FAILED, result.output
        return APPLIED, result.output
    finally:
        tf.remove_plan_file(output_dir)


def review_and_apply(output_dir: Path = OUTPUT_DIR, destroy: bool = False,
                     tf: types.ModuleType = terraform_runner,
                     input_fn: Callable[[str], str] = input) -> bool:
    """Como _review, pero devuelve True solo si el apply terminó bien."""
    return _review(output_dir, destroy, tf, input_fn)[0] == APPLIED


def offer_cleanup(output_dir: Path, tf: types.ModuleType, input_fn: Callable[[str], str]) -> bool:
    """Tras un apply fallido: muestra lo que quedó creado y ofrece destruirlo (con yes escrito).

    Devuelve True solo si ya no queda nada creado (no había nada o se destruyó bien).
    """
    left = tf.state_list(output_dir)
    if not left.ok:
        print("\nNo pude leer el estado de Terraform; revisa el portal por si quedó algo creado.")
        return False
    if not left.output:
        print("\nNo quedó nada registrado en el estado de Terraform.")
        return True
    print("\nEl apply falló a medias y esto quedó creado (y gastando crédito):\n" + left.output)
    if _review(output_dir, True, tf, input_fn)[0] == APPLIED:
        return True
    print('Siguen creados. Para destruirlos más tarde, escribe "destroy" en el agente.')
    return False


def show_secrets(output_dir: Path, tf: types.ModuleType) -> bool:
    """Muestra a la persona las contraseñas que generó Terraform. Nunca se envían al modelo."""
    reader = getattr(tf, "generated_passwords", None)
    secrets = reader(output_dir) if reader else {}
    if not secrets:
        return False
    print("\n🔑 Contraseñas generadas por Terraform (guárdalas ahora en un gestor de contraseñas):")
    for address, value in secrets.items():
        print(f"  {address} = {value}")
    print("   Usuarios: sqladmin (servidor SQL) y azureuser (máquina virtual).")
    print("⚠ Quedan en texto plano en output/terraform.tfstate (ya está en .gitignore). No las compartas, "
          "no grabes la pantalla con ellas visibles y no subas ni sincronices la carpeta output/. "
          'Puedes verlas de nuevo con el comando "secretos" mientras existan los recursos.')
    return True


def _switch_region(output_dir: Path, tried: list[str], switches: int,
                   input_fn: Callable[[str], str]) -> bool:
    """Propone y, con un yes escrito, aplica el cambio de región en main.tf. True si se cambió."""
    if switches >= MAX_REGION_SWITCHES:
        print(f"Ya se intentó cambiar de región {MAX_REGION_SWITCHES} veces; el agente se detiene.")
        return False
    path = output_dir / "main.tf"
    try:
        code = path.read_text(encoding="utf-8")
    except OSError:
        return False
    old = current_region(code)
    if old is None:
        print("No encontré una región escrita en el código; no puedo cambiarla automáticamente.")
        return False
    if old not in tried:
        tried.append(old)
    new = next_region(tried, fallback_regions())
    if new is None:
        print("No quedan regiones alternas por probar.")
        return False
    print(f"\nParece que Azure no puede crear esto en {old} (disponibilidad, cuota o política).")
    question = (f"¿Probar en {new}? Los datos quedarían en esa región (puede importar por residencia "
                f"de datos o latencia).\nEscribe yes para cambiar a {new}; cualquier otra cosa cancela: ")
    if not confirm(question, input_fn):
        print("No se cambió la región.")
        return False
    path.write_text(switch_region(code, old, new), encoding="utf-8")
    tried.append(new)
    print(f"Región cambiada a {new} en {path}. Genero un plan nuevo para que lo revises.")
    return True


def deploy(output_dir: Path = OUTPUT_DIR, tf: types.ModuleType = terraform_runner,
           input_fn: Callable[[str], str] = input) -> None:
    """Plan -> confirmación -> apply (con cambio de región si hace falta), y ofrece destruir."""
    tried: list[str] = []
    switches = 0
    while True:
        status, output = _review(output_dir, False, tf, input_fn)
        if status != APPLY_FAILED:
            break
        clean = offer_cleanup(output_dir, tf, input_fn)
        if not (clean and is_region_error(output)):
            return
        if not _switch_region(output_dir, tried, switches, input_fn):
            return
        switches += 1
    if status != APPLIED:
        return

    created = tf.state_list(output_dir)
    if created.ok and created.output:
        print("\nRecursos creados:\n" + created.output)
    show_secrets(output_dir, tf)
    print("\nPara no gastar crédito, destruye lo que creaste cuando termines de revisarlo.")
    if not review_and_apply(output_dir, destroy=True, tf=tf, input_fn=input_fn):
        print('Los recursos siguen creados. Para destruirlos más tarde, escribe "destroy" en el agente.')


def main() -> None:
    print("Agente de Terraform para Azure. Describe qué quieres crear (vacío para salir).")
    print('Escribe "destroy" para destruir lo que se haya creado y "secretos" para ver las contraseñas generadas.')
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
        command = request.strip().lower()
        if command == "destroy":
            review_and_apply(OUTPUT_DIR, destroy=True)
            continue
        if command == "secretos":
            if not show_secrets(OUTPUT_DIR, terraform_runner):
                print("No hay contraseñas generadas en el estado.")
            continue
        if handle_request(request) is not None:
            deploy()


if __name__ == "__main__":
    main()