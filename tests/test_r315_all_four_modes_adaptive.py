"""Offline regressions for both Collection and both Search by ID directions.

MyHome pagination is modelled as responding 500 from page six onward,
regardless of the actual number of listings. No network, live ads or photos.
"""
import asyncio
from dataclasses import replace
import pytest
from listinglens import workflow as w
from listinglens.search import SearchQuery
from helpers import mk

FILTERS={'cities':'1','deal_types':'2','real_estate_types':'1','currency_id':'2',
         'room_types':'4','bedroom_types':'3','price_from':'700','price_to':'1000',
         'area_from':'90','area_to':'130'}

def run(c):return asyncio.run(c)

def records(role,low,high,total=263):
    return [dict(mk(str(28000000+i),role),price_usd=low+(i*(high-low))//total,
                 area_m2=100,city_id=1,district_id=4,rooms=4,bedrooms=3)
            for i in range(total)]

def batch_source(monkeypatch,people,*,deny=False,remove_id=None):
    queries=[]
    async def source(q):
        queries.append((q.price_from,q.price_to,q.page,q.owner_type))
        if q.page>=6:
            if deny: raise w.SourceError('UPSTREAM_ACCESS_DENIED','MyHome returned HTTP 403',503)
            raise w.SourceError('UPSTREAM_HTTP_ERROR','MyHome returned HTTP 500',502)
        candidates=[x for x in people if q.price_from <= x['price_usd'] <= q.price_to]
        if remove_id and (q.price_to-q.price_from)<300:
            candidates=[x for x in candidates if x['listing_id']!=remove_id]
        rows=candidates[(q.page-1)*24:q.page*24]
        out=w.SearchBatch(rows,raw_count=len(rows))
        out.raw_ids=[r['listing_id'] for r in rows]
        out.agent_role_rows=rows if rows and rows[0]['owner_type']=='agent' else []
        return out
    monkeypatch.setattr(w,'search_source',source)
    async def no_sleep(_):pass
    monkeypatch.setattr(w.asyncio,'sleep',no_sleep)
    return source,queries

def state(tmp_path,name):
    w.DB_PATH=tmp_path/(name+'.sqlite3')
    s={'job_id':name,'status':'RUNNING','phase':'owner_search','cancel_requested':False,
       'metrics':{'search_calls':0,'owner_search_calls':0,'owner_pages_visited':0,
                  'owner_listing_pages':0,'owner_candidates_examined':0,
                  'owner_candidate_shortlisted':0}}
    w.save_state(s)
    return w.JobRuntime(s)

def detail(role,price=900):
    return dict(mk('29999001',role),price_usd=price,area_sqm=100,
        city_id=1,district_id=4,rooms=4,bedrooms=3,
        deal_type_id=2,real_estate_type_id=1)

@pytest.mark.parametrize('selected_mode',('AGENT','OWNER'))
def test_collection_full_263_via_price_partitions(monkeypatch,tmp_path,selected_mode):
    people=records('agent' if selected_mode=='AGENT' else 'physical',700,1000)
    _,calls=batch_source(monkeypatch,people)
    w.PREP.clear();w.ACTIVE.clear();w.DB_PATH=tmp_path/'collection.sqlite3'
    result=run(w.preflight({'mode':selected_mode,'filters':FILTERS,
         'pages_mode':'all','listings_mode':'all'}))
    assert not isinstance(result,w.JSONResponse)
    assert result['count_quality']=='ENUMERATED_SNAPSHOT'
    assert result['enumeration_evidence']['complete']
    assert result['enumeration_evidence']['partition_count']>=2
    assert result['scope_total_expected']==263
    assert (700,1000,6,'agent' if selected_mode=='AGENT' else 'physical') in calls
    if selected_mode=='OWNER':
        async def both():
            started=await w.start_job({'preflight_id':result['preflight_id']})
            await w.ACTIVE[started['job_id']]
            return w.load_state(started['job_id']),w.rows_for(started['job_id'])
        saved,rows=run(both())
        assert saved['status']=='COMPLETED'
        assert len(rows)==263
        assert all(r['seller_type']=='physical' for r in rows)

