#!/usr/bin/env python
"""Run the research-only causal opportunity replay and write hashed predictions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from causal_replay import (run_streams, streams_from_camera_free_artifact,
                           stream_pool_to_prediction_package, verify_prediction_package,
                           write_prediction_package)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera-free-artifact", type=Path)
    parser.add_argument("--pool", type=Path, nargs="*")
    parser.add_argument("--pool-dir", type=Path, nargs="*")
    parser.add_argument("--allow-camera-filtered", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    pool_paths = list(args.pool or [])
    for directory in args.pool_dir or []:
        pool_paths.extend(sorted(directory.rglob("pool-*.jsonl")))
    if bool(args.camera_free_artifact) == bool(pool_paths):
        parser.error("provide exactly one of --camera-free-artifact or pool inputs")
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
    if args.camera_free_artifact:
        payload = json.loads(args.camera_free_artifact.read_text(encoding="utf-8-sig"))
        streams = streams_from_camera_free_artifact(payload)
        sources = [args.camera_free_artifact]
        result = run_streams(streams, config)
        manifest = write_prediction_package(result, args.output_dir, sources)
        summaries = result["streams"]
    else:
        manifests = [
            path for directory in args.pool_dir or []
            for path in directory.rglob("pool-manifest.json")
        ]
        manifest, summary = stream_pool_to_prediction_package(
            pool_paths, args.output_dir, [*pool_paths, *manifests], config,
            allow_camera_filtered=args.allow_camera_filtered,
        )
        summaries = [summary]
    verification = verify_prediction_package(args.output_dir)
    print(json.dumps({"ok": verification["ok"], "output": str(args.output_dir),
                      "manifestContentSha256": manifest["manifestContentSha256"],
                      "streams": summaries, "verification": verification}, indent=2))
    return 0 if verification["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
