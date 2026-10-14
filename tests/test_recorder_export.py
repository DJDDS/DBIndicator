import zipfile
from pathlib import Path
from app import recorder_monitor as rm


def _mk(root: Path):
    (root / 'v133').mkdir(parents=True); (root / 'v130').mkdir()
    (root / 'v133' / 'days.jsonl').write_text('{"a":1}\n'); (root / 'v133' / 'rs_days.jsonl').write_text('{"b":2}\n')
    (root / 'v133' / 'of_2026-10-14.jsonl').write_text('{}\n'); (root / 'v130' / 'ledger.jsonl').write_text('{"c":3}\n')
    (root / 'secret_token.json').write_text('{"access_token":"x"}')            # not in any recorder pattern: must never be exported


def test_export_all_and_single_recorder_only_whitelisted_files(tmp_path):
    _mk(tmp_path)
    made = rm.export_zip(str(tmp_path), None, None, dest_dir=str(tmp_path))
    assert made and made[1].startswith('all_recorders_')
    names = set(zipfile.ZipFile(made[0]).namelist())
    assert {'v133/days.jsonl', 'v133/rs_days.jsonl', 'v133/of_2026-10-14.jsonl', 'v130/ledger.jsonl'} <= names and 'secret_token.json' not in names
    specs = rm.registry(str(tmp_path)); i = next(k for k, sp in enumerate(specs) if sp['name'] == 'V13.4 relative-strength desk')
    one = rm.export_zip(str(tmp_path), None, i, dest_dir=str(tmp_path)); assert zipfile.ZipFile(one[0]).namelist() == ['v133/rs_days.jsonl']
    assert rm.export_zip(str(tmp_path), None, 9999) is None and rm.export_zip(str(tmp_path / 'nothing'), None, None) is None


def test_report_rows_carry_export_ids(tmp_path):
    _mk(tmp_path); rows = rm.report(str(tmp_path))['rows']
    assert [r['id'] for r in rows] == list(range(len(rows)))


def test_index_vol_archives_are_exported(tmp_path):
    iv = tmp_path / 'iv'; iv.mkdir(); (iv / '2026-10-13_micro.jsonl.gz').write_bytes(b'x'); (iv / '2026-10-14_micro.jsonl').write_text('{}\n')
    made = rm.export_zip(str(tmp_path), str(iv), None, dest_dir=str(tmp_path))
    assert {'iv/2026-10-13_micro.jsonl.gz', 'iv/2026-10-14_micro.jsonl'} <= set(zipfile.ZipFile(made[0]).namelist())
