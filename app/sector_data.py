"""Dated official Nifty sector membership and atomic cache I/O."""
from __future__ import annotations
import csv
import io
import json
import os
import re
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests

BASE = 'https://www.niftyindices.com'
CATALOGUE_URL = BASE + '/indices/equity/sectoral-indices'
HOSTS = {'niftyindices.com', 'www.niftyindices.com'}


def official_url(url):
    parsed = urlparse(url)
    return parsed.scheme == 'https' and parsed.hostname in HOSTS and not parsed.username


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.href=None; self.label=[]
    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.href=dict(attrs).get('href'); self.label=[]
    def handle_data(self, data):
        if self.href:
            self.label.append(data)
    def handle_endtag(self, tag):
        if tag == 'a' and self.href:
            self.links.append((urljoin(BASE,self.href), ' '.join(''.join(self.label).split())))
            self.href=None


def parse_catalogue(html):
    parser=Links(); parser.feed(html); found={}
    for url,name in parser.links:
        path=urlparse(url).path.rstrip('/')
        if official_url(url) and re.fullmatch(r'/indices/equity/sectoral-indices/[^/]+',path):
            key=path.rsplit('/',1)[-1]
            if key not in found:
                found[key]={'id':key,'name':name or key.replace('-',' ').title(),'source':url}
    return sorted(found.values(),key=lambda r:r['name'].upper())


def constituent_url(html):
    parser=Links(); parser.feed(html)
    for url,_ in parser.links:
        if official_url(url) and urlparse(url).path.lower().endswith('.csv') and 'indexconstituent' in url.lower():
            return url
    return None


def parse_members(text):
    reader=csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    fields={f.strip().lower():f for f in reader.fieldnames or []}
    if 'symbol' not in fields:
        raise ValueError('Official constituent CSV has no Symbol column')
    members={}
    for row in reader:
        symbol=(row.get(fields['symbol']) or '').strip().upper()
        if not re.fullmatch(r'[A-Z0-9&_.-]{1,35}',symbol):
            continue
        weight=None
        for column in ('weight(%)','weight (%)','weight','weightage'):
            if column in fields:
                try: weight=float(row[fields[column]].rstrip('%'))
                except (ValueError, TypeError, AttributeError): pass
        members[symbol]={'symbol':symbol,'name':row.get(fields.get('company name','')) or symbol,
                         'industry':row.get(fields.get('industry','')) or None,'weight':weight}
    if not members:
        raise ValueError('Official constituent CSV empty')
    return list(members.values())


def load_json(path, fallback):
    try:
        with open(path,encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def save_json(path, data):
    # Serialize before opening a temporary file: bad values never replace good cache.
    encoded=json.dumps(data,allow_nan=False,separators=(',',':'),default=str)
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            f.write(encoded)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def get_text(url):
    if not official_url(url):
        raise ValueError('Only official Nifty Indices URLs allowed')
    response=requests.get(url,timeout=(10,25),headers={'User-Agent':'Mozilla/5.0','Accept':'text/html,text/csv,*/*'})
    response.raise_for_status()
    if not official_url(response.url):
        raise ValueError('Unexpected constituent source redirect')
    return response.text


def refresh_catalogue(previous, now, progress=None):
    catalogue=parse_catalogue(get_text(CATALOGUE_URL))
    if not catalogue:
        raise ValueError('Official sector catalogue empty')
    old={r['id']:r for r in previous}
    result=[]
    for i,row in enumerate(catalogue):
        try:
            url=constituent_url(get_text(row['source']))
            if not url: raise ValueError('Official constituent link unavailable')
            row.update(members=parse_members(get_text(url)),member_source=url,retrieved_at=now.isoformat(timespec='seconds'),membership_error=None)
        except Exception as exc:
            saved=old.get(row['id'],{})
            row.update(members=saved.get('members',[]),member_source=saved.get('member_source'),retrieved_at=saved.get('retrieved_at'),membership_error=str(exc)[:180])
        result.append(row)
        if progress: progress(i+1,len(catalogue))
    return result
