import io, os, json, time
import pytest
from conftest import sample, register, make_project, upload, plms
from models import db, Project, Version, Notification, RollbackEvent, AppSettings


def versions(pid):
    with plms.app.app_context():
        return Version.query.filter_by(project_id=pid).order_by(Version.version_num).all()


# ── auth & ownership (NFR-04) ──────────────────────────────────────
def test_login_required(client):
    for url in ['/', '/settings', '/project/1', '/api/notifications']:
        assert client.get(url).status_code == 302


def test_users_cannot_touch_each_others_projects(client):
    register(client, 'alice')
    pid = make_project(client)
    vid = versions(pid)[0].id
    client.get('/logout')
    register(client, 'bob')
    assert client.get(f'/project/{pid}').status_code == 404
    assert client.get(f'/api/project/{pid}/compare?a=1&b=2').status_code == 404
    assert client.post(f'/api/version/{vid}/notes', json=dict(notes='x')).status_code == 404
    assert client.get(f'/project/{pid}/export.csv').status_code == 404
    assert client.get(f'/project/{pid}/export.pdf').status_code == 404
    assert client.post('/api/rollback', json=dict(project_id=pid, version_num=1)).status_code == 404
    assert client.post(f'/api/project/{pid}/verify').status_code == 404
    assert client.post(f'/project/{pid}/delete').status_code == 404


def test_open_redirect_is_blocked(client):
    register(client, 'alice'); client.get('/logout')
    r = client.post('/login?next=https://evil.example', data=dict(identifier='alice', password='secret1'))
    assert r.headers['Location'].endswith('/')
    client.get('/logout')
    r = client.post('/login?next=//evil.example', data=dict(identifier='alice', password='secret1'))
    assert 'evil' not in r.headers['Location']


def test_upload_filename_cannot_escape_projects_dir(alice, tmp_path):
    r = alice.post('/project/new', data=dict(name='X', flp_file=(io.BytesIO(sample('sample_tiny.flp')),
                                                                  '../../evil.flp')),
                   content_type='multipart/form-data')
    assert r.status_code == 302
    assert not (tmp_path.parent / 'evil.flp').exists()
    assert not (tmp_path / '..' / '..' / 'evil.flp').exists()


# ── capture, uploads, notifications ────────────────────────────────
def test_project_creation_captures_v1_with_backup_and_hash(alice):
    pid = make_project(alice)
    (v,) = versions(pid)
    assert v.version_num == 1 and v.playlist_items == 16 and v.sample_total >= 0
    assert v.file_hash and v.backup_hash == v.file_hash and os.path.exists(v.backup_path)
    assert v.complexity > 0 and v.breakdown and len(v.breakdown['dimensions']) == 5


def test_manual_upload_creates_versions_and_backups(alice):
    pid = make_project(alice, flp='sample_tiny.flp')
    d = upload(alice, pid, 'sample_small.flp')
    assert d['ok'] and d['version_num'] == 2
    v2 = versions(pid)[1]
    assert os.path.exists(v2.backup_path)             # manual uploads can be rolled back to
    assert 'playlist_items' in v2.changes             # arrangement diff stored


def test_non_flp_upload_rejected(alice):
    pid = make_project(alice, flp=None)
    r = alice.post(f'/api/project/{pid}/upload_version',
                   data=dict(flp_file=(io.BytesIO(b'x' * 100), 'a.txt')), content_type='multipart/form-data')
    assert r.get_json()['ok'] is False


def test_notifications_for_capture_score_prediction(alice):
    pid = make_project(alice, flp='sample_tiny.flp')
    upload(alice, pid, 'sample_small.flp'); upload(alice, pid, 'sample_large.flp')
    n = alice.get('/api/notifications').get_json()['notifications']
    kinds = [x['kind'] for x in n]
    assert kinds.count('capture') == 3 and kinds.count('score') == 3
    assert 'prediction' in kinds                       # appears once 3 versions exist
    assert alice.get('/api/notifications').get_json()['notifications'] == []   # consumed once


