import json
import unittest
from pathlib import Path


DASHBOARD = (
    Path(__file__).resolve().parents[1]
    / "grafana_provisioning"
    / "dashboards"
    / "HEARTS360 Dashboards"
    / "heart360.hypertension-diabetes.program.json"
)


class NationalDetailsDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))["spec"]

    def test_details_queries_include_root_locations_for_national_filter(self):
        for panel_id in ("panel-12", "panel-dm-102"):
            with self.subTest(panel=panel_id):
                query = self.dashboard["elements"][panel_id]["spec"]["data"]["spec"][
                    "queries"
                ][0]["spec"]["query"]["spec"]["rawSql"]
                self.assertIn("child_ou.parent_id::text IS NOT DISTINCT FROM", query)
                self.assertIn("NULLIF(NULLIF(TRIM(BOTH FROM '${org_unit}'), ''), '0')", query)

    def test_details_panels_offer_xlsx_and_csv_export(self):
        for panel_id in ("panel-12", "panel-dm-102"):
            with self.subTest(panel=panel_id):
                toolbar = self.dashboard["elements"][panel_id]["spec"]["vizConfig"][
                    "spec"
                ]["options"]["toolbar"]
                self.assertTrue(toolbar.get("export", False))
                self.assertIn("xlsx", toolbar["exportFormats"])
                self.assertIn("csv", toolbar["exportFormats"])

    def test_reference_month_uses_reporting_data_and_has_empty_range_label(self):
        variable = next(
            item["spec"]
            for item in self.dashboard["variables"]
            if item["spec"]["name"] == "reference_month"
        )
        self.assertEqual(
            "postgres-hypertension-cached",
            variable["query"]["datasource"]["name"],
        )
        query = variable["query"]["spec"]["rawSql"]
        self.assertIn("heart360_patients_category", query)
        self.assertIn("AS __text", query)
        self.assertIn("Data unavailable", query)
        for panel_id in ("panel-13", "panel-dm-101"):
            with self.subTest(panel=panel_id):
                content = self.dashboard["elements"][panel_id]["spec"]["vizConfig"][
                    "spec"
                ]["options"]["content"]
                self.assertIn("${reference_month:text}", content)


if __name__ == "__main__":
    unittest.main()
