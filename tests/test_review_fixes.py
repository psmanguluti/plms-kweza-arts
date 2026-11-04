"""Tests for the issues raised in 'PLMS Prototype v1.0 – Review Feedback' (R2, R6-R12, issues 1-9)."""
import io, os, csv, threading, time
import pytest
from conftest import sample, make_project, upload, plms
from models import Version
import watcher


def versions(pid):
    with plms.app.app_context():
        return Version.query.filter_by(project_id=pid).order_by(Version.version_num).all()


def bad_upload(client, pid, blob=b'this is not an flp file at all'):
    r = client.post(f'/api/project/{pid}/upload_version',
                    data=dict(flp_file=(io.BytesIO(blob), 'broken.flp')),
                    content_type='multipart/form-data')
    return r.get_json()


# ── Issue 2 / R9: unavailable instead of invented values ──────────────────
def test_unparseable_file_is_captured_with_unavailable_properties(alice):
    pid = make_project(alice, flp='sample_small.flp')
    d = bad_upload(alice, pid)
    assert d['ok'] and d['available'] is False and d['tempo'] is None and d['channel_count'] is None
    v = versions(pid)[-1]
    assert v.version_num == 2 and v.file_hash and os.path.exists(v.backup_path)    # still captured
    assert (v.tempo, v.channel_count, v.pattern_count, v.plugins) == (None, None, None, None)
    assert v.quality_score is None


def test_unavailable_shows_in_page_comparison_and_exports_not_120_bpm(alice):
    pid = make_project(alice, flp='sample_small.flp'); bad_upload(alice, pid)
    html = alice.get(f'/project/{pid}').get_data(as_text=True)
    assert 'unavailable' in html and '120 BPM' not in html
    d = alice.get(f'/api/project/{pid}/compare?a=1&b=2').get_json()
    assert d['changed_count'] == 0 and 'unavailable' in d['summary']
    assert all(m['direction'] == 'unavailable' for m in d['metrics'])
    rows = list(csv.reader(io.StringIO(alice.get(f'/project/{pid}/export.csv').get_data(as_text=True))))
    assert rows[2][rows[0].index('tempo_bpm')] == 'unavailable'
    assert rows[2][rows[0].index('channels')] == 'unavailable'
    assert alice.get(f'/project/{pid}/export.pdf').status_code == 200


def test_unavailable_version_leaves_a_gap_in_chart_data(alice):
    import re, json
    pid = make_project(alice, flp='sample_small.flp'); bad_upload(alice, pid)
    html = alice.get(f'/project/{pid}').get_data(as_text=True)
    data = json.loads(re.search(r'id="chart-data"[^>]*>(.*?)</script>', html, re.S).group(1))
    assert data['tempos'] == [112.0, None] and data['channels'][1] is None


def test_comparison_after_unavailable_version_does_not_invent_a_change(alice):
    pid = make_project(alice, flp='sample_small.flp')
    bad_upload(alice, pid); upload(alice, pid, 'sample_small.flp')
    d = alice.get(f'/api/project/{pid}/compare?a=1&b=3').get_json()
    assert d['identical'] and d['changed_count'] == 0           # v1 and v3 are the same file


# ── R8 / R10: plugins stored and compared ─────────────────────────────────
def test_plugins_are_stored_per_version_and_compared(alice):
    pid = make_project(alice, flp='sample_small.flp'); upload(alice, pid, 'sample_large.flp')
    v1, v2 = versions(pid)
    assert 'FLEX' in v1.plugins and 'GMS' in v2.plugins
    d = alice.get(f'/api/project/{pid}/compare?a=1&b=2').get_json()
    assert 'GMS' in d['plugins']['added'] and 'FLEX' in d['plugins']['removed']
    html = alice.get(f'/project/{pid}').get_data(as_text=True)
    assert 'Plugin added' in html and 'Plugin removed' in html


# ── R11: indicators with visible formulas ─────────────────────────────────
def test_project_page_shows_four_indicators_with_formulas(alice):
    pid = make_project(alice, flp='sample_tiny.flp'); upload(alice, pid, 'sample_large.flp')
    html = alice.get(f'/project/{pid}').get_data(as_text=True)
    assert html.count('class="ind"') == 4 and html.count('Formula:') == 4
    assert '÷' in html and 'Worked:' in html


# ── R12 / Issue 6: dedicated compare and rollback screens ─────────────────
def test_compare_screen_has_pickers_and_runs_end_to_end(alice):
    pid = make_project(alice, flp='sample_tiny.flp'); upload(alice, pid, 'sample_large.flp')
    r = alice.get(f'/project/{pid}/compare?a=1&b=2')
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'id="cmp-a"' in html and 'id="cmp-b"' in html and 'Compare versions' in html
    assert alice.get(f'/api/project/{pid}/compare?a=1&b=2').get_json()['ok']


def test_comparing_identical_files_shows_no_differences_and_changes_nothing(alice):
    pid = make_project(alice, flp='sample_small.flp'); upload(alice, pid, 'sample_large.flp')
    upload(alice, pid, 'sample_small.flp')
    before = [(v.file_hash, v.changes_json, v.notes) for v in versions(pid)]
    d = alice.get(f'/api/project/{pid}/compare?a=1&b=3').get_json()
    assert d['identical'] and d['changed_count'] == 0 and 'no differences' in d['summary']
    assert [(v.file_hash, v.changes_json, v.notes) for v in versions(pid)] == before


