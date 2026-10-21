#!/usr/bin/env python3
"""Read-only, one-ID MyHome diagnostic. No external repo code is executed.
Mac Python >=3.9. Dependencies: requests, beautifulsoup4; optional selenium.
Produces comparative field/method evidence. Not a PDF generator.
"""
import argparse
import csv
import html as htmllib
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print('Missing requests / beautifulsoup4. Run RUN_MAC.sh', file=sys.stderr)
    sys.exit(2)

VERSION='R1'
FIELDS = [
    'title','price_usd','address','city','district','microdistrict','area_m2',
    'rooms','bedrooms','floor','total_floors','building_status','condition',
    'project_type','heating','hot_water','parking','internet','natural_gas',
    'furniture','stove','conditioner','oven','sofa','washing_machine','table',
    'refrigerator','bed','photo_urls','map_image_url','latitude','longitude',
    'description','owner_type'
]
ALIASES={
'title':['dynamic_title','user_title','title','headline'],
'price_usd':['price_usd','usd_price','price_in_usd','price'],
'address':['address','street_address','street','location_text'],
'city':['city_name','city'], 'district':['district_name','district'],
'microdistrict':['urban_name','urban','microdistrict'],
'area_m2':['area','apartment_size','square_meter','square_meters','total_area','area_m2','square'],
'rooms':['room','rooms','room_count','number_of_rooms'],
'bedrooms':['bedroom','bedrooms','bedroom_count','number_of_bedrooms'],
'floor':['floor','floor_number'], 'total_floors':['total_floors','floors_total','floor_count','total_floor'],
'building_status':['status_name','building_status','status','status_id'],
'condition':['condition_name','condition','condition_id'],
'project_type':['project_type_name','project_type','project_type_id'],
'heating':['heating_name','heating','heating_type'],
'hot_water':['hot_water_name','hot_water','hot_water_type'],
'parking':['parking_name','parking','parking_type'],
'internet':['internet','has_internet'],
'natural_gas':['natural_gas','has_natural_gas','gas'],
'furniture':['furniture','has_furniture','furnished'],
'stove':['stove','stove_type'], 'conditioner':['conditioner','air_conditioning','air_conditioner'],
'oven':['oven'], 'sofa':['sofa'], 'washing_machine':['washing_machine','washing machine'],
'table':['table'], 'refrigerator':['refrigerator','fridge'], 'bed':['bed'],
'photo_urls':['images','photos','photo_urls','pictures','gallery'],
'map_image_url':['map_image_url','static_map','map_image'],
'latitude':['lat','latitude'], 'longitude':['lng','lon','longitude'],
'description':['comment','description','text','user_comment'],
'owner_type':['owner_type','user_type'],
}
LABELS={
'area_m2':['Area','Apartment size','Size','ფართობი','Площадь'],
'rooms':['Rooms','Room','ოთახები','Комнаты'],
'bedrooms':['Bedrooms','Bedroom','საძინებელი','Спальни'],
'floor':['Floor','სართული','Этаж'],
'building_status':['Status','Building status','სტატუსი','Статус'],
'condition':['Condition','მდგომარეობა','Состояние'],
'project_type':['Type of project','Project type','პროექტის ტიპი'],
'heating':['Heating','გათბობა','Отопление'],
'hot_water':['Hot water','ცხელი წყალი','Горячая вода'],
'parking':['Parking','პარკინგი','Парковка'],
'internet':['Internet','ინტერნეტი','Интернет'],
'natural_gas':['Natural gas','ბუნებრივი აირი','Природный газ'],
'furniture':['Furniture','ავეჯი','Мебель'],
'stove':['Stove','ქურა','Плита'],
'conditioner':['Conditioner','Air conditioner','კონდიციონერი','Кондиционер'],
'oven':['Oven','ღუმელი','Духовка'],
'sofa':['Sofa','დივანი','Диван'],
'washing_machine':['Washing machine','სარეცხი მანქანა','Стиральная машина'],
'table':['Table','მაგიდა','Стол'],
'refrigerator':['Refrigerator','Fridge','მაცივარი','Холодильник'],
'bed':['Bed','საწოლი','Кровать']
}
STATUS={1:'Old building',2:'New building',3:'Under construction'}
# No claim that other integer IDs are human-readable values.
MAX_HTML=6_000_000
MAX_API=5_000_000
MAX_MEDIA=5_000_000


