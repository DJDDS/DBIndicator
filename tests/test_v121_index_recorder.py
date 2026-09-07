import datetime as dt
from pathlib import Path

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _nifty_rows():
    rows = []
    token = 1000
    for expiry in (dt.date(2026, 9, 8), dt.date(2026, 9, 15)):
        for strike in range(24000, 25300, 50):
            for typ in ('CE','PE'):
                rows.append({
                    'instrument_token': token, 'tradingsymbol': f'NIFTY{expiry:%d%b}{strike}{typ}'.upper(),
                    'name':'NIFTY','instrument_type':typ,'segment':'NFO-OPT','expiry':expiry,'strike':strike,'lot_size':65,
                })
                token += 1
    for expiry in (dt.date(2026, 9, 29), dt.date(2026, 10, 27)):
        rows.append({'instrument_token':token,'tradingsymbol':f'NIFTY{expiry:%b}FUT'.upper(),'name':'NIFTY','instrument_type':'FUT','segment':'NFO-FUT','expiry':expiry,'strike':0,'lot_size':65})
        token += 1
    return rows


def test_select_index_universe_uses_nearest_expiry_atm_plus_minus_12_and_refs():
    from app.v121_index_recorder import select_index_universe
    nse = [
        {'instrument_token': 11, 'tradingsymbol':'NIFTY 50','name':'NIFTY 50','instrument_type':'INDICES','segment':'INDICES'},
        {'instrument_token': 12, 'tradingsymbol':'INDIA VIX','name':'INDIA VIX','instrument_type':'INDICES','segment':'INDICES'},
    ]
    out = select_index_universe(_nifty_rows(), nse, 24612.0, dt.date(2026,9,7), strike_steps=12)
    assert out['expiry'] == '2026-09-08'
    assert out['atm_strike'] == 24600.0
    option_meta = [m for m in out['metadata'].values() if m['kind']=='OPTION']
    assert len(option_meta) == 50  # 25 strikes * CE/PE
    assert min(m['strike'] for m in option_meta) == 24000.0
    assert max(m['strike'] for m in option_meta) == 25200.0
    assert out['refs']['spot_token'] == 11
    assert out['refs']['vix_token'] == 12
    assert out['refs']['future_token'] is not None
    assert set(out['tokens']) == set(out['metadata'])


def test_select_index_universe_fails_closed_without_required_refs():
    from app.v121_index_recorder import select_index_universe
    out = select_index_universe(_nifty_rows(), [], 24612.0, dt.date(2026,9,7), strike_steps=12)
    assert out['status'] == 'WAITING_INSTRUMENTS'
    assert out['tokens'] == []


def test_normalize_micro_tick_keeps_executable_top_of_book():
    from app.v121_index_recorder import normalize_micro_tick
    meta = {'kind':'OPTION','tradingsymbol':'NIFTYOPT','instrument_token':1,'strike':24600.0,'type':'CE','expiry':'2026-09-08','lot_size':65}
    tick = {'instrument_token':1,'last_price':123.4,'volume_traded':2000,'oi':5000,'depth':{'buy':[{'price':123.0,'quantity':100,'orders':4}], 'sell':[{'price':123.8,'quantity':80,'orders':3}]}}
    refs = {'spot':24612.5,'vix':13.2,'future':24620.0}
    out = normalize_micro_tick(meta,tick,refs,dt.datetime(2026,9,7,10,30,tzinfo=IST))
    assert out['best_bid'] == 123.0 and out['best_ask'] == 123.8
    assert out['bid_qty'] == 100 and out['ask_qty'] == 80
    assert out['spread'] == 0.8
    assert out['spot'] == 24612.5 and out['india_vix'] == 13.2 and out['nifty_future'] == 24620.0
    assert out['oi'] == 5000 and out['volume'] == 2000


def test_normalize_depth_tick_preserves_five_levels():
    from app.v121_index_recorder import normalize_depth_tick
    meta = {'kind':'OPTION','tradingsymbol':'NIFTYOPT','instrument_token':1,'strike':24600.0,'type':'PE','expiry':'2026-09-08','lot_size':65}
    levels = [{'price':100+i,'quantity':10+i,'orders':i+1} for i in range(6)]
    tick = {'instrument_token':1,'last_price':101,'depth':{'buy':levels,'sell':levels}}
    out = normalize_depth_tick(meta,tick,{'spot':24600,'vix':14,'future':24610},dt.datetime(2026,9,7,10,30,tzinfo=IST))
    assert len(out['depth']['buy']) == 5 and len(out['depth']['sell']) == 5
    assert out['depth']['buy'][0]['price'] == 100.0


def test_writer_uses_five_second_micro_and_sixty_second_depth_cadence(tmp_path):
    from app.v121_index_recorder import IndexVolWriter
    root = tmp_path/'index_vol'; state = tmp_path/'state.json'
    writer = IndexVolWriter(root, state, micro_seconds=5, depth_seconds=60)
    meta={1:{'kind':'OPTION','instrument_token':1,'tradingsymbol':'OPT','expiry':'2026-09-08','strike':24600,'type':'CE','lot_size':65}}
    tick={1:{'instrument_token':1,'last_price':100,'oi':10,'volume_traded':20,'depth':{'buy':[{'price':99,'quantity':10,'orders':1}],'sell':[{'price':101,'quantity':12,'orders':1}]}}}
    refs={'spot':24600,'vix':13,'future':24605}
    t0=dt.datetime(2026,9,7,10,0,0,tzinfo=IST)
    a=writer.ingest(tick,meta,refs,t0)
    b=writer.ingest(tick,meta,refs,t0+dt.timedelta(seconds=3))
    c=writer.ingest(tick,meta,refs,t0+dt.timedelta(seconds=5))
    d=writer.ingest(tick,meta,refs,t0+dt.timedelta(seconds=60))
    assert a['micro_written'] and a['depth_written']
    assert not b['micro_written'] and not b['depth_written']
    assert c['micro_written'] and not c['depth_written']
    assert d['micro_written'] and d['depth_written']
    assert (root/'2026-09-07_micro.jsonl').exists()
    assert (root/'2026-09-07_depth.jsonl').exists()


