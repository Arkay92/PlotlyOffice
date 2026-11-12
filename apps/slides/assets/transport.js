/* Shared HTTP handling: never treat an HTML hosting error as a successful save. */
(function(root){
 'use strict';
 function failure(message,status,code){
  const error=new Error(message);error.status=status;error.code=code;return error;
 }
 async function request(url,options={},timeout=20000){
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),timeout);
  try{
   const suiteBase=(typeof location!=='undefined'&&location.pathname.startsWith('/slides'))?'/slides':'';
   const targetUrl=(typeof url==='string'&&url.startsWith('/api/'))?suiteBase+url:url;
   const response=await fetch(targetUrl,{...options,signal:controller.signal,cache:'no-store'});
   if(response.status===204)return null;
   const raw=await response.text();let data;
   try{data=JSON.parse(raw);}catch{/* Cloud proxies may replace JSON with HTML. */}
   const status=response.status,context=` (${options.method||'GET'} ${url}, HTTP ${status})`;
   if(!response.ok||data===undefined||data===null||typeof data!=='object'){
    let message=typeof data?.error==='string'?data.error:'';
    if(!message){
     if(status===401||status===403)message='Access was denied. Your workspace key or share link may no longer be valid. Your local draft has not been discarded.';
     else if(status===404)message='This presentation is unavailable. It may have been deleted, or the server database may have changed.';
     else if(status===413)message='The hosting service rejected this request as too large. Download your draft before reducing image sizes.';
     else if(status===429)message='Too many requests. Wait a moment, then retry.';
     else if(status>=500)message='The hosting service is temporarily unavailable. Your local draft has not been discarded. Retry when it is back online.';
     else message='The server returned an unexpected response instead of presentation data. Your local draft has not been discarded.';
    }
    throw failure(message+context,status,!response.ok?'http_error':'invalid_response');
   }
   return data;
  }catch(error){
   if(typeof error.code==='string')throw error;
   throw failure(controller.signal.aborted?'The request timed out. Your local draft has not been discarded. Retry the save.':'The connection failed. Your local draft has not been discarded. Check your connection and retry.',0,controller.signal.aborted?'timeout':'network_error');
  }finally{clearTimeout(timer);}
 }
 function packImages(data){
  const images=[],seen=new Map();
  const visit=value=>{
   if(Array.isArray(value))return value.map(visit);
   if(!value||typeof value!=='object')return value;
   const out=Object.fromEntries(Object.entries(value).map(([k,v])=>[k,visit(v)]));
   if(out.type==='image'&&typeof out.src==='string'&&out.src.startsWith('data:image/')){
    if(!seen.has(out.src)){seen.set(out.src,images.length);images.push(out.src);}
    out.src='@image:'+seen.get(out.src);
   }
   return out;
  };
  const packed=visit(data);return images.length?{data:packed,images}:data;
 }
 function unpackImages(value){
  if(!value?.data||!Array.isArray(value.images))return value;
  const visit=node=>{
   if(Array.isArray(node))node.forEach(visit);
   else if(node&&typeof node==='object'){
    if(node.type==='image'&&typeof node.src==='string'&&node.src.startsWith('@image:')){
     const index=node.src.slice(7),src=/^\d+$/.test(index)?value.images[Number(index)]:null;
     if(typeof src!=='string'||!src.startsWith('data:image/'))throw Error('Invalid image reference.');
     node.src=src;
    }
    Object.values(node).forEach(visit);
   }
  };
  visit(value.data);return value.data;
 }
 root.PlotlySlidesTransport={request,packImages,unpackImages};
})(globalThis);
