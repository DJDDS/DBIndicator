import datetime as dt

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _nfo_rows():
    rows=[]; tok=100
    expiry=dt.date(2026,9,8)
    for strike in range(24000,25300,50):
        for typ in ('CE','PE'):
            rows.append({'instrument_token':tok,'tradingsymbol':f'N{strike}{typ}','name':'NIFTY','instrument_type':typ,'segment':'NFO-OPT','expiry':expiry,'strike':strike,'lot_size':65}); tok+=1
    rows.append({'instrument_token':999,'tradingsymbol':'NIFTYSEP26FUT','name':'NIFTY','instrument_type':'FUT','segment':'NFO-FUT','expiry':dt.date(2026,9,29),'strike':0,'lot_size':65})
    return rows


def _nse_rows():
    return [
        {'instrument_token':11,'tradingsymbol':'NIFTY 50','name':'NIFTY 50','instrument_type':'INDICES','segment':'INDICES'},
        {'instrument_token':12,'tradingsymbol':'INDIA VIX','name':'INDIA VIX','instrument_type':'INDICES','segment':'INDICES'},
    ]


class FakeKite:
    def instruments(self, exchange):
        return _nfo_rows() if exchange=='NFO' else _nse_rows()
    def ltp(self, keys):
        return {'NSE:NIFTY 50':{'last_price':24612.0}}


class FakeTicker:
    MODE_FULL='full'
    def __init__(self):
        self.subscribed=[]; self.mode=None; self.connected=False; self.closed=False
        self.on_connect=None; self.on_ticks=None; self.on_close=None; self.on_error=None
    def subscribe(self,tokens): self.subscribed=list(tokens)
    def set_mode(self,mode,tokens): self.mode=(mode,list(tokens))
    def connect(self,threaded=False):
        self.connected=True
        self.on_connect(self,{})
        # spot/vix/future + one option tick are enough to exercise ingestion
        ticks=[]
        for tok in self.subscribed[:4]:
            ticks.append({'instrument_token':tok,'last_price':24612.0 if tok==11 else 13.0 if tok==12 else 24620.0 if tok==999 else 100.0,
                          'oi':10,'volume_traded':20,'depth':{'buy':[{'price':99,'quantity':10}],'sell':[{'price':101,'quantity':10}]}})
        self.on_ticks(self,ticks)
    def close(self): self.closed=True


def test_kite_auth_exposes_today_access_token(monkeypatch):
    from app import kite_auth
    monkeypatch.setattr(kite_auth,'_load_cache',lambda:'abc')
    assert kite_auth.get_access_token()=='abc'


def test_stream_service_waits_for_login(tmp_path):
    from app.v121_index_recorder import IndexVolStreamService
    svc=IndexVolStreamService(root=tmp_path/'data',state_file=tmp_path/'state.json',access_token_getter=lambda:None,kite_client_getter=lambda:None)
    out=svc.run_once(dt.datetime(2026,9,7,10,0,tzinfo=IST))
    assert out['status']=='WAITING_LOGIN'


def test_stream_service_connects_full_mode_and_ingests(tmp_path):
    from app.v121_index_recorder import IndexVolStreamService, _read_state
    ticker=FakeTicker()
    now=dt.datetime(2026,9,7,10,0,tzinfo=IST)
    svc=IndexVolStreamService(
        root=tmp_path/'data', state_file=tmp_path/'state.json',
        access_token_getter=lambda:'token', kite_client_getter=lambda:FakeKite(),
        ticker_factory=lambda api_key,token:ticker, api_key='key', now_provider=lambda:now,
        strike_steps=12,micro_seconds=5,depth_seconds=60,
    )
    out=svc.run_once(now)
    assert out['status'] in ('RECORDING','CONNECTED')
    assert ticker.connected and ticker.mode[0]=='full'
    assert len(ticker.subscribed)==53
    state=_read_state(tmp_path/'state.json')
    assert state['connection_count']==1
    assert state['last_tick_at'] is not None


def test_stream_service_market_closed_is_not_error(tmp_path):
    from app.v121_index_recorder import IndexVolStreamService
    svc=IndexVolStreamService(root=tmp_path/'data',state_file=tmp_path/'state.json',access_token_getter=lambda:'token',kite_client_getter=lambda:FakeKite())
    out=svc.run_once(dt.datetime(2026,9,6,10,0,tzinfo=IST))
    assert out['status']=='MARKET_CLOSED'


def test_background_starts_v121_stream_as_separate_daemon(monkeypatch):
    from app import background
    calls=[]
    class FakeService:
        def run_forever(self): calls.append('stream')
    monkeypatch.setattr(background,'_make_v121_stream_service',lambda:FakeService())
    monkeypatch.setattr(background.threading.Thread,'start',lambda self: calls.append('thread'))
    background._v121_stream_started=False
    background.start_v121_index_stream_once()
    background.start_v121_index_stream_once()
    assert calls.count('thread')==1


def test_background_postclose_backup_is_fail_soft(monkeypatch):
    from app import background
    calls=[]
    monkeypatch.setattr(background.v121_backup,'run_daily_backup_cycle',lambda *a,**k: calls.append(k['now']) or {'status':'OK'})
    background._run_v121_postclose_backup(dt.datetime(2026,9,7,16,5,tzinfo=IST))
    assert len(calls)==1
    monkeypatch.setattr(background.v121_backup,'run_daily_backup_cycle',lambda *a,**k: (_ for _ in ()).throw(RuntimeError('backup down')))
    # Must swallow auxiliary backup failure.
    background._run_v121_postclose_backup(dt.datetime(2026,9,7,16,10,tzinfo=IST))


def test_postclose_backup_call_is_not_conditioned_on_kite_login():
    import inspect
    from app import background
    src=inspect.getsource(background._run_loop)
    assert 'if kite is not None:\n                    _run_v121_postclose_backup(now_ist())' not in src
    assert '_run_v121_postclose_backup(now_ist())' in src
