/* PlotlySlides: structured canvas, optimistic autosave and capability-based sharing. */
(() => {
'use strict';
const $=id=>document.getElementById(id), uid=()=>crypto.randomUUID().replaceAll('-','');
// The model is JSON. Copy containers while sharing immutable image strings,
// instead of allocating another copy of every image for every undo snapshot.
const clone=x=>Array.isArray(x)?x.map(clone):x&&typeof x==='object'?Object.fromEntries(Object.entries(x).map(([k,v])=>[k,clone(v)])):x;
const params=new URLSearchParams(window.top.location.hash.slice(1));
const state={key:localStorage.getItem('plotlyslides.workspace')||'',link:params.get('key')||'',controller:params.get('control')||'',doc:null,base:null,slide:null,selected:null,dirty:false,saving:null,blocked:false,undo:[],redo:[],client:uid(),following:params.get('mode')==='live',presenting:false,broadcast:{active:false},scale:1};
let timer, pollTimer, polling=false, dragging=false, panelBusy=false;
const edit=()=>state.doc && state.doc.role!=='view';
const slide=()=>state.doc?.settings.slides[state.slide];
const object=()=>slide()?.objects[state.selected];
const snapshot=()=>({title:state.doc.title,settings:clone(state.doc.settings)});
const equal=(a,b)=>{if(a===b)return true;if(!a||!b||typeof a!=='object'||typeof b!=='object'||Array.isArray(a)!==Array.isArray(b))return false;const keys=Object.keys(a);return keys.length===Object.keys(b).length&&keys.every(k=>Object.hasOwn(b,k)&&equal(a[k],b[k]));};
function merge(base,current,incoming){
 if(equal(incoming,base)||equal(incoming,current))return clone(current);
 if(equal(current,base))return clone(incoming);
 if([base,current,incoming].every(x=>x&&typeof x==='object'&&!Array.isArray(x))){const out={};for(const k of new Set([...Object.keys(base),...Object.keys(current),...Object.keys(incoming)])){const v=merge(base[k],current[k],incoming[k]);if(v!==undefined)out[k]=v;}return out;}
 throw Error('Another editor changed the same object. Your draft has been preserved.');
}
function notify(message){$('status').textContent=message;}
function fail(e){notify(e.message||String(e));}
async function api(path,method='GET',data,extra={}){
 const headers={Authorization:'Bearer '+state.key,'X-Document-Key':state.link,'X-PlotlySlides-Packed':'1',...extra};
 if(data!==undefined && !(data instanceof FormData))headers['Content-Type']='application/json';
 const result=await PlotlySlidesTransport.request('/api'+path,{method,headers,body:data===undefined?undefined:data instanceof FormData?data:JSON.stringify(PlotlySlidesTransport.packImages(data))});
 return PlotlySlidesTransport.unpackImages(result);
}
function button(text,fn){const b=document.createElement('button');b.textContent=text;b.disabled=panelBusy;b.onclick=async()=>{if(panelBusy)return;panelBusy=true;$('panel').querySelectorAll('button').forEach(n=>n.disabled=true);try{await fn();}catch(e){fail(e);}finally{panelBusy=false;$('panel').querySelectorAll('button').forEach(n=>n.disabled=false);}};return b;}
$('panel').addEventListener('cancel',e=>{if(panelBusy)e.preventDefault();});
function el(tag,text,cls){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;}
function panel(title){$('panel-title').textContent=title;$('panel-body').replaceChildren();if(!$('panel').open)$('panel').showModal();return $('panel-body');}
function bind(id,fn){$(id).onclick=()=>Promise.resolve().then(fn).catch(fail);}
function draftKey(){return 'plotlyslides.draft.'+state.doc.id;}
function persist(){try{localStorage.setItem(draftKey(),JSON.stringify(PlotlySlidesTransport.packImages({base:state.base,draft:snapshot()})));}catch{$('recovery').hidden=false;$('recovery-message').textContent='Browser draft storage is full. Download your draft now; unsaved changes are only in this tab until the server saves them.';notify('Browser draft storage is full. Download your draft now.');}}
function conflict(e){state.blocked=true;persist();$('recovery').hidden=false;$('recovery-message').textContent=e.message;notify('Save paused — review your draft');}
function mutate(fn){if(!edit()||state.blocked)return;const before=snapshot();fn();if(equal(before,snapshot()))return;state.undo.push({before,after:snapshot()});if(state.undo.length>30)state.undo.shift();state.redo=[];changed();render();}
function changed(){state.dirty=true;persist();notify('Unsaved changes…');clearTimeout(timer);timer=setTimeout(()=>save().catch(fail),600);}
async function save(){
 if(state.saving)return state.saving;
 if(!state.dirty||state.blocked||!state.doc)return;
 state.saving=(async()=>{while(state.dirty&&!state.blocked){const id=state.doc.id,sent=snapshot();notify('Saving…');try{
 const saved=await api('/decks/'+id,'PUT',{...sent,version:state.base.version,base:state.base});
 const next=merge(sent,snapshot(),{title:saved.title,settings:saved.settings});
 state.doc={...saved,...next};state.base=clone(saved);state.dirty=!equal(next,{title:saved.title,settings:saved.settings});
 if(!state.dirty){localStorage.removeItem(draftKey());if(!state.blocked)$('recovery').hidden=true;notify('All changes saved');}else persist();
 if(!document.activeElement?.matches('input,textarea,[contenteditable=true]')&&!dragging)render();
 }catch(e){conflict(e);throw e;}}})();
 try{return await state.saving;}finally{state.saving=null;}
}
function blankSlide(kind='title'){
 const s={name:kind==='blank'?'Blank slide':kind==='section'?'Section':'Your next big idea',background:'#f7f7ef',notes:'',transition:'none',objects:{},layers:[]};
 function add(o){const id=uid();s.objects[id]={...defaults('text'),...o};s.layers.push(id);}
 if(kind==='title'){add({text:'YOUR STORY STARTS HERE',x:90,y:105,w:900,h:55,fontSize:19,color:'#66816b'});add({text:'Ideas that move\npeople forward.',x:85,y:210,w:1080,h:230,fontSize:80,fontFamily:'Georgia'});add({text:'A new perspective. A shared ambition.',x:90,y:535,w:950,h:70,fontSize:28,color:'#728176'});}
 if(kind==='section'){s.background='#294c3e';add({text:'A new chapter',x:100,y:240,w:1080,h:170,fontSize:80,color:'#f4f5df',fontFamily:'Georgia'});}
 if(kind==='two'){add({text:'Two sides of a great idea',x:85,y:70,w:1100,h:100,fontSize:55,fontFamily:'Georgia'});add({text:'The opportunity\n\nDescribe what matters most.',x:90,y:240,w:500,h:350,fontSize:30});add({text:'The approach\n\nMake your next step clear.',x:690,y:240,w:500,h:350,fontSize:30});}
 return s;
}
function defaults(type){return {type,x:120,y:140,w:420,h:type==='text'?130:220,text:type==='text'?'Your text here':'',fontSize:36,fontFamily:'Arial',color:'#243e34',fill:'#dce8df',bold:false,italic:false,align:'left',rotation:0};}
function newSettings(){const id=uid();return {width:1280,height:720,order:[id],slides:{[id]:blankSlide()}};}
async function createDeck(settings=newSettings(),title='Untitled presentation'){
 await save();if(state.blocked)throw Error('Resolve the current draft before creating another deck.');
 const d=await api('/decks','POST',{title,settings});state.link='';state.controller='';await openDeck(d.id);setHash();
}
function setHash(mode=''){const p=new URLSearchParams({deck:state.doc.id});if(state.link)p.set('key',state.link);if(mode)p.set('mode',mode);if(state.controller)p.set('control',state.controller);window.top.history.replaceState(null,'','#'+p);}
async function openDeck(id){
 const d=await api('/decks/'+id);state.doc=d;state.base=clone(d);state.slide=d.settings.order[0];state.selected=null;state.dirty=false;state.blocked=false;state.undo=[];state.redo=[];$('recovery').hidden=true;
 const raw=localStorage.getItem(draftKey());if(raw){try{const recovery=PlotlySlidesTransport.unpackImages(JSON.parse(raw));state.doc={...d,...recovery.draft};state.base=recovery.base;state.dirty=true;conflict(Error('A previous unsaved draft was recovered. Save a private copy, download it, or load the latest shared version.'));}catch(e){fail(e);}}
 render();notify(d.role==='view'?'View-only presentation':'All changes saved');
}
function styleObject(n,o){Object.assign(n.style,{left:o.x+'px',top:o.y+'px',width:o.w+'px',height:o.h+'px',transform:`rotate(${o.rotation||0}deg)`,fontSize:o.fontSize+'px',fontFamily:o.fontFamily,color:o.color,fontWeight:o.bold?'bold':'normal',fontStyle:o.italic?'italic':'normal',textAlign:o.align,background:o.type==='rect'||o.type==='ellipse'?o.fill:'transparent',borderRadius:o.type==='ellipse'?'50%':'0'});}
function slideNode(s,scale,interactive=false){
 const inner=el('div',undefined,'slide-inner');Object.assign(inner.style,{width:state.doc.settings.width+'px',height:state.doc.settings.height+'px',transform:`scale(${scale})`,background:s.background});
 for(const id of s.layers){const o=s.objects[id];if(!o)continue;const n=el('div',undefined,'object '+o.type);n.dataset.id=id;styleObject(n,o);if(o.type==='image'){const img=el('img');img.src=o.src;img.alt='Slide image';img.draggable=false;Object.assign(img.style,{width:'100%',height:'100%',objectFit:'fill',display:'block'});n.append(img);}else n.textContent=o.text;
 if(interactive){if(id===state.selected&&edit()){n.classList.add('selected');const handle=el('span',undefined,'resize-handle');n.append(handle);}n.onpointerdown=e=>startDrag(e,id);if(o.type==='text')n.ondblclick=e=>editText(e,n,id);}
 inner.append(n);}return inner;
}
function renderCanvas(){const s=slide();if(!s)return;const wrap=$('canvas-wrap'),d=state.doc.settings;state.scale=$('zoom').value==='fit'?Math.max(.1,Math.min((wrap.clientWidth-60)/d.width,(wrap.clientHeight-60)/d.height)):Number($('zoom').value);Object.assign($('canvas').style,{width:d.width*state.scale+'px',height:d.height*state.scale+'px'});$('canvas').replaceChildren(slideNode(s,state.scale,true));}
function render(keepCanvas=false){
 if(!state.doc)return;if(!slide())state.slide=state.doc.settings.order[0];if(!object())state.selected=null;
 $('title').value=state.doc.title;$('title').disabled=!edit();$('notes').value=slide().notes;$('notes').disabled=!edit();$('notes-panel').hidden=!edit();
 $('slide-name').value=slide().name;$('background').value=slide().background;$('transition').value=slide().transition;$('size').value=state.doc.settings.height===960?'standard':'wide';
 $('thumbnails').replaceChildren();state.doc.settings.order.forEach((id,i)=>{const s=state.doc.settings.slides[id],t=el('div',undefined,'thumb'+(id===state.slide?' active':''));t.tabIndex=0;t.setAttribute('role','button');t.setAttribute('aria-label',`Slide ${i+1}: ${s.name}`);const p=el('div',undefined,'thumb-preview');t.append(p,el('small',`${i+1}   ${s.name}`));$('thumbnails').append(t);p.append(slideNode(s,p.clientWidth/state.doc.settings.width));t.onclick=()=>{state.slide=id;state.selected=null;render();};t.onkeydown=e=>{if(e.key==='Enter')t.click();};});
 $('slide-count').textContent=`Slide ${state.doc.settings.order.indexOf(state.slide)+1} of ${state.doc.settings.order.length}`;
 $('object-properties').hidden=!object();$('selection-help').hidden=!!object();
 if(object())document.querySelectorAll('[data-prop]').forEach(n=>{const v=object()[n.dataset.prop];if(n.type==='checkbox')n.checked=!!v;else n.value=v??'';});
 document.querySelectorAll('#inspector input,#inspector select,#inspector textarea,#inspector button,[data-insert],#add-slide,#duplicate-slide,#delete-slide,#up-slide,#down-slide,#image,#chart').forEach(n=>n.disabled=!edit());
 $('undo').disabled=!edit()||!state.undo.length;$('redo').disabled=!edit()||!state.redo.length;$('share').disabled=state.doc.role!=='owner';$('history').disabled=!edit();if(keepCanvas)syncSelection();else renderCanvas();if(state.presenting)renderPresentation();
}
function syncSelection(){
 for(const node of $('canvas').querySelectorAll('.object')){
  const selected=node.dataset.id===state.selected&&edit();
  node.classList.toggle('selected',selected);
  if(!selected)node.querySelector('.resize-handle')?.remove();
  else if(!node.isContentEditable&&!node.querySelector('.resize-handle'))node.append(el('span',undefined,'resize-handle'));
 }
}
function startDrag(e,id){
 if(e.button!==0||e.target.isContentEditable)return;
 // Commit an inspector field or another text box before selection redraws it.
 if(document.activeElement?.matches('input,textarea,[contenteditable=true]'))document.activeElement.blur();
 e.preventDefault();state.selected=id;render(true);if(!edit()||state.blocked)return;
 const before=snapshot(),o=object(),start={x:e.clientX,y:e.clientY,...clone(o)},resize=e.target.classList.contains('resize-handle');dragging=true;
 const move=event=>{const o=slide().objects[id];
 // Pointer origin is kept separately from the object's canvas coordinates.
 const mx=(event.clientX-e.clientX)/state.scale,my=(event.clientY-e.clientY)/state.scale;
 if(resize){o.w=Math.max(20,start.w+mx);o.h=Math.max(20,start.h+my);}else{o.x=Math.round((start.x+mx)/5)*5;o.y=Math.round((start.y+my)/5)*5;}
 const n=[...$('canvas').querySelectorAll('[data-id]')].find(n=>n.dataset.id===id);if(n)styleObject(n,o);};
 const up=()=>{window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',up);window.removeEventListener('pointercancel',up);dragging=false;if(!equal(before,snapshot())){state.undo.push({before,after:snapshot()});state.redo=[];changed();}render(true);};window.addEventListener('pointermove',move);window.addEventListener('pointerup',up);window.addEventListener('pointercancel',up);
}
function editText(e,n,id){if(!edit()||state.blocked)return;e.stopPropagation();const before=snapshot();n.querySelector('.resize-handle')?.remove();n.contentEditable='true';n.focus();
 n.oninput=()=>{slide().objects[id].text=n.innerText;changed();};n.onpaste=ev=>{ev.preventDefault();document.execCommand('insertText',false,ev.clipboardData.getData('text/plain'));};n.onblur=()=>{n.contentEditable='false';if(!equal(before,snapshot())){state.undo.push({before,after:snapshot()});state.redo=[];}render(true);};}
function insert(type,extra={}){mutate(()=>{const id=uid();slide().objects[id]={...defaults(type),...extra};slide().layers.push(id);state.selected=id;});}
function removeObject(){if(!object())return;mutate(()=>{delete slide().objects[state.selected];slide().layers=slide().layers.filter(x=>x!==state.selected);state.selected=null;});}
async function undo(redo=false){if(!edit()||state.blocked)return;const stack=redo?state.redo:state.undo,op=stack.at(-1);if(!op)return;try{const next=merge(redo?op.before:op.after,snapshot(),redo?op.after:op.before);stack.pop();(redo?state.undo:state.redo).push(op);Object.assign(state.doc,next);changed();render();}catch(e){fail(Error('Undo overlaps a collaborator’s change. Use history or save a copy.'));}}
function download(name,data,type='application/json'){const a=el('a');a.href=URL.createObjectURL(new Blob([data],{type}));a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);}
const safeTitle=()=>state.doc.title.replace(/[<>:"/\\|?*]/g,'_')||'Presentation';
async function exportPptx(){
 await save();const d=state.doc.settings,p=new PptxGenJS();p.defineLayout({name:'CUSTOM',width:d.width/96,height:d.height/96});p.layout='CUSTOM';p.title=state.doc.title;p.author=sessionStorage.getItem('plotlyslides.name')||'PlotlySlides';
 for(const id of d.order){const s=d.slides[id],out=p.addSlide();out.background={color:s.background.slice(1)};for(const oid of s.layers){const o=s.objects[oid],opts={x:o.x/96,y:o.y/96,w:o.w/96,h:o.h/96,rotate:o.rotation||0};if(o.type==='text')out.addText(o.text,{...opts,fontFace:o.fontFamily,fontSize:o.fontSize*.75,color:o.color.slice(1),bold:o.bold,italic:o.italic,align:o.align,margin:6,breakLine:false,valign:'top'});else if(o.type==='image')out.addImage({...opts,data:o.src});else out.addShape(o.type==='ellipse'?p.ShapeType.ellipse:p.ShapeType.rect,{...opts,fill:{color:o.fill.slice(1)},line:{color:o.fill.slice(1),transparency:100}});}out.addNotes(s.notes||'');}
 await p.writeFile({fileName:safeTitle()+'.pptx'});notify('PowerPoint exported');
}
function standalone(){const d=clone(state.doc.settings);for(const s of Object.values(d.slides))delete s.notes;const data=JSON.stringify(d).replaceAll('<','\\u003c');return `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Presentation preview</title><style>body{margin:0;background:#14231a;font-family:Arial}#stage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center}#slide{position:relative;overflow:hidden;transform-origin:top left}#controls{position:fixed;bottom:10px;left:40%;color:white}button{padding:10px;margin:5px}*{box-sizing:border-box}</style><div id="stage"><div id="box"><div id="slide"></div></div></div><div id="controls"><button id="prev">←</button><span id="page"></span><button id="next">→</button></div><script>const d=${data};let i=0;function show(){const s=d.slides[d.order[i]],n=document.getElementById('slide'),k=Math.min(innerWidth/d.width,(innerHeight-70)/d.height);n.replaceChildren();Object.assign(n.style,{width:d.width+'px',height:d.height+'px',background:s.background,transform:'scale('+k+')'});Object.assign(document.getElementById('box').style,{width:d.width*k+'px',height:d.height*k+'px'});for(const id of s.layers){const o=s.objects[id],e=document.createElement(o.type==='image'?'img':'div');if(o.type==='image')e.src=o.src;else e.textContent=o.text;Object.assign(e.style,{position:'absolute',left:o.x+'px',top:o.y+'px',width:o.w+'px',height:o.h+'px',fontSize:o.fontSize+'px',fontFamily:o.fontFamily,fontWeight:o.bold?'bold':'normal',fontStyle:o.italic?'italic':'normal',color:o.color,background:['rect','ellipse'].includes(o.type)?o.fill:'transparent',borderRadius:o.type==='ellipse'?'50%':'0',textAlign:o.align,whiteSpace:'pre-wrap',lineHeight:'1.15',padding:o.type==='text'?'8px':'0',overflow:'hidden',transform:'rotate('+o.rotation+'deg)'});n.append(e);}document.getElementById('page').textContent=(i+1)+' / '+d.order.length;}function go(x){i=Math.max(0,Math.min(d.order.length-1,i+x));show()}document.getElementById('prev').onclick=()=>go(-1);document.getElementById('next').onclick=()=>go(1);onkeydown=e=>{if(['ArrowRight',' '].includes(e.key))go(1);if(e.key==='ArrowLeft')go(-1)};onresize=show;show();<\/script></html>`;}
function printDeck(){let holder=$('print-deck');if(!holder){holder=el('div');holder.id='print-deck';document.body.append(holder);}holder.replaceChildren();for(const id of state.doc.settings.order){const p=el('div',undefined,'print-slide');p.append(slideNode(state.doc.settings.slides[id],1));holder.append(p);}const style=el('style');style.textContent=`@media print{@page{size:${state.doc.settings.width/96}in ${state.doc.settings.height/96}in;margin:0}.print-slide{width:${state.doc.settings.width}px;height:${state.doc.settings.height}px}}`;holder.append(style);window.print();}
async function library(){await save();if(state.blocked)throw Error('Resolve your recovered draft first.');const body=panel('Your presentations');const list=await api('/decks');body.append(button('＋ New presentation',async()=>{await createDeck();$('panel').close();}));for(const d of list){const row=el('div',undefined,'row'),info=el('div',d.title,'grow');info.append(el('small',new Date(d.updated).toLocaleString()));row.append(info,button('Open',async()=>{state.link='';state.controller='';await openDeck(d.id);setHash();$('panel').close();}),button('Delete',async()=>{if(!confirm('Delete this presentation and its history?'))return;await api('/decks/'+d.id,'DELETE');if(d.id===state.doc.id){state.dirty=false;await createDeck();}await library();}));body.append(row);}if(!list.length)body.append(el('p','Create your first presentation.'));}
function urlFor(token,mode='preview',control=''){const url=new URL(window.top.location.href);url.hash=new URLSearchParams({deck:state.doc.id,key:token,mode,...(control?{control}: {})});return url.href;}
function linkRow(body,label,url,remove){const row=el('div',undefined,'row'),wrap=el('div',undefined,'grow');wrap.append(el('strong',label));const input=el('input');input.value=url;input.readOnly=true;input.style.width='100%';input.onclick=()=>input.select();wrap.append(input);row.append(wrap,button('Copy',async()=>{await navigator.clipboard.writeText(url);notify('Link copied');}));if(remove)row.append(button('Revoke',remove));body.append(row);}
async function sharing(){
 await save();const body=panel('Share your presentation');body.append(el('p','Anyone with a link can use its permission. Names are self-selected. Viewers can download the deck, including speaker notes.','notice'));
 const actions=el('div',undefined,'stack');actions.append(button('Create preview link',async()=>{await api('/decks/'+state.doc.id+'/shares','POST',{role:'view',label:'Self-paced preview'});await sharing();}),button('Create editing link',async()=>{await api('/decks/'+state.doc.id+'/shares','POST',{role:'edit',label:'Collaborator'});await sharing();}),button('Start / restart live session',startBroadcast),button('End live session',async()=>{await api('/decks/'+state.doc.id+'/broadcast','DELETE');state.broadcast={active:false};notify('Live session ended');}));body.append(actions);
 const links=await api('/decks/'+state.doc.id+'/shares');for(const l of links){linkRow(body,l.label+' · '+(l.role==='edit'?'Can edit':'Can view'),urlFor(l.token,l.role==='edit'?'edit':l.label==='Live audience'?'live':'preview'),async()=>{await api('/decks/'+state.doc.id+'/shares/'+l.id,'DELETE');await sharing();});body.append(button(l.role==='edit'?'Change to view only':'Allow editing',async()=>{await api('/decks/'+state.doc.id+'/shares/'+l.id,'PUT',{role:l.role==='edit'?'view':'edit'});await sharing();}));}
 body.append(el('p','Starting a live session rotates the controller key. A controller can advance slides and blackout the screen without editing the deck. Ending the session invalidates its controller key. Preview links remain valid until revoked.'));
}
async function startBroadcast(){const response=await api('/decks/'+state.doc.id+'/broadcast','POST');state.broadcast=response;state.controller=response.controller;const l=await api('/decks/'+state.doc.id+'/shares','POST',{role:'view',label:'Live audience'});const body=panel('Your live presentation is ready');body.append(el('p','Audience members can follow your slide changes or browse at their own pace. Keep the remote control link for trusted presenters.','notice'));linkRow(body,'Audience — follow along',urlFor(l.token,'live'));linkRow(body,'Remote control — trusted presenters only',urlFor(l.token,'control',response.controller));body.append(button('Present now',()=>{$('panel').close();enterPresentation();}));}
async function comments(){const body=panel('Slide comments'),sid=state.slide,rows=await api('/decks/'+state.doc.id+'/comments');if(edit()){const input=el('textarea');input.placeholder='Leave a comment on this slide…';input.style.width='100%';body.append(input,button('Post comment',async()=>{await api('/decks/'+state.doc.id+'/comments','POST',{body:input.value,slide:sid});await comments();}));}for(const c of rows){const row=el('div',undefined,'row'),info=el('div',undefined,'grow');info.append(el('strong',c.author_name+(c.resolved?' · Resolved':'')),el('p',c.body),el('small',state.doc.settings.slides[c.quote]?.name||'Deleted slide'));row.append(info);if(state.doc.settings.slides[c.quote])row.append(button('Go to slide',()=>{state.slide=c.quote;render();$('panel').close();}));if(edit())row.append(button(c.resolved?'Reopen':'Resolve',async()=>{await api('/decks/'+state.doc.id+'/comments/'+c.id,'PUT',{resolved:!c.resolved});await comments();}));if(c.can_delete)row.append(button('Delete',async()=>{await api('/decks/'+state.doc.id+'/comments/'+c.id,'DELETE');await comments();}));body.append(row);}}
async function history(){await save();const body=panel('Version history');body.append(el('p','Restoring creates a new revision. Up to 50 versions are retained.'));for(const v of await api('/decks/'+state.doc.id+'/history')){const row=el('div',undefined,'row'),info=el('div',`Version ${v.version} · ${v.author_name}`,'grow');info.append(el('small',new Date(v.updated).toLocaleString()));row.append(info,button('Restore',async()=>{if(!confirm('Restore this version?'))return;const old=await api('/decks/'+state.doc.id+'/history/'+v.version);mutate(()=>{state.doc.title=old.title;state.doc.settings=old.settings;});await save();$('panel').close();}));body.append(row);}}
function workspace(){const body=panel('Your workspace');body.append(el('p','Your workspace key restores ownership on another browser. Keep it private.'));const name=el('input');name.value=sessionStorage.getItem('plotlyslides.name')||'';body.append(name,button('Update name',async()=>{await api('/session','POST',{name:name.value});sessionStorage.setItem('plotlyslides.name',name.value);notify('Name updated');}));body.append(el('hr'),button('Download workspace key',()=>download('plotlyslides-workspace-key.txt',state.key,'text/plain')));const input=el('input');input.placeholder='Paste a workspace key to restore';input.type='password';input.style.width='100%';body.append(el('p','Restore an existing workspace'),input,button('Restore',async()=>{await save();if(state.blocked)throw Error('Save a copy or download your draft first.');const previous=state.key;state.key=input.value.trim();try{await api('/session','POST',{name:name.value});localStorage.setItem('plotlyslides.workspace',state.key);window.top.location.hash='';window.top.location.reload();}catch(e){state.key=previous;throw e;}}));}
function canDrive(){return !!state.controller||(state.doc?.role==='owner'&&!['preview','live'].includes(params.get('mode')));}
function renderPresentation(){if(!state.presenting)return;const d=state.doc.settings,s=slide(),k=Math.min(innerWidth/d.width,(innerHeight-90)/d.height);Object.assign($('screen').style,{width:d.width*k+'px',height:d.height*k+'px'});$('screen').replaceChildren(slideNode(s,k));$('screen').style.visibility=state.broadcast.blackout&&state.following?'hidden':'visible';$('screen').classList.toggle('fade',s.transition==='fade');$('position').textContent=`${d.order.indexOf(state.slide)+1} / ${d.order.length}`;$('follow').textContent='Follow presenter: '+(state.following?'on':'off');$('live-status').textContent=state.broadcast.active?'● Live session':'Self-paced preview';$('blackout').hidden=!canDrive();$('blackout').disabled=!state.broadcast.active;}
async function enterPresentation(){await save();state.broadcast=await api('/decks/'+state.doc.id+'/broadcast');if(state.broadcast.active){if(canDrive()&&!state.following&&params.get('mode')!=='control'){state.broadcast=await api('/decks/'+state.doc.id+'/broadcast','PUT',{slide:state.slide,blackout:false},{'X-Controller-Key':state.controller});}else if((state.following||params.get('mode')==='control')&&state.doc.settings.slides[state.broadcast.slide])state.slide=state.broadcast.slide;}state.presenting=true;$('presentation').hidden=false;renderPresentation();}
async function go(delta){const order=state.doc.settings.order;state.slide=order[Math.max(0,Math.min(order.length-1,order.indexOf(state.slide)+delta))];if(state.broadcast.active&&canDrive()){state.broadcast=await api('/decks/'+state.doc.id+'/broadcast','PUT',{slide:state.slide,blackout:false},{'X-Controller-Key':state.controller});}else state.following=false;renderPresentation();}
async function poll(){if(!state.doc||polling)return;polling=true;const id=state.doc.id;try{
 const peers=await api('/decks/'+id+'/presence','POST',{client:state.client,cursor:{slide:state.slide,object:state.selected}});if(state.doc.id!==id)return;$('peers').replaceChildren();for(const p of peers){const dot=el('span',p.name.slice(0,2).toUpperCase());dot.title=p.name+(p.cursor.slide===state.slide?' · on this slide':'');dot.style.background=p.color;$('peers').append(dot);}
 const latest=await api('/decks/'+id+'?version='+encodeURIComponent(state.base.version));if(state.doc.id!==id)return;
 if(state.doc.role!==latest.role){state.doc.role=latest.role;if(!edit()&&state.dirty)conflict(Error('Your editing permission changed. Save a private copy of your draft.'));render();}
 if(!latest.unchanged&&latest.version!==state.base.version&&!state.dirty&&!state.saving&&!dragging&&!document.activeElement?.matches('input,textarea,[contenteditable=true]')){state.doc=latest;state.base=clone(latest);render();}
 const previousBroadcast=state.broadcast;state.broadcast=await api('/decks/'+id+'/broadcast');if(state.following&&state.broadcast.active&&state.doc.settings.slides[state.broadcast.slide]){const changedSlide=state.slide!==state.broadcast.slide;state.slide=state.broadcast.slide;if(changedSlide)render();}
 if(state.presenting&&!equal(previousBroadcast,state.broadcast))renderPresentation();
 }catch(e){if(e.status===403||e.status===404){if(state.dirty)conflict(e);else{state.presenting=false;$('presentation').hidden=true;$('canvas').replaceChildren();$('thumbnails').replaceChildren();$('notes').value='';state.doc=null;notify('Access ended. Reopen your workspace or a valid link.');}}else notify('Connection interrupted — reconnecting…');}finally{polling=false;}}

bind('close-panel',()=>$('panel').close());bind('library',library);bind('new',()=>createDeck());bind('workspace',workspace);bind('mobile-workspace',workspace);bind('design-toggle',()=>{const open=$('inspector').classList.toggle('mobile-open');$('design-toggle').setAttribute('aria-expanded',String(open));});bind('share',sharing);bind('history',history);bind('comments',comments);bind('undo',()=>undo());bind('redo',()=>undo(true));bind('present',enterPresentation);
$('title').onchange=()=>mutate(()=>state.doc.title=$('title').value);$('notes').onchange=()=>mutate(()=>slide().notes=$('notes').value);$('slide-name').onchange=()=>mutate(()=>slide().name=$('slide-name').value);$('background').onchange=()=>mutate(()=>slide().background=$('background').value);$('transition').onchange=()=>mutate(()=>slide().transition=$('transition').value);$('size').onchange=()=>mutate(()=>{state.doc.settings.width=1280;state.doc.settings.height=$('size').value==='standard'?960:720;});$('zoom').onchange=renderCanvas;
$('theme').onchange=()=>{const themes={sage:['#f7f7ef','#243e34','#dce8df'],midnight:['#182a38','#f4f3eb','#37566d'],paper:['#ffffff','#252525','#e7e7e7'],peach:['#fff0e4','#633d34','#e8bc9e']},t=themes[$('theme').value];if(t)mutate(()=>{slide().background=t[0];for(const o of Object.values(slide().objects)){o.color=t[1];o.fill=t[2];}});$('theme').value='';};
$('layout').onchange=()=>{const value=$('layout').value;if(value&&confirm('Replace the objects on this slide with the selected layout?'))mutate(()=>state.doc.settings.slides[state.slide]=blankSlide(value));$('layout').value='';};
$('canvas').onpointerdown=e=>{if(e.target===$('canvas')||e.target.classList.contains('slide-inner')){state.selected=null;render();}};
document.querySelectorAll('[data-prop]').forEach(n=>n.onchange=()=>{if(!object())return;mutate(()=>object()[n.dataset.prop]=n.type==='checkbox'?n.checked:n.type==='number'?Number(n.value):n.value);});
document.querySelectorAll('[data-insert]').forEach(b=>b.onclick=()=>insert(b.dataset.insert));
bind('add-slide',()=>mutate(()=>{const id=uid();state.doc.settings.order.splice(state.doc.settings.order.indexOf(state.slide)+1,0,id);state.doc.settings.slides[id]=blankSlide('two');state.slide=id;state.selected=null;}));
bind('duplicate-slide',()=>mutate(()=>{const id=uid();state.doc.settings.slides[id]=clone(slide());state.doc.settings.slides[id].name+=' (copy)';state.doc.settings.order.splice(state.doc.settings.order.indexOf(state.slide)+1,0,id);state.slide=id;}));
bind('delete-slide',()=>{if(state.doc.settings.order.length<2)throw Error('Keep at least one slide.');if(confirm('Delete this slide?'))mutate(()=>{const d=state.doc.settings;delete d.slides[state.slide];d.order=d.order.filter(x=>x!==state.slide);state.slide=d.order[0];});});
for(const [id,delta] of [['up-slide',-1],['down-slide',1]])bind(id,()=>mutate(()=>{const a=state.doc.settings.order,i=a.indexOf(state.slide),j=i+delta;if(j>=0&&j<a.length)[a[i],a[j]]=[a[j],a[i]];}));
bind('delete-object',removeObject);bind('duplicate-object',()=>{if(object())insert(object().type,{...clone(object()),x:object().x+25,y:object().y+25});});
for(const [id,front] of [['front',true],['back',false]])bind(id,()=>mutate(()=>{const s=slide();s.layers=s.layers.filter(x=>x!==state.selected);s.layers[front?'push':'unshift'](state.selected);}));
bind('image',()=>$('image-input').click());$('image-input').onchange=async e=>{try{const f=e.target.files[0];if(!f)return;if(f.size>5*1024*1024)throw Error('Use an image under 5 MB.');const src=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=reject;r.readAsDataURL(f);});const im=new Image();im.src=src;await im.decode();insert('image',{src,w:Math.min(650,im.width),h:Math.min(650,im.width)*im.height/im.width});}catch(e){fail(e);}finally{e.target.value='';}};
bind('chart',()=>{const body=panel('Insert a chart'),labels=el('input'),values=el('input');labels.value='Discover, Design, Deliver';values.value='35, 65, 90';body.append(el('p','Create a simple bar chart. It is inserted as an embedded image.'),el('label','Labels, separated by commas'),labels,el('label','Values, separated by commas'),values,button('Insert chart',async()=>{const names=labels.value.split(',').map(s=>s.trim()),nums=values.value.split(',').map(Number);if(nums.some(n=>!Number.isFinite(n)||n<0)||nums.length!==names.length||!nums.length||nums.length>20)throw Error('Enter up to 20 matching labels and non-negative values.');if(!window.Plotly){await new Promise((resolve,reject)=>{const script=document.createElement('script');script.src='/assets/vendor/plotly-basic.min.js';script.onload=resolve;script.onerror=()=>reject(Error('Chart library could not load.'));document.head.append(script);});}const chart=document.createElement('div');document.body.append(chart);try{await Plotly.newPlot(chart,[{type:'bar',x:names,y:nums,marker:{color:'#557d65'}}],{width:1000,height:550,paper_bgcolor:'#ffffff',plot_bgcolor:'#ffffff',font:{family:'Arial',size:22,color:'#243e34'},margin:{t:30,b:90,l:80,r:30}},{staticPlot:true});const src=await Plotly.toImage(chart,{format:'png',width:1000,height:550});insert('image',{src,w:850,h:468});}finally{Plotly.purge(chart);chart.remove();}$('panel').close();}));});
bind('import',()=>$('file-input').click());$('file-input').onchange=async e=>{try{const f=e.target.files[0];if(!f)return;if(f.size>20*1024*1024)throw Error('Upload limit is 20 MB.');let settings,warnings=[];if(f.name.toLowerCase().endsWith('.json')){const data=JSON.parse(await f.text());settings=data.settings||data;}else{const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(Error('The selected file could not be read.'));reader.readAsDataURL(f);});const data=await api('/import','POST',{filename:f.name,content});settings=data.settings;warnings=data.warnings;}await createDeck(settings,f.name.replace(/\.[^.]+$/,''));if(warnings.length){const body=panel('PowerPoint import report');warnings.forEach(w=>body.append(el('p',w)));}}catch(e){fail(e);}finally{e.target.value='';}};
$('export').onchange=async e=>{const type=e.target.value;e.target.value='';try{if(type==='pptx')await exportPptx();if(type==='json')download(safeTitle()+'.json',JSON.stringify(snapshot(),null,2));if(type==='html')download(safeTitle()+'.html',standalone(),'text/html');if(type==='pdf')printDeck();}catch(e){fail(e);}};
bind('retry-save',async()=>{state.blocked=false;await save();if(!state.dirty)$('recovery').hidden=true;});
bind('save-copy',async()=>{const d=await api('/decks','POST',snapshot());localStorage.removeItem(draftKey());state.dirty=false;state.blocked=false;state.link='';state.controller='';await openDeck(d.id);setHash();});bind('download-draft',()=>download(safeTitle()+'-recovery.json',JSON.stringify(snapshot(),null,2)));bind('load-latest',async()=>{if(!confirm('Discard the local draft and load the latest shared version? Download it first if needed.'))return;localStorage.removeItem(draftKey());state.dirty=false;state.blocked=false;await openDeck(state.doc.id);});
bind('exit-present',()=>{state.presenting=false;$('presentation').hidden=true;if(document.fullscreenElement)document.exitFullscreen();render();});bind('prev',()=>go(-1));bind('next',()=>go(1));bind('follow',()=>{state.following=!state.following;renderPresentation();poll();});bind('fullscreen',()=>$('presentation').requestFullscreen());bind('blackout',async()=>{state.broadcast=await api('/decks/'+state.doc.id+'/broadcast','PUT',{slide:state.slide,blackout:!state.broadcast.blackout},{'X-Controller-Key':state.controller});state.following=true;renderPresentation();});
window.addEventListener('resize',()=>{renderCanvas();renderPresentation();});
window.addEventListener('beforeunload',e=>{if(state.dirty){persist();e.preventDefault();e.returnValue='';}});
document.addEventListener('keydown',e=>{if(e.target.matches('input,textarea,[contenteditable=true]'))return;if(state.presenting){if(['ArrowRight',' '].includes(e.key)){e.preventDefault();go(1).catch(fail);}if(e.key==='ArrowLeft')go(-1).catch(fail);if(e.key==='Escape')$('exit-present').click();return;}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s'){e.preventDefault();save().catch(fail);}if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){e.preventDefault();undo(e.shiftKey);}if(e.key==='Delete')removeObject();if(object()&&e.key.startsWith('Arrow')){e.preventDefault();mutate(()=>{const o=object(),step=e.shiftKey?10:1;if(e.key==='ArrowLeft')o.x-=step;if(e.key==='ArrowRight')o.x+=step;if(e.key==='ArrowUp')o.y-=step;if(e.key==='ArrowDown')o.y+=step;});}});
function startupError(error){
 $('name-error').textContent=error.message;
 $('startup-recovery').hidden=false;
 if(!$('name-dialog').open)$('name-dialog').showModal();
}
function currentRecovery(){
 const id=state.doc?.id||params.get('deck');
 if(!id)return null;
 const raw=localStorage.getItem('plotlyslides.draft.'+id);
 if(!raw)return null;
 const value=PlotlySlidesTransport.unpackImages(JSON.parse(raw));
 if(!value.draft?.settings)throw Error('The saved draft is unreadable. Download browser drafts before continuing.');
 return value.draft;
}
function rememberWorkspace(key){
 if(!key)return;
 const previous=JSON.parse(localStorage.getItem('plotlyslides.previous-workspaces')||'[]');
 if(!previous.includes(key))previous.push(key);
 localStorage.setItem('plotlyslides.previous-workspaces',JSON.stringify(previous));
}
async function boot(name,recovered=null){
 const response=await api('/session','POST',{name});
 if(response.valid===false){
  const error=Error('This server no longer recognises your workspace key. A redeployment may have replaced its database. Your old key and browser drafts are still here. Restore the server database to regain existing decks, or start a new workspace and recover this browser’s draft.');
  error.code='workspace_unavailable';throw error;
 }
 if(response.key){state.key=response.key;localStorage.setItem('plotlyslides.workspace',state.key);}
 sessionStorage.setItem('plotlyslides.name',name);
 const id=params.get('deck');
 if(recovered)await createDeck(recovered.settings,recovered.title+' (recovered)');
 else if(id)await openDeck(id);
 else{const list=await api('/decks');if(list.length)await openDeck(list[0].id);else await createDeck();}
 $('startup-recovery').hidden=true;$('name-error').textContent='';$('name-dialog').close();
 setHash(params.get('mode')||'');
 if(['preview','live','control'].includes(params.get('mode')))await enterPresentation();
 await poll();clearInterval(pollTimer);pollTimer=setInterval(poll,1500);
}
bind('startup-key',()=>download('plotlyslides-workspace-key.txt',state.key,'text/plain'));
bind('startup-drafts',()=>{
 const drafts={};for(let i=0;i<localStorage.length;i++){const key=localStorage.key(i);if(key.startsWith('plotlyslides.draft.'))drafts[key]=localStorage.getItem(key);}
 const recovered=currentRecovery();
 download('plotlyslides-browser-recovery.json',JSON.stringify(recovered||{drafts},null,2));
});
bind('startup-new',async()=>{
 try{
  const name=$('display-name').value.trim();if(!name)throw Error('Enter your display name first.');
  const recovered=currentRecovery();rememberWorkspace(state.key);
  const fresh=await api('/session','POST',{name},{Authorization:''});
  if(!fresh.key)throw Error('A new workspace could not be created. Please retry.');
  localStorage.setItem('plotlyslides.workspace',fresh.key);state.key=fresh.key;
  clearTimeout(timer);state.doc=null;state.base=null;state.dirty=false;state.blocked=false;state.link='';state.controller='';state.following=false;
  for(const key of ['deck','key','control','mode'])params.delete(key);
  window.top.history.replaceState(null,'',window.top.location.pathname+window.top.location.search);
  await boot(name,recovered);
 }catch(error){startupError(error);}
});
$('name-dialog').addEventListener('cancel',e=>e.preventDefault());$('name-form').onsubmit=async e=>{e.preventDefault();try{await boot($('display-name').value.trim());}catch(e){startupError(e);}};
const storedName=sessionStorage.getItem('plotlyslides.name');if(storedName){boot(storedName).catch(e=>{$('display-name').value=storedName;startupError(e);});}else $('name-dialog').showModal();
})();
