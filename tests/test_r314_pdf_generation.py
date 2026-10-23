"""No internet, no HTML fetch, no real seller data in unit tests."""
import json
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.pagesizes import A4

from listinglens import pdf_jobs
from listinglens.app import app


def test_validate_ids():
    assert pdf_jobs.parse_ids(['26387744']) == ['26387744']
    assert len(pdf_jobs.parse_ids([str(26000000+i) for i in range(10)])) == 10
    for ids in [[], ['123'], ['26387744','26387744'], ['26387744, 12345678'],
                ['../12345678'], ['26387744',''], ['0'*8], ['26387744 '],
                [str(26000000+i) for i in range(11)]]:
        with pytest.raises(ValueError): pdf_jobs.parse_ids(ids)


def test_home_has_third_tab_without_random():
    client = TestClient(app)
    html = client.get('/').text
    assert 'id="tab-pdf"' in html
    assert 'id="pdf-tab-panel"' in html
    assert 'id="pdf-ids"' in html
    assert 'id="tab-collection"' in html
    assert 'id="tab-single"' in html
    assert 'Random test' not in html


def test_empty_ids_rejected():
    client=TestClient(app)
    assert client.post('/api/pdf/jobs',json={'listing_ids':[]}).status_code==422
    assert client.post('/api/pdf/jobs',json={'listing_ids':['xx111111']}).status_code==422
    assert client.get('/api/pdf/jobs/doesnotexist').status_code==404


def make_pdf(path, id):
    c=Canvas(str(path),pagesize=A4)
    c.drawString(100,700, f'Listing ID {id} verified unit-test output')
    c.showPage();c.save()
    assert path.stat().st_size>1000


def test_offline_combining_multiple_results(tmp_path):
    ids=['26387744','26309856']
    job={'job_id':'test','directory':tmp_path,'ids':ids,
         'items':[{'listing_id':i,'status':'READY','reason':''} for i in ids],
         'download_ready':False}
    completed=[]
    for i in ids:
        directory=tmp_path/i;directory.mkdir()
        pdf=directory/f'MYHOME_{i}_PDF.pdf'
        make_pdf(pdf,i)
        meta=directory/'DATA_WITH_SOURCES.json'
        meta.write_text(json.dumps({'fields':{'listing_id':i}}))
        completed.append((i,pdf,meta))
    pdf_jobs._zip_outputs(job,completed)
    from pypdf import PdfReader
    assert len(PdfReader(str(tmp_path/'LISTINGLENS_COMBINED.pdf')).pages)==2
    with zipfile.ZipFile(tmp_path/'LISTINGLENS_PDF_RESULTS.zip') as z:
        assert len(z.namelist())==6
        assert f'PDFS/LISTINGLENS_{ids[0]}.pdf' in z.namelist()
        assert f'PDFS/LISTINGLENS_{ids[1]}.pdf' in z.namelist()
        assert 'MANIFEST.json' in z.namelist()
    assert job['download_ready'] is True


def test_r2_provenance_without_network():
    from listinglens.pdf_export import build_pdf
    assert build_pdf.money_meta('4 room, 1000 $, 110 m², 26387744 | Myhome','26387744')==1000
    assert build_pdf.money_meta('4 room, 1000 GEL, 26387744 | Myhome','26387744') is None
    assert build_pdf.safe_host('https://static-api-statements.tnet.ge/a.webp',build_pdf.CDN_HOSTS)
    assert not build_pdf.safe_host('https://example.com/a.webp',build_pdf.CDN_HOSTS)


def test_worker_partial_failure_and_safe_download(tmp_path, monkeypatch):
    ids=['26387744','26309856']
    job_id='offline-test-job'
    job={'job_id':job_id,'ids':ids,'directory':tmp_path,'status':'QUEUED','total':2,
         'processed':0,'success':0,'failed':0,'items':[],'current_id':None,
         'cancel_requested':False,'download_ready':False,'created_at':'test','completed_at':None}
    class Proc:
        def __init__(self, code):
            self.returncode=code
            self.stdout='';self.stderr=''
    def fake_subprocess_run(cmd, **kwargs):
        assert '--live' in cmd and '--id' in cmd
        i=cmd[cmd.index('--id')+1]
        if i=='26309856':return Proc(2)
        dst=Path(cmd[cmd.index('--output')+1]);dst.mkdir()
        path=dst/f'MYHOME_{i}_PDF.pdf'
        canvas=Canvas(str(path),pagesize=A4,pageCompression=0)
        for n in range(75):canvas.drawString(40,700-n*8,'Report PDF verified for listing '+i+' '*3)
        canvas.showPage();canvas.save()
        assert path.stat().st_size>3000
        (dst/'DATA_WITH_SOURCES.json').write_text(json.dumps({'fields':{'listing_id':i}}))
        return Proc(0)
    monkeypatch.setattr(pdf_jobs.subprocess,'run',fake_subprocess_run)
    pdf_jobs.JOBS[job_id]=job
    pdf_jobs.ACTIVE['job_id']=job_id
    try:
        pdf_jobs._work(job_id)
        assert job['status']=='PARTIAL'
        assert job['processed']==2 and job['success']==1 and job['failed']==1
        assert job['download_ready'] is True
        client=TestClient(app)
        assert client.get(f'/api/pdf/jobs/{job_id}/download?format=zip').status_code==200
        assert client.get(f'/api/pdf/jobs/{job_id}/download?format=pdf').status_code==200
        from pypdf import PdfReader
        assert len(PdfReader(str(tmp_path/'LISTINGLENS_COMBINED.pdf')).pages)==1
    finally:
        pdf_jobs.JOBS.pop(job_id,None)
        pdf_jobs.ACTIVE['job_id']=None
