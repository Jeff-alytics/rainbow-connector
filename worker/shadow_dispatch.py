"""Asynchronous dispatch from the live detector to sunlight-v2 research."""

from __future__ import annotations

import json
import os
from typing import Any


def invoke_sunlight_v2(decision_log: dict, record_count: int, function_name: str | None = None, lambda_client: Any | None = None) -> dict:
    if record_count <= 0:
        return {"ok": True, "skipped": True, "reason": "no candidate decision records"}
    if not decision_log.get("ok") or not decision_log.get("bucket") or not decision_log.get("key"):
        return {"ok": True, "skipped": True, "reason": "decision log unavailable"}
    function_name = (function_name or os.environ.get("RAINBOW_SHADOW_FUNCTION") or "").strip()
    if not function_name:
        return {"ok": True, "skipped": True, "reason": "shadow function not configured"}
    if lambda_client is None:
        import boto3
        lambda_client = boto3.client("lambda")
    payload = {"bucket": decision_log["bucket"], "key": decision_log["key"]}
    response = lambda_client.invoke(FunctionName=function_name, InvocationType="Event", Payload=json.dumps(payload).encode("utf-8"))
    return {"ok": True, "accepted": response.get("StatusCode") == 202, "functionName": function_name, "decisionLogKey": decision_log["key"]}


def safe_invoke_sunlight_v2(*args, **kwargs) -> dict:
    try:
        return invoke_sunlight_v2(*args, **kwargs)
    except Exception as error:
        print(f"[sunlight-v2] async dispatch failed: {str(error)[:300]}")
        return {"ok": False, "error": str(error)[:300], "operationalImpact": False}
