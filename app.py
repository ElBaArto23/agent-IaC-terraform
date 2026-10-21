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
from config import OUTPUT_DIR
from events import emit, emit_secret
from llm import ask_model
from plan_review import analyze_plan
from prompt_template import OUT_OF_SCOPE, SYSTEM_PROMPT
from regions import MAX_REGION_SWITCHES, current_region, fallback_regions, is_region_error, next_region, switch_region
from resource_check import check_code, check_names
from terraform_runner import Result, check_with_terraform

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
    text = "El código generado no cumple la lista de recursos permitidos y se descartó:\n"
    text += "\n".join(f"  - {problem}" for problem in problems)
    emit("rejected", text, reason="not_allowed", problems=problems)


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
        emit("stage", stage="validate", status="running", attempt=retries)
        result = check(output_dir)
        code = path.read_text(encoding="utf-8").strip()  # fmt puede haber reformateado el archivo

        if result.ok:
            emit("stage", "\n✔ Pasó la revisión de nombres y terraform fmt y validate.",
                 stage="validate", status="ok", attempt=retries, code=code)
            return path

        what = "la revisión de nombres" if result.stage == "names" else f"terraform {result.stage}"
        emit("stage", f"\n✘ Falló {what}:\n{result.output}",
             stage=result.stage, status="failed", attempt=retries, output=result.output)
        if result.stage == "init":
            emit("info", "No es un error del código (suele ser la red o los providers); no se reintenta.")
            return None
        if retries >= MAX_RETRIES:
            emit("info", f"\nNo se pudo corregir tras {MAX_RETRIES} reintentos. El agente se detiene; "
                         f"el último intento quedó en {path} para que lo revises.")
            return None

        retries += 1
        emit("retry", f"\nLe devuelvo el error al modelo (reintento {retries} de {MAX_RETRIES})...",
             attempt=retries, max=MAX_RETRIES)
        fixed = generate_tf(build_fix_request(request, code, result.output), ask)
        if fixed is None:
            emit("info", "El modelo no devolvió código corregido. El agente se detiene.")
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
        emit("rejected", f"La petición es demasiado larga (máximo {MAX_REQUEST_CHARS} caracteres).",
             reason="too_long")
        return None

    emit("request", request=request)
    emit("stage", stage="model", status="running")
    code = generate_tf(request, ask)
    if code is None:
        emit("rejected", "Esa petición está fuera del alcance: solo creo un grupo de recursos, una cuenta de "
             "almacenamiento, una red básica, Azure SQL, Cosmos DB, Key Vault, una "
             "máquina virtual Linux, App Service y Container Apps. No se guardó nada.",
             reason="out_of_scope")
        return None
    emit("stage", stage="model", status="ok", code=code)

    problems = check_code(code)
    if problems:
        _report_problems(problems)
        return None

    path = validate_with_retries(request, code, output_dir, ask, check)
    if path is not None:
        emit("info", f"\nCódigo validado en {path}:\n")
        emit("code", path.read_text(encoding="utf-8"), path=str(path))
    return path


def confirm(question: str, input_fn: Callable[[str], str] = input, action: str = "") -> bool:
    """Barrera 6: solo la palabra 'yes' completa cuenta como confirmación.

    Avisa con un evento "confirm" (sin texto, no imprime) para que un front muestre los botones;
    la respuesta sigue llegando por input_fn.
    """
    emit("confirm", question=question, action=action)
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
        emit("rejected", "Falta ARM_SUBSCRIPTION_ID en el .env (el ID de tu suscripción de Azure). "
             "No se ejecutó nada.", reason="missing_subscription")
        return NOT_APPLIED, ""

    try:
        emit("stage", f"\nGenerando el plan para {verb} (no crea nada todavía)...",
             stage="plan", status="running", destroy=destroy)
        plan = tf.plan(output_dir, destroy=destroy)
        if not plan.ok:
            emit("stage", f"\n✘ Falló terraform plan:\n{plan.output}",
                 stage="plan", status="failed", destroy=destroy, output=plan.output)
            emit("info", "\nRevisa que hayas ejecutado `az login` y que ARM_SUBSCRIPTION_ID sea el de tu suscripción.")
            return NOT_APPLIED, ""

        info = analyze_plan(plan.output, destroying=destroy)
        emit("plan", f"\n{plan.output}\n", destroy=destroy, output=plan.output, add=info.add,
             change=info.change, to_destroy=info.destroy, no_changes=info.no_changes)

        if info.blocked:
            for problem in info.blocked:
                emit("blocked", f"⛔ {problem}", problem=problem)
            emit("info", "No se continúa. No se creó ni se destruyó nada.")
            return NOT_APPLIED, ""
        if info.no_changes:
            emit("info", "No hay cambios: no hay nada que " + verb + ".")
            return NOT_APPLIED, ""
        for warning in info.warnings:
            emit("warning", f"⚠ {warning}\n", warning=warning)
        for cost in info.costs:
            emit("cost", f"💲 {cost}\n", cost=cost)

        question = (f"Lee el plan de arriba (puedes revisar el portal antes de responder).\n"
                    f"Escribe yes para {verb}; cualquier otra cosa cancela: ")
        if not confirm(question, input_fn, action="destroy" if destroy else "apply"):
            emit("info", "Cancelado. No se creó ni se destruyó nada.", cancelled=True)
            return NOT_APPLIED, ""

        emit("stage", stage="apply", status="running", destroy=destroy)
        result = tf.apply(output_dir)
        emit("output", result.output, stage="apply")
        if not result.ok:
            emit("stage", "\n✘ Falló terraform apply. Revisa el error de arriba.",
                 stage="apply", status="failed", destroy=destroy, output=result.output)
            return APPLY_FAILED, result.output
        emit("stage", stage="apply", status="ok", destroy=destroy)
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
        emit("info", "\nNo pude leer el estado de Terraform; revisa el portal por si quedó algo creado.")
        return False
    if not left.output:
        emit("info", "\nNo quedó nada registrado en el estado de Terraform.")
        return True
    emit("leftover", "\nEl apply falló a medias y esto quedó creado (y gastando crédito):\n" + left.output,
         resources=left.output.splitlines())
    if _review(output_dir, True, tf, input_fn)[0] == APPLIED:
        return True
    emit("info", 'Siguen creados. Para destruirlos más tarde, escribe "destroy" en el agente.')
    return False