def normalize_value(field,value):
    if value is None or value=='':return None
    if isinstance(value,bool):return 'Yes' if value else 'No'
    if field=='photo_urls':
        if not isinstance(value,list):value=[value]
        out=[]
        for item in value:
            if isinstance(item,dict):
                item=next((item.get(k) for k in ('large','url','original','src','medium','thumb','thumbnail') if item.get(k)),None)
            if isinstance(item,str) and item.startswith(('https://','http://')) and item not in out:
                out.append(item)
        return out[:80] or None
    if field=='building_status':
        if isinstance(value,dict): value=value.get('name') or value.get('id') or value.get('value')
        if str(value).isdigit():return STATUS.get(int(value),f'Unmapped status ID {value}')
    if field=='price_usd':
        if isinstance(value,dict):
            for k in ('2','usd','USD','usd_price','price_usd','amount_usd'):
                if value.get(k) is not None:return normalize_value('price_usd',value[k])
            return None # GEL-only or ambiguous currency must NOT be reported as USD
        if isinstance(value,(str,int,float)):
            x=str(value).strip().replace('USD','').replace('usd','').strip()
            if not re.fullmatch(r'[\d\s,.$]+',x):return None
            try:
                n=float(re.sub('[^0-9.]','',x.replace(',','')))
                return round(n,2) if 0<n<1e10 else None
            except ValueError:return None
    if field in ('rooms','bedrooms','floor','total_floors','area_m2','latitude','longitude'):
        if not isinstance(value,(str,int,float)) or isinstance(value,bool):return None
        s=str(value).strip().replace(',','.')
        if field=='floor' and '/' in s:s=s.split('/')[0].strip()
        m=re.search(r'(?<!\d)-?\d+(?:\.\d+)?',s)
        if not m:return None
        n=float(m.group())
        if field in ('rooms','bedrooms','floor','total_floors'):
            return int(n) if n.is_integer() and 0<=n<=150 else None
        if field=='area_m2':return n if 1<=n<=50000 else None
        if field=='latitude':return n if -90<=n<=90 else None
        if field=='longitude':return n if -180<=n<=180 else None
    if field=='owner_type' and isinstance(value,dict):
        value=value.get('type') or value.get('name')
    if isinstance(value,(dict,list)):return None
    v=' '.join(str(value).split()).strip()
    if not v or len(v)>6000:return None
    if field in ('title','address','city','district','microdistrict','condition','project_type','heating','hot_water','parking','description','building_status') and v.lower() in ('n/a','none','null','undefined','-'):
        return None
    if field=='map_image_url' and not v.startswith(('http://','https://')):return None
    return v

class Evidence:
    def __init__(self):self.rows=[]
    def add(self,field,method,val,where='',confidence='DIRECT'):
        if field not in FIELDS:return
        val=normalize_value(field,val)
        if val is None:return
        # prohibit random list of strings being reported as photo URLs when invalid
        self.rows.append({'field':field,'method':method,'value':val,'where':where[:260],'confidence':confidence})
    def group(self):
        d=defaultdict(list)
        for r in self.rows:d[r['field']].append(r)
        return d


def iter_objects(node,limit=25000):
    todo=[('$',node)]; count=0
    while todo and count<limit:
        path,ob=todo.pop();count+=1
        if isinstance(ob,dict):
            yield path,ob
            for k,v in ob.items():
                if isinstance(v,(dict,list)):todo.append((path+'.'+str(k),v))
        elif isinstance(ob,list):
            for i,v in enumerate(ob[:1500]):
                if isinstance(v,(dict,list)):todo.append((path+f'[{i}]',v))

