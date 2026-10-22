import base64
import csv
import hashlib
import importlib.util
import io
import tempfile
from pathlib import Path
import unittest
import zipfile
import json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("patch_cli", ROOT / "tools" / "patch_cli_dependency.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class DependencyPatch(unittest.TestCase):
    def test_runtime_attestation_blocks_modified_dependency_before_cli(self):
        spec = importlib.util.spec_from_file_location("a1grab_runtime", ROOT / "a1grab.py")
        a = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(a)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "pyvenv.cfg").write_text("test-only")
            site = root / ("Lib/site-packages" if __import__("os").name == "nt" else "lib/python3.12/site-packages")
            site.mkdir(parents=True)
            target = site / "test-ca-data"
            target.write_bytes(b"synthetic CA fixture, not a certificate")
            policy = root / "policy.json"
            policy.write_text(json.dumps({"required_file_sha256": {"test-ca-data": hashlib.sha256(target.read_bytes()).hexdigest()}}))
            binary = root / ("Scripts/oci.exe" if __import__("os").name == "nt" else "bin/oci")
            api = a.OCI({"OCI_BIN": str(binary), "OCI_CONFIG_FILE": "mock", "OCI_PROFILE": "DEFAULT"}, root)
            api.verify_dependency_policy(policy)
            target.write_bytes(b"changed")
            with self.assertRaisesRegex(a.Stop, "PATCHED_OCI_DEPENDENCY_HASH_MISMATCH"):
                api.verify_dependency_policy(policy)

    def test_only_dependency_metadata_and_record_change(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "source.whl", Path(temp) / "patched.whl"
            files = {"oci_cli/runtime.py": b"# unchanged test fixture\n", "oci_cli-3.95.1.dist-info/METADATA": b"Metadata-Version: 2.1\nName: oci-cli\nVersion: 3.95.1\n" + p.OLD + b"\n", "oci_cli-3.95.1.dist-info/RECORD": b""}
            with zipfile.ZipFile(source, "w") as archive:
                for name, data in files.items():
                    archive.writestr(name, data)
            sha = hashlib.sha256(source.read_bytes()).hexdigest()
            evidence = p.patch_wheel(source, target, sha)
            first = target.read_bytes()
            p.patch_wheel(source, target, sha)
            self.assertEqual(first, target.read_bytes())
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(files["oci_cli/runtime.py"], archive.read("oci_cli/runtime.py"))
                self.assertIn(p.NEW, archive.read("oci_cli-3.95.1.dist-info/METADATA"))
                for name, value, length in csv.reader(io.StringIO(archive.read("oci_cli-3.95.1.dist-info/RECORD").decode())):
                    if not value:
                        continue
                    data = archive.read(name)
                    self.assertEqual(int(length), len(data))
                    self.assertEqual(value, "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("="))
            self.assertEqual(len(evidence["changed_files"]), 2)

    def test_unexpected_source_hash_stops_without_output(self):
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp) / "source.whl", Path(temp) / "patched.whl"
            source.write_bytes(b"untrusted")
            with self.assertRaisesRegex(AssertionError, "hash mismatch"):
                p.patch_wheel(source, target)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
