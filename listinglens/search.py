"""Explicit user-selected filters -> bounded MyHome search URL.

Only one HTML request per call. SSR confirmation, not this module's guesses,
decides whether a requested filter actually worked.
"""
from dataclasses import dataclass, asdict
from typing import Optional, Tuple
from urllib.parse import urlencode
import re

SOURCE_ORIGIN='https://www.myhome.ge'
PER_PAGE=24

class QueryIssue(ValueError):
    def __init__(self,code,detail,http_status=422):
        super().__init__(detail); self.code=code; self.http_status=http_status

ALIASES={
    'page':'page','deal_types':'deal_types','real_estate_types':'real_estate_types',
    'cities':'cities','city_ids':'cities','room_types':'room_types','rooms':'room_types',
    'bedroom_types':'bedroom_types','bedrooms':'bedroom_types',
    'currency_id':'currency_id','currency':'currency_id','price_from':'price_from',
    'price_to':'price_to','owner_type':'owner_type','seller_type':'owner_type',
    'districts':'districts','district_ids':'districts','urbans':'urbans','urban_ids':'urbans',
    'statuses':'statuses','building_statuses':'statuses',
    'area_from':'area_from','area_to':'area_to','area_types':'area_types','city':'city',
}
LISTS={'deal_types','real_estate_types','cities','room_types','bedroom_types','districts','urbans','statuses'}
SINGLE={'page','currency_id','price_from','price_to','area_from','area_to','area_types'}
CURRENCIES={'usd':2,'gel':1,'2':2,'1':1}
OWNERS={'physical':'physical','private':'physical','owner':'physical','agent':'agent','agency':'agent'}

@dataclass(frozen=True)
class SearchQuery:
    page:int=1
    deal_types:Optional[Tuple[int,...]]=None
    real_estate_types:Optional[Tuple[int,...]]=None
    cities:Optional[Tuple[int,...]]=None
    room_types:Optional[Tuple[int,...]]=None
    bedroom_types:Optional[Tuple[int,...]]=None
    currency_id:Optional[int]=None
    price_from:Optional[int]=None
    price_to:Optional[int]=None
    owner_type:Optional[str]=None
    districts:Optional[Tuple[int,...]]=None
    urbans:Optional[Tuple[int,...]]=None
    statuses:Optional[Tuple[int,...]]=None
    area_from:Optional[int]=None
    area_to:Optional[int]=None
    area_types:Optional[int]=None
    city:Optional[str]=None  # Named city; verified against SEO path and every listing

    def as_dict(self):
        obj=asdict(self)
        return {k:list(v) if k in LISTS and v is not None else v for k,v in obj.items()}

    def upstream_params(self):
        params={}
        # Explicit only, preserve input values exactly. No implicit location, category or deal.
        for key in ('deal_types','real_estate_types','cities','currency_id','room_types',
                    'bedroom_types','price_from','price_to','districts','urbans','statuses',
                    'area_from','area_to','area_types'):
            v=getattr(self,key)
            if v is not None:params[key]=','.join(map(str,v)) if key in LISTS else str(v)
        params['page']=str(self.page)
        if self.owner_type is not None:params['owner_type']=self.owner_type
        return params

    def route_path(self):
        """Build a known SEO route where documented; use the public generic search
        page for combinations whose SEO slugs are unknown. This is deliberately
        NOT a claim that the generic path is live-verified for every filter.
        """
        path=['en','real-estate']
        if self.deal_types in ((1,),(2,)):
            path.append({1:'sale',2:'rent'}[self.deal_types[0]])
        if self.real_estate_types==(1,):path.append('apartment')
        # SEO city slugs are known for these three cities only. For other city
        # IDs use numeric filters with generic English route; never guess slugs.
        if self.city is not None:path.append(self.city)
        elif self.cities in ((1,),(15,),(96,)):
            path.append({1:'tbilisi',15:'batumi',96:'kutaisi'}[self.cities[0]])
        return '/'+'/'.join(path)+'/'

    def route_strategy(self):
        return 'verified_shape' if self.deal_types==(2,) and self.real_estate_types==(1,) and self.cities==(1,) and self.room_types==(3,) else 'experimental_seo'

    def url(self):
        return SOURCE_ORIGIN+self.route_path()+'?'+urlencode(self.upstream_params())


def parse_query_pairs(pairs):
    items={}
    for rawkey,rawvalue in pairs:
        if rawkey not in ALIASES:raise QueryIssue('UNSUPPORTED_PARAMETER',f'{rawkey}: unsupported parameter')
        key=ALIASES[rawkey]
        if key in items:raise QueryIssue('DUPLICATE_PARAMETER',f'{key}: duplicate parameter')
        if not isinstance(rawvalue,str) or len(rawvalue)>300:raise QueryIssue('INVALID_PARAMETER',f'{rawkey}: invalid length')
        raw=rawvalue.strip()
        if not raw:continue
        if key in LISTS:
            pieces=raw.split(',')
            if len(pieces)>12 or not pieces or any(not re.fullmatch(r'[0-9]{1,8}',p) or int(p)<=0 for p in pieces):
                raise QueryIssue('INVALID_PARAMETER',f'{rawkey}: expected comma-separated positive IDs')
            vals=tuple(int(x) for x in pieces)
            if len(set(vals))!=len(vals):raise QueryIssue('INVALID_PARAMETER',f'{rawkey}: duplicate ID')
            items[key]=vals
        elif key in SINGLE:
            if key=='currency_id' and raw.lower() in CURRENCIES:v=CURRENCIES[raw.lower()]
            else:
                if not re.fullmatch(r'[0-9]+' if key=='page' else r'[0-9]{1,9}',raw):raise QueryIssue('INVALID_PARAMETER',f'{rawkey}: expected an integer')
                v=int(raw)
            if key=='page' and v<1:raise QueryIssue('INVALID_PARAMETER','page must be positive')
            items[key]=v
        elif key=='city':
            names={'tbilisi':'tbilisi','тбилиси':'tbilisi','batumi':'batumi','батуми':'batumi','kutaisi':'kutaisi','кутаиси':'kutaisi'}
            value=names.get(raw.lower())
            if value is None:raise QueryIssue('UNSUPPORTED_VALUE','Named city is limited to Tbilisi, Batumi or Kutaisi')
            items[key]=value
        else:
            value=OWNERS.get(raw.lower())
            if value is None:raise QueryIssue('UNSUPPORTED_VALUE','owner_type: physical/agent')
            items[key]=value
    if not any(k!='page' for k in items):raise QueryIssue('MISSING_FILTERS','Specify at least one filter')
    q=SearchQuery(**items)
    if q.city is not None and q.cities is not None:
        raise QueryIssue('AMBIGUOUS_LOCATION','Specify a named city OR a numeric city ID, not both')
    if q.currency_id is not None and q.currency_id not in (1,2):raise QueryIssue('UNSUPPORTED_VALUE','currency_id: 1 GEL / 2 USD')
    if (q.price_from is not None or q.price_to is not None) and q.currency_id is None:
        raise QueryIssue('MISSING_CURRENCY','Select a currency for the price range')
    for lo,hi in ((q.price_from,q.price_to),(q.area_from,q.area_to)):
        if lo is not None and hi is not None and lo>hi:raise QueryIssue('INVALID_PARAMETER','Minimum exceeds maximum')
    return q