def objs_for_id(data,listing_id):
    # IDs can be int or str, multiple alias names exist. Avoid unrelated ads.
    found=[]
    for path,ob in iter_objects(data):
        for k in ('id','statement_id','statementId','raw_id'):
            if k in ob and str(ob[k])==str(listing_id):
                found.append((path,ob));break
    # If the API returns the detail card with no ID, allow root data as one fallback.
    if not found and isinstance(data,dict):
        ob=data.get('data',data)
        if isinstance(ob,dict) and any(x in ob for x in ('area','price','images','room')):
            found=[('$.data',ob)]
    return found[:20]

def api_or_embedded(data,ev,method,listing_id):
    if not isinstance(data,(dict,list)):return
    found=objs_for_id(data,listing_id)
    for path,ob in found:
        # object itself + nested single-value objects only (avoid unrelated listings)
        context=[(path,ob)]
        for k,v in ob.items():
            if k in ('property','statement','details','location','parameters','attributes','features','price','user','user_type','address') and isinstance(v,dict):context.append((path+'.'+k,v))
        for xp,ctx in context:
            low={str(k).lower():v for k,v in ctx.items()}
            for field,keys in ALIASES.items():
                for key in keys:
                    if key.lower() in low:
                        v=low[key.lower()]
                        if field=='price_usd' and key.lower()=='price' and not isinstance(v,dict):
                            # Plain numeric API price is ambiguous unless USD flag/price_currency_id is known.
                            c=ob.get('currency_id',ob.get('price_currency_id'))
                            if str(c) not in ('2','USD','usd'):continue
                        if field=='photo_urls' and not isinstance(v,list):continue
                        ev.add(field,method,v,xp+'.'+key)
        # flexible multilingual attribute/parameter labels
        for k in ('parameters','attributes','features','statement_parameters','amenities'):
            vals=ob.get(k)
            if isinstance(vals,list):
                for i,item in enumerate(vals[:250]):
                    if not isinstance(item,dict):continue
                    label=item.get('name') or item.get('title') or item.get('label') or item.get('parameter_name')
                    if isinstance(label,dict):label=label.get('en') or label.get('ka') or label.get('ru')
                    if not isinstance(label,str):continue
                    val=item.get('value_name',item.get('value',item.get('selected',item.get('name'))))
                    if isinstance(val,dict):val=val.get('en') or val.get('ka') or val.get('ru') or val.get('name')
                    for field,labels in LABELS.items():
                        if any(label.casefold()==s.casefold() for s in labels):
                            ev.add(field,method+':parameter',val,path+f'.{k}[{i}]')


def extract_json_scripts(soup,ev,listing_id,tag):
    total=0
    for script in soup.select('script'):
        t=script.get('type',''); raw=(script.string or script.get_text() or '').strip()
        if not raw or len(raw)>MAX_API:continue
        name=script.get('id','')
        if name=='__NEXT_DATA__' or t=='application/ld+json' or (t in ('application/json','application/json+ld') and raw.startswith(('{','['))):
            try:obj=json.loads(raw)
            except (ValueError,TypeError):continue
            total+=1
            if t=='application/ld+json':
                items=obj if isinstance(obj,list) else [obj]
                for j,item in enumerate(items):
                    if not isinstance(item,dict):continue
                    for field,keys in {'title':['name','headline'],'description':['description'],'address':['address'],'photo_urls':['image','photos'],'price_usd':['price']}.items():
                        for key in keys:
                            v=item.get(key)
                            if field=='address' and isinstance(v,dict):v=', '.join(str(v.get(k)) for k in ('streetAddress','addressLocality','addressRegion') if v.get(k))
                            if field=='price_usd':
                                offer=item.get('offers')
                                if isinstance(offer,dict) and str(offer.get('priceCurrency','')).upper()=='USD':v=offer.get('price')
                                else:v=None
                            if field=='photo_urls' and isinstance(v,str):v=[v]
                            ev.add(field,tag+':JSON_LD',v,f'script[{j}].{key}')
            else:api_or_embedded(obj,ev,tag+':'+('NEXT_DATA' if name=='__NEXT_DATA__' else 'INLINE_JSON'),listing_id)
    return total


