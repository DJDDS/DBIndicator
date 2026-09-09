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


class LifecycleTicker(FakeTicker):
    """Threaded fake that can finish one run without creating a second ticker."""
    def __init__(self, finish='manual_close'):
        super().__init__()
        self.connect_args=[]
        self.on_reconnect=None
        self.on_noreconnect=None
        self.finish=finish
    def connect(self, threaded=False):
        self.connect_args.append(threaded)
        self.connected=True
        self.on_connect(self,{})
        ticks=[]
        for tok in self.subscribed[:4]:
            ticks.append({'instrument_token':tok,'last_price':24612.0 if tok==11 else 13.0 if tok==12 else 24620.0 if tok==999 else 100.0,
                          'oi':10,'volume_traded':20,'depth':{'buy':[{'price':99,'quantity':10}],'sell':[{'price':101,'quantity':10}]}})
        self.on_ticks(self,ticks)
        if self.finish == 'manual_close':
            self.on_close(self,1000,'test close')
        elif self.finish == 'noreconnect':
            self.on_reconnect(self,1)
            self.on_noreconnect(self)
    def close(self):
        self.closed=True
        self.connected=False
        if self.on_close:
            self.on_close(self,1000,'manual close')


def test_stream_service_uses_threaded_kiteticker_and_finishes_single_lifecycle(tmp_path):
    from app.v121_index_recorder import IndexVolStreamService, _read_state
    ticker=LifecycleTicker(finish='noreconnect')
    now=dt.datetime(2026,9,7,10,0,tzinfo=IST)
    factory_calls=[]
    def factory(api_key, token):
        factory_calls.append((api_key, token))
        return ticker
    svc=IndexVolStreamService(
        root=tmp_path/'data', state_file=tmp_path/'state.json',
        access_token_getter=lambda:'token', kite_client_getter=lambda:FakeKite(),
        ticker_factory=factory, api_key='key', now_provider=lambda:now,
        strike_steps=12,micro_seconds=5,depth_seconds=60,
    )
    out=svc.run_once(now)
    assert ticker.connect_args == [True]
    assert len(factory_calls) == 1
    state=_read_state(tmp_path/'state.json')
    assert state['connection_count'] == 1
    assert state['last_tick_at'] is not None
    assert state.get('reconnect_attempt') == 1
    assert out['status'] in ('DISCONNECTED','ERROR')


def test_stream_service_transient_close_does_not_spawn_second_ticker(tmp_path):
    """KiteTicker owns auto-reconnect; our on_close must not call connect again."""
    from app.v121_index_recorder import IndexVolStreamService
    ticker=LifecycleTicker(finish='noreconnect')
    now=dt.datetime(2026,9,7,10,0,tzinfo=IST)
    calls=[]
    svc=IndexVolStreamService(
        root=tmp_path/'data', state_file=tmp_path/'state.json',
        access_token_getter=lambda:'token', kite_client_getter=lambda:FakeKite(),
        ticker_factory=lambda a,t: calls.append(1) or ticker,
        api_key='key', now_provider=lambda:now,
    )
    svc.run_once(now)
    assert calls == [1]
    assert ticker.connect_args == [True]


def test_stream_service_reuses_active_threaded_ticker_without_duplicate_factory(tmp_path):
    """run_forever polls run_once; a live threaded ticker must not be recreated."""
    from app.v121_index_recorder import IndexVolStreamService
    ticker=LifecycleTicker(finish='stay_open')
    now=dt.datetime(2026,9,7,10,0,tzinfo=IST)
    factory_calls=[]
    svc=IndexVolStreamService(
        root=tmp_path/'data', state_file=tmp_path/'state.json',
        access_token_getter=lambda:'token', kite_client_getter=lambda:FakeKite(),
        ticker_factory=lambda a,t: factory_calls.append((a,t)) or ticker,
        api_key='key', now_provider=lambda:now,
    )
    first=svc.run_once(now)
    second=svc.run_once(now)
    assert first['status'] in ('CONNECTED','RECORDING')
    assert second['status'] in ('CONNECTED','RECORDING')
    assert len(factory_calls) == 1
    assert ticker.connect_args == [True]


