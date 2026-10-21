"""On-demand MyHome detail SSR parser. No stored listings or third-party API dependency.
The id-only /en/pr/{id}/ is verified to return HTTP 301 to /en/real-estate/{id}/;
the latter and canonical slug resolution require Mac live acceptance.
"""
from __future__ import annotations
from datetime import datetime, timezone
from urllib.parse import urlsplit
from typing import Any
import re
from .parser import next_data, SourceError

ORIGIN = 'https://www.myhome.ge'
ID_RE = re.compile(r'[1-9][0-9]{4,11}\Z')

# The limited, documented IDs come from the historical MyHome repo mappings;
# do not invent labels for unknown IDs.
TYPES={1:'Apartment',2:'Private house',3:'Country house',4:'Plot',5:'Commercial area',6:'Hotel'}
DEALS={1:'For Sale',2:'For Rent',3:'Lease',7:'Daily rent',10:'By leased'}
STATUSES={1:'Old building',2:'New building',3:'Under construction'}
CONDITIONS={1:'Newly Renovated',2:'Old renovated',3:'Under renovation',4:'Needs renovation',5:'White frame',6:'Black frame',7:'Green frame',8:'White plus'}
METRO={2:'Vazha Pshavela',4:'Medical University',6:'Tsereteli',13:'Liberty Square'}
SELLERS={'physical':'Private','agent':'Agency','agency':'Agency','developer':'Developer','construction_company':'Developer'}


def validate_listing_id(value: str | None) -> str:
    if value is None or not isinstance(value,str) or not ID_RE.fullmatch(value):
        raise ValueError('listing_id: required numeric ID (5..12 digits), e.g. 25049178')
    return value


def detail_url(listing_id: str) -> str:
    return f'{ORIGIN}/en/pr/{validate_listing_id(listing_id)}/'


def _id_of(value: dict) -> str | None:
    candidate=value.get('id', value.get('statement_id', value.get('listing_id')))
    return str(candidate) if isinstance(candidate,(str,int)) and not isinstance(candidate,bool) else None


def _statement_candidates(page_props: dict):
    for key in ('statement','initialStatement'):
        direct=page_props.get(key)
        if isinstance(direct,dict):yield direct
    dehydrated=page_props.get('dehydratedState')
    if not isinstance(dehydrated,dict):return
    queries=dehydrated.get('queries')
    if not isinstance(queries,list):return
    for record in queries:
        if not isinstance(record,dict):continue
        state=record.get('state')
        if not isinstance(state,dict):continue
        payload=state.get('data')
        if not isinstance(payload,dict):continue
        for root in (payload,payload.get('data')):
            if not isinstance(root,dict):continue
            statement=root.get('statement')
            if isinstance(statement,dict):yield statement


def extract_detail(html: str, listing_id: str, effective_url: str) -> dict:
    ident=validate_listing_id(listing_id)
    url=urlsplit(effective_url)
    canonical=re.fullmatch(r'/(?:ru/nedvizhimost|en/real-estate|ka/udzravi-qoneba)/[a-z0-9-]+-'+ident+r'/?',url.path)
    numeric = url.path.rstrip('/') in (f'/en/pr/{ident}', f'/en/real-estate/{ident}', f'/ru/nedvizhimost/{ident}')
    if url.scheme!='https' or url.netloc!='www.myhome.ge' or url.query or url.fragment or not (numeric or canonical):
        raise SourceError('UPSTREAM_REDIRECT','Unexpected listing URL returned by source')
    doc=next_data(html)
    props=(doc.get('props') or {}).get('pageProps')
    if not isinstance(props,dict):
        raise SourceError('UPSTREAM_INVALID_SCHEMA','Missing detail pageProps')
    found=list(_statement_candidates(props))
    if not found:
        # Some URLs may return an SSR 200 page without statement data.
        raise SourceError('UPSTREAM_DETAIL_MISSING','MyHome detail SSR is missing the statement object (listing not found or schema changed)',502)
    matching=[s for s in found if _id_of(s)==ident]
    if not matching:
        raise SourceError('UPSTREAM_LISTING_MISMATCH','Received a different listing ID')
    # Multiple different matches must not be silently reconciled.
    if len(matching)>1 and any(s!=matching[0] for s in matching[1:]):
        raise SourceError('UPSTREAM_INVALID_SCHEMA','Conflicting SSR data for the same ID')
    return matching[0]


def _txt(v:Any)->str|None:
    if isinstance(v,str) and v.strip():return v.strip()
    return None


def _num(v:Any)->float|int|None:
    if isinstance(v,bool) or v is None:return None
    if isinstance(v,(str,int,float)):
        try:
            n=float(v)
            if n!=n or n in (float('inf'),float('-inf')):return None
            return int(n) if n.is_integer() else n
        except (ValueError,OverflowError):pass
    return None


def _integer(v:Any)->int|None:
    n=_num(v)
    return int(n) if n is not None and int(n)==n else None


def _first(d:dict,*names):
    for n in names:
        v=d.get(n)
        if v is not None:return v
    return None


def _price(statement:dict,currency:int):
    price=statement.get('price')
    if isinstance(price,dict):
        item=price.get(str(currency),price.get(currency))
        if isinstance(item,dict):return _num(_first(item,'price_total','total_price'))
    return _num(statement.get('price_usd' if currency==2 else 'price_gel'))


def _image_list(statement:dict)->list[str]:
    out=[]
    for key in ('images','photos','image_urls'):
        raw=statement.get(key)
        if isinstance(raw,list):
            for item in raw:
                if isinstance(item,str):candidate=item
                elif isinstance(item,dict):candidate=_first(item,'large','url','original','thumb','src')
                else:continue
                if not isinstance(candidate,str):continue
                info=urlsplit(candidate)
                if info.scheme=='https' and info.hostname in ('static-api-statements.tnet.ge','static.my.ge','www.myhome.ge') and candidate not in out:
                    out.append(candidate)
    return out


