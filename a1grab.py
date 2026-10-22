#!/usr/bin/env python3
"""Fail-closed, single-intent OCI A1 launcher. Python standard library only.

Only OCI.launch can write cloud state. Tests inject an offline mock transport.
This module never opens an OCI private key or prints CLI output/credentials.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import contextlib
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid

UTC = dt.timezone.utc
REGION = "ap-tokyo-1"
SHAPE = "VM.Standard.A1.Flex"
CLI_VERSION = "3.95.1"
POLICY_DATE = "2026-10-22"
POLICY_URL = "https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm"
TOKEN_SAFE_SECONDS = 23 * 3600
# Max age of a preflight snapshot before it may be used for a real launch. Multi-region
# / multi-compartment accounts may need many list calls; 60s proved too tight under slow
# networks. 300s still bounds staleness while allowing realistic preflight duration.
PREFLIGHT_MAX_SECONDS = 300
JOURNAL_STATES = {"PREPARED", "IN_FLIGHT", "UNKNOWN", "ACCEPTED", "SUCCEEDED", "CAPACITY", "THROTTLED", "REJECTED", "BLOCKED"}
CONFIG_KEYS = set("TENANCY COMPARTMENT SUBNET IMAGE SSH_PUBKEY_FILE OCI_BIN OCI_CONFIG_FILE OCI_PROFILE OCI_CLI_VERSION OCI_CLI_SHA256 REVIEW_FILE REGION AD MODEL OCPUS MEMGB BOOT_GB BOOT_VPUS_PER_GB NAME DRY_RUN ALLOW_CREATE".split())
READS = {
    "regions": ("iam", "region-subscription", "list"),
    "tenancy": ("iam", "tenancy", "get"),
    "ads": ("iam", "availability-domain", "list"),
    "compartments": ("iam", "compartment", "list"),
    "instances": ("compute", "instance", "list"),
    "instance": ("compute", "instance", "get"),
    "images": ("compute", "image", "list"),
    "subnet": ("network", "subnet", "get"),
    "boot": ("bv", "boot-volume", "list"),
    "block": ("bv", "volume", "list"),
    "boot_backups": ("bv", "boot-volume-backup", "list"),
    "block_backups": ("bv", "backup", "list"),
    "containers": ("container-instances", "container-instance", "list"),
    "pools": ("compute-management", "instance-pool", "list"),
    "reservations": ("compute", "capacity-reservation", "list"),
    "availability": ("limits", "resource-availability", "get"),
    "subscriptions": ("osp-gateway", "subscription-service", "subscription", "list"),
    # HTTP POST query; semantically read-only, with no provisioning side effect.
    "usage": ("usage-api", "usage-summary", "request-summarized-usages"),
}


class Stop(Exception):
    """A stable, non-sensitive error code safe to print."""


def require(condition, code):
    if not condition:
        raise Stop(code)


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def parse_time(value):
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(result.tzinfo is not None, "TIME_REQUIRES_TIMEZONE")
        return result.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise Stop("INVALID_TIME") from None


def iso(timestamp):
    return dt.datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def number(value, code):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0, code)
    return value


def safe_file(path, limit=2_000_000):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), "MISSING_OR_UNSAFE_FILE")
    require(path.stat().st_size <= limit, "FILE_TOO_LARGE")
    if os.name != "nt":
        require(path.stat().st_uid == os.getuid() and not path.stat().st_mode & 0o022, "UNSAFE_FILE_OWNER_OR_PERMISSIONS")
    return path.read_text(encoding="utf-8")


def load_json(path):
    try:
        if os.name != "nt" and Path(path).exists():
            require(not Path(path).stat().st_mode & 0o077, "PRIVATE_JSON_REQUIRES_MODE_600")
        value = json.loads(safe_file(path))
        require(isinstance(value, dict), "INVALID_JSON_OBJECT")
        return value
    except (ValueError, UnicodeError):
        raise Stop("INVALID_JSON_FILE") from None


def private_dir(path):
    path = Path(path)
    for part in (path, *path.parents):
        if part.exists():
            info = part.lstat()
            require(not part.is_symlink() and not getattr(info, "st_file_attributes", 0) & 0x400, "UNSAFE_STATE_PATH")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        require(path.stat().st_uid == os.getuid() and not path.stat().st_mode & 0o077, "STATE_REQUIRES_MODE_700")
    return path


def atomic_json(path, value):
    path = Path(path)
    require(not path.is_symlink(), "UNSAFE_STATE_FILE")
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, ensure_ascii=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


class Lock:
    """OS lock held through inventory, durable intent and the CLI process."""
    def __init__(self, directory):
        self.directory = private_dir(directory)

    def __enter__(self):
        path = self.directory / "operation.lock"
        require(not path.is_symlink(), "UNSAFE_LOCK")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.stream = os.fdopen(fd, "r+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    self.stream.write(b"0")
                self.stream.seek(0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise Stop("ANOTHER_PROCESS_HOLDS_TENANCY_LOCK") from None
        return self

    def __exit__(self, *_):
        self.stream.close()


def state_directory(tenancy):
    # Never place tokens/journal under the repository (which may be cloud synced).
    if os.name == "nt":
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        # CSIDL_LOCAL_APPDATA: OS identity, independent of per-process env vars.
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 0x1C, None, 0, buffer)
        require(result == 0 and buffer.value, "CANONICAL_LOCAL_STATE_PATH_UNAVAILABLE")
        base = Path(buffer.value)
    else:
        import pwd
        base = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local" / "state"
    return base / "a1grab" / hashlib.sha256(tenancy.encode()).hexdigest()


def ocid(value, kind, regional=False):
    pattern = rf"ocid1\.{kind}\.oc1\." + (r"ap-tokyo-1\." if regional else r"\.") + r"[a-z0-9]{8,}"
    require(isinstance(value, str) and re.fullmatch(pattern, value), "INVALID_OR_WRONG_REGION_OCID")
    return value


def config(path):
    values = {}
    for line in safe_file(path, 64_000).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        require(sep and key in CONFIG_KEYS and key not in values, "UNKNOWN_OR_DUPLICATE_CONFIG_KEY")
        require(not any(char in value for char in ("$", "`", ";", "\x00")), "EXECUTABLE_CONFIG_FORBIDDEN")
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError:
            raise Stop("INVALID_CONFIG_VALUE") from None
        require(len(parts) == 1, "CONFIG_REQUIRES_ONE_LITERAL_VALUE")
        values[key] = parts[0]
    require(set(values) == CONFIG_KEYS, "MISSING_CONFIG_KEYS")
    expected = {"REGION": REGION, "AD": "AD-1", "MODEL": SHAPE, "OCPUS": "2", "MEMGB": "12", "BOOT_GB": "50", "BOOT_VPUS_PER_GB": "10"}
    require(all(values[k] == v for k, v in expected.items()), "CONFIG_OUTSIDE_APPROVED_FREE_TIER_PROFILE")
    require(values["DRY_RUN"] in ("0", "1") and values["ALLOW_CREATE"] in ("0", "1"), "INVALID_BOOLEAN_CONFIG")
    ocid(values["TENANCY"], "tenancy")
    if values["COMPARTMENT"] != values["TENANCY"]:
        ocid(values["COMPARTMENT"], "compartment")
    for key, kind in (("SUBNET", "subnet"), ("IMAGE", "image")):
        ocid(values[key], kind, regional=True)
    require(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", values["NAME"]), "INVALID_DISPLAY_NAME")
    require(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", values["OCI_PROFILE"]), "INVALID_PROFILE")
    require(values["OCI_CLI_VERSION"] == CLI_VERSION, "UNREVIEWED_CLI_VERSION")
    require(re.fullmatch(r"[0-9a-f]{64}", values["OCI_CLI_SHA256"]), "REVIEWED_CLI_HASH_REQUIRED")
    for key in ("SSH_PUBKEY_FILE", "OCI_BIN", "OCI_CONFIG_FILE", "REVIEW_FILE"):
        require(Path(values[key]).is_absolute(), "ABSOLUTE_PATH_REQUIRED")
    return values


def public_key(path):
    text = safe_file(path, 20_000).strip()
    require("PRIVATE" not in text and "\n" not in text and "\r" not in text, "ONLY_ONE_SSH_PUBLIC_KEY_ALLOWED")
    parts = text.split()
    require(len(parts) in (2, 3) and parts[0] in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256"), "INVALID_PUBLIC_KEY")
    try:
        raw = base64.b64decode(parts[1], validate=True)
        length = int.from_bytes(raw[:4], "big")
        require(raw[4:4 + length].decode("ascii") == parts[0] and len(raw) > 4 + length + 16, "INVALID_PUBLIC_KEY")
    except (ValueError, UnicodeError):
        raise Stop("INVALID_PUBLIC_KEY") from None
    return " ".join(parts[:2])


class OCI:
    """No shell; only allowlisted reads and exactly one fixed launch endpoint."""
    def __init__(self, cfg, directory, runner=subprocess.run, command=None):
        self.cfg, self.directory, self.runner = cfg, Path(directory), runner
        self.command = command or [cfg["OCI_BIN"]]
        self.writes_enabled = False
        allowed_env = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA", "LANG", "LC_ALL"}
        self.env = {k: v for k, v in os.environ.items() if k.upper() in allowed_env}
        self.env["OCI_CLI_SUPPRESS_FILE_PERMISSIONS_WARNING"] = "False"
        rc = self.directory / "empty-cli-rc"
        try:
            fd = os.open(rc, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            require(not rc.is_symlink() and rc.stat().st_size == 0, "UNSAFE_CLI_RC_FILE")
        self.flags = ["--config-file", cfg["OCI_CONFIG_FILE"], "--profile", cfg["OCI_PROFILE"], "--auth", "api_key", "--region", REGION, "--cli-rc-file", str(rc), "--no-retry", "--connection-timeout", "10", "--read-timeout", "45", "--output", "json"]

    def verify(self):
        binary = Path(self.cfg["OCI_BIN"])
        require(binary.is_file() and not binary.is_symlink(), "REVIEWED_CLI_MISSING")
        require(hashlib.sha256(binary.read_bytes()).hexdigest() == self.cfg["OCI_CLI_SHA256"], "CLI_HASH_CHANGED")
        self.verify_dependency_policy()
        auth = safe_file(self.cfg["OCI_CONFIG_FILE"], 64_000)
        require(not re.search(r"(?im)^\s*(endpoint|proxy|cert_bundle|security_token_file|delegation_token_file)\s*=", auth), "OCI_CONFIG_OVERRIDE_FORBIDDEN")
        try:
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(auth)
            profile = parser[self.cfg["OCI_PROFILE"]]
            require(profile.get("tenancy") == self.cfg["TENANCY"] and profile.get("region") == REGION, "AUTH_TENANCY_OR_REGION_MISMATCH")
        except (configparser.Error, KeyError):
            raise Stop("INVALID_OCI_AUTH_CONFIG") from None
        result = self._process(["--version"])
        require(result.returncode == 0 and result.stdout.strip() == self.cfg["OCI_CLI_VERSION"], "CLI_VERSION_CHANGED")

    def verify_dependency_policy(self, policy_path=None):
        # Check CA data and affected metadata BEFORE executing any installed OCI
        # code. A matching launcher .exe hash alone cannot attest its libraries.
        policy_path = policy_path or Path(__file__).parent / "audit" / "runtime-policy.json"
        policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        binary = Path(self.cfg["OCI_BIN"])
        root = binary.parent.parent
        require((root / "pyvenv.cfg").is_file(), "ISOLATED_REVIEWED_OCI_VENV_REQUIRED")
        candidates = [root / "Lib" / "site-packages"] if os.name == "nt" else list((root / "lib").glob("python3.*/site-packages"))
        candidates = [p for p in candidates if p.is_dir()]
        require(len(candidates) == 1, "OCI_SITE_PACKAGES_NOT_CONFIRMED")
        site = candidates[0]
        for name, expected in policy["required_file_sha256"].items():
            target = site / name
            require(target.is_file() and not target.is_symlink() and target.stat().st_size <= 2_000_000, "PATCHED_OCI_DEPENDENCY_FILE_MISSING")
            require(hashlib.sha256(target.read_bytes()).hexdigest() == expected, "PATCHED_OCI_DEPENDENCY_HASH_MISMATCH")

    def _process(self, args):
        try:
            return self.runner([*self.command, *args], shell=False, capture_output=True, text=True, encoding="utf-8", timeout=75, env=self.env, cwd=str(self.directory))
        except subprocess.TimeoutExpired:
            raise Stop("OCI_TIMEOUT_RESULT_UNKNOWN") from None
        except OSError:
            raise Stop("OCI_PROCESS_FAILED_RESULT_UNKNOWN") from None

    def read(self, name, *args):
        require(name in READS, "API_NOT_ALLOWLISTED")
        result = self._process([*self.flags, *READS[name], *args])
        require(result.returncode == 0, "OCI_READ_FAILED_" + name.upper())
        try:
            output = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise Stop("OCI_READ_INVALID_JSON") from None
        require(isinstance(output, dict) and "data" in output, "OCI_READ_MISSING_DATA")
        require(not output.get("opc-next-page"), "UNCONSUMED_PAGINATION")
        return output["data"]

    def usage(self, tenant, start, end):
        result, seen, page = [], set(), None
        while True:
            args = [*self.flags, *READS["usage"], "--tenant-id", tenant, "--time-usage-started", iso(start), "--time-usage-ended", iso(end), "--granularity", "MONTHLY", "--query-type", "USAGE", "--group-by", '["skuPartNumber","unit"]']
            if page:
                args += ["--page", page]
            response = self._process(args)
            require(response.returncode == 0, "OCI_USAGE_READ_FAILED")
            try:
                value = json.loads(response.stdout)
                rows = value["data"]["items"]
                require(isinstance(rows, list), "INVALID_USAGE_ITEMS")
            except (ValueError, TypeError, KeyError):
                raise Stop("INVALID_USAGE_RESPONSE") from None
            result.extend(rows)
            page = value.get("opc-next-page")
            if not page:
                return result
            require(isinstance(page, str) and page not in seen and len(seen) < 1000, "INVALID_USAGE_PAGINATION")
            seen.add(page)

    def launch(self, body, retry_token):
        require(self.writes_enabled, "CLOUD_WRITES_DISABLED")
        headers_path = self.directory / "request-headers.json"
        body_path = self.directory / "request-body.json"
        atomic_json(headers_path, {"opc-retry-token": retry_token, "Content-Type": "application/json"})
        atomic_json(body_path, body)
        try:
            # OCI CLI strips 'file://' literally; this is NOT an RFC file URI.
            # Path.as_uri() would break Windows drive letters/spaces/non-ASCII.
            result = self._process([*self.flags, "raw-request", "--http-method", "POST", "--target-uri", f"https://iaas.{REGION}.oraclecloud.com/20160918/instances", "--request-headers", "file://" + str(headers_path), "--request-body", "file://" + str(body_path)])
            text = result.stdout if result.stdout.strip() else result.stderr
            try:
                response = json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
                require(isinstance(response, dict), "INVALID_LAUNCH_RESPONSE")
            except (ValueError, TypeError):
                raise Stop("OCI_LAUNCH_RESULT_UNKNOWN") from None
            status = str(response.get("status", "")).split(" ")[0]
            data = response.get("data", response)
            if result.returncode == 0 and status in ("200", "201", "202") and isinstance(data, dict) and data.get("id"):
                ocid(data["id"], "instance", regional=True)
                return "ACCEPTED", data["id"], 0
            code = data.get("code") if isinstance(data, dict) else None
            message = data.get("message") if isinstance(data, dict) else None
            if status == "500" and code == "InternalError" and message == "Out of host capacity.":
                return "CAPACITY", None, 600
            if status == "429" and (code == "TooManyRequests" or (isinstance(message, str) and "too many requests" in message.lower())):
                headers = response.get("headers", {})
                delay = str(headers.get("retry-after", headers.get("Retry-After", "600"))) if isinstance(headers, dict) else "600"
                require(delay.isdigit() and int(delay) <= 86400, "INVALID_RETRY_AFTER")
                return "THROTTLED", None, max(600, int(delay))
            if code in ("LimitExceeded", "QuotaExceeded", "NotAuthenticated", "NotAuthorizedOrNotFound", "InvalidParameter") and status in ("400", "401", "403", "404"):
                return "REJECTED", None, 0
            raise Stop("OCI_LAUNCH_RESULT_UNKNOWN")
        finally:
            for path in (headers_path, body_path):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()


def array(value, code):
    require(isinstance(value, list) and all(isinstance(row, dict) for row in value), code)
    return value


def live(row, allowed):
    state = row.get("lifecycle-state")
    require(state in allowed | {"TERMINATED"}, "UNKNOWN_RESOURCE_LIFECYCLE")
    return state != "TERMINATED"


def inventory(api, cfg):
    """All compartments incl. root, all list pages, no RUNNING-only filter."""
    root = cfg["TENANCY"]
    regions = array(api.read("regions", "--tenancy-id", root, "--all"), "INVALID_REGIONS")
    require(regions, "INVALID_REGIONS")
    # An account may subscribe to several regions; only the Home Region matters for
    # the free-tier A1 target. Require Tokyo to be present, READY and the single Home
    # Region; do NOT reject merely because other regions are also subscribed. Any
    # subscription reporting an unexpected lifecycle state is still treated as unknown.
    for row in regions:
        require(row.get("status") in ("READY", "INACTIVE"), "REGION_SUBSCRIPTION_STATE_UNKNOWN")
    tokyo = [row for row in regions if row.get("region-name") == REGION]
    home = [row for row in regions if row.get("is-home-region") is True]
    require(len(tokyo) == 1 and tokyo[0].get("status") == "READY" and len(home) == 1 and home[0].get("region-name") == REGION, "HOME_REGION_OR_REGION_SCOPE_NOT_CONFIRMED")
    tenancy = api.read("tenancy", "--tenancy-id", root)
    require(isinstance(tenancy, dict) and tenancy.get("id") == root and tenancy.get("home-region-key") == "NRT", "TENANCY_HOME_REGION_NOT_CONFIRMED")
    ads = array(api.read("ads", "--compartment-id", root, "--all"), "INVALID_ADS")
    require(len(ads) == 1 and re.fullmatch(r"[a-zA-Z0-9_-]+:AP-TOKYO-1-AD-1", ads[0].get("name", ""), re.I), "SINGLE_TOKYO_AD1_NOT_CONFIRMED")
    ad = ads[0]["name"]
    listed = array(api.read("compartments", "--compartment-id", root, "--compartment-id-in-subtree", "true", "--access-level", "ANY", "--all"), "INVALID_COMPARTMENTS")
    compartments = {root}
    for row in listed:
        require(row.get("lifecycle-state") in ("ACTIVE", "DELETED"), "COMPARTMENT_TRANSITION_UNCERTAIN")
        if row["lifecycle-state"] == "ACTIVE":
            compartments.add(ocid(row.get("id"), "compartment"))
    require(cfg["COMPARTMENT"] in compartments, "TARGET_COMPARTMENT_NOT_ENUMERATED")
    instances, volumes, backups = [], [], []
    ids = set()
    for compartment in sorted(compartments):
        common = ("--compartment-id", compartment, "--all")
        for row in array(api.read("instances", *common), "INVALID_INSTANCES"):
            require(isinstance(row.get("shape"), str), "MISSING_INSTANCE_SHAPE")
            if live(row, {"MOVING", "PROVISIONING", "RUNNING", "STARTING", "STOPPING", "STOPPED", "TERMINATING", "CREATING_IMAGE"}):
                identifier = ocid(row.get("id"), "instance", regional=True)
                require(identifier not in ids, "DUPLICATED_INVENTORY_ID")
                ids.add(identifier)
                if "A1" in row["shape"]:
                    shape_cfg = row.get("shape-config")
                    require(isinstance(shape_cfg, dict), "MISSING_A1_SHAPE_USAGE")
                    number(shape_cfg.get("ocpus"), "UNKNOWN_A1_CPU_USAGE")
                    number(shape_cfg.get("memory-in-gbs"), "UNKNOWN_A1_MEMORY_USAGE")
                    instances.append(row)
                elif row["lifecycle-state"] in ("PROVISIONING", "MOVING", "TERMINATING", "STARTING"):
                    raise Stop("OTHER_INSTANCE_PENDING_STORAGE_UNCERTAIN")
        for name in ("boot", "block"):
            args = (*common, "--availability-domain", ad) if name == "boot" else common
            for row in array(api.read(name, *args), "INVALID_VOLUME_LIST"):
                if live(row, {"PROVISIONING", "RESTORING", "AVAILABLE", "TERMINATING", "FAULTY"}):
                    require(isinstance(row.get("id"), str) and row["id"] not in ids, "DUPLICATED_OR_MISSING_VOLUME_ID")
                    ids.add(row["id"])
                    number(row.get("size-in-gbs"), "UNKNOWN_VOLUME_SIZE")
                    require(number(row.get("vpus-per-gb"), "UNKNOWN_VOLUME_PERFORMANCE") <= 10, "PAID_VOLUME_PERFORMANCE_PRESENT")
                    require(not row.get("block-volume-replicas") and not row.get("boot-volume-replicas") and not row.get("autotune-policies") and not row.get("is-auto-tune-enabled"), "VOLUME_REPLICATION_OR_AUTOTUNE_UNCERTAIN")
                    volumes.append(row)
        for name in ("boot_backups", "block_backups"):
            for row in array(api.read(name, *common), "INVALID_BACKUP_LIST"):
                if live(row, {"CREATING", "AVAILABLE", "TERMINATING", "FAULTY", "REQUEST_RECEIVED"}):
                    backups.append(row)
        for name in ("containers", "pools", "reservations"):
            extra = ("--availability-domain", ad) if name == "reservations" else ()
            rows = api.read(name, *common, *extra)
            if isinstance(rows, dict):
                rows = rows.get("items")
            for row in array(rows, "INVALID_OTHER_COMPUTE_LIST"):
                if row.get("lifecycle-state") not in ("DELETED", "TERMINATED"):
                    raise Stop("OTHER_COMPUTE_OR_CONTROLLER_PRESENT")
    return {"ad": ad, "compartments": sorted(compartments), "instances": instances, "volumes_gb": sum(v["size-in-gbs"] for v in volumes), "backups": len(backups), "a1_ocpus": sum(v["shape-config"]["ocpus"] for v in instances), "a1_memory_gb": sum(v["shape-config"]["memory-in-gbs"] for v in instances)}


def request_body(cfg, snapshot):
    return {"availabilityDomain": snapshot["ad"], "compartmentId": cfg["COMPARTMENT"], "shape": SHAPE, "shapeConfig": {"ocpus": 2, "memoryInGBs": 12}, "sourceDetails": {"sourceType": "image", "imageId": cfg["IMAGE"], "bootVolumeSizeInGBs": 50, "bootVolumeVpusPerGB": 10}, "createVnicDetails": {"subnetId": cfg["SUBNET"], "assignPublicIp": False, "assignIpv6Ip": False}, "capacityReservationId": "", "isAIEnterpriseEnabled": False, "displayName": cfg["NAME"], "metadata": {"ssh_authorized_keys": public_key(cfg["SSH_PUBKEY_FILE"])}, "freeformTags": {"a1grab-operation": digest({"tenancy": cfg["TENANCY"], "profile": "single-a1-tokyo-2-12-v1"})}}


def validate_resources(api, cfg, snapshot):
    require(not snapshot["instances"], "A1_ALREADY_EXISTS_INCLUDING_PENDING_OR_STOPPED")
    require(snapshot["a1_ocpus"] + 2 <= 2 and snapshot["a1_memory_gb"] + 12 <= 12, "A1_FREE_ALLOCATION_EXCEEDED")
    require(snapshot["volumes_gb"] + 50 <= 200 and snapshot["backups"] <= 5, "STORAGE_FREE_ALLOCATION_EXCEEDED")
    subnet = api.read("subnet", "--subnet-id", cfg["SUBNET"])
    require(isinstance(subnet, dict) and subnet.get("id") == cfg["SUBNET"] and subnet.get("lifecycle-state") == "AVAILABLE" and subnet.get("compartment-id") in snapshot["compartments"] and subnet.get("availability-domain") in (None, snapshot["ad"]), "SUBNET_NOT_CONFIRMED")
    images = array(api.read("images", "--compartment-id", cfg["COMPARTMENT"], "--shape", SHAPE, "--all"), "INVALID_IMAGES")
    selected = [row for row in images if row.get("id") == cfg["IMAGE"]]
    require(len(selected) == 1, "IMAGE_NOT_COMPATIBLE_OR_NOT_FOUND")
    image = selected[0]
    require("compartment-id" in image and image["compartment-id"] is None and image.get("operating-system") in ("Oracle Linux", "Canonical Ubuntu") and image.get("lifecycle-state") == "AVAILABLE", "IMAGE_NOT_FREE_OFFICIAL_PLATFORM_IMAGE")
    require(number(image.get("size-in-mbs"), "UNKNOWN_IMAGE_SIZE") <= 50 * 1024, "IMAGE_EXCEEDS_BOOT_VOLUME")
    for limit, needed in (("standard-a1-core-count", 2), ("standard-a1-memory-count", 12)):
        for compartment in {cfg["TENANCY"], cfg["COMPARTMENT"]}:
            value = api.read("availability", "--compartment-id", compartment, "--service-name", "compute", "--limit-name", limit, "--availability-domain", snapshot["ad"])
            require(isinstance(value, dict), "INVALID_RESOURCE_AVAILABILITY")
            available = number(value.get("fractional-availability") if value.get("fractional-availability") is not None else value.get("available"), "UNKNOWN_AVAILABLE_QUOTA")
            used = number(value.get("fractional-usage") if value.get("fractional-usage") is not None else value.get("used"), "UNKNOWN_USED_QUOTA")
            require(number(value.get("used"), "UNKNOWN_USED_QUOTA") == math.ceil(used) and number(value.get("available"), "UNKNOWN_AVAILABLE_QUOTA") == math.floor(available), "QUOTA_COUNTERS_DISAGREE")
            require(used == 0 and available >= needed, "QUOTA_USAGE_INCONSISTENT_OR_INSUFFICIENT")


def billing(api, cfg, now):
    collection = api.read("subscriptions", "--compartment-id", cfg["TENANCY"], "--osp-home-region", REGION, "--all")
    require(isinstance(collection, dict), "INVALID_SUBSCRIPTION_COLLECTION")
    rows = array(collection.get("items"), "INVALID_SUBSCRIPTIONS")
    require(len(rows) == 1 and isinstance(rows[0].get("id"), str) and rows[0].get("plan-type") == "FREE_TIER" and rows[0].get("time-plan-upgrade") is None and rows[0].get("upgrade-state") is None, "FREE_TIER_ACCOUNT_NOT_CONFIRMED")
    current = dt.datetime.fromtimestamp(now, UTC)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    rows = array(api.usage(cfg["TENANCY"], start.timestamp(), end.timestamp()), "INVALID_USAGE_LIST")
    cpu, memory = 0.0, 0.0
    for row in rows:
        sku = row.get("sku-part-number")
        require(isinstance(sku, str), "USAGE_WITHOUT_SKU")
        if sku in ("B93297", "B93298"):
            require(row.get("is-correction") in (None, False), "BILLING_CORRECTION_REQUIRES_REVIEW")
            quantity = number(row.get("computed-quantity"), "UNKNOWN_MONTHLY_A1_USAGE")
            require(isinstance(row.get("unit"), str) and "hour" in row["unit"].lower(), "UNKNOWN_A1_USAGE_UNIT")
            if sku == "B93297":
                cpu += quantity
            else:
                memory += quantity
    return {"month": start.strftime("%Y-%m"), "ocpu_hours_reported": cpu, "memory_gb_hours_reported": memory, "remaining_hours": (end.timestamp() - now) / 3600, "report_rows": len(rows)}


def check_review(cfg, body, bill, now):
    review = load_json(cfg["REVIEW_FILE"])
    require(review.get("schema") == 1 and review.get("approved") is True, "EXPLICIT_HUMAN_APPROVAL_REQUIRED")
    require(review.get("tenancy_sha256") == hashlib.sha256(cfg["TENANCY"].encode()).hexdigest() and review.get("request_sha256") == digest(body), "APPROVAL_NOT_BOUND_TO_THIS_REQUEST")
    reviewed, expires = parse_time(review.get("reviewed_at")), parse_time(review.get("expires_at"))
    require(0 <= now - reviewed <= 3600 and now < expires <= reviewed + 3600, "HUMAN_REVIEW_STALE_OR_CLOCK_INVALID")
    require(review.get("policy_date") == POLICY_DATE and review.get("policy_url") == POLICY_URL and review.get("account_plan") == "FREE_TIER" and review.get("billing_month") == bill["month"], "PRICING_OR_ACCOUNT_REVIEW_REQUIRED")
    for name in ("all_compartments_visible", "exclusive_writer", "no_unresolved_requests", "official_image_free", "balanced_boot_free", "unreported_usage_accounted_for", "local_state_preserved"):
        require(review.get(name) is True, "UNCONFIRMED_REVIEW_" + name.upper())
    cpu = number(review.get("a1_ocpu_hours_upper_bound"), "UNKNOWN_BILLING_CPU_UPPER_BOUND")
    mem = number(review.get("a1_memory_gb_hours_upper_bound"), "UNKNOWN_BILLING_MEMORY_UPPER_BOUND")
    require(cpu >= bill["ocpu_hours_reported"] and mem >= bill["memory_gb_hours_reported"], "BILLING_EXCEEDS_REVIEWED_UPPER_BOUND")
    remaining = math.ceil(bill["remaining_hours"]) + 1
    require(cpu + 2 * remaining <= 1500 and mem + 12 * remaining <= 9000, "MONTHLY_ALWAYS_FREE_BUDGET_NOT_PROVEN")


class Engine:
    def __init__(self, cfg, api, directory, clock=time.time):
        self.cfg, self.api, self.directory, self.clock = cfg, api, Path(directory), clock
        self.journal = self.directory / "journal.json"

    def save(self, state):
        atomic_json(self.journal, state)

    def run(self, create=False, resume_unknown=False):
        now = self.clock()
        with Lock(self.directory):
            state = load_json(self.journal) if self.journal.exists() else None
            snapshot = inventory(self.api, self.cfg)
            if state:
                require(state.get("schema") == 1 and state.get("state") in JOURNAL_STATES, "INVALID_JOURNAL")
                require(state.get("tenancy_sha256") == hashlib.sha256(self.cfg["TENANCY"].encode()).hexdigest(), "JOURNAL_TENANCY_MISMATCH")
                expected_token = uuid.uuid5(uuid.NAMESPACE_URL, "a1grab-single-a1-tokyo-2-12-v1:" + self.cfg["TENANCY"]).hex
                require(state.get("retry_token") == expected_token, "JOURNAL_RETRY_TOKEN_CHANGED")
                number(state.get("created_at"), "INVALID_JOURNAL_TIME")
                require(type(state.get("attempts")) is int and state["attempts"] >= 0, "INVALID_JOURNAL_ATTEMPTS")
                number(state.get("next_attempt_at", 0), "INVALID_JOURNAL_BACKOFF")
                matching = [r for r in snapshot["instances"] if isinstance(r.get("freeform-tags"), dict) and r["freeform-tags"].get("a1grab-operation") == state.get("operation")]
                require(len(matching) <= 1, "MULTIPLE_MATCHING_INSTANCES_STOP_AND_REVIEW")
                if matching:
                    state.update(state="SUCCEEDED", instance_id=matching[0]["id"])
                    self.save(state)
                    return {"state": "SUCCEEDED", "reconciled": True, "cloud_write": False}
                if state.get("instance_id"):
                    instance = self.api.read("instance", "--instance-id", state["instance_id"])
                    require(isinstance(instance, dict) and instance.get("id") == state["instance_id"], "ACCEPTED_INSTANCE_NOT_CONFIRMED")
                    return {"state": "ACCEPTED", "cloud_write": False}
                if state["state"] in ("SUCCEEDED", "REJECTED", "BLOCKED"):
                    raise Stop("PERSISTENT_TERMINAL_STATE_NO_NEW_INTENT_ALLOWED")
            validate_resources(self.api, self.cfg, snapshot)
            body = request_body(self.cfg, snapshot)
            body_hash = digest(body)
            if state:
                require(state.get("request_sha256") == body_hash and state.get("body") == body, "REQUEST_CHANGED_WITH_EXISTING_INTENT")
            bill = billing(self.api, self.cfg, now)
            if not create:
                return {"state": "DRY_RUN", "cloud_write": False, "request_sha256": body_hash, "tenancy_sha256": hashlib.sha256(self.cfg["TENANCY"].encode()).hexdigest(), "existing_a1": len(snapshot["instances"]), "existing_storage_gb": snapshot["volumes_gb"], "existing_backups": snapshot["backups"], "billing_month": bill["month"], "reported_a1_ocpu_hours": bill["ocpu_hours_reported"], "reported_a1_memory_gb_hours": bill["memory_gb_hours_reported"], "manual_billing_upper_bound_and_approval_required": True}
            require(self.cfg["DRY_RUN"] == "0" and self.cfg["ALLOW_CREATE"] == "1" and os.environ.get("DRY_RUN", "0") == "0", "CREATION_NOT_EXPLICITLY_ENABLED")
            check_review(self.cfg, body, bill, now)
            require(0 <= self.clock() - now <= PREFLIGHT_MAX_SECONDS, "PREFLIGHT_TOO_OLD_REPEAT_READS")
            if state:
                age = self.clock() - state["created_at"]
                require(0 <= age < TOKEN_SAFE_SECONDS, "RETRY_TOKEN_EXPIRED_OR_CLOCK_REVERSED_STOP")
                require(self.clock() >= state.get("next_attempt_at", 0), "PERSISTED_BACKOFF_ACTIVE")
                require(state["state"] not in ("IN_FLIGHT", "UNKNOWN") or resume_unknown, "UNKNOWN_RESULT_RECONCILED_BUT_EXPLICIT_RESUME_REQUIRED")
            else:
                token = uuid.uuid5(uuid.NAMESPACE_URL, "a1grab-single-a1-tokyo-2-12-v1:" + self.cfg["TENANCY"]).hex
                state = {"schema": 1, "state": "PREPARED", "created_at": self.clock(), "tenancy_sha256": hashlib.sha256(self.cfg["TENANCY"].encode()).hexdigest(), "request_sha256": body_hash, "operation": body["freeformTags"]["a1grab-operation"], "retry_token": token, "body": body, "attempts": 0}
                self.save(state)
            state.update(state="IN_FLIGHT", attempts=state["attempts"] + 1, last_attempt_at=self.clock())
            self.save(state)
            self.api.writes_enabled = True
            try:
                outcome, identifier, delay = self.api.launch(state["body"], state["retry_token"])
            except (Stop, OSError):
                state["state"] = "UNKNOWN"
                self.save(state)
                raise Stop("LAUNCH_UNKNOWN_STOP_RECONCILE_WITH_SAME_INTENT") from None
            finally:
                self.api.writes_enabled = False
            state.update(state=outcome, next_attempt_at=self.clock() + delay)
            if identifier:
                state["instance_id"] = identifier
            self.save(state)
            return {"state": outcome, "cloud_write": True, "retry_after_seconds": delay}


def local_setup(target):
    target = Path(target)
    require(not target.exists(), "CONFIG_ALREADY_EXISTS_NOT_OVERWRITTEN")
    source = Path(__file__).with_name("config.example.sh").read_text(encoding="utf-8")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(source)
    return {"state": "LOCAL_TEMPLATE_CREATED", "cloud_write": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "run", "status"))
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.sh")))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--create", action="store_true")
    parser.add_argument("--resume-unknown", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "setup":
            result = local_setup(args.config)
        else:
            require(os.environ.get("DRY_RUN", "0") in ("0", "1"), "INVALID_DRY_RUN_ENV")
            require(not args.resume_unknown or args.create, "RESUME_REQUIRES_EXPLICIT_CREATE")
            cfg = config(args.config)
            directory = state_directory(cfg["TENANCY"])
            if args.action == "status":
                path = directory / "journal.json"
                state = load_json(path) if path.exists() else {}
                require(not state or state.get("state") in JOURNAL_STATES, "INVALID_JOURNAL")
                require(type(state.get("attempts", 0)) is int and state.get("attempts", 0) >= 0, "INVALID_JOURNAL_ATTEMPTS")
                result = {"state": state.get("state", "NO_INTENT"), "attempts": state.get("attempts", 0), "cloud_write": False}
            else:
                private_dir(directory)
                api = OCI(cfg, directory)
                api.verify()
                result = Engine(cfg, api, directory).run(args.create, args.resume_unknown)
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return 0
    except Stop as error:
        print(json.dumps({"state": "STOPPED", "reason": str(error)}), file=sys.stderr)
        return 2
    except Exception:
        print('{"state":"STOPPED","reason":"LOCAL_OR_SCHEMA_FAILURE_REVIEW_REQUIRED"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
