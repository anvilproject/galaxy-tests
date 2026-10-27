"""Billing query results and API failure propagation."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "reconcile", Path(__file__).with_name("anvil_reconcile_costs.py")
)
reconcile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reconcile)


class BillingQueryTest(unittest.TestCase):
    def query(self):
        return reconcile.query_bigquery(
            project="test-project", billing_table="test-project.billing.export",
            github_run_id="123", resource_keys=["batch-a", "batch-b"],
            usage_start=datetime(2026, 10, 25, tzinfo=timezone.utc),
            usage_end=datetime(2026, 10, 26, tzinfo=timezone.utc),
        )

    @patch("google.cloud.bigquery.Client")
    def test_typed_rows_and_parameterized_labels(self, client_type):
        client = client_type.return_value.__enter__.return_value
        client.query.return_value.result.return_value = [
            {"github_run_id": "123", "batch_job_id": None, "cost_at_list": "1.25"},
            {"github_run_id": "123", "batch_job_id": None, "cost_at_list": "0.25"},
            {"github_run_id": None, "batch_job_id": "batch-a", "cost_at_list": "2.5"},
            {"github_run_id": None, "batch_job_id": "batch-a", "cost_at_list": "0.5"},
        ]
        self.assertEqual(self.query(), (1.5, {"batch-a": 3.0}))
        client_type.assert_called_once_with(project="test-project")
        sql = client.query.call_args.args[0]
        config = client.query.call_args.kwargs["job_config"]
        self.assertIn("SUM(cost_at_list)", sql)
        self.assertIn("@github_run_id", sql)
        self.assertIn("IN UNNEST(@resource_keys)", sql)
        params = [p.to_api_repr() for p in config.query_parameters]
        self.assertEqual(params[0]["parameterValue"], {"value": "123"})
        self.assertEqual(params[1]["parameterValue"], {
            "arrayValues": [{"value": "batch-a"}, {"value": "batch-b"}]
        })

    @patch("google.cloud.bigquery.Client")
    def test_query_failure_is_not_a_zero_cost_result(self, client_type):
        from google.api_core.exceptions import Forbidden
        client = client_type.return_value.__enter__.return_value
        client.query.return_value.result.side_effect = Forbidden("billing access denied")
        with self.assertRaisesRegex(Forbidden, "billing access denied"):
            self.query()
        client_type.return_value.__exit__.assert_called_once()

    @patch("google.cloud.bigquery.Client")
    def test_empty_result_is_supported(self, client_type):
        client_type.return_value.__enter__.return_value.query.return_value.result.return_value = []
        self.assertEqual(self.query(), (0.0, {}))


if __name__ == "__main__":
    unittest.main()
