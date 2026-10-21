"""Revisiones de calidad y costo de Cosmos DB, Key Vault, máquinas virtuales, App Service y
Container Apps.

Igual que el resto de check_names: no son la barrera de seguridad (esa es la lista cerrada de
resource_check.check_code), sus problemas se devuelven al modelo para que los corrija. Fijan
tamaños y niveles baratos para que una petición no gaste el crédito sin que se note.
"""

import re

from resource_check import _attr, _check_suffixed_name, _string_attr, _text

VM_SIZES = {"Standard_B1s", "Standard_B1ms", "Standard_B2s"}
VM_RESERVED_USERS = {"admin", "administrator", "root", "guest", "user", "test", "azure", "azureadmin",
                     "sa", "sys", "oracle", "sql", "support", "backup", "operator", "adm", "ftp", "mail"}
APP_PLAN_SKUS = {"F1", "B1"}
MAX_REPLICAS = 2
MAX_CPU = 0.5
MAX_MEMORY_GI = 1.0
MAX_LOG_QUOTA_GB = 1.0
TRUSTED_IMAGE_PREFIX = "mcr.microsoft.com/"

_FIXED_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


def _nested_string(lines, name: str):
    """Valor de `name = "..."` en cualquier nivel del recurso (para atributos de sub-bloques)."""
    found = re.search(rf'^\s*{name}\s*=\s*"((?:[^"\\]|\\.)*)"', _text(lines), re.M)
    return found.group(1) if found else None


def _count_blocks(lines, name: str) -> int:
    return len(re.findall(rf"^\s*{name}\s*\{{", _text(lines), re.M))


def _labels(blocks, rtype: str) -> set[str]:
    return {label for t, label, _ in blocks if t == rtype}


def _ref(lines, attr: str, rtype: str, known: set[str], where: str, problems: list[str], list_form=False):
    """Comprueba `attr = rtype.<x>.id` (o [rtype.<x>.id]) apuntando a un recurso definido en el archivo."""
    value = r"\[\s*{r}\.([A-Za-z_]\w*)\.id\s*\]" if list_form else r"{r}\.([A-Za-z_]\w*)\.id"
    found = re.search(rf"^\s*{attr}\s*=\s*" + value.format(r=rtype), _text(lines), re.M)  # puede estar en un sub-bloque
    label = found.group(1) if found else None
    if label is None or label not in known:
        shape = f"[{rtype}.<nombre>.id]" if list_form else f"{rtype}.<nombre>.id"
        problems.append(f"{where}: {attr} debe ser {shape} de un recurso definido en este archivo.")


def _fixed_name(where: str, lines, max_len: int, min_len: int = 1, lowercase=False, start_letter=False):
    name = _string_attr(lines, "name")
    if name is None or "${" in name:
        return [f"{where}: el nombre debe ser un texto fijo."]
    ok = _FIXED_NAME.fullmatch(name) and min_len <= len(name) <= max_len and not name.endswith("-")
    if lowercase and name != name.lower():
        ok = False
    if start_letter and not name[:1].isalpha():
        ok = False
    if not ok:
        return [f"{where}: el nombre {name!r} no es válido (entre {min_len} y {max_len} caracteres; letras, "
                "números y guiones; no puede terminar en guion"
                + ("; solo minúsculas" if lowercase else "") + ("; empieza con letra" if start_letter else "") + ")."]
    return []


def _check_cosmos(blocks, lengths) -> list[str]:
    problems: list[str] = []
    for rtype, label, lines in blocks:
        if rtype == "azurerm_cosmosdb_account":
            where = f"azurerm_cosmosdb_account.{label}"
            problems += _check_suffixed_name(where, lines, lengths, max_len=44, min_len=3, allow_hyphen=True)
            if _attr(lines, "free_tier_enabled", r"(true)") is None:
                problems.append(f"{where}: debe fijar free_tier_enabled = true (si no, cobra por capacidad).")
            if _string_attr(lines, "kind") != "GlobalDocumentDB":
                problems.append(f'{where}: kind debe ser "GlobalDocumentDB".')
            if _string_attr(lines, "offer_type") != "Standard":
                problems.append(f'{where}: offer_type debe ser "Standard".')
            if _count_blocks(lines, "geo_location") != 1:
                problems.append(f"{where}: debe tener exactamente un bloque geo_location (una sola región).")
            if _count_blocks(lines, "consistency_policy") != 1:
                problems.append(f"{where}: debe tener un bloque consistency_policy.")
            if re.search(r"multiple_write_locations_enabled\s*=\s*true|capabilities\s*\{|backup\s*\{",
                         _text(lines)):
                problems.append(f"{where}: no uses escrituras múltiples, capabilities ni backup personalizado.")
        elif rtype == "azurerm_cosmosdb_sql_database":
            where = f"azurerm_cosmosdb_sql_database.{label}"
            name = _string_attr(lines, "name")
            if name is None or "${" in name or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", name):
                problems.append(f"{where}: el nombre debe ser un texto fijo (letras, números, guiones).")
            if re.search(r"\bthroughput\b|autoscale_settings", _text(lines)):
                problems.append(f"{where}: no fijes throughput ni autoscale_settings (el free tier ya incluye capacidad).")
            if not re.search(r"\baccount_name\s*=\s*azurerm_cosmosdb_account\.([A-Za-z_]\w*)\.name\b",
                             _text(lines)):
                problems.append(f"{where}: account_name debe ser azurerm_cosmosdb_account.<nombre>.name.")
    return problems


