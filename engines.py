"""
engines.py — PLMS scoring, change detection, and prediction.

ScoringEngine
─────────────
Scores a project version based on production-structure decisions
extracted from the .flp file. Criteria are modelled on what professional
mixing/mastering engineers would look for in an arrangement file,
adapted for what is actually parseable from FL Studio project data.

The score is 0–100 and is broken into five weighted dimensions:

  1. Tempo Fitness          (20 pts)  — Is the BPM in a genre-appropriate range?
  2. Arrangement Density   (25 pts)  — How many patterns are active in the playlist?
  3. Channel Complexity    (25 pts)  — How many unique instrument/sample channels?
  4. Sample Diversity      (15 pts)  — Ratio of sample channels to total channels.
                                       High ratio → relies on sampled content;
                                       moderate ratio → balanced; low → mostly synths.
  5. Production Maturity   (15 pts)  — Based on number of saved versions.
                                       More iterations → more developed project.

These dimensions map to real production decisions:
  - Pattern count  → arrangement structure and song sections
  - Channel count  → mix complexity / layering
  - Sample count   → use of external audio (loops, one-shots, vocals, etc.)
  - Version depth  → how thoroughly the producer has iterated
"""

import math
import numpy as np


DEFAULT_WEIGHTS = dict(tempo=20.0, patterns=25.0, channels=25.0, samples=15.0, maturity=15.0)


def structural_complexity(d: dict) -> float:
    """
    Structural complexity index, 0-100 (FR-10).
    Weighted blend of how much material the project holds:
      channels 30% (cap 32) | patterns 25% (cap 16) | playlist clips 25% (cap 100)
      | playlist tracks used 10% (cap 16) | arrangement length 10% (cap 128 bars)
    """
    c = lambda v, cap: min(float(v or 0) / cap, 1.0)
    idx = (0.30 * c(d.get('channel_count'),   32) +
           0.25 * c(d.get('pattern_count'),   16) +
           0.25 * c(d.get('playlist_items'), 100) +
           0.10 * c(d.get('tracks_used'),     16) +
           0.10 * c(d.get('arrangement_bars'), 128))
    return round(idx * 100, 1)


# ── Scoring ────────────────────────────────────────────────────
class ScoringEngine:
    """
    Weights are user-configurable (Settings -> Scoring). They are normalised to
    sum to 100, so any slider combination is valid. Defaults: see DEFAULT_WEIGHTS.
    """
    PATTERN_SOFT_CAP = 12
    PATTERN_HARD_CAP = 30
    CHANNEL_SOFT_CAP = 16
    CHANNEL_HARD_CAP = 48
    IDEAL_SAMPLE_RATIO = 0.45
    MATURITY_CAP = 15

    LABELS = dict(tempo='Tempo fitness', patterns='Arrangement density',
                  channels='Channel complexity', samples='Sample diversity',
                  maturity='Production maturity')

    def __init__(self, weights: dict = None):
        w = {k: max(float((weights or {}).get(k, DEFAULT_WEIGHTS[k])), 0.0)
             for k in DEFAULT_WEIGHTS}
        total = sum(w.values())
        if total <= 0:
            w = dict(DEFAULT_WEIGHTS); total = sum(w.values())
        self.weights = {k: v * 100.0 / total for k, v in w.items()}

    # --- per-dimension ratios (0..1) ---------------------------------
    def ratios(self, data: dict) -> dict:
        tempo    = float(data.get('tempo', 120.0))
        channels = int(data.get('channel_count', 0))
        patterns = int(data.get('pattern_count', 1))
        samples  = int(data.get('sample_count', 0) or 0)
        vnum     = int(data.get('version_num', 1))

        if 80 <= tempo <= 160:   t = 1.0
        elif 60 <= tempo <= 200: t = 0.6
        else:                    t = 0.2

        if patterns <= 0:                       p = 0.0
        elif patterns <= self.PATTERN_SOFT_CAP: p = patterns / self.PATTERN_SOFT_CAP
        elif patterns <= self.PATTERN_HARD_CAP:
            p = 1.0 - 0.25 * ((patterns - self.PATTERN_SOFT_CAP) /
                              (self.PATTERN_HARD_CAP - self.PATTERN_SOFT_CAP))
        else:
            p = max(0.75 - 0.1 * ((patterns - self.PATTERN_HARD_CAP) / 10), 0.3)

        if channels <= 0:                       c = 0.0
        elif channels <= self.CHANNEL_SOFT_CAP: c = channels / self.CHANNEL_SOFT_CAP
        elif channels <= self.CHANNEL_HARD_CAP:
            c = 1.0 - 0.3 * ((channels - self.CHANNEL_SOFT_CAP) /
                             (self.CHANNEL_HARD_CAP - self.CHANNEL_SOFT_CAP))
        else:                                   c = 0.5

        if channels > 0:
            s = math.exp(-4.0 * (samples / channels - self.IDEAL_SAMPLE_RATIO) ** 2)
        else:
            s = 0.5

        m = min(math.log1p(vnum) / math.log1p(self.MATURITY_CAP), 1.0)
        return dict(tempo=t, patterns=p, channels=c, samples=s, maturity=m)

    def calculate(self, data: dict) -> float:
        r = self.ratios(data)
        score = sum(r[k] * self.weights[k] for k in r)
        return round(min(score, 100.0), 1)

    def breakdown(self, data: dict) -> dict:
        """Per-dimension points for display (FR-13): sums to `total`."""
        r = self.ratios(data)
        dims = [dict(key=k, label=self.LABELS[k],
                     points=round(r[k] * self.weights[k], 1),
                     max=round(self.weights[k], 1),
                     ratio=round(r[k], 3)) for k in r]
        return dict(dimensions=dims, total=self.calculate(data))


