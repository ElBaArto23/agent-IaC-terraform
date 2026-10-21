"""Barrera 3: lista cerrada de lo que el código generado puede contener.

Todo lo que no esté explícitamente permitido se rechaza. Se revisa ANTES de ejecutar
cualquier comando de Terraform, porque incluso `init` y `validate` leen el código.
"""

import ipaddress
import re

ALLOWED_RESOURCES = {
    "azurerm_resource_group",
    "azurerm_storage_account",
    "azurerm_virtual_network",
    "azurerm_subnet",
    "azurerm_network_security_group",
    "azurerm_subnet_network_security_group_association",
    "azurerm_mssql_server",
    "azurerm_mssql_database",
    "azurerm_cosmosdb_account",
    "azurerm_cosmosdb_sql_database",
    "azurerm_key_vault",
    "azurerm_network_interface",
    "azurerm_linux_virtual_machine",
    "azurerm_service_plan",
    "azurerm_linux_web_app",
    "azurerm_log_analytics_workspace",
    "azurerm_container_app_environment",
    "azurerm_container_app",
    "random_string",
    "random_password",
}
# Único data source permitido: solo lee el tenant de tu sesión (lo necesita Key Vault)
ALLOWED_DATA = {"azurerm_client_config"}
ALLOWED_TOP_LEVEL = {"terraform", "provider", "resource", "data"}
# Tope de recursos por archivo: evita que una respuesta desbocada cree decenas de cosas
MAX_RESOURCES = 20

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_COMMENT = re.compile(r"(#|//).*$")
_TOP_WORD = re.compile(r"^\s*([A-Za-z_][\w-]*)")
_RESOURCE_TYPE = re.compile(r'^\s*resource\s+"([^"]+)"')
_DATA_TYPE = re.compile(r'^\s*data\s+"([^"]+)"')
_PROVIDER_NAME = re.compile(r'^\s*provider\s+"([^"]+)"')
# Ejecución de comandos, estado remoto, heredocs y reglas de red personalizadas (un NSG con
# reglas inventadas por la IA podría abrir puertos a internet: solo se usan las reglas por defecto)
_FORBIDDEN_WORDS = re.compile(r"\b(provisioner|local-exec|remote-exec|backend|security_rule)\b|<<")
# Funciones que leen archivos o rutas de la máquina donde corre el agente
_FORBIDDEN_FUNCS = re.compile(r"\b(file|filebase64|templatefile|fileset|abspath|pathexpand)\s*\(")


def check_code(code: str) -> list[str]:
    """Devuelve la lista de problemas encontrados; vacía si el código es aceptable."""
    problems: list[str] = []
    depth = 0
    resources = 0

    for number, raw in enumerate(_BLOCK_COMMENT.sub("", code).splitlines(), start=1):
        # Sin textos ni comentarios, para contar llaves y detectar palabras sin falsos positivos
        line = _COMMENT.sub("", _STRING.sub('""', raw))

        if depth == 0 and line.strip() and not line.strip().startswith("}"):
            match = _TOP_WORD.match(line)
            kind = match.group(1) if match else line.strip()
            if kind not in ALLOWED_TOP_LEVEL:
                problems.append(f"Línea {number}: bloque no permitido '{kind}'.")
            elif kind == "resource":
                resources += 1
                found = _RESOURCE_TYPE.match(raw)
                rtype = found.group(1) if found else "(ilegible)"
                if rtype not in ALLOWED_RESOURCES:
                    problems.append(f"Línea {number}: recurso no permitido '{rtype}'.")
            elif kind == "data":
                found = _DATA_TYPE.match(raw)
                dtype = found.group(1) if found else "(ilegible)"
                if dtype not in ALLOWED_DATA:
                    problems.append(f"Línea {number}: data source no permitido '{dtype}'.")
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
    if resources > MAX_RESOURCES:
        problems.append(f"Hay {resources} recursos y el máximo permitido es {MAX_RESOURCES}.")
    return problems


