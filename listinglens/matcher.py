"""Conservative visual evidence. Historical findorigupd11.py histogram concept,
reimplemented without its Selenium, path dependencies or contacts.
"""
from __future__ import annotations
from dataclasses import dataclass
from io import BytesIO
from urllib.parse import urlsplit
import numpy as np
from PIL import Image, ImageOps, ImageFile
import httpx
Image.MAX_IMAGE_PIXELS = 16_000_000
ImageFile.LOAD_TRUNCATED_IMAGES=False
ALLOWED={'static-api-statements.tnet.ge','static.my.ge','www.myhome.ge'}
MAX_BYTES=4_000_000

@dataclass(frozen=True)
class PhotoPrint:
    histogram: np.ndarray
    hashes: tuple[int,...]
    sift_jpeg: bytes | None = None  # grayscale preview for local SIFT, no second download


def fingerprint(content:bytes)->PhotoPrint:
    if len(content)>MAX_BYTES or not content:raise ValueError('image size limit')
    with Image.open(BytesIO(content)) as opened:
        if opened.width * opened.height>16_000_000:raise ValueError('image pixel limit')
        src=ImageOps.exif_transpose(opened).convert('L')
        src.thumbnail((640,640))
        a=np.asarray(src,dtype=np.uint8)
        # SIFT needs richer detail than the 640px histogram thumbnail.
        sift_src=ImageOps.exif_transpose(opened).convert('L')
        sift_src.thumbnail((940,940))
        sift_buf=BytesIO()
        sift_src.save(sift_buf,format='JPEG',quality=92)
    hist=np.bincount(a.flatten(), minlength=256).astype('float64')
    hist/=max(float(hist.sum()),1)
    # dHash is robust to resizing and moderate brightness shifts; add centre crop
    imgs=[a]
    h,w=a.shape
    if h>=32 and w>=32:imgs.append(a[h//8:h-h//8,w//8:w-w//8])
    hashes=[]
    for img in imgs:
        im=Image.fromarray(img).resize((9,8),Image.Resampling.LANCZOS)
        data=np.asarray(im,dtype=np.int16)
        bits=data[:,:-1]>data[:,1:]
        val=0
        for bit in bits.flat:val=(val<<1)|int(bit)
        hashes.append(val)
    return PhotoPrint(hist,tuple(hashes),sift_buf.getvalue())


def scores(a:PhotoPrint,b:PhotoPrint)->dict:
    h1=a.histogram;h2=b.histogram
    c1=h1-h1.mean();c2=h2-h2.mean()
    d=float(np.linalg.norm(c1)*np.linalg.norm(c2))
    corr=float(np.dot(c1,c2)/d) if d>1e-14 else 0.
    dist=min((x^y).bit_count() for x in a.hashes for y in b.hashes)
    return {'hist_correlation':round(corr,4),'hash_distance':dist,'photo_strong':dist<=7 and corr>=0.80,
            'photo_possible':dist<=12 and corr>=0.70}


def assess(agent:dict,owner:dict,comparisons:list[dict])->dict:
    """One strong photo match is enough under the selected search geometry.

    This helper uses the same >0.95 rule as the production workflow.
    Similarity is an image heuristic, never a calibrated identity probability.
    """
    a_city=agent.get('city_id') or agent.get('city')
    b_city=owner.get('city_id') or owner.get('city')
    a_district=agent.get('district_id') or agent.get('district')
    b_district=owner.get('district_id') or owner.get('district')
    location_consistent=bool(a_city and b_city and a_city==b_city)
    if a_district is not None and b_district is not None:
        location_consistent=location_consistent and a_district==b_district
    for key in ('rooms','bedrooms'):
        if agent.get(key) is not None and owner.get(key) is not None and agent[key]!=owner[key]:
            location_consistent=False
    candidates=[(photo_similarity(item),item) for item in comparisons]
    candidates.sort(key=lambda pair:pair[0],reverse=True)
    matched=next(((similarity,item) for similarity,item in candidates if similarity>0.95),None)
    ok=location_consistent and matched is not None
    area_a=agent.get('area_sqm') or agent.get('area_m2')
    area_b=owner.get('area_sqm') or owner.get('area_m2')
    area_ok=bool(area_a and area_b and area_a-10<=area_b<=area_a+5)
    if area_a is not None and area_b is not None and not area_ok:ok=False
    return {'status':'MATCH_CONFIRMED' if ok else 'NOT_FOUND',
            'strong_photo_pairs':1 if ok else 0,
            'histogram_similarity':max((x.get('hist_correlation',0) for x in comparisons),default=None),
            'photo_similarity':matched[0] if matched and ok else None,
            'area_consistent':area_ok,'location_consistent':location_consistent,
            'evidence':[matched[1]] if matched and ok else [],
            'reason':'Photo match > 0.95' if ok else 'No suitable photo match'}


async def download_photo(client:httpx.AsyncClient,url:str)->bytes:
    u=urlsplit(url)
    if u.scheme!='https' or u.hostname not in ALLOWED or u.username or u.password or u.port not in (None,443):raise ValueError('unapproved photo host')
    async with client.stream('GET',url,follow_redirects=False,timeout=12) as r:
        if r.status_code in (401,403,429):raise ValueError('image source access denied')
        r.raise_for_status()
        if 'image/' not in r.headers.get('content-type',''):raise ValueError('unexpected photo MIME')
        if int(r.headers.get('content-length','0'))>MAX_BYTES:raise ValueError('photo too large')
        chunks=[];size=0
        async for chunk in r.aiter_bytes():
            size+=len(chunk)
            if size>MAX_BYTES:raise ValueError('photo download limit')
            chunks.append(chunk)
        return b''.join(chunks)


def photo_similarity(metrics:dict)->float:
    """0..1 photo similarity: histogram correlation AND perceptual-hash agreement.

    A high grayscale histogram alone is insufficient: use the product with
    normalized dHash similarity to guard against two unrelated interiors
    having similar brightness. Rounding is to 4 decimals; accept STRICT > .95.
    This is a heuristic visual score, not a calibrated probability.
    """
    corr=max(0.0,min(1.0,float(metrics['hist_correlation'])))
    hamming=max(0,min(64,int(metrics['hash_distance'])))
    return round(corr*(1.0-hamming/64.0),4)