# ── Change detection / comparison ──────────────────────────────
class ChangeEngine:
    TRACKED = {
        'tempo':            {'label': 'Tempo',              'unit': 'BPM'},
        'channel_count':    {'label': 'Channels',           'unit': 'tracks'},
        'pattern_count':    {'label': 'Patterns',           'unit': 'patterns'},
        'sample_count':     {'label': 'Samples',            'unit': 'samples'},
        'playlist_items':   {'label': 'Playlist clips',     'unit': 'clips',  'arr': True},
        'tracks_used':      {'label': 'Playlist tracks',    'unit': 'tracks', 'arr': True},
        'arrangement_bars': {'label': 'Arrangement length', 'unit': 'bars',   'arr': True},
        'complexity':       {'label': 'Structural complexity', 'unit': 'pts', 'arr': True},
    }

    @staticmethod
    def _with_derived(d: dict) -> dict:
        d = dict(d)
        d['complexity'] = structural_complexity(d)
        return d

    def compute(self, v1: dict, v2: dict) -> dict:
        """Changed metrics only (stored with each version)."""
        a, b = self._with_derived(v1), self._with_derived(v2)
        both_arr = bool(a.get('has_arrangement') and b.get('has_arrangement'))
        changes = {}
        for key, meta in self.TRACKED.items():
            if meta.get('arr') and not both_arr:
                continue
            old, new = float(a.get(key, 0) or 0), float(b.get(key, 0) or 0)
            delta = new - old
            if abs(delta) < 0.01:
                continue
            changes[key] = dict(
                label=meta['label'], unit=meta['unit'],
                before=round(old, 2), after=round(new, 2), delta=round(delta, 2),
                percent=round((delta / old * 100) if old else 0.0, 1),
                direction='increased' if delta > 0 else 'decreased')
        changes['total'] = len(changes)
        return changes

    def compare(self, a: dict, b: dict) -> dict:
        """
        Full comparison between ANY two versions (FR-06..FR-10).
        a = earlier/base version dict, b = later/target version dict.
        """
        A, B = self._with_derived(a), self._with_derived(b)
        both_arr = bool(a.get('has_arrangement') and b.get('has_arrangement'))
        metrics = []
        for key, meta in self.TRACKED.items():
            avail = (not meta.get('arr')) or both_arr
            old, new = float(A.get(key, 0) or 0), float(B.get(key, 0) or 0)
            delta = new - old
            metrics.append(dict(
                key=key, label=meta['label'], unit=meta['unit'], available=avail,
                before=round(old, 2), after=round(new, 2),
                delta=round(delta, 2) if avail else None,
                percent=round((delta / old * 100) if old else 0.0, 1) if avail else None,
                direction=('unchanged' if abs(delta) < 0.01 else
                           'increased' if delta > 0 else 'decreased') if avail else 'n/a'))

        arrangement = dict(available=both_arr, patterns_added=[], patterns_removed=[],
                           patterns_changed=[], tracks_added=[], tracks_removed=[],
                           tracks_changed=[])
        if both_arr:
            pa = (a.get('arrangement') or {}).get('patterns', {})
            pb = (b.get('arrangement') or {}).get('patterns', {})
            ta = (a.get('arrangement') or {}).get('tracks', {})
            tb = (b.get('arrangement') or {}).get('tracks', {})
            def diff(x, y, added, removed, changed):
                for k in sorted(set(y) - set(x), key=int):
                    arrangement[added].append(dict(id=int(k), count=y[k]))
                for k in sorted(set(x) - set(y), key=int):
                    arrangement[removed].append(dict(id=int(k), count=x[k]))
                for k in sorted(set(x) & set(y), key=int):
                    if x[k] != y[k]:
                        arrangement[changed].append(dict(id=int(k), before=x[k], after=y[k]))
            diff(pa, pb, 'patterns_added', 'patterns_removed', 'patterns_changed')
            diff(ta, tb, 'tracks_added', 'tracks_removed', 'tracks_changed')

        sa, sb = float(a.get('quality_score', 0) or 0), float(b.get('quality_score', 0) or 0)
        changed = [m for m in metrics if m['available'] and m['direction'] not in ('unchanged', 'n/a')]
        return dict(
            a=dict(version_num=a.get('version_num'), quality_score=sa),
            b=dict(version_num=b.get('version_num'), quality_score=sb),
            score_delta=round(sb - sa, 1),
            metrics=metrics, arrangement=arrangement,
            changed_count=len(changed),
            summary=self._summary(a, b, sb - sa, changed))

    @staticmethod
    def _summary(a, b, sd, changed):
        if not changed and abs(sd) < 0.05:
            return f"v{b.get('version_num')} is identical to v{a.get('version_num')} on every tracked metric."
        parts = [f"{m['label'].lower()} {m['direction']} ({m['before']:g} -> {m['after']:g})"
                 for m in changed[:3]]
        trend = 'up' if sd > 0.05 else 'down' if sd < -0.05 else 'unchanged'
        return (f"v{a.get('version_num')} -> v{b.get('version_num')}: score {trend} "
                f"({sd:+.1f}); " + '; '.join(parts) + '.')


