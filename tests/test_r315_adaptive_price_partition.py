"""Simulated source where page>=6 always returns 500, despite >120 results."""
import asyncio
from listinglens import workflow as w
from helpers import mk

FILTERS={'cities':'1','deal_types':'2','real_estate_types':'1','currency_id':'2',
         'room_types':'5','bedroom_types':'3,4','price_from':'700','price_to':'1000',
         'area_from':'90','area_to':'130'}

def payload(mode):
    return {'mode':mode,'filters':FILTERS,'pages_mode':'all','listings_mode':'all'}

def install(monkeypatch,mode,total=263, *, fail_hard=False, missing_id=None):
    people=[]
    for i in range(total):
        price=700+(i*301)//total
        row=mk(str(25000000+i),mode)
        row['price_usd']=price
        people.append(row)
    seen_queries=[]
    async def search(q):
        seen_queries.append((q.price_from,q.price_to,q.page,q.owner_type))
        if q.page>=6:
            raise w.SourceError('UPSTREAM_HTTP_ERROR','MyHome returned HTTP 500',502)
        if fail_hard and q.price_to<=850:
            raise w.SourceError('UPSTREAM_ACCESS_DENIED','MyHome returned HTTP 403',503)
        rows=[r for r in people if q.price_from<=r['price_usd']<=q.price_to]
        if missing_id is not None and q.price_to-q.price_from<300:
            rows=[r for r in rows if r['listing_id']!=missing_id]
        batch_rows=rows[(q.page-1)*24:q.page*24]
        batch=w.SearchBatch(batch_rows,raw_count=len(batch_rows))
        batch.raw_ids=[r['listing_id'] for r in batch_rows]
        batch.agent_role_rows=batch_rows if mode=='agent' else []
        return batch
    monkeypatch.setattr(w,'search_source',search)
    async def no_sleep(_):pass
    monkeypatch.setattr(w.asyncio,'sleep',no_sleep)
    w.PREP.clear();w.ACTIVE.clear()
    return seen_queries

def run(coro):return asyncio.run(coro)

def test_agent_263_recovered_across_price_ranges(monkeypatch):
    calls=install(monkeypatch,'agent')
    res=run(w.preflight(payload('AGENT')))
    assert isinstance(res,dict)
    assert res['enumerated_agent_total']==263
    assert res['count_quality']=='ENUMERATED_SNAPSHOT'
    assert res['source_total_status']=='ENUMERATED_AGENT_SNAPSHOT'
    assert res['enumeration_evidence']['complete'] is True
    assert res['enumeration_evidence']['end_reason']=='PRICE_PARTITIONS_FIRST_EMPTY_SSR'
    assert res['enumeration_evidence']['partition_count']>=2
    assert res['enumeration_evidence']['partition_original_unrecovered_ids']==0
    assert len({r['listing_id'] for r in w.PREP[res['preflight_id']]['agent_snapshot']['rows']})==263
    assert (700,1000,6,'agent') in calls
    assert all(c[2]<=6 for c in calls)

def test_owner_263_recovered_across_price_ranges(monkeypatch):
    calls=install(monkeypatch,'physical')
    res=run(w.preflight(payload('OWNER')))
    assert res['enumerated_owner_total']==263
    assert res['enumeration_evidence']['complete'] is True
    assert res['enumeration_evidence']['partition_count']>=2
    assert (700,1000,6,'physical') in calls

def test_rare_missing_record_blocks_complete_claim(monkeypatch):
    calls=install(monkeypatch,'agent',missing_id='25000000')
    res=run(w.preflight(payload('AGENT')))
    assert res['source_total_status']=='PARTIAL_AGENT_SNAPSHOT'
    assert res['enumeration_evidence']['complete'] is False
    assert res['enumeration_evidence']['partition_original_unrecovered_ids']==1
    assert res['partial_agent_rows']==263

def test_denial_does_not_become_verified_end(monkeypatch):
    install(monkeypatch,'agent',fail_hard=True)
    res=run(w.preflight(payload('AGENT')))
    assert res.status_code==503
    assert b'UPSTREAM_ACCESS_DENIED' in res.body

def test_single_price_cannot_split(monkeypatch):
    install(monkeypatch,'agent',total=200)
    # A too-narrow user range cannot prove full coverage by more splitting.
    p=payload('AGENT');p['filters']=dict(FILTERS,price_from='700',price_to='701')
    res=run(w.preflight(p))
    assert res['enumeration_evidence']['complete'] is True
