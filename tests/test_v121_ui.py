from pathlib import Path

HTML=Path('app/templates/index.html')
WEB=Path('app/web.py')


def test_v121_index_volatility_card_has_health_backup_and_development_fields():
    html=HTML.read_text(encoding='utf-8')
    for token in (
        'id="v121-index-recorder"','id="v121-recorder-status"','id="v121-storage-status"',
        'id="v121-expiry"','id="v121-atm"','id="v121-tokens"','id="v121-last-tick"',
        'id="v121-micro-rows"','id="v121-depth-rows"','id="v121-micro-size"','id="v121-depth-size"',
        'id="v121-backup-status"','id="v121-last-backup"','id="v121-development-status"',
        'id="v121-development-sample"','id="v121-trial25-status"','id="v121-run-development"',
        'DEVELOPMENT ONLY · NOT VALIDATED',
    ):
        assert token in html


def test_v121_dashboard_polling_and_routes_exist():
    html=HTML.read_text(encoding='utf-8')
    web=WEB.read_text(encoding='utf-8')
    assert 'function renderV121IndexVol' in html
    assert 'renderV121IndexVol(state.v121_index_vol, state.v121_backup, state.v121_development);' in html
    for route in ('/api/v121-index-vol-health','/api/v121-development-status','/api/v121-development-run','/api/v121-index-micro/export','/api/v121-index-depth/export'):
        assert route in web


def test_v121_trial25_lock_language_is_explicit():
    html=HTML.read_text(encoding='utf-8')
    assert 'TRIAL 25 LOCKED' in html
    assert 'No V12.1 option-volatility setup is executable or validated.' in html