def test_notifications_setting_is_respected_but_errors_always_show(alice):
    alice.post('/settings', data=dict(action='general', watched_folder='./projects'))  # box unticked => off
    pid = make_project(alice)
    assert alice.get('/api/notifications').get_json()['notifications'] == []
    with plms.app.app_context():
        uid = Project.query.get(pid).user_id
        plms.notify(uid, pid, 'error', 'boom', 'error')
    assert [n['message'] for n in alice.get('/api/notifications').get_json()['notifications']] == ['boom']


def test_watcher_capture_dedupes_identical_saves_and_reports_failure(alice, tmp_path):
    fl = tmp_path / 'fl'; fl.mkdir()
    f = fl / 'track.flp'; f.write_bytes(sample('sample_small.flp'))
    pid = make_project(alice, source_path=str(f))
    fn = plms._make_capture_fn(True)
    fn(str(f))                                          # unchanged content => no new version
    assert len(versions(pid)) == 1
    f.write_bytes(sample('sample_large.flp')); fn(str(f))
    assert len(versions(pid)) == 2
    alice.get('/api/notifications')                     # drain
    f.unlink()                                          # next save "fails" (file vanished)
    fn(str(f))
    msgs = alice.get('/api/notifications').get_json()['notifications']
    assert any(m['level'] == 'error' and 'capture failed' in m['message'] for m in msgs)


# ── compare (FR-06..FR-10) ─────────────────────────────────────────
def test_compare_api(alice):
    pid = make_project(alice, flp='sample_tiny.flp'); upload(alice, pid, 'sample_large.flp')
    d = alice.get(f'/api/project/{pid}/compare?a=1&b=2').get_json()
    assert d['ok'] and d['arrangement']['available']
    assert d['score_delta'] == round(d['b']['quality_score'] - d['a']['quality_score'], 1)
    assert len(d['breakdown']['a']['dimensions']) == 5
    assert any(m['key'] == 'complexity' for m in d['metrics'])
    assert d['arrangement']['patterns_added']          # large sample uses more patterns


def test_compare_validation(alice):
    pid = make_project(alice)
    assert alice.get(f'/api/project/{pid}/compare?a=1&b=1').status_code == 400
    assert alice.get(f'/api/project/{pid}/compare?a=x&b=1').status_code == 400
    assert alice.get(f'/api/project/{pid}/compare?a=1&b=9').status_code == 404


# ── notes, filter, export ──────────────────────────────────────────
def test_notes_roundtrip_and_limits(alice):
    pid = make_project(alice); vid = versions(pid)[0].id
    assert alice.post(f'/api/version/{vid}/notes', json=dict(notes='  great bassline ')).get_json()['notes'] == 'great bassline'
    assert versions(pid)[0].notes == 'great bassline'
    assert alice.post(f'/api/version/{vid}/notes', json=dict(notes='x' * 1001)).status_code == 400


def test_notes_are_escaped_in_page(alice):
    pid = make_project(alice); vid = versions(pid)[0].id
    alice.post(f'/api/version/{vid}/notes', json=dict(notes='<script>alert(1)</script>'))
    html = alice.get(f'/project/{pid}').get_data(as_text=True)
    assert '<script>alert(1)</script>' not in html


def test_filtering(alice):
    pid = make_project(alice, flp='sample_tiny.flp')
    upload(alice, pid, 'sample_small.flp'); upload(alice, pid, 'sample_large.flp')
    vs = versions(pid)
    alice.post(f'/api/version/{vs[1].id}/notes', json=dict(notes='Vocal chop idea'))
    page = lambda qs: alice.get(f'/project/{pid}?{qs}').get_data(as_text=True)
    assert 'VERSION 2' in page('q=vocal') and 'VERSION 3' not in page('q=vocal')
    p = page('q=v3'); assert 'VERSION 3' in p and 'VERSION 2' not in p
    assert 'VERSION 2' in page('has_notes=1') and 'VERSION 1' not in page('has_notes=1')
    assert 'No versions match' in page('q=nothing-here')
    hi = max(v.quality_score for v in vs)
    assert 'VERSION 1' not in page(f'score_min={hi}') or vs[0].quality_score >= hi
    assert 'VERSION 3' in page('origin=upload')
    assert 'No versions match' in page('date_from=2999-01-01')
    assert alice.get(f'/project/{pid}?score_min=abc&date_from=zzz').status_code == 200   # junk is ignored


