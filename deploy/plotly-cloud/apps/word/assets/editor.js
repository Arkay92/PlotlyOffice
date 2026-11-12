/* Browser editing and selection bridge. Persistence and conversion live in Python. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  let editor, current, savedRange, saveTimer, metricsTimer, dirty = false, saving = null;
  let generation = 0, zoom = 1, docs = [], capabilities = {}, toastTimer;
  let workspaceKey='',displayName='Guest',shareKey=null,baseSnapshot=null,pendingRemote=null;
  let lastLive=0,peers=[],pointer=null,cursorTimer=null,conflicted=false,lastPacket=0;
  const clientId=crypto.randomUUID();
  let fallbackSyncing=false;
  let undoSteps=[],redoSteps=[],tracked=null,historyBusy=false,lastTypingNode=null,lastTypingTime=0;
  let comments=[],commentTarget={quote:'',anchor:0},commentsLoading=false,commentsSignature='';
  let namePrompt=null;
  const defaults = {size:'A4', orientation:'portrait', margin:'normal'};
  const welcome = `<p style="font-size:10pt;color:#7a8d7a">A SPACE FOR YOUR WORDS</p>
    <h1>Something worth saying.</h1>
    <p>Every good idea starts somewhere. A scribbled note. A conversation. A sentence you can’t quite get out of your head.</p>
    <p>This is your space to give it a little room.</p>
    <h2>Begin with the big idea</h2>
    <p>What do you want to say? Start with the part that matters most. You can find the perfect words later.</p>
    <blockquote>“You don’t have to see the whole staircase, just take the first step.”</blockquote>
    <h2>Make it your own</h2>
    <p>A few things to try as you settle in:</p>
    <ul><li><strong>Find your voice.</strong> Add a heading, a little emphasis, or a fresh perspective.</li>
    <li><strong>Bring ideas together.</strong> Insert a table, an image, or a useful link.</li>
    <li><strong>Take your work with you.</strong> Export your draft as a Word document or plain text.</li></ul>
    <h2>A note to your future self</h2><p>It doesn’t have to be perfect. It just has to begin.</p>`;
  const templates = {
    blank: {title:'Untitled document', html:'<p><br></p>'},
    brief: {title:'Project brief', html:'<p style="color:#7a8d7a">PROJECT BRIEF</p><h1>A clear direction.</h1><p>Project name · Prepared by your team</p><h2>The opportunity</h2><p>Describe the problem and why it matters.</p><h2>What success looks like</h2><ul><li>Define the primary outcome.</li><li>Choose how you will measure it.</li></ul><h2>The plan</h2><table><tbody><tr><th>Milestone</th><th>Owner</th><th>Target date</th></tr><tr><td>Discovery</td><td>Your name</td><td>To be confirmed</td></tr><tr><td>Delivery</td><td>Your name</td><td>To be confirmed</td></tr></tbody></table><h2>Next steps</h2><p>What happens next, and who will take the lead?</p>'},
    meeting: {title:'Meeting notes',html:'<p style="color:#7a8d7a">MEETING NOTES</p><h1>A conversation, captured.</h1><p>Date · Attendees</p><h2>On the agenda</h2><ol><li>What we need to discuss</li><li>Decisions we need to make</li></ol><h2>Discussion</h2><p>Capture the ideas that matter.</p><h2>Decisions</h2><ul><li>Record what was agreed.</li></ul><h2>Action items</h2><table><tbody><tr><th>Action</th><th>Owner</th><th>Due</th></tr><tr><td>Next step</td><td>Name</td><td>Date</td></tr></tbody></table>'},
    letter: {title:'A thoughtful letter',html:'<h1>A thoughtful letter.</h1><p>Your name<br>Your address</p><p>Dear recipient,</p><p>Start with what you want them to know.</p><p>Add a little context, and give your ideas room to breathe.</p><p>Warm regards,</p><p>Your name</p>'}
  };

  async function requestResponse(path, options={}) {
    const headers=new Headers(options.headers||{});
    if(workspaceKey)headers.set('Authorization','Bearer '+workspaceKey);
    if(shareKey)headers.set('X-Document-Key',shareKey);
    const suiteBase = location.pathname.startsWith('/word') ? '/word' : '';
    const targetPath = (typeof path === 'string' && path.startsWith('/api/')) ? suiteBase + path : path;
    const response = await fetch(targetPath, {...options,headers});
    const contentType=(response.headers.get('content-type')||'').toLowerCase();
    if(response.headers.get('cf-mitigated')==='challenge') {
      const ray=response.headers.get('cf-ray');
      throw new Error('The hosting security check blocked this request before it reached PlotlyWord. Refresh the page and complete any browser verification, then try again. If it continues, contact the app owner.'+(ray?' Reference: '+ray+'.':''));
    }
    if (!response.ok) {
      let data; try { data = await response.json(); } catch (_) { data = {}; }
      const fallback=response.status===403
        ? 'The host denied this request (403). Refresh the page and try again; if it continues, contact the app owner.'
        : response.status===413 ? 'The upload exceeds the server limit. Please choose a smaller file.'
        : `Request failed (${response.status}). Please try again.`;
      const error=new Error(data?.error || fallback);error.status=response.status;
      error.permissionDenied=response.status===403&&Boolean(data?.error);
      error.code=response.status===409?'conflict':response.status===403?'forbidden':'request';throw error;
    }
    if(contentType.includes('text/html') && !/^attachment(?:;|$)/i.test(response.headers.get('content-disposition')||''))
      throw new Error('The host returned a page instead of document data. The app may be waking up or require browser verification. Wait a moment, then refresh and try again.');
    return response;
  }
  async function api(path, options={}) {
    const response = await requestResponse(path, options);
    return response.status === 204 ? null : response.json();
  }
  const json = (method, body) => ({method, headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  function toast(message, error=false) {
    $('toast').textContent = message; $('toast').className = 'visible' + (error ? ' error' : '');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').className = '', error ? 12000 : 5000);
  }
  function status(text, error=false) { $('save-status').textContent=text; $('save-status').className=error?'error-text':''; }
  function settings() { return {size:$('page-size').value,orientation:$('orientation').value,margin:$('page-margin').value}; }
  function snapshot() { return {title:$('document-title').value || 'Untitled document',html:editor.innerHTML,settings:settings(),version:current?.version}; }
  function documentFields(doc){return {title:doc.title,html:doc.html,settings:{...doc.settings}};}
  function sameFields(a,b){return a.html===b.html&&a.title===b.title&&Object.keys(defaults).every(key=>a.settings[key]===b.settings[key]);}
  function session(){return current?{doc:current.id,key:workspaceKey,link:shareKey,client:clientId}:null;}
  function recoveryKey(){return 'plotlyword-recovery-'+workspaceKey.slice(0,12);}
  function canEdit(){return current&&['owner','edit'].includes(current.role);}
  function backup() {
    if(!current||!canEdit())return;
    try { localStorage.setItem(recoveryKey(),JSON.stringify({id:current?.id, ...snapshot(), dirty, updated:Date.now()})); }
    catch (_) { status('Browser backup unavailable. Save to disk.',true); }
  }
  function changed(event) {
    if(!canEdit())return;
    recordEdit(event);generation++; dirty=true; status('Unsaved changes…'); backup();
    clearTimeout(saveTimer); saveTimer=setTimeout(() => save().catch(e=>toast(e.message,true)),400);
    clearTimeout(metricsTimer); metricsTimer=setTimeout(updateMetrics,250);
  }
  async function save() {
    clearTimeout(saveTimer);
    // Share the WHOLE save cycle, including the library refresh. Waiting on
    // only the write acknowledgement leaves a resolved promise here while refreshLibrary is
    // pending; recursively retrying it starves the browser's event loop.
    if (saving) return saving;
    if (!dirty || !current) return;
    saving = (async () => {
      // Drain edits made during a save without recursion or parallel writes.
      while (dirty && current) {
        if(!canEdit())throw new Error('This document is view-only. Your local draft is preserved.');
        if(conflicted)throw new Error('Resolve the overlapping edit before continuing. Your draft is preserved.');
        const stamp=generation,data=snapshot(),base={...baseSnapshot,settings:{...baseSnapshot.settings}};
        status('Saving…');
        try {
          const result=await persistDocument(data,base);
          result.role=current.role;
          if (generation === stamp) { dirty=false;renderRemote(result);if(!historyBusy)status('All changes saved'); }
          else {current=result;baseSnapshot=documentFields(data);status('Unsaved changes…');}
          if(!dirty&&pendingRemote&&pendingRemote.version>current.version)renderRemote(pendingRemote);
          pendingRemote=null;
          backup(); await refreshLibrary();
        } catch (e) {
          status('Not saved · ' + e.message,true);
          if(e.code==='conflict'){conflicted=true;conflictDialog();}
          throw e;
        }
      }
    })().finally(() => { saving=null; });
    return saving;
  }
  async function refreshLibrary() { docs=await api('/api/documents'); renderLibrary(); }
  function renderLibrary() {
    const search=$('document-search').value.toLowerCase();
    const filtered=docs.filter(d=>d.title.toLowerCase().includes(search));
    $('document-list').innerHTML=filtered.map(d=>`<button class="document-item ${d.id===current?.id?'selected':''}" data-open="${d.id}" title="${esc(d.title)}"><span class="document-icon">▤</span><span><strong>${esc(d.title)}</strong><small>${new Date(d.updated).toLocaleDateString(undefined,{month:'short',day:'numeric'})} · Document</small></span></button>`).join('') || '<p class="empty-note">No documents found.</p>';
  }
  function load(doc) {
    current=doc; dirty=false; conflicted=false; savedRange=null; generation++;pendingRemote=null;
    baseSnapshot=documentFields(doc);lastLive=0;lastPacket=0;peers=[];
    undoSteps=[];redoSteps=[];tracked=documentFields(doc);lastTypingNode=null;
    comments=[];commentsSignature='';commentTarget={quote:'',anchor:0};$('comment-body').value='';$('comment-quote').textContent='Comment on this document';renderComments();refreshComments().catch(()=>{});
    editor.innerHTML=doc.html || '<p><br></p>';
    $('document-title').value=doc.title;
    const s={...defaults,...doc.settings};
    $('page-size').value=s.size; $('orientation').value=s.orientation; $('page-margin').value=s.margin;
    applyLayout(); updateMetrics(); renderLibrary(); status('All changes saved');
    tracked=documentFields(snapshot());
    setRole(doc.role);renderPeers();
    const url=new URL(location.href);url.search='?doc='+doc.id;url.hash=shareKey?'share='+encodeURIComponent(shareKey):'';history.replaceState(null,'',url);
    window.dash_clientside.set_props('collab-session',{data:session()});
    try {localStorage.setItem('plotlyword-current',doc.id);} catch (_) {}
    backup(); $('canvas').scrollTop=0;
  }
  async function create(template=templates.blank) {
    await save();
    const doc=await api('/api/documents',json('POST',{...template,settings:template.settings||defaults}));
    shareKey=null;await refreshLibrary(); load(doc); closeDialog(); editor.focus();
  }
  function applyLayout() {
    const s=settings();
    let [w,h]=s.size==='Letter'?[816,1056]:[794,1123];
    if (s.orientation==='landscape') [w,h]=[h,w];
    $('paper').style.width=w+'px'; $('paper').style.minHeight=h+'px';
    const padding=s.margin==='narrow'?48:96;
    $('paper').style.padding=padding+'px';
    editor.style.minHeight=Math.max(200,h-2*padding-2)+'px';
    $('paper').style.zoom=zoom;
    $('page-status').textContent=s.size+' · '+(s.orientation==='landscape'?'Landscape':'Portrait');
    let printStyle=$('print-page-style');
    if (!printStyle) {printStyle=document.createElement('style');printStyle.id='print-page-style';document.head.append(printStyle);}
    printStyle.textContent=`@page { size: ${s.size==='Letter'?'letter':'A4'} ${s.orientation}; margin: ${s.margin==='narrow'?'0.5in':'1in'}; }`;
  }
  function rememberSelection() {
    const sel=window.getSelection();
    if (sel.rangeCount && editor.contains(sel.anchorNode) && editor.contains(sel.focusNode)) {savedRange=sel.getRangeAt(0).cloneRange();queueCursor();}
  }
  function restoreSelection(focus=true) {
    if(focus)editor.focus(); const sel=window.getSelection();
    if (savedRange && editor.contains(savedRange.commonAncestorContainer)) { sel.removeAllRanges();sel.addRange(savedRange); }
    else {const range=document.createRange();range.selectNodeContents(editor);range.collapse(false);sel.removeAllRanges();sel.addRange(range);}
  }
  function command(name,value=null,focus=true) {
    if(!canEdit()||historyBusy)return;restoreSelection(focus);
    const color=name==='foreColor'||name==='hiliteColor',previousCSS=document.queryCommandState('styleWithCSS');
    try{if(color)document.execCommand('styleWithCSS',false,true);document.execCommand(name,false,value);}
    finally{if(color)document.execCommand('styleWithCSS',false,previousCSS);}
    rememberSelection();changed();
  }
  function updateMetrics() {
    const text=editor.innerText.trim(), words=text?text.split(/\s+/u).length:0;
    $('word-count').textContent=words.toLocaleString()+' words';
    $('character-count').textContent=text.length.toLocaleString()+' characters';
    const headings=[...editor.querySelectorAll('h1,h2,h3')];
    $('outline').innerHTML=headings.length?headings.map((h,i)=>`<button data-heading="${i}" class="outline-item level-${h.tagName.toLowerCase()}">${esc(h.textContent||'Untitled section')}</button>`).join(''):'<p class="empty-note">Add headings to find your way around.</p>';
    const sections=[]; let active={title:'Introduction',words:0};
    [...editor.children].forEach(node=>{
      if (/^H[1-3]$/.test(node.tagName)) {if(active.words)sections.push(active);active={title:node.textContent,words:0};}
      const t=node.innerText.trim();active.words+=t?t.split(/\s+/u).length:0;
    });
    if(active.words)sections.push(active);
    if(window.dash_clientside?.set_props) window.dash_clientside.set_props('editor-metrics',{data:{words,sections}});
  }
  function dialog(title,body) {
    rememberSelection(); $('dialog-title').textContent=title;$('dialog-body').innerHTML=body;
    if(!$('dialog').open)$('dialog').showModal();
    setTimeout(()=>$('dialog-body').querySelector('input,button,select')?.focus(),30);
  }
  function closeDialog() { if(!namePrompt)$('dialog').close(); }
  function exportDialog() {
    dialog('Take your words with you',`<p class="dialog-intro">Download a copy of your current draft.</p><div class="format-grid">${[
      ['docx','Word document','.docx · Common formatting'],['txt','Plain text','.txt · Just your words'],['html','Web document','.html · Styled text'],
      ['doc','Word 97–2003','.doc · LibreOffice required'],['odt','OpenDocument','.odt · LibreOffice required'],['rtf','Rich text','.rtf · LibreOffice required'],
      ['pdf','PDF document','.pdf · LibreOffice required']].map(([ext,name,info])=>`<button data-export="${ext}" ${!capabilities.libreoffice&&['doc','odt','rtf','pdf'].includes(ext)?'disabled':''}><b>${name}</b><small>${info}</small></button>`).join('')}</div>
      ${!capabilities.libreoffice?'<p class="notice">LibreOffice is not installed. Install it to enable legacy Word, ODT, RTF and PDF conversion. You can also print to PDF from your browser.</p>':''}
      <p class="muted">Advanced Word features may be simplified. Keep original files when importing.</p><button class="secondary" data-action="print">Print / Save as PDF</button>`);
  }
  async function exportFile(ext) {
    const button=$('dialog-body').querySelector(`[data-export="${ext}"]`); if(button)button.disabled=true;
    try {
      const response=await requestResponse('/api/export/'+ext,json('POST',snapshot()));
      const url=URL.createObjectURL(await response.blob()),a=document.createElement('a');
      a.href=url;a.download=($('document-title').value||'document').replace(/[<>:"/\\|?*]/g,'_')+'.'+ext;
      a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);toast('Your '+ext.toUpperCase()+' file is ready.');
    } finally {if(button)button.disabled=false;}
  }
  async function historyDialog() {
    await save();const history=await api(`/api/documents/${current.id}/history`);
    dialog('A little history',`<p class="dialog-intro">The last 50 saved versions. Restoring creates a new version.</p><div class="history-list">${history.map(h=>`<div><span><b>${esc(h.title)}</b><span class="history-author" style="--author-color:${esc(h.author_color||'#647568')}"><i class="author-dot"></i>${esc(h.author_name||'Unknown (older version)')}</span><small>Version ${h.version} · ${new Date(h.updated).toLocaleString()}</small></span><button class="secondary" data-restore="${h.version}" ${h.version===current.version?'disabled':''}>${h.version===current.version?'Current':'Restore'}</button></div>`).join('')}</div>`);
  }
  function templateDialog() {
    dialog('A good place to begin',`<p class="dialog-intro">A little structure for whatever comes next.</p><div class="template-grid">${[['blank','01','A blank page','All the space you need.'],['brief','02','Project brief','Give your next idea direction.'],['meeting','03','Meeting notes','Keep the conversation moving.'],['letter','04','A thoughtful letter','Make a personal connection.']].map(([key,n,title,desc])=>`<button data-template="${key}"><span class="template-preview"><i>${n}</i><span>━━━━<br>━━━━━━━━<br>━━━━━━━━<br>━━━━━</span></span><b>${title}</b><small>${desc}</small></button>`).join('')}</div>`);
  }
  function findDialog() {
    dialog('Find & replace','<label class="field">Find<input id="find-text" placeholder="What are you looking for?"></label><label class="field">Replace with<input id="replace-text" placeholder="Replacement text"></label><p id="find-feedback" class="muted">Matches are case-sensitive. Replace preserves surrounding formatting.</p><div class="dialog-actions"><button class="secondary" data-action="find-next">Find next</button><button class="primary" data-action="replace-all">Replace all</button></div>');
  }
  function textNodes() {
    const walk=document.createTreeWalker(editor,NodeFilter.SHOW_TEXT),nodes=[];let node;
    while((node=walk.nextNode()))nodes.push(node);
    return nodes;
  }
  function findNext() {
    const query=$('find-text').value;if(!query)return;
    const nodes=textNodes();let text='',spans=[];
    nodes.forEach(node=>{spans.push({node,start:text.length,end:text.length+node.length});text+=node.textContent;});
    let from=0;
    if(savedRange){const span=spans.find(s=>s.node===savedRange.endContainer);if(span)from=span.start+savedRange.endOffset;}
    let at=text.indexOf(query,from);if(at<0)at=text.indexOf(query);
    if(at<0){$('find-feedback').textContent='No matches found.';return;}
    const start=spans.find(s=>at>=s.start&&at<s.end),end=spans.find(s=>at+query.length>s.start&&at+query.length<=s.end);
    const range=document.createRange();range.setStart(start.node,at-start.start);range.setEnd(end.node,at+query.length-end.start);
    savedRange=range;closeDialog();restoreSelection();start.node.parentElement.scrollIntoView({block:'center'});
  }
  function replaceAll() {
    const query=$('find-text').value,replacement=$('replace-text').value;if(!query)return;
    const nodes=textNodes();let text='',spans=[];
    nodes.forEach(node=>{spans.push({node,start:text.length,end:text.length+node.length});text+=node.textContent;});
    const matches=[];let at=0;
    while((at=text.indexOf(query,at))>=0){matches.push(at);at+=query.length;}
    // Replace from the end so earlier offsets remain valid, including across inline formatting.
    matches.reverse().forEach(index=>{
      const first=spans.find(s=>index>=s.start&&index<s.end),last=spans.find(s=>index+query.length>s.start&&index+query.length<=s.end);
      const range=document.createRange();range.setStart(first.node,index-first.start);range.setEnd(last.node,index+query.length-last.start);
      range.deleteContents();range.insertNode(document.createTextNode(replacement));
    });
    savedRange=null;changed();$('find-feedback').textContent=`Replaced ${matches.length} occurrence${matches.length===1?'':'s'}. Use version history to restore earlier text.`;
  }
  function selectedTable() { const node=savedRange?.startContainer; return (node?.nodeType===1?node:node?.parentElement)?.closest('table'); }

  function selectionOffsets(){
    const sel=getSelection();
    if(!sel.rangeCount||!editor.contains(sel.anchorNode)||!editor.contains(sel.focusNode))return null;
    const offset=(node,n)=>{const range=document.createRange();range.selectNodeContents(editor);range.setEnd(node,n);return range.toString().length;};
    return {anchor:offset(sel.anchorNode,sel.anchorOffset),head:offset(sel.focusNode,sel.focusOffset)};
  }
  function nodeAt(offset,forward=false){
    const nodes=textNodes();let remaining=Math.max(0,offset);
    for(const node of nodes){if(remaining<node.length||(remaining===node.length&&!forward))return [node,remaining];remaining-=node.length;}
    return nodes.length?[nodes[nodes.length-1],nodes[nodes.length-1].length]:[editor,0];
  }
  function renderRemote(doc){
    let selected=selectionOffsets();
    if(!selected&&savedRange&&editor.contains(savedRange.commonAncestorContainer)){
      const before=document.createRange();before.selectNodeContents(editor);before.setEnd(savedRange.startContainer,savedRange.startOffset);
      selected={anchor:before.toString().length,head:before.toString().length+savedRange.toString().length};
    }
    const oldText=editor.textContent,focused=document.activeElement===editor;
    const scroll=$('canvas').scrollTop;
    if(editor.innerHTML!==doc.html){
      editor.innerHTML=doc.html||'<p><br></p>';savedRange=null;
      if(selected){
        const newText=editor.textContent;let prefix=0,suffix=0;
        while(prefix<Math.min(oldText.length,newText.length)&&oldText[prefix]===newText[prefix])prefix++;
        while(suffix<Math.min(oldText.length,newText.length)-prefix&&oldText[oldText.length-suffix-1]===newText[newText.length-suffix-1])suffix++;
        const map=n=>n<=prefix?n:n>=oldText.length-suffix?n+newText.length-oldText.length:Math.min(n,newText.length-suffix);
        const [a,ao]=nodeAt(map(selected.anchor)),[h,ho]=nodeAt(map(selected.head));
        if(focused){const sel=getSelection();sel.setBaseAndExtent(a,ao,h,ho);rememberSelection();}
        else{
          const [start,so]=nodeAt(Math.min(map(selected.anchor),map(selected.head))),[end,eo]=nodeAt(Math.max(map(selected.anchor),map(selected.head)));
          savedRange=document.createRange();savedRange.setStart(start,so);savedRange.setEnd(end,eo);
        }
      }
    }
    current=doc;baseSnapshot=documentFields(doc);
    $('document-title').value=doc.title;
    const s={...defaults,...doc.settings};$('page-size').value=s.size;$('orientation').value=s.orientation;$('page-margin').value=s.margin;
    applyLayout();updateMetrics();$('canvas').scrollTop=scroll;renderPeers();renderCommentMarkers();
    if(!dirty)tracked=documentFields(snapshot());updateUndoControls();
  }
  function setRole(role){
    if(current)current.role=role;
    const writable=['owner','edit'].includes(role);
    editor.contentEditable=String(writable&&!historyBusy);editor.setAttribute('aria-readonly',String(!writable));
    $('document-title').readOnly=!writable||historyBusy;
    document.querySelectorAll('#ribbon-home button,#ribbon-home select,#ribbon-home input,#ribbon-insert button,#ribbon-layout select').forEach(el=>el.disabled=!writable||historyBusy);
    document.querySelector('[data-action="history"]').disabled=!writable;
    $('editing-role').textContent=role==='owner'?'OWNER':role==='edit'?'EDITING':role==='view'?'VIEW ONLY':'NO ACCESS';
    $('app-root').classList.toggle('view-only',!writable);updateUndoControls();
  }
  async function persistDocument(document,base){
    // Durable writes must not depend on the presence socket. HTTP uses the
    // same permission checks and transactional merge as WebSocket writes.
    return api('/api/documents/'+current.id,json('PUT',{...document,base}));
  }
  function unavailable(){
    backup();clearTimeout(saveTimer);setRole('none');peers=[];comments=[];renderComments();renderPeers();editor.innerHTML='';$('document-title').value='Access unavailable';
    $('collab-status').textContent='Link revoked or document unavailable';status('Your local draft is preserved.',true);
  }
  async function syncWithoutSocket(){
    if(!current||current.role==='none'||Date.now()-lastLive<=7000||fallbackSyncing)return;
    fallbackSyncing=true;
    const id=current.id;
    peers=[];renderPeers();$('collab-status').textContent='Live cursors reconnecting…';
    try{
      const doc=await api('/api/documents/'+id);
      if(current?.id!==id||current.role==='none')return;
      if(doc.role!==current.role){
        if(doc.role==='view'&&dirty){backup();dirty=false;conflicted=false;clearTimeout(saveTimer);renderRemote(doc);toast('Your permission changed to view-only. Any unsaved draft is kept in this browser.',true);}
        setRole(doc.role);
      }
      if(doc.version>current.version){
        if(dirty||saving||historyBusy)pendingRemote=doc;else{renderRemote(doc);status('All changes saved');}
      }
      if(Date.now()-lastLive>7000)$('collab-status').textContent='Synced · live cursors reconnecting';
      if(dirty&&!saving&&!conflicted&&canEdit())await save();
    }catch(e){
      if(current?.id!==id)return;
      if(e.permissionDenied||e.status===404)unavailable();
      else if(Date.now()-lastLive>7000)$('collab-status').textContent='Connection interrupted · retrying';
    }finally{fallbackSyncing=false;}
  }
  function queueCursor(){
    if(cursorTimer||!current||Date.now()-lastLive>7000)return;
    cursorTimer=setTimeout(()=>{
      cursorTimer=null;if(!current||current.role==='none'||Date.now()-lastLive>7000)return;
      const offsets=selectionOffsets();
      window.dash_clientside.set_props('collab-cursor',{data:{nonce:crypto.randomUUID(),session:session(),cursor:{...(offsets||{}),pointer,version:current.version}}});
    },100);
  }
  function renderPeers(){
    if(!$('collab-cursors'))return;
    const others=peers.filter(p=>p.id!==clientId);
    $('collab-avatars').innerHTML=others.slice(0,6).map(p=>`<span class="collab-avatar" style="background:${p.color}" title="${esc(p.name)}">${esc(p.name.slice(0,1).toUpperCase())}</span>`).join('');
    $('collab-cursors').replaceChildren();
    if(!current||current.role==='none')return;
    const box=$('paper').getBoundingClientRect(),scale=box.width/$('paper').offsetWidth;
    for(const peer of others){
      const cursor=peer.cursor||{};
      if(cursor.pointer){
        const pointerNode=document.createElement('div');pointerNode.className='remote-pointer';pointerNode.style.color=peer.color;
        pointerNode.style.left=cursor.pointer.x*100+'%';pointerNode.style.top=cursor.pointer.y*100+'%';
        const label=document.createElement('span');label.textContent=peer.name;label.style.background=peer.color;pointerNode.append(label);$('collab-cursors').append(pointerNode);
      }
      if(!dirty&&cursor.version===current.version&&Number.isInteger(cursor.anchor)&&Number.isInteger(cursor.head)){
        const [a,ao]=nodeAt(Math.min(cursor.anchor,cursor.head)),[h,ho]=nodeAt(Math.max(cursor.anchor,cursor.head));
        const range=document.createRange();range.setStart(a,ao);range.setEnd(h,ho);
        if(!range.collapsed){for(const r of range.getClientRects()){
          const mark=document.createElement('div');mark.className='remote-selection';mark.style.background=peer.color;
          Object.assign(mark.style,{left:(r.left-box.left)/scale+'px',top:(r.top-box.top)/scale+'px',width:r.width/scale+'px',height:r.height/scale+'px'});$('collab-cursors').append(mark);
        }}
        const [n,o]=nodeAt(cursor.head);range.setStart(n,o);range.collapse(true);let r=range.getBoundingClientRect();
        if(!r.height&&n.parentElement)r=n.parentElement.getBoundingClientRect();
        if(r.height){const caret=document.createElement('div');caret.className='remote-caret';caret.style.borderColor=peer.color;
          Object.assign(caret.style,{left:(r.left-box.left)/scale+'px',top:(r.top-box.top)/scale+'px',height:Math.min(30,r.height/scale)+'px'});
          const label=document.createElement('span');label.textContent=peer.name;label.style.background=peer.color;caret.append(label);$('collab-cursors').append(caret);}
      }
    }
  }
  window.PlotlyWordCollab={
    receive(packet){
      if(!current||packet.doc!==current.id||packet.heartbeat<=lastPacket)return;
      lastPacket=packet.heartbeat;
      if(packet.forbidden){
        unavailable();return;
      }
      const wasDisconnected=Date.now()-lastLive>7000;lastLive=Date.now();
      if(packet.role!==current.role){
        if(packet.role==='view'&&dirty){backup();dirty=false;conflicted=false;clearTimeout(saveTimer);renderRemote(packet.document||current);toast('Your permission changed to view-only. Any unsaved draft is kept in this browser.',true);}
        setRole(packet.role);
      }
      peers=packet.peers||[];
      $('collab-status').textContent=peers.length>1?`${peers.length} people here · Live`:'Only you · Live';
      if(packet.document&&packet.document.version>current.version){
        if(dirty||saving||historyBusy)pendingRemote=packet.document;else{renderRemote(packet.document);status('All changes saved');}
      }
      if(packet.refresh){
        const docId=current.id;
        api('/api/documents/'+docId).then(doc=>{
          if(current?.id!==docId||current.role==='none'||doc.version<=current.version)return;
          if(dirty||saving||historyBusy)pendingRemote=doc;else{renderRemote(doc);status('All changes saved');}
        }).catch(e=>toast(e.message,true));
      }
      renderPeers();
      if(wasDisconnected&&dirty&&!saving&&!conflicted&&canEdit())save().catch(e=>toast(e.message,true));
    }
  };
  function shareUrl(link){const url=new URL(location.href);url.search='?doc='+current.id;url.hash='share='+encodeURIComponent(link.token);return url.href;}
  async function shareDialog(){
    if(!current)return;
    if(current.role!=='owner'){dialog('Shared with you',`<p>You have <strong>${current.role==='edit'?'edit':'view-only'}</strong> access through this link. Only the owner can change sharing settings.</p><button class="primary" data-action="close-dialog">Done</button>`);return;}
    await save();const links=await api(`/api/documents/${current.id}/shares`);
    dialog('Share this document',`<p class="dialog-intro">${links.length?'Anyone with an active link can use its permission.':'Restricted — only your workspace can open this document.'} Names are display names, not verified identities.</p>
      <div class="share-create"><label class="field">Link label<input id="share-label" placeholder="e.g. Review team" maxlength="60"></label><label class="field">Permission<select id="share-role"><option value="view">Can view</option><option value="edit">Can edit</option></select></label><button class="primary" data-action="create-share">Create link</button></div>
      <div class="share-links">${links.map(link=>`<div class="share-link"><b>${esc(link.label)}</b><select data-link-role="${link.id}" aria-label="Permission for ${esc(link.label)}"><option value="view" ${link.role==='view'?'selected':''}>Can view</option><option value="edit" ${link.role==='edit'?'selected':''}>Can edit</option></select><input readonly value="${esc(shareUrl(link))}" aria-label="Share link"><button class="secondary" data-copy-link="${esc(shareUrl(link))}">Copy link</button><button class="quiet danger" data-revoke-link="${link.id}">Revoke</button></div>`).join('')}</div><p class="muted">Viewers can read and download. Editors can change content, but cannot delete this document or manage its links. Revoking a link also removes access for connected visitors.</p>`);
  }
  function profileDialog(){
    dialog('Your workspace',`<label class="field">Your display name<input id="profile-name" value="${esc(displayName)}" maxlength="40"></label><button class="primary" data-action="save-profile">Save name</button><hr><p>Keep your workspace key to open your private documents on another device. Treat it like a password.</p><button class="secondary" data-action="download-key">Download workspace key</button>${Object.keys(localStorage).some(k=>k.startsWith('plotlyword-unavailable-workspace-'))?'<button class="secondary" data-action="download-previous-keys">Download previous workspace keys</button>':''}<label class="field">Restore an existing workspace<input id="restore-key" type="password" placeholder="Paste your workspace key" autocomplete="off"></label><button class="secondary" data-action="restore-workspace">Open workspace</button><button class="quiet" data-action="recover-draft">Recover an unsaved draft</button>`);
  }
  function conflictDialog(){
    dialog('Two edits need a little attention','<p>Another editor changed the same passage. Your text is still here, and the shared version has not been overwritten.</p><div class="dialog-actions"><button class="secondary" data-action="conflict-latest">Keep a copy & load latest</button><button class="primary" data-action="conflict-copy">Continue in my copy</button></div>');
  }
  async function copyDraft(loadLatest=false){
    const original=current.id,link=shareKey;
    const copy=await api('/api/documents',json('POST',{...snapshot(),title:$('document-title').value+' (my draft)'}));
    dirty=false;conflicted=false;clearTimeout(saveTimer);await refreshLibrary();
    if(loadLatest){shareKey=link;load(await api('/api/documents/'+original));}else{shareKey=null;load(copy);}
    closeDialog();toast('Your draft was saved as a private copy.');
  }
  const commands=new Set(['bold','italic','underline','strikeThrough','justifyLeft','justifyCenter','justifyRight','justifyFull','insertUnorderedList','insertOrderedList','indent','outdent','removeFormat','insertHorizontalRule']);
  async function action(name) {
    if(current?.role==='none'&&!['new','templates','profile','close-dialog','restore-workspace','recover-draft','save-profile','download-key','download-previous-keys','session-name'].includes(name))throw new Error('This link no longer grants access to the document.');
    if(!canEdit()&&['replace-all','link','insert-link','table','insert-table','table-row','table-remove','image','page-break','save'].includes(name))throw new Error('This document is view-only.');
    if(commands.has(name)){command(name);return;}
    if(name.startsWith('tab-')){
      const tab=name.slice(4);document.querySelectorAll('.ribbon-panel').forEach(p=>p.classList.toggle('hidden',p.id!=='ribbon-'+tab));
      document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('active',t.id===name));return;
    }
    switch(name){
      case 'undo':await travelHistory(false);break;
      case 'redo':await travelHistory(true);break;
      case 'session-name':{
        const value=$('session-name').value.trim();if(!value)throw new Error('Enter your name to continue.');
        const result=await api('/api/profile',json('PUT',{name:value}));displayName=result.name;
        try{sessionStorage.setItem('plotlyword-named-'+workspaceKey,'yes');}catch(_){}
        const done=namePrompt;namePrompt=null;$('dialog').querySelector('.dialog-heading button').hidden=false;closeDialog();done?.();break;
      }
      case 'comments':$('comments-panel').classList.toggle('hidden');if(!$('comments-panel').classList.contains('hidden')){captureCommentTarget();await refreshComments();}break;
      case 'comment-selection':captureCommentTarget();break;
      case 'post-comment':{
        if(!canEdit())throw new Error('This document is view-only.');
        await api('/api/documents/'+current.id+'/comments',json('POST',{body:$('comment-body').value,...commentTarget}));
        $('comment-body').value='';commentTarget={quote:'',anchor:0};$('comment-quote').textContent='Comment on this document';await refreshComments();break;
      }
      case 'share':await shareDialog();break;
      case 'create-share':await api(`/api/documents/${current.id}/shares`,json('POST',{label:$('share-label').value,role:$('share-role').value}));await shareDialog();break;
      case 'profile':profileDialog();break;
      case 'save-profile':{
        const result=await api('/api/profile',json('PUT',{name:$('profile-name').value}));displayName=result.name;closeDialog();queueCursor();toast('Display name updated.');break;
      }
      case 'download-key':{
        const url=URL.createObjectURL(new Blob([workspaceKey+'\n'],{type:'text/plain'})),link=document.createElement('a');link.href=url;link.download='plotlyword-workspace-key.txt';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);break;
      }
      case 'download-previous-keys':{
        const archived=Object.keys(localStorage).filter(k=>k.startsWith('plotlyword-unavailable-workspace-')).map(k=>JSON.parse(localStorage.getItem(k)));
        const url=URL.createObjectURL(new Blob([JSON.stringify(archived,null,2)],{type:'application/json'})),link=document.createElement('a');
        link.href=url;link.download='plotlyword-previous-workspaces.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);break;
      }
      case 'restore-workspace':{
        const candidate=$('restore-key').value.trim();if(!candidate)throw new Error('Paste a workspace key first.');
        await save();const oldKey=workspaceKey;workspaceKey=candidate;
        try{const identity=await api('/api/session',{method:'POST'});if(identity.valid===false)throw new Error('This server does not recognise that workspace key. Check the key or ask the app owner to restore its saved database.');localStorage.setItem('plotlyword-workspace-key',workspaceKey);location.href=location.pathname;}
        catch(e){workspaceKey=oldKey;throw e;}break;
      }
      case 'recover-draft':{
        const draft=JSON.parse(localStorage.getItem(recoveryKey())||'null');
        if(!draft?.dirty)throw new Error('No unsaved recovery draft is available in this workspace.');
        const copy=await api('/api/documents',json('POST',{...draft,title:draft.title+' (recovered)'}));
        dirty=false;shareKey=null;await refreshLibrary();load(copy);closeDialog();break;
      }
      case 'conflict-copy':await copyDraft();break;
      case 'conflict-latest':await copyDraft(true);break;
      case 'new': await create();break;
      case 'templates':templateDialog();break;
      case 'export':exportDialog();break;
      case 'history':await historyDialog();break;
      case 'find':findDialog();break;
      case 'find-next':findNext();break;
      case 'replace-all':replaceAll();break;
      case 'close-dialog':closeDialog();break;
      case 'print':closeDialog();window.print();break;
      case 'outline':$('outline-panel').classList.toggle('hidden');break;
      case 'focus':$('app-root').classList.toggle('focus-mode');break;
      case 'insights':$('insights-panel').classList.toggle('hidden');window.dispatchEvent(new Event('resize'));break;
      case 'zoom-in':case 'zoom-out':zoom=Math.min(1.5,Math.max(.5,zoom+(name==='zoom-in'?.1:-.1)));$('zoom-label').textContent=Math.round(zoom*100)+'%';applyLayout();break;
      case 'link':dialog('Add a link','<label class="field">Link text<input id="link-label" placeholder="Selected text" value="'+esc(savedRange?.toString()||'')+'"></label><label class="field">Web address<input id="link-url" type="url" placeholder="https://example.com"></label><button class="primary" data-action="insert-link">Insert link</button>');break;
      case 'insert-link':{
        const url=$('link-url').value.trim(),label=$('link-label').value||url;
        if(!/^(https?:\/\/|mailto:)/i.test(url))throw new Error('Enter a link beginning with https://, http:// or mailto:.');
        closeDialog();command('insertHTML',`<a href="${esc(url)}" rel="noopener noreferrer">${esc(label)}</a>`);break;
      }
      case 'table':dialog('Bring a little order','<div class="field-row"><label class="field">Rows<input id="table-rows" type="number" min="1" max="30" value="3"></label><label class="field">Columns<input id="table-cols" type="number" min="1" max="12" value="3"></label></div><button class="primary" data-action="insert-table">Insert table</button>');break;
      case 'insert-table':{
        const r=Number($('table-rows').value),c=Number($('table-cols').value);
        if(!Number.isInteger(r)||!Number.isInteger(c)||r<1||r>30||c<1||c>12)throw new Error('Choose 1–30 rows and 1–12 columns.');
        const table='<table><tbody>'+Array.from({length:r},(_,row)=>'<tr>'+Array.from({length:c},(_,col)=>`<${row?'td':'th'}>${row?'&nbsp;':'Column '+(col+1)}</${row?'td':'th'}>`).join('')+'</tr>').join('')+'</tbody></table><p><br></p>';
        closeDialog();command('insertHTML',table);break;
      }
      case 'table-row':{const table=selectedTable();if(!table)throw new Error('Place the cursor inside a table first.');const row=table.insertRow();for(let i=0;i<table.rows[0].cells.length;i++)row.insertCell().innerHTML='<br>';changed();break;}
      case 'table-remove':{const table=selectedTable();if(!table)throw new Error('Place the cursor inside a table first.');table.remove();savedRange=null;changed();break;}
      case 'image':$('image-input').click();break;
      case 'page-break':command('insertHTML','<div style="page-break-before:always"><br></div><p><br></p>');break;
      case 'more':dialog('Document actions','<div class="menu-actions"><button class="secondary" data-action="save">Save now <small>Ctrl / ⌘ + S</small></button><button class="secondary" data-action="duplicate">Make a copy</button><button class="secondary danger" data-action="delete">Delete this document</button></div>');break;
      case 'save':await save();toast('All changes saved.');break;
      case 'duplicate':await create({...snapshot(),title:$('document-title').value+' (copy)'});toast('A fresh copy is ready.');break;
      case 'delete':if(current.role!=='owner')throw new Error('Only the owner can delete this document.');dialog('Delete this document?',`<p>This removes <strong>${esc(current.title)}</strong>, its saved versions and all share links.</p><div class="dialog-actions"><button class="secondary" data-action="close-dialog">Keep document</button><button class="primary danger" data-action="confirm-delete">Delete document</button></div>`);break;
      case 'confirm-delete':{
        clearTimeout(saveTimer);if(saving)await saving;await api('/api/documents/'+current.id,{method:'DELETE'});dirty=false;current=null;
        shareKey=null;await refreshLibrary();if(docs.length)load(await api('/api/documents/'+docs[0].id));else await create();closeDialog();toast('Document deleted.');break;
      }
    }
  }
  async function importFile(file) {
    if(!file)return;if(file.size>20*1024*1024)throw new Error('Choose a file smaller than 20 MB.');
    if(!file.size)throw new Error('Choose a nonempty document.');
    await save();status('Importing…');
    // Use the same base64 representation as Dash uploads, inside a JSON
    // envelope. The server validates encoding and the original byte limit.
    const content=await new Promise((resolve,reject)=>{
      const reader=new FileReader();
      reader.onload=()=>resolve(String(reader.result).split(',')[1]);
      reader.onerror=()=>reject(new Error('This file could not be read. Please select it again.'));
      reader.onabort=()=>reject(new Error('Reading the file was cancelled.'));
      reader.readAsDataURL(file);
    });
    const imported=await api('/api/import',json('POST',{filename:file.name,content}));
    const doc=await api('/api/documents',json('POST',imported));shareKey=null;await refreshLibrary();load(doc);
    if(imported.warnings.length)dialog('Your document is ready',`<p>${imported.warnings.map(esc).join('</p><p>')}</p><button class="primary" data-action="close-dialog">Start editing</button>`);
    else toast('Document imported.');
  }
  async function addImage(file) {
    if(!file)return;if(file.size>5*1024*1024)throw new Error('Choose an image smaller than 5 MB.');
    if(!/^image\/(png|jpeg|gif|webp)$/.test(file.type))throw new Error('Choose a PNG, JPEG, GIF or WebP image.');
    const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(file);});
    command('insertHTML',`<img src="${data}" alt="${esc(file.name)}"><p><br></p>`);
  }
  function updateUndoControls(){
    const undo=document.querySelector('[data-action="undo"]'),redo=document.querySelector('[data-action="redo"]');
    if(undo)undo.disabled=!canEdit()||historyBusy||!undoSteps.length;
    if(redo)redo.disabled=!canEdit()||historyBusy||!redoSteps.length;
  }
  function recordEdit(event){
    const next=documentFields(snapshot());
    if(tracked&&!sameFields(next,tracked)){
      const node=getSelection()?.anchorNode,now=Date.now();
      if(event?.target===editor&&event?.inputType==='insertText'&&node===lastTypingNode&&now-lastTypingTime<750&&undoSteps.length)undoSteps[undoSteps.length-1].after=next;
      else undoSteps.push({before:tracked,after:next});
      redoSteps=[];lastTypingNode=event?.target===editor&&event?.inputType==='insertText'?node:null;lastTypingTime=now;
      // Retain at most 50 local operations and bound snapshot memory. Keep the
      // newest operation even for a single large imported document.
      let bytes=undoSteps.reduce((n,step)=>n+2*(step.before.html.length+step.after.html.length),0);
      while(undoSteps.length>1&&(undoSteps.length>50||bytes>64*1024*1024)){
        const old=undoSteps.shift();bytes-=2*(old.before.html.length+old.after.html.length);
      }
    }
    tracked=next;updateUndoControls();
  }
  async function travelHistory(redo){
    if(historyBusy||!canEdit())return;
    const from=redo?redoSteps:undoSteps,to=redo?undoSteps:redoSteps;if(!from.length)return;
    const step=from[from.length-1],expected=redo?step.before:step.after,target=redo?step.after:step.before;
    const visible=documentFields(snapshot());
    if(sameFields(visible,expected)){
      // The visible state is still our own edit: reverse it immediately, even
      // while autosave is in flight or offline. Keep baseSnapshot untouched so
      // the ordinary save merges safely with any unseen collaborator edits.
      from.pop();to.push(step);clearTimeout(saveTimer);lastTypingNode=null;
      editor.innerHTML=target.html||'<p><br></p>';$('document-title').value=target.title;
      const s={...defaults,...target.settings};$('page-size').value=s.size;$('orientation').value=s.orientation;$('page-margin').value=s.margin;
      savedRange=null;generation++;dirty=true;tracked=documentFields(target);
      applyLayout();updateMetrics();renderCommentMarkers();updateUndoControls();backup();restoreSelection();
      status('Unsaved changes…');save().catch(e=>toast(e.message,true));return;
    }
    historyBusy=true;setRole(current.role);status(redo?'Redoing edit…':'Undoing edit…');
    try{
      await save();status(redo?'Redoing edit…':'Undoing edit…');const step=from[from.length-1];
      const base=redo?step.before:step.after,target=redo?step.after:step.before;
      // Apply the inverse local edit against the CURRENT server document. A
      // simultaneous edit to the same passage conflicts instead of being lost.
      const result=await api('/api/documents/'+current.id,json('PUT',{...target,base}));
      from.pop();to.push(step);dirty=false;generation++;lastTypingNode=null;
      renderRemote(result);backup();await refreshLibrary();status('All changes saved');
    }catch(e){
      status('Could not '+(redo?'redo':'undo')+' this edit.',true);
      if(e.code==='conflict')throw new Error('Cannot '+(redo?'redo':'undo')+' this edit because the same passage changed. The shared document has not been overwritten.');
      throw e;
    }finally{historyBusy=false;if(!dirty&&pendingRemote&&pendingRemote.version>current.version)renderRemote(pendingRemote);pendingRemote=null;setRole(current.role);if(canEdit())restoreSelection();}
  }
  async function askSessionName(){
    try{if(sessionStorage.getItem('plotlyword-named-'+workspaceKey))return;}catch(_){}
    await new Promise(resolve=>{
      namePrompt=resolve;
      dialog('What should collaborators call you?',`<p>Your name appears beside your cursor, comments and edits.</p><label class="field">Your name<input id="session-name" maxlength="40" autocomplete="name" value="${esc(displayName==='Guest'?'':displayName)}" placeholder="Enter your name"></label><button class="primary" data-action="session-name">Continue</button>`);
      $('dialog').querySelector('.dialog-heading button').hidden=true;
    });
  }
  function captureCommentTarget(){
    const range=savedRange;
    if(range&&editor.contains(range.commonAncestorContainer)&&!range.collapsed){
      const before=document.createRange();before.selectNodeContents(editor);before.setEnd(range.startContainer,range.startOffset);
      const quote=range.toString();if(quote.length>2000)throw new Error('Select up to 2,000 characters.');
      commentTarget={quote,anchor:before.toString().length};
    }else commentTarget={quote:'',anchor:0};
    $('comment-quote').textContent=commentTarget.quote?'Selected: '+commentTarget.quote:'Comment on this document';
  }
  function commentPosition(c){
    if(!c?.quote)return -1;const text=editor.textContent;
    if(text.slice(c.anchor,c.anchor+c.quote.length)===c.quote)return c.anchor;
    const at=text.indexOf(c.quote);return at>=0&&text.indexOf(c.quote,at+1)<0?at:-1;
  }
  function renderCommentMarkers(){
    const layer=$('comment-markers');if(!layer)return;layer.replaceChildren();
    if(!current||current.role==='none')return;
    const box=$('paper').getBoundingClientRect(),scale=box.width/$('paper').offsetWidth;
    const rows=new Map();
    for(const c of comments){
      if(c.resolved)continue;const at=commentPosition(c);if(at<0)continue;
      const [node,offset]=nodeAt(at,true),range=document.createRange();range.setStart(node,offset);range.collapse(true);
      let rect=range.getBoundingClientRect();if(!rect.height)rect=node.parentElement.getBoundingClientRect();
      const row=Math.round((rect.top-box.top)/scale),count=rows.get(row)||0;rows.set(row,count+1);
      const marker=document.createElement('button');marker.className='comment-marker';marker.dataset.commentOpen=c.id;
      marker.style.setProperty('--author-color',c.color);marker.style.top=(row+count*25)+'px';marker.textContent=c.author_name.slice(0,1).toUpperCase();marker.title=c.author_name+': '+c.body;marker.setAttribute('aria-label','Comment by '+c.author_name);layer.append(marker);
    }
  }
  function renderComments(){
    $('comment-list').innerHTML=comments.length?comments.map(c=>`<article id="comment-${c.id}" class="comment-card ${c.resolved?'resolved':''}" tabindex="-1" style="--author-color:${esc(c.color)}"><strong><i class="author-dot"></i>${esc(c.author_name)}</strong><small>${new Date(c.created).toLocaleString()}${c.resolved?' · Resolved':''}</small>${c.quote?`<blockquote>${esc(c.quote)}</blockquote>${commentPosition(c)<0?'<small>Quoted text changed or is ambiguous.</small>':''}`:''}<p>${esc(c.body)}</p>${c.quote?`<button class="quiet" data-comment-open="${c.id}">Find text</button>`:''}${canEdit()?`<button class="quiet" data-comment-resolve="${c.id}" data-resolved="${c.resolved}">${c.resolved?'Reopen':'Resolve'}</button>`:''}${c.can_delete?`<button class="quiet danger" data-comment-delete="${c.id}">Delete</button>`:''}</article>`).join(''):'<p class="empty-note">No comments yet. Select some text or comment on the whole document.</p>';
    renderCommentMarkers();
  }
  async function refreshComments(){
    if(!current||current.role==='none'||commentsLoading)return;commentsLoading=true;const id=current.id;
    try{
      const result=await api('/api/documents/'+id+'/comments');if(current?.id!==id||current.role==='none')return;
      const signature=JSON.stringify(result)+current.role;if(signature!==commentsSignature){comments=result;commentsSignature=signature;renderComments();}
    }finally{commentsLoading=false;}
  }
  async function init() {
    document.querySelectorAll('[data-native-input]').forEach(holder=>{
      const props=JSON.parse(holder.dataset.nativeInput),input=document.createElement('input');
      Object.entries(props).forEach(([k,v])=>{if(k==='style')Object.assign(input.style,v);else input.setAttribute(k,String(v));});holder.append(input);
    });
    editor=$('editor');
    setRole('none');
    editor.addEventListener('input',changed);
    document.addEventListener('selectionchange',rememberSelection);
    $('dialog').addEventListener('cancel',event=>{if(namePrompt)event.preventDefault();});
    editor.addEventListener('beforeinput',event=>{
      if(['historyUndo','historyRedo'].includes(event.inputType)){
        event.preventDefault();travelHistory(event.inputType==='historyRedo').catch(e=>toast(e.message,true));
      }
    });
    document.addEventListener('mousedown',event=>{if(event.target.closest('.tool,.ribbon-button'))event.preventDefault();});
    document.addEventListener('click',event=>{
      const el=event.target.closest('[data-action],[data-open],[data-template],[data-export],[data-restore],[data-heading],[data-copy-link],[data-revoke-link]');
      if(!el||historyBusy)return;
      (async()=>{
        if(el.dataset.action)await action(el.dataset.action);
        else if(el.dataset.open){await save();shareKey=null;load(await api('/api/documents/'+el.dataset.open));}
        else if(el.dataset.copyLink){await navigator.clipboard.writeText(el.dataset.copyLink);toast('Share link copied.');}
        else if(el.dataset.revokeLink){await api(`/api/documents/${current.id}/shares/${el.dataset.revokeLink}`,{method:'DELETE'});await shareDialog();toast('Link revoked.');}
        else if(el.dataset.template)await create(templates[el.dataset.template]);
        else if(el.dataset.export)await exportFile(el.dataset.export);
        else if(el.dataset.restore){const old=await api(`/api/documents/${current.id}/history/${el.dataset.restore}`);editor.innerHTML=old.html;$('document-title').value=old.title;const s=old.settings;$('page-size').value=s.size;$('orientation').value=s.orientation;$('page-margin').value=s.margin;applyLayout();savedRange=null;changed();await save();closeDialog();toast('Earlier version restored.');}
        else if(el.dataset.heading!==undefined)editor.querySelectorAll('h1,h2,h3')[Number(el.dataset.heading)]?.scrollIntoView({behavior:'smooth',block:'center'});
      })().catch(e=>toast(e.message,true));
    });
    document.addEventListener('click',event=>{
      const el=event.target.closest('[data-comment-open],[data-comment-resolve],[data-comment-delete]');if(!el||!current)return;
      (async()=>{
        if(el.dataset.commentOpen){
          $('comments-panel').classList.remove('hidden');const card=$('comment-'+el.dataset.commentOpen);card?.focus();card?.scrollIntoView({block:'nearest'});
          const c=comments.find(c=>c.id===el.dataset.commentOpen),at=commentPosition(c);
          if(at>=0){const [n,o]=nodeAt(at,true);n.parentElement?.scrollIntoView({block:'center'});}
        }else{
          const id=el.dataset.commentDelete||el.dataset.commentResolve;
          await api('/api/documents/'+current.id+'/comments/'+id,el.dataset.commentDelete?{method:'DELETE'}:json('PATCH',{resolved:el.dataset.resolved!=='1'}));await refreshComments();
        }
      })().catch(e=>toast(e.message,true));
    });
    document.addEventListener('change',event=>{
      if(event.target.dataset.linkRole)api(`/api/documents/${current.id}/shares/${event.target.dataset.linkRole}`,json('PATCH',{role:event.target.value})).then(()=>toast('Permission updated.')).catch(e=>toast(e.message,true));
    });
    $('document-title').addEventListener('input',changed);
    $('document-search').addEventListener('input',renderLibrary);
    [['block-style','formatBlock'],['font-family','fontName'],['font-size','fontSize']].forEach(([id,cmd])=>$(id).addEventListener('change',()=>command(cmd,$(id).value)));
    [['text-color','foreColor'],['highlight-color','hiliteColor']].forEach(([id,cmd])=>{
      const input=$(id);let applied=null;
      input.addEventListener('pointerdown',()=>{rememberSelection();applied=null;});
      const apply=()=>{if(applied===input.value)return;command(cmd,input.value,false);applied=input.value;};
      input.addEventListener('input',apply);input.addEventListener('change',apply);
    });
    ['page-size','orientation','page-margin'].forEach(id=>$(id).addEventListener('change',()=>{applyLayout();changed();}));
    $('file-input').addEventListener('change',e=>{importFile(e.target.files[0]).catch(err=>{status('Import failed',true);toast(err.message,true);});e.target.value='';});
    $('image-input').addEventListener('change',e=>{addImage(e.target.files[0]).catch(err=>toast(err.message,true));e.target.value='';});
    // Sanitize rich clipboard content on the server before it can enter the DOM.
    editor.addEventListener('paste',async event=>{
      event.preventDefault();if(!canEdit())return;rememberSelection();const pasted=event.clipboardData.getData('text/html'),plain=event.clipboardData.getData('text/plain');
      try {if(pasted){const result=await api('/api/sanitize',json('POST',{html:pasted}));command('insertHTML',result.html);}else command('insertText',plain);}
      catch(e){toast(e.message,true);}
    });
    editor.addEventListener('dragover',event=>event.preventDefault());
    editor.addEventListener('drop',event=>{event.preventDefault();toast('Use Import document or Insert → Image to add files.');});
    editor.addEventListener('keydown',event=>{if(event.key==='Tab'){event.preventDefault();command(event.shiftKey?'outdent':'indent');}});
    document.addEventListener('keydown',event=>{
      if(namePrompt){if(event.key==='Enter'&&event.target.id==='session-name'){event.preventDefault();action('session-name').catch(e=>toast(e.message,true));}return;}
      const editing=editor.contains(event.target)||event.target.id==='document-title';
      if(editing&&(event.ctrlKey||event.metaKey)&&['z','y'].includes(event.key.toLowerCase())){
        event.preventDefault();travelHistory(event.key.toLowerCase()==='y'||event.shiftKey).catch(e=>toast(e.message,true));return;
      }
      if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='s'){event.preventDefault();save().then(()=>toast(canEdit()?'All changes saved.':'View-only document.')).catch(e=>toast(e.message,true));}
      if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='f'){event.preventDefault();findDialog();}
      if(event.key==='Escape')$('app-root').classList.remove('focus-mode');
    });
    window.addEventListener('beforeunload',event=>{if(dirty){backup();event.preventDefault();event.returnValue='';}});
    $('paper').addEventListener('mousemove',event=>{const r=$('paper').getBoundingClientRect();pointer={x:(event.clientX-r.left)/r.width,y:(event.clientY-r.top)/r.height};queueCursor();});
    $('paper').addEventListener('mouseleave',()=>{pointer=null;queueCursor();});
    $('canvas').addEventListener('scroll',()=>{renderPeers();renderCommentMarkers();});window.addEventListener('resize',()=>{renderPeers();renderCommentMarkers();});
    setInterval(()=>refreshComments().catch(()=>{}),3000);
    setInterval(syncWithoutSocket,3000);
    window.addEventListener('online',()=>syncWithoutSocket());
    try{workspaceKey=localStorage.getItem('plotlyword-workspace-key')||'';}catch(_){}
    let identity=await api('/api/session',{method:'POST'}),previousRecovery=null,workspaceUnavailable=false;
    if(identity.valid===false){
      // Never discard the old capability or draft when a host loses/replaces
      // its database. They may become usable again after a database restore.
      const previousKey=workspaceKey;
      const rawRecovery=localStorage.getItem(recoveryKey());
      localStorage.setItem('plotlyword-unavailable-workspace-'+previousKey,JSON.stringify({key:previousKey,archived:Date.now()}));
      try{previousRecovery=JSON.parse(rawRecovery);}catch(_){}
      workspaceKey='';
      identity=await api('/api/session',{method:'POST'});
      workspaceUnavailable=true;
    }
    if(identity.key){workspaceKey=identity.key;localStorage.setItem('plotlyword-workspace-key',workspaceKey);}
    displayName=identity.name;await askSessionName();
    capabilities=await api('/api/capabilities');await refreshLibrary();
    let last,recovery=previousRecovery;try{last=localStorage.getItem('plotlyword-current');recovery=recovery||JSON.parse(localStorage.getItem(recoveryKey()));}catch(_){}
    const requested=new URL(location.href).searchParams.get('doc');shareKey=new URLSearchParams(location.hash.slice(1)).get('share');
    if(requested){
      try{load(await api('/api/documents/'+encodeURIComponent(requested)));}
      catch(e){$('document-title').value='Document unavailable';editor.textContent=e.message;status('This document is private or its link is no longer valid.',true);toast(e.message,true);}
    }else if(docs.length)load(await api('/api/documents/'+(docs.some(d=>d.id===last)?last:docs[0].id)));
    else await create({title:'Something worth saying',html:welcome});
    if(recovery?.dirty){
      // Recover as a separate document; never overwrite a newer server revision.
      const doc=await api('/api/documents',json('POST',{...recovery,title:(recovery.title||'Document')+' (recovered)'}));
      await refreshLibrary();if(!requested){shareKey=null;load(doc);}toast('Recovered an unsaved draft as a private document in your workspace.');
    }
    if(workspaceUnavailable)dialog('Previous workspace unavailable','<p>The server no longer recognises your saved workspace. This can happen after a deployment replaces its database.</p><p>A new private workspace is ready. Your previous key and any browser recovery draft have been kept on this device. Existing server documents require the app owner to restore the original database; creating a new workspace does not restore them.</p><div class="dialog-actions"><button class="secondary" data-action="download-previous-keys">Download previous keys</button><button class="primary" data-action="close-dialog">Continue</button></div>');
  }
  const observer=new MutationObserver(()=>{if($('editor')&&!editor){observer.disconnect();editor=$('editor');init().catch(e=>{status('Could not open workspace',true);toast(e.message,true);});}});
  observer.observe(document.documentElement,{childList:true,subtree:true});
})();
