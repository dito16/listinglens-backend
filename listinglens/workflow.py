"""Local workflow with factual preflight, durable incremental rows,
read-only source and conservative matching. No canned production data.
"""
from __future__ import annotations
import asyncio,csv,hashlib,io,json,math,os,sqlite3,time,uuid,re,html as html_module
from contextlib import contextmanager
from collections import OrderedDict
from html.parser import HTMLParser
from dataclasses import dataclass,replace
from datetime import datetime,timezone
from pathlib import Path
from typing import Any
from fastapi import APIRouter
from fastapi.responses import JSONResponse,StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Font,PatternFill
from .search import SearchQuery,parse_query_pairs,QueryIssue,PER_PAGE
from .locations import validate_location
from .parser import extract,normalize,verify_item,SourceError,next_data
from .transport import fetch_html
from .detail import detail_url,extract_detail,normalize_detail,validate_listing_id
from .matcher import fingerprint,download_photo,scores,assess,photo_similarity
from .sift_matcher import extract_features,compare_features,RULE_VERSION
import httpx

DB_PATH=Path(os.environ.get('LISTINGLENS_DATA_DIR',str(Path(__file__).resolve().parent.parent/'data')))/'jobs.sqlite3'
router=APIRouter(prefix='/api')
# No app-imposed network page/row ceilings or local permission-checkbox gate.
# Only user-selected ranges/row limits apply; upstream refusals are respected.
PER_PAGE=24
PREFLIGHT_TTL=600
PREP:dict[str,dict]={}
ACTIVE:dict[str,asyncio.Task]={}


@contextmanager
def db():
    """A transaction that always closes its SQLite connection.

    sqlite3.Connection.__exit__ commits/rolls back but does NOT close; frequent
    progress updates must not accumulate open connections on the user's Mac.
    """
    DB_PATH.parent.mkdir(exist_ok=True,parents=True)
    conn=sqlite3.connect(DB_PATH,timeout=10)
    try:
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,state TEXT NOT NULL,created REAL NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS rows (job_id TEXT NOT NULL,listing_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(job_id,listing_id))')
        conn.commit()
        with conn:
            yield conn
    finally:
        conn.close()

def save_state(s):
    with db() as c:c.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?)',(s['job_id'],json.dumps(s,ensure_ascii=False),time.time()))

def load_state(job_id):
    with db() as c:
        row=c.execute('SELECT state FROM jobs WHERE id=?',(job_id,)).fetchone()
        return json.loads(row[0]) if row else None

def save_row(job_id,row):
    with db() as c:c.execute('INSERT OR REPLACE INTO rows VALUES (?,?,?)',(job_id,row['listing_id'],json.dumps(row,ensure_ascii=False)))

def rows_for(job_id,limit=None,offset=0):
    with db() as c:
        if limit is None:
            entries=c.execute('SELECT payload FROM rows WHERE job_id=? ORDER BY rowid',(job_id,)).fetchall()
        else:
            entries=c.execute('SELECT payload FROM rows WHERE job_id=? ORDER BY rowid LIMIT ? OFFSET ?', (job_id,limit,offset)).fetchall()
        return [json.loads(r[0]) for r in entries]

def interrupted_recovery():
    with db() as c:
        for item in c.execute('SELECT id,state FROM jobs').fetchall():
            state=json.loads(item['state'])
            if state['status'] in ('RUNNING','CANCELLING'):
                state['status']='INTERRUPTED';state['phase']='interrupted';state['error']='The server restarted; partial results were saved'
                c.execute('UPDATE jobs SET state=? WHERE id=?',(json.dumps(state,ensure_ascii=False),item['id']))


@dataclass(frozen=True)
class Scope:
    pages_mode:str
    first:int
    last:int|None
    listings_mode:str
    row_limit:int|None
    legacy:bool=False


def parse_scope(payload:dict)->tuple[str,SearchQuery,Scope]:
    if not isinstance(payload,dict):raise QueryIssue('INVALID_PAYLOAD','Expected a JSON object')
    mode=payload.get('mode')
    if mode not in ('OWNER','AGENT'):raise QueryIssue('INVALID_MODE','Choose OWNER or AGENT')
    criteria=payload.get('filters')
    if not isinstance(criteria,dict):raise QueryIssue('INVALID_FILTERS','filters must be an object')
    if 'owner_type' in criteria or 'seller_type' in criteria:raise QueryIssue('SELLER_MODE_CONFLICT','Seller role is determined by the workflow mode')
    if 'city' in criteria:
        raise QueryIssue('CITY_ID_REQUIRED','Select a city from the list.')
    try:
        validate_location(criteria)
    except ValueError as exc:
        raise QueryIssue('INVALID_LOCATION',str(exc)) from exc
    required=('deal_types','real_estate_types','price_from','price_to')
    missing=[k.replace('_',' ') for k in required if not str(criteria.get(k,'')).strip()]
    if missing:
        raise QueryIssue('MISSING_REQUIRED_FILTERS','Required: '+', '.join(missing))
    extras=('area_from','area_to','room_types','bedroom_types','districts','urbans')
    count=sum(bool(str(criteria.get(k,'')).strip()) for k in extras)
    if count<3:
        raise QueryIssue('INSUFFICIENT_FILTERS',f'Choose at least 3 additional filters. Selected: {count}/3. Choose area min, area max, rooms, bedrooms, district or microdistrict.')

    pairs=[]
    for k,v in criteria.items():
        if isinstance(v,list):v=','.join(map(str,v))
        if v is None or v=='':continue
        if isinstance(v,(dict,bool)):raise QueryIssue('INVALID_FILTERS',f'{k}: invalid value')
        pairs.append((k,str(v)))
    pairs=[p for p in pairs if p[0]!='page']
    if not any(k=='currency_id' for k,v in pairs):pairs.append(('currency_id','2'))  # USD default for collection only
    if mode=='OWNER':pairs.append(('owner_type','physical'))
    q=parse_query_pairs(pairs)
    # R2 clients accepted for regression only; new UI emits explicit modes.
    legacy='pages_mode' not in payload and 'listings_mode' not in payload
    pages_mode=payload.get('pages_mode', 'range')
    listings_mode=payload.get('listings_mode','limit')
    if pages_mode not in ('all','range') or listings_mode not in ('all','limit'):
        raise QueryIssue('INVALID_SCOPE','Choose all pages or a page range, and all listings or a limit')
    first=payload.get('page_from',1)
    last=payload.get('page_to',payload.get('max_pages',1)) if pages_mode=='range' else None
    limit=payload.get('row_limit',payload.get('max_rows',24)) if listings_mode=='limit' else None
    if not isinstance(first,int) or isinstance(first,bool) or first<1:
        raise QueryIssue('INVALID_SCOPE','Starting page must be a positive integer')
    if pages_mode=='range' and (not isinstance(last,int) or isinstance(last,bool) or last<first):
        raise QueryIssue('INVALID_SCOPE','Ending page must not be less than starting page')
    if listings_mode=='limit' and (not isinstance(limit,int) or isinstance(limit,bool) or limit<1):
        raise QueryIssue('INVALID_SCOPE','Listing limit must be a positive integer')
    if any(key in payload for key in ('owner_search_page_budget','owner_candidate_budget')):
        raise QueryIssue('UNSUPPORTED_LEGACY_MATCH_LIMIT','Legacy limits are unsupported; refresh the application')
    if os.environ.get('LISTINGLENS_PUBLIC_DEMO') == '1':
        # The private/local package retains its full user-controlled scope.
        # Render Free is a shared public preview, not an unbounded collector.
        if pages_mode != 'range' or listings_mode != 'limit' or last is None or limit is None or last-first+1>5 or first>5 or limit>100:
            raise QueryIssue('PUBLIC_DEMO_SCOPE_LIMIT',
                'Public demo: select a page range within pages 1–5 and a maximum of 100 listings.')
    return mode,q,Scope(pages_mode,first,last,listings_mode,limit,legacy)


def parse_body(payload:dict):
    """Preserve the historical four-tuple for imported R2 regression tests."""
    mode,q,scope=parse_scope(payload)
    return mode,q,scope.last or 0,scope.row_limit or 0


def scope_parameters(scope:Scope,source_last_page:int|None):
    if scope.pages_mode=='all':
        first,last=1,source_last_page
    else:first,last=scope.first,scope.last
    if source_last_page is not None:last=min(last,source_last_page)
    if last is not None and last<first:return (first,first-1)
    return first,last


def scope_fingerprint(mode,q,scope):
    return hashlib.sha256(json.dumps({'mode':mode,'filters':q.as_dict(),
        'scope':{'pages_mode':scope.pages_mode,'first':scope.first,'last':scope.last,
        'listings_mode':scope.listings_mode,'row_limit':scope.row_limit}},sort_keys=True).encode()).hexdigest()


def count_from_ssr(html,q):
    """Only a semantically corroborated statements/count React Query record counts.
    `statementsCount` or len(list) is NOT total. No authenticated API bypass.
    """
    obj=next_data(html)
    queries=((obj.get('props') or {}).get('pageProps') or {}).get('dehydratedState',{}).get('queries',[])
    for r in queries if isinstance(queries,list) else []:
        if not isinstance(r,dict):continue
        key=r.get('queryKey')
        if not isinstance(key,list) or len(key)<3 or key[:2]!=['statements','count'] or not isinstance(key[2],dict):continue
        from .parser import check_query
        try:check_query(key[2].get('query'),q.upstream_params(),'count query')
        except SourceError:continue
        response=(r.get('state') or {}).get('data')
        if not isinstance(response,dict) or response.get('result') is not True:continue
        data=response.get('data')
        if not isinstance(data,dict):continue
        total,per,last=data.get('total'),data.get('per_page'),data.get('last_page')
        if not all(type(x) is int for x in (total,per,last)) or total<0 or per!=PER_PAGE or last!=(total+per-1)//per:
            continue
        return {'total':total,'per_page':per,'last_page':last,
                'source':'SSR_VERIFIED_STATEMENTS_COUNT_QUERY',
                'timestamp':clock(),'filter_signature':hashlib.sha256(json.dumps(q.upstream_params(),sort_keys=True).encode()).hexdigest(),
                'count_source_url':q.url()}
    return None


