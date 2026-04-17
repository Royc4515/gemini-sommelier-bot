"""
tests/test_wine_inventory.py

Unit tests for WineInventory — all network calls are mocked so
these run fully offline.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Allow imports from the project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wine_inventory import WineInventory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_CSV = """\
יקב,שם היין,בציר,זנים,סטטוס חדש,מטרה,המלצת פתיחה
פלם,Classico,2021,Cabernet,Closed,שמירה,Hold until 2026
רזיאל,Raziel,2022,Syrah,Open,שתייה,Ready to Drink 🍷
קסטל,Grand Vin,2015,Merlot,Finished,שמירה,Hold
,Missing Winery,2020,Cabernet,Closed,שמירה,Hold
פלם,,2019,Carignan,Closed,שמירה,Hold
"""


class TestParseInventory(unittest.TestCase):
    """WineInventory.parse_inventory — parsing and filtering logic."""

    def setUp(self):
        os.environ["WINE_CSV_URL"] = "https://fake-url/wines.csv"
        self.inventory = WineInventory()

    def test_returns_list_of_dicts(self):
        rows = self.inventory.parse_inventory(SAMPLE_CSV)
        self.assertIsInstance(rows, list)
        self.assertTrue(all(isinstance(r, dict) for r in rows))

    def test_valid_rows_are_included(self):
        rows = self.inventory.parse_inventory(SAMPLE_CSV)
        names = [r["שם היין"] for r in rows]
        self.assertIn("Classico", names)
        self.assertIn("Raziel", names)

    def test_finished_bottles_are_excluded(self):
        rows = self.inventory.parse_inventory(SAMPLE_CSV)
        statuses = [r["סטטוס חדש"] for r in rows]
        self.assertNotIn("Finished", statuses)

    def test_rows_missing_winery_are_excluded(self):
        rows = self.inventory.parse_inventory(SAMPLE_CSV)
        wineries = [r["יקב"].strip() for r in rows]
        self.assertNotIn("", wineries)

    def test_rows_missing_wine_name_are_excluded(self):
        rows = self.inventory.parse_inventory(SAMPLE_CSV)
        names = [r["שם היין"].strip() for r in rows]
        self.assertNotIn("", names)

    def test_missing_expected_column_handled_gracefully(self):
        # CSV without the 'סטטוס חדש' column
        corrupted_csv = "יקב,שם היין,בציר\nפלם,Classico,2021\n"
        rows = self.inventory.parse_inventory(corrupted_csv)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["שם היין"], "Classico")

    def test_empty_csv_returns_empty_list(self):
        rows = self.inventory.parse_inventory("יקב,שם היין\n")
        self.assertEqual(rows, [])


class TestGetFormattedInventory(unittest.TestCase):
    """WineInventory.get_formatted_inventory — output format."""

    def setUp(self):
        os.environ["WINE_CSV_URL"] = "https://fake-url/wines.csv"
        self.inventory = WineInventory()

    @patch.object(WineInventory, "fetch_inventory", return_value=SAMPLE_CSV)
    def test_output_is_non_empty_string(self, _mock):
        result = self.inventory.get_formatted_inventory()
        self.assertIsInstance(result, str)
        self.assertTrue(len(result) > 0)

    @patch.object(WineInventory, "fetch_inventory", return_value=SAMPLE_CSV)
    def test_excluded_bottles_absent_from_output(self, _mock):
        result = self.inventory.get_formatted_inventory()
        self.assertNotIn("Grand Vin", result)   # Finished — should be excluded

    @patch.object(WineInventory, "fetch_inventory", return_value=SAMPLE_CSV)
    def test_valid_bottles_present_in_output(self, _mock):
        result = self.inventory.get_formatted_inventory()
        self.assertIn("פלם", result)
        self.assertIn("רזיאל", result)

    @patch.object(WineInventory, "fetch_inventory", return_value="יקב,שם היין\n")
    def test_empty_inventory_returns_hebrew_message(self, _mock):
        result = self.inventory.get_formatted_inventory()
        self.assertIn("ריק", result)

    @patch.object(WineInventory, "fetch_inventory", return_value=SAMPLE_CSV)
    def test_output_contains_opening_recommendation(self, _mock):
        result = self.inventory.get_formatted_inventory()
        self.assertIn("המלצת פתיחה", result)

    @patch.object(WineInventory, "fetch_inventory", return_value=SAMPLE_CSV)
    def test_rows_are_numbered(self, _mock):
        result = self.inventory.get_formatted_inventory()
        self.assertIn("1.", result)
        self.assertIn("2.", result)


class TestFetchInventory(unittest.TestCase):
    """WineInventory.fetch_inventory — network operations."""
    
    def setUp(self):
        os.environ["WINE_CSV_URL"] = "https://fake-url/wines.csv"
        self.inventory = WineInventory()

    @patch("urllib.request.urlopen")
    def test_fetch_timeout_raises_error(self, mock_urlopen):
        import urllib.error
        # Mock a timeout error
        mock_urlopen.side_effect = urllib.error.URLError("timeout")
        
        with self.assertRaises(urllib.error.URLError):
            self.inventory.fetch_inventory()

if __name__ == "__main__":
    unittest.main()
