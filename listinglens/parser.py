"""Strict parsing and reconciliation of server-side Next.js result data."""
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit
from .search import SearchQuery


class SourceError(Exception):
    def __init__(self, code: str, detail: str, http_status: int = 502):
        super().__init__(detail)
        self.code = code
        self.http_status = http_status


_NEXT = re.compile(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.I | re.S)


def next_data(html: str) -> Dict[str, Any]:
    if not html or "captcha" in html[:1000].lower() and "__NEXT_DATA__" not in html:
        raise SourceError("UPSTREAM_CHALLENGE", "MyHome returned no search HTML")
    m = _NEXT.search(html)
    if m is None:
        raise SourceError("UPSTREAM_NEXT_DATA_MISSING", "MyHome HTML is missing __NEXT_DATA__")
    try:
        obj = json.loads(m.group(1))
    except (ValueError, TypeError):
        raise SourceError("UPSTREAM_INVALID_JSON", "Invalid __NEXT_DATA__")
    if not isinstance(obj, dict):
        raise SourceError("UPSTREAM_INVALID_SCHEMA", "The root __NEXT_DATA__ is not an object")
    return obj


def _normalize_key_params(raw: Any) -> Optional[Dict[str, str]]:
    if not isinstance(raw, dict):
        return None
    return {str(k): str(v) for k, v in raw.items()}


def check_query(actual: Any, wanted: Dict[str, str], label: str):
    p = _normalize_key_params(actual)
    if p is None:
        raise SourceError("UPSTREAM_FILTER_MISMATCH", f"{label}: filters could not be verified")
    for k, v in wanted.items():
        if p.get(k) != v:
            raise SourceError("UPSTREAM_FILTER_MISMATCH", f"{label}: {k} differs from the request")


def extract(html: str, query: SearchQuery, effective_url: str, *, verify_listings: bool = True) -> List[Dict[str, Any]]:
    # Strict per-item validation is the default and remains unchanged for the
    # public search_listings endpoint. Workflow can validate individual hits
    # separately after the full server-side filter echo has been confirmed.

    target_url = urlsplit(effective_url)
    if target_url.hostname != "www.myhome.ge" or target_url.scheme != "https" or target_url.path != query.route_path():
        raise SourceError("UPSTREAM_REDIRECT", "MyHome redirected the request to a different URL")
    document = next_data(html)
    if document.get("page") != "/[...slug]":
        raise SourceError("UPSTREAM_FILTER_MISMATCH", "Unexpected Next.js page type")
    query_in_document = document.get("query")
    if not isinstance(query_in_document, dict):
        raise SourceError("UPSTREAM_FILTER_MISMATCH", "Next.js did not verify the page parameters")
    check_query(query_in_document, query.upstream_params(), "Next.js query")
    page_props = (document.get("props") or {}).get("pageProps") or {}
    if not isinstance(page_props, dict):
        raise SourceError("UPSTREAM_INVALID_SCHEMA", "Missing pageProps")
    if query.city is not None:
        seo=(page_props.get('initialSeoParams') or {}).get('slugs')
        if not isinstance(seo,list) or query.city not in seo:
            raise SourceError('UPSTREAM_FILTER_MISMATCH','Source SEO parameters did not verify the city')
    request_args = page_props.get("initialFetchArgs") or {}
    check_query(request_args.get("query"), query.upstream_params(), "initialFetchArgs")
    query_records = ((page_props.get("dehydratedState") or {}).get("queries"))
    if not isinstance(query_records, list):
        raise SourceError("UPSTREAM_INVALID_SCHEMA", "Missing dehydratedState.queries")
    matches = []
    for record in query_records:
        if not isinstance(record, dict):
            continue
        key = record.get("queryKey")
        if not (isinstance(key, list) and len(key) >= 3 and key[:2] == ["statements", "list"]):
            continue
        if not isinstance(key[2], dict):
            continue
        check_query(key[2].get("query"), query.upstream_params(), "statements/list queryKey")
        raw = (record.get("state") or {}).get("data")
        if not isinstance(raw, dict) or raw.get("result") is False:
            raise SourceError("UPSTREAM_INVALID_SCHEMA", "Invalid statements/list result")
        envelope = raw.get("data")
        if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
            raise SourceError("UPSTREAM_INVALID_SCHEMA", "Missing statements/list listings array")
        matches.append(envelope["data"])
    if len(matches) != 1:
        raise SourceError("UPSTREAM_INVALID_SCHEMA", "Expected exactly one verified statements/list")
    rows = matches[0]
    if len(rows) > 24:
        raise SourceError("UPSTREAM_INVALID_SCHEMA", "More than 24 cards received: source format may have changed")
    if not rows:
        # Page might be legitimately empty; source echo is checked above.
        return []
    if any(not isinstance(r, dict) for r in rows):
        raise SourceError("UPSTREAM_INVALID_SCHEMA", "Invalid listing card")
    ids = [str(r.get("id")) for r in rows]
    if any(i in ("None", "", "0") for i in ids) or len(set(ids)) != len(ids):
        raise SourceError("UPSTREAM_INVALID_SCHEMA", "Empty or duplicate listing IDs")
    if verify_listings:
        for item in rows:
            verify_item(item, query)
    return rows