class _VisiblePageText(HTMLParser):
    """Extract visible server-rendered text; ignore scripts/JSON and styles.

    MyHome may display a count that ignores a filter. It MUST NOT be treated
    as a verified `total`, and never controls traversal or stop conditions.
    """
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skipped=0;self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):
            self.skipped+=1
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript') and self.skipped:
            self.skipped-=1
    def handle_data(self,data):
        if not self.skipped and data.strip():
            self.parts.append(data.strip()[:500])


def displayed_ui_total(html:str)->int|None:
    """An *unverified* visible UI hint, NEVER the API count or last_page."""
    if not isinstance(html,str):return None
    parser=_VisiblePageText()
    try:parser.feed(html[:4_000_000])
    except (ValueError,TypeError):return None
    content=' '.join(parser.parts)
    patterns=(r'Найдено\s+([0-9][0-9\s]{0,12})\s+объявлен(?:и[еяй]|ий)',
              r'Found\s+([0-9][0-9,]{0,12})\s+listing',
              r'ნაპოვნია\s+([0-9][0-9\s]{0,12})')
    for pat in patterns:
        result=re.search(pat,content,re.I)
        if result:
            digits=re.sub(r'[^0-9]','',result.group(1))
            if digits:return int(digits)
    return None


def clock():return datetime.now(timezone.utc).isoformat()


def safe_error(exc):
    if isinstance(exc,(SourceError,QueryIssue)):return f'{exc.code}: {exc}'
    return f'{type(exc).__name__}: operation failed (no personal data stored)'


class SearchBatch(list):
    """Verified source query plus only validated, displayable listing rows."""
    def __init__(self, rows, raw_count=0, excluded_count=0, excluded_examples=()):
        super().__init__(rows)
        self.raw_count=raw_count
        self.excluded_count=excluded_count
        self.excluded_examples=list(excluded_examples)
        self.source_count=None
        self.displayed_ui_total=None
        self.raw_ids=[]
        self.seller_role_mismatches=0
        # The seller-role-verified list from the SSR search includes visible
        # upstream results even when internal numeric prices differ slightly.
        # Legacy SearchBatch test doubles may omit this attribute (None).
        self.agent_role_rows=None


async def search_source(q:SearchQuery)->list[dict]:
    html,url,status=await fetch_html(q.url())
    # All original R3 guarantees on URL, Next.js, request filters and schema
    # stay mandatory. The workflow only isolates individual sponsored/stale
    # cards that contradict the requested filters, rather than failing the
    # entire table because of one card. We never accept an out-of-filter row.
    raw=extract(html,q,url,verify_listings=False)
    accepted=[]; rejected=[]; agent_role_rows=[]
    for item in raw:
        normalized=None; variance=None
        try:
            verify_item(item,q)
            normalized=normalize(item,q)
            accepted.append(normalized)
        except SourceError as exc:
            variance=str(exc)[:170]
            if len(rejected)<3:
                rejected.append({'listing_id':str(item.get('id') or ''),
                                 'error_code':exc.code, 'reason':variance})
        # In AGENT enumeration, the source search and seller role determine the
        # denominator.  MyHome sometimes returns cards whose converted USD
        # price is just outside the user-facing price range.  Keep those cards
        # in the role-verified enumeration, without weakening the strict public
        # search endpoint or disguising their local filter disagreement.
        user=item.get('user_type')
        if q.owner_type!='physical' and isinstance(user,dict) and user.get('type')=='agent':
            if normalized is None:
                normalized=normalize(item,q)  # invalid schema must fail closed
            role_row=dict(normalized)
            if variance:
                role_row['source_filter_warning']=variance
            agent_role_rows.append(role_row)
    batch=SearchBatch(accepted,raw_count=len(raw),excluded_count=len(raw)-len(accepted),excluded_examples=rejected)
    batch.agent_role_rows=agent_role_rows
    batch.raw_ids=[str(item['id']) for item in raw]
    if q.owner_type is not None:
        batch.seller_role_mismatches=sum(1 for item in raw if not isinstance(item.get('user_type'),dict) or item['user_type'].get('type') != q.owner_type)
    batch.source_count=count_from_ssr(html,q)
    batch.displayed_ui_total=displayed_ui_total(html)
    return batch


async def detail_source(ident:str)->dict:
    html,url,status=await fetch_html(detail_url(ident))
    return normalize_detail(extract_detail(html,ident,url),ident)


async def enumerate_agent_ids(q:SearchQuery, first_batch:SearchBatch, scope:Scope):
    """Enumerate an actual MyHome HTML search, never an API count.

    The denominator is the number of distinct, locally seller-role-verified
    owner_type=agent IDs in this *time-varying enumeration snapshot*. Two successive
    SSR-verified RAW-empty pages are required. No hardcoded page count cap.
    No detail pages or images are requested here.
    """
    first=1 if scope.pages_mode=='all' else scope.first
    last=scope.last if scope.pages_mode=='range' else None
    seen=set(); raw_page_fingerprints=set(); included=[]; source_rows=0
    rejected=0; role_variances=0; empty_streak=0; page=first; last_nonempty=None; pages_seen=0
    while last is None or page<=last:
        if page != first:
            await asyncio.sleep(1.5)
        items=first_batch if page==first else await search_source(replace(q,page=page))
        raw=getattr(items,'raw_count',len(items))
        raw_ids=tuple(getattr(items,'raw_ids',None) or [x['listing_id'] for x in items])
        if raw != len(raw_ids):
            raise SourceError('RAW_PAGE_IDS_UNAVAILABLE',f'Page integrity could not be verified: {page}')
        if len(raw_ids)>PER_PAGE:
            raise SourceError('UPSTREAM_INVALID_SCHEMA','MyHome returned more than 24 cards')
        if q.owner_type=='agent' and getattr(items,'seller_role_mismatches',0):
            raise SourceError('AGENT_FILTER_NOT_EFFECTIVE',f'Page {page} contains unexpected seller roles')
        source_rows+=raw;rejected+=getattr(items,'excluded_count',0);pages_seen+=1
        if raw==0:
            empty_streak+=1
            if empty_streak==2:break
        else:
            if raw_ids in raw_page_fingerprints:
                raise SourceError('REPEATED_SOURCE_PAGE','Source returned a repeated nonempty page; coverage is unverified')
            raw_page_fingerprints.add(raw_ids)
            last_nonempty=page;empty_streak=0
            # A strict local price check must not silently shrink the count of
            # real agent ads already present in MyHome's SSR-filtered results.
            candidates=getattr(items,'agent_role_rows',None)
            if candidates is None:
                candidates=items  # unit-test doubles and legacy search adapters
            for hit in candidates:
                role=hit.get('owner_type')
                if role!='agent' or hit['listing_id'] in seen:
                    continue
                seen.add(hit['listing_id']);included.append(hit)
                role_variances+=bool(hit.get('source_filter_warning'))
        page+=1
    if last is not None:
        end_reason='USER_SELECTED_LAST_PAGE' if empty_streak<2 else 'TWO_EMPTY_SSR_PAGES'
    else:
        if empty_streak!=2:
            raise SourceError('SOURCE_END_UNVERIFIED','Could not confirm end of results')
        end_reason='TWO_EMPTY_SSR_PAGES'
    return {'rows':included,'total':len(included),'pages_visited':pages_seen,'listing_pages':len(raw_page_fingerprints),
            'last_nonempty_page':last_nonempty,'raw_rows':source_rows,
            'excluded_rows':rejected,'agent_role_filter_variances':role_variances,
            'end_reason':end_reason,
            'enumerated_at':clock(),'role_scope':'AGENT_ONLY' if q.owner_type=='agent' else 'AGENT_ONLY_FROM_MIXED'}


