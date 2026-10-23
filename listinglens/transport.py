"""One bounded on-demand MyHome HTML request, with no caches or secrets."""
from urllib.parse import urlsplit, urljoin
import asyncio
import re
from .parser import SourceError


REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ka-GE,ka;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


_DETAIL_ID = re.compile(r"[1-9][0-9]{4,11}\Z")
_SLUG_PATH = re.compile(r"/(?:en/real-estate|ru/nedvizhimost|ka/udzravi-qoneba)/[a-z0-9-]+-([1-9][0-9]{4,11})/?\Z")
_NUMERIC_PATH = re.compile(r"/(?:en/pr|en/real-estate|ru/nedvizhimost)/([1-9][0-9]{4,11})/?\Z")


def _path_detail_id(path: str) -> str | None:
    for pattern in (_NUMERIC_PATH, _SLUG_PATH):
        match = pattern.fullmatch(path)
        if match is not None:
            return match.group(1)
    return None


def _valid_detail_redirect(original_url: str, candidate: str) -> bool:
    """Accept only same-origin HTTPS detail routes that retain the exact ID.

    This is not a general-purpose redirect follower: search requests, other
    domains, empty listing pages, URL query strings and ID swaps are rejected.
    """
    first = urlsplit(original_url)
    final = urlsplit(candidate)
    if first.scheme != 'https' or first.netloc != 'www.myhome.ge':
        return False
    if final.scheme != 'https' or final.netloc != 'www.myhome.ge' or final.query or final.fragment:
        return False
    requested = _path_detail_id(first.path)
    return requested is not None and _path_detail_id(final.path) == requested


async def fetch_html(url: str, timeout: int = 15):
    """curl_cffi chrome124 was independently shown to receive HTML 200 on user's Mac.

    Does not retry 401/403/429, or use cookies, CAPTCHA handling or third-party proxies.
    Return (text, effective_url, http_status). Never store complete response on disk.
    """
    url_parts = urlsplit(url)
    if url_parts.scheme != "https" or url_parts.netloc != "www.myhome.ge":
        raise SourceError("UPSTREAM_INVALID_DESTINATION", "Invalid source")
    try:
        from curl_cffi.requests import AsyncSession
    except ImportError:
        raise SourceError("MISSING_DEPENDENCY", "Install dependencies using RUN_MAC.sh", 503)
    try:
        # Up to three *validated* 3xx hops. This allows observed
        # /en/pr/<id>/ -> /en/real-estate/<id>/ -> /en/real-estate/<slug>-<id>/.
        # No retries for refused access, timeouts, or different listing IDs.
        current_url = url
        redirects = 0
        search_http500_retries = 0
        async with AsyncSession(impersonate="chrome124", trust_env=False) as session:
            while True:
                resp = await session.get(current_url, headers=REQUEST_HEADERS,
                                         timeout=timeout, allow_redirects=False)
                status = int(resp.status_code)
                if status in (401, 403, 429):
                    raise SourceError("UPSTREAM_ACCESS_DENIED", f"MyHome returned HTTP {status}; retries disabled", 503)
                if status in (301, 302, 303, 307, 308):
                    candidate = urljoin(current_url, resp.headers.get('Location', ''))
                    if not _valid_detail_redirect(url, candidate):
                        raise SourceError("UPSTREAM_REDIRECT", "MyHome redirected the request to a different URL or ID")
                    if redirects >= 3:
                        raise SourceError("UPSTREAM_REDIRECT_LIMIT", "Too many MyHome redirects")
                    redirects += 1
                    current_url = candidate
                    continue
                # Only a transient HTTP 500 on a read-only SEARCH URL gets one
                # bounded second attempt. Do not retry detail-ID requests, access
                # denials (401/403/429), redirects, other statuses or parse errors.
                # The user-selected URL and filters remain unchanged.
                if status == 500 and _path_detail_id(urlsplit(url).path) is None and search_http500_retries == 0:
                    search_http500_retries = 1
                    await asyncio.sleep(1.0)
                    continue
                if status != 200:
                    raise SourceError("UPSTREAM_HTTP_ERROR", f"MyHome returned HTTP {status}", 502)
                # Guard against non-http redirects or unexpected final URLs.
                if current_url != url and not _valid_detail_redirect(url, current_url):
                    raise SourceError("UPSTREAM_REDIRECT", "Unexpected final URL")
                break
    except SourceError:
        raise
    except Exception as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise SourceError("UPSTREAM_NETWORK_ERROR", "MyHome network error or timeout", 503) from None
    html = resp.text
    if not isinstance(html, str) or not html:
        raise SourceError("UPSTREAM_EMPTY_HTML", "MyHome returned an empty response")
    return html, current_url, status
