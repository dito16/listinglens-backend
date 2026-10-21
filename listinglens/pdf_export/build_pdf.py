#!/usr/bin/env python3
"""MyHome PDF R2. One listing; provenance-first PDF, photo and map cache.
Uses the locally supplied R1 RETURN.zip by default. Optional --live performs ONE
read-only HTML request; never attempts protected APIs or bypasses access denials.
Python 3.9+. Requires requests, Pillow, reportlab; bs4 for --live.
"""
from __future__ import annotations
import argparse, hashlib, html, io, json, math, os, re, sys, zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
    from PIL import Image, ImageOps, ImageDraw
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.colors import HexColor, Color
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase import pdfmetrics
except ImportError as ex:
    print(f'MISSING_DEPENDENCY={ex}; use RUN_MAC.sh', file=sys.stderr); sys.exit(2)

FIELDS = ('title price_usd address city district microdistrict area_m2 rooms bedrooms floor total_floors building_status condition project_type heating hot_water parking internet natural_gas furniture stove conditioner oven sofa washing_machine table refrigerator bed photo_urls map_image_url latitude longitude description owner_type').split()
MAX_IMAGE=6_000_000
CDN_HOSTS={'static-api-statements.tnet.ge'}
SOURCE_URL='https://www.myhome.ge/en/pr/{id}/details/'
MAC_UA='ListingLens-PDF-Preview/2.0 (single user-initiated listing export; contact: local demo)'


def utc():return datetime.now(timezone.utc).isoformat()
def load_inputs(archive=None,fixture=None):
    if archive:
        with zipfile.ZipFile(archive) as z:
            d=json.loads(z.read('EXTRACTED.json'))
            e=json.loads(z.read('EVIDENCE.json'))
        return d,e,'R1_RETURN_ZIP',str(archive)
    obj=json.loads(Path(fixture).read_text(encoding='utf-8'))
    return obj['extracted'],obj['evidence'],'BUNDLED_R1_SNAPSHOT',str(fixture)


def clean_text(value):
    s=html.unescape(re.sub(r'<[^>]*>', ' ', str(value or '')))
    s=re.sub(r'\s+', ' ',s).strip()
    return s


def money_meta(v, listing_id):
    s=str(v)
    # A listing-specific HTML meta title, with explicit dollar marker.
    # Example: "... 1000 $, 110 m², 26387744 | Myhome".
    if str(listing_id) not in s: return None
    candidates=re.findall(r'(?<!\d)(\d[\d\s,]{0,10})\s*(?:\$|USD\b)',s,re.I)
    vals=[]
    for n in candidates:
        try:
            f=int(re.sub('[^0-9]','',n))
            if 50<=f<=10_000_000:vals.append(f)
        except ValueError:pass
    return vals[0] if len(set(vals))==1 else None


def first_evidence(all_e,field,accept):
    for r in all_e:
        if r.get('field')==field and accept(r):return r
    return None


