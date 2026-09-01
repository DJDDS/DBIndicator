# V9.3.2 Research UI Isolation

- Isolates V9.3, V9.2 manual diagnostic, and 4H diagnostic into separate progress/error/result channels.
- V9.3 progress can no longer render inside the V9.2 card.
- Relabels V9.2 as manual diagnostic only.
- Gives 4H Diagnostic its own card and status channel.
- Removes the public Custom Backtest UI and `/api/backtest/start` + `/api/backtest/status` routes.
- Leaves fixed research engines, production evidence gate, Trial 13 preregistration, 0.18% friction, 1.25 ATR guard, and final-sample locks unchanged.
