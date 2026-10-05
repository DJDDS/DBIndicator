"""Dependency-free reader for a LightGBM text model (regression, numerical splits only).

The live server does not need the lightgbm package: this parses the frozen model file and evaluates
the trees with numpy. ``tests/test_v130_gbm.py`` checks it against reference predictions produced by
LightGBM itself when the model was frozen.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


class TextGBM:
    def __init__(self, path: str | Path):
        text = Path(path).read_text()
        self.feature_names: list[str] = []
        self.trees = []
        block: dict = {}
        in_tree = False
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('feature_names='):
                self.feature_names = line.split('=', 1)[1].split()
            elif line.startswith('Tree='):
                if block:
                    self.trees.append(self._tree(block))
                block = {}; in_tree = True
            elif in_tree and '=' in line:
                k, v = line.split('=', 1); block[k] = v
            elif line.startswith('end of trees'):
                if block:
                    self.trees.append(self._tree(block))
                block = {}; in_tree = False
        if not self.trees:
            raise ValueError('no trees found in model file')

    @staticmethod
    def _tree(b: dict) -> dict:
        arr = lambda k, t=float: np.array(b[k].split(), dtype=t) if k in b and b[k] else np.array([], dtype=t)
        dt_ = arr('decision_type', int)
        if dt_.size and np.any((dt_ & 1) != 0):
            raise ValueError('categorical splits are not supported')
        return dict(feat=arr('split_feature', int), thr=arr('threshold'), dtype=dt_, left=arr('left_child', int), right=arr('right_child', int),
                    leaf=arr('leaf_value'), shrink=float(b.get('shrinkage', 1)))

    def predict(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=np.float64)
        out = np.zeros(len(X))
        for t in self.trees:
            if t['feat'].size == 0:
                out += t['leaf'][0]
                continue
            node = np.zeros(len(X), dtype=int)
            active = np.ones(len(X), dtype=bool)
            res = np.zeros(len(X))
            while active.any():
                idx = np.where(active)[0]; nd = node[idx]
                f = t['feat'][nd]; thr = t['thr'][nd]; d = t['dtype'][nd]
                x = X[idx, f]
                default_left = (d & 2) != 0
                missing_type = (d >> 2) & 3            # 0 none, 1 zero, 2 nan
                isnan = np.isnan(x)
                xz = np.where(isnan & (missing_type != 2), 0.0, x)
                is_missing = (missing_type == 2) & isnan | (missing_type == 1) & (np.abs(xz) <= 1e-35)
                go_left = np.where(is_missing, default_left, xz <= thr)
                nxt = np.where(go_left, t['left'][nd], t['right'][nd])
                leaf = nxt < 0
                res[idx[leaf]] = t['leaf'][~nxt[leaf]]
                active[idx[leaf]] = False
                node[idx[~leaf]] = nxt[~leaf]
            out += res
        return out
