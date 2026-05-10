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


# ── Scoring ────────────────────────────────────────────────────
class ScoringEngine:

    # Score ceilings for each dimension (must sum to 100)
    W_TEMPO     = 20.0
    W_PATTERNS  = 25.0
    W_CHANNELS  = 25.0
    W_SAMPLES   = 15.0
    W_MATURITY  = 15.0

    # Optimal pattern count range for a finished arrangement
    PATTERN_SOFT_CAP = 12   # full score
    PATTERN_HARD_CAP = 30   # beyond this, structure is probably messy

    # Optimal channel range
    CHANNEL_SOFT_CAP = 16
    CHANNEL_HARD_CAP = 48

    # Optimal sample ratio (samples / total channels)
    IDEAL_SAMPLE_RATIO = 0.45

    # Maturity: how many versions = "fully mature"
    MATURITY_CAP = 15

    def calculate(self, data: dict) -> float:
        tempo         = float(data.get('tempo',         120.0))
        channel_count = int(data.get('channel_count',   0))
        pattern_count = int(data.get('pattern_count',   1))
        sample_count  = int(data.get('sample_count',    0))
        version_num   = int(data.get('version_num',     1))

        score = 0.0

        # 1. Tempo Fitness ───────────────────────────────────────
        # Award full marks if BPM is in a common production range.
        # Gracefully degrades outside that range (not zero — just penalised).
        if 60 <= tempo <= 200:
            # Sweet spot: 80–160 BPM (covers Amapiano, Afrobeats, Hip-hop, R&B)
            if 80 <= tempo <= 160:
                score += self.W_TEMPO
            else:
                # Partial: fringe tempos (60-80 or 160-200)
                score += self.W_TEMPO * 0.6
        else:
            score += self.W_TEMPO * 0.2  # Very unusual tempo — minor penalty

        # 2. Arrangement Density (pattern count) ─────────────────
        # Scoring curve: rises up to PATTERN_SOFT_CAP, then gently falls
        # beyond PATTERN_HARD_CAP (too many patterns = chaotic arrangement).
        if pattern_count <= 0:
            p_score = 0.0
        elif pattern_count <= self.PATTERN_SOFT_CAP:
            p_score = (pattern_count / self.PATTERN_SOFT_CAP)
        elif pattern_count <= self.PATTERN_HARD_CAP:
            # Slight decline past soft cap
            excess  = pattern_count - self.PATTERN_SOFT_CAP
            range_  = self.PATTERN_HARD_CAP - self.PATTERN_SOFT_CAP
            p_score = 1.0 - 0.25 * (excess / range_)
        else:
            p_score = 0.75 - 0.1 * ((pattern_count - self.PATTERN_HARD_CAP) / 10)
            p_score = max(p_score, 0.3)
        score += p_score * self.W_PATTERNS

        # 3. Channel Complexity ──────────────────────────────────
        # Similar bell curve: reward layering up to soft cap.
        if channel_count <= 0:
            c_score = 0.0
        elif channel_count <= self.CHANNEL_SOFT_CAP:
            c_score = channel_count / self.CHANNEL_SOFT_CAP
        elif channel_count <= self.CHANNEL_HARD_CAP:
            excess  = channel_count - self.CHANNEL_SOFT_CAP
            range_  = self.CHANNEL_HARD_CAP - self.CHANNEL_SOFT_CAP
            c_score = 1.0 - 0.3 * (excess / range_)
        else:
            c_score = 0.5  # Very large session: probably messy
        score += c_score * self.W_CHANNELS

        # 4. Sample Diversity ────────────────────────────────────
        # Reward a balanced mix of sampled content vs synths/plugins.
        # Pure sample sessions and pure synth sessions both score lower
        # than a balanced 40-55% sample ratio.
        if channel_count > 0 and sample_count >= 0:
            ratio = sample_count / channel_count
            # Gaussian-style reward centred on IDEAL_SAMPLE_RATIO
            distance  = abs(ratio - self.IDEAL_SAMPLE_RATIO)
            s_score   = math.exp(-4.0 * distance ** 2)
        else:
            s_score = 0.5  # Unknown — give neutral score
        score += s_score * self.W_SAMPLES

        # 5. Production Maturity ─────────────────────────────────
        # Log-scaled so early iterations gain quickly,
        # later ones show diminishing returns (realistic).
        m_score = math.log1p(version_num) / math.log1p(self.MATURITY_CAP)
        m_score = min(m_score, 1.0)
        score  += m_score * self.W_MATURITY

        return round(min(score, 100.0), 1)

    def breakdown(self, data: dict) -> dict:
        """Return per-dimension scores for display in the UI."""
        tempo         = float(data.get('tempo',         120.0))
        channel_count = int(data.get('channel_count',   0))
        pattern_count = int(data.get('pattern_count',   1))
        sample_count  = int(data.get('sample_count',    0))
        version_num   = int(data.get('version_num',     1))

        return dict(
            tempo    = round(self.calculate({**data, 'channel_count': 0, 'pattern_count': 0,
                                             'sample_count': 0, 'version_num': 1}) *
                             (self.W_TEMPO / 100), 1),
            total    = self.calculate(data),
        )


# ── Change detection ───────────────────────────────────────────
class ChangeEngine:
    TRACKED = {
        'tempo':         {'label': 'Tempo',    'unit': 'BPM'},
        'channel_count': {'label': 'Channels', 'unit': 'tracks'},
        'pattern_count': {'label': 'Patterns', 'unit': 'patterns'},
        'sample_count':  {'label': 'Samples',  'unit': 'samples'},
    }

    def compute(self, v1: dict, v2: dict) -> dict:
        changes = {}
        for key, meta in self.TRACKED.items():
            old = float(v1.get(key, 0))
            new = float(v2.get(key, 0))
            delta = new - old
            if abs(delta) < 0.01:
                continue
            pct = (delta / old * 100) if old != 0 else 0.0
            changes[key] = dict(
                label     = meta['label'],
                unit      = meta['unit'],
                before    = round(old, 2),
                after     = round(new, 2),
                delta     = round(delta, 2),
                percent   = round(pct, 1),
                direction = 'increased' if delta > 0 else 'decreased',
            )
        changes['total'] = len(changes)
        return changes


# ── Prediction ─────────────────────────────────────────────────
class PredictionEngine:
    MIN = 3

    def predict(self, versions: list, metric: str) -> dict:
        n = len(versions)
        if n < self.MIN:
            return dict(available=False,
                        message=f'Need at least {self.MIN} versions.')
        X = np.array([v.version_num for v in versions], dtype=float)
        y = np.array([getattr(v, metric, 0) for v in versions], dtype=float)
        A = np.column_stack([X, np.ones(n)])
        slope, intercept = np.linalg.lstsq(A, y, rcond=None)[0]
        next_x = float(versions[-1].version_num + 1)
        pred   = slope * next_x + intercept
        trend  = ('growing'   if slope >  1   else
                  'improving' if slope >  0.1 else
                  'declining' if slope < -1   else
                  'tapering'  if slope < -0.1 else 'stable')
        return dict(
            available       = True,
            metric          = metric,
            next_version    = int(next_x),
            predicted_value = round(float(pred), 2),
            trend           = trend,
            slope           = round(float(slope), 3),
            confidence      = 'high' if n >= 5 else 'moderate',
        )

    def predict_all(self, versions: list) -> dict:
        return {m: self.predict(versions, m)
                for m in ('quality_score', 'channel_count', 'tempo')}
