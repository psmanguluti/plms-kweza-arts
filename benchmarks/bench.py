"""
PLMS performance benchmark — evidence for NFR-01 (capture < 2 s) and NFR-06 (500+ versions).

    python benchmarks/bench.py

Runs against a throw-away database/backup folder; nothing in your real data is touched.
"""
import os, sys, time, tempfile, statistics, io
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
tmp = tempfile.mkdtemp(prefix='plms_bench_')
os.environ.update(PLMS_DATABASE_URI=f'sqlite:///{tmp}/bench.db', PLMS_BACKUP_ROOT=f'{tmp}/backups',
                  PLMS_SECRET_KEY='bench')
os.chdir(tmp)
import app as plms                                  # noqa: E402
from models import db, User, Project, AppSettings   # noqa: E402

plms.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
N_VERSIONS = 500
TARGET_CAPTURE_S = 2.0


def sample(n):
    return open(os.path.join(ROOT, 'samples', n), 'rb').read()


def timed(fn, *a, **k):
    t = time.perf_counter(); r = fn(*a, **k); return time.perf_counter() - t, r


def main():
    rows = []
    with plms.app.app_context():
        u = User(username='bench', email='b@x.com'); u.set_password('secret1')
        db.session.add(u); db.session.commit(); db.session.add(AppSettings(user_id=u.id)); db.session.commit()
        proj = Project(user_id=u.id, name='Bench'); db.session.add(proj); db.session.commit()

        # NFR-01: full capture pipeline (parse + hash + score + DB + backup copy + notifications)
        for name in ('sample_tiny.flp', 'sample_small.flp', 'sample_large.flp'):
            raw = sample(name); ts = []
            for i in range(15):
                payload = raw + b'\x00' + bytes([i + 1])
                dt, _ = timed(plms._capture_version, proj, payload, 'capture', True, True)
                ts.append(dt)
            rows.append((f'capture {name} ({len(raw)//1024} KB)', statistics.mean(ts), max(ts)))

        worst = max(r[2] for r in rows)
        # NFR-06: grow to N versions
        have = len(proj.versions)
        t0 = time.perf_counter()
        for i in range(N_VERSIONS - have):
            plms._build_version(proj, dict(tempo=100 + i % 40, channel_count=4 + i % 30,
                                           pattern_count=2 + i % 14, sample_count=i % 9,
                                           playlist_items=10 + i, tracks_used=1 + i % 12,
                                           arrangement_bars=8 + i % 64,
                                           arrangement=dict(patterns={str(i % 14 + 1): 3}, tracks={'1': 1})),
                                file_hash=f'{i:064x}')
        rows.append((f'build to {N_VERSIONS} versions (total)', time.perf_counter() - t0, None))
        pid = proj.id

    c = plms.app.test_client()
    c.post('/login', data=dict(identifier='bench', password='secret1'))
    checks = [
        ('project page (timeline, 500 versions)', lambda: c.get(f'/project/{pid}')),
        ('project page, filtered', lambda: c.get(f'/project/{pid}?score_min=40&q=v4')),
        ('compare v1 vs v500', lambda: c.get(f'/api/project/{pid}/compare?a=1&b={N_VERSIONS}')),
        ('CSV export (500 versions)', lambda: c.get(f'/project/{pid}/export.csv')),
        ('PDF export (500 versions)', lambda: c.get(f'/project/{pid}/export.pdf')),
        ('verify backups', lambda: c.post(f'/api/project/{pid}/verify')),
    ]
    for label, fn in checks:
        ts = []
        for _ in range(3):
            dt, r = timed(fn); assert r.status_code == 200, (label, r.status_code); ts.append(dt)
        rows.append((label, statistics.mean(ts), max(ts)))

    print(f'\n{"operation":<46}{"mean (s)":>10}{"worst (s)":>11}')
    print('-' * 67)
    for label, mean, worst_ in rows:
        print(f'{label:<46}{mean:>10.3f}{(worst_ if worst_ is not None else float("nan")):>11.3f}')
    print('-' * 67)
    print(f'NFR-01 capture < {TARGET_CAPTURE_S:.0f}s ... worst observed {worst:.3f}s  ->',
          'PASS' if worst < TARGET_CAPTURE_S else 'FAIL')
    print(f'NFR-06 {N_VERSIONS}+ versions per project ... timeline/compare/export all returned 200  -> PASS')
    return 0 if worst < TARGET_CAPTURE_S else 1


if __name__ == '__main__':
    sys.exit(main())