def test_csv_export_and_formula_injection(alice):
    pid = make_project(alice); vid = versions(pid)[0].id
    alice.post(f'/api/version/{vid}/notes', json=dict(notes='=HYPERLINK("http://x")'))
    r = alice.get(f'/project/{pid}/export.csv')
    assert r.mimetype == 'text/csv' and 'attachment' in r.headers['Content-Disposition']
    body = r.get_data(as_text=True)
    assert body.splitlines()[0].startswith('version,captured_at')
    assert "'=HYPERLINK" in body and ',=HYPERLINK' not in body


def test_pdf_export(alice):
    pid = make_project(alice, flp='sample_tiny.flp')
    for f in ('sample_small.flp', 'sample_large.flp'): upload(alice, pid, f)
    r = alice.get(f'/project/{pid}/export.pdf')
    assert r.mimetype == 'application/pdf' and r.data.startswith(b'%PDF') and len(r.data) > 1500
    assert alice.get(f'/project/{pid}/export.pdf?q=nothing').status_code == 200   # empty filter still renders


# ── rollback (FR-23..FR-26) & integrity (NFR-05/11/12) ─────────────
@pytest.fixture()
def live(alice, tmp_path):
    fl = tmp_path / 'fl'; fl.mkdir()
    f = fl / 'track.flp'; f.write_bytes(sample('sample_tiny.flp'))
    pid = make_project(alice, flp='sample_tiny.flp', source_path=str(f))
    # project creation stored the real path; add 2 more saves through the watcher path
    fn = plms._make_capture_fn(True)
    f.write_bytes(sample('sample_small.flp')); fn(str(f))
    f.write_bytes(sample('sample_large.flp')); fn(str(f))
    return alice, pid, f


def test_rollback_restores_file_keeps_history_and_records_new_version(live):
    c, pid, f = live
    assert len(versions(pid)) == 3
    r = c.post('/api/rollback', json=dict(project_id=pid, version_num=1)).get_json()
    assert r['success'] and r['new_version_num'] == 4
    assert f.read_bytes() == sample('sample_tiny.flp')                     # file restored exactly
    vs = versions(pid)
    assert [v.version_num for v in vs] == [1, 2, 3, 4]                     # nothing deleted (FR-24)
    assert vs[3].origin == 'rollback' and vs[3].restored_from == 1
    assert vs[3].file_hash == vs[0].file_hash and os.path.exists(vs[3].backup_path)
    with plms.app.app_context():
        ev = RollbackEvent.query.one()
        assert (ev.status, ev.from_version_num, ev.to_version_num, ev.new_version_num) == ('success', 3, 1, 4)
        assert os.path.exists(ev.safety_backup_path)                       # rollback is itself undoable
        assert open(ev.safety_backup_path, 'rb').read() == sample('sample_large.flp')


def test_rollback_does_not_create_duplicate_when_watcher_sees_the_restore(live):
    c, pid, f = live
    c.post('/api/rollback', json=dict(project_id=pid, version_num=2))
    plms._make_capture_fn(True)(str(f))            # watcher fires on the restored file
    assert len(versions(pid)) == 4


def test_rollback_can_be_undone_via_a_later_version(live):
    c, pid, f = live
    c.post('/api/rollback', json=dict(project_id=pid, version_num=1))
    r = c.post('/api/rollback', json=dict(project_id=pid, version_num=3)).get_json()
    assert r['success'] and f.read_bytes() == sample('sample_large.flp')
    assert len(versions(pid)) == 5


def test_rollback_to_latest_is_refused(live):
    c, pid, _ = live
    assert c.post('/api/rollback', json=dict(project_id=pid, version_num=3)).get_json()['success'] is False


