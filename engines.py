import numpy as np


class ChangeEngine:
    TRACKED = {
        'tempo':         {'label': 'Tempo',    'unit': 'BPM'},
        'channel_count': {'label': 'Channels', 'unit': 'tracks'},
        'pattern_count': {'label': 'Patterns', 'unit': 'patterns'},
    }

    def compute(self, v1: dict, v2: dict) -> dict:
        changes = {}
        for key, meta in self.TRACKED.items():
            old = float(v1.get(key, 0)); new = float(v2.get(key, 0))
            delta = new - old
            if abs(delta) < 0.01: continue
            pct = (delta / old * 100) if old != 0 else 0.0
            changes[key] = dict(label=meta['label'], unit=meta['unit'],
                                before=round(old,2), after=round(new,2),
                                delta=round(delta,2), percent=round(pct,1),
                                direction='increased' if delta>0 else 'decreased')
        changes['total'] = len(changes)
        return changes


class ScoringEngine:
    def __init__(self, weights=None):
        self.weights = weights or dict(tempo=25.0, channels=35.0, patterns=25.0, maturity=15.0)

    def calculate(self, data: dict) -> float:
        w = self.weights; score = 0.0
        tempo = float(data.get('tempo', 0))
        score += w['tempo'] if 60 <= tempo <= 180 else (w['tempo'] * 0.4 if tempo > 0 else 0)
        score += min(int(data.get('channel_count', 0)) / 20.0, 1.0) * w['channels']
        score += min(int(data.get('pattern_count', 0)) / 15.0, 1.0) * w['patterns']
        score += min(int(data.get('version_num', 1)) / 10.0,   1.0) * w['maturity']
        return round(min(score, 100.0), 1)


class PredictionEngine:
    MIN = 3

    def predict(self, versions: list, metric: str) -> dict:
        n = len(versions)
        if n < self.MIN:
            return dict(available=False, message=f'Need at least {self.MIN} versions.')
        X = np.array([v.version_num for v in versions], dtype=float)
        y = np.array([getattr(v, metric, 0) for v in versions], dtype=float)
        A = np.column_stack([X, np.ones(n)])
        slope, intercept = np.linalg.lstsq(A, y, rcond=None)[0]
        next_x = float(versions[-1].version_num + 1)
        pred   = slope * next_x + intercept
        trend  = ('growing' if slope > 1 else 'improving' if slope > 0.1 else
                  'declining' if slope < -1 else 'tapering' if slope < -0.1 else 'stable')
        return dict(available=True, metric=metric, next_version=int(next_x),
                    predicted_value=round(float(pred), 2), trend=trend,
                    slope=round(float(slope), 3),
                    confidence='high' if n >= 5 else 'moderate')

    def predict_all(self, versions: list) -> dict:
        return {m: self.predict(versions, m)
                for m in ('quality_score', 'channel_count', 'tempo')}
