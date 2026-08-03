#!/usr/bin/env python
"""Create and verify the immutable storm-object prediction package."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.storm_object_replay import (
    SCHEMA_VERSION,
    SCHEDULER_VERSION,
    StormObjectReplay,
    artifact_paths,
    content_sha256,
    load_artifact,
)

MANIFEST_VERSION = "storm-object-prediction-package.v1"
ARTIFACT_MANIFEST_VERSION = "storm-object-scan-artifact-manifest.v1"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_line(payload: dict) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + chr(10)).encode("utf-8")


def build_artifact_manifest(artifact_root: Path, output: Path) -> dict:
    paths = artifact_paths(artifact_root)
    if not paths:
        raise ValueError(f"No storm-object scan artifacts found in {artifact_root}")
    files = []
    pool_records: dict[Path, dict[str, str]] = {}
    for path in paths:
        artifact = load_artifact(path)
        shard = next(part for part in path.parts if part.startswith("shard-"))
        day = artifact["observedAt"][:10]
        pool_path = ROOT / "validation" / "historical-camera-free-pool" / shard / f"pool-{day}.jsonl"
        if pool_path not in pool_records:
            records = {}
            with pool_path.open("rb") as stream:
                for raw in stream:
                    if raw.strip():
                        row = json.loads(raw)
                        records[row["observedAt"]] = hashlib.sha256(raw).hexdigest()
            pool_records[pool_path] = records
        expected_source_hash = pool_records[pool_path].get(artifact["observedAt"])
        if expected_source_hash != artifact["sourceRecordSha256"]:
            raise ValueError(
                f"Frozen pool source mismatch for {artifact['observedAt']}: {pool_path}"
            )
        files.append({
            "path": str(path),
            "observedAt": artifact["observedAt"],
            "compressedSha256": file_sha256(path),
            "expandedMrmsSha256": artifact["expandedMrmsSha256"],
            "sourceRecordSha256": artifact["sourceRecordSha256"],
            "sourcePoolPath": str(pool_path),
            "s3Key": artifact["s3Key"],
        })
    manifest = {
        "schemaVersion": ARTIFACT_MANIFEST_VERSION,
        "artifactCount": len(files),
        "firstObservedAt": files[0]["observedAt"],
        "lastObservedAt": files[-1]["observedAt"],
        "files": files,
    }
    manifest["manifestContentSha256"] = content_sha256(manifest)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2) + chr(10), encoding="utf-8")
    return manifest


def verify_artifact_manifest(path: Path) -> dict:
    errors = []
    pool_records: dict[Path, dict[str, str]] = {}
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {"ok": False, "errors": [f"invalid artifact manifest: {error}"]}
    expected = manifest.pop("manifestContentSha256", None)
    if content_sha256(manifest) != expected:
        errors.append("artifact manifest hash mismatch")
    for item in manifest.get("files") or []:
        artifact_path = Path(item["path"])
        if not artifact_path.exists():
            errors.append(f"missing scan artifact: {artifact_path}")
        elif file_sha256(artifact_path) != item["compressedSha256"]:
            errors.append(f"scan artifact hash mismatch: {artifact_path}")
        pool_path = Path(item.get("sourcePoolPath") or "")
        if not pool_path.exists():
            errors.append(f"missing frozen pool source: {pool_path}")
        else:
            if pool_path not in pool_records:
                records = {}
                with pool_path.open("rb") as stream:
                    for raw in stream:
                        if raw.strip():
                            row = json.loads(raw)
                            records[row["observedAt"]] = hashlib.sha256(raw).hexdigest()
                pool_records[pool_path] = records
            if pool_records[pool_path].get(item["observedAt"]) != item["sourceRecordSha256"]:
                errors.append(f"frozen pool source hash mismatch: {pool_path}:{item['observedAt']}")
    return {"ok": not errors, "errors": errors}


def run_package(artifact_manifest_path: Path, config_path: Path, output_dir: Path) -> dict:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Prediction output directory must be empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    verification = verify_artifact_manifest(artifact_manifest_path)
    if not verification["ok"]:
        raise ValueError("Artifact package failed verification: " + "; ".join(verification["errors"]))
    artifact_manifest = json.loads(artifact_manifest_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    replay = StormObjectReplay(config)
    day_file = None
    day_path = None
    day_digest = None
    day_records = 0
    previous_hash = "0" * 64
    files = []
    totals = defaultdict(int)

    def close_day():
        nonlocal day_file, day_path, day_digest, day_records, previous_hash
        if day_file is None:
            return
        day_file.close()
        files.append({
            "path": day_path.name,
            "records": day_records,
            "contentSha256": day_digest.hexdigest(),
            "finalRecordHash": previous_hash,
        })
        day_file = day_path = day_digest = None
        day_records = 0
        previous_hash = "0" * 64

    for source in artifact_manifest["files"]:
        artifact = load_artifact(Path(source["path"]))
        record = replay.process_artifact(artifact)
        day = record["scanTime"][:10]
        next_path = output_dir / f"predictions-{day}.jsonl"
        if day_path != next_path:
            close_day()
            day_path = next_path
            day_file = day_path.open("xb")
            day_digest = hashlib.sha256()
        day_records += 1
        envelope = {
            "sequence": day_records,
            "previousHash": previous_hash,
            "prediction": record,
        }
        record_hash = content_sha256(envelope)
        raw = canonical_line({**envelope, "recordHash": record_hash})
        day_file.write(raw)
        day_digest.update(raw)
        previous_hash = record_hash
        totals["scans"] += 1
        totals["stormObjects"] += record["diagnostics"]["stormObjects"]
        totals["familyCandidates"] += record["diagnostics"]["familyCandidates"]
        totals["eligibleFamilies"] += record["diagnostics"]["eligibleFamilies"]
        totals["unattachedSeeds"] += record["diagnostics"]["unattachedSeeds"]
        for alert in record["schedulerAlerts"]:
            totals[f"alerts_capacity{alert['capacity']}_threshold{alert['threshold']}"] += 1
    close_day()
    manifest = {
        "schemaVersion": MANIFEST_VERSION,
        "replaySchemaVersion": SCHEMA_VERSION,
        "schedulerVersion": SCHEDULER_VERSION,
        "configPath": str(config_path),
        "configSha256": file_sha256(config_path),
        "artifactManifestPath": str(artifact_manifest_path),
        "artifactManifestSha256": file_sha256(artifact_manifest_path),
        "files": files,
        "totals": dict(sorted(totals.items())),
        "generatedFromCausalInputsOnly": True,
    }
    manifest["manifestContentSha256"] = content_sha256(manifest)
    (output_dir / "prediction-manifest.json").write_text(
        json.dumps(manifest, indent=2) + chr(10), encoding="utf-8"
    )
    return manifest


def verify_prediction_package(output_dir: Path) -> dict:
    errors = []
    manifest_path = Path(output_dir) / "prediction-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return {"ok": False, "errors": [f"invalid prediction manifest: {error}"]}
    expected = manifest.pop("manifestContentSha256", None)
    if content_sha256(manifest) != expected:
        errors.append("prediction manifest hash mismatch")
    for external, key in (
        (manifest.get("configPath"), "configSha256"),
        (manifest.get("artifactManifestPath"), "artifactManifestSha256"),
    ):
        path = Path(external or "")
        if not path.exists() or file_sha256(path) != manifest.get(key):
            errors.append(f"external source hash mismatch: {path}")
    for item in manifest.get("files") or []:
        path = Path(output_dir) / item["path"]
        if not path.exists():
            errors.append(f"missing prediction file: {path.name}")
            continue
        digest = hashlib.sha256()
        previous_hash = "0" * 64
        count = 0
        with path.open("rb") as stream:
            for count, raw in enumerate(stream, 1):
                digest.update(raw)
                try:
                    row = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    errors.append(f"invalid prediction record: {path.name}:{count}: {error}")
                    break
                record_hash = row.pop("recordHash", None)
                if row.get("sequence") != count or row.get("previousHash") != previous_hash:
                    errors.append(f"prediction chain sequence mismatch: {path.name}:{count}")
                    break
                if content_sha256(row) != record_hash:
                    errors.append(f"prediction record hash mismatch: {path.name}:{count}")
                    break
                previous_hash = record_hash
        if digest.hexdigest() != item["contentSha256"]:
            errors.append(f"prediction content hash mismatch: {path.name}")
        if count != item["records"] or previous_hash != item["finalRecordHash"]:
            errors.append(f"prediction chain terminus mismatch: {path.name}")
    return {"ok": not errors, "errors": errors}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--artifact-manifest", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "validation/storm-object-replay-gate/gate-config-v2.json")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--build-artifact-manifest", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    if args.build_artifact_manifest:
        if not args.artifact_root:
            parser.error("--artifact-root is required with --build-artifact-manifest")
        result = build_artifact_manifest(args.artifact_root, args.artifact_manifest)
    elif args.verify_only:
        if args.output_dir:
            result = verify_prediction_package(args.output_dir)
        else:
            result = verify_artifact_manifest(args.artifact_manifest)
    else:
        if not args.output_dir:
            parser.error("--output-dir is required to run replay")
        result = run_package(args.artifact_manifest, args.config, args.output_dir)
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
