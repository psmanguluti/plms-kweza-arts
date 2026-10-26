from types import SimpleNamespace as NS
import pytest
from conftest import sample
from flp_parser import parse_flp_bytes
from engines import (ScoringEngine, ChangeEngine, PredictionEngine, PatternAnalyzer,
                     structural_complexity, DEFAULT_WEIGHTS)


# ---- parser -------------------------------------------------------------
def test_parser_reads_tempo_and_arrangement():
    d = parse_flp_bytes(sample('sample_small.flp'))
    assert d['tempo'] == 112.0
    assert d['channel_count'] == 12
    assert d['playlist_items'] == 16
    assert d['tracks_used'] == 3
    assert d['arrangement_bars'] == 16.0
    assert set(d['arrangement']['patterns']) == {'1', '2'}
    assert d['pattern_count'] == 2


def test_parser_large_sample():
    d = parse_flp_bytes(sample('sample_large.flp'))
    assert d['playlist_items'] == 81 and d['tracks_used'] == 7
    assert d['pattern_count'] == 6


@pytest.mark.parametrize('blob', [b'', b'garbage', b'FLhd' + b'\x00' * 40, sample('sample_tiny.flp')[:200]])
def test_parser_never_raises_on_bad_input(blob):
    d = parse_flp_bytes(blob)
    assert 'tempo' in d and 'channel_count' in d


# ---- scoring ------------------------------------------------------------
BASE = dict(tempo=112, channel_count=12, pattern_count=6, sample_count=5, version_num=3)


def test_score_in_range_and_default_weights_sum_100():
    assert sum(DEFAULT_WEIGHTS.values()) == 100
    assert 0 <= ScoringEngine().calculate(BASE) <= 100


def test_breakdown_sums_to_total():
    bd = ScoringEngine().breakdown(BASE)
    assert len(bd['dimensions']) == 5
    assert abs(sum(d['points'] for d in bd['dimensions']) - bd['total']) < 0.3


def test_weights_change_the_score_and_are_normalised():
    only_tempo = ScoringEngine(dict(tempo=1, channels=0, patterns=0, samples=0, maturity=0))
    assert only_tempo.calculate(BASE) == 100.0           # 112 BPM is in the sweet spot
    assert only_tempo.calculate({**BASE, 'tempo': 300}) == 20.0
    scaled = ScoringEngine({k: v * 3 for k, v in DEFAULT_WEIGHTS.items()})
    assert scaled.calculate(BASE) == ScoringEngine().calculate(BASE)


def test_all_zero_weights_fall_back_to_defaults():
    assert ScoringEngine(dict(tempo=0, channels=0, patterns=0, samples=0, maturity=0)).calculate(BASE) \
        == ScoringEngine().calculate(BASE)


def test_complexity_bounds():
    assert structural_complexity({}) == 0
    big = dict(channel_count=99, pattern_count=99, playlist_items=999, tracks_used=99, arrangement_bars=999)
    assert structural_complexity(big) == 100


# ---- change / compare ---------------------------------------------------
def v(**kw):
    d = dict(tempo=100, channel_count=4, pattern_count=2, sample_count=1, playlist_items=10,
             tracks_used=2, arrangement_bars=8, has_arrangement=True, quality_score=50,
             version_num=1, arrangement=dict(patterns={'1': 4, '2': 6}, tracks={'1': 5, '2': 5}))
    d.update(kw); return d


def test_compare_detects_arrangement_changes():
    a = v()
    b = v(version_num=2, channel_count=7, playlist_items=14, quality_score=61.5,
          arrangement=dict(patterns={'1': 4, '3': 10}, tracks={'1': 5, '2': 5, '3': 4}))
    r = ChangeEngine().compare(a, b)
    assert r['score_delta'] == 11.5
    ch = next(m for m in r['metrics'] if m['key'] == 'channel_count')
    assert ch['delta'] == 3 and ch['direction'] == 'increased'
    arr = r['arrangement']
    assert [p['id'] for p in arr['patterns_added']] == [3]
    assert [p['id'] for p in arr['patterns_removed']] == [2]
    assert [t['id'] for t in arr['tracks_added']] == [3]


def test_compare_identical_versions_is_unchanged():
    r = ChangeEngine().compare(v(), v(version_num=2))
    assert r['changed_count'] == 0 and 'identical' in r['summary']


def test_compare_skips_arrangement_when_legacy_version():
    r = ChangeEngine().compare(v(has_arrangement=False, playlist_items=0), v(version_num=2))
    assert r['arrangement']['available'] is False
    assert next(m for m in r['metrics'] if m['key'] == 'playlist_items')['available'] is False


def test_change_compute_ignores_arrangement_for_legacy():
    c = ChangeEngine().compute(v(has_arrangement=False, playlist_items=0), v())
    assert 'playlist_items' not in c


# ---- prediction ---------------------------------------------------------
def versions(vals, metric='quality_score'):
    return [NS(version_num=i + 1, **{metric: x}) for i, x in enumerate(vals)]


def test_prediction_needs_three_versions():
    assert PredictionEngine().predict(versions([1, 2]), 'quality_score')['available'] is False


def test_prediction_linear_series_is_exact_and_high_confidence():
    p = PredictionEngine().predict(versions([10, 20, 30, 40, 50]), 'quality_score')
    assert p['predicted_value'] == 60 and p['r_squared'] == 1.0 and p['confidence'] == 'high'
    assert p['trend'] == 'growing'


def test_prediction_clamped_to_valid_range():
    p = PredictionEngine().predict(versions([80, 90, 99, 99.9]), 'quality_score')
    assert p['predicted_value'] <= 100
    p = PredictionEngine().predict(versions([30, 20, 10]), 'channel_count' if False else 'quality_score')
    assert p['predicted_value'] >= 0


def test_trend_is_scale_independent():
    # same 2% relative growth must read the same on tempo (~120) and score (~50)
    t = PredictionEngine().predict(versions([120, 122.4, 124.8, 127.2], 'tempo'), 'tempo')
    s = PredictionEngine().predict(versions([50, 51, 52, 53], 'quality_score'), 'quality_score')
    assert t['trend'] == s['trend']


def test_noisy_data_gets_low_confidence():
    p = PredictionEngine().predict(versions([50, 90, 20, 80, 30, 85]), 'quality_score')
    assert p['confidence'] == 'low'


def test_flat_series_is_stable():
    p = PredictionEngine().predict(versions([60, 60, 60, 60]), 'quality_score')
    assert p['trend'] == 'stable' and p['r_squared'] == 1.0


# ---- pattern analyzer ---------------------------------------------------
def test_patterns_detect_plateau_and_regression():
    vs = [NS(version_num=i + 1, tempo=100, channel_count=c, pattern_count=p, quality_score=s)
          for i, (c, p, s) in enumerate([(4, 2, 40), (8, 4, 70), (8, 4, 70.2), (8, 4, 70.3), (8, 4, 60)])]
    r = PatternAnalyzer().summarise(vs)
    titles = [i['title'] for i in r['insights']]
    assert r['available'] and 'Below your best' in titles and 'Biggest score change' in titles


def test_patterns_unavailable_below_three():
    assert PatternAnalyzer().summarise(versions([1, 2]))['available'] is False
