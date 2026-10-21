"""Offline MyHome location hierarchy. ID and parent relations are preserved exactly."""
from functools import lru_cache
from pathlib import Path
import json

@lru_cache(maxsize=1)
def catalog():
    return json.loads((Path(__file__).parent/'location_catalog.json').read_text(encoding='utf-8'))['cities']

def validate_location(filters):
    cityid=str(filters.get('cities','')).strip()
    city=next((c for c in catalog() if str(c['id'])==cityid),None)
    if city is None: raise ValueError('Select a valid city from the list.')
    district_id=str(filters.get('districts','')).strip()
    urban_id=str(filters.get('urbans','')).strip()
    district=None
    if district_id:
        district=next((d for d in city['districts'] if str(d['id'])==district_id),None)
        if district is None: raise ValueError('The district does not belong to the selected city.')
    if urban_id:
        if district is None: raise ValueError('Choose a district before choosing a microdistrict.')
        if not any(str(u['id'])==urban_id for u in district['urbans']):
            raise ValueError('The microdistrict does not belong to the selected district.')
    return True
