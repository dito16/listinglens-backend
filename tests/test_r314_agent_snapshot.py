"""R3.1.4 behavior tests: agent denominator and evidence-led progress."""
import asyncio
from dataclasses import replace
import pytest
from listinglens import workflow as w
from listinglens.search import parse_query_pairs
from helpers import mk

FILTERS={'cities':'1','deal_types':'2','real_estate_types':'1','currency_id':'2',
         'price_from':'500','price_to':'600','area_from':'95','area_to':'100',
         'room_types':'3','bedroom_types':'2','area_types':'1'}

async def fast_sleep(_):pass

def run(coro):return asyncio.run(coro)

def setup(monkeypatch,tmp_path,source):
    w.DB_PATH=tmp_path/'test.sqlite3';w.PREP.clear();w.ACTIVE.clear()
    monkeypatch.setenv('LISTINGLENS_AUTHORIZED_BULK','1')
    monkeypatch.setattr(w,'search_source',source)
    monkeypatch.setattr(w.asyncio,'sleep',fast_sleep)


def test_area_types_is_preserved_in_ssr_query_contract():
    q=parse_query_pairs(FILTERS.items())
    assert q.area_types==1
    assert 'area_types=1' in q.url()
    assert q.upstream_params()['area_types']=='1'


def test_mixed_pages_count_only_owner_type_agent(monkeypatch,tmp_path):
    called=[]
    async def source(q):
        called.append((q.page,q.owner_type))
        if q.owner_type=='agent':
            raise w.SourceError('UPSTREAM_FILTER_MISMATCH','No corroborated agent SSR')
        ids={1:[mk('10000001','agent'),mk('10000002','physical'),mk('10000003','agency')],
             2:[mk('10000004','physical'),mk('10000005','agent')]}.get(q.page,[])
        return w.SearchBatch(ids,raw_count=len(ids))
    setup(monkeypatch,tmp_path,source)
    pre=run(w.preflight({'mode':'AGENT','filters':FILTERS,'pages_mode':'all','listings_mode':'all'}))
    assert pre['enumerated_agent_total']==2
    assert pre['scope_total_expected']==2
    assert pre['source_total'] is None  # not an official server count
    assert pre['source_total_status']=='ENUMERATED_AGENT_SNAPSHOT'
    assert pre['enumeration_evidence']['end_reason']=='FIRST_EMPTY_SSR_PAGE'
    assert pre['enumeration_evidence']['raw_rows']==5
    assert pre['enumeration_evidence']['pages_visited']==3
    assert pre['enumeration_evidence']['role_scope']=='AGENT_ONLY_FROM_MIXED'
    assert [x['listing_id'] for x in w.PREP[pre['preflight_id']]['agent_snapshot']['rows']]==['10000001','10000005']
    assert (3,None) in called and (4,None) not in called


def test_direct_agent_pages_have_separate_agent_denominator(monkeypatch,tmp_path):
    async def source(q):
        assert q.owner_type=='agent'
        ids={1:[mk('10000101','agent')],2:[mk('10000102','agent')],3:[],4:[]}.get(q.page,[])
        return w.SearchBatch(ids,raw_count=len(ids))
    setup(monkeypatch,tmp_path,source)
    pre=run(w.preflight({'mode':'AGENT','filters':FILTERS,'pages_mode':'all','listings_mode':'all'}))
    assert pre['enumerated_agent_total']==2
    assert pre['enumeration_evidence']['role_scope']=='AGENT_ONLY'
    assert pre['agent_role_handling']=='SSR_ECHO_AND_ROLE_SAMPLE_ONLY'


def test_direct_agent_filter_fails_on_later_role_mismatch_and_falls_back(monkeypatch,tmp_path):
    async def source(q):
        if q.owner_type=='agent':
            b=w.SearchBatch([mk(f'10000{q.page:03d}','agent')],raw_count=1)
            if q.page==2:b.seller_role_mismatches=1
            return b
        ids=[mk('10000201','agent'),mk('10000202','physical')] if q.page==1 else []
        return w.SearchBatch(ids,raw_count=len(ids))
    setup(monkeypatch,tmp_path,source)
    pre=run(w.preflight({'mode':'AGENT','filters':FILTERS,'pages_mode':'all','listings_mode':'all'}))
    assert pre['enumerated_agent_total']==1
    assert pre['agent_role_handling']=='MIXED_FALLBACK_STRICT_ROLE_CLASSIFICATION'
    assert pre['enumeration_evidence']['raw_rows']==2


