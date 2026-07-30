"""Non-blocking dispatch of a persisted rain footprint to the ledger worker."""

import json
import os


def invoke_opportunity_ledger(footprint, function_name=None, lambda_client=None):
    if not footprint.get("ok") or not footprint.get("bucket") or not footprint.get("key"):
        return {"ok": True, "skipped": True, "reason": "rain footprint unavailable"}
    function_name = (function_name or os.environ.get("RAINBOW_OPPORTUNITY_LEDGER_FUNCTION") or "").strip()
    if not function_name:
        return {"ok": True, "skipped": True, "reason": "opportunity ledger function not configured"}
    if lambda_client is None:
        import boto3
        lambda_client = boto3.client("lambda")
    payload = {"bucket": footprint["bucket"], "key": footprint["key"], "contentSha256": footprint.get("contentSha256")}
    response = lambda_client.invoke(FunctionName=function_name, InvocationType="Event", Payload=json.dumps(payload).encode())
    return {"ok": True, "accepted": response.get("StatusCode") == 202, "functionName": function_name, "rainFootprintKey": footprint["key"]}


def safe_invoke_opportunity_ledger(*args, **kwargs):
    try:
        return invoke_opportunity_ledger(*args, **kwargs)
    except Exception as error:
        print(f"[opportunity-ledger] async dispatch failed: {str(error)[:300]}")
        return {"ok": False, "error": str(error)[:300], "operationalImpact": False}
