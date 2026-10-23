from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def test_clean_ui_keeps_activity_only_during_real_work():
    html=(ROOT/'listinglens/web/index.html').read_text()
    js=(ROOT/'listinglens/web/app.js').read_text()
    css=(ROOT/'listinglens/web/origin.css').read_text()
    assert 'id="area_types"' not in html
    assert 'Тип площади' not in html
    assert "'area_types'" not in js
    assert 'id="job-state"' in html and 'id="preflight-summary"' in html
    assert "setLoading(true)" in js
    assert 'setLoading(false)' in js
    assert 'setLoading(!finished)' in js
    assert '#job-state.is-loading::before' in css
    assert '#preflight-summary.is-loading::before' in css
    assert 'prefers-reduced-motion' in css
    assert 'NO EXTERNAL.API' not in html
    assert 'Внутренних лимитов' not in html
    assert 'PDF по ID' not in html


def test_r317_one_search_no_old_tiers():
    src=(ROOT/'listinglens/workflow.py').read_text()
    assert 'Tier two relaxes' not in src
    assert 'tiers=[' not in src
    assert 'agent_urls=list(agent.get(\'image_urls\') or [])[:3]' in src
    assert 'similarity>0.95' in src
