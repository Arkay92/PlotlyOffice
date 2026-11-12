"""Validated, JSON slide model and deliberately bounded PowerPoint import."""
import base64
import io
import math
import re
import zipfile
from uuid import uuid4
from PIL import Image
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from lxml import etree

MAX_BYTES = 20 * 1024 * 1024

def sanitize(value):
    # Storage's inherited HTML slot is unused: slides are structured JSON.
    return ''

def color(value, default='#ffffff'):
    return value if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value) else default

def number(value, default, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return default
    return max(low, min(high, value))

def text(value, limit=20000):
    if not isinstance(value, str): raise ValueError('Expected text.')
    return value[:limit]

def image_checked(value):
    if not isinstance(value, str) or not re.match(r'^data:image/(png|jpeg|gif|webp);base64,', value):
        raise ValueError('Use an embedded PNG, JPEG, GIF or WebP image.')
    try:
        raw = base64.b64decode(value.split(',', 1)[1], validate=True)
        if len(raw) > 5 * 1024 * 1024: raise ValueError('Images must be under 5 MB.')
        with Image.open(io.BytesIO(raw)) as im:
            if im.width * im.height > 25000000: raise ValueError('Image dimensions are too large.')
            im.verify()
    except Exception as exc:
        raise ValueError('Invalid or oversized image.') from exc
    return value

def valid_id(value):
    return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value) and value not in ('__proto__','constructor','prototype')

def checked(data):
    if not isinstance(data, dict): raise ValueError('Expected presentation data.')
    order, slides = data.get('order'), data.get('slides')
    if not isinstance(order, list) or not 1 <= len(order) <= 200 or not all(valid_id(k) for k in order) or len(set(order)) != len(order):
        raise ValueError('A deck needs 1–200 uniquely identified slides.')
    if not isinstance(slides, dict) or set(slides) != set(order): raise ValueError('Slide order is invalid.')
    out={'order':order,'slides':{},'width':number(data.get('width'),1280,320,2560),'height':number(data.get('height'),720,240,2560)}
    for sid in order:
        s=slides[sid]
        if not isinstance(s,dict): raise ValueError('Invalid slide.')
        objects=s.get('objects',{})
        layers=s.get('layers',list(objects))
        if not isinstance(objects,dict) or len(objects)>150 or not isinstance(layers,list) or not all(valid_id(k) for k in layers) or set(layers)!=set(objects) or len(layers)!=len(objects):
            raise ValueError('Invalid slide objects (maximum 150).')
        slide={'name':text(s.get('name','Slide'),120),'background':color(s.get('background')),'notes':text(s.get('notes','')),'objects':{},'layers':layers,'transition':s.get('transition') if s.get('transition') in ('none','fade') else 'none'}
        for oid,o in objects.items():
            if not isinstance(o,dict) or o.get('type') not in ('text','rect','ellipse','image'): raise ValueError('Unsupported object.')
            obj={'type':o['type'],'x':number(o.get('x'),80,-2560,5120),'y':number(o.get('y'),80,-2560,5120),'w':number(o.get('w'),400,10,5120),'h':number(o.get('h'),100,10,5120),'rotation':number(o.get('rotation'),0,-360,360),'fill':color(o.get('fill'),'#dce8df'),'color':color(o.get('color'),'#233b32'),'fontSize':number(o.get('fontSize'),32,8,240),'fontFamily':o.get('fontFamily') if o.get('fontFamily') in ('Arial','Georgia','Verdana','Courier New') else 'Arial','bold':o.get('bold') is True,'italic':o.get('italic') is True,'align':o.get('align') if o.get('align') in ('left','center','right') else 'left','text':text(o.get('text',''))}
            if o['type']=='image': obj['src']=image_checked(o.get('src'))
            slide['objects'][oid]=obj
        out['slides'][sid]=slide
    return out