def show_secrets(output_dir: Path, tf: types.ModuleType) -> bool:
    """Muestra a la persona las contraseñas que generó Terraform. Nunca se envían al modelo."""
    reader = getattr(tf, "generated_passwords", None)
    secrets = reader(output_dir) if reader else {}
    if not secrets:
        return False
    emit("info", "\n🔑 Contraseñas generadas por Terraform (guárdalas ahora en un gestor de contraseñas):")
    # Los valores van solo en `data` para quien escucha; el texto completo solo lo ve la consola.
    emit_secret("secrets", "\n".join(f"  {address} = {value}" for address, value in secrets.items()),
                secrets=secrets)
    emit("info", "   Usuarios: sqladmin (servidor SQL) y azureuser (máquina virtual).")
    emit("warning", "⚠ Quedan en texto plano en output/terraform.tfstate (ya está en .gitignore). No las compartas, "
         "no grabes la pantalla con ellas visibles y no subas ni sincronices la carpeta output/. "
         'Puedes verlas de nuevo con el comando "secretos" mientras existan los recursos.')
    return True


def _switch_region(output_dir: Path, tried: list[str], switches: int,
                   input_fn: Callable[[str], str]) -> bool:
    """Propone y, con un yes escrito, aplica el cambio de región en main.tf. True si se cambió."""
    if switches >= MAX_REGION_SWITCHES:
        emit("info", f"Ya se intentó cambiar de región {MAX_REGION_SWITCHES} veces; el agente se detiene.")
        return False
    path = output_dir / "main.tf"
    try:
        code = path.read_text(encoding="utf-8")
    except OSError:
        return False
    old = current_region(code)
    if old is None:
        emit("info", "No encontré una región escrita en el código; no puedo cambiarla automáticamente.")
        return False
    if old not in tried:
        tried.append(old)
    new = next_region(tried, fallback_regions())
    if new is None:
        emit("info", "No quedan regiones alternas por probar.")
        return False
    emit("region", f"\nParece que Azure no puede crear esto en {old} (disponibilidad, cuota o política).",
         old=old, new=new)
    question = (f"¿Probar en {new}? Los datos quedarían en esa región (puede importar por residencia "
                f"de datos o latencia).\nEscribe yes para cambiar a {new}; cualquier otra cosa cancela: ")
    if not confirm(question, input_fn, action="region"):
        emit("info", "No se cambió la región.")
        return False
    path.write_text(switch_region(code, old, new), encoding="utf-8")
    tried.append(new)
    emit("info", f"Región cambiada a {new} en {path}. Genero un plan nuevo para que lo revises.",
         old=old, new=new, changed=True)
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
        emit("resources", "\nRecursos creados:\n" + created.output, resources=created.output.splitlines())
    show_secrets(output_dir, tf)
    emit("info", "\nPara no gastar crédito, destruye lo que creaste cuando termines de revisarlo.")
    if not review_and_apply(output_dir, destroy=True, tf=tf, input_fn=input_fn):
        emit("info", 'Los recursos siguen creados. Para destruirlos más tarde, escribe "destroy" en el agente.')


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