"""R3.1.7 user-specified single-search/first-photo rules, no MyHome traffic."""
import asyncio
from dataclasses import replace
import pytest
from listinglens import workflow as w
from listinglens.matcher import photo_similarity, fingerprint
from listinglens.search import SearchQuery
from helpers import mk, pic


def run(c):return asyncio.run(c)


def setup(tmp_path,name='single'):
    w.DB_PATH=tmp_path/(name+'.sqlite3');w.PREP.clear();w.ACTIVE.clear()
    state={'job_id':name,'status':'RUNNING','mode':'AGENT','phase':'owner_search',
        'cancel_requested':False,'metrics':{
            'search_calls':0,'detail_calls':0,'photo_calls':0,'owner_search_calls':0,
            'owner_pages_visited':0,'owner_candidates_examined':0,'owner_candidate_shortlisted':0,
            'skipped':0,'photo_errors':0,'image_comparisons':0,'image_pairs_attempted':0,
            'image_pairs_failed':0,'agent_processed':0,'matches_confirmed':0,
            'matches_to_review':0,'errors':0}}
    w.save_state(state)
    return state,w.JobRuntime(state)


def sample_agent(price=900):
    return dict(mk('26554321','agent'),price_usd=price,area_m2=100,
                rooms=4,bedrooms=3,district_id=4,city_id=1)


@pytest.mark.parametrize('price,expected',[
    (700,(650,800)),(900,(850,1000)),(901,(801,1101)),(1200,(1100,1400))])
def test_one_query_price_breakpoint_and_exact_location_rooms(monkeypatch,tmp_path,price,expected):
    state,rt=setup(tmp_path,'p'+str(price))
    queries=[]
    async def search(q):
        queries.append(q)
        return w.SearchBatch([],raw_count=0)
    monkeypatch.setattr(rt,'search',search)
    owners,complete,trace=run(rt.owner_candidates(sample_agent(price),SearchQuery(
        cities=(1,),deal_types=(2,),real_estate_types=(1,),
        room_types=(4,),bedroom_types=(3,),area_types=1)))
    assert owners==[] and complete
    assert len(trace)==1 and len(queries)==1
    assert all(q.owner_type=='physical' for q in queries)
    assert all(q.cities==(1,) and q.districts==(4,) for q in queries)
    assert all(q.room_types==(4,) and q.bedroom_types==(3,) for q in queries)
    assert all((q.price_from,q.price_to)==expected for q in queries)
    assert all((q.area_from,q.area_to)==(90,105) for q in queries)
    assert trace[0]['end_evidence']=='FIRST_EMPTY_SSR_PAGE'
    assert set(q.page for q in queries)=={1}


def test_owner_candidates_reject_other_district_rooms_and_bedrooms(monkeypatch,tmp_path):
    state,rt=setup(tmp_path,'district')
    good=dict(mk('26551001'),district_id=4,area_m2=95,rooms=4,bedrooms=3)
    wrong_d=dict(mk('26551002'),district_id=5,area_m2=95,rooms=4,bedrooms=3)
    wrong_r=dict(mk('26551003'),district_id=4,area_m2=95,rooms=5,bedrooms=3)
    wrong_b=dict(mk('26551004'),district_id=4,area_m2=95,rooms=4,bedrooms=2)
    async def search(q):
        return w.SearchBatch([good,wrong_d,wrong_r,wrong_b],raw_count=4) if q.page==1 else w.SearchBatch([],raw_count=0)
    monkeypatch.setattr(rt,'search',search)
    owners,complete,trace=run(rt.owner_candidates(sample_agent(),SearchQuery(cities=(1,),deal_types=(2,))))
    assert complete and len(trace)==1
    assert [r['listing_id'] for r in owners]==[good['listing_id']]


def test_missing_district_never_falls_back_to_citywide_search(monkeypatch,tmp_path):
    state,rt=setup(tmp_path,'missing')
    seen=[]
    async def search(q):seen.append(q);return []
    monkeypatch.setattr(rt,'search',search)
    agent=sample_agent();agent['district_id']=None
    with pytest.raises(w.SourceError) as exc:
        run(rt.owner_candidates(agent,SearchQuery(cities=(1,))))
    assert exc.value.code=='INSUFFICIENT_MATCH_ANCHORS' and seen==[]


