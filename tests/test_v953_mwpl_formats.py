from app import nse_mwpl


def test_v953_parses_legacy_ncl_oi_with_nse_open_interest_column():
    csv = '''Date,ISIN,Scrip Name,NSE Symbol,MWPL,NSE Open Interest,Limit_for_Next_Day\n02-SEP-2024,INE001A01036,ABC LTD,ABC,1000000,920000,80000\n'''
    rows = nse_mwpl.parse_combined_oi_csv(csv)
    assert rows['ABC']['mwpl'] == 1000000
    assert rows['ABC']['open_interest'] == 920000
    assert rows['ABC']['mwpl_pct'] == 92.0


def test_v953_parses_current_combined_oi_with_future_equivalent_extra_column():
    csv = '''Date,ISIN,Scrip Name,NSE Symbol,MWPL,Open Interest,Future Equivalent Open Interest,Limit for Next Day\n02-SEP-2026,INE001A01036,ABC LTD,ABC,1000000,960000,950000,50000\n'''
    rows = nse_mwpl.parse_combined_oi_csv(csv)
    assert rows['ABC']['mwpl_pct'] == 96.0

def test_v953_parses_zipped_combined_oi_payload():
    import io, zipfile
    csv = b'Date,ISIN,Scrip Name,NSE Symbol,MWPL,Open Interest\n02-SEP-2026,INE001A01036,ABC LTD,ABC,1000000,960000\n'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('combineoi_02092026.csv', csv)
    rows = nse_mwpl.parse_combined_oi_csv(buf.getvalue())
    assert rows['ABC']['mwpl_pct'] == 96.0