def test_more_than_eight_pages_and_two_empties(monkeypatch,tmp_path):
    visited=[]
    async def source(q):
        visited.append(q.page)
        if q.owner_type=='agent':
            raise w.SourceError('UPSTREAM_FILTER_MISMATCH','direct not available')
        ids=[mk(str(10005000+q.page),'agent')] if q.page<=12 else []
        return w.SearchBatch(ids,raw_count=len(ids))
    setup(monkeypatch,tmp_path,source)
    p=run(w.preflight({'mode':'AGENT','filters':FILTERS,'pages_mode':'all','listings_mode':'all'}))
    assert p['enumerated_agent_total']==12
    assert p['enumeration_evidence']['pages_visited']==13
    assert 13 in visited and 14 not in visited


def test_one_completed_comparison_is_one_progress_unit_and_failed_not_counted(monkeypatch,tmp_path):
    async def source(q):
        if q.owner_type=='agent':
            raise w.SourceError('UPSTREAM_FILTER_MISMATCH','fallback')
        ids=[mk('10006001','agent'),mk('10006002','agent'),mk('10006003','physical')] if q.page==1 else []
        return w.SearchBatch(ids,raw_count=len(ids))
    setup(monkeypatch,tmp_path,source)
    async def fake_match(self,row,q):
        if row['listing_id']=='10006001':
            row.update(matching_status='NOT_FOUND',candidate_coverage_complete=True,photo_coverage_complete=True)
        else:
            row.update(matching_status='POSSIBLE_MATCH',candidate_coverage_complete=False,photo_coverage_complete=False)
    monkeypatch.setattr(w.JobRuntime,'match',fake_match)
    async def go():
        pre=await w.preflight({'mode':'AGENT','filters':FILTERS,'pages_mode':'all','listings_mode':'all'})
        assert pre['scope_total_expected']==2
        result=await w.start_job({'preflight_id':pre['preflight_id']})
        await w.ACTIVE[result['job_id']]
        return w.load_state(result['job_id']),w.rows_for(result['job_id'])
    state,rows=run(go())
    assert state['status']=='PARTIAL'
    assert state['metrics']['agent_attempted']==2
    assert state['metrics']['listings_processed']==1
    assert state['metrics']['agent_incomplete']==1
    assert state['progress_percent']==50
    assert state['scope_total_expected']==2
    assert state['stop_reason']=='AGENT_SNAPSHOT_EXHAUSTED'
    assert rows[0]['processing_status']=='COMPARISON_COMPLETE'
    assert rows[1]['processing_status']=='COMPARISON_INCOMPLETE'


def test_all_completed_gets_one_hundred_percent(monkeypatch,tmp_path):
    async def source(q):
        if q.owner_type=='agent':
            raise w.SourceError('UPSTREAM_FILTER_MISMATCH','fallback')
        ids=[mk('10007001','agent'),mk('10007002','agent')] if q.page==1 else []
        return w.SearchBatch(ids,raw_count=len(ids))
    setup(monkeypatch,tmp_path,source)
    async def fake_match(self,row,q):
        row.update(matching_status='NOT_FOUND',candidate_coverage_complete=True,photo_coverage_complete=True)
    monkeypatch.setattr(w.JobRuntime,'match',fake_match)
    async def go():
        pre=await w.preflight({'mode':'AGENT','filters':FILTERS,'pages_mode':'all','listings_mode':'all'})
        result=await w.start_job({'preflight_id':pre['preflight_id']})
        await w.ACTIVE[result['job_id']]
        return w.load_state(result['job_id'])
    state=run(go())
    assert state['status']=='COMPLETED'
    assert state['metrics']['listings_processed']==2
    assert state['progress_percent']==100


def test_unconfirmed_end_does_not_fabricate_total(monkeypatch,tmp_path):
    async def source(q):
        if q.owner_type=='agent':
            raise w.SourceError('UPSTREAM_FILTER_MISMATCH','fallback')
        return w.SearchBatch([mk('10008001','agent')],raw_count=1)
    setup(monkeypatch,tmp_path,source)
    pre=run(w.preflight({'mode':'AGENT','filters':FILTERS,'pages_mode':'all','listings_mode':'all'}))
    assert getattr(pre,'status_code',None)==502
    assert b'REPEATED_SOURCE_PAGE' in pre.body