def html_extract(page,ev,listing_id,tag,output_dir):
    soup=BeautifulSoup(page,'html.parser')
    scripts=extract_json_scripts(soup,ev,listing_id,tag)
    title=soup.find('h1')
    if title:ev.add('title',tag+':H1',title.get_text(' ',strip=True),'h1')
    for key in ('og:title','twitter:title'):
        meta=soup.find('meta',attrs={'property':key}) or soup.find('meta',attrs={'name':key})
        if meta:ev.add('title',tag+':META',meta.get('content'),key)
    for key in ('description','og:description'):
        meta=soup.find('meta',attrs={'name':key}) or soup.find('meta',attrs={'property':key})
        if meta:ev.add('description',tag+':META',meta.get('content'),key)
    # Price text in old PDF's sticky block, only with explicit currency symbol.
    for price_sel in ('div.sticky .flex > .flex > span','[data-testid="listing-price"]'):
        pe=soup.select_one(price_sel)
        if pe:
            ps=pe.get_text(' ',strip=True)
            if '$' in ps or 'USD' in ps.upper():ev.add('price_usd',tag+':PDF18_PRICE',ps,price_sel)
    og=soup.find('meta',attrs={'property':'og:image'})
    if og:ev.add('photo_urls',tag+':META',[og.get('content')],'og:image')
    # Historical selectors from pdf-18.py (tested here, not assumed valid).
    address=soup.select_one('.px-4 div.flex.flex-col.items-start>div>svg+span')
    if address:ev.add('address',tag+':PDF18_SELECTOR',address.get_text(' ',strip=True),'address selector')
    features=soup.select_one('.col-span-12 .hidden .items-center.flex-wrap.border')
    if features:
        spans=features.find_all('span')
        for i,span in enumerate(spans):
            text=span.get_text(' ',strip=True)
            if 'm²' in text:ev.add('area_m2',tag+':PDF18_SELECTOR',text,'main features')
            if i+1<len(spans):
                for f,labels in [('rooms',['Rooms']),('bedrooms',['Bedroom','Bedrooms']),('floor',['Floor'])]:
                    if text in labels:ev.add(f,tag+':PDF18_SELECTOR',spans[i+1].get_text(' ',strip=True),'main feature spans')
    for selector,group in [('.grid-cols-2.overflow-hidden .flex.flex-col','FURNITURE'),('.grid-cols-1.overflow-hidden .flex.flex-col','AMENITIES')]:
        for item in soup.select(selector)[:100]:
            lab=item.select_one('span')
            if not lab:continue
            label=lab.get_text(' ',strip=True)
            value_container=item.select_one('div.hidden.text-xs')
            val=value_container.get_text(' ',strip=True) if value_container else ('Yes' if group=='FURNITURE' else None)
            for f,labels in LABELS.items():
                if label.casefold() in [l.casefold() for l in labels]:ev.add(f,tag+':PDF18_SELECTOR',val,selector)
    for selector in ('img[alt="Google Map"]','img[alt*="Map"]'):
        img=soup.select_one(selector)
        if img:ev.add('map_image_url',tag+':MAP_IMG',urljoin('https://www.myhome.ge',img.get('src') or ''),selector)
    gallery=soup.select_one('.bottom-6 .swiper-wrapper')
    if gallery:
        urls=[urljoin('https://www.myhome.ge',x.get('data-src') or x.get('src') or '') for x in gallery.find_all('img')]
        ev.add('photo_urls',tag+':PDF18_GALLERY',urls,'swiper-wrapper img')
    # Generic DOM label: immediate sibling and value from parent; useful when CSS classes change.
    for element in soup.select('dt,th,label,span,div')[:13000]:
        if element.find(['div','span','table','section','article']):continue
        label=element.get_text(' ',strip=True)
        if not label or len(label)>50:continue
        for f,labels in LABELS.items():
            if label.casefold().rstrip(':') not in [x.casefold() for x in labels]:continue
            if element.parent:
                siblings=list(element.parent.find_all(recursive=False))
                for sib in siblings[:8]:
                    if sib is element:continue
                    val=sib.get_text(' ',strip=True)
                    if val and val!=label and len(val)<100:
                        ev.add(f,tag+':LABEL_SIBLING',val,label);break
    # Regex fallback from VISIBLE text, only bounded local patterns (not arbitrary page source).
    for t in soup(['script','style','noscript','svg']):t.decompose()
    text='\n'.join(soup.stripped_strings)
    # PDF export does not persist page text, which can contain seller contact details.
    for f,labels in LABELS.items():
        for lab in labels:
            matches=re.findall(r'(?im)(?:^|\n)\s*'+re.escape(lab)+r'\s*[:：]?\s*(?:\n)?\s*([^\n]{1,70})',text)
            for v in matches[:2]:
                if v.casefold() not in [x.casefold() for x in labels]:
                    ev.add(f,tag+':VISIBLE_LABEL',v,lab,'HEURISTIC')
    # Embedded scripts photos only from script JSON extraction; do not scrape all image icons.
    return scripts