async def enumerate_owner_ids(q: SearchQuery, first_batch: SearchBatch, scope: Scope):
    """Count OWNER listings by SSR-verified, role-checked public search pages.

    MyHome search pages may have no `statements/count` record.  A full, finite
    enumeration is then the only locally established *snapshot* denominator.
    Two empty RAW pages establish the end; short pages alone do not.
    Never count duplicates, unrelated seller roles, or unvalidated cards.
    This does not scrape details/photos and never requests another endpoint.
    """
    if q.owner_type != 'physical':
        raise SourceError('OWNER_ROLE_REQUIRED','Owner enumeration requires the physical filter')
    first=scope.first if scope.pages_mode=='range' else 1
    last=scope.last if scope.pages_mode=='range' else None
    seen=set(); fingerprints=set(); accepted=[]
    page=first; empties=0; pages_visited=0; listing_pages=0
    raw_rows=0; excluded_rows=0; last_nonempty=None
    while last is None or page<=last:
        if page!=first:
            await asyncio.sleep(0.6)
        batch=first_batch if page==first else await search_source(replace(q,page=page))
        raw=getattr(batch,'raw_count',len(batch))
        ids=tuple(getattr(batch,'raw_ids',None) or [x['listing_id'] for x in batch])
        if raw!=len(ids) or raw>PER_PAGE or len(set(ids))!=len(ids):
            raise SourceError('RAW_PAGE_IDS_UNAVAILABLE',f'Unverified integrity of page {page}')
        if getattr(batch,'seller_role_mismatches',0):
            raise SourceError('OWNER_FILTER_NOT_EFFECTIVE',f'Page {page} contains a non-owner listing')
        pages_visited+=1; raw_rows+=raw
        excluded_rows+=getattr(batch,'excluded_count',0)
        if not raw:
            empties+=1
            if empties>=2:break
        else:
            if ids in fingerprints:
                raise SourceError('REPEATED_SOURCE_PAGE',f'Page {page} repeats another nonempty page')
            fingerprints.add(ids); listing_pages+=1; empties=0; last_nonempty=page
            for hit in batch:
                if hit.get('owner_type')!='physical':
                    raise SourceError('OWNER_FILTER_NOT_EFFECTIVE',f'Listing {hit.get("listing_id")} is not an owner listing')
                ident=hit['listing_id']
                if ident in seen:
                    raise SourceError('REPEATED_LISTING_ID',f'Listing {ident} occurs on multiple pages; exact enumeration is unverified')
                seen.add(ident); accepted.append(hit)
        page+=1
    if last is None and empties!=2:
        raise SourceError('SOURCE_END_UNVERIFIED','Missing two consecutive empty pages at the end')
    return {'rows':accepted,'total':len(accepted),'pages_visited':pages_visited,
        'listing_pages':listing_pages,'raw_rows':raw_rows,'excluded_rows':excluded_rows,
        'last_nonempty_page':last_nonempty,
        'end_reason':'TWO_EMPTY_SSR_PAGES' if empties==2 else 'USER_SELECTED_LAST_PAGE',
        'role_scope':'OWNER_ONLY','enumerated_at':clock()}


@router.post('/preflight')
async def preflight(payload:dict):
    try:
        mode,q,scope=parse_scope(payload)
        # Expire unused heavy snapshots without touching any job records.
        for old_key,old in list(PREP.items()):
            if time.monotonic()-old['ts']>PREFLIGHT_TTL:PREP.pop(old_key,None)
        # If the page range begins with 5, preflight must inspect page 5, not page 1.
        sample_page=scope.first if scope.pages_mode=='range' else 1
        initial_q=replace(q,page=sample_page)
        direct_filter_proof='MIXED_ROLE_CHECK' if mode=='AGENT' else 'UPSTREAM_OWNER_FILTER_VERIFIED'
        live_q=initial_q
        if mode=='AGENT':
            # Try direct agent search. If SSR/role evidence disagrees, do NOT
            # attribute a count to agents; fall back to explicitly mixed rows.
            direct_q=replace(initial_q,owner_type='agent')
            try:
                direct=await search_source(direct_q)
                if direct and all(r.get('owner_type')=='agent' for r in direct):
                    sample=direct;live_q=direct_q
                    direct_filter_proof='SSR_ECHO_AND_ROLE_SAMPLE_ONLY'
                else:sample=await search_source(initial_q)
            except (SourceError,QueryIssue):
                sample=await search_source(initial_q)
        else:
            sample=await search_source(initial_q)
            if any(x.get('owner_type')!='physical' for x in sample):
                raise SourceError('SELLER_UNVERIFIED','Owner results contain an unverified seller role')
        snapshot=None
        owner_snapshot=None
        # Full agent enumeration follows the user's selected scope. No local checkbox gate.
        enumerating=(mode=='AGENT' and scope.pages_mode=='all' and scope.listings_mode=='all')
        if enumerating:
            try:
                snapshot=await enumerate_agent_ids(live_q,sample,scope)
            except SourceError as e:
                if live_q.owner_type=='agent' and e.code=='AGENT_FILTER_NOT_EFFECTIVE':
                    # The HTML may echo owner_type=agent while still returning
                    # nonagents. Never call that an agent-only count.
                    live_q=initial_q
                    sample=await search_source(initial_q)
                    direct_filter_proof='MIXED_FALLBACK_STRICT_ROLE_CLASSIFICATION'
                    snapshot=await enumerate_agent_ids(live_q,sample,scope)
                else:raise
        # OWNER count: if no verified MyHome count, enumerate public SSR pages
        # in an explicit All/All selection rather than calling first-page 24 the total.
        # Reuse this frozen snapshot when Start is clicked; no duplicate search requests.
        direct_owner_count=getattr(sample,'source_count',None)
        if mode=='OWNER' and direct_owner_count is None and scope.pages_mode=='all' and scope.listings_mode=='all':
            owner_snapshot=await enumerate_owner_ids(live_q,sample,scope)
        raw=getattr(sample,'raw_count',len(sample)); excluded=getattr(sample,'excluded_count',0)
        direct_count=getattr(sample,'source_count',None)
        # AGENT exact count is impossible without proof that the agent-only filter is effective.
        count=direct_count if mode=='OWNER' else None
        last=count['last_page'] if count else None
        selected=scope_parameters(scope,last)
        range_known=selected is not None
        page_cap=None if selected[1] is None else max(0,selected[1]-selected[0]+1)
        slots=None if page_cap is None else page_cap*PER_PAGE
        if count and selected[1] is not None:
            effective_start,effective_last=selected
            available=max(0,min(count['total']-(effective_start-1)*PER_PAGE,page_cap*PER_PAGE))
        else:available=slots
        row_cap=scope.row_limit  # Only the USER-selected cap; source count/24-per-page never creates a hidden cutoff.
        # Informational warnings only. No app-imposed ceiling or checkbox gate.
        blockers=[]
        if mode=='AGENT' and snapshot is None and direct_filter_proof!='SSR_ECHO_AND_ROLE_SAMPLE_ONLY':
            blockers.append('AGENT_FILTER_UNVERIFIED: roles will be verified per listing; agent total is unknown')
        # When source returned count, this is a provenance claim for precise filter signature ONLY.
        sample_agents=sum(r.get('owner_type')=='agent' for r in sample)
        token=uuid.uuid4().hex
        result={'preflight_id':token,'mode':mode,'filters':live_q.as_dict(),
           'query_url':live_q.url(),'requested_scope':{'pages_mode':scope.pages_mode,'page_from':scope.first,'page_to':scope.last,
             'listings_mode':scope.listings_mode,'row_limit':scope.row_limit},
           'source':'myhome.ge','source_probe':'LIVE_HTML_SSR','probe_page':sample_page,
           'first_page_count':len(sample),'first_page_raw_count':raw,'first_page_excluded_count':excluded,
           'first_page_excluded_examples':getattr(sample,'excluded_examples',[]),
           'first_page_agent_count':sample_agents if mode=='AGENT' else None,
           'enumerated_agent_total':snapshot['total'] if snapshot else None,
           'enumerated_owner_total':owner_snapshot['total'] if owner_snapshot else None,
           'enumeration_evidence':{k:v for k,v in (snapshot or owner_snapshot).items() if k!='rows'} if (snapshot or owner_snapshot) else None,
           'agent_role_handling':direct_filter_proof,
           'source_total':count['total'] if count else None,
           'displayed_ui_total_unverified':getattr(sample,'displayed_ui_total',None),
           'access_status':'NO_UPSTREAM_ACCESS_DENIAL_OBSERVED',
           'source_last_page':last,'source_per_page':count['per_page'] if count else PER_PAGE,
           'source_total_status':'VERIFIED' if count else 'ENUMERATED_AGENT_SNAPSHOT' if snapshot else 'ENUMERATED_OWNER_SNAPSHOT' if owner_snapshot else 'COUNT_UNKNOWN',
           'count_evidence':count,'count_quality':'VERIFIED' if count else 'ENUMERATED_SNAPSHOT' if (snapshot or owner_snapshot) else 'UNKNOWN',
           'count_explanation':'Verified SSR statements/count' if count else 'Agent IDs from verified SSR pages; not an official MyHome total' if snapshot else 'Owner IDs from verified SSR pages; not an official MyHome total' if owner_snapshot else 'COUNT_UNKNOWN: SSR did not provide a verified total',
           'selected_page_from':selected[0],
           'selected_page_to':selected[1],
           'planned_max_pages':page_cap,'planned_max_rows':row_cap,
           'theoretical_listing_slots':slots,'estimated_total':available,
           'owner_matching_policy':'SINGLE_EXACT_GEO_ROOMS_BEDROOMS_PRICE_AREA;FIRST_FOUR_AGENT_IMAGES;FIRST_SIMILARITY_GT_0_95',
           'scope_total_expected':snapshot['total'] if snapshot else owner_snapshot['total'] if owner_snapshot else min(available,scope.row_limit) if count and scope.row_limit is not None else available if count else None,
           'scope_total_provenance':'LIVE_AGENT_SNAPSHOT_ENUMERATION' if snapshot else 'LIVE_OWNER_SNAPSHOT_ENUMERATION' if owner_snapshot else 'VERIFIED_SOURCE_COUNT' if count else 'UNKNOWN',
           'blockers':blockers,'can_start':True,
           'scope_signature':scope_fingerprint(mode,q,scope),
           'created_at':clock(),'expires_after_seconds':PREFLIGHT_TTL}
        PREP[token]={'ts':time.monotonic(),'mode':mode,'q':live_q,'scope':scope,'selected':selected,
                     'rows':row_cap,'pages':page_cap,'result':result,'agent_snapshot':snapshot,'owner_snapshot':owner_snapshot}
        return result
    except (QueryIssue,SourceError) as e:
        return JSONResponse(status_code=e.http_status,content={'error_code':e.code,'detail':str(e)})