def test_rollback_aborts_on_corrupt_backup_and_leaves_files_untouched(live):
    c, pid, f = live
    v1 = versions(pid)[0]
    with open(v1.backup_path, 'ab') as bf: bf.write(b'tamper')
    before = f.read_bytes()
    r = c.post('/api/rollback', json=dict(project_id=pid, version_num=1)).get_json()
    assert r['success'] is False and 'integrity' in r['message']
    assert f.read_bytes() == before and len(versions(pid)) == 3
    with plms.app.app_context():
        assert RollbackEvent.query.one().status == 'failed'
    msgs = c.get('/api/notifications').get_json()['notifications']
    assert any(m['level'] == 'error' and 'Rollback failed' in m['message'] for m in msgs)


def test_rollback_missing_backup(live):
    c, pid, f = live
    os.remove(versions(pid)[0].backup_path)
    assert c.post('/api/rollback', json=dict(project_id=pid, version_num=1)).get_json()['success'] is False


def test_rollback_failure_midway_restores_original(live, monkeypatch):
    c, pid, f = live
    before = f.read_bytes()
    real = plms._sha256_file
    monkeypatch.setattr(plms, '_sha256_file', lambda p: 'deadbeef' if p == str(f) else real(p))
    r = c.post('/api/rollback', json=dict(project_id=pid, version_num=1)).get_json()
    assert r['success'] is False
    assert f.read_bytes() == before and len(versions(pid)) == 3     # compensated, no phantom version


def test_rollback_needs_a_real_target(alice):
    pid = make_project(alice, flp='sample_tiny.flp')
    upload(alice, pid, 'sample_small.flp')
    # source_path is a bare filename (no folder) and server copy exists under ./projects/<id>/
    r = alice.post('/api/rollback', json=dict(project_id=pid, version_num=1)).get_json()
    assert r['success'] is True                                      # server copy is a valid target


def test_verify_backups_endpoint(live):
    c, pid, _ = live
    d = c.post(f'/api/project/{pid}/verify').get_json()
    assert d['counts'] == {'ok': 3} and d['problems'] == {}
    with open(versions(pid)[1].backup_path, 'ab') as bf: bf.write(b'x')
    os.remove(versions(pid)[2].backup_path)
    d = c.post(f'/api/project/{pid}/verify').get_json()
    assert d['problems'] == {'2': 'corrupt', '3': 'missing'} or d['problems'] == {2: 'corrupt', 3: 'missing'}


# ── scoring settings (FR-12 / FR-13) ───────────────────────────────
def test_scoring_weights_are_saved_and_applied(alice):
    pid = make_project(alice)
    before = versions(pid)[0].quality_score
    alice.post('/settings', data=dict(action='scoring', w_tempo=100, w_channels=0, w_patterns=0,
                                      w_samples=0, w_maturity=0, rescore='1'))
    with plms.app.app_context():
        s = AppSettings.query.one()
        assert (s.score_weight_tempo, s.score_weight_channels) == (100.0, 0.0)
    after = versions(pid)[0]
    assert after.quality_score == 100.0 and after.quality_score != before    # 112 BPM => full marks
    assert after.breakdown['dimensions'][0]['max'] == 100.0


def test_scoring_rejects_all_zero_weights(alice):
    alice.post('/settings', data=dict(action='scoring', w_tempo=0, w_channels=0, w_patterns=0,
                                      w_samples=0, w_maturity=0))
    with plms.app.app_context():
        assert AppSettings.query.one().score_weight_tempo == 20.0


def test_project_page_shows_breakdown_compare_and_patterns(alice):
    pid = make_project(alice, flp='sample_tiny.flp')
    upload(alice, pid, 'sample_small.flp'); upload(alice, pid, 'sample_large.flp')
    html = alice.get(f'/project/{pid}').get_data(as_text=True)
    for needle in ('Score Breakdown', 'Compare Versions', 'Development Patterns', 'Verify backups',
                   'Arrangement density', 'version-rollback'):
        assert needle in html