def _check_key_vault(code: str, blocks, lengths) -> list[str]:
    problems: list[str] = []
    data_labels = set(re.findall(r'^\s*data\s+"azurerm_client_config"\s+"([^"]+)"', code, re.M))
    for rtype, label, lines in blocks:
        if rtype != "azurerm_key_vault":
            continue
        where = f"azurerm_key_vault.{label}"
        problems += _check_suffixed_name(where, lines, lengths, max_len=24, min_len=3, allow_hyphen=True)
        name = _string_attr(lines, "name") or ""
        if not name[:1].isalpha() or "--" in name:
            problems.append(f"{where}: el nombre debe empezar con una letra y no tener guiones seguidos.")
        if _string_attr(lines, "sku_name") != "standard":
            problems.append(f'{where}: sku_name debe ser "standard".')
        tenant = _attr(lines, "tenant_id", r"data\.azurerm_client_config\.([A-Za-z_]\w*)\.tenant_id")
        if tenant is None or tenant not in data_labels:
            problems.append(f"{where}: tenant_id debe ser data.azurerm_client_config.<nombre>.tenant_id con ese "
                            'data source definido en el archivo (data "azurerm_client_config" "current" {}).')
        if _attr(lines, "rbac_authorization_enabled", r"(true)") is None:
            problems.append(f"{where}: debe fijar rbac_authorization_enabled = true (sin access_policy).")
        if re.search(r"^\s*access_policy\s*\{", _text(lines), re.M):
            problems.append(f"{where}: no uses access_policy; el acceso va por roles (RBAC).")
        if _attr(lines, "purge_protection_enabled", r"(true)") is not None:
            problems.append(f"{where}: purge_protection_enabled no puede ser true (impediría destruirlo al terminar).")
        days = _attr(lines, "soft_delete_retention_days", r"(\d+)")
        if days is not None and not 7 <= int(days) <= 90:
            problems.append(f"{where}: soft_delete_retention_days debe estar entre 7 y 90.")
    return problems


def _check_vm(blocks, password_labels) -> list[str]:
    problems: list[str] = []
    subnets = _labels(blocks, "azurerm_subnet")
    nics = _labels(blocks, "azurerm_network_interface")
    for rtype, label, lines in blocks:
        if rtype == "azurerm_network_interface":
            where = f"azurerm_network_interface.{label}"
            problems += _fixed_name(where, lines, 80)
            _ref(lines, "subnet_id", "azurerm_subnet", subnets, where, problems)
            if _nested_string(lines, "private_ip_address_allocation") != "Dynamic":
                problems.append(f'{where}: private_ip_address_allocation debe ser "Dynamic".')
            if "public_ip_address_id" in _text(lines):
                problems.append(f"{where}: no uses public_ip_address_id; la máquina queda sin IP pública.")
        elif rtype == "azurerm_linux_virtual_machine":
            where = f"azurerm_linux_virtual_machine.{label}"
            problems += _fixed_name(where, lines, 64)
            if _string_attr(lines, "size") not in VM_SIZES:
                problems.append(f"{where}: size debe ser uno de {sorted(VM_SIZES)}; otros tamaños cuestan mucho más.")
            user = _string_attr(lines, "admin_username")
            if user is None or "${" in user or user.lower() in VM_RESERVED_USERS \
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", user):
                problems.append(f'{where}: admin_username debe ser un texto fijo, en minúsculas y no reservado '
                                '(por ejemplo "azureuser").')
            password = _attr(lines, "admin_password", r"random_password\.([A-Za-z_]\w*)\.result")
            if password is None or password not in password_labels:
                problems.append(f"{where}: admin_password debe ser random_password.<nombre>.result de un "
                                "random_password definido en este archivo, nunca un texto escrito a mano.")
            if _attr(lines, "disable_password_authentication", r"(false)") is None:
                problems.append(f"{where}: debe fijar disable_password_authentication = false.")
            _ref(lines, "network_interface_ids", "azurerm_network_interface", nics, where, problems, list_form=True)
            if _nested_string(lines, "storage_account_type") != "Standard_LRS":
                problems.append(f'{where}: os_disk.storage_account_type debe ser "Standard_LRS".')
            if _nested_string(lines, "publisher") != "Canonical":
                problems.append(f'{where}: source_image_reference.publisher debe ser "Canonical" (Ubuntu).')
            if _count_blocks(lines, "os_disk") != 1 or _count_blocks(lines, "source_image_reference") != 1:
                problems.append(f"{where}: necesita un bloque os_disk y un bloque source_image_reference.")
            if re.search(r"custom_data|user_data|identity\s*\{|admin_ssh_key", _text(lines)):
                problems.append(f"{where}: no uses custom_data, user_data, identity ni admin_ssh_key.")
    return problems