def base_row(hit,mode):
    return dict(hit,source_url=hit.get('url'),seller_type=hit.get('owner_type') or 'UNKNOWN',
       row_mode='OWNER_DIRECT' if mode=='OWNER' else 'AGENT_ORIGIN_LOOKUP',
       matching_status='NOT_APPLICABLE' if mode=='OWNER' else 'PENDING',
       processing_status='COLLECTED',original_owner_id=None,original_owner_url=None,original_owner_price_usd=None,
       price_difference_usd=None, matched_photo_count=0,match_method=None,histogram_similarity=None,photo_similarity=None,confidence='UNVERIFIED',
       match_evidence=[],reason='',error=None,images_count=len(hit.get('image_urls',[])) if hit.get('image_urls') else None,photo_urls=hit.get('image_urls',[]),details_loaded=False)


def progress(state,phase,**vals):
    # Cancellation submitted from another request must survive concurrent progress writes.
    persisted=load_state(state['job_id'])
    if persisted and persisted.get('cancel_requested'):state['cancel_requested']=True
    state.update(vals);state['phase']=phase;state['updated_at']=clock()
    # Progress is a verified count of fully processed listing IDs, never an ETA.
    done=state.get('metrics',{}).get('listings_processed',0)
    expected=state.get('scope_total_expected')
    actual=state.get('scope_total_actual')
    denominator=actual if actual is not None else expected
    state['progress_percent']=min(100,round(100*done/denominator,1)) if isinstance(denominator,int) and denominator>0 else (100 if denominator==0 and state.get('status')=='COMPLETED' else None)
    save_state(state)


async def compare_photo_sets(rt,client,source_urls,candidate_urls,source_key,candidate_key):
    """FAST all pairs then SIFT fallback all pairs; first valid proof stops.

    A pair is comparable only after both downloaded. No fabricated similarity
    number for SIFT; "match" is an independent geometry decision.
    """
    source_urls=list(source_urls)[:3]
    candidate_urls=list(candidate_urls)
    diag={'attempted_pairs':0,'compared_pairs':0,'failed_pairs':0,
          'sift_pairs_attempted':0,'sift_pairs_compared':0,
          'agent_images_considered':len(source_urls) if source_key=='agent_photo' else len(candidate_urls),
          'owner_images':len(candidate_urls) if source_key=='agent_photo' else len(source_urls),
          'threshold_hit':False,'match_method':None}
    if not source_urls or not candidate_urls:return None,False,diag
    # Pass 1: identical R3.1.9 heuristic and strict >0.95. Read-once photo cache.
    for si,source_url in enumerate(source_urls):
        if rt.cancelled():raise asyncio.CancelledError()
        source_fp=await rt.photo(client,source_url)
        for ci,candidate_url in enumerate(candidate_urls):
            if rt.cancelled():raise asyncio.CancelledError()
            diag['attempted_pairs']+=1
            rt.s['metrics']['image_pairs_attempted']+=1
            candidate_fp=await rt.photo(client,candidate_url) if source_fp is not None else None
            if source_fp is None or candidate_fp is None:
                diag['failed_pairs']+=1;rt.s['metrics']['image_pairs_failed']+=1
                continue
            values=scores(source_fp,candidate_fp)
            similarity=photo_similarity(values)
            diag['compared_pairs']+=1;rt.s['metrics']['image_comparisons']+=1
            if similarity>0.95:
                diag['threshold_hit']=True;diag['match_method']='HIST_PHASH'
                return {source_key:si,candidate_key:ci,'method':'HIST_PHASH',
                        'similarity':similarity,**values},True,diag
    # Pass 2: only after entire cheap pass had no hit, and with the same
    # first-three source scope (never source photos 4+).
    for si,source_url in enumerate(source_urls):
        if rt.cancelled():raise asyncio.CancelledError()
        source_fp=await rt.photo(client,source_url)
        if source_fp is None:continue
        source_features=await rt.sift_features(source_url,source_fp)
        for ci,candidate_url in enumerate(candidate_urls):
            if rt.cancelled():raise asyncio.CancelledError()
            candidate_fp=await rt.photo(client,candidate_url)
            if candidate_fp is None:continue
            diag['sift_pairs_attempted']+=1
            rt.s['metrics']['sift_pairs_attempted']=rt.s['metrics'].get('sift_pairs_attempted',0)+1
            candidate_features=await rt.sift_features(candidate_url,candidate_fp)
            proof=await asyncio.to_thread(compare_features,source_features,candidate_features)
            diag['sift_pairs_compared']+=1
            rt.s['metrics']['sift_pairs_compared']=rt.s['metrics'].get('sift_pairs_compared',0)+1
            if proof['match']:
                diag['threshold_hit']=True;diag['match_method']='SIFT_RANSAC'
                rt.s['metrics']['sift_matches_confirmed']=rt.s['metrics'].get('sift_matches_confirmed',0)+1
                return {source_key:si,candidate_key:ci,**proof},True,diag
    return None,not bool(diag['failed_pairs']),diag