def _coords(st:dict):
    for key in ('location','coordinates','coords','map'):
        inner=st.get(key)
        if isinstance(inner,dict):
            lat=_num(_first(inner,'lat','latitude'));lng=_num(_first(inner,'lng','lon','long','longitude'))
            if lat is not None and lng is not None:return lat,lng
    return _num(_first(st,'latitude','lat')),_num(_first(st,'longitude','lng'))


def _label(st:dict,idfield:str,label:str,mapping:dict):
    v=_txt(st.get(label))
    if v:return v
    return mapping.get(_integer(st.get(idfield)))


def normalize_detail(st:dict,listing_id:str)->dict:
    """Emit only data supplied by statement or documented unambiguous ID maps.

    Unknown optional fields remain null. Never substitute user's sample response.
    """
    ident=validate_listing_id(listing_id)
    if _id_of(st)!=ident:raise SourceError('UPSTREAM_LISTING_MISMATCH','Listing ID changed')
    usd=_price(st,2);gel=_price(st,1)
    area=_num(_first(st,'area','area_size'))
    owner=st.get('user_type')
    owner_type=owner.get('type') if isinstance(owner,dict) else owner
    user=st.get('user') if isinstance(st.get('user'),dict) else {}
    slug=None
    href=st.get('href_lang')
    if isinstance(href,dict):slug=href.get('en') or href.get('ru')
    if not slug:slug=st.get('dynamic_slug')
    # ID-only fallback avoids inventing an SEO slug if the source omitted one.
    url=detail_url(ident)
    if isinstance(slug,str) and re.fullmatch(r'[a-z0-9-]+',slug):
        url=f'https://www.myhome.ge/en/real-estate/{slug}-{ident}/' if isinstance(href,dict) and href.get('en')==slug else detail_url(ident)
    image_urls=_image_list(st)
    price_info=st.get('price') if isinstance(st.get('price'),dict) else {}
    usd_info=price_info.get('2',price_info.get(2))
    gel_info=price_info.get('1',price_info.get(1))
    usd_sq=_num(usd_info.get('price_square')) if isinstance(usd_info,dict) else None
    gel_sq=_num(gel_info.get('price_square')) if isinstance(gel_info,dict) else None
    lat,lng=_coords(st)
    vip=_first(st,'is_vip','vip')
    negotiable=_first(st,'price_negotiable','is_price_negotiable')
    # Explicit bool / 0 / 1 only. Lack of evidence stays null.
    def flag(v):return v if isinstance(v,bool) else bool(v) if v in (0,1) and not isinstance(v,str) else None
    description=_txt(_first(st,'comment','description'))
    seller=_txt(_first(st,'seller_name','user_name','user_title','author_name')) or _txt(_first(user,'name','full_name','display_name'))
    return {
      'listing_id':ident,'owner_type':owner_type if isinstance(owner_type,str) else None,
      'deal_type_id':_integer(st.get('deal_type_id')),
      'real_estate_type_id':_integer(st.get('real_estate_type_id')),
      'title':_txt(_first(st,'dynamic_title','title','user_title')),
      'real_estate_type':_label(st,'real_estate_type_id','real_estate_type',TYPES),
      'transaction_type':_label(st,'deal_type_id','transaction_type',DEALS),
      'price_usd':usd,'price_gel':gel,
      'price_per_sqm_usd':usd_sq if usd_sq is not None else (round(usd/area) if usd is not None and area and area>0 else None),
      'price_per_sqm_gel':gel_sq if gel_sq is not None else (round(gel/area) if gel is not None and area and area>0 else None),
      'price_negotiable':flag(negotiable),
      'area_sqm':area,
      'yard_area_sqm':_num(_first(st,'yard_area','yard_area_size')),
      'city':_txt(_first(st,'city_name','city')),
      'city_id':_integer(st.get('city_id')),
      'district_id':_integer(st.get('district_id')),
      'district':_txt(st.get('district_name')),
      'sub_district':_txt(st.get('urban_name')),
      'street_address':_txt(_first(st,'address','street_address')),
      'nearest_metro':_label(st,'metro_station_id','metro_station_name',METRO),
      'building_status':_label(st,'status_id','building_status',STATUSES),
      'condition':_label(st,'condition_id','condition',CONDITIONS),
      'rooms':_integer(_first(st,'room','rooms','room_type_id')),
      'bedrooms':_integer(_first(st,'bedroom','bedrooms','bedroom_type_id')),
      'floor':_integer(st.get('floor')),
      'total_floors':_integer(st.get('total_floors')),
      'seller_type':SELLERS.get(owner_type) if isinstance(owner_type,str) else None,
      'seller_name':seller,
      'published_date':_txt(_first(st,'last_updated','published_date')),
      'is_vip':flag(vip),
      'image_url':image_urls[0] if image_urls else None,
      'url':url,
      'created_date':_txt(_first(st,'create_date','created_date')),
      'description':description,
      'latitude':lat,'longitude':lng,
      'ceiling_height_m':_num(_first(st,'ceiling_height','ceiling_height_m')),
      'build_year':_txt(_first(st,'build_year','build_year_name')) or (str(st.get('build_year')) if isinstance(st.get('build_year'),(int,float)) else None),
      'hot_water_type':_txt(_first(st,'hot_water_type','hot_water_type_name')),
      'door_window_type':_txt(_first(st,'door_window_type','door_window_type_name')),
      'views':_integer(_first(st,'views','view_count')),
      'myhome_price_scale':_txt(_first(st,'price_label','myhome_price_scale')),
      'image_urls':image_urls,
    }
