"""R3.1.9 regression: shape copied from Mac diagnostic 26071632; synthetic photo URLs.
No source HTML, private contacts, network calls, or production sample injection.
"""
import asyncio
from pathlib import Path
from fastapi.testclient import TestClient
import pytest
from listinglens.detail import normalize_detail
from listinglens.workflow import (
    _single_source_summary,_single_match_settings,_single_search_query,
    _owner_to_agent_ranges,compare_agent_to_selected_owner,
    QueryIssue,JobRuntime
)
from listinglens import workflow as w
from listinglens.app import app

ROOT=Path(__file__).resolve().parents[1]


def fixture_statement():
    return {
      'id':26071632,'room_type_id':4,'bedroom_type_id':2,
      'city_id':1,'city_name':'Tbilisi','district_id':4,
      'district_name':'Vake-Saburtalo','urban_name':'Didi digomi',
      'deal_type_id':2,'real_estate_type_id':1,'area':95,
      'price':{'2':{'price_total':700},'1':{'price_total':1900}},
      'total_price':700,'user_type':{'type':'physical'},
      'images':[{'url':f'https://static.my.ge/synthetic_{i}.jpg'} for i in range(14)],
    }


def test_real_machead_shape_26071632_regression():
    d=normalize_detail(fixture_statement(),'26071632')
    fields={k:d[k] for k in ('city_id','district_id','city','district','sub_district',
            'price_usd','area_sqm','rooms','bedrooms','owner_type')}
    assert fields=={'city_id':1,'district_id':4,'city':'Tbilisi',
      'district':'Vake-Saburtalo','sub_district':'Didi digomi',
      'price_usd':700,'area_sqm':95,'rooms':4,'bedrooms':2,'owner_type':'physical'}
    assert len(d['image_urls'])==14
    query=_single_search_query(d)
    assert query.cities==(1,) and query.deal_types==(2,) and query.real_estate_types==(1,)
    f=_single_source_summary(d,'OWNER_TO_AGENTS')
    assert {k:f[k] for k in ('price_from','price_to','area_from','area_to','source_photo_count')}=={
        'price_from':600,'price_to':750,'area_from':90,'area_to':105,'source_photo_count':3}
    assert f['candidate_role']=='agent'


def test_alternative_explicit_room_fields_and_no_invention():
    st=fixture_statement()
    st['room']=3;st['bedrooms']=1
    assert normalize_detail(st,'26071632')['rooms']==3
    assert normalize_detail(st,'26071632')['bedrooms']==1
    for key in ('room_type_id','bedroom_type_id'):
        st.pop(key,None)
    st.pop('room');st.pop('bedrooms')
    d=normalize_detail(st,'26071632')
    assert d['rooms'] is None and d['bedrooms'] is None


@pytest.mark.parametrize('amount,bounds',[
    (900,(850,1000)),(901,(801,1101)),(1100,(1000,1150)),(1101,(901,1201))
])
def test_thresholds_are_direction_specific(amount,bounds):
    d=normalize_detail(fixture_statement(),'26071632')
    d['price_usd']=amount
    owner_filters=_single_source_summary(d,'OWNER_TO_AGENTS')
    owner_lo,owner_hi=(-100,50) if amount<=1100 else (-200,100)
    assert (owner_filters['price_from'],owner_filters['price_to'])==(amount+owner_lo,amount+owner_hi)
    agent_filters=_single_source_summary(d,'AGENT_TO_OWNER')
    lo,hi=(-50,100) if amount<=900 else (-100,200)
    assert (agent_filters['price_from'],agent_filters['price_to'])==(amount+lo,amount+hi)
    assert (agent_filters['area_from'],agent_filters['area_to'])==(85,100)
    assert (owner_filters['area_from'],owner_filters['area_to'])==(90,105)


def test_no_manual_offsets_in_ui_or_api():
    page=(ROOT/'listinglens/web/index.html').read_text()
    js=(ROOT/'listinglens/web/app.js').read_text()
    for name in ('reverse-price-min','reverse-price-max','reverse-area-min','reverse-area-max','reverse-options'):
        assert name not in page and name not in js
    assert 'reverse_offsets' not in js
    with pytest.raises(QueryIssue):_single_match_settings({'price_from_delta':-1})
    response=TestClient(app).post('/api/single/jobs',json={
        'listing_id':'26071632','direction':'OWNER_TO_AGENTS','reverse_offsets':{}})
    assert response.status_code==422
    assert response.json()['error_code']=='MANUAL_OFFSETS_DISABLED'


def test_owner_source_three_images_agent_candidate_all_and_first_hit(tmp_path,monkeypatch):
    w.DB_PATH=tmp_path/'photo.sqlite3'
    state={'job_id':'photo','cancel_requested':False,'metrics':{
        'image_pairs_attempted':0,'image_pairs_failed':0,'image_comparisons':0}}
    w.save_state({'job_id':'photo','status':'RUNNING','metrics':{},'cancel_requested':False})
    rt=JobRuntime(state)
    owner_urls=[f'https://static.my.ge/owner_{i}.jpg' for i in range(8)]
    agent_urls=[f'https://static.my.ge/agent_{i}.jpg' for i in range(11)]
    got=[]
    async def photo(client,url):got.append(url);return url
    def score(a,b):return {'hist_correlation':1 if (a,b)==(owner_urls[2],agent_urls[4]) else .4,'hash_distance':0}
    monkeypatch.setattr(rt,'photo',photo)
    monkeypatch.setattr(w,'scores',score)
    result,complete=asyncio.run(compare_agent_to_selected_owner(rt,
       {'image_urls':agent_urls},{'image_urls':owner_urls}))
    assert complete and (result['owner_photo'],result['agent_photo'])==(2,4)
    assert result['similarity']==1
    assert state['metrics']['image_comparisons']==2*11+5
    assert owner_urls[3] not in got and agent_urls[5] in got
    assert len(got)==3+2*11+5


def test_threshold_strictly_more_than_095(tmp_path,monkeypatch):
    w.DB_PATH=tmp_path/'photo2.sqlite3'
    w.save_state({'job_id':'photo2','status':'RUNNING','metrics':{},'cancel_requested':False})
    state={'job_id':'photo2','cancel_requested':False,'metrics':{
        'image_pairs_attempted':0,'image_pairs_failed':0,'image_comparisons':0}}
    rt=JobRuntime(state)
    async def photo(client,url):return url
    monkeypatch.setattr(rt,'photo',photo)
    monkeypatch.setattr(w,'scores',lambda a,b:{'hist_correlation':.95,'hash_distance':0})
    result,complete=asyncio.run(compare_agent_to_selected_owner(rt,
       {'image_urls':['https://static.my.ge/a.png']},
       {'image_urls':['https://static.my.ge/o.png']}))
    assert complete and result is None
