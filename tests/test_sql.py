"""Pruebas de Azure SQL, contraseñas generadas, costos y estado."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import terraform_runner  # noqa: E402
from plan_review import analyze_plan  # noqa: E402
from resource_check import check_code, check_names  # noqa: E402
from test_checks import GOOD  # noqa: E402

SQL = GOOD + '''
resource "random_password" "sql_admin" {
  length           = 24
  special          = true
  override_special = "!#$%*-_=+"
  min_upper        = 1
  min_lower        = 1
  min_numeric      = 1
  min_special      = 1
}

resource "azurerm_mssql_server" "main" {
  name                         = "sql-backups-dev-${random_string.storage_suffix.result}"
  resource_group_name          = azurerm_resource_group.backups.name
  location                     = azurerm_resource_group.backups.location
  version                      = "12.0"
  administrator_login          = "sqladmin"
  administrator_login_password = random_password.sql_admin.result
  minimum_tls_version          = "1.2"
}

resource "azurerm_mssql_database" "main" {
  name        = "sqldb-backups-dev"
  server_id   = azurerm_mssql_server.main.id
  sku_name    = "Basic"
  max_size_gb = 2
}
'''


class TestSql(unittest.TestCase):
    def test_good_sql_passes(self):
        self.assertEqual(check_code(SQL), [])
        self.assertEqual(check_names(SQL), [])

    def test_bad_variants_fail(self):
        cases = {
            "hand written password": SQL.replace("random_password.sql_admin.result", '"Abc12345!xyzxyz"'),
            "undefined random_password": SQL.replace("random_password.sql_admin.result", "random_password.other.result"),
            "reserved login": SQL.replace('"sqladmin"', '"admin"'),
            "old tls": SQL.replace('minimum_tls_version          = "1.2"', 'minimum_tls_version          = "1.0"'),
            "expensive sku": SQL.replace('"Basic"', '"P4"'),
            "big db": SQL.replace("max_size_gb = 2", "max_size_gb = 250"),
            "no suffix": SQL.replace("sql-backups-dev-${random_string.storage_suffix.result}", "sql-backups-dev"),
            "uppercase server": SQL.replace("sql-backups-dev-", "SQL-Backups-"),
            "short password": SQL.replace("length           = 24", "length           = 8"),
            "weird specials": SQL.replace('"!#$%*-_=+"', '"\'\\"@;"'),
            "special false": SQL.replace("special          = true", "special          = false"),
        }
        for name, code in cases.items():
            self.assertTrue(check_names(code), name)

    def test_secret_output_blocked(self):
        self.assertTrue(check_code(SQL + '\noutput "pw" {\n  value = random_password.sql_admin.result\n}\n'))

    def test_cost_note_on_create_only(self):
        plan = ("  # azurerm_mssql_database.main will be created\n  + resource \"azurerm_mssql_database\" \"main\" {\n  }\n\n"
                "Plan: 1 to add, 0 to change, 0 to destroy.\n")
        self.assertEqual(len(analyze_plan(plan).costs), 1)
        self.assertEqual(analyze_plan(plan.replace("created", "destroyed"), destroying=True).costs, [])


class TestGeneratedPasswords(unittest.TestCase):
    def write_state(self, state):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "terraform.tfstate").write_text(json.dumps(state), encoding="utf-8")
        return Path(tmp.name)

    def test_reads_only_random_passwords(self):
        d = self.write_state({"resources": [
            {"mode": "managed", "type": "random_password", "name": "sql_admin",
             "instances": [{"attributes": {"result": "S3cret!"}}]},
            {"mode": "managed", "type": "random_string", "name": "s", "instances": [{"attributes": {"result": "abc"}}]},
        ]})
        self.assertEqual(terraform_runner.generated_passwords(d), {"random_password.sql_admin": "S3cret!"})

    def test_missing_or_broken_state_gives_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(terraform_runner.generated_passwords(Path(tmp)), {})
            (Path(tmp) / "terraform.tfstate").write_text("{rota", encoding="utf-8")
            self.assertEqual(terraform_runner.generated_passwords(Path(tmp)), {})


if __name__ == "__main__":
    unittest.main()