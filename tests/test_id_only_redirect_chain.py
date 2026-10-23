"""Offline redirect-chain contract tests; never presented as live evidence."""
import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from urllib.parse import urlsplit

import pytest
from listinglens.detail import extract_detail, detail_url
from listinglens.parser import SourceError
from listinglens.transport import fetch_html, _valid_detail_redirect

ID='26369131'
BASE=detail_url(ID)
MID=f'https://www.myhome.ge/en/real-estate/{ID}/'
FULL=f'https://www.myhome.ge/en/real-estate/2-room-apartment-for-rent-in-nadzaladevi-{ID}/'

def document():
    statement={'id':int(ID),'room':2,'bedroom':1,'dynamic_title':'offline fixture'}
    payload={'data':{'statement':statement}}
    body={'props': {'pageProps': {'dehydratedState': {
        'queries': [{'state': {'data': payload}}]
    }}}}
    return '<script id="__NEXT_DATA__" type="application/json">'+json.dumps(body)+'</script>'

def fake_session(monkeypatch, responses):
    calls=[]
    class Session:
        def __init__(self,**kwargs): pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return False
        async def get(self,url,**kwargs):
            assert not kwargs.get('allow_redirects')
            calls.append(url)
            if url not in responses:raise AssertionError(f'Unexpected url: {url}')
            status,loc,html=responses[url]
            return SimpleNamespace(status_code=status,headers={'Location':loc} if loc else {},text=html,url=url)
    pkg=ModuleType('curl_cffi')
    sub=ModuleType('curl_cffi.requests')
    sub.AsyncSession=Session
    pkg.requests=sub
    monkeypatch.setitem(sys.modules,'curl_cffi',pkg)
    monkeypatch.setitem(sys.modules,'curl_cffi.requests',sub)
    return calls

def test_301_to_numeric_then_full_slug(monkeypatch):
    calls=fake_session(monkeypatch,{BASE:(301,MID,''),MID:(302,FULL,''),FULL:(200,'',document())})
    html,final,status=asyncio.run(fetch_html(BASE))
    assert status==200 and final==FULL
    assert calls==[BASE,MID,FULL]
    assert extract_detail(html,ID,final)['id']==int(ID)

def test_numeric_en_detail_no_second_redirect(monkeypatch):
    calls=fake_session(monkeypatch,{BASE:(301,MID,''),MID:(200,'',document())})
    html,final,status=asyncio.run(fetch_html(BASE))
    assert (final,status)==(MID,200)
    assert extract_detail(html,ID,final)['id']==int(ID)
    assert len(calls)==2

@pytest.mark.parametrize('redirect',[
    'https://www.myhome.ge/en/real-estate/26369132/',
    'https://evil.example/en/real-estate/26369131/',
    'http://www.myhome.ge/en/real-estate/26369131/',
    'https://www.myhome.ge/en/real-estate/26369131/?session=secret',
    'https://www.myhome.ge/en/real-estate/',
])
def test_untrusted_redirect_rejected_before_next_get(monkeypatch,redirect):
    calls=fake_session(monkeypatch,{BASE:(301,redirect,'')})
    with pytest.raises(SourceError) as e:asyncio.run(fetch_html(BASE))
    assert e.value.code=='UPSTREAM_REDIRECT'
    assert calls==[BASE]

def test_redirect_limit(monkeypatch):
    x1=f'https://www.myhome.ge/en/real-estate/example-{ID}/'
    x2=f'https://www.myhome.ge/ru/nedvizhimost/example-{ID}/'
    calls=fake_session(monkeypatch,{BASE:(301,MID,''),MID:(302,x1,''),x1:(301,x2,''),x2:(301,BASE,'')})
    with pytest.raises(SourceError) as e:asyncio.run(fetch_html(BASE))
    assert e.value.code=='UPSTREAM_REDIRECT_LIMIT'
    assert len(calls)==4

@pytest.mark.parametrize('status',[401,403,429])
def test_access_refusal_stops_immediately(monkeypatch,status):
    calls=fake_session(monkeypatch,{BASE:(status,'','')})
    with pytest.raises(SourceError) as e:asyncio.run(fetch_html(BASE))
    assert e.value.code=='UPSTREAM_ACCESS_DENIED' and len(calls)==1

def test_search_redirects_remain_forbidden(monkeypatch):
    search='https://www.myhome.ge/ru/nedvizhimost/arenda/?deal_types=2'
    calls=fake_session(monkeypatch,{search:(301,BASE,'')})
    with pytest.raises(SourceError) as e:asyncio.run(fetch_html(search))
    assert e.value.code=='UPSTREAM_REDIRECT' and len(calls)==1

def test_slug_validity():
    assert _valid_detail_redirect(BASE,MID)
    assert _valid_detail_redirect(BASE,FULL)
    assert _valid_detail_redirect(MID,FULL)
    assert not _valid_detail_redirect(BASE,FULL.replace(ID,'26369132'))
