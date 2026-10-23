"""First verified empty page closes all-page enumeration; HTTP500 does not."""
import asyncio
from listinglens import workflow as w
from helpers import mk

FILTERS={'cities':'1','deal_types':'2','real_estate_types':'1','currency_id':'2',
         'room_types':'4','bedroom_types':'3','price_from':'700','price_to':'720',
         'area_from':'90','area_to':'100'}


def preflight_payload(mode='AGENT'):
    return {'mode':mode,'filters':FILTERS,'pages_mode':'all','listings_mode':'all'}


def setup(monkeypatch,tmp_path,fn):
    w.DB_PATH=tmp_path/'first_empty.sqlite3'
    w.PREP.clear();w.ACTIVE.clear()
    monkeypatch.setattr(w,'search_source',fn)
    async def no_sleep(_):pass
    monkeypatch.setattr(w.asyncio,'sleep',no_sleep)


def gen(monkeypatch,tmp_path,mode='agent',n=12,error_page=None,error_status=500):
    calls=[]
    async def get(q):
        calls.append(q.page)
        if error_page == q.page:
            if error_status==500:
                raise w.SourceError('UPSTREAM_HTTP_ERROR','MyHome returned HTTP 500',502)
            raise w.SourceError('UPSTREAM_ACCESS_DENIED','MyHome returned HTTP 403',503)
        if q.page>n:
            return w.SearchBatch([],raw_count=0)
        item=mk(str(26100000+q.page),mode)
        batch=w.SearchBatch([item],raw_count=1)
        batch.raw_ids=[item['listing_id']]
        batch.agent_role_rows=[item] if mode=='agent' else []
        return batch
    setup(monkeypatch,tmp_path,get)
    return calls


def run(coro):return asyncio.run(coro)


def test_agent_first_empty_after_twelve_pages_unbounded(monkeypatch,tmp_path):
    calls=gen(monkeypatch,tmp_path,n=12)
    res=run(w.preflight(preflight_payload()))
    assert res['enumerated_agent_total']==12
    assert res['enumeration_evidence']['pages_visited']==13
    assert res['enumeration_evidence']['end_reason']=='FIRST_EMPTY_SSR_PAGE'
    assert res['enumeration_evidence']['complete'] is True
    assert calls==list(range(1,14))


def test_owner_first_empty_after_eight_pages_unbounded(monkeypatch,tmp_path):
    calls=gen(monkeypatch,tmp_path,mode='physical',n=8)
    res=run(w.preflight(preflight_payload('OWNER')))
    assert res['enumerated_owner_total']==8
    assert res['enumeration_evidence']['pages_visited']==9
    assert res['enumeration_evidence']['end_reason']=='FIRST_EMPTY_SSR_PAGE'
    assert calls==list(range(1,10))


def test_agent_page_six_http500_nonblocking_partial(monkeypatch,tmp_path):
    calls=gen(monkeypatch,tmp_path,error_page=6)
    res=run(w.preflight(preflight_payload()))
    assert res['source_total_status']=='PARTIAL_AGENT_SNAPSHOT'
    assert res['partial_agent_rows']==5
    assert res['enumerated_agent_total'] is None
    assert res['enumeration_evidence']['complete'] is False
    assert res['enumeration_evidence']['failed_page']==6
    assert res['can_start']
    assert calls==[1,2,3,4,5,6]


def test_owner_page_six_http500_nonblocking_partial(monkeypatch,tmp_path):
    calls=gen(monkeypatch,tmp_path,mode='physical',error_page=6)
    async def exercise():
        res=await w.preflight(preflight_payload('OWNER'))
        assert res['source_total_status']=='PARTIAL_OWNER_SNAPSHOT'
        assert res['partial_owner_rows']==5
        assert res['enumerated_owner_total'] is None
        assert res['scope_total_expected']==5
        assert res['enumeration_evidence']['complete'] is False
        assert res['enumeration_evidence']['failed_page']==6
        assert res['can_start']
        started=await w.start_job({'preflight_id':res['preflight_id']})
        await w.ACTIVE[started['job_id']]
        state=w.load_state(started['job_id'])
        assert state['status']=='PARTIAL'
        assert state['stop_reason']=='UPSTREAM_HTTP_500_UNVERIFIED_END'
        assert state['metrics']['rows_collected']==5
        assert state['scope_total_actual'] is None
        return state
    run(exercise())
    assert calls==[1,2,3,4,5,6]


def test_http500_first_page_is_error_not_empty(monkeypatch,tmp_path):
    calls=gen(monkeypatch,tmp_path,error_page=1)
    res=run(w.preflight(preflight_payload()))
    assert res.status_code==502
    assert calls==[1,1]  # Direct agent check then mixed fallback, both fail


def test_403_never_disguised_as_empty(monkeypatch,tmp_path):
    calls=gen(monkeypatch,tmp_path,error_page=6,error_status=403)
    res=run(w.preflight(preflight_payload()))
    assert res.status_code==503
    assert calls==[1,2,3,4,5,6]