def test_scope_without_authorization_does_enumerate_to_two_empty(monkeypatch,tmp_path):
    monkeypatch.delenv('LISTINGLENS_AUTHORIZED_BULK',raising=False)
    w.DB_PATH=tmp_path/'small.sqlite3';w.PREP.clear();w.ACTIVE.clear()
    calls=[]
    async def source(q):
        calls.append(q.page)
        if q.page<=2:
            return w.SearchBatch([mk('1000900'+str(q.page),'agent')],raw_count=1)
        return w.SearchBatch([],raw_count=0)
    monkeypatch.setattr(w,'search_source',source)
    result=run(w.preflight({'mode':'AGENT','filters':FILTERS,'pages_mode':'all','listings_mode':'all'}))
    assert result['can_start']
    assert result['scope_total_expected']==2
    assert result['enumerated_agent_total']==2
    assert calls==[1,2,3]

# R3.1.5: distinguish the actual SSR-listed agent count from the additional
# local price validation, which is susceptible to FX conversion/rounding drift.
def test_r315_raw_ssr_agent_cards_counted_with_transparent_variances(monkeypatch,tmp_path):
    from helpers import _item, mock_html
    import os
    q=parse_query_pairs(FILTERS.items())
    q=replace(q,owner_type='agent')
    source_rows={
        1:[_item(q,21001000+i,499.5 if i<2 else 510) for i in range(24)],
        2:[_item(q,21002000+i,600.5 if i<2 else 510) for i in range(21)],
    }
    urls=[]
    async def stub(url):
        from urllib.parse import parse_qs,urlsplit
        params=parse_qs(urlsplit(url).query)
        page=int(params['page'][0]);urls.append(page)
        actual=parse_query_pairs([(k,v[0]) for k,v in params.items()])
        return mock_html(actual,rows=source_rows.get(page,[])),url,200
    monkeypatch.setattr(w,'fetch_html',stub)
    monkeypatch.setattr(w.asyncio,'sleep',fast_sleep)
    monkeypatch.setenv('LISTINGLENS_AUTHORIZED_BULK','1')
    w.DB_PATH=tmp_path/'r315.sqlite3';w.PREP.clear();w.ACTIVE.clear()
    first=run(w.search_source(q))
    assert len(first)==22 and first.raw_count==24
    assert len(first.agent_role_rows)==24
    assert sum(bool(x.get('source_filter_warning')) for x in first.agent_role_rows)==2
    pre=run(w.preflight({'mode':'AGENT','filters':FILTERS,'pages_mode':'all','listings_mode':'all'}))
    assert pre['enumerated_agent_total']==45
    assert pre['scope_total_expected']==45
    assert pre['enumeration_evidence']['agent_role_filter_variances']==4
    assert pre['enumeration_evidence']['raw_rows']==45
    assert pre['enumeration_evidence']['end_reason']=='FIRST_EMPTY_SSR_PAGE'
    assert pre['agent_role_handling']=='SSR_ECHO_AND_ROLE_SAMPLE_ONLY'
    assert [3]==urls[-1:]
    assert len(w.PREP[pre['preflight_id']]['agent_snapshot']['rows'])==45


def test_r315_mixed_fallback_counts_real_agents_even_if_local_price_drift(monkeypatch,tmp_path):
    async def source(q):
        if q.owner_type=='agent':
            raise w.SourceError('UPSTREAM_FILTER_MISMATCH','No direct agent SSR available')
        b=w.SearchBatch([mk('10011001','agent')],raw_count=2) if q.page==1 else w.SearchBatch([],raw_count=0)
        if q.page==1:
            b.raw_ids=['10011001','10011002']
            b.agent_role_rows=[mk('10011001','agent'),dict(mk('10011002','agent'),source_filter_warning='local USD drift')]
        else:
            b.raw_ids=[];b.agent_role_rows=[]
        return b
    setup(monkeypatch,tmp_path,source)
    pre=run(w.preflight({'mode':'AGENT','filters':FILTERS,'pages_mode':'all','listings_mode':'all'}))
    assert pre['enumerated_agent_total']==2
    assert pre['enumeration_evidence']['role_scope']=='AGENT_ONLY_FROM_MIXED'
    assert pre['enumeration_evidence']['agent_role_filter_variances']==1
