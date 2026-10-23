"""OFFLINE ONLY. The statement fixtures here are fabricated to test parsing,
not to pretend the detail endpoint has passed live acceptance.
"""
import json
from urllib.parse import urlsplit
import pytest
from fastapi.testclient import TestClient
from listinglens.app import app
from listinglens.detail import detail_url,validate_listing_id,extract_detail,normalize_detail
from listinglens.parser import SourceError

ID='25049178'
BASE=detail_url(ID)

@pytest.fixture
def statement():
    # All numbers and labels intentionally differ from the user's external API example.
    return {'id':int(ID),'dynamic_title':'OFFLINE SYNTHETIC PROPERTY',
      'dynamic_slug':'fixture-sample', 'href_lang':{'en':'synthetic-property','ru':'sinteticheskoe-ob-iavlenie'},
      'real_estate_type_id':1,'deal_type_id':1,'price':{'1':{'price_total':2020,'price_square':51},'2':{'price_total':700,'price_square':18}},
      'area':39,'room':2,'bedroom':1,'status_id':2,'condition_id':1,'metro_station_id':2,
      'city_name':'TEST CITY','district_name':'TEST DISTRICT','urban_name':'TEST URBAN',
      'address':'TEST STREET','floor':3,'total_floors':5,
      'user_type':{'type':'physical'},'user_title':'FAKE_USER',
      'comment':'OFFLINE FIXTURE, NOT A REAL LISTING','lat':41.2,'lng':44.1,
      'is_vip':False,'price_negotiable':False,
      'last_updated':'2026-01-01 10:10:10',
      'images':[{'large':'https://static-api-statements.tnet.ge/test.webp'}]}

def html_with(st,kind='queries'):
    if kind=='queries':
      pp={'dehydratedState':{'queries':[{'queryKey':['statement','detail',{'id':ID}], 'state':{'data':{'result':True,'data':{'statement':st}}}}]}}
    elif kind=='pageProps':pp={'statement':st}
    elif kind=='missing':pp={'dehydratedState':{'queries':[]}}
    elif kind=='search_only':pp={'dehydratedState':{'queries':[{'queryKey':['statements','list'], 'state':{'data':{'data':{'data':[st]}}}}]}}
    else:raise AssertionError()
    return '<html><script id="__NEXT_DATA__" type="application/json">'+json.dumps({'props':{'pageProps':pp}})+'</script></html>'

@pytest.mark.parametrize('value',['','0','123','abc','1234x','-25049178','00001','1'*13,'25/049178','25049178?x=1',None])
def test_invalid_ids(value):
  with pytest.raises(ValueError):validate_listing_id(value)

@pytest.mark.parametrize('kind',['queries','pageProps'])
def test_parse_statement_by_id_only(statement,kind):
  assert extract_detail(html_with(statement,kind),ID,BASE)['id']==int(ID)


def test_missing_and_search_result_never_substitute_detail(statement):
  for kind in ['missing','search_only']:
    with pytest.raises(SourceError) as exc:extract_detail(html_with(statement,kind),ID,BASE)
    assert exc.value.code=='UPSTREAM_DETAIL_MISSING'


def test_id_mismatch_fails_closed(statement):
  st=statement.copy();st['id']=98765432
  with pytest.raises(SourceError) as exc:extract_detail(html_with(st),ID,BASE)
  assert exc.value.code=='UPSTREAM_LISTING_MISMATCH'


def test_url_mismatch_fails(statement):
  with pytest.raises(SourceError) as exc:extract_detail(html_with(statement),ID,'https://www.myhome.ge/ru/nedvizhimost/5555555')
  assert exc.value.code=='UPSTREAM_REDIRECT'


def test_price_image_labels(statement):
  obj=normalize_detail(statement,ID)
  assert obj['listing_id']==ID
  assert (obj['price_usd'],obj['price_gel'])==(700,2020)
  assert (obj['price_per_sqm_usd'],obj['price_per_sqm_gel'])==(18,51)
  assert obj['url']=='https://www.myhome.ge/en/real-estate/synthetic-property-'+ID+'/'
  assert obj['condition']=='Newly Renovated' and obj['nearest_metro']=='Vazha Pshavela'
  assert obj['image_urls']==['https://static-api-statements.tnet.ge/test.webp']
  assert (obj['latitude'],obj['longitude'])==(41.2,44.1)
  assert obj['seller_name']=='FAKE_USER'
  assert obj['is_vip'] is False


def test_absent_fields_are_null(statement):
  minimal={'id':int(ID)}
  data=normalize_detail(minimal,ID)
  assert data['price_usd'] is None and data['condition'] is None
  assert data['image_urls']==[] and data['description'] is None
  assert data['seller_name'] is None and data['is_vip'] is None


