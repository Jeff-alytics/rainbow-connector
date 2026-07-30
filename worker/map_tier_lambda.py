"""Async Lambda entry point for static ingredients-map generation."""
import base64, gzip, json, os, time
import requests
from map_tiers import build_map_tiers, encode_payload

def handler(event,context):
    started=time.time(); bucket=event["bucket"]; key=event["key"]
    import boto3
    body=boto3.client("s3").get_object(Bucket=bucket,Key=key)["Body"].read()
    sidecar=json.loads(gzip.decompress(body))
    payload=build_map_tiers(sidecar); raw,compressed,content_hash=encode_payload(payload)
    envelope={"schemaVersion":"map-tier-publish.v1","scanTime":payload["scanTime"],
      "generatedAt":payload["generatedAt"],"tierRuleVersion":payload["tierRuleVersion"],
      "contentSha256":content_hash,"gzipBytes":len(compressed),
      "gzipBase64":base64.b64encode(compressed).decode("ascii")}
    url=os.environ["RAINBOW_MAP_TIER_PUBLISH_URL"]; secret=os.environ["SATELLITE_PUBLISH_SECRET"]
    response=requests.post(url,json=envelope,headers={"Authorization":f"Bearer {secret}"},timeout=25)
    response.raise_for_status()
    return {"ok":True,"scanTime":payload["scanTime"],"generatedAt":payload["generatedAt"],
      "generationLagSeconds":payload["generationLagSeconds"],"gzipBytes":len(compressed),
      "contentSha256":content_hash,"publisher":response.json(),"runtimeMs":round((time.time()-started)*1000)}
