"""NFR-01 / NFR-06 regression guards (generous limits; see benchmarks/bench.py for real numbers)."""
import time
from conftest import sample, register, make_project, plms
from models import Project


def test_capture_under_two_seconds(alice):
    pid = make_project(alice, flp='sample_tiny.flp')
    with plms.app.app_context():
        proj = Project.query.get(pid)
        for name in ('sample_small.flp', 'sample_large.flp'):
            t = time.perf_counter()
            plms._capture_version(proj, sample(name) + b'\x00\x07', 'capture', True, True)
            assert time.perf_counter() - t < 2.0


def test_500_versions_stay_usable(alice):
    pid = make_project(alice, flp='sample_tiny.flp')
    with plms.app.app_context():
        proj = Project.query.get(pid)
        for i in range(499):
            plms._build_version(proj, dict(tempo=100 + i % 40, channel_count=4 + i % 30,
                                           pattern_count=2 + i % 14, sample_count=i % 9),
                                file_hash=f'{i:064x}')
    t = time.perf_counter()
    assert alice.get(f'/project/{pid}').status_code == 200
    assert alice.get(f'/api/project/{pid}/compare?a=1&b=500').status_code == 200
    assert alice.get(f'/project/{pid}/export.csv').status_code == 200
    assert time.perf_counter() - t < 15
