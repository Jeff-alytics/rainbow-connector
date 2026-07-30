import { del, list, put } from "@vercel/blob";
import { configuredStore, redis } from "./alert-common.mjs";

export const MAP_TIER_POINTER_KEY="rainbow:map:tiers:latest";
export const MAP_TIER_SCHEMA="map-tiers.v1";
const PREFIX="map-tiers/map-tiers-2026-07-v1/";

export function validateMapTiers(value){
  if(value?.schemaVersion!==MAP_TIER_SCHEMA)return "invalid schemaVersion";
  if(!Number.isFinite(new Date(value?.scanTime).getTime()))return "invalid scanTime";
  if(!Number.isFinite(new Date(value?.generatedAt).getTime()))return "invalid generatedAt";
  if(value?.tierRuleVersion!=="map-tiers-2026-07-v1")return "invalid tierRuleVersion";
  if(!value?.rain?.geometry||!value?.aligned?.geometry)return "missing tier geometry";
  return "";
}

export async function saveMapTiers(raw,metadata){
  if(!process.env.BLOB_READ_WRITE_TOKEN||!configuredStore())throw new Error("map tier storage is not configured");
  const stamp=metadata.scanTime.replace(/[:.]/g,"-");
  const pathname=`${PREFIX}${stamp}-${metadata.contentSha256.slice(0,12)}.json`;
  const blob=await put(pathname,raw,{access:"public",contentType:"application/json",addRandomSuffix:false,
    cacheControlMaxAge:31536000,token:process.env.BLOB_READ_WRITE_TOKEN});
  const pointer={...metadata,url:blob.url,pathname,publishedAt:new Date().toISOString()};
  await redis(["SET",MAP_TIER_POINTER_KEY,JSON.stringify(pointer),"EX",86400]);
  const found=await list({prefix:PREFIX,limit:100,token:process.env.BLOB_READ_WRITE_TOKEN});
  const old=(found.blobs||[]).sort((a,b)=>new Date(b.uploadedAt)-new Date(a.uploadedAt)).slice(50);
  if(old.length)await del(old.map(item=>item.url),{token:process.env.BLOB_READ_WRITE_TOKEN});
  return pointer;
}

export async function loadMapTierPointer(){
  if(!configuredStore())return null;
  const raw=await redis(["GET",MAP_TIER_POINTER_KEY]);
  return raw?(typeof raw==="string"?JSON.parse(raw):raw):null;
}
