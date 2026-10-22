#!/usr/bin/env python3
"""Offline evidence generation from Git objects and downloaded wheel archives.

Reads archives as data/AST; never imports/installs OCI or reads user credentials.
"""
import argparse
import ast
import email
import hashlib
import json
from pathlib import Path
import re
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BASE = "b6bc5cb20e0d8f5e51e4a4c1691c5573ef3c9e61"
GENERATED = {
    "regions": ("identity_cli.py", "list_region_subscriptions"),
    "tenancy": ("identity_cli.py", "get_tenancy"),
    "ads": ("identity_cli.py", "list_availability_domains"),
    "compartments": ("identity_cli.py", "list_compartments"),
    "instances": ("compute_cli.py", "list_instances"),
    "instance": ("compute_cli.py", "get_instance"),
    "images": ("compute_cli.py", "list_images"),
    "subnet": ("virtualnetwork_cli.py", "get_subnet"),
    "boot": ("blockstorage_cli.py", "list_boot_volumes"),
    "block": ("blockstorage_cli.py", "list_volumes"),
    "boot_backups": ("blockstorage_cli.py", "list_boot_volume_backups"),
    "block_backups": ("blockstorage_cli.py", "list_volume_backups"),
    "containers": ("containerinstance_cli.py", "list_container_instances"),
    "pools": ("computemanagement_cli.py", "list_instance_pools"),
    "reservations": ("compute_cli.py", "list_compute_capacity_reservations"),
    "availability": ("limits_cli.py", "get_resource_availability"),
    "subscriptions": ("subscriptionservice_cli.py", "list_subscriptions"),
    "usage": ("usageapi_cli.py", "request_summarized_usages"),
}


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def metadata(wheel):
    with zipfile.ZipFile(wheel) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
        return email.message_from_bytes(archive.read(name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    args = parser.parse_args()
    filenames = git("ls-tree", "-r", "--name-only", BASE).decode().splitlines()
    manifest = {"repository": "https://github.com/FBX-Dev/oracle-cloud-a1-grab", "audit_commit": BASE, "tree": git("rev-parse", BASE + "^{tree}").decode().strip(), "stars_at_lookup": 1, "stars_lookup_date": "2026-10-22 Asia/Shanghai", "stars_source": "GitHub repository page, native in-app browser and public web retrieval; anonymous API was rate-limited", "files": [{"path": name, "bytes": len(git("show", BASE + ":" + name)), "sha256": hashlib.sha256(git("show", BASE + ":" + name)).hexdigest()} for name in filenames]}
    save(ROOT / "audit" / "source-manifest.json", manifest)
    commits = git("rev-list", BASE).decode().splitlines()
    findings = []
    scans = 0
    for commit in commits:
        for name in git("ls-tree", "-r", "--name-only", commit).decode().splitlines():
            data = git("show", commit + ":" + name)
            scans += 1
            # Record paths/rules only, NEVER matched key material.
            for rule, pattern in {"private-key-material": rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", "credential-assignment": rb'(?im)^\s*(?:TK|TOKEN|pushplus_token|pass_phrase)\s*=\s*[\"\x27][a-zA-Z0-9+/]{24,}[\"\x27]'}.items():
                if re.search(pattern, data):
                    findings.append({"commit": commit, "path": name, "rule": rule})
    save(ROOT / "audit" / "history-scan.json", {"commits": commits, "file_versions_scanned": scans, "rules": ["private-key-material", "credential-assignment"], "findings": findings, "note": "Pattern scan supplements full manual review of the fixed source tree; absence is not proof of no secret."})
    packages = []
    for wheel in sorted(args.wheelhouse.glob("*.whl")):
        info = metadata(wheel)
        packages.append({"name": info["Name"], "version": info["Version"], "wheel": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()})
    provenance_path = args.wheelhouse / "local-patch-provenance.json"
    provenance = json.loads(provenance_path.read_text()) if provenance_path.exists() else None
    save(ROOT / "audit" / "dependency-manifest.json", {"target": "CPython 3.12 Windows amd64", "source": "PyPI HTTPS wheel artifacts; local CLI metadata patch explicitly recorded; not installed", "local_patch": provenance, "packages": packages})
    lock = ["# CPython 3.12 / Windows amd64 only. No auto-install; see docs/DEPLOYMENT.md.", "# Every transitive dependency is version- and hash-pinned.", "--only-binary=:all:"]
    lock += [f'{p["name"]}=={p["version"]} --hash=sha256:{p["sha256"]}' for p in sorted(packages, key=lambda p: p["name"].lower())]
    (ROOT / "requirements-oci-windows-py312.lock").write_text("\n".join(lock) + "\n", encoding="utf-8")
    cli = next(args.wheelhouse.glob("oci_cli-3.95.1-*.whl"))
    sdk = next(args.wheelhouse.glob("oci-2.189.0-*.whl"))
    contract = {"cli_version": "3.95.1", "sdk_version": "2.189.0", "cli_wheel_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(), "sdk_wheel_sha256": hashlib.sha256(sdk.read_bytes()).hexdigest(), "commands": {}, "models": {}}
    with zipfile.ZipFile(cli) as archive:
        parsed = {}
        for key, (basename, function) in GENERATED.items():
            paths = [n for n in archive.namelist() if n.endswith("/generated/" + basename)]
            assert len(paths) == 1, paths
            path = paths[0]
            if path not in parsed:
                parsed[path] = ast.parse(archive.read(path).decode())
            definition = next(n for n in parsed[path].body if isinstance(n, ast.FunctionDef) and n.name == function)
            options = []
            for dec in definition.decorator_list:
                if isinstance(dec, ast.Call) and dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str) and dec.args[0].value.startswith("--"):
                    options.append(dec.args[0].value)
            contract["commands"][key] = {"file": path, "function": function, "options": options, "output": [ast.literal_eval(k.value) for dec in definition.decorator_list if isinstance(dec, ast.Call) for k in dec.keywords if k.arg == "output_type"]}
        # Preserve precise facts established from the pinned CLI's implementation.
        contract["file_parameter"] = "file:// followed by native literal path; prefix stripped without URI decoding"
        contract["raw_request_no_retry"] = "--no-retry selects oci.retry.NoneRetryStrategy"
        contract["raw_request_status"] = "HTTP status is in JSON status; even non-2xx HTTP can exit 0"
    with zipfile.ZipFile(sdk) as archive:
        names = ["oci/core/models/launch_instance_details.py", "oci/core/models/create_vnic_details.py", "oci/core/models/instance_source_via_image_details.py", "oci/osp_gateway/models/subscription_summary.py", "oci/osp_gateway/models/subscription_collection.py", "oci/limits/models/resource_availability.py"]
        for path in names:
            tree = ast.parse(archive.read(path).decode())
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == "attribute_map" for t in node.targets):
                    contract["models"][Path(path).stem] = ast.literal_eval(node.value)
    save(ROOT / "audit" / "oci-cli-contract.json", contract)
    runtime = {"cli_version": "3.95.1", "certifi_version": "2026.10.19", "required_file_sha256": {}}
    for wheel, wanted in [(cli, {"oci_cli-3.95.1.dist-info/METADATA"}), (args.wheelhouse / "certifi-2026.10.19-py3-none-any.whl", {"certifi-2026.10.19.dist-info/METADATA", "certifi/__init__.py", "certifi/core.py", "certifi/cacert.pem"})]:
        with zipfile.ZipFile(wheel) as archive:
            for name in sorted(wanted):
                runtime["required_file_sha256"][name] = hashlib.sha256(archive.read(name)).hexdigest()
    save(ROOT / "audit" / "runtime-policy.json", runtime)
    print(json.dumps({"source_files": len(filenames), "history_commits": len(commits), "history_file_versions": scans, "credential_pattern_findings": len(findings), "locked_packages": len(packages), "contract_commands": len(contract["commands"])}))


if __name__ == "__main__":
    main()
