"""Synthetic data only. No real OCI accounts, keys, tokens or network access."""
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
TENANT = "ocid1.tenancy.oc1.." + "a" * 32
CHILD = "ocid1.compartment.oc1.." + "b" * 32
SUBNET = "ocid1.subnet.oc1.ap-tokyo-1." + "c" * 32
IMAGE = "ocid1.image.oc1.ap-tokyo-1." + "d" * 32
INSTANCE = "ocid1.instance.oc1.ap-tokyo-1." + "e" * 32
AD = "TEST:AP-TOKYO-1-AD-1"
NOW = dt.datetime(2026, 10, 22, 10, tzinfo=dt.timezone.utc).timestamp()


def write(path, value):
    path = Path(path)
    path.write_text(json.dumps(value), encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)


def cfg(directory):
    directory = Path(directory)
    # Structurally valid public key, generated from fixed TEST bytes. No private key.
    key_type = b"ssh-ed25519"
    raw = len(key_type).to_bytes(4, "big") + key_type + (32).to_bytes(4, "big") + b"x" * 32
    key = directory / "test.pub"
    key.write_text("ssh-ed25519 " + base64.b64encode(raw).decode() + " synthetic-test", encoding="utf-8")
    auth = directory / "mock-auth-config"
    auth.write_text(f"[DEFAULT]\ntenancy={TENANT}\nregion=ap-tokyo-1\n", encoding="utf-8")
    return {"TENANCY": TENANT, "COMPARTMENT": TENANT, "SUBNET": SUBNET, "IMAGE": IMAGE, "SSH_PUBKEY_FILE": str(key), "OCI_BIN": sys.executable, "OCI_CONFIG_FILE": str(auth), "OCI_PROFILE": "DEFAULT", "OCI_CLI_VERSION": "3.95.1", "OCI_CLI_SHA256": hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(), "REVIEW_FILE": str(directory / "test-review.json"), "REGION": "ap-tokyo-1", "AD": "AD-1", "MODEL": "VM.Standard.A1.Flex", "OCPUS": "2", "MEMGB": "12", "BOOT_GB": "50", "BOOT_VPUS_PER_GB": "10", "NAME": "a1-free-tokyo", "DRY_RUN": "0", "ALLOW_CREATE": "1"}


def instance(state="RUNNING", operation=None, identifier=INSTANCE):
    return {"id": identifier, "shape": "VM.Standard.A1.Flex", "shape-config": {"ocpus": 2, "memory-in-gbs": 12}, "lifecycle-state": state, "compartment-id": TENANT, "availability-domain": AD, "image-id": IMAGE, "freeform-tags": {"a1grab-operation": operation} if operation else {}}


def defaults():
    return {"now": NOW, "regions": [{"region-name": "ap-tokyo-1", "is-home-region": True, "status": "READY"}], "tenancy": {"id": TENANT, "home-region-key": "NRT"}, "ads": [{"name": AD}], "compartments": [], "instances": [], "boot": [], "block": [], "boot_backups": [], "block_backups": [], "containers": {"items": []}, "pools": [], "reservations": [], "subnet": {"id": SUBNET, "compartment-id": TENANT, "lifecycle-state": "AVAILABLE", "availability-domain": None}, "images": [{"id": IMAGE, "compartment-id": None, "operating-system": "Oracle Linux", "lifecycle-state": "AVAILABLE", "size-in-mbs": 47694}], "availability": {"used": 0, "available": 20, "fractional-usage": 0.0, "fractional-availability": 20.0}, "subscriptions": {"items": [{"id": "synthetic-subscription", "plan-type": "FREE_TIER", "upgrade-state": None, "time-plan-upgrade": None}]}, "usage": [], "launch_sequence": ["success"]}


def approve(module, config, now=NOW, **changes):
    body = module.request_body(config, {"ad": AD})
    review = {"schema": 1, "approved": True, "tenancy_sha256": hashlib.sha256(TENANT.encode()).hexdigest(), "request_sha256": module.digest(body), "reviewed_at": module.iso(now), "expires_at": module.iso(now + 3500), "policy_date": module.POLICY_DATE, "policy_url": module.POLICY_URL, "account_plan": "FREE_TIER", "billing_month": dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("%Y-%m"), "all_compartments_visible": True, "exclusive_writer": True, "no_unresolved_requests": True, "official_image_free": True, "balanced_boot_free": True, "unreported_usage_accounted_for": True, "local_state_preserved": True, "a1_ocpu_hours_upper_bound": 0, "a1_memory_gb_hours_upper_bound": 0}
    review.update(changes)
    write(config["REVIEW_FILE"], review)
    return review
