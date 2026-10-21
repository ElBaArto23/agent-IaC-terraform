"""Pruebas de Cosmos DB, Key Vault, máquinas virtuales, App Service y Container Apps."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prompt_template  # noqa: E402
from plan_review import analyze_plan  # noqa: E402
from resource_check import check_code, check_names  # noqa: E402
from test_checks import GOOD  # noqa: E402
from test_network import NET  # noqa: E402
from test_sql import SQL  # noqa: E402

RG = "azurerm_resource_group.backups"

COSMOS = f'''
resource "azurerm_cosmosdb_account" "main" {{
  name                = "cosmos-app-dev-${{random_string.storage_suffix.result}}"
  location            = {RG}.location
  resource_group_name = {RG}.name
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"
  free_tier_enabled   = true

  consistency_policy {{
    consistency_level = "Session"
  }}

  geo_location {{
    location          = {RG}.location
    failover_priority = 0
  }}
}}

resource "azurerm_cosmosdb_sql_database" "main" {{
  name                = "appdb"
  resource_group_name = {RG}.name
  account_name        = azurerm_cosmosdb_account.main.name
}}
'''

KEYVAULT = f'''
data "azurerm_client_config" "current" {{}}

resource "azurerm_key_vault" "main" {{
  name                       = "kv-app-dev-${{random_string.storage_suffix.result}}"
  location                   = {RG}.location
  resource_group_name        = {RG}.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  purge_protection_enabled   = false
}}
'''

VM = f'''
resource "azurerm_network_interface" "main" {{
  name                = "nic-app-dev"
  location            = {RG}.location
  resource_group_name = {RG}.name

  ip_configuration {{
    name                          = "internal"
    subnet_id                     = azurerm_subnet.main.id
    private_ip_address_allocation = "Dynamic"
  }}
}}

resource "azurerm_linux_virtual_machine" "main" {{
  name                            = "vm-app-dev"
  location                        = {RG}.location
  resource_group_name             = {RG}.name
  size                            = "Standard_B1s"
  admin_username                  = "azureuser"
  admin_password                  = random_password.sql_admin.result
  disable_password_authentication = false
  network_interface_ids           = [azurerm_network_interface.main.id]

  os_disk {{
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }}

  source_image_reference {{
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }}
}}
'''

WEB = f'''
resource "azurerm_service_plan" "main" {{
  name                = "plan-app-dev"
  location            = {RG}.location
  resource_group_name = {RG}.name
  os_type             = "Linux"
  sku_name            = "F1"
}}

resource "azurerm_linux_web_app" "main" {{
  name                = "app-app-dev-${{random_string.storage_suffix.result}}"
  location            = {RG}.location
  resource_group_name = {RG}.name
  service_plan_id     = azurerm_service_plan.main.id
  https_only          = true

  site_config {{}}
}}
'''

CONTAINERS = f'''
resource "azurerm_log_analytics_workspace" "main" {{
  name                = "log-app-dev"
  location            = {RG}.location
  resource_group_name = {RG}.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  daily_quota_gb      = 0.5
}}

resource "azurerm_container_app_environment" "main" {{
  name                       = "cae-app-dev"
  location                   = {RG}.location
  resource_group_name        = {RG}.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
}}

resource "azurerm_container_app" "main" {{
  name                         = "ca-app-dev"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = {RG}.name
  revision_mode                = "Single"

  template {{
    max_replicas = 1

    container {{
      name   = "app"
      image  = "mcr.microsoft.com/k8se/quickstart:latest"
      cpu    = 0.25
      memory = "0.5Gi"
    }}
  }}
}}
'''

ALL = SQL + NET + COSMOS + KEYVAULT + VM + WEB + CONTAINERS


def sample(*parts):
    return SQL + NET + "".join(parts)


class TestGoodSamples(unittest.TestCase):
    def test_each_service_passes(self):
        for name, part in (("cosmos", COSMOS), ("keyvault", KEYVAULT), ("vm", VM), ("web", WEB),
                           ("containers", CONTAINERS)):
            code = sample(part)
            self.assertEqual(check_code(code), [], name)
            self.assertEqual(check_names(code), [], name)

    def test_everything_together_is_within_the_limit(self):
        # 19 recursos con la base; justo dentro del tope de 20
        self.assertEqual(check_code(ALL), [])
        self.assertEqual(check_names(ALL), [])


class TestDataSource(unittest.TestCase):
    def test_only_client_config_is_allowed(self):
        self.assertTrue(check_code(sample(KEYVAULT.replace("azurerm_client_config", "external"))))
        self.assertTrue(check_code(sample('\ndata "http" "x" {\n}\n')))

    def test_plan_allows_client_config_but_not_other_data(self):
        ok = "  # data.azurerm_client_config.current will be read during apply\n\nPlan: 1 to add, 0 to change, 0 to destroy.\n"
        bad = ok.replace("azurerm_client_config", "external")
        self.assertEqual(analyze_plan(ok).blocked, [])
        self.assertTrue(analyze_plan(bad).blocked)


def mutate(code, old, new):
    assert old in code, old
    return code.replace(old, new, 1)


class TestBadVariants(unittest.TestCase):
    def assert_all_rejected(self, base, cases):
        for name, (old, new) in cases.items():
            self.assertTrue(check_names(mutate(base, old, new)), name)

    def test_cosmos(self):
        base = sample(COSMOS)
        self.assert_all_rejected(base, {
            "no free tier": ("free_tier_enabled   = true", "free_tier_enabled   = false"),
            "no suffix": ("cosmos-app-dev-${random_string.storage_suffix.result}", "cosmos-app-dev"),
            "uppercase": ("cosmos-app-dev-", "Cosmos-App-"),
            "wrong kind": ('"GlobalDocumentDB"', '"MongoDB"'),
            "two regions": ("  geo_location {", "  geo_location {\n    location = \"x\"\n    failover_priority = 1\n  }\n\n  geo_location {"),
            "multi write": ("  free_tier_enabled   = true", "  free_tier_enabled   = true\n  multiple_write_locations_enabled = true"),
            "throughput": ('name                = "appdb"', 'name                = "appdb"\n  throughput = 4000'),
            "db without account": ("account_name        = azurerm_cosmosdb_account.main.name", 'account_name = "x"'),
        })

    def test_key_vault(self):
        base = sample(KEYVAULT)
        self.assert_all_rejected(base, {
            "no suffix": ("kv-app-dev-${random_string.storage_suffix.result}", "kv-app-dev"),
            "too long": ("kv-app-dev-", "kv-" + "a" * 20 + "-"),
            "premium": ('"standard"', '"premium"'),
            "no rbac": ("rbac_authorization_enabled = true", "rbac_authorization_enabled = false"),
            "purge protection": ("purge_protection_enabled   = false", "purge_protection_enabled   = true"),
            "literal tenant": ("data.azurerm_client_config.current.tenant_id", '"00000000-0000-0000-0000-000000000000"'),
            "undefined data": ("data.azurerm_client_config.current.tenant_id", "data.azurerm_client_config.otro.tenant_id"),
            "access policy": ("  purge_protection_enabled   = false", "  purge_protection_enabled   = false\n  access_policy {\n    tenant_id = \"x\"\n  }"),
            "starts with digit": ("kv-app-dev-", "1kv-app-"),
        })

    def test_vm(self):
        base = sample(VM)
        self.assert_all_rejected(base, {
            "big size": ('"Standard_B1s"', '"Standard_D16s_v5"'),
            "hand written password": ("random_password.sql_admin.result\n  disable", '"Abc12345!xyzxyz"\n  disable'),
            "reserved user": ('"azureuser"', '"root"'),
            "uppercase user": ('"azureuser"', '"AzureUser"'),
            "password auth on": ("disable_password_authentication = false", "disable_password_authentication = true"),
            "premium disk": ('"Standard_LRS"', '"Premium_LRS"'),
            "foreign image": ('"Canonical"', '"SomeoneElse"'),
            "public ip": ('private_ip_address_allocation = "Dynamic"', 'private_ip_address_allocation = "Dynamic"\n    public_ip_address_id = "x"'),
            "static ip": ('private_ip_address_allocation = "Dynamic"', 'private_ip_address_allocation = "Static"'),
            "unknown subnet": ("subnet_id                     = azurerm_subnet.main.id", "subnet_id = azurerm_subnet.zzz.id"),
            "unknown nic": ("[azurerm_network_interface.main.id]", "[azurerm_network_interface.zzz.id]"),
            "custom data": ("  size ", '  custom_data = "x"\n  size '),
            "long name": ('"vm-app-dev"', '"vm-' + "a" * 70 + '"'),
        })

    def test_vm_without_any_password_resource(self):
        code = GOOD + NET + VM
        self.assertTrue(any("admin_password" in p for p in check_names(code)))

    def test_app_service(self):
        base = sample(WEB)
        self.assert_all_rejected(base, {
            "expensive plan": ('"F1"', '"P3v3"'),
            "windows": ('"Linux"', '"Windows"'),
            "no suffix": ("app-app-dev-${random_string.storage_suffix.result}", "app-app-dev"),
            "http allowed": ("https_only          = true", "https_only          = false"),
            "unknown plan": ("azurerm_service_plan.main.id", "azurerm_service_plan.zzz.id"),
            "no site_config": ("  site_config {}", ""),
        })

    def test_containers(self):
        base = sample(CONTAINERS)
        self.assert_all_rejected(base, {
            "dockerhub image": ("mcr.microsoft.com/k8se/quickstart:latest", "docker.io/evil/miner:latest"),
            "lookalike image": ("mcr.microsoft.com/k8se", "mcr.microsoft.com.evil.io/k8se"),
            "big cpu": ("0.25", "4"),
            "big memory": ('"0.5Gi"', '"8Gi"'),
            "bad memory format": ('"0.5Gi"', '"512MB"'),
            "many replicas": ("max_replicas = 1", "max_replicas = 30"),
            "no log quota": ("  daily_quota_gb      = 0.5\n", ""),
            "huge log quota": ("daily_quota_gb      = 0.5", "daily_quota_gb      = 50"),
            "long retention": ("retention_in_days   = 30", "retention_in_days   = 730"),
            "multiple revisions": ('"Single"', '"Multiple"'),
            "unknown env": ("azurerm_container_app_environment.main.id", "azurerm_container_app_environment.zzz.id"),
            "unknown workspace": ("azurerm_log_analytics_workspace.main.id", "azurerm_log_analytics_workspace.zzz.id"),
            "uppercase app name": ('"ca-app-dev"', '"CA-App"'),
        })

    def test_second_container_with_untrusted_image_is_rejected(self):
        extra = '    container {\n      name   = "b"\n      image  = "docker.io/x/y"\n      cpu    = 0.25\n      memory = "0.5Gi"\n    }\n'
        code = sample(CONTAINERS).replace('      memory = "0.5Gi"\n    }\n', '      memory = "0.5Gi"\n    }\n' + extra)
        self.assertTrue(any("docker.io" in p for p in check_names(code)))

    def test_cost_notes_for_new_services(self):
        for rtype in ("azurerm_linux_virtual_machine", "azurerm_service_plan", "azurerm_cosmosdb_account",
                      "azurerm_log_analytics_workspace", "azurerm_container_app", "azurerm_key_vault"):
            plan = (f"  # {rtype}.main will be created\n\nPlan: 1 to add, 0 to change, 0 to destroy.\n")
            self.assertEqual(len(analyze_plan(plan).costs), 1, rtype)

    def test_prompt_mentions_new_services(self):
        for word in ("azurerm_cosmosdb_account", "azurerm_key_vault", "azurerm_linux_virtual_machine",
                     "azurerm_linux_web_app", "azurerm_container_app", "mcr.microsoft.com", "APIM"):
            self.assertIn(word, prompt_template.SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()