#!/usr/bin/env python3
import requests, zipfile, io, pandas as pd
tests=[("2011","JAN","03JAN2011"),("2015","DEC","24DEC2015"),("2016","JAN","04JAN2016"),("2020","JAN","01JAN2020")]
hosts=[
 "https://archives.nseindia.com/content/historical/DERIVATIVES/{y}/{m}/fo{d}bhav.csv.zip",
 "https://www1.nseindia.com/content/historical/DERIVATIVES/{y}/{m}/fo{d}bhav.csv.zip",
 "http://www1.nseindia.com/content/historical/DERIVATIVES/{y}/{m}/fo{d}bhav.csv.zip",
]
s=requests.Session()
headers={
 "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/154 Safari/537.36",
 "Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
 "Accept-Language":"en-US,en;q=0.9",
 "Referer":"https://www1.nseindia.com/products/content/derivatives/equities/historical_fo.htm",
}
for warm in ["https://www.nseindia.com","https://www1.nseindia.com/products/content/derivatives/equities/historical_fo.htm"]:
    try:
        rw=s.get(warm,headers=headers,timeout=20)
        print("warm",warm,rw.status_code,len(rw.content),dict(s.cookies))
    except Exception as e: print("warmerr",repr(e))
for y,m,d in tests:
    for pat in hosts:
        url=pat.format(y=y,m=m,d=d)
        try:
            r=s.get(url,timeout=30,headers=headers,allow_redirects=True)
            print(url,r.status_code,len(r.content),r.headers.get("content-type"),r.url)
            if r.status_code==200 and r.content[:2]==b'PK':
                z=zipfile.ZipFile(io.BytesIO(r.content)); raw=z.read(z.namelist()[0]); df=pd.read_csv(io.BytesIO(raw),nrows=2)
                print("OK",z.namelist()[0],df.columns.tolist())
                break
        except Exception as e: print("ERR",url,repr(e))
