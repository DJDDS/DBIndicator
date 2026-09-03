import pandas as pd

from app import nse_earnings_history as eh


def test_v974_parse_board_meetings_keeps_financial_result_meeting_dates_only():
    payload = [
        {'symbol': 'AAA', 'purpose': 'Financial Results', 'meetingDate': '15-Oct-2020'},
        {'symbol': 'AAA', 'purpose': 'Issue of Bonus Shares', 'meetingDate': '20-Oct-2020'},
        {'symbol': 'AAA', 'purpose': 'Audited Financial Results', 'bm_date': '28-Jan-2021'},
    ]
    got = eh.parse_board_meeting_rows(payload, symbol='AAA')
    assert list(got) == [pd.Timestamp('2020-10-15'), pd.Timestamp('2021-01-28')]


def test_v974_earnings_client_prefers_historical_board_meeting_api(tmp_path):
    class Resp:
        def __init__(self, payload): self._payload = payload; self.status_code = 200
        def raise_for_status(self): return None
        def json(self): return self._payload
    class Session:
        def __init__(self): self.headers = {}; self.calls = []
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, dict(params or {})))
            if url.endswith('/'):
                return Resp({})
            if 'corporate-board-meetings' in url:
                return Resp([{'symbol':'AAA','purpose':'Financial Results','meetingDate':'15-Oct-2020'}])
            raise AssertionError(url)
    s = Session()
    c = eh.NSEEarningsHistoryClient(session=s, cache_dir=tmp_path)
    got = c.fetch_symbol('AAA', pd.Timestamp('2018-09-01'), pd.Timestamp('2021-08-31'))
    assert list(got) == [pd.Timestamp('2020-10-15')]
    api = [x for x in s.calls if 'corporate-board-meetings' in x[0]][0]
    assert api[1]['symbol'] == 'AAA'
    assert api[1]['from_date'] == '01-09-2018'
    assert api[1]['to_date'] == '31-08-2021'