def ungrouped(shapes, sx=1, sy=1, dx=0, dy=0):
    """Map nested group child coordinates into slide coordinates."""
    for shape in shapes:
        if shape.shape_type == 6:
            transform = shape._element.grpSpPr.xfrm
            if transform is None:
                yield from ungrouped(shape.shapes, sx, sy, dx, dy)
                continue
            kx = transform.ext.cx / (transform.chExt.cx or transform.ext.cx or 1)
            ky = transform.ext.cy / (transform.chExt.cy or transform.ext.cy or 1)
            yield from ungrouped(shape.shapes, sx*kx, sy*ky,
                dx + sx*(transform.off.x-transform.chOff.x*kx),
                dy + sy*(transform.off.y-transform.chOff.y*ky))
        else:
            yield shape, (dx+sx*shape.left, dy+sy*shape.top,
                          sx*shape.width, sy*shape.height), sy


def theme_colors(slide):
    master=slide.slide_layout.slide_master
    try:
        root=etree.fromstring(master.part.part_related_by(RT.THEME).blob)
        ns={'a':'http://schemas.openxmlformats.org/drawingml/2006/main'}
        result={}
        for node in root.findall('.//a:clrScheme/*',ns):
            if len(node):result[etree.QName(node).localname]=node[0].get('lastClr',node[0].get('val'))
        mapping=master._element.find('{http://schemas.openxmlformats.org/presentationml/2006/main}clrMap')
        if mapping is not None:
            for name,target in mapping.attrib.items():
                if target in result:result[name]=result[target]
        return result
    except (KeyError,AttributeError):return {}


def xml_color(node, colors):
    if node is None:return None
    for child in node:
        kind=etree.QName(child).localname
        value=child.get('val') if kind=='srgbClr' else child.get('lastClr') if kind=='sysClr' else colors.get(child.get('val')) if kind=='schemeClr' else None
        if value and re.fullmatch(r'[0-9A-Fa-f]{6}',value):return '#'+value
    return None


def inherited_text(shape, slide, colors, scale):
    """Resolve paragraph/run defaults and a layout placeholder's first-level style."""
    out={}
    sources=[]
    if shape.is_placeholder:
        for ph in slide.slide_layout.placeholders:
            if ph.placeholder_format.idx==shape.placeholder_format.idx:
                sources.extend(ph._element.xpath('./p:txBody/a:lstStyle/a:lvl1pPr/a:defRPr'))
                break
    sources.extend(shape._element.xpath('./p:txBody/a:lstStyle/a:lvl1pPr/a:defRPr'))
    # A leading Material Icons run is often much larger than the actual body copy.
    paragraph=max(shape.text_frame.paragraphs,key=lambda p:len(p.text))
    sources.extend(paragraph._p.xpath('./a:pPr/a:defRPr'))
    if paragraph.runs:
        run=max(paragraph.runs,key=lambda r:('icon' not in (r.font.name or '').lower(),len(r.text)))
        sources.extend(run._r.xpath('./a:rPr'))
    for props in sources:
        if props.get('sz'):out['fontSize']=int(props.get('sz'))*127*scale
        for attr,key in [('b','bold'),('i','italic')]:
            if props.get(attr) is not None:out[key]=props.get(attr) in ('1','true')
        fill=props.find('{http://schemas.openxmlformats.org/drawingml/2006/main}solidFill')
        value=xml_color(fill,colors)
        if value:out['color']=value
        latin=props.find('{http://schemas.openxmlformats.org/drawingml/2006/main}latin')
        if latin is not None:out['fontFamily']=latin.get('typeface','Arial')
    return out


def import_image(image, cache):
    if image.sha1 in cache:return cache[image.sha1]
    raw=image.blob;kind=image.content_type
    # PowerPoint frequently repeats large opaque PNG backgrounds on many slides.
    # Keep transparency intact; compress opaque photographic images for browser saves.
    if kind=='image/png' and len(raw)>250000:
        with Image.open(io.BytesIO(raw)) as picture:
            opaque='A' not in picture.getbands() or picture.getchannel('A').getextrema()==(255,255)
            if opaque and 'transparency' not in picture.info:
                stream=io.BytesIO();picture.convert('RGB').save(stream,format='JPEG',quality=95,optimize=True)
                if len(stream.getvalue())<len(raw)*.8:raw=stream.getvalue();kind='image/jpeg'
    value='data:'+kind+';base64,'+base64.b64encode(raw).decode()
    cache[image.sha1]=value
    return value


