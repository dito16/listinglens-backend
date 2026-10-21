"""Render Free demo protection: global request budgets and single heavy workload.

Not intended to be an anti-abuse service for substantial public production usage.
Budgets are in-process and reset when the free instance sleeps/restarts.
"""
import os
import threading
import time
from collections import deque
from fastapi.responses import JSONResponse
from .workflow import ACTIVE as COLLECTION_ACTIVE
from .pdf_jobs import ACTIVE as PDF_ACTIVE

LOCK=threading.Lock()
REQUESTS=deque()
HEAVY=deque()

def _error(code, message, status=429):
    return JSONResponse(status_code=status, content={'error_code': code,'detail':message},
                        headers={'Cache-Control':'no-store'})

async def public_demo_guard(request, call_next):
    if os.environ.get('LISTINGLENS_PUBLIC_DEMO') != '1':
        return await call_next(request)
    path=request.url.path
    if path.startswith(('/docs','/redoc','/openapi.json')):
        return _error('NOT_FOUND','Not found',404)
    if path in ('/v1/search_listings','/search_listings','/v1/get_listing','/get_listing'):
        return _error('PUBLIC_DEMO_ROUTE_DISABLED','Use the three ListingLens workflows.',403)
    now=time.monotonic()
    heavy=request.method=='POST' and path in {'/api/preflight','/api/jobs','/api/single/jobs','/api/pdf/jobs'}
    with LOCK:
        while REQUESTS and REQUESTS[0]<now-60: REQUESTS.popleft()
        while HEAVY and HEAVY[0]<now-3600: HEAVY.popleft()
        if len(REQUESTS)>=240:
            return _error('DEMO_RATE_LIMIT','Public demo request budget reached. Try later.')
        if heavy and len(HEAVY)>=24:
            return _error('DEMO_TASK_LIMIT','Shared demo task budget reached. Try later.')
        REQUESTS.append(now)
        if heavy: HEAVY.append(now)
    if request.method=='POST' and path in {'/api/jobs','/api/single/jobs'} and PDF_ACTIVE['job_id']:
        return _error('DEMO_BUSY','A PDF batch is already running. Please wait.',409)
    if path=='/api/pdf/jobs' and request.method=='POST' and any(not t.done() for t in COLLECTION_ACTIVE.values()):
        return _error('DEMO_BUSY','A search job is already running. Please wait.',409)
    return await call_next(request)