# ── Prediction ─────────────────────────────────────────────────
class PredictionEngine:
    """
    Least-squares linear trend per metric.
    - trend labels use the *relative* slope (change per version as a fraction of
      the metric's average), so tempo, channels and score share one scale.
    - confidence combines sample size with goodness of fit (R^2).
    """
    MIN = 3
    BOUNDS = {'quality_score': (0.0, 100.0), 'channel_count': (0.0, None),
              'tempo': (20.0, 400.0), 'pattern_count': (0.0, None),
              'complexity': (0.0, 100.0)}

    @staticmethod
    def _trend(rel: float) -> str:
        if rel >  0.05:  return 'growing'
        if rel >  0.01:  return 'improving'
        if rel < -0.05:  return 'declining'
        if rel < -0.01:  return 'tapering'
        return 'stable'

    def predict(self, versions: list, metric: str) -> dict:
        n = len(versions)
        if n < self.MIN:
            return dict(available=False, message=f'Need at least {self.MIN} versions.')
        X = np.array([v.version_num for v in versions], dtype=float)
        y = np.array([float(getattr(v, metric, 0) or 0) for v in versions], dtype=float)
        A = np.column_stack([X, np.ones(n)])
        slope, intercept = np.linalg.lstsq(A, y, rcond=None)[0]
        fitted = slope * X + intercept
        ss_res = float(np.sum((y - fitted) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        r2 = 1.0 if ss_tot < 1e-9 else max(0.0, 1.0 - ss_res / ss_tot)

        next_x = float(versions[-1].version_num + 1)
        pred = slope * next_x + intercept
        lo, hi = self.BOUNDS.get(metric, (None, None))
        if lo is not None: pred = max(pred, lo)
        if hi is not None: pred = min(pred, hi)

        scale = max(abs(float(y.mean())), 1e-9)
        rel = float(slope) / scale
        if ss_tot < 1e-9:
            rel = 0.0

        if n >= 5 and r2 >= 0.7:   conf = 'high'
        elif r2 >= 0.4:            conf = 'moderate'
        else:                      conf = 'low'
        return dict(available=True, metric=metric, next_version=int(next_x),
                    predicted_value=round(float(pred), 2), trend=self._trend(rel),
                    slope=round(float(slope), 3), relative_slope=round(rel, 4),
                    r_squared=round(r2, 3), confidence=conf)

    def predict_all(self, versions: list) -> dict:
        return {m: self.predict(versions, m)
                for m in ('quality_score', 'channel_count', 'tempo')}


# ── Pattern identification (FR-16) ─────────────────────────────
class PatternAnalyzer:
    """Plain-language development patterns found across a project's versions."""
    MIN = 3
    METRICS = [('tempo', 'Tempo'), ('channel_count', 'Channels'),
               ('pattern_count', 'Patterns'), ('quality_score', 'Score')]

    def summarise(self, versions: list) -> dict:
        n = len(versions)
        if n < self.MIN:
            return dict(available=False, insights=[],
                        message=f'Need at least {self.MIN} versions.')
        g = lambda v, m: float(getattr(v, m, 0) or 0)
        insights = []

        # 1. biggest score movement
        deltas = [(versions[i].version_num, g(versions[i], 'quality_score') -
                   g(versions[i - 1], 'quality_score')) for i in range(1, n)]
        vnum, d = max(deltas, key=lambda t: abs(t[1]))
        if abs(d) >= 0.5:
            insights.append(dict(title='Biggest score change',
                detail=f"v{vnum} moved the score by {d:+.1f} points."))

        # 2. most volatile metric (mean absolute change relative to average)
        vol = {}
        for m, label in self.METRICS[:3]:
            vals = [g(v, m) for v in versions]
            mean = abs(np.mean(vals)) or 1.0
            vol[label] = float(np.mean(np.abs(np.diff(vals)))) / mean
        label, v = max(vol.items(), key=lambda t: t[1])
        if v > 0.01:
            insights.append(dict(title='Most active metric',
                detail=f"{label} changes the most between saves."))

        # 3. phase: building vs refining (last 3 saves vs everything before)
        struct_chg = lambda i: (abs(g(versions[i], 'channel_count') - g(versions[i - 1], 'channel_count')) +
                                abs(g(versions[i], 'pattern_count') - g(versions[i - 1], 'pattern_count')))
        recent = np.mean([struct_chg(i) for i in range(n - 2, n)])
        earlier = np.mean([struct_chg(i) for i in range(1, n - 2)]) if n > 3 else recent
        if recent == 0:
            insights.append(dict(title='Development phase',
                detail='Refinement phase: no channels or patterns added in the last saves.'))
        elif recent < 0.5 * earlier:
            insights.append(dict(title='Development phase',
                detail='Slowing down: structural growth is well below earlier saves.'))
        else:
            insights.append(dict(title='Development phase',
                detail='Building phase: channels/patterns are still being added.'))

        # 4. plateau
        flat = 0
        for _, dd in reversed(deltas):
            if abs(dd) < 1.0: flat += 1
            else: break
        if flat >= 2:
            insights.append(dict(title='Score plateau',
                detail=f"Score has moved less than 1 point for the last {flat} saves."))

        # 5. regression vs best
        best = max(versions, key=lambda v: g(v, 'quality_score'))
        if best is not versions[-1] and g(best, 'quality_score') - g(versions[-1], 'quality_score') > 5:
            insights.append(dict(title='Below your best',
                detail=f"v{best.version_num} scored {g(best, 'quality_score'):.1f}, "
                       f"{g(best, 'quality_score') - g(versions[-1], 'quality_score'):.1f} "
                       f"points above the latest version."))
        return dict(available=True, insights=insights)
