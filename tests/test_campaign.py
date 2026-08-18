import json
import tempfile
import unittest
from pathlib import Path

import campaign


ROOT = Path(__file__).resolve().parents[1]


class CampaignTests(unittest.TestCase):
    def test_example_manifest_is_valid(self):
        manifest = campaign.load_manifest(ROOT / "example-manifest.json")
        self.assertEqual(manifest["version"], 1)

    def test_shell_string_is_rejected(self):
        data = json.loads((ROOT / "example-manifest.json").read_text(encoding="utf-8"))
        data["targets"][0]["common_commands"][0]["argv"] = "echo unsafe"
        with self.assertRaisesRegex(campaign.ManifestError, "argv array"):
            campaign.validate_manifest(data, ROOT)

    def test_plan_is_deterministic_and_paired(self):
        manifest = campaign.load_manifest(ROOT / "example-manifest.json")
        first = campaign.build_plan(manifest)
        second = campaign.build_plan(manifest)
        projection = lambda plan: [
            (row["pair_key"], row["arm"], row["interaction"]["name"], row["seed"])
            for row in plan
        ]
        self.assertEqual(projection(first), projection(second))
        self.assertEqual({row["arm"] for row in first}, {"fixed", "varied"})
        self.assertEqual(len(first), 4)
        self.assertTrue(all(0 <= row["seed"] <= 2 ** 32 - 1 for row in first))

    def test_campaign_captures_outputs_coverage_and_reports(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result"
            payload = campaign.run_campaign(ROOT / "example-manifest.json", output)
            self.assertEqual(len(payload["records"]), 4)
            self.assertTrue(all(row["status"] == "success" for row in payload["records"]))
            self.assertTrue(all(row["metrics"] for row in payload["records"]))
            self.assertTrue(all(row["coverage_artifacts"] for row in payload["records"]))
            for filename in ("results.json", "runs.csv", "summary.csv", "summary.md", "plan.json"):
                self.assertTrue((output / filename).is_file(), filename)
            differences = payload["summary"]["paired_differences"]
            line_differences = [row["difference"] for row in differences
                                if row["metric"] == "lines_covered"]
            self.assertEqual(sorted(line_differences), [4.0, 6.0])
            command = payload["records"][0]["commands"][0]
            self.assertTrue(Path(command["stdout_path"]).is_file())
            self.assertTrue(Path(command["stderr_path"]).is_file())
            with self.assertRaisesRegex(campaign.ManifestError, "already exists"):
                campaign.run_campaign(ROOT / "example-manifest.json", output)


if __name__ == "__main__":
    unittest.main()