def limited_get(session,url,max_bytes,headers):
    result={'url':url,'status':None,'error':None,'bytes':0,'content_type':None}
    try:
        with session.get(url,headers=headers,timeout=(10,22),stream=True,allow_redirects=True) as response:
            result['status']=response.status_code;result['url']=response.url
            result['content_type']=response.headers.get('Content-Type','')
            if response.status_code in (403,429):
                result['error']='STOP_ACCESS_DENIED';return result,None,True
            if response.status_code!=200:
                result['error']='HTTP_'+str(response.status_code);return result,None,False
            chunks=[];n=0
            for c in response.iter_content(65536):
                if not c:continue
                n+=len(c)
                if n>max_bytes:
                    result['error']='MAX_RESPONSE_BYTES';return result,None,False
                chunks.append(c)
            raw=b''.join(chunks);result['bytes']=len(raw)
            return result,raw,False
    except requests.RequestException as e:
        result['error']=type(e).__name__+': '+str(e)[:160]
        return result,None,False


def selenium_html(url,out,show_browser=False):
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:return None,'SELENIUM_NOT_INSTALLED'
    options=Options()
    if not show_browser:options.add_argument('--headless=new')
    options.add_argument('--window-size=1365,900')
    options.add_argument('--no-first-run')
    options.add_argument('--disable-extensions')
    driver=None
    try:
        driver=webdriver.Chrome(options=options)
        driver.set_page_load_timeout(40)
        driver.get(url)
        time.sleep(2.5)
        try:driver.save_screenshot(str(out/'BROWSER_SCREENSHOT.png'))
        except Exception:pass
        return driver.page_source[:MAX_HTML],'PASS'
    except Exception as e:return None,type(e).__name__+': '+str(e)[:400]
    finally:
        if driver:
            try:driver.quit()
            except Exception:pass


def slug(v,maxchars=140):
    v=json.dumps(v,ensure_ascii=False,sort_keys=True) if isinstance(v,(dict,list)) else str(v)
    return v[:maxchars]

def build_results(ev):
    result={};details=[]; groups=ev.group()
    # Prioritize direct structured API, then embedded JSON, DOM historical, and heuristics.
    for f in FIELDS:
        candidates=groups.get(f,[])
        def priority(r):
            m=r['method'];return (0 if m.startswith('API:') else 1 if ':NEXT_DATA' in m or ':INLINE_JSON' in m else 2 if ':JSON_LD' in m else 3 if ':PDF18_' in m else 4 if ':H1' in m or ':META' in m else 5 if ':LABEL_' in m else 6,r['confidence']!='DIRECT')
        candidates=sorted(candidates,key=priority)
        if candidates:
            result[f]=candidates[0]['value']
            first=slug(candidates[0]['value'])
            different=[r for r in candidates[1:] if slug(r['value'])!=first]
            state='CONFLICT_REVIEW' if different else 'FOUND'
            details.append((f,state,candidates[0]['method'],slug(candidates[0]['value'],120),len(candidates)))
        else:
            result[f]=None;details.append((f,'MISSING','','',0))
    # Do not silently generate floor count from floor, etc.
    return result,details

def safe_json_dump(p,obj):p.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')

