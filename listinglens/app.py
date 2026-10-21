"""ListingLens universal *experimental* live SSR search, one user request -> one upstream HTML GET."""
from datetime import datetime,timezone
from pathlib import Path
from time import monotonic
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from .search import parse_query_pairs,QueryIssue,PER_PAGE
from .parser import extract,next_data,SourceError,normalize
from .transport import fetch_html
from .detail import validate_listing_id, detail_url, extract_detail, normalize_detail
from .workflow import router,interrupted_recovery
from .pdf_jobs import router as pdf_router
from .demo_guard import public_demo_guard

app=FastAPI(title='ListingLens',version='3.1.16',docs_url=None,redoc_url=None,openapi_url=None)
# R2.1: static assets must not keep stale R1 form validation after an update.
@app.middleware("http")
async def prevent_stale_ui_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["X-ListingLens-UI-Version"] = "origin-match-r3.1.16"
    return response

app.middleware("http")(public_demo_guard)
app.include_router(router)
app.include_router(pdf_router)
interrupted_recovery()
@app.get('/health', include_in_schema=False)
async def health():
    return {'status': 'ok', 'source': 'myhome.ge', 'mode': 'on_demand', 'live_source_checked': False}

@app.get('/v1/search_listings')
@app.get('/search_listings')
async def search_listings(request:Request):
    query=None
    effective_url=None
    upstream_status=None
    try:
        query=parse_query_pairs(request.query_params.multi_items())
        before=monotonic()
        html,effective_url,status=await fetch_html(query.url())
        upstream_status=status
        rows=extract(html,query,effective_url)
        document=next_data(html)
        actual=document['props']['pageProps']['initialFetchArgs']['query']
        listings=[normalize(item,query) for item in rows]
        return {'source':'myhome.ge','transport':'html_ssr','fetched_at':datetime.now(timezone.utc).isoformat(),
                'upstream_status':status,'latency_ms':round((monotonic()-before)*1000),
                'upstream_url':effective_url,'route_strategy':query.route_strategy(),
                'query_requested':query.as_dict(),'query_effective':actual,
                'ssr_filters_verified':True,'page':query.page,'per_page':PER_PAGE,
                'returned_count':len(listings),'total':None,'last_page':None,'has_more':None,
                'count_status':'unavailable','listings':listings}
    except (QueryIssue,SourceError) as exc:
        payload={'error_code':exc.code,'detail':str(exc)}
        if query is not None:
            payload['upstream_url']=effective_url or query.url()
            payload['upstream_status']=upstream_status
            payload['query_requested']=query.as_dict()
            payload['ssr_filters_verified']=False
        return JSONResponse(status_code=exc.http_status,content=payload)
    except Exception:
        return JSONResponse(status_code=502,content={'error_code':'UPSTREAM_UNEXPECTED_ERROR','detail':'Upstream processing error; no fabricated result returned'})



@app.get('/v1/get_listing')
@app.get('/get_listing')
async def get_listing(listing_id: str | None = None):
    """Fresh ID-only HTML detail request with strictly verified redirects; no saved response.

    Proof of production network success requires running VERIFY_GET_LISTING_MAC.sh.
    """
    try:
        ident = validate_listing_id(listing_id)
    except ValueError as exc:
        return JSONResponse(status_code=422,content={'error_code':'INVALID_LISTING_ID','detail':str(exc)})
    url=detail_url(ident)
    try:
        before=monotonic()
        html,effective_url,status=await fetch_html(url)
        raw=extract_detail(html,ident,effective_url)
        data=normalize_detail(raw,ident)
        return {'status':'success','source':'myhome.ge','transport':'html_ssr',
                'fetched_at':datetime.now(timezone.utc).isoformat(),
                'upstream_status':status,'upstream_url':effective_url,
                'latency_ms':round((monotonic()-before)*1000),
                'detail_id_verified':True,'data':data}
    except SourceError as exc:
        return JSONResponse(status_code=exc.http_status,content={
            'error_code':exc.code,'detail':str(exc),'listing_id':ident,
            'upstream_url':url,'detail_id_verified':False})
    except Exception:
        return JSONResponse(status_code=502,content={
            'error_code':'UPSTREAM_UNEXPECTED_ERROR',
            'detail':'MyHome detail data could not be processed; no fabricated result',
            'listing_id':ident,'detail_id_verified':False})

WEB_ROOT=Path(__file__).parent/'web'
@app.get('/',include_in_schema=False)
async def homepage():
    return FileResponse(WEB_ROOT/'index.html',media_type='text/html',headers={'Cache-Control':'no-store'})
app.mount('/assets',StaticFiles(directory=str(WEB_ROOT)),name='assets')
