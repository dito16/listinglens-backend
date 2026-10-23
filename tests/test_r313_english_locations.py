"""New contracts introduced in R3.1.13, strictly offline."""
from pathlib import Path
import json,re
import pytest
from listinglens.locations import catalog,validate_location
from listinglens.workflow import parse_scope
from listinglens.search import SearchQuery,QueryIssue
ROOT=Path(__file__).resolve().parents[1]

VALID={'mode':'OWNER','filters':{'cities':'1','deal_types':'2','real_estate_types':'1','price_from':'700','price_to':'1000','room_types':'3,4','bedroom_types':'3,4','districts':'4'},'pages_mode':'all','listings_mode':'limit','row_limit':45,'page_from':1,'page_to':2}

def test_catalog_counts_and_hierarchy():
 cities=catalog()
 assert len(cities)==126
 assert sum(len(c['districts']) for c in cities)==3042
 assert sum(len(d['urbans']) for c in cities for d in c['districts'])==76
 assert next(c for c in cities if c['id']==1)['name']=='Tbilisi'

def test_valid_nested_district_and_microdistrict():
 assert validate_location({'cities':'1','districts':'4','urbans':'29'})
 assert validate_location({'cities':'15'})
 assert validate_location({'cities':'245'})

@pytest.mark.parametrize('filters',[{'cities':'1','districts':'6','urbans':'29'},{'cities':'1','urbans':'29'},{'cities':'15','districts':'4'},{'cities':'999999'}])
def test_reject_foreign_hierarchy(filters):
 with pytest.raises(ValueError):validate_location(filters)

def test_01_accepts_three_additional_filters():
 mode,q,scope=parse_scope(VALID)
 assert mode=='OWNER' and q.cities==(1,) and q.districts==(4,)
 assert 'owner_type=physical' in q.url()
 assert '/en/' in q.url()

def test_01_accepts_area_bounds_and_rooms_as_three():
 form=json.loads(json.dumps(VALID));form['filters'].pop('bedroom_types');form['filters'].pop('districts');form['filters']['area_from']='80';form['filters']['area_to']='110'
 assert parse_scope(form)[1].area_from==80

def test_01_for_all_city_ids():
 for city in ['1','15','96','245','73']:
  form=json.loads(json.dumps(VALID));form['filters']['cities']=city;form['filters'].pop('districts');form['filters']['area_from']='70';
  assert parse_scope(form)[1].cities==(int(city),)

@pytest.mark.parametrize('missing', ['cities','deal_types','real_estate_types','price_from','price_to'])
def test_01_rejects_missing_required(missing):
 form=json.loads(json.dumps(VALID));form['filters'].pop(missing)
 with pytest.raises(QueryIssue):parse_scope(form)

def test_01_rejects_two_extras():
 form=json.loads(json.dumps(VALID));form['filters'].pop('districts')
 with pytest.raises(QueryIssue, match='at least 3 additional filters'):parse_scope(form)

def test_01_rejects_bad_microdistrict():
 form=json.loads(json.dumps(VALID));form['filters']['urbans']='57'
 with pytest.raises(QueryIssue, match='microdistrict'):parse_scope(form)

def test_en_search_url_and_no_random():
 html=(ROOT/'listinglens/web/index.html').read_text()
 js=(ROOT/'listinglens/web/app.js').read_text()
 assert '<html lang="en">' in html
 assert 'Random test' not in html and 'random-test' not in js
 assert 'type="number"' not in html
 assert 'id="collect-validation"' in html
 assert 'id="status-picker"' in html
 assert all(x in html for x in ['Old building','New building','Under construction'])
 assert 'src="/assets/listinglens-logo.png"' in html
 assert not re.search('[А-Яа-яЁё]',html+js)
 for f in ROOT.glob('listinglens/**/*.py'):
  assert ('parse'+'.bot') not in f.read_text().lower()
 assert SearchQuery(cities=(1,),deal_types=(2,),real_estate_types=(1,)).url().startswith('https://www.myhome.ge/en/real-estate/rent/apartment/tbilisi/')
 assert SearchQuery(cities=(245,),deal_types=(2,),real_estate_types=(1,)).url().startswith('https://www.myhome.ge/en/')


def test_original_workflow_layout_retained():
 css=(ROOT/'listinglens/web/styles.css').read_text()
 html=(ROOT/'listinglens/web/index.html').read_text()
 assert '.tester-body{display:grid;grid-template-columns:340px minmax(0,1fr)' in css
 assert 'class="tester-card"' in html
 assert all(f'id="{x}"' in html for x in ('tab-collection','tab-single','tab-pdf'))