def image_download(session,urls,out,limit):
    downloaded=[];errors=[];halt=False
    d=out/'PHOTOS';d.mkdir(exist_ok=True)
    for i,url in enumerate((urls or [])[:limit],1):
        host=urlparse(url).hostname or ''
        if urlparse(url).scheme!='https' or host in ('localhost','127.0.0.1','0.0.0.0') or re.match(r'^\d+\.\d+\.\d+\.\d+$',host) or not host:
            errors.append({'photo':i,'error':'UNSAFE_OR_UNSUPPORTED_URL'});continue
        status,raw,blocked=limited_get(session,url,MAX_MEDIA,{'User-Agent':'ListingLens-Diagnostic/1.0','Accept':'image/avif,image/webp,image/png,image/jpeg,*/*'})
        if blocked:errors.append({'photo':i,'error':'ACCESS_DENIED_STOP'});halt=True;break
        if raw and (raw.startswith(b'\xff\xd8\xff') or raw.startswith(b'\x89PNG') or raw.startswith(b'RIFF') or raw.startswith(b'\x00\x00\x00') ):
            ct=status['content_type'] or ''
            ext='.jpg' if raw.startswith(b'\xff\xd8') else '.png' if raw.startswith(b'\x89PNG') else '.webp' if raw.startswith(b'RIFF') else '.img'
            p=d/f'{i:02}{ext}';p.write_bytes(raw)
            downloaded.append({'photo':i,'path':str(p.name),'bytes':len(raw),'url':url})
        else: errors.append({'photo':i,'status':status})
    return {'files':downloaded,'errors':errors,'stopped':halt}