def select_fields(extracted,evidence):
    lid=str(extracted['listing_id'])
    selected={k:extracted.get('fields',{}).get(k) for k in FIELDS}
    origin={k:{'status':'MISSING','method':None,'where':None} for k in FIELDS}
    E=defaultdict(list)
    for row in evidence:E[row.get('field')].append(row)
    for field,val in selected.items():
        if val is None:continue
        candidates=E.get(field,[])
        matched=next((r for r in candidates if r.get('value')==val),None)
        if matched is None and candidates:matched=candidates[0]
        if matched:
            origin[field]={'status':'OBSERVED','method':matched['method'],'where':matched.get('where')}
        else:
            origin[field]={'status':'OBSERVED_UNSOURCED','method':'R1_EXTRACTED','where':None}

    # Extract USD only from a listing-specific meta title with an explicit $ marker.
    if selected['price_usd'] is None:
        for r in E['title']:
            if ':META' not in r.get('method',''):continue
            value=money_meta(r.get('value'),lid)
            if value is not None:
                selected['price_usd']=value
                origin['price_usd']={'status':'OBSERVED','method':'HTML_META_TITLE_EXPLICIT_USD','where':r.get('where','meta')}
                break
    # Textual property type, never an undocumented integer code.
    for field in ('project_type','condition'):
        def good(r):
            x=clean_text(r.get('value'))
            return bool(x and not x.isdigit() and len(x)<80 and x.casefold() not in ('er','n/a'))
        selected_ev=next((r for r in E[field] if 'PDF18_SELECTOR' in r.get('method','') and good(r)),None)
        if not selected_ev:
            selected_ev=next((r for r in E[field] if ':NEXT_DATA' in r.get('method','') and good(r)),None)
        if not selected_ev:
            selected_ev=next((r for r in E[field] if ':LABEL_SIBLING' in r.get('method','') and good(r)),None)
        if selected_ev:
            selected[field]=clean_text(selected_ev['value'])
            origin[field]={'status':'OBSERVED','method':selected_ev['method'],'where':selected_ev.get('where')}
        elif selected[field] is not None and str(selected[field]).isdigit():
            selected[field]=None;origin[field]={'status':'MISSING_UNMAPPED_ID','method':None,'where':None}
    # Only exact historical CSS selectors for boolean furniture attributes.
    for field in ('furniture','conditioner','oven','sofa','washing_machine','table','refrigerator','bed','internet','natural_gas'):
        choices=[r for r in E[field] if ':PDF18_SELECTOR' in r.get('method','')]
        if choices:
            selected[field]=choices[0]['value']
            origin[field]={'status':'OBSERVED','method':choices[0]['method'],'where':choices[0].get('where')}
        elif selected[field] is not None and any(':VISIBLE_LABEL' in x.get('method','') for x in E[field]):
            # Ignore heuristic-only attributes; their values may be adjacent labels.
            selected[field]=None;origin[field]={'status':'MISSING_WEAK_EVIDENCE','method':None,'where':None}
    # Explicit null is UNKNOWN, not FALSE.
    for key in ('parking','oven','map_image_url'):
        if selected.get(key) is None and origin[key]['status']=='MISSING':origin[key]['status']='NOT_SPECIFIED'
    # Rich text may contain HTML and arbitrary contact details. Don't include it in the PDF.
    if selected.get('description'):
        selected['description']=clean_text(selected['description'])
        origin['description']['pdf_policy']='OMIT_FULL_TEXT_TO_AVOID_PERSONAL_CONTACT_DETAILS'
    return selected,origin


def safe_host(url,valid):
    u=urlparse(str(url));return u.scheme=='https' and u.hostname in valid and not u.username and not u.password


def download_image(session,url,dest,maxbytes=MAX_IMAGE):
    if not safe_host(url,CDN_HOSTS):raise ValueError('DISALLOWED_IMAGE_DOMAIN')
    with session.get(url,stream=True,timeout=(8,20),headers={'User-Agent':MAC_UA},allow_redirects=True) as r:
        if r.status_code in (403,429):raise PermissionError(f'IMAGE_ACCESS_DENIED_{r.status_code}')
        r.raise_for_status()
        if not safe_host(r.url,CDN_HOSTS):raise ValueError('IMAGE_REDIRECT_OTHER_DOMAIN')
        if not r.headers.get('Content-Type','').lower().startswith('image/'):
            raise ValueError('NOT_AN_IMAGE')
        data=bytearray()
        for chunk in r.iter_content(64*1024):
            data.extend(chunk)
            if len(data)>maxbytes:raise ValueError('IMAGE_TOO_LARGE')
    with Image.open(io.BytesIO(data)) as test:
        test.verify()
    dest.write_bytes(data)
    return {'file':dest.name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'source_url':url}