def test_compare_and_rollback_screens_are_owner_only(client):
    from conftest import register
    register(client, 'alice'); pid = make_project(client); client.get('/logout')
    register(client, 'bob')
    assert client.get(f'/project/{pid}/compare').status_code == 404
    assert client.get(f'/project/{pid}/rollback').status_code == 404


# ── Section 6: scoring/prediction hidden by default, switchable ───────────
def test_scoring_hidden_everywhere_by_default(alice):
    pid = make_project(alice, flp='sample_tiny.flp')
    upload(alice, pid, 'sample_small.flp'); upload(alice, pid, 'sample_large.flp')
    dash = alice.get('/').get_data(as_text=True)
    for word in ('Avg Quality Score', 'Declining', 'Improving'):
        assert word not in dash
    csv_text = alice.get(f'/project/{pid}/export.csv').get_data(as_text=True)
    assert 'quality_score' not in csv_text.splitlines()[0]
    settings = alice.get('/settings').get_data(as_text=True)
    assert 'Scoring Weights' not in settings and 'Experimental' in settings


def test_experimental_switch_turns_scoring_on_and_off(alice):
    pid = make_project(alice, flp='sample_tiny.flp'); upload(alice, pid, 'sample_large.flp')
    alice.post('/settings', data=dict(action='experimental', show_experimental='1'))
    assert 'Avg Quality Score' in alice.get('/').get_data(as_text=True)
    assert 'quality_score' in alice.get(f'/project/{pid}/export.csv').get_data(as_text=True).splitlines()[0]
    alice.post('/settings', data=dict(action='experimental'))
    assert 'Avg Quality Score' not in alice.get('/').get_data(as_text=True)


# ── Issue 9: path validation ─────────────────────────────────────────────
@pytest.mark.parametrize('path', ['/no/such/file.flp', 'song.mp3', '/tmp'])
def test_create_project_rejects_bad_paths(alice, path):
    r = alice.post('/project/new', data=dict(name='X', source_path=path),
                   content_type='multipart/form-data')
    assert r.status_code == 302 and r.headers['Location'].endswith('/')
    with plms.app.app_context():
        assert plms.Project.query.count() == 0


def test_create_project_accepts_an_existing_flp_path(alice, tmp_path):
    f = tmp_path / 'ok.flp'; f.write_bytes(sample('sample_small.flp'))
    pid = make_project(alice, flp=None, source_path=str(f))
    with plms.app.app_context():
        assert plms.Project.query.get(pid).source_path == str(f)


# ── Issue 8: debug off ───────────────────────────────────────────────────
def test_debug_is_off_unless_explicitly_requested():
    src = open(os.path.join(os.path.dirname(plms.__file__), 'app.py')).read()
    assert 'debug=True' not in src and "PLMS_DEBUG" in src


# ── Issue 3 / R2: capture only after the file has stopped changing ───────
def test_wait_until_stable_returns_only_after_size_and_mtime_stop_changing(tmp_path):
    f = tmp_path / 'slow.flp'; f.write_bytes(b'a')
    def writer():
        for _ in range(6):
            time.sleep(0.15); open(f, 'ab').write(b'x' * 50)
    t = threading.Thread(target=writer); t.start()
    ok = watcher.wait_until_stable(str(f), interval=0.1, checks=4, timeout=10)
    size_when_stable = f.stat().st_size
    t.join()
    assert ok and size_when_stable == 1 + 6 * 50 == f.stat().st_size      # all writes finished first


def test_wait_until_stable_never_accepts_missing_or_empty_files(tmp_path):
    assert not watcher.wait_until_stable(str(tmp_path / 'none.flp'), interval=0.02, checks=3, timeout=0.2)
    e = tmp_path / 'empty.flp'; e.write_bytes(b'')
    assert not watcher.wait_until_stable(str(e), interval=0.02, checks=3, timeout=0.2)


def test_wait_until_stable_times_out_on_a_file_that_keeps_changing(tmp_path):
    f = tmp_path / 'busy.flp'; f.write_bytes(b'a')
    stop = threading.Event()
    def writer():
        while not stop.is_set():
            open(f, 'ab').write(b'x'); time.sleep(0.02)
    t = threading.Thread(target=writer); t.start()
    try:
        assert watcher.wait_until_stable(str(f), interval=0.05, checks=3, timeout=0.6) is False
    finally:
        stop.set(); t.join()


def test_handler_captures_a_staged_write_once_and_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, 'STABLE_INTERVAL', 0.1); monkeypatch.setattr(watcher, 'STABLE_CHECKS', 3)
    f = tmp_path / 'song.flp'; full = sample('sample_large.flp')
    f.write_bytes(full[:100])
    captured = []
    h = watcher.FLPHandler(lambda p: captured.append(open(p, 'rb').read()))
    def writer():                                  # FL Studio-style staged save
        step = max(len(full) // 8, 1)
        for i in range(100, len(full), step):
            time.sleep(0.15); open(f, 'ab').write(full[i:i + step])
    t = threading.Thread(target=writer); t.start()
    h._handle(str(f))                              # first event fires mid-write
    for _ in range(5):                             # more events for the same finished file
        h._handle(str(f))
    t.join()
    assert len(captured) == 1 and captured[0] == full             # one capture, never partial