def import_pptx(raw):
    if len(raw)>MAX_BYTES: raise ValueError('Upload limit is 20 MB.')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if len(z.infolist())>5000 or sum(x.file_size for x in z.infolist())>100*1024*1024: raise ValueError('Expanded presentation exceeds 100 MB or 5,000 entries.')
        prs=Presentation(io.BytesIO(raw))
    except (zipfile.BadZipFile, KeyError) as exc: raise ValueError('Invalid PowerPoint file.') from exc
    if not 1<=len(prs.slides)<=200: raise ValueError('Import 1–200 slides.')
    width=1280
    scale=width/prs.slide_width
    data={'width':width,'height':prs.slide_height*scale,'order':[],'slides':{}}
    warnings=['Imports simplify advanced themes and mixed text styles. Large opaque PNGs may be compressed as high-quality JPEGs. Groups are flattened into editable objects. Group rotation, animations, media, charts and tables are not preserved. Keep your original file.']
    image_cache={}
    for index,slide in enumerate(prs.slides):
        sid=uuid4().hex
        colors=theme_colors(slide)
        s={'name':f'Slide {index+1}','background':'#ffffff','notes':'','objects':{},'layers':[]}
        for layer in (slide,slide.slide_layout,slide.slide_layout.slide_master):
            fills=layer._element.xpath('./p:cSld/p:bg/p:bgPr/a:solidFill')
            background=xml_color(fills[0],colors) if fills else None
            if background:s['background']=background;break
        if slide.has_notes_slide: s['notes']=slide.notes_slide.notes_text_frame.text
        shapes=[]
        if slide._element.get('showMasterSp') not in ('0','false'):
            for layer in (slide.slide_layout.slide_master,slide.slide_layout):
                shapes.extend(sh for sh in layer.shapes if not sh.is_placeholder)
        shapes.extend(slide.shapes)
        for shape, bounds, font_scale in ungrouped(shapes):
            oid=uuid4().hex
            o=dict(zip(('x','y','w','h'), (value*scale for value in bounds)))
            o['rotation']=shape.rotation
            if shape.shape_type==13:
                im=shape.image
                if im.content_type not in ('image/png','image/jpeg','image/gif','image/webp'):
                    warnings.append(f'Slide {index+1}: skipped unsupported image format.'); continue
                o.update(type='image',src=import_image(im,image_cache))
            elif shape.has_text_frame and shape.text.strip():
                o.update(type='text',text=shape.text)
                p=shape.text_frame.paragraphs[0]
                if p.runs:
                    f=p.runs[0].font
                    o.update(fontSize=f.size*scale*font_scale if f.size else 32,bold=bool(f.bold),italic=bool(f.italic),fontFamily=f.name or 'Arial')
                    try:
                        if f.color.rgb:o['color']='#'+str(f.color.rgb)
                    except (AttributeError,TypeError):pass
                o.update(inherited_text(shape,slide,colors,scale*font_scale))
                if p.alignment: o['align']={1:'left',2:'center',3:'right'}.get(int(p.alignment),'left')
            elif shape.shape_type==1:
                o['type']='ellipse' if int(shape.auto_shape_type)==9 else 'rect'
                try:
                    if shape.fill.fore_color.rgb:o['fill']='#'+str(shape.fill.fore_color.rgb)
                except (AttributeError,TypeError):pass
            else:
                warnings.append(f'Slide {index+1}: skipped unsupported object.');continue
            s['objects'][oid]=o;s['layers'].append(oid)
        data['order'].append(sid);data['slides'][sid]=s
    return checked(data),list(dict.fromkeys(warnings))