class FakeReactor:
    def __init__(self, running):
        self.running = running
        self.calls = []
    def callFromThread(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return fn(*args, **kwargs)


class SilentTicker(LifecycleTicker):
    def connect(self, threaded=False):
        self.connect_args.append(threaded)
        self.connected = True
        self.on_connect(self, {})
        # Intentionally no ticks: exercises stale-session watchdog.


def _multi_day_nfo_rows():
    rows=[]; tok=100
    for expiry in (dt.date(2026,9,8), dt.date(2026,9,15)):
        for strike in range(24000,25300,50):
            for typ in ('CE','PE'):
                rows.append({'instrument_token':tok,'tradingsymbol':f'N{expiry:%d}{strike}{typ}','name':'NIFTY','instrument_type':typ,'segment':'NFO-OPT','expiry':expiry,'strike':strike,'lot_size':65}); tok+=1
    rows.append({'instrument_token':999,'tradingsymbol':'NIFTYSEP26FUT','name':'NIFTY','instrument_type':'FUT','segment':'NFO-FUT','expiry':dt.date(2026,9,29),'strike':0,'lot_size':65})
    return rows


class MultiDayFakeKite(FakeKite):
    def instruments(self, exchange):
        return _multi_day_nfo_rows() if exchange=='NFO' else _nse_rows()


def test_second_lifecycle_is_dispatched_into_running_twisted_reactor(tmp_path):
    """A new session must not invoke Twisted connection APIs from the service thread."""
    from app.v121_index_recorder import IndexVolStreamService
    ticker = LifecycleTicker(finish='stay_open')
    reactor = FakeReactor(running=True)
    now = dt.datetime(2026,9,9,9,16,tzinfo=IST)
    svc = IndexVolStreamService(
        root=tmp_path/'data', state_file=tmp_path/'state.json',
        access_token_getter=lambda:'token', kite_client_getter=lambda:MultiDayFakeKite(),
        ticker_factory=lambda a,t:ticker, api_key='key', now_provider=lambda:now,
        reactor_getter=lambda:reactor,
    )
    svc.run_once(now)
    assert len(reactor.calls) == 1
    assert ticker.connect_args == [True]


def test_new_trading_day_retires_previous_session_and_rebuilds_expiry(tmp_path):
    """An apparently active prior-day ticker must never block today's expiry rollover."""
    from app.v121_index_recorder import IndexVolStreamService, _read_state
    day1 = dt.datetime(2026,9,8,10,0,tzinfo=IST)
    day2 = dt.datetime(2026,9,9,9,16,tzinfo=IST)
    clock = {'now': day1}
    tickers = [LifecycleTicker(finish='stay_open'), LifecycleTicker(finish='stay_open')]
    calls=[]
    def factory(a,t):
        ticker = tickers[len(calls)]
        calls.append(ticker)
        return ticker
    svc = IndexVolStreamService(
        root=tmp_path/'data', state_file=tmp_path/'state.json',
        access_token_getter=lambda:'token', kite_client_getter=lambda:MultiDayFakeKite(),
        ticker_factory=factory, api_key='key', now_provider=lambda:clock['now'],
        reactor_getter=lambda:FakeReactor(running=False),
    )
    svc.run_once(day1)
    assert _read_state(tmp_path/'state.json')['active_expiry'] == '2026-09-08'
    clock['now'] = day2
    svc.run_once(day2)
    state = _read_state(tmp_path/'state.json')
    assert len(calls) == 2
    assert tickers[0].closed is True
    assert state['active_expiry'] == '2026-09-15'


def test_live_session_watchdog_retires_ticker_when_no_ticks_arrive(tmp_path):
    from app.v121_index_recorder import IndexVolStreamService, _read_state
    t0 = dt.datetime(2026,9,9,10,0,tzinfo=IST)
    clock = {'now': t0}
    ticker = SilentTicker(finish='stay_open')
    svc = IndexVolStreamService(
        root=tmp_path/'data', state_file=tmp_path/'state.json',
        access_token_getter=lambda:'token', kite_client_getter=lambda:MultiDayFakeKite(),
        ticker_factory=lambda a,t:ticker, api_key='key', now_provider=lambda:clock['now'],
        reactor_getter=lambda:FakeReactor(running=False), watchdog_stale_seconds=30,
    )
    svc.run_once(t0)
    clock['now'] = t0 + dt.timedelta(seconds=31)
    out = svc.run_once(clock['now'])
    state = _read_state(tmp_path/'state.json')
    assert ticker.closed is True
    assert out['status'] == 'STALE'
    assert state['status'] == 'STALE'
    assert state['connected'] is False


def test_market_close_retires_live_ticker_instead_of_leaving_it_overnight(tmp_path):
    from app.v121_index_recorder import IndexVolStreamService
    t0 = dt.datetime(2026,9,9,15,30,tzinfo=IST)
    clock = {'now': t0}
    ticker = LifecycleTicker(finish='stay_open')
    svc = IndexVolStreamService(
        root=tmp_path/'data', state_file=tmp_path/'state.json',
        access_token_getter=lambda:'token', kite_client_getter=lambda:MultiDayFakeKite(),
        ticker_factory=lambda a,t:ticker, api_key='key', now_provider=lambda:clock['now'],
        reactor_getter=lambda:FakeReactor(running=False),
    )
    svc.run_once(t0)
    clock['now'] = dt.datetime(2026,9,9,15,41,tzinfo=IST)
    out = svc.run_once(clock['now'])
    assert ticker.closed is True
    assert out['status'] == 'MARKET_CLOSED'
    assert out['connected'] is False
