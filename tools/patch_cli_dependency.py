#!/usr/bin/env python3
"""Reproducible local OCI CLI wheel metadata patch for the certifi advisory.

Changes one Requires-Dist line and regenerates RECORD. All runtime code bytes
remain identical to the SHA256-pinned official wheel. Installs/runs nothing.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import shutil
import zipfile

OFFICIAL_SHA = "d71ef956f66e667e3f4815c8dfb84619f1d984f7cf34e7842d04f7b92a569508"
CERTIFI_SHA = "42d35340cc5b9ba78e818ddbff0c5b1c2b779de448a340fd1b989f5fa8670e1b"
OLD = b"Requires-Dist: certifi (<2026.0.0,>=2025.1.31)"
NEW = b"Requires-Dist: certifi (==2026.10.19)"


def patch_wheel(source, output, expected_sha=OFFICIAL_SHA):
    source, output = Path(source), Path(output)
    original = source.read_bytes()
    assert hashlib.sha256(original).hexdigest() == expected_sha, "Official CLI wheel hash mismatch"
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        files = {item.filename: archive.read(item.filename) for item in archive.infolist()}
        metadata = next(n for n in files if n.endswith(".dist-info/METADATA"))
        record = next(n for n in files if n.endswith(".dist-info/RECORD"))
        assert files[metadata].count(OLD) == 1, "Unexpected dependency metadata; stop and review"
        files[metadata] = files[metadata].replace(OLD, NEW)
        table = io.StringIO(newline="")
        writer = csv.writer(table, lineterminator="\n")
        for name, data in files.items():
            if name != record:
                encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
                writer.writerow([name, "sha256=" + encoded, len(data)])
        writer.writerow([record, "", ""])
        files[record] = table.getvalue().encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as result:
            for info in archive.infolist():
                result.writestr(info, files[info.filename])
    data = buffer.getvalue()
    if output.exists():
        assert output.read_bytes() == data, "Existing output differs; no overwrite"
    else:
        output.write_bytes(data)
    return {"official_cli_sha256": expected_sha, "patched_cli_sha256": hashlib.sha256(data).hexdigest(), "changed_files": [metadata, record], "change": "Only certifi constraint changed to ==2026.10.19; RECORD regenerated; runtime source unchanged"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-wheelhouse", type=Path, required=True)
    parser.add_argument("--patched-certifi-wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert args.patched_certifi_wheel.name == "certifi-2026.10.19-py3-none-any.whl"
    assert hashlib.sha256(args.patched_certifi_wheel.read_bytes()).hexdigest() == CERTIFI_SHA, "Patched certifi hash mismatch"
    args.output.mkdir(parents=True, exist_ok=True)
    for path in args.original_wheelhouse.glob("*.whl"):
        if path.name.startswith(("oci_cli-", "certifi-")):
            continue
        shutil.copyfile(path, args.output / path.name)
    shutil.copyfile(args.patched_certifi_wheel, args.output / args.patched_certifi_wheel.name)
    original = args.original_wheelhouse / "oci_cli-3.95.1-py3-none-any.whl"
    evidence = patch_wheel(original, args.output / original.name)
    evidence.update(certifi_wheel=args.patched_certifi_wheel.name, certifi_sha256=hashlib.sha256(args.patched_certifi_wheel.read_bytes()).hexdigest())
    (args.output / "local-patch-provenance.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence))


if __name__ == "__main__":
    main()
