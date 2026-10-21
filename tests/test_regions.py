"""Pruebas del cambio de región."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import regions  # noqa: E402


class TestRegions(unittest.TestCase):
    def test_detects_region_errors(self):
        for text in ("Error: RegionDoesNotAllowProvisioning", "Location is not accepting creation of new Windows Azure SQL Database servers",
                     "SkuNotAvailable", "ResourceQuotaExceeded", "LocationNotAvailableForResourceType",
                     "The region is experiencing high demand"):
            self.assertTrue(regions.is_region_error(text), text)

    def test_ignores_code_errors(self):
        for text in ("", "Error: Invalid value for argument", "Error: name already taken", None):
            self.assertFalse(regions.is_region_error(text), text)

    def test_current_region_reads_the_literal(self):
        self.assertEqual(regions.current_region('  location = "eastus2"\n'), "eastus2")
        self.assertIsNone(regions.current_region("location = azurerm_resource_group.rg.location"))

    def test_next_region_skips_tried(self):
        c = ["a1b", "c2d", "e3f"]
        self.assertEqual(regions.next_region(["a1b"], c), "c2d")
        self.assertIsNone(regions.next_region(c, c))

    def test_switch_only_changes_literal_old_region(self):
        code = 'location = "eastus2"\nlocation = azurerm_resource_group.rg.location\nlocation = "westus2"\n'
        out = regions.switch_region(code, "eastus2", "centralus")
        self.assertEqual(out, 'location = "centralus"\nlocation = azurerm_resource_group.rg.location\nlocation = "westus2"\n')

    def test_switch_rejects_invalid_region(self):
        with self.assertRaises(ValueError):
            regions.switch_region('location = "eastus2"', "eastus2", 'x"; rm')

    def test_env_override_and_invalid_values(self):
        with mock.patch.dict(os.environ, {"AZURE_FALLBACK_REGIONS": "Westus2, centralus"}):
            self.assertEqual(regions.fallback_regions(), ["westus2", "centralus"])
        with mock.patch.dict(os.environ, {"AZURE_FALLBACK_REGIONS": 'bad"region'}):
            self.assertEqual(regions.fallback_regions(), list(regions.DEFAULT_FALLBACK))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(regions.fallback_regions(), list(regions.DEFAULT_FALLBACK))


if __name__ == "__main__":
    unittest.main()