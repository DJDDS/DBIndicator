import pandas as pd
from app import nse_mwpl


def test_v971_parses_legacy_nseoi_xml_payload():
    xml = b'''<?xml version="1.0"?>
<OpenInterestReport>
  <Record>
    <Date>03-SEP-2018</Date>
    <ISIN>INE000A01001</ISIN>
    <Scrip_Name>AAA LTD</Scrip_Name>
    <NSE_Symbol>AAA</NSE_Symbol>
    <MWPL>1000000</MWPL>
    <NSE_Open_Interest>920000</NSE_Open_Interest>
    <Limit_for_Next_Day>80000</Limit_for_Next_Day>
  </Record>
</OpenInterestReport>'''
    rows = nse_mwpl.parse_combined_oi_payload(xml)
    assert rows['AAA']['mwpl'] == 1_000_000
    assert rows['AAA']['open_interest'] == 920_000
    assert rows['AAA']['mwpl_pct'] == 92.0


def test_v971_direct_legacy_probes_xml_and_remembers_working_route(tmp_path):
    xml = b'''<root><row><NSE_Symbol>AAA</NSE_Symbol><MWPL>1000000</MWPL><NSE_Open_Interest>900000</NSE_Open_Interest></row></root>'''

    class Resp:
        def __init__(self, content=b'', status=200, ctype='text/plain'):
            self.content=content; self.status_code=status; self.headers={'content-type':ctype}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f'HTTP {self.status_code}')
        def json(self): return {'message':'unavailable'}

    class Session:
        def __init__(self): self.calls=[]; self.headers={}
        def get(self, url, **kwargs):
            self.calls.append(url)
            if url == 'https://www.nseindia.com/': return Resp(b'warm')
            # Simulate the legacy XML living under archives/nsccl.
            if '/archives/nsccl/nseoi_03092018.xml' in url: return Resp(xml, 200, 'application/xml')
            if '/archives/nsccl/nseoi_04092018.xml' in url: return Resp(xml.replace(b'900000', b'910000'), 200, 'application/xml')
            return Resp(b'', 404)

    s=Session()
    client=nse_mwpl.NSEHistoricalReportClient(session=s, cache_dir=tmp_path)
    first=client.fetch_combined_oi(pd.Timestamp('2018-09-03').date())
    assert first['AAA']['mwpl_pct'] == 90.0
    before=len(s.calls)
    second=client.fetch_combined_oi(pd.Timestamp('2018-09-04').date())
    new_calls=s.calls[before:]
    assert second['AAA']['mwpl_pct'] == 91.0
    assert new_calls[0].endswith('/archives/nsccl/nseoi_04092018.xml')


def test_v971_secban_probes_legacy_content_and_archive_locations(tmp_path):
    csv=b'SYMBOL\nAAA\nBBB\n'

    class Resp:
        def __init__(self, content=b'', status=200):
            self.content=content; self.status_code=status; self.headers={'content-type':'text/csv'}
        def raise_for_status(self):
            if self.status_code>=400: raise RuntimeError('bad')
        def json(self): return {'message':'unavailable'}
    class Session:
        def __init__(self): self.calls=[]; self.headers={}
        def get(self,url,**kwargs):
            self.calls.append(url)
            if url == 'https://www.nseindia.com/': return Resp(b'warm')
            if '/content/nsccl/fo_secban_03092018.csv' in url: return Resp(csv)
            return Resp(b'',404)
    client=nse_mwpl.NSEHistoricalReportClient(session=Session(),cache_dir=tmp_path)
    out=client.fetch_secban(pd.Timestamp('2018-09-03').date())
    assert out == {'AAA','BBB'}
    assert any('/content/nsccl/fo_secban_03092018.csv' in u for u in client.session.calls)


def test_v971_client_sends_accept_language_to_nse_archives():
    class Session:
        def __init__(self): self.headers={}
    s=Session(); nse_mwpl.NSEHistoricalReportClient(session=s)
    assert 'Accept-Language' in s.headers


def test_v971_build_controls_marks_legacy_history_applied_with_full_coverage():
    dates=pd.bdate_range('2018-09-03',periods=4)
    class Client:
        def fetch_combined_oi(self, day):
            pct={pd.Timestamp(d).date():v for d,v in zip(dates,[70,96,94,79])}[pd.Timestamp(day).date()]
            return {'AAA':{'mwpl':1_000_000.0,'open_interest':pct*10_000.0,'mwpl_pct':float(pct)}}
        def fetch_secban(self, day): return set()
    out=nse_mwpl.build_validation_mwpl_controls(validation_dates=dates,symbols=['AAA'],client=Client(),min_date_coverage=0.95)
    assert out['available'] is True
    assert out['date_coverage'] == 1.0
    assert out['mwpl_by_symbol']['AAA'].tolist() == [70.0,96.0,94.0,79.0]
    # Ban activates one trading day after >95 and clears one day after <=80.
    assert out['ban_by_symbol']['AAA'].tolist() == [False,False,True,True]
