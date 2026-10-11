"""Read-only presentation of the existing q80/top-one underlying research profile.

No model inference, recorder, signal renewal, option routing or order placement.
Historical figures remain metadata claims pending trade-level verification.
"""
import datetime as dt
import json
import math


def presentation(day, now):
    result = {'state':'WAITING','pick':None,'entry_allowed':False,'validated':False,
              'reason':'No current-session 09:25 record available.',
              'exit_at':'15:15', 'profile':'q80 / trailing 250 sessions / top one / hold'}
    if not isinstance(day, dict) or day.get('date') != str(now.date()):
        return result
    settings = day.get('settings') or {}
    if not isinstance(settings, dict):
        result.update(state='INVALID_RECORD', reason='Stored selection settings are malformed.')
        return result
    if settings.get('hc_quantile') != 0.8 or settings.get('window') != 250:
        result.update(state='INCOMPATIBLE_PROFILE',reason='Stored selection used different day-strength settings.')
        return result
    try:
        at = dt.datetime.fromisoformat(str(day['at']))
        if at.tzinfo is not None or at.date() != now.date() or at > now or at.time() < dt.time(9,25):
            raise ValueError('availability timestamp')
        lane = day['lists']['hc']
        if not isinstance(lane, dict) or not isinstance(lane.get('picks'), list):
            raise ValueError('selection schema')
        view, threshold = float(lane['view']), float(lane['threshold'])
        if not math.isfinite(view) or not 0 <= view <= 1 or not math.isfinite(threshold) or threshold < 0:
            raise ValueError('day strength')
        strong = lane.get('strong')
        if not isinstance(strong, bool):
            raise ValueError('missing producer classification')
        # The producer stores view/threshold rounded to four decimals. Preserve
        # its full-precision classification inside the combined rounding band.
        margin = abs(view - 0.5) - threshold
        if (margin > 0) != strong and abs(margin) > 0.000100000001:
            raise ValueError('inconsistent strong flag')
        if not strong:
            result.update(state='NO_SETUP',reason='Day strength does not clear the frozen q80 threshold.')
            return result
        pick = lane['picks'][0]  # never refill a malformed or unavailable first selection
        probability = float(pick['p_up'])
        symbol, side = str(pick['symbol']), pick['side']
        if not symbol or not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError('first selection')
        if side not in ('UP', 'DOWN') or (probability != 0.5 and side != ('UP' if probability > 0.5 else 'DOWN')):
            raise ValueError('direction/probability mismatch')
    except (KeyError, IndexError, TypeError, ValueError):
        result.update(state='INVALID_RECORD',reason='Stored selection or availability clock is incomplete or inconsistent.')
        return result
    result.update(state='DIRECTION_WATCH' if now.time() < dt.time(15,15) else 'HORIZON_ENDED',
                  pick={'symbol':symbol,'side':side,'p_up':probability}, recorded_at=str(at),
                  reason='Original underlying direction remains visible for observation; late entry and option execution are unvalidated.')
    return result


def stored_presentation(root, now):
    """Read existing data without creating storage or starting background jobs."""
    path = root / 'days.jsonl'
    day = None
    try:
        if path.exists():
            with path.open() as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                        if isinstance(record, dict) and record.get('date') == str(now.date()):
                            day = record
                    except ValueError:
                        continue
    except (OSError, UnicodeError):
        result = presentation(None, now)
        result.update(state='DATA_UNAVAILABLE', reason='Existing direction records could not be read.')
        return result
    return presentation(day, now)