def test_no_arbitrary_or_http_image_urls(statement):
  statement['images']=[{'large':'http://static.my.ge/bad.jpg'},'https://evil.example/path','https://static.my.ge/good.jpg']
  assert normalize_detail(statement,ID)['image_urls']==['https://static.my.ge/good.jpg']


def test_full_api_offline_one_fresh_fetch_per_click(monkeypatch,statement):
  from listinglens import app as module
  calls=[]
  async def fake_fetch(url):
    calls.append(url)
    return html_with(statement),url,200
  monkeypatch.setattr(module,'fetch_html',fake_fetch)
  client=TestClient(app)
  for _ in range(2):
    response=client.get('/get_listing',params={'listing_id':ID})
    assert response.status_code==200
    body=response.json()
    assert body['status']=='success' and body['source']=='myhome.ge'
    assert body['data']['listing_id']==ID and body['detail_id_verified'] is True
    assert body['data']['description']=='OFFLINE FIXTURE, NOT A REAL LISTING'
  assert calls==[BASE,BASE]


def test_alias_and_no_listing_id_rejected_without_network(monkeypatch):
  from listinglens import app as module
  async def cant_fetch(_):raise AssertionError('must not fetch without a valid ID')
  monkeypatch.setattr(module,'fetch_html',cant_fetch)
  client=TestClient(app)
  for path in ['/get_listing','/v1/get_listing']:
    r=client.get(path)
    assert r.status_code==422 and r.json()['error_code']=='INVALID_LISTING_ID'
    assert client.get(path,params={'listing_id':'abc'}).status_code==422


def test_network_denial_is_not_a_success(monkeypatch):
  from listinglens import app as module
  async def denied(_):raise SourceError('UPSTREAM_ACCESS_DENIED','HTTP 403',503)
  monkeypatch.setattr(module,'fetch_html',denied)
  r=TestClient(app).get('/get_listing',params={'listing_id':ID})
  assert r.status_code==503 and r.json()['detail_id_verified'] is False


def test_wrong_id_upstream_status_502(monkeypatch,statement):
  from listinglens import app as module
  wrong=statement.copy();wrong['id']=12345678
  async def fake(url):return html_with(wrong),url,200
  monkeypatch.setattr(module,'fetch_html',fake)
  r=TestClient(app).get('/get_listing',params={'listing_id':ID})
  assert r.status_code==502 and r.json()['error_code']=='UPSTREAM_LISTING_MISMATCH'


def test_no_snapshot_or_externalapi_in_frontend():
  client=TestClient(app)
  html=client.get('/').text
  js=client.get('/assets/app.js').text
  assert 'Origin Match' in html
  assert 'preflight' in js
  assert client.get('/get_listing',params={'listing_id':'bad'}).status_code==422
  assert 'external.example/scraper/' not in html

def test_detail_redirect_host_and_id_are_strict():
  from listinglens.transport import _valid_detail_redirect
  origin='https://www.myhome.ge/en/pr/'+ID+'/'
  assert _valid_detail_redirect(origin,'https://www.myhome.ge/en/real-estate/sample-'+ID+'/')
  assert not _valid_detail_redirect(origin,'https://evil.example/en/real-estate/sample-'+ID+'/')
  assert not _valid_detail_redirect(origin,'https://www.myhome.ge/en/real-estate/sample-12345678/')
  assert not _valid_detail_redirect(origin,'https://www.myhome.ge/ru/nedvizhimost/')
  assert not _valid_detail_redirect(origin,'http://www.myhome.ge/en/real-estate/sample-'+ID+'/')
  assert not _valid_detail_redirect('https://www.myhome.ge/ru/nedvizhimost/','https://www.myhome.ge/en/real-estate/sample-'+ID+'/')


def test_user_requested_numeric_route_for_any_id():
    from listinglens.detail import detail_url
    assert detail_url('26369131')=='https://www.myhome.ge/en/pr/26369131/'
    assert detail_url('25049178')=='https://www.myhome.ge/en/pr/25049178/'

def test_source_redirect_validation_matches_numeric_id_only():
    from listinglens.transport import _valid_detail_redirect
    from listinglens.parser import SourceError
    origin='https://www.myhome.ge/en/pr/26369131/'
    assert _valid_detail_redirect(origin,'https://www.myhome.ge/en/real-estate/26369131/')
    assert _valid_detail_redirect(origin,'https://www.myhome.ge/ru/nedvizhimost/example-property-26369131/')
    assert _valid_detail_redirect(origin,'https://www.myhome.ge/en/real-estate/example-property-26369131/')
    for bad in ('https://www.myhome.ge/ru/nedvizhimost/example-property-26369132/',
                'https://www.myhome.ge/ru/nedvizhimost/',
                'https://example.com/ru/nedvizhimost/example-property-26369131/',
                'https://www.myhome.ge/ka/pr/26369131'):
        assert not _valid_detail_redirect(origin,bad)
