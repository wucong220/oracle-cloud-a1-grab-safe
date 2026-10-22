#!/usr/bin/env python3
"""Query OSV for PUBLIC package names/versions. No OCI access or credentials.

Optional online audit maintenance tool; NOT part of the offline test suite.
"""
import datetime as dt
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    packages = json.loads((ROOT / "audit" / "dependency-manifest.json").read_text())["packages"]
    request = {"queries": [{"package": {"name": p["name"], "ecosystem": "PyPI"}, "version": p["version"]} for p in packages]}
    result = {"checked_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "provider": "https://api.osv.dev/v1/querybatch", "data_sent": "public PyPI package names and versions only", "status": "UNAVAILABLE", "packages": []}
    try:
        query = urllib.request.Request(result["provider"], data=json.dumps(request).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(query, timeout=45) as response:
            results = json.load(response)["results"]
        assert len(results) == len(packages)
        assert all(not row.get("next_page_token") for row in results), "OSV pagination requires a follow-up review"
        result["status"] = "COMPLETED"
        result["packages"] = [{"name": package["name"], "version": package["version"], "advisories": [v["id"] for v in row.get("vulns", [])]} for package, row in zip(packages, results)]
    except Exception:
        result["status"] = "UNAVAILABLE_NOT_A_PASS"
    (ROOT / "audit" / "dependency-advisories.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    count = sum(len(row["advisories"]) for row in result["packages"])
    print(json.dumps({"status": result["status"], "package_count": len(packages), "advisory_count": count}))
    return 0 if result["status"] == "COMPLETED" and count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
