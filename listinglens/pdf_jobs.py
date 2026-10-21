"""Local, bounded batch PDF generation from public MyHome details.

One active batch at a time, one HTML page per listing, up to six photos and
nine OpenStreetMap tiles each. Never use persisted fixtures or contact data.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix='/api/pdf', tags=['PDF Generator'])
BASE = Path(__file__).resolve().parent.parent / 'data' / 'pdf_jobs'
PDF_SCRIPT = Path(__file__).resolve().parent / 'pdf_export' / 'build_pdf.py'
MAX_LISTINGS = 10
JOBS = {}
LOCK = threading.RLock()
EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix='listinglens-pdf')
ACTIVE = {'job_id': None}
TERMINAL = {'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED'}


class PDFRequest(BaseModel):
    listing_ids: list[str]


def parse_ids(value):
    """Never coerce floats or accept extra parameters, paths, or duplicate IDs."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_LISTINGS:
        raise ValueError('Provide 1 to 10 MyHome listing IDs.')
    if not all(isinstance(v, str) and re.fullmatch(r'[1-9][0-9]{5,11}', v) for v in value):
        raise ValueError('Each ID must contain 6-12 decimal digits (no spaces or other characters).')
    if len(set(value)) != len(value):
        raise ValueError('Duplicate IDs are not allowed.')
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


def status_snapshot(job):
    return {k: (list(job[k]) if k == 'items' else job[k]) for k in (
        'job_id', 'status', 'total', 'processed', 'success', 'failed', 'current_id',
        'items', 'download_ready', 'cancel_requested', 'created_at', 'completed_at'
    )}


def _zip_outputs(job, completed):
    if not completed:
        return
    from pypdf import PdfReader, PdfWriter
    directory = job['directory']
    merged = directory / 'LISTINGLENS_COMBINED.pdf'
    writer = PdfWriter()
    for item_id, path, _metadata in completed:
        reader = PdfReader(str(path))
        if len(reader.pages) != 1:
            raise ValueError(f'PDF for {item_id} must have exactly 1 page; got {len(reader.pages)}')
        writer.append(reader)
    with merged.open('wb') as file:
        writer.write(file)
    with (directory / 'MANIFEST.json').open('w', encoding='utf-8') as out:
        json.dump({'created_at': now(), 'requested_ids': job['ids'],
                   'individual_pdfs': [f'LISTINGLENS_{lid}.pdf' for lid, _, _ in completed],
                   'results': job['items'], 'combined_page_count': len(completed)},
                  out, ensure_ascii=False, indent=2)
    with zipfile.ZipFile(directory / 'LISTINGLENS_PDF_RESULTS.zip', 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for lid, src, meta in completed:
            archive.write(src, f'PDFS/LISTINGLENS_{lid}.pdf')
            archive.write(meta, f'METADATA/LISTINGLENS_{lid}.json')
        archive.write(merged, merged.name)
        archive.write(directory/'MANIFEST.json', 'MANIFEST.json')
    if (directory / 'LISTINGLENS_PDF_RESULTS.zip').stat().st_size > 150_000_000:
        raise ValueError('Batch ZIP exceeds the 150 MB output cap')
    job['download_ready'] = True


def _safe_error(stderr):
    """A short public reason only; never return raw HTML, seller data or traceback."""
    message = str(stderr or '')[-1000:].upper()
    if 'HTTP_403' in message or 'HTTP 403' in message:
        return 'MyHome refused this listing (HTTP 403)'
    if 'HTTP_429' in message or 'HTTP 429' in message:
        return 'MyHome rate limit reached (HTTP 429)'
    if 'LISTING_ID_NOT_IN_NEXT_DATA' in message:
        return 'Listing ID could not be verified on its page'
    if 'HTML_TOO_LARGE' in message:
        return 'Listing page was too large'
    if 'MISSING_DEPENDENCY' in message:
        return 'PDF dependency missing from Python environment'
    return 'Live listing data or PDF generation unavailable'


def _work(job_id):
    with LOCK:
        job = JOBS[job_id]
        job['status'] = 'RUNNING'
        directory = job['directory']
    completed = []
    try:
        for listing_id in job['ids']:
            with LOCK:
                if job['cancel_requested']:
                    break
                job['current_id'] = listing_id
            dst = directory / listing_id
            # An isolated subprocess prevents one broken listing from aborting a batch.
            # The --live flag is mandatory: no fixture is used in public PDF generation.
            cmd = [sys.executable, str(PDF_SCRIPT), '--live', '--id', listing_id,
                   '--output', str(dst)]
            try:
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, timeout=175, check=False)
                pdf = dst / f'MYHOME_{listing_id}_PDF.pdf'
                meta = dst / 'DATA_WITH_SOURCES.json'
                if proc.returncode or not pdf.is_file() or not meta.is_file():
                    raise RuntimeError(_safe_error(proc.stderr))
                if not 3_000 <= pdf.stat().st_size < 35_000_000:
                    raise RuntimeError('Generated PDF size was outside expected bounds')
                # Metadata is allowed only after validating origin listing ID.
                metadata = json.loads(meta.read_text(encoding='utf-8'))
                if str(metadata.get('fields', {}).get('listing_id')) != listing_id:
                    raise RuntimeError('Listing ID mismatch in PDF output')
                completed.append((listing_id, pdf, meta))
                item = {'listing_id': listing_id, 'status': 'READY', 'reason': ''}
                with LOCK:
                    job['success'] += 1
            except subprocess.TimeoutExpired:
                item = {'listing_id': listing_id, 'status': 'FAILED', 'reason': 'Timed out while loading this listing'}
                with LOCK:
                    job['failed'] += 1
            except Exception as exc:
                # Never expose responses, seller details or internal paths to the UI.
                allowed={'Live listing data or PDF generation unavailable',
                         'Listing ID could not be verified on its page',
                         'Listing page was too large',
                         'PDF dependency missing from Python environment',
                         'MyHome refused this listing (HTTP 403)',
                         'MyHome rate limit reached (HTTP 429)'}
                item = {'listing_id': listing_id, 'status': 'FAILED', 'reason':
                        str(exc) if str(exc) in allowed else 'Could not extract or generate a verified PDF'}
                with LOCK:
                    job['failed'] += 1
            finally:
                # Page text can contain private contacts; do not retain it on disk.
                for file in dst.glob('*visible_text*.txt') if dst.exists() else []:
                    file.unlink(missing_ok=True)
            with LOCK:
                job['items'].append(item)
                job['processed'] += 1
                job['current_id'] = None
        with LOCK:
            job['status'] = 'FINALIZING'
        _zip_outputs(job, completed)
        with LOCK:
            if job['cancel_requested']:
                job['status'] = 'CANCELLED'
            elif job['success'] == job['total']:
                job['status'] = 'COMPLETED'
            elif job['success'] > 0:
                job['status'] = 'PARTIAL'
            else:
                job['status'] = 'FAILED'
    except Exception:
        with LOCK:
            job['status'] = 'FAILED' if not job['download_ready'] else 'PARTIAL'
    finally:
        with LOCK:
            job['current_id'] = None
            job['completed_at'] = now()
            ACTIVE['job_id'] = None


