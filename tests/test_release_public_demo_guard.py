import pytest
from listinglens.workflow import parse_scope
from listinglens.search import QueryIssue

FILTERS={'cities':'1','deal_types':'2','real_estate_types':'1','currency_id':'2',
         'room_types':'4','bedroom_types':'3','price_from':'700','price_to':'1000',
         'area_from':'90','area_to':'130'}

def test_public_demo_rejects_unbounded_scope(monkeypatch):
    monkeypatch.setenv('LISTINGLENS_PUBLIC_DEMO','1')
    with pytest.raises(QueryIssue,match='Public demo'):
        parse_scope({'mode':'AGENT','filters':FILTERS,
                     'pages_mode':'all','listings_mode':'all'})

def test_public_demo_allows_bounded_scope(monkeypatch):
    monkeypatch.setenv('LISTINGLENS_PUBLIC_DEMO','1')
    mode,q,scope=parse_scope({'mode':'AGENT','filters':FILTERS,
               'pages_mode':'range','page_from':1,'page_to':5,
               'listings_mode':'limit','row_limit':100})
    assert mode=='AGENT' and scope.last==5 and scope.row_limit==100

def test_local_unbounded_scope_unchanged(monkeypatch):
    monkeypatch.delenv('LISTINGLENS_PUBLIC_DEMO',raising=False)
    mode,q,scope=parse_scope({'mode':'AGENT','filters':FILTERS,
               'pages_mode':'all','listings_mode':'all'})
    assert scope.pages_mode=='all' and scope.row_limit is None
