#!/usr/bin/env python3
import requests, zipfile, io, pandas as pd
tests=[
("2011","JAN","03JAN2011"),
("2015","JAN","01JAN2015"),
("2020","JAN","01JAN2020"),
("2023","JAN","02JAN2023"),
("2024","JUL","01JUL2024"),
]
for y,m,d in tests:
    url=f"https://archives.nseindia.com/content/historical/DERIVATIVES/{y}/{m}/fo{d}bhav.csv.zip"
    try:
        r=requests.get(url,timeout=30,headers={"User-Agent":"Mozilla/5.0"})
        print(url,r.status_code,len(r.content),r.headers.get("content-type"))
        if r.status_code==200 and r.content[:2]==b'PK':
            z=zipfile.ZipFile(io.BytesIO(r.content))
            print(" files",z.namelist()[:3])
            raw=z.read(z.namelist()[0])
            df=pd.read_csv(io.BytesIO(raw),nrows=5)
            print(df.columns.tolist())
            print(df.head().to_string(index=False))
    except Exception as e:
        print("ERR",url,repr(e))