@router.post('/jobs', status_code=202)
async def create_pdf_job(payload: PDFRequest):
    try:
        ids = parse_ids(payload.listing_ids)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={'error_code': 'INVALID_PDF_IDS', 'detail': str(exc)})
    with LOCK:
        if ACTIVE['job_id'] is not None:
            return JSONResponse(status_code=409, content={'error_code': 'PDF_BATCH_BUSY',
                   'detail': 'Another PDF generation batch is running. Wait for completion.'})
        job_id = uuid.uuid4().hex
        job_dir = BASE / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        job = {'job_id': job_id, 'ids': list(ids), 'directory': job_dir,
               'status': 'QUEUED', 'total': len(ids), 'processed': 0, 'success': 0,
               'failed': 0, 'items': [], 'current_id': None, 'cancel_requested': False,
               'download_ready': False, 'created_at': now(), 'completed_at': None}
        JOBS[job_id] = job
        ACTIVE['job_id'] = job_id
        EXECUTOR.submit(_work, job_id)
    return {'job_id': job_id, 'total': len(ids), 'status': 'QUEUED'}


@router.get('/jobs/{job_id}')
async def get_pdf_job(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return JSONResponse(status_code=404, content={'error_code': 'PDF_JOB_NOT_FOUND'})
        return status_snapshot(job)


@router.post('/jobs/{job_id}/cancel')
async def cancel_pdf_job(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return JSONResponse(status_code=404, content={'error_code': 'PDF_JOB_NOT_FOUND'})
        if job['status'] not in TERMINAL:
            job['cancel_requested'] = True
        return {'status': job['status'], 'cancel_requested': job['cancel_requested']}


@router.get('/jobs/{job_id}/download')
async def download_pdf_job(job_id: str, format: str = 'zip'):
    if format not in {'zip', 'pdf'}:
        return JSONResponse(status_code=422, content={'error_code': 'INVALID_EXPORT_FORMAT'})
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return JSONResponse(status_code=404, content={'error_code': 'PDF_JOB_NOT_FOUND'})
        if not job['download_ready']:
            return JSONResponse(status_code=409, content={'error_code': 'PDF_NOT_READY'})
        file = job['directory'] / ('LISTINGLENS_PDF_RESULTS.zip' if format == 'zip' else 'LISTINGLENS_COMBINED.pdf')
    if not file.is_file():
        return JSONResponse(status_code=404, content={'error_code': 'PDF_EXPORT_MISSING'})
    return FileResponse(file, filename=file.name, media_type='application/zip' if format == 'zip' else 'application/pdf', headers={'Cache-Control':'no-store'})