class JobRuntime:
    def __init__(self,state):
        self.s=state;self.cache={};self.image_cache={};self.detail_cache={};self.owner_cache={};self.last_request=0;self.error_codes={};self.match_circuit_open=False;self.sift_cache=OrderedDict()
    async def throttle(self):
        delay=.55-(time.monotonic()-self.last_request)
        if delay>0:await asyncio.sleep(delay)
        self.last_request=time.monotonic()
    async def search(self,q):
        await self.throttle();self.s['metrics']['search_calls']+=1
        try:
            result=await search_source(q)
        except SourceError as e:
            # A second attempt only on transient connection errors, NEVER access denials.
            if e.code not in ('UPSTREAM_NETWORK_ERROR','UPSTREAM_HTTP_ERROR'):raise
            await asyncio.sleep(1.0)
            await self.throttle();self.s['metrics']['search_calls']+=1
            result=await search_source(q)
        self.tick()
        return result
    async def detail(self,ident):
        if ident not in self.detail_cache:
            await self.throttle();self.s['metrics']['detail_calls']+=1
            try:
                value=await detail_source(ident)
            except SourceError as e:
                if e.code not in ('UPSTREAM_NETWORK_ERROR','UPSTREAM_HTTP_ERROR'):raise
                await asyncio.sleep(1.0);await self.throttle();self.s['metrics']['detail_calls']+=1
                value=await detail_source(ident)
            self.detail_cache[ident]=value
            self.tick()
        return self.detail_cache[ident]
    def count_error(self,code):
        self.error_codes[code]=self.error_codes.get(code,0)+1
        if self.error_codes[code]>=5:
            self.match_circuit_open=True
            raise SourceError('FIVE_FAILURE_CIRCUIT_OPEN','Five similar errors; comparisons stopped')
    def tick(self):
        progress(self.s,self.s['phase'])
    def cancelled(self):return self.s.get('cancel_requested',False) or bool((load_state(self.s['job_id']) or {}).get('cancel_requested'))
    async def photo(self,client,url):
        if url not in self.image_cache:
            if self.cancelled():raise asyncio.CancelledError()
            await self.throttle();self.s['metrics']['photo_calls']+=1
            try:self.image_cache[url]=fingerprint(await download_photo(client,url))
            except (ValueError,httpx.HTTPError,OSError) as e:
                self.image_cache[url]=None;self.s['metrics']['photo_errors']+=1
                self.count_error(type(e).__name__)
            self.tick()
        return self.image_cache[url]
    async def sift_features(self,url,fp):
        """Lazy feature extraction; bounded job-local LRU, no extra photo requests."""
        if url not in self.sift_cache:
            feature=await asyncio.to_thread(extract_features,fp)
            self.sift_cache[url]=feature
            if len(self.sift_cache)>64:self.sift_cache.popitem(last=False)
            self.s['metrics']['sift_features_computed']=self.s['metrics'].get('sift_features_computed',0)+1
        else:self.sift_cache.move_to_end(url)
        return self.sift_cache[url]

    async def owner_candidates(self,agent,q):
        """One owner-only MyHome search, exact city/district/rooms/bedrooms.

        At price <= $900: [-50,+100]; above $900: [-100,+200].
        Area range [-10,+5] m2. Two SSR-validated empty pages end the
        search; source problems are recorded as INCOMPLETE, never NOT_FOUND.
        """
        price=agent.get('price_usd')
        area=agent.get('area_sqm')
        if area is None:area=agent.get('area_m2')
        city=agent.get('city_id')
        district=agent.get('district_id')
        rooms=agent.get('rooms')
        bedrooms=agent.get('bedrooms')
        missing=[name for name,val in (('price_usd',price),('area_sqm',area),
            ('city_id',city),('district_id',district),('rooms',rooms),
            ('bedrooms',bedrooms)) if val is None]
        if missing:
            raise SourceError('INSUFFICIENT_MATCH_ANCHORS',
                'Cannot build an exact owner search; missing fields: '+', '.join(missing))
        if price<=0 or area<=0 or city<=0 or district<=0 or rooms<=0 or bedrooms<0:
            raise SourceError('INVALID_MATCH_ANCHORS','Invalid numeric parameters in agent listing')
        p_lo,p_hi=(-50,100) if price<=900 else (-100,200)
        probe=replace(q,page=1,owner_type='physical',city=None,
            cities=(int(city),),districts=(int(district),),urbans=None,
            room_types=(int(rooms),),bedroom_types=(int(bedrooms),),
            statuses=None,currency_id=2,
            price_from=max(0,math.floor(price+p_lo)),price_to=math.ceil(price+p_hi),
            area_from=max(0,math.floor(area-10)),area_to=math.ceil(area+5))
        found={};visited=0;end_evidence=None;empty_streak=0
        seen_source_pages=set();fully_scanned=True;page=1
        while True:
            if self.cancelled():raise asyncio.CancelledError()
            query=replace(probe,page=page)
            key=json.dumps(query.as_dict(),sort_keys=True)
            if key not in self.owner_cache:
                self.s['metrics']['owner_search_calls']+=1
                try:self.owner_cache[key]=await self.search(query)
                except SourceError as e:
                    fully_scanned=False;end_evidence='UPSTREAM_ERROR:'+e.code
                    break
            items=self.owner_cache[key]
            self.s['metrics']['owner_pages_visited']+=1;visited+=1
            raw=getattr(items,'raw_count',len(items))
            source_ids=tuple(getattr(items,'raw_ids',None) or (x['listing_id'] for x in items))
            if raw and not source_ids:
                fully_scanned=False;end_evidence='RAW_PAGE_IDS_UNAVAILABLE';break
            if raw and source_ids in seen_source_pages:
                fully_scanned=False;end_evidence='REPEATED_SOURCE_PAGE_NOT_END';break
            if raw:
                seen_source_pages.add(source_ids)
                self.s['metrics']['owner_listing_pages']=self.s['metrics'].get('owner_listing_pages',0)+1
            for owner in items:
                if owner.get('owner_type')!='physical' or owner['listing_id']==agent['listing_id']:
                    continue
                self.s['metrics']['owner_candidates_examined']+=1
                # Do not trust filter echo alone: require exact per-card geography
                # and room/bedroom IDs. Missing fields do not count as equality.
                if any(owner.get(k)!=v for k,v in (
                    ('city_id',int(city)),('district_id',int(district)),
                    ('rooms',int(rooms)),('bedrooms',int(bedrooms)))):
                    continue
                found.setdefault(owner['listing_id'],owner)
            evidence=getattr(items,'source_count',None)
            if evidence and page>=evidence['last_page']:
                end_evidence='VERIFIED_LAST_PAGE';break
            if raw==0:
                empty_streak+=1
                if empty_streak>=2:
                    end_evidence='TWO_EMPTY_SSR_PAGES';break
            else:empty_streak=0
            page+=1
        choices=list(found.values())
        self.s['metrics']['owner_candidate_shortlisted']+=len(choices)
        trace=[{'search':1,'query':probe.upstream_params(),
            'pages_visited':visited,'end_evidence':end_evidence,
            'coverage_complete':bool(fully_scanned and end_evidence in ('VERIFIED_LAST_PAGE','TWO_EMPTY_SSR_PAGES')),
            'candidate_count':len(choices)}]
        return choices,fully_scanned and bool(trace[0]['coverage_complete']),trace


    async def match(self,row,q):
        ident=row['listing_id']
        progress(self.s,'agent_details',last_listing_id=ident)
        try:
            agent=dict(await self.detail(ident))
            # Search cards carry validated numeric geographic IDs that may be
            # missing from the detail SSR. Never invent a district from a name.
            for key,source_key in (('price_usd','price_usd'),('area_sqm','area_m2'),
                ('rooms','rooms'),('bedrooms','bedrooms'),('city_id','city_id'),
                ('district_id','district_id')):
                if agent.get(key) is None:agent[key]=row.get(source_key)
            row['details_loaded']=True
            agent_urls=list(agent.get('image_urls') or [])[:3]
            row['images_count']=len(agent.get('image_urls') or [])
            row['photo_urls']=list(agent.get('image_urls') or [])
            row['agent_photos_selected']=len(agent_urls)
            progress(self.s,'owner_search')
            owners,coverage_complete,trace=await self.owner_candidates(agent,q)
            row['owner_search_trace']=trace
            row['candidate_coverage_complete']=coverage_complete
            candidate_proofs=[];photo_complete=True;winner=None
            if not owners:
                row['photo_coverage_complete']=True
                row.update(matching_status='NOT_FOUND' if coverage_complete else 'SEARCH_INCOMPLETE',
                    reason='No owners found in one exact search' if coverage_complete else 'Owner search incomplete')
                return
            async with httpx.AsyncClient(trust_env=False,follow_redirects=False) as client:
                # Download source photos lazily (first three only), stopping on
                # the first similarity > 0.95 without fetching later photos.
                for owner in owners:
                    if self.cancelled():raise asyncio.CancelledError()
                    progress(self.s,'photo_comparison',last_listing_id=ident)
                    try:od=await self.detail(owner['listing_id'])
                    except SourceError:
                        self.s['metrics']['skipped']+=1
                        coverage_complete=False;photo_complete=False
                        continue
                    owner_urls=list(od.get('image_urls') or [])
                    # Agent source (first 3) vs every candidate owner image.
                    found_pair,complete,diagnostic=await compare_photo_sets(
                        self,client,agent_urls,owner_urls,'agent_photo','owner_photo')
                    if not complete:
                        photo_complete=False;coverage_complete=False
                    candidate_proofs.append({'owner_id':owner['listing_id'],**diagnostic})
                    if found_pair:
                        winner=(owner,od,found_pair)
                        break
            row['candidate_photo_coverage']=candidate_proofs
            if winner:
                owner,od,pair=winner
                # A successful early stop is goal-complete even if pages or
                # later candidates were not scanned after the matching photo.
                row['comparison_goal_complete']=True
                row['photo_coverage_complete']=photo_complete
                row['candidate_coverage_complete']=coverage_complete
                row.update(matching_status='MATCH_CONFIRMED',
                    original_owner_id=owner['listing_id'],
                    original_owner_url=od.get('url') or owner.get('url'),
                    original_owner_price_usd=od.get('price_usd') if od.get('price_usd') is not None else owner.get('price_usd'),
                    price_difference_usd=round((row.get('price_usd') or 0)-(owner.get('price_usd') or 0),2),
                    matched_photo_count=1,match_method=pair['method'],
                    histogram_similarity=pair.get('hist_correlation'),
                    photo_similarity=pair.get('similarity'),confidence='HIGH',
                    match_evidence=[pair],
                    reason=('HIST_PHASH similarity >0.95' if pair['method']=='HIST_PHASH' else 'SIFT + RANSAC: geometric match confirmed')+'; source: first three agent photos; comparison stopped.')
                self.s['metrics']['matches_confirmed']+=1
            else:
                row['photo_coverage_complete']=photo_complete
                row['candidate_coverage_complete']=bool(coverage_complete and photo_complete)
                if not agent_urls or not photo_complete:
                    row.update(matching_status='SEARCH_INCOMPLETE',
                        reason='No confirmed match; some images or candidates were not checked')
                else:
                    row.update(matching_status='NOT_FOUND',
                        reason='All available candidates checked; HIST_PHASH >0.95 or SIFT_RANSAC confirmation not found')
        except asyncio.CancelledError:raise
        except Exception as e:
            row['matching_status']='MATCH_ERROR';row['error']=safe_error(e)
            self.s['metrics']['errors']+=1
        finally:
            self.s['metrics']['agent_processed']+=1


def agent_match_fully_checked(row):
    """One unit of progress = one agent fully checked against owner/photo scope."""
    if row.get('matching_status')=='MATCH_CONFIRMED' and row.get('comparison_goal_complete') is True:
        return True
    if row.get('matching_status')!='NOT_FOUND':return False
    return row.get('candidate_coverage_complete') is True and row.get('photo_coverage_complete',True) is True


async def run_agent_snapshot(state,q,snapshot):
    """Process the frozen verified agent-ID set, not mutable pages twice."""
    runtime=JobRuntime(state)
    state['status']='RUNNING';progress(state,'agent_snapshot_matching')
    stop_reason=None
    try:
        for hit in snapshot['rows']:
            if runtime.cancelled():
                state['status']='CANCELLED';stop_reason='USER_CANCELLED';break
            ident=hit['listing_id']
            row=base_row(hit,'AGENT')
            save_row(state['job_id'],row)
            state['metrics']['rows_collected']+=1
            progress(state,'agent_matching',last_listing_id=ident)
            if runtime.match_circuit_open:
                row.update(matching_status='MATCH_ERROR',error='Five errors: check stopped')
            else:
                await runtime.match(row,q)
            state['metrics']['agent_attempted']+=1
            if agent_match_fully_checked(row):
                row['processing_status']='COMPARISON_COMPLETE'
                state['metrics']['listings_processed']+=1
            else:
                row['processing_status']='COMPARISON_INCOMPLETE'
                state['metrics']['agent_incomplete']+=1
            save_row(state['job_id'],row)
            progress(state,'agent_matching',last_listing_id=ident)
        if state['status']=='RUNNING':
            stop_reason='AGENT_SNAPSHOT_EXHAUSTED'
            if state['metrics']['agent_incomplete']:
                state['status']='PARTIAL';state['match_coverage']='INCOMPLETE'
            else:
                state['status']='COMPLETED';state['match_coverage']='MATCH_FOUND_OR_SINGLE_SEARCH_EXHAUSTED'
    except asyncio.CancelledError:
        state['status']='INTERRUPTED';state['error']='Operation interrupted; results saved'
        stop_reason='INTERRUPTED'
    except Exception as e:
        state['status']='PARTIAL' if state['metrics']['rows_collected'] else 'FAILED'
        state['error']=safe_error(e);state['metrics']['errors']+=1
        stop_reason='UPSTREAM_FAILED'
    finally:
        state['stop_reason']=stop_reason
        # The denominator is the enumerated agent snapshot, never the mixed total.
        # Preserve denominator even after cancellation/errors.
        progress(state,'done' if state['status']=='COMPLETED' else 'partial')
        ACTIVE.pop(state['job_id'],None)