# --- Nombres --------------------------------------------------------------------------
# Terraform no puede validar estos nombres antes de crear (llevan el sufijo aleatorio, que
# se conoce al aplicar), así que un nombre inválido fallaría en el apply, con el grupo de
# recursos ya creado. Esta revisión lo detecta antes. A diferencia de check_code, no es una
# barrera de seguridad: sus problemas se devuelven al modelo para que los corrija.

RESOURCE_GROUP_NAME_RE = re.compile(r"[A-Za-z0-9_().-]{1,90}")

_RESOURCE_HEADER = re.compile(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"')
_SUFFIX_REF = re.compile(r"\$\{random_string\.([A-Za-z_]\w*)\.result\}")
_END = r"\s*(?:(?:#|//).*)?$"


def _resource_blocks(code: str) -> list[tuple[str, str, list[tuple[str, int]]]]:
    """[(tipo, etiqueta, [(línea, profundidad al empezar la línea)])] de cada resource de primer nivel."""
    blocks = []
    current = None
    depth = 0
    for raw in _BLOCK_COMMENT.sub("", code).splitlines():
        line = _COMMENT.sub("", _STRING.sub('""', raw))
        if depth == 0 and current is None:
            found = _RESOURCE_HEADER.match(raw)
            if found:
                current = (found.group(1), found.group(2), [])
        if current is not None:
            current[2].append((raw, depth))
        depth = max(depth + line.count("{") - line.count("}"), 0)
        if current is not None and depth == 0:
            blocks.append(current)
            current = None
    return blocks


def _attr(lines, name: str, value_pattern: str):
    """Valor del atributo `name` escrito directamente en el recurso (no dentro de sub-bloques)."""
    pattern = re.compile(rf"^\s*{name}\s*=\s*{value_pattern}{_END}")
    for raw, depth in lines:
        if depth == 1:
            found = pattern.match(raw)
            if found:
                return found.group(1)
    return None


def _string_attr(lines, name: str):
    return _attr(lines, name, r'"((?:[^"\\]|\\.)*)"')


def _check_suffixed_name(where: str, lines, lengths: dict[str, int | None], max_len: int, min_len: int,
                         allow_hyphen: bool) -> list[str]:
    """Nombre único en todo Azure: texto fijo + sufijo de random_string. Calcula el nombre final."""
    name = _string_attr(lines, "name")
    if name is None:
        return [f"{where}: no pude leer el nombre; debe ser un texto fijo seguido del sufijo random_string."]

    suffixes = 0

    def fill(match: re.Match) -> str:
        nonlocal suffixes
        length = lengths.get(match.group(1))
        if not length:
            return match.group(0)
        suffixes += 1
        return "x" * length

    resolved = _SUFFIX_REF.sub(fill, name)
    if "${" in resolved:
        return [f"{where}: el nombre usa algo que no puedo verificar; usa solo texto fijo y "
                "${random_string.<nombre>.result} de un random_string con length fijo."]

    problems = []
    if suffixes == 0:
        problems.append(f"{where}: el nombre debe incluir el sufijo aleatorio de random_string.")
    if len(resolved) > max_len:
        problems.append(f"{where}: el nombre {name!r} quedaría de {len(resolved)} caracteres y el máximo es "
                        f"{max_len}; acorta la parte fija.")
    if len(resolved) < min_len:
        problems.append(f"{where}: el nombre {name!r} quedaría de menos de {min_len} caracteres.")
    if re.search(r"[^a-z0-9-]" if allow_hyphen else r"[^a-z0-9]", resolved):
        problems.append(f"{where}: el nombre {name!r} solo puede tener minúsculas, números y guiones."
                        if allow_hyphen else
                        f"{where}: el nombre {name!r} solo puede tener minúsculas y números, sin guiones "
                        "ni mayúsculas.")
    if allow_hyphen and (resolved.startswith("-") or resolved.endswith("-")):
        problems.append(f"{where}: el nombre {name!r} no puede empezar ni terminar con guion.")
    return problems


def _check_storage_name(label: str, lines, lengths: dict[str, int | None]) -> list[str]:
    return _check_suffixed_name(f"azurerm_storage_account.{label}", lines, lengths,
                                max_len=24, min_len=3, allow_hyphen=False)


def _check_sql_server_name(label: str, lines, lengths: dict[str, int | None]) -> list[str]:
    return _check_suffixed_name(f"azurerm_mssql_server.{label}", lines, lengths,
                                max_len=63, min_len=1, allow_hyphen=True)


def _check_group_name(label: str, lines) -> list[str]:
    where = f"azurerm_resource_group.{label}"
    name = _string_attr(lines, "name")
    if name is None or "${" in name:
        return [f"{where}: el nombre debe ser un texto fijo."]
    if not RESOURCE_GROUP_NAME_RE.fullmatch(name) or name.endswith("."):
        return [f"{where}: el nombre {name!r} no es válido (máximo 90 caracteres; letras, números, "
                "guiones, guiones bajos, puntos y paréntesis; no puede terminar en punto)."]
    return []


PRIVATE_RANGES = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
MAX_NETWORK_PREFIX = 29  # Azure no acepta redes ni subredes más pequeñas que /29
_NETWORK_NAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9_])?")
_NETWORK_NAME_MAX = {"azurerm_virtual_network": 64, "azurerm_subnet": 80, "azurerm_network_security_group": 80}
_VNET_REF = re.compile(r"\bvirtual_network_name\s*=\s*azurerm_virtual_network\.([A-Za-z_]\w*)\.name\b")


