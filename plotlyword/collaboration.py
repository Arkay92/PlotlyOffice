"""Native Dash WebSocket callbacks, with SQLite-backed multi-worker presence.

Every message is authorised at the server. SQLite transactions serialise edits;
the persistent callback pushes changes and cursors over the Dash WebSocket.
"""
import asyncio
import json
import re
import time

from dash import Input, Output, ctx, set_props
from .formats import sanitize, settings_checked, MAX_BYTES
from .merge import MergeConflict
from .storage import ConflictError


def session_checked(value):
    if not isinstance(value,dict): raise PermissionError('Open a document first.')
    doc,key,client=value.get('doc'),value.get('key'),value.get('client')
    if not isinstance(doc,str) or not re.fullmatch('[a-f0-9]{32}',doc): raise PermissionError('Invalid document.')
    if not isinstance(client,str) or not re.fullmatch('[a-f0-9-]{32,36}',client): raise PermissionError('Invalid browser session.')
    return doc,key,value.get('link'),client


def checked_fields(value):
    if not isinstance(value,dict): raise ValueError('Invalid edit.')
    title=value.get('title','Untitled document')
    if not isinstance(title,str): raise ValueError('Title must be text.')
    return {'title':title.strip()[:180] or 'Untitled document','html':sanitize(value.get('html','')),
            'settings':settings_checked(value.get('settings'))}


def apply_edit(store, message):
    if not isinstance(message,dict): raise ValueError('Invalid message.')
    doc,key,link,client=session_checked(message.get('session'))
    grant=store.access(doc,key,link,'edit')
    incoming=checked_fields(message.get('document'))
    base=checked_fields(message.get('base'))
    result=store.save(**incoming,doc_id=doc,base=base,credentials=(key,link))
    return {k:v for k,v in result.items() if k != 'owner_id'} | {'role':grant['role']}


def apply_cursor(store, message):
    if not isinstance(message,dict): raise ValueError('Invalid cursor message.')
    doc,key,link,client=session_checked(message.get('session'))
    grant=store.access(doc,key,link)
    cursor=message.get('cursor') or {}
    cleaned={}
    if not isinstance(cursor,dict): raise ValueError('Invalid cursor.')
    for field in ('anchor','head','version'):
        v=cursor.get(field)
        if isinstance(v,int) and not isinstance(v,bool) and 0<=v<=MAX_BYTES*2: cleaned[field]=v
    pointer=cursor.get('pointer')
    if isinstance(pointer,dict) and all(isinstance(pointer.get(k),(int,float)) and 0<=pointer[k]<=1 for k in ('x','y')):
        cleaned['pointer']={k:pointer[k] for k in ('x','y')}
    store.touch_presence(doc,client,grant,cleaned)


def attach(app, store):
    @app.callback(Output('collab-ack','data'),Input('collab-write','data'),websocket=True,prevent_initial_call=True)
    async def write(message):
        nonce=message.get('nonce') if isinstance(message,dict) else None
        try:
            result=await asyncio.to_thread(apply_edit,store,message)
            return {'nonce':nonce,'document':result}
        except (PermissionError,KeyError,ValueError,ConflictError) as exc:
            code='conflict' if isinstance(exc,(MergeConflict,ConflictError)) else 'forbidden' if isinstance(exc,(PermissionError,KeyError)) else 'invalid'
            return {'nonce':nonce,'error':str(exc),'code':code}

    @app.callback(Output('collab-cursor-ack','data'),Input('collab-cursor','data'),websocket=True,prevent_initial_call=True)
    async def cursor(message):
        try:
            await asyncio.to_thread(apply_cursor,store,message)
        except (PermissionError,KeyError,ValueError):
            pass
        return None

    @app.callback(persistent=True,websocket=True)
    async def stream():
        ws=ctx.websocket
        previous=None; signature=None; heartbeat=0; last_touch=0; old_client=None; old_identity=None
        try:
            while not ws.is_shutdown:
                try:
                    raw=await ws.get_prop('collab-session','data',timeout=10)
                except TimeoutError:
                    # A background tab or slow connection can miss a probe.
                    # Keep the stream alive rather than ending it permanently.
                    await asyncio.sleep(1)
                    continue
                if not raw:
                    await asyncio.sleep(.2); continue
                try:
                    doc,key,link,client=session_checked(raw)
                    grant=await asyncio.to_thread(store.access,doc,key,link)
                    identity=grant['key_hash']
                    room=(doc,client,identity,link)
                    if room != previous:
                        if old_client: await asyncio.to_thread(store.leave,old_client,old_identity)
                        previous=room; signature=None; last_touch=0
                    old_client,old_identity=client,identity
                    now=time.monotonic()
                    if now-last_touch>3:
                        await asyncio.to_thread(store.touch_presence,doc,client,grant)
                        last_touch=now
                    peers=await asyncio.to_thread(store.peers,doc)
                    state=(grant['version'],grant['role'],json.dumps(peers,sort_keys=True))
                    if state != signature or now-heartbeat>3:
                        packet={'doc':doc,'client':client,'role':grant['role'],'peers':peers,'heartbeat':time.time()}
                        if signature is None or signature[0] != grant['version']:
                            document=await asyncio.to_thread(store.get,doc)
                            if len(document['html'].encode('utf-8')) > 4*1024*1024:
                                packet['refresh']=True
                            else:
                                packet['document']={k:v for k,v in document.items() if k != 'owner_id'} | {'role':grant['role']}
                        set_props('collab-incoming',{'data':packet})
                        signature=state; heartbeat=now
                except (PermissionError,KeyError,ValueError) as exc:
                    if old_client:
                        await asyncio.to_thread(store.leave,old_client,old_identity)
                        old_client=None
                    if time.monotonic()-heartbeat>1:
                        set_props('collab-incoming',{'data':{'doc':raw.get('doc') if isinstance(raw,dict) else None,'error':str(exc),'forbidden':True,'heartbeat':time.time()}})
                        heartbeat=time.monotonic()
                    signature=None
                await asyncio.sleep(.1)
        except asyncio.CancelledError:
            pass
        finally:
            if old_client: await asyncio.to_thread(store.leave,old_client,old_identity)
