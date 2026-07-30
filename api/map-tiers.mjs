import { json } from "./alert-common.mjs";
import { loadMapTierPointer, validateMapTiers } from "./map-tier-common.mjs";
export default async function handler(req,res){
  if(req.method!=="GET"){res.setHeader("Allow","GET");return json(res,405,{ok:false,error:"Method not allowed."});}
  try{
    const pointer=await loadMapTierPointer();
    if(!pointer?.url)return json(res,503,{ok:false,error:"Ingredients layer unavailable."});
    const response=await fetch(pointer.url,{cache:"no-store"});if(!response.ok)throw new Error("blob unavailable");
    const payload=await response.json();const error=validateMapTiers(payload);if(error)throw new Error(error);
    const age=Date.now()-new Date(payload.scanTime).getTime();const stale=!Number.isFinite(age)||age>20*60000;
    res.setHeader("Vercel-CDN-Cache-Control","max-age=60, stale-while-revalidate=60");
    res.setHeader("Cache-Control","public, max-age=0, must-revalidate");
    return json(res,200,{...payload,stale,storage:{publishedAt:pointer.publishedAt,generationLagSeconds:pointer.generationLagSeconds}});
  }catch(error){return json(res,503,{ok:false,error:"Ingredients layer unavailable."});}
}