async def run_owner_snapshot(state,snapshot):
    # Turn the already verified preflight snapshot into durable table rows.
    # Start materializes real SSR listings without repeated MyHome requests.
    state['status']='RUNNING'; progress(state,'collecting')
    try:
        evidence={k:v for k,v in snapshot.items() if k!='rows'}
        state['source_end_evidence']=evidence['end_reason']
        state['metrics']['pages_visited']=evidence['pages_visited']
        state['metrics']['listing_pages']=evidence['listing_pages']
        state['metrics']['source_examined']=evidence['raw_rows']
        state['metrics']['source_eligible']=len(snapshot['rows'])
        state['metrics']['source_excluded']=evidence['excluded_rows']
        progress(state,'collecting')
        for hit in snapshot['rows']:
            if state.get('cancel_requested'):
                state['status']='CANCELLED';state['stop_reason']='USER_CANCELLED';break
            row=base_row(hit,'OWNER')
            save_row(state['job_id'],row)
            state['metrics']['rows_collected']+=1
            state['metrics']['listings_processed']+=1
            progress(state,'table_write',last_listing_id=hit['listing_id'])
            # Permit UI polling / cancellation during larger snapshot writes.
            await asyncio.sleep(0.025)
        if state['status']=='RUNNING':
            state['status']='COMPLETED';state['stop_reason']='OWNER_SNAPSHOT_EXHAUSTED'
            state['scope_total_actual']=state['metrics']['rows_collected']
            state['scope_total_provenance']='COMPLETED_OWNER_SNAPSHOT_WRITE'
    except asyncio.CancelledError:
        state['status']='INTERRUPTED';state['error']='Operation interrupted; saved rows remain';state['stop_reason']='INTERRUPTED'
    except Exception as e:
        state['status']='PARTIAL' if state['metrics']['rows_collected'] else 'FAILED'
        state['error']=safe_error(e);state['metrics']['errors']+=1;state['stop_reason']='SNAPSHOT_WRITE_FAILED'
    finally:
        progress(state,'done' if state['status']=='COMPLETED' else 'partial')
        ACTIVE.pop(state['job_id'],None)


async def run_job(state,q):
    runtime=JobRuntime(state)
    state['status']='RUNNING';progress(state,'collecting')
    seen=set();stop_reason=None
    try:
        page=state['selected_page_from'];seen_source_pages=set();short_page_pending=False;empty_streak=0
        while state['selected_page_to'] is None or page<=state['selected_page_to']:
            if runtime.cancelled():state['status']='CANCELLED';stop_reason='USER_CANCELLED';break
            progress(state,'collecting',current_page=page)
            try:items=await runtime.search(replace(q,page=page))
            except SourceError as e:
                state['error']=safe_error(e);state['metrics']['errors']+=1
                state['status']='PARTIAL' if seen else 'FAILED'
                stop_reason='ACCESS_BLOCKED' if e.code=='UPSTREAM_ACCESS_DENIED' else 'UPSTREAM_FAILED'
                break
            state['metrics']['pages_visited']+=1
            state['metrics']['source_excluded']+=getattr(items,'excluded_count',0)
            source_count=getattr(items,'raw_count',len(items))
            state['metrics']['source_examined']+=source_count
            state['metrics']['source_eligible']+=len(items)
            if source_count==0:
                empty_streak+=1
                if empty_streak>=2:
                    stop_reason='SOURCE_END'
                    state['source_end_evidence']='TWO_EMPTY_SSR_PAGES'
                    break
                page+=1
                continue
            empty_streak=0
            state['metrics']['listing_pages']+=1
            raw_ids=tuple(getattr(items,'raw_ids',None) or [r['listing_id'] for r in items])
            if raw_ids and raw_ids in seen_source_pages:
                stop_reason='SOURCE_END_UNVERIFIED';state['status']='PARTIAL';break
            if raw_ids:seen_source_pages.add(raw_ids)
            # Do not claim the next nonempty page is a failure just because
            # the preceding page had fewer than PER_PAGE RAW cards.
            # Only a verified source end or an explicit selected bound ends a job.
            for hit in items:
                if runtime.cancelled():state['status']='CANCELLED';stop_reason='USER_CANCELLED';break
                if hit['listing_id'] in seen:continue
                role=hit.get('owner_type')
                if state['mode']=='OWNER' and role!='physical' or state['mode']=='AGENT' and role!='agent':
                    state['metrics']['skipped']+=1;continue
                seen.add(hit['listing_id'])
                row=base_row(hit,state['mode']);save_row(state['job_id'],row)
                state['metrics']['rows_collected']+=1
                progress(state,'table_write',last_listing_id=hit['listing_id'])
                if state['mode']=='AGENT':
                    if runtime.match_circuit_open:row.update(matching_status='MATCH_ERROR',error='Stopped after five errors')
                    else:await runtime.match(row,q)
                    save_row(state['job_id'],row)
                if state['mode']=='AGENT':
                    state['metrics']['agent_attempted']+=1
                    if agent_match_fully_checked(row):
                        row['processing_status']='COMPARISON_COMPLETE'
                        state['metrics']['listings_processed']+=1
                    else:
                        row['processing_status']='COMPARISON_INCOMPLETE'
                        state['metrics']['agent_incomplete']+=1
                    save_row(state['job_id'],row)
                else:
                    state['metrics']['listings_processed']+=1
                progress(state,'collecting',last_listing_id=hit['listing_id'])
                if state['max_rows'] is not None and len(seen)>=state['max_rows']:
                    stop_reason='ROW_LIMIT_REACHED';break
            if stop_reason:break
            if source_count<PER_PAGE:
                short_page_pending=True
                # In a user-chosen finite range, the boundary is the requested end.
                if state['selected_page_to'] is not None and page>=state['selected_page_to']:
                    stop_reason='EXACT_SCOPE_COMPLETE';break
            page+=1
        if state['status']=='RUNNING':
            if state['selected_page_to'] is None and stop_reason is None:
                state['status']='PARTIAL';stop_reason='SOURCE_END_UNVERIFIED'
            else:
                state['status']='PARTIAL' if state['metrics']['errors'] else 'COMPLETED'
                stop_reason=stop_reason or 'EXACT_SCOPE_COMPLETE'
            if state['mode']=='AGENT' and state['metrics']['agent_incomplete']:
                state['match_coverage']='INCOMPLETE';state['status']='PARTIAL'
    except asyncio.CancelledError:
        state['status']='INTERRUPTED';state['error']='Operation interrupted; rows saved';stop_reason='UPSTREAM_FAILED'
    except Exception as e:
        state['status']='PARTIAL' if state['metrics']['rows_collected'] else 'FAILED'
        state['error']=safe_error(e);state['metrics']['errors']+=1;stop_reason='UPSTREAM_FAILED'
    finally:
        state['stop_reason']=stop_reason
        # Enumeration total is known only after the explicitly selected scope ends.
        # A row-limit stop does not prove a complete global total.
        if stop_reason in ('SOURCE_END','EXACT_SCOPE_COMPLETE') and state['metrics']['errors']==0:
            state['scope_total_actual']=state['metrics']['rows_collected']
            state['scope_total_provenance']='COMPLETED_SCOPE_ENUMERATION'
        progress(state,'done' if state['status']=='COMPLETED' else 'partial')
        ACTIVE.pop(state['job_id'],None)


@router.post('/jobs')
async def start_job(payload:dict):
    token=payload.get('preflight_id') if isinstance(payload,dict) else None
    p=PREP.pop(token,None)
    if not p or time.monotonic()-p['ts']>PREFLIGHT_TTL:
        return JSONResponse(status_code=422,content={'error_code':'PREFLIGHT_REQUIRED','detail':'Run the count check again'})
    res=p['result']
    if not res['can_start']:
        return JSONResponse(status_code=422,content={'error_code':'PREFLIGHT_BLOCKED','detail':'; '.join(res['blockers'])})
    if any(not t.done() for t in ACTIVE.values()):
        return JSONResponse(status_code=409,content={'error_code':'JOB_ALREADY_RUNNING'})
    jid=uuid.uuid4().hex
    first,last=p['selected'];page_cap=p['pages'];row_cap=p['rows']
    state={'job_id':jid,'status':'PENDING','mode':p['mode'],'filters':p['q'].as_dict(),
           'page_mode':p['scope'].pages_mode,'listing_mode':p['scope'].listings_mode,
           'selected_page_from':first,'selected_page_to':last,
           'max_pages':page_cap,'planned_max_pages':page_cap,'max_rows':row_cap,
           'scope_signature':res['scope_signature'],
           'source_end_evidence':None,
           'count_status':res['source_total_status'],'site_total':res['source_total'],
           'agent_filter_status':res['agent_role_handling'],
           'agent_snapshot_evidence':res.get('enumeration_evidence') if p.get('agent_snapshot') is not None else None,
           'owner_snapshot_evidence':res.get('enumeration_evidence') if p.get('owner_snapshot') is not None else None,
           'phase':'pending','created_at':clock(),'updated_at':clock(),
           'current_page':0,'last_listing_id':None,'error':None,'stop_reason':None,
           'cancel_requested':False,
           'scope_total_expected':res['scope_total_expected'],
           'scope_total_provenance':res['scope_total_provenance'],
           'scope_total_actual':None,'progress_percent':None,
           'metrics':{'pages_visited':0,'listing_pages':0,'search_calls':0,'detail_calls':0,'owner_search_calls':0,
              'owner_pages_visited':0,'owner_listing_pages':0,'owner_candidates_examined':0,'owner_candidate_shortlisted':0,
              'photo_calls':0,'photo_errors':0,'image_comparisons':0,'image_pairs_attempted':0,'image_pairs_failed':0,'rows_collected':0,
              'source_examined':0,'source_eligible':0,'listings_processed':0,
              'agent_processed':0,'agent_attempted':0,'agent_incomplete':0,'matches_confirmed':0,'matches_to_review':0,
              'errors':0,'skipped':0,'source_excluded':0}}
    save_state(state)
    if p.get('agent_snapshot') is not None:
        ACTIVE[jid]=asyncio.create_task(run_agent_snapshot(state,p['q'],p['agent_snapshot']))
    elif p.get('owner_snapshot') is not None:
        ACTIVE[jid]=asyncio.create_task(run_owner_snapshot(state,p['owner_snapshot']))
    else:
        ACTIVE[jid]=asyncio.create_task(run_job(state,p['q']))
    return {'job_id':jid,'status':'PENDING','status_url':f'/api/jobs/{jid}'}