def test_incomplete_search_does_not_claim_not_found(monkeypatch,tmp_path):
    state,rt=setup(tmp_path,'incomplete')
    async def search(q):
        if q.page==2:raise w.SourceError('UPSTREAM_ACCESS_DENIED','Denied')
        # First page is nonempty so the new FIRST EMPTY rule does not stop here.
        return w.SearchBatch([dict(sample_agent(),owner_type='physical',listing_id='26554322')],raw_count=1)
    monkeypatch.setattr(rt,'search',search)
    _,complete,trace=run(rt.owner_candidates(sample_agent(),SearchQuery(cities=(1,))))
    assert not complete and trace[0]['end_evidence']=='UPSTREAM_ERROR:UPSTREAM_ACCESS_DENIED'


def test_similarity_requires_both_histogram_and_perceptual_hash():
    assert photo_similarity({'hist_correlation':1,'hash_distance':0})==1
    assert photo_similarity({'hist_correlation':.95,'hash_distance':0})==.95
    assert photo_similarity({'hist_correlation':1,'hash_distance':4})==.9375
    assert photo_similarity({'hist_correlation':.999,'hash_distance':0})>.95
    assert photo_similarity({'hist_correlation':.5,'hash_distance':0})<=.95


def test_first_high_photo_match_short_circuits_remaining_owner_photos_and_candidates(monkeypatch,tmp_path):
    state,rt=setup(tmp_path,'early')
    agent=sample_agent();owner1=mk('26550001');owner2=mk('26550002')
    agenturls=[f'https://static.my.ge/a_{i}.png' for i in range(7)]
    ownerurls=[f'https://static.my.ge/o_{i}.png' for i in range(11)]
    called_details=[];called_photos=[]
    async def detail(ident):
        called_details.append(ident)
        if ident==agent['listing_id']:
            return {'listing_id':ident,'image_urls':agenturls}
        if ident==owner1['listing_id']:
            return {'listing_id':ident,'image_urls':ownerurls,'url':owner1['url']}
        raise AssertionError('Later owners must not be fetched')
    async def candidates(a,q):return [owner1,owner2],True,[{'search':1,'coverage_complete':True}]
    ref=fingerprint(pic(889))
    async def photo(client,url):
        called_photos.append(url)
        if url==agenturls[0] or url==ownerurls[1]:return ref
        return fingerprint(pic(900+len(called_photos)))
    monkeypatch.setattr(rt,'detail',detail);monkeypatch.setattr(rt,'owner_candidates',candidates)
    monkeypatch.setattr(rt,'photo',photo)
    row=w.base_row(agent,'AGENT')
    run(rt.match(row,SearchQuery(cities=(1,))))
    assert row['matching_status']=='MATCH_CONFIRMED'
    assert row['original_owner_id']==owner1['listing_id']
    assert row['photo_similarity']>0.95
    assert row['matched_photo_count']==1
    assert len(called_photos)==3  # first source photo + owner photos 0,1
    assert all(url not in called_photos for url in agenturls[1:]+ownerurls[2:])
    assert owner2['listing_id'] not in called_details
    assert state['metrics']['image_comparisons']==2
    assert w.agent_match_fully_checked(row)
    assert row['match_evidence'][0]['agent_photo']==0
    assert row['match_evidence'][0]['owner_photo']==1


def test_exact_point_95_does_not_match(monkeypatch,tmp_path):
    state,rt=setup(tmp_path,'exact')
    agent=sample_agent();owner=mk('26550001')
    async def detail(ident):
        return {'listing_id':ident,'image_urls':[
            'https://static.my.ge/a.png' if ident==agent['listing_id'] else 'https://static.my.ge/o.png']}
    async def candidates(a,q):return [owner],True,[{'search':1,'coverage_complete':True}]
    async def photo(client,url):return fingerprint(pic(10 if url.endswith('a.png') else 11))
    # Deliberate threshold equality; not above -> NOT_FOUND
    monkeypatch.setattr(rt,'detail',detail);monkeypatch.setattr(rt,'owner_candidates',candidates)
    monkeypatch.setattr(rt,'photo',photo)
    monkeypatch.setattr(w,'scores',lambda a,b:{'hist_correlation':.95,'hash_distance':0})
    row=w.base_row(agent,'AGENT')
    run(rt.match(row,SearchQuery(cities=(1,))))
    assert row['matching_status']=='NOT_FOUND'
    assert row['photo_similarity'] is None
    assert w.agent_match_fully_checked(row)