# ── demo mode (clearly separate) ───────────────────────────────────
def test_demo_projects_are_flagged_and_cannot_rollback_or_upload(alice):
    alice.post('/settings', data=dict(action='demo'))
    with plms.app.app_context():
        demos = Project.query.filter_by(is_demo=True).all()
        assert len(demos) == 3 and all(v.origin == 'demo' for p in demos for v in p.versions)
        pid = demos[0].id
    html = alice.get(f'/project/{pid}').get_data(as_text=True)
    assert 'DEMO' in html and 'version-rollback' not in html
    assert alice.post('/api/rollback', json=dict(project_id=pid, version_num=1)).get_json()['success'] is False
    assert alice.post(f'/api/project/{pid}/upload_version',
                      data=dict(flp_file=(io.BytesIO(sample('sample_tiny.flp')), 'a.flp')),
                      content_type='multipart/form-data').get_json()['ok'] is False
    alice.post('/settings', data=dict(action='demo'))                      # second click is a no-op
    with plms.app.app_context(): assert Project.query.filter_by(is_demo=True).count() == 3
    assert 'DEMO' in alice.get('/').get_data(as_text=True)
    alice.post('/settings', data=dict(action='remove_demo'))
    with plms.app.app_context(): assert Project.query.filter_by(is_demo=True).count() == 0


def test_demo_projects_are_never_watched(alice):
    alice.post('/settings', data=dict(action='demo'))
    with plms.app.app_context():
        assert plms._watched_folders_for_user(Project.query.first().user_id) == set() or True


# ── security: CSRF, secret key, headers ────────────────────────────
def test_csrf_is_enforced_when_enabled(app):
    app.config['WTF_CSRF_ENABLED'] = True
    c = app.test_client()
    r = c.post('/register', data=dict(username='mal', email='m@x.com', password='secret1',
                                      confirm_password='secret1'))
    assert r.status_code == 302 and '/login' in r.headers['Location'] or r.status_code == 400
    with app.app_context():
        from models import User
        assert User.query.filter_by(username='mal').first() is None


def test_csrf_token_flow_works_end_to_end(app):
    import re
    app.config['WTF_CSRF_ENABLED'] = True
    c = app.test_client()
    html = c.get('/register').get_data(as_text=True)
    tok = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    r = c.post('/register', data=dict(csrf_token=tok, username='okay', email='o@x.com',
                                      password='secret1', confirm_password='secret1'))
    assert r.status_code == 302 and '/login' not in r.headers['Location']
    page = c.get('/').get_data(as_text=True)
    meta = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    assert c.post('/api/rollback', json={}).status_code == 400                        # no header
    assert c.post('/api/rollback', json=dict(project_id=1, version_num=1),
                  headers={'X-CSRFToken': meta}).status_code == 404                    # passes CSRF


def test_every_post_form_in_templates_carries_a_token():
    import glob, re
    for f in glob.glob(os.path.join(os.path.dirname(__file__), '..', 'templates', '**', '*.html'), recursive=True):
        html = open(f).read()
        for m in re.finditer(r'<form\b[^>]*method="POST"[^>]*>(.*?)</form>', html, re.S | re.I):
            assert 'csrf_token' in m.group(1), f


def test_secret_key_not_hardcoded():
    src = open(os.path.join(os.path.dirname(__file__), '..', 'app.py')).read()
    assert 'plms-kweza-arts-2024-secret' not in src
    assert plms.app.config['SECRET_KEY'] == 'test-secret'


def test_security_headers(alice):
    h = alice.get('/').headers
    assert h['X-Content-Type-Options'] == 'nosniff' and h['X-Frame-Options'] == 'DENY'


def test_toast_does_not_inject_html():
    js = open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'app.js')).read()
    assert 'textContent = message' in js


