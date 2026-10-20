"""Pruebas de la lista cerrada de recursos (barrera 3)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from resource_check import check_code  # noqa: E402

# Un main.tf real generado por el agente en la Fase 1 (con la corrección del prompt)
GOOD = '''
terraform {
  required_version = ">= 1.6.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "azurerm" {
  features {}
}

# comentario con "comillas" y llaves { sueltas
resource "random_string" "storage_suffix" {
  length  = 6
  upper   = false
  special = false
}

resource "azurerm_resource_group" "backups" {
  name     = "rg-backups-dev"
  location = "eastus"

  tags = {
    project     = "backend { de la app"
    environment = "dev"
    managed_by  = "terraform"
  }
}

resource "azurerm_storage_account" "backups" {
  name                            = "stbackups${random_string.storage_suffix.result}"
  resource_group_name             = azurerm_resource_group.backups.name
  location                        = azurerm_resource_group.backups.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false
}
'''


class TestResourceCheck(unittest.TestCase):
    def test_accepts_a_real_generated_file(self):
        self.assertEqual(check_code(GOOD), [])

    def test_rejects_extra_resource(self):
        code = GOOD + '\nresource "azurerm_virtual_machine" "vm" {\n  name = "x"\n}\n'
        problems = check_code(code)
        self.assertEqual(len(problems), 1)
        self.assertIn("azurerm_virtual_machine", problems[0])

    def test_rejects_other_top_level_blocks(self):
        for block in ('data "external" "x" {\n}', 'module "m" {\n  source = "./x"\n}',
                      'output "k" {\n  value = "x"\n}', 'variable "v" {\n}', 'locals {\n}'):
            self.assertTrue(check_code(GOOD + "\n" + block + "\n"), block)

    def test_rejects_stray_top_level_assignment(self):
        self.assertTrue(check_code(GOOD + '\nfoo = "bar"\n'))

    def test_rejects_other_providers(self):
        self.assertTrue(check_code(GOOD + '\nprovider "aws" {\n}\n'))

    def test_rejects_provisioner_inside_an_allowed_resource(self):
        code = GOOD.replace(
            'special = false',
            'special = false\n  provisioner "local-exec" {\n    command = "calc"\n  }',
        )
        self.assertTrue(check_code(code))

    def test_rejects_backend(self):
        code = GOOD.replace('required_version = ">= 1.6.0"',
                            'required_version = ">= 1.6.0"\n  backend "local" {\n  }')
        self.assertTrue(check_code(code))

    def test_rejects_file_reading_functions(self):
        for func in ('file("C:/secreto.txt")', 'templatefile("x", {})', 'abspath(".")'):
            code = GOOD.replace('environment = "dev"', f'environment = {func}')
            self.assertTrue(check_code(code), func)

    def test_rejects_function_hidden_in_interpolation(self):
        code = GOOD.replace('environment = "dev"', 'environment = "${file("x")}"')
        self.assertTrue(check_code(code))

    def test_rejects_heredoc(self):
        code = GOOD.replace('environment = "dev"', 'environment = <<EOT\nhola\nEOT')
        self.assertTrue(check_code(code))

    def test_rejects_unbalanced_braces(self):
        self.assertTrue(check_code(GOOD + '\nresource "random_string" "x" {\n'))
        self.assertTrue(check_code("}\n" + GOOD))

    def test_resource_hidden_in_a_comment_is_ignored_but_real_one_is_caught(self):
        commented = GOOD + '\n/* resource "azurerm_virtual_machine" "vm" {\n} */\n'
        self.assertEqual(check_code(commented), [])
        real = GOOD + '\nresource "azurerm_virtual_machine" "vm" {\n}\n'
        self.assertTrue(check_code(real))


if __name__ == "__main__":
    unittest.main()