def _text(lines) -> str:
    return "\n".join(raw for raw, _ in lines)


def _list_strings(lines, name: str) -> list[str] | None:
    """Los textos de `name = ["a", "b"]`; None si falta o si trae algo que no sea texto fijo."""
    found = re.search(rf"\b{name}\s*=\s*\[([^\]]*)\]", _text(lines))
    if not found:
        return None
    inner = re.sub(r"(?:#|//)[^\n]*", "", found.group(1))
    if re.sub(r'"[^"\\]*"|[,\s]', "", inner):
        return None
    return re.findall(r'"([^"\\]*)"', inner)


def _parse_cidrs(values: list[str], where: str, attr: str, problems: list[str]):
    """Redes IPv4 privadas, bien escritas y de tamaño aceptable; los errores se agregan a `problems`."""
    networks = []
    for value in values:
        try:
            net = ipaddress.ip_network(value, strict=True)
        except ValueError:
            problems.append(f"{where}: {attr} {value!r} no es un CIDR válido (ejemplo: 10.0.0.0/16, "
                            "sin bits de host).")
            continue
        if net.version != 4 or not any(net.subnet_of(p) for p in PRIVATE_RANGES):
            problems.append(f"{where}: {attr} {value!r} no es un rango privado; usa 10.x.x.x, "
                            "172.16-31.x.x o 192.168.x.x.")
        elif net.prefixlen > MAX_NETWORK_PREFIX:
            problems.append(f"{where}: {attr} {value!r} es demasiado pequeño; el mínimo es /{MAX_NETWORK_PREFIX}.")
        else:
            networks.append(net)
    return networks


def _check_network_name(rtype: str, label: str, lines) -> list[str]:
    where = f"{rtype}.{label}"
    name = _string_attr(lines, "name")
    if name is None or "${" in name:
        return [f"{where}: el nombre debe ser un texto fijo."]
    if len(name) > _NETWORK_NAME_MAX[rtype] or not _NETWORK_NAME_RE.fullmatch(name):
        return [f"{where}: el nombre {name!r} no es válido (máximo {_NETWORK_NAME_MAX[rtype]} caracteres; "
                "letras, números, guiones, guiones bajos y puntos; empieza con letra o número)."]
    return []


