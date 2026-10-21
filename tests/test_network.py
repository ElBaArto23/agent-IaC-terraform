"""Pruebas de la red básica: lista cerrada, rangos de IP y nombres."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import prompt_template  # noqa: E402
from plan_review import analyze_plan  # noqa: E402
from resource_check import check_code, check_names  # noqa: E402
from test_checks import GOOD  # noqa: E402

NET = '''
resource "azurerm_virtual_network" "main" {
  name                = "vnet-red-dev"
  address_space       = ["10.0.0.0/16"]
  location            = azurerm_resource_group.backups.location
  resource_group_name = azurerm_resource_group.backups.name

  tags = {
    project     = "red"
    environment = "dev"
    managed_by  = "terraform"
  }
}

resource "azurerm_subnet" "main" {
  name                 = "snet-red-dev"
  resource_group_name  = azurerm_resource_group.backups.name
  virtual_network_name = azurerm_virtual_network.main.name
  address_prefixes     = ["10.0.1.0/24"]
}

resource "azurerm_network_security_group" "main" {
  name                = "nsg-red-dev"
  location            = azurerm_resource_group.backups.location
  resource_group_name = azurerm_resource_group.backups.name

  tags = {
    project     = "red"
    environment = "dev"
    managed_by  = "terraform"
  }
}

resource "azurerm_subnet_network_security_group_association" "main" {
  subnet_id                 = azurerm_subnet.main.id
  network_security_group_id = azurerm_network_security_group.main.id
}
'''

CODE = GOOD + NET


def edit(old: str, new: str) -> str:
    assert old in CODE, old
    return CODE.replace(old, new)


class TestNetworkAllowed(unittest.TestCase):
    def test_basic_network_is_accepted(self):
        self.assertEqual(check_code(CODE), [])
        self.assertEqual(check_names(CODE), [])

    def test_network_in_the_plan_is_not_blocked(self):
        plan = "\n".join(f"  # {a} will be created" for a in (
            "azurerm_virtual_network.main", "azurerm_subnet.main", "azurerm_network_security_group.main",
            "azurerm_subnet_network_security_group_association.main")
        ) + "\n\nPlan: 4 to add, 0 to change, 0 to destroy.\n"
        self.assertEqual(analyze_plan(plan).blocked, [])

    def test_custom_security_rules_are_rejected(self):
        inline = edit('resource_group_name = azurerm_resource_group.backups.name\n\n  tags = {\n    project     = "red"\n    environment = "dev"\n    managed_by  = "terraform"\n  }\n}\n\nresource "azurerm_subnet_network',
                      'resource_group_name = azurerm_resource_group.backups.name\n\n  security_rule {\n    name = "ssh"\n    source_address_prefix = "*"\n  }\n}\n\nresource "azurerm_subnet_network')
        self.assertTrue(any("construcción no permitida" in p for p in check_code(inline)))

    def test_separate_security_rule_resource_is_rejected(self):
        code = CODE + '\nresource "azurerm_network_security_rule" "ssh" {\n  name = "ssh"\n}\n'
        self.assertTrue(any("azurerm_network_security_rule" in p for p in check_code(code)))

    def test_too_many_resources_are_rejected(self):
        extra = "".join(f'\nresource "random_string" "r{i}" {{\n  length = 4\n}}\n' for i in range(20))
        problems = check_code(CODE + extra)
        self.assertTrue(any("máximo permitido" in p for p in problems))

    def test_prompt_describes_the_network(self):
        for word in ("virtual network", "subnet", "azurerm_subnet_network_security_group_association",
                     "NO llevan tags", "security_rule", prompt_template.OUT_OF_SCOPE):
            self.assertIn(word, prompt_template.SYSTEM_PROMPT)


class TestAddressing(unittest.TestCase):
    def problems(self, old, new):
        return check_names(edit(old, new))

    def test_other_private_ranges_are_fine(self):
        for vnet, subnet in (("172.16.0.0/12", "172.16.5.0/24"), ("192.168.0.0/16", "192.168.10.0/24"),
                             ("10.1.0.0/16", "10.1.0.0/29")):
            code = edit('["10.0.0.0/16"]', f'["{vnet}"]').replace('["10.0.1.0/24"]', f'["{subnet}"]')
            self.assertEqual(check_names(code), [], (vnet, subnet))

    def test_public_range_is_rejected(self):
        problems = self.problems('["10.0.0.0/16"]', '["8.8.0.0/16"]')
        self.assertTrue(any("no es un rango privado" in p for p in problems))

    def test_range_just_outside_172_16_12_is_rejected(self):
        problems = self.problems('["10.0.0.0/16"]', '["172.32.0.0/16"]')
        self.assertTrue(any("no es un rango privado" in p for p in problems))

    def test_invalid_cidr_is_rejected(self):
        for bad in ("10.0.0.5/16", "10.0.0.0/33", "banana", ""):
            problems = self.problems('["10.0.0.0/16"]', f'["{bad}"]')
            self.assertTrue(any("no es un CIDR válido" in p for p in problems), bad)
        # Sin prefijo, Python lo lee como /32: es válido pero demasiado pequeño
        self.assertTrue(any("demasiado pequeño" in p for p in self.problems('["10.0.0.0/16"]', '["10.0.0.0"]')))

    def test_too_small_network_is_rejected(self):
        problems = self.problems('["10.0.1.0/24"]', '["10.0.1.0/30"]')
        self.assertTrue(any("demasiado pequeño" in p for p in problems))

    def test_subnet_outside_the_network_is_rejected(self):
        problems = self.problems('["10.0.1.0/24"]', '["10.9.1.0/24"]')
        self.assertTrue(any("no está dentro del address_space" in p for p in problems))

    def test_overlapping_subnets_are_rejected(self):
        second = NET.split('resource "azurerm_subnet" "main"')[1].split("resource ")[0]
        extra = 'resource "azurerm_subnet" "otra"' + second.replace('"snet-red-dev"', '"snet-otra-dev"') \
            .replace("10.0.1.0/24", "10.0.1.128/25")
        problems = check_names(CODE + "\n" + extra)
        self.assertTrue(any("se solapa" in p for p in problems))

    def test_two_separate_subnets_are_fine(self):
        second = NET.split('resource "azurerm_subnet" "main"')[1].split("resource ")[0]
        extra = 'resource "azurerm_subnet" "otra"' + second.replace('"snet-red-dev"', '"snet-otra-dev"') \
            .replace("10.0.1.0/24", "10.0.2.0/24")
        self.assertEqual(check_names(CODE + "\n" + extra), [])

    def test_subnet_with_unknown_network_is_rejected(self):
        problems = self.problems("azurerm_virtual_network.main.name", "azurerm_virtual_network.fantasma.name")
        self.assertTrue(any("virtual_network_name" in p for p in problems))

    def test_unverifiable_address_space_is_rejected(self):
        problems = self.problems('["10.0.0.0/16"]', "[var.rango]")
        self.assertTrue(any("texto fijo" in p for p in problems))

    def test_multiline_address_space_is_read(self):
        code = edit('["10.0.0.0/16"]', '[\n    "10.0.0.0/16",\n    "10.5.0.0/16",\n  ]')
        self.assertEqual(check_names(code), [])
        bad = edit('["10.0.0.0/16"]', '[\n    "10.0.0.0/16",\n    "8.8.8.0/24",\n  ]')
        self.assertTrue(check_names(bad))

    def test_network_names(self):
        for old, bad in (('"vnet-red-dev"', '"vnet red"'), ('"vnet-red-dev"', '"-vnet"'),
                         ('"snet-red-dev"', '"snet-' + "a" * 80 + '"'), ('"nsg-red-dev"', '"nsg-${var.x}"'),
                         ('"vnet-red-dev"', '"v' + "a" * 64 + '"')):
            self.assertTrue(self.problems(old, bad), bad)
        self.assertEqual(self.problems('"vnet-red-dev"', '"vnet_red.dev-1"'), [])


if __name__ == "__main__":
    unittest.main()