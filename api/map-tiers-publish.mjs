import { gunzipSync } from "node:zlib";
import { createHash } from "node:crypto";
import { json, readJsonBody } from "./alert-common.mjs";
import { verifyPublishSecret } from "./satellite-candidates.mjs";
import { saveMapTiers, validateMapTiers } from "./map-tier-common.mjs";
export const config={maxDuration:20};

export default async function handler(req,res){
  if(req.method!=="POST"){res.setHeader("Allow","POST");return json(res,405,{ok:false,error:"Method not allowed."});}
  if(!verifyPublishSecret(req))return json(res,401,{ok:false,error:"Unauthorized."});
  const body=await readJsonBody(req);
  if(body?.schemaVersion!=="map-tier-publish.v1")return json(res,400,{ok:false,error:"Invalid envelope."});
  const compressed=Buffer.from(String(body.gzipBase64||""),"base64");
  if(compressed.length>150000)return json(res,413,{ok:false,error:"Tier payload exceeds 150 KB."});
  let raw,payload;
  try{raw=gunzipSync(compressed);payload=JSON.parse(raw.toString("utf8"));}catch{return json(res,400,{ok:false,error:"Invalid compressed payload."});}
  const error=validateMapTiers(payload);if(error)return json(res,400,{ok:false,error});
  const hash=createHash("sha256").update(raw).digest("hex");
  if(hash!==body.contentSha256)return json(res,400,{ok:false,error:"Content hash mismatch."});
  const pointer=await saveMapTiers(raw,{scanTime:payload.scanTime,generatedAt:payload.generatedAt,
    generationLagSeconds:payload.generationLagSeconds,tierRuleVersion:payload.tierRuleVersion,
    contentSha256:hash,gzipBytes:compressed.length});
  return json(res,200,{ok:true,pointer});
}
