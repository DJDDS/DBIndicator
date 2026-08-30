# V9.1.2 shutil cleanup fix

Build: `2026-08-30-INSTITUTIONAL-V9.1.2-SHUTIL-FIX`

- Fixes `NameError: shutil is not defined` after a successful streaming backtest completes and removes its checkpoint directory.
- Adds a regression test that executes the real background completion/cleanup path.
- No trading rules, thresholds, 15-minute/180-day protocol, costs, frozen Bear FSB fingerprint, or final-sample handling changed.