# ID lookups share the same durable job/row/export model as bulk collection.
def _single_search_query(detail):
    city=detail.get('city_id')
    deal=detail.get('deal_type_id')
    estate=detail.get('real_estate_type_id')
    if not all(type(x) is int and x>0 for x in (city,deal,estate)):
        raise SourceError('INSUFFICIENT_MATCH_ANCHORS','Listing is missing verified city, deal or property IDs')
    return SearchQuery(cities=(city,),deal_types=(deal,),real_estate_types=(estate,),currency_id=2)


def _single_detail_hit(detail):
    return {'listing_id':detail['listing_id'],'url':detail['url'],
       'owner_type':detail.get('owner_type'), 'city_id':detail.get('city_id'),
       'city_name':detail.get('city'),'district_id':detail.get('district_id'),
       'district_name':detail.get('district'),'urban_name':detail.get('sub_district'),
       'price_usd':detail.get('price_usd'),'area_m2':detail.get('area_sqm'),
       'rooms':detail.get('rooms'),'bedrooms':detail.get('bedrooms'),
       'image_urls':detail.get('image_urls') or [],'listing_updated_at':detail.get('published_date')}


def _single_match_settings(raw=None):
    """Both ID directions derive ranges from the source listing, never UI."""
    if raw is not None:
        raise QueryIssue('MANUAL_OFFSETS_DISABLED','Manual ranges are disabled in search by ID')
    return None


def _owner_to_agent_ranges(price,area):
    """Owner ID -> agent listings: exact user-approved auto price bands."""
    if price is None or area is None or price<=0 or area<=0:
        raise SourceError('INSUFFICIENT_MATCH_ANCHORS','Listing has no positive price or area')
    p_lo,p_hi=(-100,50) if price<=1100 else (-200,100)
    return (max(0,math.floor(price+p_lo)),max(0,math.ceil(price+p_hi)),
            max(0,math.floor(area-5)),max(0,math.ceil(area+10)))


def _single_source_summary(detail,direction):
    """Expose automatically applied filters without personal contact data."""
    price=detail.get('price_usd');area=detail.get('area_sqm')
    if direction=='OWNER_TO_AGENTS':
        pl,ph,al,ah=_owner_to_agent_ranges(price,area)
    else:
        if price is None or area is None or price<=0 or area<=0:
            raise SourceError('INSUFFICIENT_MATCH_ANCHORS','Listing has no positive price or area')
        lo,hi=(-50,100) if price<=900 else (-100,200)
        pl,ph=max(0,math.floor(price+lo)),math.ceil(price+hi)
        al,ah=max(0,math.floor(area-10)),math.ceil(area+5)
    return {'listing_id':detail['listing_id'],'role':detail.get('owner_type'),
        'city':detail.get('city'),'district':detail.get('district'),
        'city_id':detail.get('city_id'),'district_id':detail.get('district_id'),
        'rooms':detail.get('rooms'),'bedrooms':detail.get('bedrooms'),
        'price_usd':price,'area_sqm':area,
        'price_from':pl,'price_to':ph,'area_from':al,'area_to':ah,
        'source_photo_count':min(3,len(detail.get('image_urls') or [])),
        'candidate_role':'agent' if direction=='OWNER_TO_AGENTS' else 'physical'}


async def reverse_agents_for_owner(rt, owner, q, offsets=None, *, mixed=False):
    """One reverse search, all pages, strict role+geography+rooms+bedrooms."""
    city,district,rooms,bedrooms=(owner.get(k) for k in ('city_id','district_id','rooms','bedrooms'))
    price,area=owner.get('price_usd'),owner.get('area_sqm')
    if any(x is None for x in (city,district,rooms,bedrooms,price,area)):
        raise SourceError('INSUFFICIENT_MATCH_ANCHORS','Owner listing is missing city, district, rooms, bedrooms, price or area')
    if min(city,district,rooms,price,area)<=0 or bedrooms<0:
        raise SourceError('INVALID_MATCH_ANCHORS','Invalid owner listing values')
    price_lo,price_hi,area_lo,area_hi=_owner_to_agent_ranges(price,area)
    query=replace(q,owner_type=None if mixed else 'agent',page=1,
        cities=(int(city),),districts=(int(district),),
        room_types=(int(rooms),),bedroom_types=(int(bedrooms),),
        price_from=price_lo,price_to=price_hi,
        area_from=area_lo,area_to=area_hi,
        urbans=None,statuses=None)
    if query.price_to<query.price_from or query.area_to<query.area_from:
        raise SourceError('INVALID_MATCH_RANGE','Range is outside allowed values')
    rows={};fingerprints=set();page=1;empties=0;nonempty=0
    while True:
        if rt.cancelled():raise asyncio.CancelledError()
        rt.s['metrics']['owner_search_calls']+=1
        batch=await rt.search(replace(query,page=page))
        if not mixed and getattr(batch,'seller_role_mismatches',0):
            # Re-run using mixed results; never label a mixed response agent-only.
            return await reverse_agents_for_owner(rt,owner,q,mixed=True)
        rt.s['metrics']['owner_pages_visited']+=1
        raw=getattr(batch,'raw_count',len(batch))
        ids=tuple(getattr(batch,'raw_ids',None) or [x['listing_id'] for x in batch])
        if raw and raw!=len(ids):raise SourceError('RAW_PAGE_IDS_UNAVAILABLE','Could not verify the full ID set')
        if raw and ids in fingerprints:raise SourceError('REPEATED_SOURCE_PAGE','Results repeat an earlier page')
        if raw:
            fingerprints.add(ids);nonempty+=1;empties=0
            rt.s['metrics']['owner_listing_pages']=rt.s['metrics'].get('owner_listing_pages',0)+1
        else:empties+=1
        candidates=getattr(batch,'agent_role_rows',None)
        if candidates is None:candidates=batch
        for hit in candidates:
            if hit.get('owner_type')!='agent':continue
            if any(hit.get(k)!=value for k,value in (('city_id',int(city)),('district_id',int(district)),('rooms',int(rooms)),('bedrooms',int(bedrooms)))):continue
            # SSR search must obey desired price/area ranges, but tolerate its
            # own per-card rounding; warn for discrepancies rather than invent matches.
            if hit['listing_id']!=owner['listing_id']:
                rows.setdefault(hit['listing_id'],hit)
        if empties==2:break
        page+=1
    rt.s['metrics']['listing_pages']=nonempty
    rt.s['metrics']['pages_visited']=page
    return list(rows.values())


async def compare_agent_to_selected_owner(rt,agent,owner):
    """Owner (first 3) -> ALL photos of candidate agent, two-stage matching.
    The public return signature remains (proof|None, complete) for R3.1.9.
    """
    owner_urls=list(owner.get('image_urls') or [])[:3]
    agent_urls=list(agent.get('image_urls') or [])
    if not owner_urls or not agent_urls:return None,False
    async with httpx.AsyncClient(trust_env=False,follow_redirects=False) as client:
        proof,complete,_=await compare_photo_sets(rt,client,owner_urls,agent_urls,
                                                  'owner_photo','agent_photo')
        return proof,complete