def _check_network(blocks) -> list[str]:
    """Rangos de IP y nombres de la red básica: errores que Azure solo reportaría en el apply."""
    problems: list[str] = []

    vnets: dict[str, list] = {}
    for rtype, label, lines in blocks:
        if rtype != "azurerm_virtual_network":
            continue
        where = f"azurerm_virtual_network.{label}"
        values = _list_strings(lines, "address_space")
        if values is None:
            problems.append(f"{where}: address_space debe ser una lista de CIDR escritos como texto fijo.")
            vnets[label] = []
            continue
        vnets[label] = _parse_cidrs(values, where, "address_space", problems)

    subnets_by_vnet: dict[str, list] = {}
    for rtype, label, lines in blocks:
        if rtype != "azurerm_subnet":
            continue
        where = f"azurerm_subnet.{label}"
        ref = _VNET_REF.search(_text(lines))
        if not ref or ref.group(1) not in vnets:
            problems.append(f"{where}: virtual_network_name debe ser azurerm_virtual_network.<nombre>.name "
                            "de una red que esté en este archivo.")
            continue
        values = _list_strings(lines, "address_prefixes")
        if values is None:
            problems.append(f"{where}: address_prefixes debe ser una lista de CIDR escritos como texto fijo.")
            continue
        nets = _parse_cidrs(values, where, "address_prefixes", problems)
        parent = vnets[ref.group(1)]
        for net in nets:
            if parent and not any(net.version == p.version and net.subnet_of(p) for p in parent):
                problems.append(f"{where}: la subnet {net} no está dentro del address_space de la red "
                                f"({', '.join(str(p) for p in parent)}).")
        for net in nets:
            for other_label, other in subnets_by_vnet.setdefault(ref.group(1), []):
                if net.version == other.version and net.overlaps(other):
                    problems.append(f"{where}: la subnet {net} se solapa con {other} "
                                    f"(azurerm_subnet.{other_label}).")
            subnets_by_vnet[ref.group(1)].append((label, net))

    for rtype, label, lines in blocks:
        if rtype in _NETWORK_NAME_MAX:
            problems += _check_network_name(rtype, label, lines)
    return problems


SQL_RESERVED_LOGINS = {"admin", "administrator", "sa", "root", "dbmanager", "loginmanager", "dbo", "guest",
                       "public", "sys"}
# Solo el nivel más barato: otros niveles cuestan mucho más y gastarían el crédito sin que lo notes
ALLOWED_SQL_SKUS = {"Basic"}
MAX_SQL_BASIC_GB = 2
_SAFE_SPECIAL_RE = re.compile(r"[!#$%*_=+-]{1,20}")
_DB_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def _check_passwords(blocks) -> tuple[list[str], set[str]]:
    """random_password: largo y complejidad que exige Azure, y caracteres que no rompen conexiones."""
    problems: list[str] = []
    labels: set[str] = set()
    for rtype, label, lines in blocks:
        if rtype != "random_password":
            continue
        labels.add(label)
        where = f"random_password.{label}"
        length = _attr(lines, "length", r"(\d+)")
        if length is None or int(length) < 16:
            problems.append(f"{where}: length debe ser un número fijo de 16 o más.")
        for flag in ("min_upper", "min_lower", "min_numeric", "min_special"):
            value = _attr(lines, flag, r"(\d+)")
            if value is None or int(value) < 1:
                problems.append(f"{where}: debe fijar {flag} = 1 (o más) para cumplir la complejidad que exige Azure.")
        if _attr(lines, "special", r"(false)") is not None:
            problems.append(f"{where}: special no puede ser false.")
        special = _string_attr(lines, "override_special")
        if special is None or not _SAFE_SPECIAL_RE.fullmatch(special):
            problems.append(f"{where}: debe fijar override_special usando solo estos caracteres: !#$%*-_=+ "
                            "(otros, como @ : / o comillas, rompen las cadenas de conexión).")
    return problems, labels