def test_owner_collection_limit_with_verified_full_search(monkeypatch,tmp_path):
    people=records('physical',700,1000)
    batch_source(monkeypatch,people)
    w.PREP.clear();w.ACTIVE.clear();w.DB_PATH=tmp_path/'lim.sqlite3'
    result=run(w.preflight({'mode':'OWNER','filters':FILTERS,'pages_mode':'all',
                     'listings_mode':'limit','row_limit':30}))
    assert result['enumerated_owner_total']==263
    assert result['scope_total_expected']==30
    assert len(w.PREP[result['preflight_id']]['owner_snapshot']['rows'])==30
    async def both():
        started=await w.start_job({'preflight_id':result['preflight_id']})
        await w.ACTIVE[started['job_id']]
        return w.load_state(started['job_id']),w.rows_for(started['job_id'])
    saved,rows=run(both())
    assert saved['status']=='COMPLETED' and len(rows)==30

def test_single_agent_to_owner_263_adaptive(monkeypatch,tmp_path):
    people=records('physical',850,1000)
    _,calls=batch_source(monkeypatch,people)
    rt=state(tmp_path,'agent_to_owner')
    async def rt_search(q):return await w.search_source(q)
    monkeypatch.setattr(rt,'search',rt_search)
    owners,complete,trace=run(rt.owner_candidates(detail('agent',900),
          SearchQuery(cities=(1,),deal_types=(2,),real_estate_types=(1,))))
    assert complete and len(owners)==263
    assert trace[0]['partition_count']>=2
    assert trace[0]['end_evidence']=='PRICE_PARTITIONS_FIRST_EMPTY_SSR'
    assert (850,1000,6,'physical') in calls

def test_single_owner_to_agent_263_adaptive(monkeypatch,tmp_path):
    people=records('agent',800,950)
    _,calls=batch_source(monkeypatch,people)
    rt=state(tmp_path,'owner_to_agent')
    async def rt_search(q):return await w.search_source(q)
    monkeypatch.setattr(rt,'search',rt_search)
    output=run(w.reverse_agents_for_owner(rt,detail('physical',900),
        SearchQuery(cities=(1,),deal_types=(2,),real_estate_types=(1,))))
    assert len(output)==263
    assert rt.s['single_candidate_coverage_complete'] is True
    assert rt.s['single_candidate_partition_count']>=2
    assert (800,950,6,'agent') in calls

def test_partial_single_search_is_not_a_negative_match(monkeypatch,tmp_path):
    people=records('physical',899,900,140)
    batch_source(monkeypatch,people,remove_id=people[0]['listing_id'])
    rt=state(tmp_path,'partial')
    async def rt_search(q):return await w.search_source(q)
    monkeypatch.setattr(rt,'search',rt_search)
    q=SearchQuery(cities=(1,),deal_types=(2,),real_estate_types=(1,))
    # Force a 1-unit price search; splitting is not possible.
    owner=detail('agent',900)
    async def search_non_split(q):return await w.search_source(replace(q,price_from=899,price_to=900))
    monkeypatch.setattr(rt,'search',search_non_split)
    found,complete,trace=run(rt.owner_candidates(owner,q))
    assert not complete
    assert trace[0]['coverage_complete'] is False

def test_single_does_not_bypass_access_denial(monkeypatch,tmp_path):
    people=records('agent',800,950)
    batch_source(monkeypatch,people,deny=True)
    rt=state(tmp_path,'deny')
    async def rt_search(q):return await w.search_source(q)
    monkeypatch.setattr(rt,'search',rt_search)
    with pytest.raises(w.SourceError) as exc:
        run(w.reverse_agents_for_owner(rt,detail('physical',900),
            SearchQuery(cities=(1,),deal_types=(2,),real_estate_types=(1,))))
    assert exc.value.code=='UPSTREAM_ACCESS_DENIED'
