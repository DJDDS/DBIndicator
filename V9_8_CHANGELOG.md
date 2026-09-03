# V9.8.0 — Incremental OI Validation

- Preserves frozen Trial 19: total FUTSTK OI z >= 1.5, 2018-09-01 through 2021-08-31.
- Adds next-session Yang-Zhang-style daily variance proxy and Garman-Klass robustness target.
- Adds full HAR daily/weekly/monthly realised-variance controls.
- Adds point-in-time abnormal total FUTSTK volume z-score and HAR+volume+OI horse race.
- Repairs earnings validation auditability: symbols with actual result dates, result-date counts, event overlaps, examples, and inside/outside +/-5-session splits.
- Bumps resumable Trial-19 schema so older shards without futures-volume fields are not reused.
- Trial 18 remains LOCKED; V9.8 cannot activate TRADE/WATCH.