def draw_location_map(session,lat,lon,dest):
    """One user-requested static map, <= 9 OSM tiles. Respect denied access."""
    if not (-85<=lat<=85 and -180<=lon<=180):return {'status':'INVALID_COORDS'}
    zoom=14; world=2**zoom
    tx=(lon+180)/360*world
    latr=math.radians(lat)
    ty=(1-math.asinh(math.tan(latr))/math.pi)/2*world
    cw,ch=590,300
    left=tx*256-cw/2;top=ty*256-ch/2
    firstx=int(math.floor(left/256));lastx=int(math.floor((left+cw-1)/256))
    firsty=int(math.floor(top/256));lasty=int(math.floor((top+ch-1)/256))
    count=(lastx-firstx+1)*(lasty-firsty+1)
    if count>9:return {'status':'TOO_MANY_TILES'}
    canvas_img=Image.new('RGB',(cw,ch),'#e3e8e4')
    fetched=0
    for ix in range(firstx,lastx+1):
        for iy in range(firsty,lasty+1):
            url=f'https://tile.openstreetmap.org/{zoom}/{ix}/{iy}.png'
            with session.get(url,stream=True,timeout=(6,12),allow_redirects=False,
                headers={'User-Agent':'ListingLensPDFPrototype/2.0 (single-map local PDF; user requested; no bulk fetching)'}) as r:
                if r.status_code in (403,429):return {'status':f'MAP_ACCESS_DENIED_{r.status_code}','tiles':fetched}
                if r.status_code!=200 or not r.headers.get('Content-Type','').startswith('image/'):
                    return {'status':f'MAP_HTTP_{r.status_code}','tiles':fetched}
                raw=r.content
                if len(raw)>350000:return {'status':'TILE_TOO_LARGE','tiles':fetched}
                with Image.open(io.BytesIO(raw)) as im:tile=im.convert('RGB')
            canvas_img.paste(tile,(round(ix*256-left),round(iy*256-top)))
            fetched+=1
    pen=ImageDraw.Draw(canvas_img)
    cx,cy=cw//2,ch//2
    pen.ellipse((cx-14,cy-14,cx+14,cy+14),fill='#00ba78',outline='white',width=4)
    pen.ellipse((cx-4,cy-4,cx+4,cy+4),fill='white')
    canvas_img.save(dest,format='PNG')
    return {'status':'MAP_OK','tiles':fetched,'attribution':'© OpenStreetMap contributors','file':dest.name}


def register_fonts():
    fontfiles=[
      ('/Library/Fonts/Arial Unicode.ttf',None),
      ('/System/Library/Fonts/Supplemental/Arial Unicode.ttf',None),
      ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
    ]
    for normal,bold in fontfiles:
        if not Path(normal).is_file():continue
        try:
            pdfmetrics.registerFont(TTFont('PdfSans',normal))
            pdfmetrics.registerFont(TTFont('PdfSansBold',bold or normal))
            return 'PdfSans','PdfSansBold'
        except Exception:pass
    return 'Helvetica','Helvetica-Bold'


def text_fit(c,text,x,y,maxwidth,fontsize=10,font='Helvetica',minsize=7):
    text=str(text or '')
    size=fontsize
    while size>minsize and pdfmetrics.stringWidth(text,font,size)>maxwidth:size-=0.35
    if pdfmetrics.stringWidth(text,font,size)>maxwidth:
        while text and pdfmetrics.stringWidth(text+'…',font,size)>maxwidth:text=text[:-1]
        text+='…'
    c.setFont(font,size);c.drawString(x,y,text)


def rounded(c,x,y,w,h,r=7,fill='#f1f4f3',stroke=None):
    c.setFillColor(HexColor(fill))
    if stroke:c.setStrokeColor(HexColor(stroke))
    c.roundRect(x,y,w,h,r,fill=1,stroke=int(bool(stroke)))


def cover(c,path,x,y,w,h):
    with Image.open(path) as im:
        im=ImageOps.exif_transpose(im).convert('RGB')
        # crop to fit, no color manipulation
        scale=3  # Higher-resolution raster inside the same PDF layout (not 72 dpi).
        pw,ph=max(1,int(w*scale)),max(1,int(h*scale))
        ratio=max(pw/im.width,ph/im.height)
        new=(int(im.width*ratio+.5),int(im.height*ratio+.5))
        im=im.resize(new,Image.Resampling.LANCZOS)
        l=(im.width-pw)//2;t=(im.height-ph)//2
        im=im.crop((l,t,l+pw,t+ph))
        c.drawImage(ImageReader(im),x,y,width=w,height=h)