def test_index_recorder_health_reports_files_counts_and_market_closed(tmp_path):
    from app.v121_index_recorder import IndexVolWriter, index_recorder_health
    root=tmp_path/'index_vol'; state=tmp_path/'state.json'
    writer=IndexVolWriter(root,state,micro_seconds=5,depth_seconds=60)
    meta={1:{'kind':'OPTION','instrument_token':1,'tradingsymbol':'OPT','expiry':'2026-09-08','strike':24600,'type':'CE','lot_size':65}}
    tick={1:{'instrument_token':1,'last_price':100,'depth':{'buy':[{'price':99,'quantity':10}],'sell':[{'price':101,'quantity':10}]}}}
    now=dt.datetime(2026,9,7,10,0,tzinfo=IST)
    writer.set_universe({'expiry':'2026-09-08','atm_strike':24600,'tokens':[1,2,3]})
    writer.ingest(tick,meta,{'spot':24600,'vix':13,'future':24605},now)
    health=index_recorder_health(root,state,now=now,storage_mode='PERSISTENT_VOLUME')
    assert health['recorder_status']=='RECORDING'
    assert health['active_expiry']=='2026-09-08'
    assert health['atm_strike']==24600.0
    assert health['token_count']==3
    assert health['micro_contract_rows']==1 and health['depth_contract_rows']==1
    assert health['micro_file_bytes']>0 and health['depth_file_bytes']>0
    weekend=index_recorder_health(root,state,now=dt.datetime(2026,9,6,10,0,tzinfo=IST),storage_mode='PERSISTENT_VOLUME')
    assert weekend['recorder_status']=='MARKET_CLOSED'


def test_writer_resets_current_day_counts_on_new_trading_day(tmp_path):
    from app.v121_index_recorder import IndexVolWriter, index_recorder_health
    root=tmp_path/'index_vol'; state=tmp_path/'state.json'; w=IndexVolWriter(root,state)
    meta={1:{'kind':'OPTION','instrument_token':1,'tradingsymbol':'OPT','expiry':'2026-09-08','strike':24600,'type':'CE','lot_size':65}}
    tick={1:{'instrument_token':1,'last_price':100,'depth':{'buy':[{'price':99,'quantity':1}],'sell':[{'price':101,'quantity':1}]}}}
    w.ingest(tick,meta,{'spot':24600,'vix':13,'future':24605},dt.datetime(2026,9,7,10,0,tzinfo=IST))
    w.ingest(tick,meta,{'spot':24600,'vix':13,'future':24605},dt.datetime(2026,9,8,10,0,tzinfo=IST))
    h=index_recorder_health(root,state,now=dt.datetime(2026,9,8,10,1,tzinfo=IST),storage_mode='PERSISTENT_VOLUME')
    assert h['micro_contract_rows']==1 and h['depth_contract_rows']==1


def test_micro_tick_preserves_exchange_and_last_trade_timestamps_for_staleness():
    from app.v121_index_recorder import normalize_micro_tick
    ts=dt.datetime(2026,9,7,10,30,5,tzinfo=IST)
    ex=dt.datetime(2026,9,7,10,30,4,tzinfo=IST)
    lt=dt.datetime(2026,9,7,10,29,55,tzinfo=IST)
    meta={'kind':'OPTION','tradingsymbol':'OPT','instrument_token':1,'strike':24600,'type':'CE','expiry':'2026-09-08','lot_size':65}
    tick={'instrument_token':1,'last_price':100,'exchange_timestamp':ex,'last_trade_time':lt,'depth':{'buy':[{'price':99,'quantity':1}],'sell':[{'price':101,'quantity':1}]}}
    out=normalize_micro_tick(meta,tick,{'spot':24600,'vix':13,'future':24605},ts)
    assert out['exchange_timestamp'].startswith('2026-09-07T10:30:04')
    assert out['last_trade_time'].startswith('2026-09-07T10:29:55')
    assert out['quote_age_seconds']==1.0


def test_quote_age_handles_production_naive_ist_vs_naive_utc_case():
    """Recorder wall clock is naive IST; Kite WebSocket exchange time is naive UTC."""
    from app.v121_index_recorder import _age_seconds
    observed = dt.datetime(2026, 9, 7, 10, 25, 52)  # now_ist(): naive IST
    quoted = dt.datetime(2026, 9, 7, 4, 55, 51)     # Kite tick: naive UTC
    assert _age_seconds(observed, quoted) == 1.0


def test_quote_age_honours_aware_offsets_and_real_staleness():
    from app.v121_index_recorder import _age_seconds
    observed = dt.datetime(2026, 9, 7, 10, 30, 0, tzinfo=IST)
    quoted = dt.datetime(2026, 9, 7, 4, 50, 0, tzinfo=dt.timezone.utc)
    assert _age_seconds(observed, quoted) == 600.0
    assert _age_seconds(None, quoted) is None
    assert _age_seconds(observed, 'not-a-date') is None