def _check_app_service(blocks, lengths) -> list[str]:
    problems: list[str] = []
    plans = _labels(blocks, "azurerm_service_plan")
    for rtype, label, lines in blocks:
        if rtype == "azurerm_service_plan":
            where = f"azurerm_service_plan.{label}"
            problems += _fixed_name(where, lines, 60)
            if _string_attr(lines, "os_type") != "Linux":
                problems.append(f'{where}: os_type debe ser "Linux".')
            if _string_attr(lines, "sku_name") not in APP_PLAN_SKUS:
                problems.append(f"{where}: sku_name debe ser uno de {sorted(APP_PLAN_SKUS)}; otros niveles cuestan mucho más.")
        elif rtype == "azurerm_linux_web_app":
            where = f"azurerm_linux_web_app.{label}"
            problems += _check_suffixed_name(where, lines, lengths, max_len=60, min_len=2, allow_hyphen=True)
            _ref(lines, "service_plan_id", "azurerm_service_plan", plans, where, problems)
            if _attr(lines, "https_only", r"(true)") is None:
                problems.append(f"{where}: debe fijar https_only = true.")
            if _count_blocks(lines, "site_config") != 1:
                problems.append(f"{where}: necesita un bloque site_config (puede estar vacío).")
    return problems


def _gi(memory: str) -> float | None:
    found = re.fullmatch(r"(\d+(?:\.\d+)?)Gi", memory)
    return float(found.group(1)) if found else None


def _check_containers(blocks) -> list[str]:
    problems: list[str] = []
    workspaces = _labels(blocks, "azurerm_log_analytics_workspace")
    envs = _labels(blocks, "azurerm_container_app_environment")
    for rtype, label, lines in blocks:
        text = _text(lines)
        if rtype == "azurerm_log_analytics_workspace":
            where = f"azurerm_log_analytics_workspace.{label}"
            problems += _fixed_name(where, lines, 63, min_len=4)
            if _string_attr(lines, "sku") != "PerGB2018":
                problems.append(f'{where}: sku debe ser "PerGB2018".')
            retention = _attr(lines, "retention_in_days", r"(\d+)")
            if retention is None or not 30 <= int(retention) <= 30:
                problems.append(f"{where}: retention_in_days debe ser 30.")
            quota = _attr(lines, "daily_quota_gb", r"(\d+(?:\.\d+)?)")
            if quota is None or not 0 < float(quota) <= MAX_LOG_QUOTA_GB:
                problems.append(f"{where}: daily_quota_gb debe estar entre 0 y {MAX_LOG_QUOTA_GB} (tope de costo).")
        elif rtype == "azurerm_container_app_environment":
            where = f"azurerm_container_app_environment.{label}"
            problems += _fixed_name(where, lines, 60)
            _ref(lines, "log_analytics_workspace_id", "azurerm_log_analytics_workspace", workspaces, where, problems)
        elif rtype == "azurerm_container_app":
            where = f"azurerm_container_app.{label}"
            problems += _fixed_name(where, lines, 32, lowercase=True, start_letter=True)
            _ref(lines, "container_app_environment_id", "azurerm_container_app_environment", envs, where, problems)
            if _string_attr(lines, "revision_mode") != "Single":
                problems.append(f'{where}: revision_mode debe ser "Single".')
            images = re.findall(r'^\s*image\s*=\s*"([^"]*)"', text, re.M)
            if not images:
                problems.append(f"{where}: necesita al menos un contenedor con image.")
            for image in images:
                if not image.startswith(TRUSTED_IMAGE_PREFIX) or "${" in image:
                    problems.append(f"{where}: la imagen {image!r} no está permitida; solo imágenes de "
                                    f"{TRUSTED_IMAGE_PREFIX} (por ejemplo mcr.microsoft.com/k8se/quickstart:latest).")
            for cpu in re.findall(r"^\s*cpu\s*=\s*([\d.]+)", text, re.M):
                if float(cpu) > MAX_CPU:
                    problems.append(f"{where}: cpu no puede pasar de {MAX_CPU}.")
            for memory in re.findall(r'^\s*memory\s*=\s*"([^"]*)"', text, re.M):
                value = _gi(memory)
                if value is None or value > MAX_MEMORY_GI:
                    problems.append(f'{where}: memory debe ser como "0.5Gi" y no pasar de {MAX_MEMORY_GI}Gi.')
            for replicas in re.findall(r"^\s*max_replicas\s*=\s*(\d+)", text, re.M):
                if int(replicas) > MAX_REPLICAS:
                    problems.append(f"{where}: max_replicas no puede pasar de {MAX_REPLICAS}.")
            if re.search(r"^\s*(secret|identity|registry|volume)\s*\{", text, re.M):
                problems.append(f"{where}: no uses secret, identity, registry ni volume.")
    return problems


def check_services(code: str, blocks, lengths, password_labels) -> list[str]:
    problems: list[str] = []
    problems += _check_cosmos(blocks, lengths)
    problems += _check_key_vault(code, blocks, lengths)
    problems += _check_vm(blocks, password_labels)
    problems += _check_app_service(blocks, lengths)
    problems += _check_containers(blocks)
    return problems