def render_pdf(out,fields,photos,map_file,origins,kind):
    W,H=A4
    regular,bold=register_fonts()
    c=canvas.Canvas(str(out),pagesize=A4,pageCompression=1)
    c.setTitle(f'MyHome listing {fields["listing_id"]} | PDF prototype')
    ink='#182825';green='#16a16c';muted='#72827d'
    c.setFillColor(HexColor('#ffffff'));c.rect(0,0,W,H,stroke=0,fill=1)
    hero_h=260
    hero_y=H-hero_h
    if photos:
        cover(c,photos[0],0,hero_y,W,hero_h)
    else:
        c.setFillColor(HexColor('#263a33'));c.rect(0,hero_y,W,hero_h,fill=1,stroke=0)
        c.setFillColor(HexColor('#b8c5c0'));c.setFont(regular,14)
        c.drawCentredString(W/2,hero_y+145,'PHOTO UNAVAILABLE IN OFFLINE PREVIEW')
    c.setFillColor(Color(0.06,0.12,0.11,alpha=.94));c.rect(0,hero_y, W,62,stroke=0,fill=1)
    c.setFillColor(HexColor('#ffffff'))
    text_fit(c,fields.get('title') or 'Apartment listing',33,hero_y+37,W-180,15,bold,9)
    c.setFillColor(HexColor('#c7e4d4'));text_fit(c,'MyHome ID '+str(fields['listing_id']),33,hero_y+17,185,9,regular)
    p=fields.get('price_usd');price=f'${p:,.0f}' if isinstance(p,(int,float)) else 'Price not verified'
    c.setFillColor(HexColor('#a9f4cb'));text_fit(c,price,W-165,hero_y+28,142,17,bold)
    # Core + map
    top=hero_y-9
    c.setFillColor(HexColor(ink));c.setFont(bold,10)
    c.drawString(32,top-12,'PROPERTY DETAILS')
    c.setFillColor(HexColor(muted))
    val=lambda key: 'Not specified' if fields.get(key) in (None,'') else str(fields[key])
    specs=[('Area',val('area_m2')+' m²'),('Rooms',val('rooms')),('Bedrooms',val('bedrooms')),
           ('Floor',f"{val('floor')} / {val('total_floors')}"),('Building',val('building_status')),
           ('Condition',val('condition'))]
    y=top-35
    for i,(label,v) in enumerate(specs):
        dy=y-i*23
        c.setFillColor(HexColor(muted));text_fit(c,label,33,dy,80,9,regular)
        c.setFillColor(HexColor(ink));text_fit(c,v,119,dy,168,9,bold)
    map_x=W-283;map_y=y-5*23-12;map_w=255;map_h=138
    rounded(c,map_x,map_y,map_w,map_h,fill='#edf2f0')
    if map_file and Path(map_file).exists():
        cover(c,map_file,map_x+2,map_y+2,map_w-4,map_h-4)
        c.setFont(regular,5.8);c.setFillColor(HexColor('#15271f'))
        c.drawString(map_x+6,map_y+5,'© OpenStreetMap contributors')
    else:
        c.setFillColor(HexColor(ink));c.setFont(bold,9)
        c.drawCentredString(map_x+map_w/2,map_y+80,'LOCATION')
        c.setFont(regular,8);c.drawCentredString(map_x+map_w/2,map_y+63,'Map tiles unavailable')
    lat,lon=fields.get('latitude'),fields.get('longitude')
    if lat is not None and lon is not None:
        c.setFillColor(HexColor(muted));c.setFont(regular,7)
        c.drawString(map_x+4,map_y-13,f'Coordinates: {lat}, {lon}')
        url=f'https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=16/{lat}/{lon}'
        c.linkURL(url,(map_x,map_y,map_x+map_w,map_y+map_h),relative=0)
    # Lower split: secondary characteristics on left, photos on right
    mid_y=top-185
    c.setFillColor(HexColor(ink));c.setFont(bold,10)
    c.drawString(33,mid_y,'FEATURES & AMENITIES')
    long=[('Project',val('project_type')),('Heating',val('heating')),('Hot water',val('hot_water')),
          ('Parking',val('parking')),('Internet',val('internet')),('Natural gas',val('natural_gas')),
          ('Furniture',val('furniture')),('Stove',val('stove')),('Conditioner',val('conditioner')),
          ('Oven',val('oven')),('Sofa',val('sofa')),('Washing machine',val('washing_machine')),
          ('Table',val('table')),('Refrigerator',val('refrigerator')),('Bed',val('bed'))]
    for i,(label,v) in enumerate(long):
        yy=mid_y-21-i*16
        c.setFillColor(HexColor(muted));text_fit(c,label,34,yy,115,8,regular)
        c.setFillColor(HexColor(ink));text_fit(c,v,146,yy,143,8,bold,7)
    c.setFillColor(HexColor(ink));c.setFont(bold,10)
    c.drawString(W-280,mid_y,'GALLERY')
    gx=W-280;gy=mid_y-89;gw=82;gh=76;gap=8
    for idx in range(6):
        xx=gx+(idx%3)*(gw+gap);yy=gy-(idx//3)*(gh+gap)
        if idx<len(photos):
            cover(c,photos[idx],xx,yy,gw,gh)
        else:
            rounded(c,xx,yy,gw,gh,4,fill='#e9eeee')
            c.setFillColor(HexColor('#899a94'));c.setFont(regular,7)
            c.drawCentredString(xx+gw/2,yy+gh/2,'No photo')
    # Footer description deliberately constructed only from verified fields.
    city=val('city');urb=val('microdistrict')
    c.setFillColor(HexColor(green));c.rect(0,0,W,5,fill=1,stroke=0)
    rounded(c,28,30,W-56,67,5,fill='#f0f6f3')
    c.setFillColor(HexColor(ink));c.setFont(bold,9)
    c.drawString(39,77,'SUMMARY')
    a=fields.get('area_m2');b=fields.get('bedrooms')
    summary=f"{val('rooms')}-room apartment, {a:g} m²" if isinstance(a,(int,float)) else f"{val('rooms')}-room apartment"
    if b is not None:summary+=f', {b} bedroom(s)'
    summary+=f' in {urb}, {city}. Status: {val("building_status")}.'
    text_fit(c,summary,39,61,W-85,8.5,regular)
    text_fit(c,'Source: MyHome listing '+str(fields['listing_id'])+' | '+('Offline layout preview' if kind=='preview' else 'Generated from extracted listing data'),39,44,W-85,7,regular)
    c.setFillColor(HexColor(muted));c.setFont(regular,6.5)
    c.drawString(32,18,'No contact details, fabricated amenities, or unverified property claims are included.')
    c.save()
    return out


def try_live_html(listing_id,dest):
    from bs4 import BeautifulSoup
    # Reuse the same local parser as R1; no protected API endpoint.
    import probe_r1
    url=SOURCE_URL.format(id=listing_id)
    ses=requests.Session()
    with ses.get(url,timeout=(8,22),stream=True,headers={'User-Agent':MAC_UA}) as resp:
        status=resp.status_code
        if status in (403,429):raise PermissionError(f'HTML_ACCESS_DENIED_{status}')
        resp.raise_for_status()
        raw=resp.content
        if len(raw)>6_000_000:raise ValueError('HTML_TOO_LARGE')
        if urlparse(resp.url).hostname not in ('www.myhome.ge','myhome.ge'):raise ValueError('UNEXPECTED_HTML_HOST')
    page=raw.decode('utf-8','replace')
    soup=BeautifulSoup(page,'html.parser')
    scripted=' '.join((x.string or x.get_text() or '')[:2000000] for x in soup.select('script#__NEXT_DATA__'))
    if str(listing_id) not in scripted:raise ValueError('LISTING_ID_NOT_IN_NEXT_DATA')
    ev=probe_r1.Evidence()
    probe_r1.html_extract(page,ev,str(listing_id),'HTTP_HTML',dest)
    fields,summary=probe_r1.build_results(ev)
    return {'listing_id':str(listing_id),'url':resp.url,'fields':fields},ev.rows,{'html_status':status,'html_bytes':len(raw)}


def selftest(fixture):
    extracted,ev,*_=load_inputs(fixture=fixture)
    fields,prov=select_fields(extracted,ev)
    assert fields['price_usd']==1000,(fields['price_usd'],prov['price_usd'])
    assert fields['project_type']=='Non-standard',fields['project_type']
    assert fields['condition'].casefold()=='newly renovated'
    assert len(fields['photo_urls'])==6
    assert fields['parking'] is None and fields['oven'] is None
    assert fields['rooms']==4 and fields['bedrooms']==3
    assert money_meta('4 room, 1000 $, 110 m², 26387744 | Myhome','26387744')==1000
    assert money_meta('4 room, 1000 GEL, 26387744 | Myhome','26387744') is None
    assert money_meta('4 room, 1000 $, 26387745 | Myhome','26387744') is None
    assert not safe_host('http://static-api-statements.tnet.ge/x.webp',CDN_HOSTS)
    assert not safe_host('https://evil.tnet.ge/x.webp',CDN_HOSTS)
    for f in ('sofa','table','refrigerator','washing_machine','conditioner'):
        assert prov[f]['method'].endswith('PDF18_SELECTOR'),f
    print('OFFLINE_SELFTEST=PASS; NETWORK_REQUESTS=0; PRICE=1000; PROJECT=Non-standard; PHOTOS=6')


def main():
    root=Path(__file__).resolve().parent
    pa=argparse.ArgumentParser(description='One-listing MyHome data-to-PDF proof; no ListingLens changes')
    pa.add_argument('--id',default='26387744')
    pa.add_argument('--return-zip',type=Path,help='R1 RETURN.zip (preferred evidence source)')
    pa.add_argument('--live',action='store_true',help='One fresh HTML fetch; no auth API or Selenium')
    pa.add_argument('--no-media',action='store_true',help='No photo or map requests, produces layout preview')
    pa.add_argument('--no-map',action='store_true',help='Skip OSM tiles')
    pa.add_argument('--self-test',action='store_true')
    pa.add_argument('--output',type=Path)
    args=pa.parse_args()
    fixture=root/'fixture'/'R1_SAFE_SNAPSHOT.json'
    if args.self_test:
        if not fixture.exists():
            print('PDF_SCRIPT_SELFTEST=SKIPPED_NO_FIXTURE');return
        selftest(fixture);return
    if not re.fullmatch(r'[1-9]\d{5,11}',args.id):pa.error('invalid listing ID')
    out=args.output or Path.home()/'Downloads'/f'MYHOME_PDF_R2_{args.id}_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    out.mkdir(parents=True,exist_ok=False)
    warnings=[];live_meta=None
    if not args.live and not args.return_zip:
        raise ValueError('Explicit --live or --return-zip is required; no implicit sample data')
    if args.live:
        extracted,ev,live_meta=try_live_html(args.id,out)
        source_kind,source_path='LIVE_HTML_1_REQUEST',str(extracted.get('url'))
    else:
        extracted,ev,source_kind,source_path=load_inputs(archive=args.return_zip,fixture=fixture)
    if extracted['listing_id']!=args.id:raise ValueError('listing ID mismatch')
    selected,prov=select_fields(extracted,ev)
    selected['listing_id']=args.id
    selected['source_listing_url']=f'https://www.myhome.ge/en/pr/{args.id}/details/'
    selected['extracted_at_utc']=utc()
    selected['input_source']=source_kind
    selected['source_observation']='2026-10-20T04:08:41Z' if source_kind=='BUNDLED_R1_SNAPSHOT' else 'See R1 report or live HTML fetch time'
    media=[];map_path=None;photo_log=[];map_log={'status':'SKIPPED'}
    session=requests.Session()
    if not args.no_media:
        dest=out/'photos';dest.mkdir(exist_ok=True)
        for i,url in enumerate((selected.get('photo_urls') or [])[:6],1):
            p=dest/f'{i:02d}.webp'
            try:
                rec=download_image(session,url,p)
                media.append(p)
                photo_log.append({'position':i,'status':'OK',**rec})
                print(f'PHOTO_{i}=OK',flush=True)
            except PermissionError as ex:
                photo_log.append({'position':i,'status':str(ex)})
                warnings.append(str(ex)+'; stop further photos')
                break
            except Exception as ex:
                photo_log.append({'position':i,'status':'ERROR','error':type(ex).__name__+': '+str(ex)[:120]})
                warnings.append(f'photo_{i}: {str(ex)[:100]}')
        if not args.no_map and isinstance(selected.get('latitude'),(float,int)) and isinstance(selected.get('longitude'),(float,int)):
            try:
                dest=out/'map.png'
                map_log=draw_location_map(session,float(selected['latitude']),float(selected['longitude']),dest)
                if map_log['status']=='MAP_OK':map_path=dest
                else:warnings.append(map_log['status'])
            except Exception as ex:
                map_log={'status':'MAP_ERROR','detail':str(ex)[:160]};warnings.append('map: '+str(ex)[:100])
    else:warnings.append('Offline preview: no photographs or actual street map embedded')
    if selected.get('parking') is None:warnings.append('Parking not specified in source')
    if selected.get('oven') is None:warnings.append('Oven not specified in source')
    if selected.get('price_usd') is None:warnings.append('USD price unverified (will not guess)')
    if len(media)<len((selected.get('photo_urls') or [])[:6]) and not args.no_media:warnings.append('Not all listing photographs downloaded')
    full_pdf=out/f'MYHOME_{args.id}_{"OFFLINE_PREVIEW" if args.no_media else "PDF"}.pdf'
    render_pdf(full_pdf,selected,media,map_path,prov,'preview' if args.no_media else 'full')
    public_fields=dict(selected)
    # Keep field sources, do not expose source description or contacts beyond local result.
    public_fields['description']='[WITHHELD_FROM_PDF_AND_RETURN_ARCHIVE]'
    (out/'DATA_WITH_SOURCES.json').write_text(json.dumps({'fields':public_fields,'provenance':prov,'warnings':warnings,
        'photo_downloads':photo_log,'map':map_log,'live_request':live_meta,'source_kind':source_kind},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    report=['MYHOME PDF DATA R2',f'LISTING_ID={args.id}',f'SOURCE={source_kind}',f'INPUT={source_path}',
     f'USD_PRICE={selected.get("price_usd")}',f'PROJECT_TYPE={selected.get("project_type")}',
     f'PHOTOS={len(media)}/{min(len(selected.get("photo_urls") or []),6)}',f'MAP={map_log["status"]}',
     f'OUTPUT_PDF={full_pdf.name}', 'MISSING_FIELDS='+', '.join(k for k in FIELDS if selected.get(k) is None),'',
     'WARNINGS:']+['- '+w for w in warnings]
    (out/'RESULTS.txt').write_text('\n'.join(report)+'\n',encoding='utf-8')
    ret=out.parent/(out.name+'_RETURN.zip')
    with zipfile.ZipFile(ret,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for x in (full_pdf,out/'DATA_WITH_SOURCES.json',out/'RESULTS.txt'):z.write(x,x.name)
        # small thumbnail optional, no raw HTML, personal descriptions or contacts
    print('\n'.join(report),flush=True)
    print('RETURN_ZIP='+str(ret),flush=True)
    print('DONE=PASS' if len(media)>=5 and selected['price_usd'] is not None else 'DONE=PARTIAL_CHECK_RESULTS',flush=True)

if __name__=='__main__':
    try:main()
    except Exception as ex:
        print(f'ERROR={type(ex).__name__}: {ex}',file=sys.stderr);sys.exit(2)