async def run_single_job(state,ident,direction,offsets):
    rt=JobRuntime(state)
    state['status']='RUNNING';progress(state,'agent_details',last_listing_id=ident)
    try:
        detail=await rt.detail(ident)
        expected_role='agent' if direction=='AGENT_TO_OWNER' else 'physical'
        if detail.get('owner_type')!=expected_role:
            raise SourceError('SELLER_ROLE_MISMATCH',f'Listing {ident} does not have seller role {expected_role}')
        q=_single_search_query(detail)
        state['single_filters']=_single_source_summary(detail,direction)
        progress(state,'agent_details')
        if direction=='AGENT_TO_OWNER':
            row=base_row(_single_detail_hit(detail),'AGENT')
            save_row(state['job_id'],row);state['metrics']['rows_collected']+=1
            state['scope_total_expected']=1;progress(state,'owner_search')
            await rt.match(row,q)
            row['processing_status']='COMPARISON_COMPLETE' if agent_match_fully_checked(row) else 'COMPARISON_INCOMPLETE'
            if agent_match_fully_checked(row):state['metrics']['listings_processed']+=1
            else:state['metrics']['agent_incomplete']+=1
            save_row(state['job_id'],row)
        else:
            progress(state,'owner_search')
            agents=await reverse_agents_for_owner(rt,detail,q)
            state['scope_total_expected']=len(agents)
            state['metrics']['source_examined']=len(agents)
            progress(state,'photo_comparison')
            for hit in agents:
                if rt.cancelled():raise asyncio.CancelledError()
                # Incremental output: a candidate exists before photo matching.
                row=base_row(hit,'AGENT')
                row.update(matching_status='PENDING',processing_status='DETAIL_PENDING',
                           reason='Candidate found using automatic filters')
                save_row(state['job_id'],row)
                state['metrics']['rows_collected']+=1
                progress(state,'agent_details',last_listing_id=hit['listing_id'])
                try:
                    agent=await rt.detail(hit['listing_id'])
                    if agent.get('owner_type')!='agent':
                        raise SourceError('SELLER_ROLE_MISMATCH','Candidate is no longer an agent')
                    fields=('city_id','district_id','rooms','bedrooms','deal_type_id','real_estate_type_id')
                    if any(agent.get(k)!=detail.get(k) for k in fields):
                        raise SourceError('CANDIDATE_DETAIL_MISMATCH','Candidate details do not match ID-derived filters')
                    limits=state['single_filters']
                    if agent.get('price_usd') is None or agent.get('area_sqm') is None or not (
                        limits['price_from']<=agent['price_usd']<=limits['price_to'] and
                        limits['area_from']<=agent['area_sqm']<=limits['area_to']):
                        raise SourceError('CANDIDATE_DETAIL_MISMATCH','Candidate price or area is outside the automatic range')
                except SourceError as exc:
                    row.update(matching_status='SEARCH_INCOMPLETE',processing_status='DETAIL_INCOMPLETE',
                               reason='Could not verify candidate parameters: '+exc.code)
                    save_row(state['job_id'],row)
                    state['metrics']['agent_incomplete']+=1
                    progress(state,'photo_comparison',last_listing_id=hit['listing_id'])
                    continue
                row.update(_single_detail_hit(agent))
                row['photo_urls']=list(agent.get('image_urls') or [])
                row['images_count']=len(row['photo_urls'])
                row['processing_status']='PHOTO_PENDING'
                save_row(state['job_id'],row)
                progress(state,'photo_comparison',last_listing_id=hit['listing_id'])
                proof,complete=await compare_agent_to_selected_owner(rt,agent,detail)
                if proof:
                    row.update(matching_status='MATCH_CONFIRMED',original_owner_id=ident,
                        original_owner_url=detail['url'],original_owner_price_usd=detail.get('price_usd'),
                        matched_photo_count=1,match_method=proof['method'],
                        photo_similarity=proof.get('similarity'),
                        histogram_similarity=proof.get('hist_correlation'),confidence='HIGH',
                        match_evidence=[proof],comparison_goal_complete=True,
                        reason=('HIST_PHASH >0.95' if proof['method']=='HIST_PHASH' else 'SIFT + RANSAC geometry confirmed')+'; source: first three owner photos',
                        processing_status='COMPARISON_COMPLETE')
                    state['metrics']['matches_confirmed']+=1
                elif complete:
                    row.update(matching_status='NOT_FOUND',processing_status='COMPARISON_COMPLETE',
                        reason='All candidate photos checked; HIST_PHASH >0.95 or SIFT_RANSAC confirmation not found')
                else:
                    row.update(matching_status='SEARCH_INCOMPLETE',processing_status='COMPARISON_INCOMPLETE',
                        reason='Some photos are unavailable; result unverified')
                    state['metrics']['agent_incomplete']+=1
                save_row(state['job_id'],row)
                state['metrics']['listings_processed']+=1
                progress(state,'photo_comparison',last_listing_id=hit['listing_id'])
        if state['metrics']['agent_incomplete']:
            state['status']='PARTIAL'
        else:state['status']='COMPLETED'
    except asyncio.CancelledError:
        state['status']='CANCELLED';state['error']='Stopped by user'
    except Exception as exc:
        state['status']='PARTIAL' if state['metrics']['rows_collected'] else 'FAILED'
        state['metrics']['errors']+=1;state['error']=safe_error(exc)
    finally:
        progress(state,'done' if state['status']=='COMPLETED' else 'partial')
        ACTIVE.pop(state['job_id'],None)


@router.post('/single/jobs')
async def start_single_job(payload:dict):
    if not isinstance(payload,dict):
        return JSONResponse(status_code=422,content={'error_code':'INVALID_PAYLOAD'})
    try:
        ident=validate_listing_id(str(payload.get('listing_id','')))
        direction=payload.get('direction')
        if direction not in ('AGENT_TO_OWNER','OWNER_TO_AGENTS'):
            raise QueryIssue('INVALID_DIRECTION','Choose search direction')
        offsets=_single_match_settings(payload.get('reverse_offsets'))
    except (QueryIssue,ValueError) as exc:
        return JSONResponse(status_code=422,content={'error_code':getattr(exc,'code','INVALID_LISTING_ID'),'detail':str(exc)})
    if any(not t.done() for t in ACTIVE.values()):
        return JSONResponse(status_code=409,content={'error_code':'JOB_ALREADY_RUNNING','detail':'Another search is already running'})
    jid=uuid.uuid4().hex
    metrics={'pages_visited':0,'listing_pages':0,'search_calls':0,'detail_calls':0,
        'owner_search_calls':0,'owner_pages_visited':0,'owner_listing_pages':0,'owner_candidates_examined':0,
        'owner_candidate_shortlisted':0,'photo_calls':0,'photo_errors':0,
        'image_comparisons':0,'image_pairs_attempted':0,'image_pairs_failed':0,
        'rows_collected':0,'source_examined':0,'source_eligible':0,
        'listings_processed':0,'agent_processed':0,'agent_attempted':0,
        'agent_incomplete':0,'matches_confirmed':0,'matches_to_review':0,
        'errors':0,'skipped':0,'source_excluded':0}
    state={'job_id':jid,'status':'PENDING','mode':'AGENT','single_direction':direction,
        'target_id':ident,'phase':'pending','created_at':clock(),'updated_at':clock(),
        'last_listing_id':ident,'cancel_requested':False,'error':None,
        'scope_total_expected':None,'scope_total_actual':None,'metrics':metrics}
    save_state(state)
    ACTIVE[jid]=asyncio.create_task(run_single_job(state,ident,direction,offsets))
    return {'job_id':jid,'status':'PENDING','status_url':f'/api/jobs/{jid}'}


@router.get('/jobs/{job_id}')
async def get_job(job_id:str):
    s=load_state(job_id)
    return s if s else JSONResponse(status_code=404,content={'error_code':'NOT_FOUND'})

@router.get('/jobs/{job_id}/rows')
async def get_rows(job_id:str,limit:int=100,offset:int=0):
    if not load_state(job_id):return JSONResponse(status_code=404,content={'error_code':'NOT_FOUND'})
    page_size=max(1,min(limit,2000))
    selected=rows_for(job_id,page_size,max(0,offset))
    return {'rows':selected,'offset':offset,'page_size':page_size,
            'has_more':len(selected)==page_size and bool(rows_for(job_id,1,offset+page_size))}

@router.post('/jobs/{job_id}/cancel')
async def cancel_job(job_id:str):
    s=load_state(job_id)
    if not s:return JSONResponse(status_code=404,content={'error_code':'NOT_FOUND'})
    if s['status'] in ('PENDING','RUNNING'):
        s['cancel_requested']=True;s['status']='CANCELLING';save_state(s)
    return {'job_id':job_id,'status':s['status']}

FIELDS=['listing_id','photo_urls','row_mode','seller_type','listing_updated_at','title','city_name','district_name','urban_name','price_usd','price_gel','area_m2','rooms','bedrooms','floor','url','images_count','processing_status','matching_status','original_owner_id','original_owner_url','original_owner_price_usd','price_difference_usd','matched_photo_count','match_method','histogram_similarity','photo_similarity','confidence','reason','error','match_evidence','candidate_photo_coverage','owner_search_trace','candidate_coverage_complete']

def excel_safe(val):
    if val is None:return ''
    if isinstance(val,(list,dict)):val=json.dumps(val,ensure_ascii=False)
    s=str(val)
    if s.startswith(('=','+','-','@','\t','\r')):return "'"+s
    return s

@router.get('/jobs/{job_id}/export')
async def export_rows(job_id:str,format:str='csv'):
    if not load_state(job_id):return JSONResponse(status_code=404,content={'error_code':'NOT_FOUND'})
    records=rows_for(job_id)
    if format=='csv':
        output=io.StringIO();writer=csv.writer(output);writer.writerow(FIELDS)
        for record in records:writer.writerow([excel_safe(record.get(k)) for k in FIELDS])
        blob=output.getvalue().encode('utf-8-sig')
        return StreamingResponse(io.BytesIO(blob),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="listinglens_{job_id[:8]}.csv"'})
    if format=='xlsx':
        wb=Workbook();ws=wb.active;ws.title='ListingLens';ws.append(FIELDS)
        for i,k in enumerate(FIELDS,1):ws.cell(1,i).font=Font(bold=True,color='64CE96')
        for record in records:
            ws.append([excel_safe(record.get(k)) for k in FIELDS])
            for key in ('url','original_owner_url'):
                if record.get(key):
                    col=FIELDS.index(key)+1;cell=ws.cell(ws.max_row,col)
                    if str(record[key]).startswith('https://www.myhome.ge/'):
                        cell.hyperlink=record[key];cell.font=Font(color='008855',underline='single')
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for i in range(1,len(FIELDS)+1):ws.column_dimensions[ws.cell(1,i).column_letter].width=25
        buf=io.BytesIO();wb.save(buf);buf.seek(0)
        return StreamingResponse(buf,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':f'attachment; filename="listinglens_{job_id[:8]}.xlsx"'})
    return JSONResponse(status_code=422,content={'error_code':'INVALID_EXPORT_FORMAT'})
