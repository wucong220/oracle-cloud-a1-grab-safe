"""Offline contract checks against AST facts from hash-verified vendor wheels."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fixtures import ROOT, cfg

spec = importlib.util.spec_from_file_location("a1grab_contract", ROOT / "a1grab.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class VendorContract(unittest.TestCase):
    def setUp(self):
        self.contract = json.loads((ROOT / "audit" / "oci-cli-contract.json").read_text())

    def test_every_read_has_a_vendor_command(self):
        self.assertEqual(set(a.READS), set(self.contract["commands"]))
        for name in ("regions", "ads", "compartments", "instances", "images", "boot", "block", "boot_backups", "block_backups", "containers", "pools", "reservations", "subscriptions"):
            with self.subTest(name=name):
                self.assertIn("--all", self.contract["commands"][name]["options"])
        self.assertIn("--page", self.contract["commands"]["usage"]["options"])
        self.assertNotIn("--all", self.contract["commands"]["usage"]["options"])

    def test_launch_json_fields_exist_in_vendor_sdk(self):
        for model, fields in {"launch_instance_details": {"availabilityDomain", "compartmentId", "shape", "shapeConfig", "sourceDetails", "createVnicDetails", "capacityReservationId", "isAIEnterpriseEnabled", "displayName", "metadata", "freeformTags"}, "create_vnic_details": {"subnetId", "assignPublicIp", "assignIpv6Ip"}, "instance_source_via_image_details": {"sourceType", "imageId", "bootVolumeSizeInGBs", "bootVolumeVpusPerGB"}}.items():
            with self.subTest(model=model):
                self.assertLessEqual(fields, set(self.contract["models"][model].values()))

    def test_subscription_collection_and_actual_plan_field(self):
        self.assertIn("items", self.contract["models"]["subscription_collection"].values())
        self.assertIn("planType", self.contract["models"]["subscription_summary"].values())
        self.assertNotIn("compartmentId", self.contract["models"]["subscription_summary"].values())

    def test_non_2xx_raw_request_can_exit_zero(self):
        for status, code, message, expected in (("500 Internal Server Error", "InternalError", "Out of host capacity.", "CAPACITY"), ("429 Too Many Requests", "TooManyRequests", "User-rate limit exceeded.", "THROTTLED")):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                path = Path(temp)
                config = cfg(path)
                def runner(command, **kwargs):
                    return subprocess.CompletedProcess(command, 0, json.dumps({"status": status, "data": {"code": code, "message": message}, "headers": {}}), "")
                api = a.OCI(config, path, runner=runner)
                api.writes_enabled = True
                self.assertEqual(api.launch({}, "synthetic")[0], expected)

    def test_cli_file_parameters_are_literal_native_paths(self):
        with tempfile.TemporaryDirectory(prefix="a1grab path with spaces-") as temp:
            path = Path(temp)
            config = cfg(path)
            def runner(command, **kwargs):
                for option in ("--request-headers", "--request-body"):
                    value = command[command.index(option) + 1]
                    self.assertTrue(value.startswith("file://"))
                    native = value[len("file://"):]
                    self.assertTrue(Path(native).is_file())
                    self.assertEqual(native, str(path / ("request-headers.json" if option == "--request-headers" else "request-body.json")))
                return subprocess.CompletedProcess(command, 0, '{"status":"500 Error","data":{"code":"InternalError","message":"Out of host capacity."}}', "")
            api = a.OCI(config, path, runner=runner)
            api.writes_enabled = True
            self.assertEqual(api.launch({}, "synthetic")[0], "CAPACITY")


if __name__ == "__main__":
    unittest.main()