def _safe_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def item_price(item: dict, currency_id: int) -> Optional[float]:
    price = item.get("price")
    if not isinstance(price, dict):
        return None
    record = price.get(str(currency_id)) or price.get(currency_id)
    if not isinstance(record, dict):
        return None
    return _safe_number(record.get("price_total"))


def verify_item(item: Dict[str, Any], query: SearchQuery):
    # Strictly check every user-specified filter; never re-filter a wrong response.
    for key, values in (("deal_type_id", query.deal_types), ("real_estate_type_id", query.real_estate_types),
                        ("city_id", query.cities), ("room", query.room_types), ("bedroom", query.bedroom_types),
                        ("district_id", query.districts), ("urban_id", query.urbans), ("status_id", query.statuses)):
        if values is None: continue
        got = item.get(key)
        if got is None or str(got) not in {str(x) for x in values}:
            raise SourceError("UPSTREAM_FILTER_MISMATCH", f"Listing {item.get('id')}: {key} does not match the request")
    if query.city is not None:
        variants={
          'tbilisi':{'tbilisi','тбилиси','თბილისი'},
          'batumi':{'batumi','батуми','ბათუმი'},
          'kutaisi':{'kutaisi','кутаиси','ქუთაისი'},
        }[query.city]
        actual=str(item.get('city_name') or '').casefold()
        if actual not in variants:
            raise SourceError('UPSTREAM_FILTER_MISMATCH',f"Listing {item.get('id')}: city {actual!r} is not {query.city}")
    if query.owner_type is not None:
        user=item.get("user_type")
        if not isinstance(user,dict) or user.get("type") != query.owner_type:
            raise SourceError("UPSTREAM_FILTER_MISMATCH", f"Listing {item.get('id')}: owner_type does not match")
    if query.area_from is not None or query.area_to is not None:
        area=_safe_number(item.get("area"))
        if area is None or (query.area_from is not None and area < query.area_from) or (query.area_to is not None and area > query.area_to):
            raise SourceError("UPSTREAM_FILTER_MISMATCH", f"Listing {item.get('id')}: area does not match")
    if query.price_from is not None or query.price_to is not None:
        price=item_price(item,query.currency_id)
        if price is None or (query.price_from is not None and price<query.price_from) or (query.price_to is not None and price>query.price_to):
            raise SourceError("UPSTREAM_FILTER_MISMATCH", f"Listing {item.get('id')}: price does not match")


def _nonempty(value: Any) -> Optional[str]:
    return str(value) if value is not None and str(value).strip() else None


def _search_images(item: dict) -> list[str]:
    # Only explicit absolute public image-CDN URLs; no inferred photos.
    urls=[]
    for key in ('photos','images','image_urls'):
        collection=item.get(key)
        if not isinstance(collection,list):continue
        for v in collection:
            if isinstance(v,dict):
                v=v.get('src') or v.get('url') or v.get('image_url')
            if not isinstance(v,str):continue
            p=urlsplit(v)
            if p.scheme=='https' and p.hostname in ('static.my.ge','static-api-statements.tnet.ge') and v not in urls:
                urls.append(v)
    return urls


def normalize(item: Dict[str, Any], query: SearchQuery) -> Dict[str, Any]:
    ident = str(item["id"])
    slug = (item.get("href_lang") or {}).get("en") if isinstance(item.get("href_lang"), dict) else None
    if not slug:
        slug = item.get("dynamic_slug")
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9-]+", slug):
        raise SourceError("UPSTREAM_INVALID_SCHEMA", f"Listing {ident}: slug is unverified")
    area = _safe_number(item.get("area"))
    owner = item.get("user_type")
    return {
        "listing_id": ident,
        "url": f"https://www.myhome.ge/en/real-estate/{slug}-{ident}/" if (item.get("href_lang") or {}).get("en")==slug else f"https://www.myhome.ge/en/pr/{ident}/",
        "title": _nonempty(item.get("dynamic_title")),
        "price": item_price(item, query.currency_id) if query.currency_id is not None else None,
        "currency": ("USD" if query.currency_id == 2 else "GEL") if query.currency_id is not None else None,
        "price_usd": item_price(item, 2),
        "price_gel": item_price(item, 1),
        "area_m2": area,
        "rooms": _safe_optional_int(item.get("room")),
        "bedrooms": _safe_optional_int(item.get("bedroom")),
        "floor": _safe_optional_int(item.get("floor")),
        "total_floors": _safe_optional_int(item.get("total_floors")),
        "city_id": _safe_optional_int(item.get("city_id")),
        "city_name": _nonempty(item.get("city_name")),
        "district_id": _safe_optional_int(item.get("district_id")),
        "district_name": _nonempty(item.get("district_name")),
        "urban_id": _safe_optional_int(item.get("urban_id")),
        "urban_name": _nonempty(item.get("urban_name")),
        "street": _nonempty(item.get("address")),
        "owner_type": owner.get("type") if isinstance(owner, dict) else None,
        "listing_updated_at": _nonempty(item.get("last_updated")),
        "image_urls": _search_images(item),
    }


def _safe_optional_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