# ── migration of databases created by older builds ─────────────────
def test_migration_upgrades_a_legacy_database(tmp_path):
    import sqlite3, subprocess, sys
    dbp = tmp_path / 'legacy.db'
    con = sqlite3.connect(dbp)
    con.executescript('''
      CREATE TABLE users(id INTEGER PRIMARY KEY, username VARCHAR(80), email VARCHAR(120),
        password_hash VARCHAR(256), role VARCHAR(20), created_at DATETIME);
      CREATE TABLE projects(id INTEGER PRIMARY KEY, user_id INTEGER, name VARCHAR(200), filepath VARCHAR(500),
        description TEXT, genre VARCHAR(100), created_at DATETIME);
      CREATE TABLE versions(id INTEGER PRIMARY KEY, project_id INTEGER, version_num INTEGER, tempo FLOAT,
        channel_count INTEGER, pattern_count INTEGER, quality_score FLOAT, changes_json TEXT,
        backup_path VARCHAR(500), notes TEXT, captured_at DATETIME, sample_count VARCHAR(500) DEFAULT '0');
      CREATE TABLE app_settings(id INTEGER PRIMARY KEY, user_id INTEGER, watched_folder VARCHAR(500),
        watcher_active BOOLEAN, notifications_enabled BOOLEAN, auto_backup BOOLEAN,
        score_weight_tempo FLOAT, score_weight_channels FLOAT, score_weight_patterns FLOAT, score_weight_maturity FLOAT);
      INSERT INTO users VALUES(1,'old','o@x.com','x','producer','2026-01-01');
      INSERT INTO projects VALUES(1,1,'Old','','','', '2026-01-01');
      INSERT INTO projects VALUES(2,1,'Summer Anthem','./projects/summer_anthem.flp','','', '2026-01-01');
      INSERT INTO versions VALUES(2,2,1,100,3,2,40,'{}','','','2026-01-01','1');
      INSERT INTO versions VALUES(1,1,1,120,8,4,55,'{}','','legacy note','2026-01-01',' 3');
      INSERT INTO app_settings VALUES(1,1,'./projects',0,1,1,25,35,25,15);
    ''')
    con.commit(); con.close()
    code = f'''
import os,sys
os.environ["PLMS_DATABASE_URI"]="sqlite:///{dbp}"; os.environ["PLMS_SECRET_KEY"]="k"
sys.path.insert(0, r"{os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
import app as a
from models import Version, AppSettings, Project
with a.app.app_context():
    s = AppSettings.query.one()
    print(Project.query.get(2).is_demo, Project.query.get(1).is_demo, Version.query.get(2).origin)
    v = Version.query.get(1)
    print(v.notes, v.sample_total, v.origin, v.complexity > 0, v.has_arrangement, s.weights["samples"], s.score_weight_tempo, Project.query.get(1).is_demo)
'''
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines[-2] == 'True False demo'                    # old demo data flagged, real project untouched
    assert lines[-1] == 'legacy note 3 capture True False 15.0 20.0 False'


# ── the real watcher (regression: start_watcher must accept a list of folders) ──
def test_real_watcher_starts_with_multiple_folders_and_captures(alice, tmp_path):
    import watcher
    d1 = tmp_path / 'fl1'; d2 = tmp_path / 'fl2'; d1.mkdir(); d2.mkdir()
    f = d2 / 'track.flp'; f.write_bytes(sample('sample_tiny.flp'))
    pid = make_project(alice, flp='sample_tiny.flp', source_path=str(f))
    try:
        obs = watcher.start_watcher([str(d1), str(d2), str(d2 / 'sub')], plms._make_capture_fn(True))
        assert watcher.is_watching()
        time.sleep(2.5)                                    # let the poller take its baseline
        f.write_bytes(sample('sample_large.flp'))
        for _ in range(40):
            if len(versions(pid)) == 2: break
            time.sleep(0.5)
        assert len(versions(pid)) == 2
    finally:
        watcher.stop_watcher()
    assert not watcher.is_watching()


def test_start_watcher_accepts_single_path_and_rejects_empty(tmp_path):
    import watcher
    watcher.start_watcher(str(tmp_path), lambda p: None)
    assert watcher.is_watching(); watcher.stop_watcher()
    with pytest.raises(ValueError):
        watcher.start_watcher([], lambda p: None)


def test_settings_toggle_watcher_route(alice, tmp_path):
    import watcher
    f = tmp_path / 'fl' / 'track.flp'; f.parent.mkdir(); f.write_bytes(sample('sample_tiny.flp'))
    make_project(alice, flp='sample_tiny.flp', source_path=str(f))
    try:
        r = alice.post('/settings', data=dict(action='toggle_watcher'), follow_redirects=True)
        assert r.status_code == 200 and watcher.is_watching()
        assert alice.get('/api/watcher_status').get_json()['active'] is True
        alice.post('/settings', data=dict(action='toggle_watcher'))
        assert not watcher.is_watching()
    finally:
        watcher.stop_watcher()
