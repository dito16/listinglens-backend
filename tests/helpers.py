"""Synthetic offline listing fixtures shared by current test modules."""
import io
import json
from PIL import Image


def mk(id, kind='physical'):
    return {'listing_id': str(id), 'url': f'https://www.myhome.ge/en/real-estate/test-{id}/',
        'title': 'Sample', 'owner_type': kind, 'price_usd': 900, 'price_gel': 2340,
        'area_m2': 60, 'rooms': 2, 'bedrooms': 1, 'city_id': 1,
        'city_name': 'Tbilisi', 'district_id': 4, 'district_name': 'Test',
        'urban_name': 'Test', 'listing_updated_at': '2026-10-19 00:00:00'}


def pic(seed):
    import numpy as np
    data=np.random.default_rng(seed).integers(0,256,(60,80),dtype='uint8')
    out=io.BytesIO()
    Image.fromarray(data).save(out,'PNG')
    return out.getvalue()

def _item(q,identifier,price):
    return {'id':identifier,'deal_type_id':2,'real_estate_type_id':1,'city_id':1,'city_name':'Тбилиси',
            'room':'3','bedroom':'2','area':97,'price':{'2':{'price_total':price},'1':{'price_total':price*2.6}},
            'user_type':{'type':'agent'},'status_id':1,'district_id':4,'urban_id':38,
            'dynamic_slug':'test-apartment','dynamic_title':'SYNTHETIC'}

def mock_html(q, rows=None, *, echo_override=None, city_seo=True, duplicate=False):
 vals=q.upstream_params().copy()
 if echo_override:vals.update(echo_override)
 if rows is None:rows=[dict(id=900000001,deal_type_id=q.deal_types[0] if q.deal_types else 2,
     real_estate_type_id=q.real_estate_types[0] if q.real_estate_types else 1,
     city_id=q.cities[0] if q.cities else 1,city_name=({'batumi':'Батуми','kutaisi':'Кутаиси','tbilisi':'Тбилиси'}.get(q.city) or 'Тбилиси'),
     room=str(q.room_types[0] if q.room_types else 2),bedroom=str(q.bedroom_types[0] if q.bedroom_types else 1),
     area=70,price={'2':{'price_total':900},'1':{'price_total':2400}},
     user_type={'type':q.owner_type or 'physical'},status_id=q.statuses[0] if q.statuses else 1,
     district_id=q.districts[0] if q.districts else 4,urban_id=q.urbans[0] if q.urbans else 5,
     dynamic_slug='sdaetsia-kvartira',dynamic_title='OFFLINE TEST ONLY')]
 if duplicate:rows=rows+rows
 qk=['statements','list',{'params':{'locale':'ru'},'query':vals}]
 data={'page':'/[...slug]','query':{**vals,'slug':q.route_path().strip('/').split('/')[1:]},'props':{'pageProps':{
   'initialFetchArgs':{'query':vals},'initialSeoParams':{'slugs':q.route_path().strip('/').split('/')[2:] if city_seo else []},
   'dehydratedState':{'queries':[{'queryKey':qk,'state':{'data':{'result':True,'data':{'data':rows}}}}]}}}}
 return '<html><script id="__NEXT_DATA__" type="application/json">'+json.dumps(data,ensure_ascii=False)+'</script></html>'
