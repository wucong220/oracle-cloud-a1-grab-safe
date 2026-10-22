#!/usr/bin/env python3
"""Executable Mock OCI CLI. Refuses commands outside this synthetic protocol."""
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urlparse

COMMANDS = {
    ("iam", "region-subscription", "list"): "regions",
    ("iam", "tenancy", "get"): "tenancy",
    ("iam", "availability-domain", "list"): "ads",
    ("iam", "compartment", "list"): "compartments",
    ("compute", "instance", "list"): "instances",
    ("compute", "instance", "get"): "instance",
    ("compute", "image", "list"): "images",
    ("network", "subnet", "get"): "subnet",
    ("bv", "boot-volume", "list"): "boot",
    ("bv", "volume", "list"): "block",
    ("bv", "boot-volume-backup", "list"): "boot_backups",
    ("bv", "backup", "list"): "block_backups",
    ("container-instances", "container-instance", "list"): "containers",
    ("compute-management", "instance-pool", "list"): "pools",
    ("compute", "capacity-reservation", "list"): "reservations",
    ("limits", "resource-availability", "get"): "availability",
    ("osp-gateway", "subscription-service", "subscription", "list"): "subscriptions",
    ("usage-api", "usage-summary", "request-summarized-usages"): "usage",
}


def uri_file(uri):
    native = Path(uri[len("file://"):])
    if native.is_file():
        return native
    value = unquote(urlparse(uri).path)
    if sys.platform == "win32" and value.startswith("/"):
        value = value[1:]
    return Path(value)


def main():
    args = sys.argv[1:]
    if args == ["--version"]:
        print("3.95.1")
        return 0
    fixture = json.loads(Path("fixture.json").read_text(encoding="utf-8"))
    remote_path = Path("mock-remote.json")
    remote = json.loads(remote_path.read_text()) if remote_path.exists() else {"launch_calls": 0, "resources": {}, "history": []}
    def arg(name):
        return args[args.index(name) + 1]
    for name in ("--config-file", "--profile", "--auth", "--region", "--cli-rc-file", "--connection-timeout", "--read-timeout", "--output"):
        assert name in args, name
    assert arg("--region") == "ap-tokyo-1" and "--no-retry" in args
    if "raw-request" in args:
        assert arg("--http-method") == "POST"
        assert arg("--target-uri") == "https://iaas.ap-tokyo-1.oraclecloud.com/20160918/instances"
        headers = json.loads(uri_file(arg("--request-headers")).read_text())
        body = json.loads(uri_file(arg("--request-body")).read_text())
        token = headers["opc-retry-token"]
        intent_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        remote["launch_calls"] += 1
        remote["history"].append({"token": token, "request_hash": intent_hash})
        sequence = fixture.get("launch_sequence", ["success"])
        outcome = sequence[min(remote["launch_calls"] - 1, len(sequence) - 1)]
        accepted = outcome in ("success", "timeout_after_accept", "duplicate_after_accept")
        if accepted and token not in remote["resources"]:
            remote["resources"][token] = {"id": "ocid1.instance.oc1.ap-tokyo-1." + "e" * 32, "shape": body["shape"], "shape-config": {"ocpus": 2, "memory-in-gbs": 12}, "lifecycle-state": "PROVISIONING", "compartment-id": body["compartmentId"], "availability-domain": body["availabilityDomain"], "image-id": body["sourceDetails"]["imageId"], "freeform-tags": body["freeformTags"], "request_hash": intent_hash}
        elif token in remote["resources"]:
            assert remote["resources"][token]["request_hash"] == intent_hash, "changed retry body"
        remote_path.write_text(json.dumps(remote))
        if outcome in ("timeout", "timeout_after_accept"):
            print("ReadTimeout: secret-sentinel-must-never-be-logged", file=sys.stderr)
            return 1
        if outcome == "invalid_json":
            print("not JSON")
            return 0
        if outcome == "success":
            print(json.dumps({"status": "200 OK", "headers": {}, "data": {"id": remote["resources"][token]["id"]}}))
            return 0
        mapping = {"capacity": (500, "InternalError", "Out of host capacity."), "throttled": (429, "TooManyRequests", "Too many requests for the user"), "throttled_words": (429, "Other", "Too many requests for the user"), "duplicate": (409, "Conflict", "Duplicate request"), "duplicate_after_accept": (409, "Conflict", "Duplicate request"), "invalidated": (409, "InvalidatedRetryToken", "Invalidated"), "server_error": (500, "InternalError", "Unknown server error"), "limit": (400, "LimitExceeded", "Limit exceeded")}
        status, code, message = mapping[outcome]
        print(json.dumps({"status": status, "code": code, "message": message, "headers": fixture.get("response_headers", {})}), file=sys.stderr)
        return 1
    action = None
    for command, name in COMMANDS.items():
        if any(tuple(args[i:i + len(command)]) == command for i in range(len(args))):
            action = name
            break
    assert action is not None, "not a mock command"
    with Path("mock-calls.jsonl").open("a") as stream:
        stream.write(json.dumps({"action": action, "compartment": arg("--compartment-id") if "--compartment-id" in args else None}) + "\n")
    if fixture.get("read_error") == action:
        print("secret-sentinel-must-never-be-logged", file=sys.stderr)
        return 1
    data = fixture.get(action)
    # Some OCI CLI list commands emit completely empty stdout for an empty result
    # (e.g. `iam compartment list` for a tenancy with only the root compartment).
    if fixture.get("empty_stdout") == action:
        return 0
    if action == "instances":
        data = list(data)
        if not fixture.get("hide_accepted"):
            data += list(remote["resources"].values())
        if arg("--compartment-id") != "ocid1.tenancy.oc1.." + "a" * 32:
            data = fixture.get("child_instances", [])
    if action == "instance":
        candidates = [r for r in remote["resources"].values() if r["id"] == arg("--instance-id")]
        if not candidates:
            return 1
        data = candidates[0]
    if action == "usage":
        pages = fixture.get("usage_pages", [fixture["usage"]])
        page = int(arg("--page")) if "--page" in args else 0
        value = {"data": {"items": pages[page]}}
        if page + 1 < len(pages):
            value["opc-next-page"] = str(page + 1)
        print(json.dumps(value))
    else:
        print(json.dumps({"data": data}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
