"""Offline integration and regression contracts for the two-stage matcher."""
import asyncio
from io import BytesIO
from PIL import Image
from listinglens.matcher import fingerprint
from listinglens.sift_matcher import extract_features,compare_features,RULE_VERSION
from listinglens import workflow as w


def photo(v):
    im=Image.new('RGB',(200,130),(v,v,v))
    out=BytesIO();im.save(out,format='PNG');return fingerprint(out.getvalue())


def state():
    return {'cancel_requested':False,'metrics':{'image_pairs_attempted':0,
        'image_pairs_failed':0,'image_comparisons':0}}


def test_strict_fast_threshold_skips_sift(monkeypatch):
    s=state(); rt=w.JobRuntime(s)
    rt.cancelled=lambda:False
    pictures={'s':photo(80),'c':photo(80)}
    async def get(client,url):return pictures[url]
    rt.photo=get
    async def forbidden(*_):raise AssertionError('SIFT must not run after hist match')
    rt.sift_features=forbidden
    proof,ok,d=asyncio.run(w.compare_photo_sets(rt,None,['s'],['c'],'owner_photo','agent_photo'))
    assert ok and proof['method']=='HIST_PHASH' and proof['similarity']>0.95
    assert d['sift_pairs_compared']==0


def test_fallback_wins_without_fabricated_percent(monkeypatch):
    s=state();rt=w.JobRuntime(s);rt.cancelled=lambda:False
    photos={'s0':photo(80),'s1':photo(99),'c0':photo(140),'c1':photo(170)}
    seen=[]
    async def get(client,url):seen.append(url);return photos[url]
    async def feats(url,fp):return url
    rt.photo=get;rt.sift_features=feats
    monkeypatch.setattr(w,'scores',lambda *_:{'hist_correlation':0.94,'hash_distance':0})
    monkeypatch.setattr(w,'compare_features',lambda a,b:{'match':a=='s1' and b=='c0',
        'method':'SIFT_RANSAC','sift_ransac_inliers':78,'sift_inlier_ratio':.69,
        'sift_geometry_valid':True,'rule':RULE_VERSION})
    proof,ok,d=asyncio.run(w.compare_photo_sets(rt,None,['s0','s1'],['c0','c1'],
        'owner_photo','agent_photo'))
    assert ok and proof['method']=='SIFT_RANSAC'
    assert (proof['owner_photo'],proof['agent_photo'])==(1,0)
    assert 'similarity' not in proof and 'hist_correlation' not in proof
    assert d['compared_pairs']==4 and d['sift_pairs_compared']==3


def test_exact_095_does_not_stop_first_pass(monkeypatch):
    s=state();rt=w.JobRuntime(s);rt.cancelled=lambda:False
    async def get(client,url):return photo(90)
    async def feats(url,fp):return None
    rt.photo=get;rt.sift_features=feats
    monkeypatch.setattr(w,'scores',lambda *_:{'hist_correlation':0.95,'hash_distance':0})
    hit,ok,diag=asyncio.run(w.compare_photo_sets(rt,None,['s'],['c'],'agent_photo','owner_photo'))
    assert hit is None and ok and diag['sift_pairs_compared']==1


def test_fallback_checks_only_first_three_source_photos(monkeypatch):
    s=state();rt=w.JobRuntime(s);rt.cancelled=lambda:False
    seen=[]
    async def get(client,url):seen.append(url);return photo(100)
    async def feats(url,fp):return url
    rt.photo=get;rt.sift_features=feats
    monkeypatch.setattr(w,'scores',lambda *_:{'hist_correlation':0.1,'hash_distance':20})
    monkeypatch.setattr(w,'compare_features',lambda a,b:{'match':False})
    hit,ok,diag=asyncio.run(w.compare_photo_sets(rt,None,['s1','s2','s3','s4'],['c1','c2'],'agent_photo','owner_photo'))
    assert hit is None and ok and 's4' not in seen
    assert diag['sift_pairs_compared']==6


def test_error_loading_not_marked_complete(monkeypatch):
    s=state();rt=w.JobRuntime(s);rt.cancelled=lambda:False
    async def get(client,url):return None if url=='bad' else photo(120)
    async def feats(url,fp):return None
    rt.photo=get;rt.sift_features=feats
    monkeypatch.setattr(w,'scores',lambda *_:{'hist_correlation':0.1,'hash_distance':30})
    hit,ok,diag=asyncio.run(w.compare_photo_sets(rt,None,['s'],['bad','good'],'owner_photo','agent_photo'))
    assert hit is None and not ok and diag['failed_pairs']==1


def test_sift_different_blank_frames_do_not_match():
    one=extract_features(photo(60));two=extract_features(photo(200))
    result=compare_features(one,two)
    assert not result['match']
    assert result['method']=='SIFT_RANSAC' and result['rule']==RULE_VERSION


def test_schema_match_method_in_csv_xlsx():
    assert 'match_method' in w.FIELDS
    assert 'match_evidence' in w.FIELDS