def _check_sql(blocks, lengths: dict[str, int | None], password_labels: set[str]) -> list[str]:
    """Azure SQL: nombre único, contraseña solo de random_password, TLS 1.2 y nivel barato."""
    problems: list[str] = []
    for rtype, label, lines in blocks:
        if rtype == "azurerm_mssql_server":
            where = f"azurerm_mssql_server.{label}"
            problems += _check_sql_server_name(label, lines, lengths)
            login = _string_attr(lines, "administrator_login")
            if login is None or "${" in login:
                problems.append(f'{where}: administrator_login debe ser un texto fijo (por ejemplo "sqladmin").')
            elif login.lower() in SQL_RESERVED_LOGINS:
                problems.append(f'{where}: administrator_login {login!r} está reservado por Azure; '
                                'usa otro, por ejemplo "sqladmin".')
            elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", login):
                problems.append(f"{where}: administrator_login {login!r} no es válido (letras, números y "
                                "guion bajo; empieza con una letra).")
            password = _attr(lines, "administrator_login_password", r"random_password\.([A-Za-z_]\w*)\.result")
            if password is None or password not in password_labels:
                problems.append(f"{where}: administrator_login_password debe ser "
                                "random_password.<nombre>.result de un random_password definido en este "
                                "archivo, nunca un texto escrito a mano.")
            if _string_attr(lines, "minimum_tls_version") != "1.2":
                problems.append(f'{where}: minimum_tls_version debe ser "1.2".')
        elif rtype == "azurerm_mssql_database":
            where = f"azurerm_mssql_database.{label}"
            name = _string_attr(lines, "name")
            if name is None or "${" in name:
                problems.append(f"{where}: el nombre debe ser un texto fijo.")
            elif not _DB_NAME_RE.fullmatch(name) or name.endswith("."):
                problems.append(f"{where}: el nombre {name!r} no es válido (máximo 128 caracteres; letras, "
                                "números, guiones, guiones bajos y puntos).")
            sku = _string_attr(lines, "sku_name")
            if sku not in ALLOWED_SQL_SKUS:
                problems.append(f"{where}: sku_name debe ser uno de {sorted(ALLOWED_SQL_SKUS)}; "
                                "otros niveles cuestan mucho más.")
            size = _attr(lines, "max_size_gb", r"(\d+)")
            if size is not None and int(size) > MAX_SQL_BASIC_GB:
                problems.append(f"{where}: max_size_gb no puede pasar de {MAX_SQL_BASIC_GB} en el nivel Basic.")
    return problems


def check_names(code: str) -> list[str]:
    """Problemas con nombres, rangos de red, contraseñas y SQL; lista vacía si todo es válido."""
    problems: list[str] = []
    blocks = _resource_blocks(code)

    lengths: dict[str, int | None] = {}
    for rtype, label, lines in blocks:
        if rtype != "random_string":
            continue
        length = _attr(lines, "length", r"(\d+)")
        lengths[label] = int(length) if length else None
        for flag in ("upper", "special"):
            if _attr(lines, flag, r"(false)") is None:
                problems.append(f"random_string.{label}: debe fijar {flag} = false; si no, el sufijo puede "
                                "traer caracteres que Azure no acepta en el nombre de la cuenta.")

    for rtype, label, lines in blocks:
        if rtype == "azurerm_storage_account":
            problems += _check_storage_name(label, lines, lengths)
        elif rtype == "azurerm_resource_group":
            problems += _check_group_name(label, lines)
    problems += _check_network(blocks)
    password_problems, password_labels = _check_passwords(blocks)
    problems += password_problems
    problems += _check_sql(blocks, lengths, password_labels)
    from service_checks import check_services  # import tardío: service_checks usa helpers de este módulo
    problems += check_services(code, blocks, lengths, password_labels)
    return problems