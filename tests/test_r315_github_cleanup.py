"""Private repository release hygiene; never calls external websites."""
from pathlib import Path
from fastapi.testclient import TestClient
from listinglens.app import app

ROOT=Path(__file__).resolve().parents[1]

def test_docs_are_not_published():
    c=TestClient(app)
    for route in ('/docs','/redoc','/openapi.json','/capabilities'):
        assert c.get(route).status_code==404, route
    assert c.get('/health').status_code==200

def test_no_docs_or_health_card_in_page():
    h=(ROOT/'listinglens/web/index.html').read_text(encoding='utf-8')
    css=(ROOT/'listinglens/web/styles.css').read_text(encoding='utf-8')
    for term in ('/docs','Swagger','API DOCS','health-card','WORKFLOW HEALTH','id="conn"'):
        assert term not in h
    assert 'health-card' not in css
    assert 'header-docs' not in css
    for id in ('tab-collection','tab-single','tab-pdf','status-picker','source-search-link'):
        assert f'id="{id}"' in h

def test_no_removed_vendor_names_in_distribution():
    blocked=('parse'+'.bot','parse'+'-bot','parse'+'_bot','parse'+'bot','api.'+'parse'+'.bot')
    for p in ROOT.rglob('*'):
        if not p.is_file() or p.suffix not in ('.py','.js','.html','.css','.json','.md','.txt','.sh'):
            continue
        if p.name=='test_r315_github_cleanup.py':
            continue
        data=p.read_text(encoding='utf-8').lower()
        assert not any(x in data for x in blocked), p

def test_en_interface_and_no_random_test():
    h=(ROOT/'listinglens/web/index.html').read_text()
    assert 'Random test' not in h
    assert 'PDF Generator' in h
    assert 'Building status' in h
    assert '<html lang="en">' in h
