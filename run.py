"""
Entry point. Run with: python run.py
Or in production, with a proper WSGI server, e.g.:
    gunicorn -w 1 -b 0.0.0.0:5000 run:app
(Use exactly 1 worker - the background scanner thread and token cache
are per-process, so multiple workers would each try to log in/scan
independently.)
"""
from flask import jsonify

from app import config, recorder_observability, scanner, v12_feasibility_freeze
from app.web import create_app

log = logging.getLogger(__name__)

# Freeze the completed first-ten-day stock-option feasibility sample at
# process boot, before any future market session can append to the live state.
# This is intentionally fail-soft: provenance work must never prevent the web
# service or live recorders from starting.
try:
    V12_FEASIBILITY_FREEZE_BOOT = v12_feasibility_freeze.maybe_freeze_10d(
        config.V12_OPTION_STATE_FILE,
        config.V12_STORAGE_ROOT,
    )
except Exception as exc:  # pragma: no cover - production safety net
    V12_FEASIBILITY_FREEZE_BOOT = {
        "status": "FREEZE_VERIFICATION_ERROR",
        "reason": str(exc),
    }

app = create_app()


@app.route("/api/recorder-operational-health")
def recorder_operational_health():
    """Low-sensitivity, read-only recorder health for external auditing."""
    return jsonify(recorder_observability.operational_health(
        v12_snapshot_file=config.V12_OPTION_SNAPSHOT_FILE,
        v12_state_file=config.V12_OPTION_STATE_FILE,
        v121_root=config.V121_INDEX_VOL_ROOT,
        v121_state_file=config.V121_INDEX_VOL_STATE_FILE,
        backup_state_file=config.V121_INDEX_VOL_BACKUP_STATE_FILE,
        now=scanner.now_ist(),
        storage_mode=config.V12_STORAGE_MODE,
    ))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
