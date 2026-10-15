# V13.6 - Hierarchy desk: both sides, walk-forward lab

## Why
The V4.7 hierarchy desk (`/admin/intraday#hierarchy`) was hard-wired to DOWN only, so UP candidates were counted and discarded and could never prove themselves.
It also produced nothing while the 09:30-09:45 volume baseline was still under 10 days, without saying why.

## What changed
- **Both sides scored every day** (`app/v135_hierarchy.py`). New `side_mode` setting: `auto` (default), `both`, `down`, `up`.
  An active side gets one PICK. An inactive side still records its best stock as **SHADOW** (never traded), so it builds a live record.
- **`auto` mode follows the hierarchy lab.** A side is ACTIVE only when the lab's walk-forward test supports it, and it then uses the lab's chosen gates and exit.
  Before the lab has run: DOWN active, UP shadow (the earlier 2019-26 replication).
- **Hierarchy lab** (`app/v136_hierarchy_lab.py`, `POST /api/v136/lab/start`, `GET /api/v136/lab/status`, "Run lab" button on the card).
  - Downloads Kite 5-minute history for today's F&O stocks (default from 2019-01-01, incremental cache in `v133/v47lab/`).
  - Replays the exact live rule for UP and DOWN over 64 fixed variants per side: efficiency gate {0, 0.5} x volume gate {0, 1.5} x market tailwind {0, 0.15%} x exit {+30 min, +60 min, 15:15, book +1%} x top {1, 3}.
  - The variant is chosen on days before 2024-01-01 and judged on 2024 onwards.
  - ACTIVE needs: test net > 0 after cost, t >= 1.5, 100+ test days, positive in most test years.
  - Refused during market hours (it shares Kite's historical rate limit).
  - Also fills the 20-day volume baseline at once, which ends the warm-up.
- **Warm-up message** on the card when the volume baseline is short. **Baseline days** box (3-20) in settings.
- New outcome `book1`: book at +1% on first touch, else hold to the exit.
- Scorecard per side: live picks on their own exit rule, and each side's best stock every day (pick or shadow).

## Limits
- Survivorship bias: today's F&O list is used, not point-in-time membership.
- Present-day industry map.
- Entry is the 09:40 bar close (= the 09:45 price).