def run(args):
    listing_id=str(args.id)
    if not re.fullmatch(r'[1-9][0-9]{5,10}',listing_id):raise ValueError('ID must be 6-11 digits')
    out=Path(args.output).expanduser() if args.output else Path.home()/'Downloads'/('MYHOME_PDF_INPUT_PROBE_'+listing_id+'_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
    out.mkdir(parents=True,exist_ok=False)
    ev=Evidence();session=requests.Session()
    fetch_log=[];halt=False
    print('ID=',listing_id,' OUTPUT=',out,sep='',flush=True)
    # First: attempt the structured endpoint; the HTML response remains an independent fallback.
    api_url='https://api-statements.tnet.ge/v1/statements/'+listing_id
    for name,url,headers,limit in [
        ('API',api_url,{'X-Website-Key':'myhome','locale':'en','Accept':'application/json','User-Agent':'ListingLens-PDF-Input-Probe/1.0'},MAX_API),
        ('HTML','https://www.myhome.ge/en/pr/'+listing_id+'/details/',{'Accept-Language':'en-US,en;q=0.8','User-Agent':'ListingLens-PDF-Input-Probe/1.0'},MAX_HTML),
    ]:
        if halt:break
        print('FETCH',name,url,flush=True)
        info,raw,blocked=limited_get(session,url,limit,headers)
        fetch_log.append({'method':name,**info})
        if blocked:
            halt=True;print('ACCESS_DENIED: no more requests',flush=True);break
        if raw is None:continue
        if name=='API':
            try:
                data=json.loads(raw)
                safe_json_dump(out/'API_RESPONSE.json',data)
                api_or_embedded(data,ev,'API:DETAIL',listing_id)
            except ValueError:
                info['parse_error']='JSON_PARSE_ERROR'
        else:
            decoded=raw.decode('utf-8','replace')
            (out/'RAW_HTML.html').write_text(decoded,encoding='utf-8')
            html_extract(decoded,ev,listing_id,'HTTP_HTML',out)
        time.sleep(.9)
    if not halt and not args.no_browser:
        print('FETCH BROWSER Selenium DOM',flush=True)
        page,status=selenium_html('https://www.myhome.ge/en/pr/'+listing_id+'/details/',out,args.show_browser)
        fetch_log.append({'method':'BROWSER','status':status})
        if page:
            (out/'BROWSER_HTML.html').write_text(page,encoding='utf-8')
            html_extract(page,ev,listing_id,'BROWSER_DOM',out)
    chosen,summary=build_results(ev)
    safe_json_dump(out/'EXTRACTED.json',{'listing_id':listing_id,'url':'https://www.myhome.ge/en/pr/'+listing_id+'/details/','fields':chosen})
    safe_json_dump(out/'EVIDENCE.json',ev.rows)
    safe_json_dump(out/'REQUEST_LOG.json',fetch_log)
    # Every field x technique combination gets a row, including missing outcomes.
    categories=[
        ('API_DETAIL',lambda m:m.startswith('API:')),
        ('SERVER_NEXT_DATA',lambda m:m.startswith('HTTP_HTML:NEXT_DATA')),
        ('SERVER_INLINE_JSON',lambda m:m.startswith('HTTP_HTML:INLINE_JSON')),
        ('SERVER_JSON_LD',lambda m:m.startswith('HTTP_HTML:JSON_LD')),
        ('SERVER_HISTORICAL_PDF18',lambda m:m.startswith('HTTP_HTML:PDF18_')),
        ('SERVER_META_H1',lambda m: m.startswith('HTTP_HTML:H1') or m.startswith('HTTP_HTML:META')),
        ('SERVER_DOM_LABELS',lambda m:m.startswith('HTTP_HTML:LABEL_') or m.startswith('HTTP_HTML:VISIBLE_')),
        ('BROWSER_NEXT_DATA',lambda m:m.startswith('BROWSER_DOM:NEXT_DATA')),
        ('BROWSER_INLINE_JSON',lambda m:m.startswith('BROWSER_DOM:INLINE_JSON')),
        ('BROWSER_JSON_LD',lambda m:m.startswith('BROWSER_DOM:JSON_LD')),
        ('BROWSER_HISTORICAL_PDF18',lambda m:m.startswith('BROWSER_DOM:PDF18_')),
        ('BROWSER_META_H1',lambda m:m.startswith('BROWSER_DOM:H1') or m.startswith('BROWSER_DOM:META')),
        ('BROWSER_DOM_LABELS',lambda m:m.startswith('BROWSER_DOM:LABEL_') or m.startswith('BROWSER_DOM:VISIBLE_')),
    ]
    method_rows=[]
    for f in FIELDS:
        for name,check in categories:
            matches=[r for r in ev.group().get(f,[]) if check(r['method'])]
            source='API' if name=='API_DETAIL' else ('BROWSER' if name.startswith('BROWSER') else 'HTML')
            requested=any(x['method']==source for x in fetch_log)
            successful=any(x['method']==source and (x.get('status')==200 or x.get('status')=='PASS') for x in fetch_log)
            state='FOUND' if matches else ('NOT_ATTEMPTED' if not requested else 'SOURCE_UNAVAILABLE' if not successful else 'NOT_FOUND')
            method_rows.append({'field':f,'method':name,'status':state,'evidence_count':len(matches),
                'values':' | '.join(slug(r['value'],110) for r in matches[:3]),
                'where':' | '.join(r['where'] for r in matches[:3])})
    with (out/'FIELD_METHOD_MATRIX.csv').open('w',encoding='utf-8-sig',newline='') as fh:
        w=csv.DictWriter(fh,fieldnames=['field','method','status','evidence_count','values','where']);w.writeheader();w.writerows(method_rows)
    safe_json_dump(out/'FIELD_METHOD_MATRIX.json',method_rows)
    photo_report={'skipped':'--photos 0 or no URLs'}
    if not halt and args.photos:
        print('CHECK PHOTOS',min(args.photos,len(chosen['photo_urls'] or [])),flush=True)
        photo_report=image_download(session,chosen['photo_urls'],out,args.photos)
    safe_json_dump(out/'PHOTO_DOWNLOAD_REPORT.json',photo_report)
    lines=['MYHOME PDF INPUT DATA PROBE '+VERSION,'ID='+listing_id,
           'DATE_UTC='+datetime.now(timezone.utc).isoformat(),
           'HTTP_CALLS_MAX=2; BROWSER_PAGE_LOAD_MAX=1; PHOTO_DOWNLOAD_MAX='+str(args.photos),
           'REQUESTS='+str(fetch_log),'',
           'FIELD | STATUS | SELECTED METHOD | VALUE | NUMBER OF EVIDENCES','-'*120]
    for f,state,method,value,num in summary:
        lines.append(f'{f:19} | {state:15} | {method:26} | {value[:70]} | {num}')
        if state=='CONFLICT_REVIEW':
            for r in ev.group()[f][:8]:lines.append('   ALTERNATIVE '+r['method']+' : '+slug(r['value'],120))
    lines.extend(['','PHOTO_DOWNLOAD_REPORT='+slug(photo_report,1000),
    'NOTE: CONFIRMED extraction = data present in response, NOT proof of correctness.',
    'NOTE: RAW_HTML and API_RESPONSE are saved locally and may contain seller contact details. Do not share full raw files if private data matters.',
    'SEND: REPORT.txt, EXTRACTED.json, EVIDENCE.json, REQUEST_LOG.json, PHOTO_DOWNLOAD_REPORT.json.',
    'Do not send raw seller data unless needed to debug missing fields.'])
    (out/'REPORT.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    # Small, safe-to-share result archive. Raw pages and API dumps remain local.
    import zipfile
    ret=out.parent/(out.name+'_RETURN.zip')
    with zipfile.ZipFile(ret,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name in ('REPORT.txt','EXTRACTED.json','EVIDENCE.json','REQUEST_LOG.json',
                     'PHOTO_DOWNLOAD_REPORT.json','FIELD_METHOD_MATRIX.csv','FIELD_METHOD_MATRIX.json'):
            z.write(out/name,arcname=name)
    print('\n'.join(lines[:7]),flush=True)
    print('RETURN_ZIP=',ret,sep='')
    print('FOUND',sum(1 for x in summary if x[1]!='MISSING'),'/',len(FIELDS),'CONFLICTS',sum(x[1]=='CONFLICT_REVIEW' for x in summary))
    print('REPORT=',out/'REPORT.txt',sep='')
    print('DONE=PASS_WITH_EVIDENCE' if not halt else 'DONE=PARTIAL_ACCESS_DENIED')


def self_test():
    ev=Evidence()
    example={'data':{'id':26387744,'price':{'1':1900,'2':700},'area':85,'room':4,'bedroom':3,'floor':7,
      'total_floors':12,'status_id':2,'city_name':'Tbilisi','images':[{'large':'https://images.example/1.webp'}],
      'lat':41.7,'lng':44.8}}
    api_or_embedded(example,ev,'API:DETAIL','26387744')
    assert ev.group()['price_usd'][0]['value']==700
    assert ev.group()['rooms'][0]['value']==4
    assert ev.group()['building_status'][0]['value']=='New building'
    assert ev.group()['photo_urls'][0]['value'][0].endswith('1.webp')
    assert normalize_value('price_usd',{'1':1900}) is None
    sample='<h1>4 room apartment for rent</h1><meta property="og:image" content="https://img.example/a.webp"><script id="__NEXT_DATA__" type="application/json">'+json.dumps({'props':{'pageProps':{'listing':example['data']}}})+'</script>'
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as d:html_extract(sample,ev,'26387744','HTTP_HTML',Path(d))
    assert ev.group()['title'][0]['value']=='4 room apartment for rent'
    assert any(r['method']=='HTTP_HTML:NEXT_DATA' for r in ev.group()['rooms'])
    res,summary=build_results(ev)
    assert res['price_usd']==700 and res['area_m2']==85
    print('OFFLINE_SELFTEST=PASS; NETWORK_REQUESTS=0')

def main():
    p=argparse.ArgumentParser(description='One-listing multi-method evidence test for PDF data')
    p.add_argument('--id',default='26387744');p.add_argument('--photos',type=int,default=4)
    p.add_argument('--no-browser',action='store_true');p.add_argument('--show-browser',action='store_true')
    p.add_argument('--output');p.add_argument('--self-test',action='store_true')
    args=p.parse_args()
    if args.self_test:self_test();return
    if not 0<=args.photos<=7:p.error('--photos must be 0..7')
    run(args)

if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as ex:
        print('ERROR',ex,file=sys.stderr);sys.exit(2)
