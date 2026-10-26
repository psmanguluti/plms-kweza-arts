import os, io, csv, json, shutil, hashlib, logging, threading, time, secrets, re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, flash, Response)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)
from flask_wtf.csrf import CSRFProtect, CSRFError
from sqlalchemy import inspect, text
from werkzeug.utils import secure_filename

from models  import (db, User, Project, Version, AppSettings,
                     RollbackEvent, Notification)
from engines import (ChangeEngine, ScoringEngine, PredictionEngine,
                     PatternAnalyzer, structural_complexity)
from flp_parser import parse_flp_bytes, parse_flp
from watcher import start_watcher, stop_watcher, is_watching

logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] %(levelname)s: %(message)s')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI']        = os.environ.get('PLMS_DATABASE_URI', 'sqlite:///plms.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH']             = 100 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY']        = True
app.config['SESSION_COOKIE_SAMESITE']        = 'Lax'
app.config['REMEMBER_COOKIE_HTTPONLY']       = True


def _secret_key() -> str:
    """NFR-06: no hard-coded secret. Env var wins, else a random key persisted in instance/."""
    key = os.environ.get('PLMS_SECRET_KEY')
    if key:
        return key
    p = Path(app.instance_path) / 'secret_key'
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        return p.read_text().strip()
    key = secrets.token_hex(32)
    p.write_text(key)
    try: os.chmod(p, 0o600)
    except OSError: pass
    return key


app.config['SECRET_KEY'] = _secret_key()
csrf = CSRFProtect(app)
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view    = 'auth_login'
login_manager.login_message = ''

@login_manager.user_loader
def load_user(uid): return db.session.get(User, int(uid))


# ── Schema migration (SQLAlchemy 2 compatible) ─────────────────
NEW_COLUMNS = {
    'projects': [('source_path', 'VARCHAR(500)', "''"), ('is_demo', 'BOOLEAN', '0')],
    'versions': [('sample_count', 'INTEGER', '0'), ('playlist_items', 'INTEGER', '0'),
                 ('tracks_used', 'INTEGER', '0'), ('arrangement_bars', 'FLOAT', '0'),
                 ('complexity', 'FLOAT', '0'), ('arrangement_json', 'TEXT', "''"),
                 ('breakdown_json', 'TEXT', "''"), ('file_hash', 'VARCHAR(64)', "''"),
                 ('backup_hash', 'VARCHAR(64)', "''"), ('origin', 'VARCHAR(20)', "'capture'"),
                 ('restored_from', 'INTEGER', 'NULL')],
    'app_settings': [('score_weight_samples', 'FLOAT', '15')],
}


def _migrate():
    db.create_all()
    insp = inspect(db.engine)
    with db.engine.begin() as conn:
        for table, cols in NEW_COLUMNS.items():
            have = {c['name'] for c in insp.get_columns(table)}
            for name, typ, default in cols:
                if name not in have:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {typ} DEFAULT {default}'))
                    logging.info(f'[DB] Migrated: {table}.{name} added.')
        # Old builds stored weights (25/35/25/15) that were never applied. Reset that
        # untouched default so scores stay what users already see.
        conn.execute(text(
            'UPDATE app_settings SET score_weight_tempo=20, score_weight_channels=25, '
            'score_weight_patterns=25, score_weight_maturity=15, score_weight_samples=15 '
            'WHERE score_weight_tempo=25 AND score_weight_channels=35 '
            'AND score_weight_patterns=25 AND score_weight_maturity=15'))
    # Older builds loaded demo data as ordinary projects. Flag them (conservatively:
    # the three demo names, no real file path, and no backup on any version).
    demo_names = ('Summer Anthem', 'Midnight Groove', 'Afro Fusion Vol.1')
    for p in Project.query.filter(Project.name.in_(demo_names), Project.is_demo == False).all():  # noqa: E712
        if not (p.source_path or '').strip() and not any(v.backup_path for v in p.versions):
            p.is_demo = True
            for v in p.versions:
                v.origin = 'demo'
    # back-fill complexity for versions captured before it existed
    for v in Version.query.filter((Version.complexity == None) | (Version.complexity == 0)).all():  # noqa: E711
        v.complexity = structural_complexity(v.metrics())
    db.session.commit()


with app.app_context():
    _migrate()


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    return resp


@app.errorhandler(CSRFError)
def _csrf_error(e):
    if request.path.startswith('/api/'):
        return jsonify(ok=False, success=False, error='Session expired — reload the page.',
                       message='Session expired — reload the page.'), 400
    flash('Your session expired. Please try again.', 'error')
    return redirect(request.referrer or url_for('auth_login'))


# ── Helpers ────────────────────────────────────────────────────
def _backup_root() -> str:
    """Backups live outside FL Studio's folders so the watcher never sees them."""
    root = os.environ.get('PLMS_BACKUP_ROOT') or os.path.join(Path.home(), 'Documents', 'PLMS_Backups')
    os.makedirs(root, exist_ok=True)
    return root


def _safe_name(s: str) -> str:
    return ''.join(c if c.isalnum() or c in ' _-' else '_' for c in s).strip() or 'project'


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _norm(path: str) -> str:
    return (path or '').lower().replace('\\', '/')


def _safe_next(target):
    if not target:
        return None
    u = urlparse(target)
    if u.netloc or u.scheme or not target.startswith('/') or target.startswith('//'):
        return None
    return target


# kept for backwards compatibility with earlier imports / tests
_parse_flp_bytes = parse_flp_bytes
_parse_flp       = parse_flp


def _user_settings(user_id) -> AppSettings:
    s = AppSettings.query.filter_by(user_id=user_id).first()
    if not s:
        s = AppSettings(user_id=user_id)
        db.session.add(s); db.session.commit()
    return s


def _scorer_for(user_id) -> ScoringEngine:
    return ScoringEngine(_user_settings(user_id).weights)


def _owned_project(project_id) -> Project:
    return Project.query.filter_by(id=project_id, user_id=current_user.id).first_or_404()


def _latest_version(project_id):
    return (Version.query.filter_by(project_id=project_id)
            .order_by(Version.version_num.desc()).first())


# ── Notifications (FR-27 … FR-31) ──────────────────────────────
def notify(user_id, project_id, kind, message, level='success'):
    """Errors always notify; other levels respect the user's notification setting."""
    try:
        s = AppSettings.query.filter_by(user_id=user_id).first()
        if level != 'error' and s is not None and not s.notifications_enabled:
            return
        db.session.add(Notification(user_id=user_id, project_id=project_id, kind=kind,
                                    level=level, message=message[:300]))
        db.session.commit()
    except Exception as e:                       # never let notifications break a capture
        db.session.rollback()
        logging.warning(f'[Notify] {e}')


def _announce_capture(proj, v):
    label = {'upload': 'uploaded', 'rollback': 'restored'}.get(v.origin, 'captured')
    notify(proj.user_id, proj.id, 'capture', f'"{proj.name}": v{v.version_num} {label}.')
    notify(proj.user_id, proj.id, 'score',
           f'"{proj.name}": quality score for v{v.version_num} is {v.quality_score:.1f}.', 'info')
    versions = Version.query.filter_by(project_id=proj.id).order_by(Version.version_num).all()
    if len(versions) >= PredictionEngine.MIN:
        p = PredictionEngine().predict(versions, 'quality_score')
        if p.get('available'):
            notify(proj.user_id, proj.id, 'prediction',
                   f'"{proj.name}": prediction ready — next score ≈ {p["predicted_value"]:.0f} '
                   f'({p["trend"]}).', 'info')


# ── Version creation ───────────────────────────────────────────
_capture_lock    = threading.RLock()     # watcher thread + request threads (NFR-13)
_recent_restores = {}                    # normalised path -> (sha256, timestamp)


def _build_version(project, data, file_hash='', origin='capture', restored_from=None, notes=''):
    prev  = _latest_version(project.id)
    vnum  = (prev.version_num + 1) if prev else 1
    scorer = _scorer_for(project.user_id)

    arr = data.get('arrangement') or {}
    has_arr = bool(data.get('playlist_items'))
    vdata = dict(tempo=float(data.get('tempo', 120.0)),
                 channel_count=int(data.get('channel_count', 0)),
                 pattern_count=int(data.get('pattern_count', 1)),
                 sample_count=int(data.get('sample_count', 0) or 0),
                 playlist_items=int(data.get('playlist_items', 0) or 0),
                 tracks_used=int(data.get('tracks_used', 0) or 0),
                 arrangement_bars=float(data.get('arrangement_bars', 0.0) or 0.0),
                 version_num=vnum)
    bd = scorer.breakdown(vdata)
    changes = {}
    if prev:
        changes = ChangeEngine().compute(prev.metrics(), {**vdata, 'has_arrangement': has_arr,
                                                          'arrangement': arr})
    v = Version(project_id=project.id, version_num=vnum,
                tempo=vdata['tempo'], channel_count=vdata['channel_count'],
                pattern_count=vdata['pattern_count'], sample_count=vdata['sample_count'],
                playlist_items=vdata['playlist_items'], tracks_used=vdata['tracks_used'],
                arrangement_bars=vdata['arrangement_bars'],
                complexity=structural_complexity(vdata),
                arrangement_json=json.dumps(arr) if (has_arr and arr) else '',
                quality_score=bd['total'], breakdown_json=json.dumps(bd),
                changes_json=json.dumps(changes), file_hash=file_hash,
                origin=origin, restored_from=restored_from, notes=notes)
    db.session.add(v)
    db.session.commit()
    return v


def _store_backup(proj, v, raw: bytes):
    """Write the captured bytes to the backup folder and record its SHA-256."""
    bdir = os.path.join(_backup_root(), f'{proj.id}_{_safe_name(proj.name)}')
    os.makedirs(bdir, exist_ok=True)
    bp = os.path.join(bdir, f'v{v.version_num:03d}.flp')
    with open(bp, 'wb') as f:
        f.write(raw)
    v.backup_path = bp
    v.backup_hash = _sha256_bytes(raw)
    db.session.commit()


def _capture_version(proj, raw: bytes, origin='capture', do_backup=True, dedupe=False):
    """Single capture pipeline used by the watcher, uploads and project creation."""
    with _capture_lock:
        h = _sha256_bytes(raw)
        latest = _latest_version(proj.id)
        if dedupe and latest is not None and latest.file_hash == h:
            return None                                   # FL saved identical content
        v = _build_version(proj, parse_flp_bytes(raw), file_hash=h, origin=origin)
        if do_backup:
            _store_backup(proj, v, raw)
    _announce_capture(proj, v)
    return v


def _verify_backup(v) -> str:
    """'ok' | 'corrupt' | 'missing' | 'no_backup' | 'unverified' (legacy, no stored hash)."""
    if not v.backup_path:
        return 'no_backup'
    if not os.path.exists(v.backup_path):
        return 'missing'
    expected = v.backup_hash or v.file_hash
    if not expected:
        return 'unverified'
    return 'ok' if _sha256_file(v.backup_path) == expected else 'corrupt'


def _read_stable(path, tries=4):
    last = None
    for _ in range(tries):
        try:
            with open(path, 'rb') as f:
                return f.read()
        except OSError as e:
            last = e; time.sleep(0.4)
    raise last


# ── Watcher callback ───────────────────────────────────────────
def _make_capture_fn(do_backup: bool):
    def on_flp_change(filepath):
        with app.app_context():
            fp_norm = _norm(filepath)
            fp_base = os.path.basename(fp_norm)
            proj = Project.query.filter(db.func.lower(Project.source_path) == fp_norm).first()
            if not proj:
                proj = Project.query.filter(db.func.lower(Project.filepath) == fp_norm).first()
            if not proj:
                for p in Project.query.filter_by(is_demo=False).all():
                    if fp_base in (os.path.basename(_norm(p.source_path)),
                                   os.path.basename(_norm(p.filepath))):
                        proj = p; break
            if not proj:
                logging.info(f'[Watcher] No project matched: {filepath}')
                return
            try:
                if _norm(proj.source_path) != fp_norm:
                    proj.source_path = filepath
                    db.session.commit()
                raw = _read_stable(filepath)
                rr = _recent_restores.get(fp_norm)
                if rr and rr[0] == _sha256_bytes(raw) and time.time() - rr[1] < 60:
                    return                                    # echo of our own rollback
                v = _capture_version(proj, raw, 'capture', do_backup=do_backup, dedupe=True)
                if v:
                    logging.info(f'[Watcher] v{v.version_num} for "{proj.name}" '
                                 f'(score {v.quality_score:.1f})')
            except Exception as e:
                db.session.rollback()
                logging.error(f'[Watcher] Capture failed for {filepath}: {e}', exc_info=True)
                notify(proj.user_id, proj.id, 'error',
                       f'Version capture failed for "{proj.name}": {e}', 'error')
    return on_flp_change


def _watched_folders_for_user(user_id):
    folders = set()
    s = AppSettings.query.filter_by(user_id=user_id).first()
    if s and s.watched_folder:
        f = os.path.abspath(s.watched_folder)
        if os.path.isdir(f):
            folders.add(f)
    for p in Project.query.filter_by(user_id=user_id, is_demo=False).all():
        for path in [p.source_path, p.filepath]:
            if path:
                parent = os.path.dirname(os.path.abspath(path))
                if os.path.isdir(parent):
                    folders.add(parent)
    return folders


@app.context_processor
def inject_globals():
    active = False
    if current_user.is_authenticated:
        s = AppSettings.query.filter_by(user_id=current_user.id).first()
        if s and s.watcher_active:
            if is_watching():
                active = True
            else:
                logging.warning('[App] Observer dead — restarting...')
                try:
                    start_watcher(list(_watched_folders_for_user(current_user.id)),
                                  _make_capture_fn(bool(s.auto_backup)))
                    active = True
                except Exception as e:
                    logging.error(f'[App] Restart failed: {e}')
                    s.watcher_active = False
                    db.session.commit()
    return dict(watcher_active=active)


# ── Demo data (clearly flagged, never mixed with real behaviour) ──
def _load_demo(uid) -> bool:
    if Project.query.filter_by(user_id=uid, is_demo=True).count():
        return False
    demos = [
        dict(name='Summer Anthem', genre='Afrobeats', description='Upbeat track with traditional percussion',
             vs=[(102,4,3,2),(102,7,5,3),(104,10,7,5),(104,13,9,6),(105,16,11,7),(105,18,12,8)]),
        dict(name='Midnight Groove', genre='Amapiano', description='Deep bass log drum exploration',
             vs=[(112,3,2,1),(112,6,4,2),(113,9,6,4),(113,12,8,5)]),
        dict(name='Afro Fusion Vol.1', genre='Afro-fusion', description='Traditional meets electronic',
             vs=[(95,5,4,2),(95,9,6,4),(96,14,8,6),(96,18,11,8),(97,20,13,9),(97,22,14,10),(97,24,15,11)]),
    ]
    base = datetime.utcnow() - timedelta(days=10)
    for pd in demos:
        proj = Project(user_id=uid, name=pd['name'], genre=pd['genre'],
                       description=pd['description'], is_demo=True)
        db.session.add(proj); db.session.commit()
        for i, (t, c, p, s) in enumerate(pd['vs'], 1):
            v = _build_version(proj, dict(tempo=t, channel_count=c, pattern_count=p, sample_count=s),
                               origin='demo')
            v.captured_at = base + timedelta(days=i)
        db.session.commit()
    return True


# ── Auth ───────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def auth_login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        ident = request.form.get('identifier', '').strip()
        pw    = request.form.get('password', '')
        user  = User.query.filter((User.username == ident) | (User.email == ident)).first()
        if user and user.check_password(pw):
            login_user(user, remember=bool(request.form.get('remember')))
            return redirect(_safe_next(request.args.get('next')) or url_for('dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('auth/login.html')


@app.route('/register', methods=['GET', 'POST'])
def auth_register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        uname = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        pw    = request.form.get('password', '')
        cpw   = request.form.get('confirm_password', '')
        errs  = []
        if len(uname) < 3:                                errs.append('Username ≥ 3 characters.')
        if '@' not in email:                              errs.append('Valid email required.')
        if len(pw) < 6:                                   errs.append('Password ≥ 6 characters.')
        if pw != cpw:                                     errs.append('Passwords do not match.')
        if User.query.filter_by(username=uname).first():  errs.append('Username taken.')
        if User.query.filter_by(email=email).first():     errs.append('Email already registered.')
        if errs:
            for e in errs: flash(e, 'error')
        else:
            u = User(username=uname, email=email); u.set_password(pw)
            db.session.add(u); db.session.commit()
            db.session.add(AppSettings(user_id=u.id)); db.session.commit()
            login_user(u)
            flash('Account created. Welcome to PLMS.', 'success')
            return redirect(url_for('dashboard'))
    return render_template('auth/register.html')


@app.route('/logout')
@login_required
def auth_logout():
    logout_user()
    return redirect(url_for('auth_login'))


# ── Dashboard ──────────────────────────────────────────────────
@app.route('/')
@login_required
def dashboard():
    projects = Project.query.filter_by(user_id=current_user.id)\
                   .order_by(Project.created_at.desc()).all()
    total_v  = sum(p.version_count for p in projects)
    scored   = [p.latest_score for p in projects if p.latest_score > 0]
    avg      = round(sum(scored) / len(scored), 1) if scored else 0.0
    return render_template('dashboard.html', projects=projects,
                           total_projects=len(projects),
                           total_versions=total_v, avg_score=avg)


# ── Projects ───────────────────────────────────────────────────
@app.route('/project/new', methods=['POST'])
@login_required
def create_project():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Project name is required.', 'error')
        return redirect(url_for('dashboard'))

    source_path = request.form.get('source_path', '').strip()
    proj = Project(user_id=current_user.id, name=name,
                   description=request.form.get('description', '').strip(),
                   source_path=source_path,
                   genre=request.form.get('genre', '').strip())
    db.session.add(proj); db.session.commit()

    flp_file = request.files.get('flp_file')
    fname = secure_filename(flp_file.filename) if flp_file and flp_file.filename else ''
    if fname.lower().endswith('.flp'):
        raw = flp_file.read()
        save_dir  = os.path.join('projects', str(proj.id))
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, fname)
        with open(save_path, 'wb') as f: f.write(raw)
        proj.filepath = save_path
        if not proj.source_path:
            proj.source_path = fname
        db.session.commit()
        _capture_version(proj, raw, 'upload', do_backup=True)
        flash(f'Project "{name}" created — initial analytics captured.', 'success')
        s = _user_settings(current_user.id)
        if s.watcher_active and is_watching():
            try:
                start_watcher(list(_watched_folders_for_user(current_user.id)),
                              _make_capture_fn(bool(s.auto_backup)))
            except Exception: pass
    else:
        flash(f'Project "{name}" created.', 'success')
    return redirect(url_for('project_detail', project_id=proj.id))


def _filter_versions(versions, args):
    """Search / filter a project's version history (FR-09)."""
    q = (args.get('q') or '').strip().lower()
    def num(key):
        try:    return float(args.get(key)) if args.get(key) not in (None, '') else None
        except ValueError: return None
    def day(key):
        try:    return datetime.strptime(args.get(key), '%Y-%m-%d')
        except (TypeError, ValueError): return None
    smin, smax = num('score_min'), num('score_max')
    dfrom, dto = day('date_from'), day('date_to')
    origin     = (args.get('origin') or '').strip()
    only_notes = args.get('has_notes') in ('1', 'on', 'true')
    out = []
    for v in versions:
        if q:
            m = re.fullmatch(r'v?(\d+)', q)
            if m:
                if v.version_num != int(m.group(1)): continue
            elif q not in (v.notes or '').lower():
                continue
        if smin is not None and v.quality_score < smin: continue
        if smax is not None and v.quality_score > smax: continue
        if dfrom and v.captured_at < dfrom: continue
        if dto and v.captured_at >= dto + timedelta(days=1): continue
        if origin and (v.origin or 'capture') != origin: continue
        if only_notes and not (v.notes or '').strip(): continue
        out.append(v)
    return out


def _breakdown_for(v, user_id):
    bd = v.breakdown
    if bd:
        return dict(**bd, approximate=False)
    bd = _scorer_for(user_id).breakdown(v.metrics())      # legacy versions: recomputed
    return dict(**bd, approximate=True)


@app.route('/project/<int:project_id>')
@login_required
def project_detail(project_id):
    proj = _owned_project(project_id)
    all_versions = list(proj.versions)
    versions = _filter_versions(all_versions, request.args)
    filters  = {k: request.args.get(k, '') for k in
                ('q', 'score_min', 'score_max', 'date_from', 'date_to', 'origin', 'has_notes')}
    preds    = PredictionEngine().predict_all(all_versions) if len(all_versions) >= 3 else {}
    insights = PatternAnalyzer().summarise(all_versions)
    chart = dict(
        labels  =[f'v{v.version_num}' for v in all_versions],
        scores  =[v.quality_score for v in all_versions],
        channels=[v.channel_count for v in all_versions],
        tempos  =[v.tempo for v in all_versions],
        patterns=[v.pattern_count for v in all_versions],
        samples =[v.sample_total for v in all_versions],
    )
    latest = all_versions[-1] if all_versions else None
    breakdown = _breakdown_for(latest, current_user.id) if latest else None
    cmp_default = dict(a=all_versions[-2].version_num, b=latest.version_num) \
        if len(all_versions) >= 2 else None
    return render_template('project.html', project=proj, versions=versions,
                           all_count=len(all_versions), filters=filters,
                           filtered=any(filters.values()), predictions=preds,
                           insights=insights, breakdown=breakdown,
                           cmp_default=cmp_default, rollbacks=proj.rollbacks[:5],
                           chart_data=json.dumps(chart))


@app.route('/project/<int:project_id>/delete', methods=['POST'])
@login_required
def delete_project(project_id):
    proj = _owned_project(project_id)
    name = proj.name
    db.session.delete(proj); db.session.commit()
    flash(f'Project "{name}" deleted.', 'success')
    return redirect(url_for('dashboard'))


# ── API: Analyse FLP (upload preview) ─────────────────────────
@app.route('/api/analyze_flp', methods=['POST'])
@login_required
def api_analyze_flp():
    flp_file = request.files.get('flp_file')
    if not flp_file:
        return jsonify(ok=False, error='No file uploaded.')
    if not flp_file.filename.lower().endswith('.flp'):
        return jsonify(ok=False, error='Only .flp files are supported.')
    raw = flp_file.read()
    if len(raw) < 22:
        return jsonify(ok=False, error='File too small.')
    parsed = parse_flp_bytes(raw)
    score  = _scorer_for(current_user.id).calculate(dict(**parsed, version_num=1))
    parsed.pop('arrangement', None)
    return jsonify(ok=True, filename=flp_file.filename,
                   size_kb=round(len(raw) / 1024, 1), quality_score=round(score, 1), **parsed)


# ── API: Manual version upload ────────────────────────────────
@app.route('/api/project/<int:project_id>/upload_version', methods=['POST'])
@login_required
def api_upload_version(project_id):
    proj = _owned_project(project_id)
    if proj.is_demo:
        return jsonify(ok=False, error='Demo projects cannot receive uploads.')
    flp_file = request.files.get('flp_file')
    if not flp_file:
        return jsonify(ok=False, error='No file uploaded.')
    fname = secure_filename(flp_file.filename or '')
    if not fname.lower().endswith('.flp'):
        return jsonify(ok=False, error='Only .flp files are supported.')
    raw = flp_file.read()
    if len(raw) < 22:
        return jsonify(ok=False, error='File too small.')
    if proj.filepath and os.path.isdir(os.path.dirname(proj.filepath) or '.'):
        with open(proj.filepath, 'wb') as f: f.write(raw)
    else:
        save_dir = os.path.join('projects', str(proj.id))
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, fname)
        with open(save_path, 'wb') as f: f.write(raw)
        proj.filepath = save_path
        db.session.commit()
    try:
        v = _capture_version(proj, raw, 'upload', do_backup=True)
    except Exception as e:
        db.session.rollback()
        notify(proj.user_id, proj.id, 'error', f'Version capture failed for "{proj.name}": {e}', 'error')
        return jsonify(ok=False, error=f'Capture failed: {e}')
    p = parse_flp_bytes(raw); p.pop('arrangement', None)
    return jsonify(ok=True, version_num=v.version_num,
                   quality_score=round(v.quality_score, 1), **p)


# ── API: Watcher status ───────────────────────────────────────
@app.route('/api/watcher_status')
@login_required
def api_watcher_status():
    s = _user_settings(current_user.id)
    db_on = bool(s.watcher_active)
    live  = is_watching()
    if db_on and not live:
        try:
            start_watcher(list(_watched_folders_for_user(current_user.id)),
                          _make_capture_fn(bool(s.auto_backup)))
            live = True
        except Exception:
            s.watcher_active = False
            db.session.commit()
            db_on = False
    return jsonify(active=(db_on and live))


# ── API: Compare any two versions (FR-06..FR-10, FR-22) ───────
@app.route('/api/project/<int:project_id>/compare')
@login_required
def api_compare(project_id):
    proj = _owned_project(project_id)
    try:
        a, b = int(request.args.get('a', '')), int(request.args.get('b', ''))
    except ValueError:
        return jsonify(ok=False, error='Choose two versions to compare.'), 400
    if a == b:
        return jsonify(ok=False, error='Choose two different versions.'), 400
    va = Version.query.filter_by(project_id=proj.id, version_num=a).first()
    vb = Version.query.filter_by(project_id=proj.id, version_num=b).first()
    if not va or not vb:
        return jsonify(ok=False, error='Version not found.'), 404
    result = ChangeEngine().compare(va.metrics(), vb.metrics())
    result['breakdown'] = dict(a=_breakdown_for(va, current_user.id),
                               b=_breakdown_for(vb, current_user.id))
    result['notes'] = dict(a=va.notes or '', b=vb.notes or '')
    return jsonify(ok=True, **result)


# ── API: Version annotations (FR-08 / second FR list) ─────────
@app.route('/api/version/<int:version_id>/notes', methods=['POST'])
@login_required
def api_version_notes(version_id):
    v = (Version.query.join(Project, Version.project_id == Project.id)
         .filter(Version.id == version_id, Project.user_id == current_user.id).first_or_404())
    data  = request.get_json(silent=True) or {}
    notes = str(data.get('notes', '')).strip()
    if len(notes) > 1000:
        return jsonify(ok=False, error='Notes are limited to 1000 characters.'), 400
    v.notes = notes
    db.session.commit()
    return jsonify(ok=True, notes=v.notes)


# ── Export (second FR list: FR-10) ────────────────────────────
def _csv_safe(x):
    s = '' if x is None else str(x)
    return "'" + s if s[:1] in ('=', '+', '-', '@', '\t', '\r') else s


def _export_rows(proj, versions):
    for v in versions:
        yield [v.version_num, v.captured_at.strftime('%Y-%m-%d %H:%M'), v.origin or 'capture',
               v.restored_from or '', round(v.tempo, 1), v.channel_count, v.pattern_count,
               v.sample_total, v.playlist_items or 0, v.tracks_used or 0,
               v.arrangement_bars or 0, v.complexity or 0, v.quality_score,
               v.notes or '', v.file_hash or '']


EXPORT_HEADER = ['version', 'captured_at', 'origin', 'restored_from', 'tempo_bpm', 'channels',
                 'patterns', 'samples', 'playlist_clips', 'playlist_tracks', 'arrangement_bars',
                 'complexity', 'quality_score', 'notes', 'sha256']


@app.route('/project/<int:project_id>/export.csv')
@login_required
def export_csv(project_id):
    proj = _owned_project(project_id)
    versions = _filter_versions(list(proj.versions), request.args)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_HEADER)
    for row in _export_rows(proj, versions):
        w.writerow([_csv_safe(c) for c in row])
    fname = f'{_safe_name(proj.name)}_versions.csv'
    return Response(buf.getvalue(), mimetype='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})


@app.route('/project/<int:project_id>/export.pdf')
@login_required
def export_pdf(project_id):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    proj = _owned_project(project_id)
    all_v = list(proj.versions)
    versions = _filter_versions(all_v, request.args)
    st = getSampleStyleSheet()
    small = st['BodyText'].clone('small'); small.fontSize = 7.5; small.leading = 9

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), leftMargin=14*mm, rightMargin=14*mm,
                            topMargin=14*mm, bottomMargin=14*mm,
                            title=f'PLMS report — {proj.name}')
    el = [Paragraph(f'PLMS Version Report — {escape(proj.name)}', st['Title'])]
    meta = [f'Genre: {escape(proj.genre or "—")}',
            f'Generated: {datetime.now().strftime("%d %b %Y %H:%M")}',
            f'Versions shown: {len(versions)} of {len(all_v)}']
    el += [Paragraph(' &nbsp;|&nbsp; '.join(meta), st['BodyText']), Spacer(1, 6)]

    if all_v:
        scores = [v.quality_score for v in all_v]
        best = max(all_v, key=lambda v: v.quality_score)
        summ = (f'Latest score: <b>{all_v[-1].quality_score:.1f}</b> (v{all_v[-1].version_num}) &nbsp;|&nbsp; '
                f'Best: <b>{best.quality_score:.1f}</b> (v{best.version_num}) &nbsp;|&nbsp; '
                f'Average: <b>{sum(scores)/len(scores):.1f}</b>')
        p = PredictionEngine().predict(all_v, 'quality_score')
        if p.get('available'):
            summ += (f' &nbsp;|&nbsp; Predicted next score: <b>{p["predicted_value"]:.0f}</b> '
                     f'({p["trend"]}, {p["confidence"]} confidence)')
        el += [Paragraph(summ, st['BodyText']), Spacer(1, 8)]

    data = [['Ver', 'Captured', 'Origin', 'BPM', 'Ch', 'Pat', 'Smp', 'Clips', 'Cplx', 'Score', 'Notes']]
    for v in versions:
        origin = (v.origin or 'capture') + (f' (v{v.restored_from})' if v.restored_from else '')
        data.append([f'v{v.version_num}', v.captured_at.strftime('%Y-%m-%d %H:%M'), origin,
                     f'{v.tempo:.0f}', v.channel_count, v.pattern_count, v.sample_total,
                     v.playlist_items or '—', f'{v.complexity or 0:.0f}', f'{v.quality_score:.1f}',
                     Paragraph(escape((v.notes or '')[:300]), small)])
    t = Table(data, repeatRows=1, colWidths=[28, 78, 70, 32, 26, 26, 28, 34, 32, 36, 270])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1f2937')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f3f4f6')]),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#d1d5db'))]))
    el.append(t)

    if proj.rollbacks:
        el += [Spacer(1, 12), Paragraph('Rollback history', st['Heading3'])]
        rb = [['When', 'From', 'Restored', 'Recorded as', 'Status']]
        for r in proj.rollbacks[:20]:
            rb.append([r.created_at.strftime('%Y-%m-%d %H:%M'), f'v{r.from_version_num}',
                       f'v{r.to_version_num}', f'v{r.new_version_num}' if r.new_version_num else '—',
                       r.status])
        t2 = Table(rb, repeatRows=1)
        t2.setStyle(TableStyle([('FONTSIZE', (0, 0), (-1, -1), 8),
                                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#e5e7eb')),
                                ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#d1d5db'))]))
        el.append(t2)
    doc.build(el)
    fname = f'{_safe_name(proj.name)}_report.pdf'
    return Response(buf.getvalue(), mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})


# ── API: Rollback (FR-23..FR-26) ──────────────────────────────
def _restore_targets(proj):
    out = []
    for label, path in (('FL Studio file', proj.source_path), ('server copy', proj.filepath)):
        if not path or not path.lower().endswith('.flp'):
            continue
        d = os.path.dirname(path)
        if d and os.path.isdir(d):
            out.append((label, path))
    return out


def _atomic_write(path, raw):
    tmp = path + '.plms_tmp'
    with open(tmp, 'wb') as f:
        f.write(raw)
    os.replace(tmp, path)


@app.route('/api/rollback', methods=['POST'])
@login_required
def api_rollback():
    data = request.get_json(silent=True) or {}
    proj = _owned_project(data.get('project_id'))
    ver  = Version.query.filter_by(project_id=proj.id,
                                   version_num=data.get('version_num')).first_or_404()
    latest = _latest_version(proj.id)

    def fail(msg):
        db.session.add(RollbackEvent(project_id=proj.id,
                                     from_version_num=latest.version_num if latest else None,
                                     to_version_num=ver.version_num, status='failed', message=msg))
        db.session.commit()
        notify(proj.user_id, proj.id, 'error', f'Rollback failed for "{proj.name}": {msg}', 'error')
        return jsonify(success=False, message=msg)

    if proj.is_demo:
        return jsonify(success=False, message='Demo projects have no real files to restore.')
    if latest and ver.version_num == latest.version_num:
        return jsonify(success=False, message=f'v{ver.version_num} is already the latest version.')
    status = _verify_backup(ver)
    if status in ('no_backup', 'missing'):
        return fail(f'No backup file found for v{ver.version_num}. '
                    f'Enable auto-backup in settings to capture backups.')
    if status == 'corrupt':
        return fail(f'Backup for v{ver.version_num} failed its integrity check '
                    f'(the file changed since it was captured). No files were modified.')
    targets = _restore_targets(proj)
    if not targets:
        return fail('Backup found but could not locate a project file to restore.')

    with _capture_lock:
        raw = _read_stable(ver.backup_path)
        h   = _sha256_bytes(raw)
        # 1. safety copy of the current state (so the rollback itself is undoable)
        safety, safety_raw = '', None
        for _, path in targets:
            if os.path.exists(path):
                safety_raw = _read_stable(path)
                sdir = os.path.join(_backup_root(), f'{proj.id}_{_safe_name(proj.name)}', '_pre_rollback')
                os.makedirs(sdir, exist_ok=True)
                safety = os.path.join(sdir, f'before_restore_v{ver.version_num}_'
                                            f'{datetime.now().strftime("%Y%m%d_%H%M%S")}.flp')
                with open(safety, 'wb') as f: f.write(safety_raw)
                break
        # 2. record the restore as a new version BEFORE touching files, so the
        #    watcher sees identical content and does not create a duplicate
        newv = _build_version(proj, parse_flp_bytes(raw), file_hash=h, origin='rollback',
                              restored_from=ver.version_num,
                              notes=f'Restored from v{ver.version_num}')
        written = []
        try:
            for label, path in targets:
                _recent_restores[_norm(path)] = (h, time.time())
                _atomic_write(path, raw)
                written.append((label, path))
                if _sha256_file(path) != h:
                    raise IOError(f'verification failed for {label}')
        except Exception as e:
            for _, path in written:                           # compensate
                try:
                    if safety_raw is not None: _atomic_write(path, safety_raw)
                except Exception: pass
            db.session.delete(newv); db.session.commit()
            return fail(f'Restore aborted and rolled back: {e}')
        _store_backup(proj, newv, raw)
        db.session.add(RollbackEvent(project_id=proj.id,
                                     from_version_num=latest.version_num if latest else None,
                                     to_version_num=ver.version_num,
                                     new_version_num=newv.version_num,
                                     safety_backup_path=safety, status='success',
                                     message=f'Restored: {", ".join(l for l, _ in written)}'))
        db.session.commit()
    notify(proj.user_id, proj.id, 'rollback',
           f'"{proj.name}": rolled back to v{ver.version_num} — recorded as v{newv.version_num}.')
    return jsonify(success=True, new_version_num=newv.version_num,
                   message=f'Rolled back to v{ver.version_num} (recorded as v{newv.version_num}). '
                           f'All earlier versions are kept. Reopen the project in FL Studio.')


# ── API: Backup integrity check (NFR-05 / NFR-11) ─────────────
@app.route('/api/project/<int:project_id>/verify', methods=['POST'])
@login_required
def api_verify(project_id):
    proj = _owned_project(project_id)
    results = {}
    for v in proj.versions:
        results[v.version_num] = _verify_backup(v)
    counts = {}
    for st in results.values():
        counts[st] = counts.get(st, 0) + 1
    problems = {k: s for k, s in results.items() if s in ('corrupt', 'missing')}
    return jsonify(ok=True, counts=counts, problems=problems, total=len(results))


# ── API: Latest version / notifications ───────────────────────
@app.route('/api/project/<int:project_id>/latest')
@login_required
def api_latest(project_id):
    proj = _owned_project(project_id)
    lv   = _latest_version(proj.id)
    return jsonify(version_count=Version.query.filter_by(project_id=proj.id).count(),
                   latest_version=lv.version_num if lv else 0,
                   latest_score=lv.quality_score if lv else 0)


@app.route('/api/notifications')
@login_required
def api_notifications():
    rows = (Notification.query.filter_by(user_id=current_user.id, read=False)
            .order_by(Notification.id).limit(20).all())
    out = [dict(id=r.id, kind=r.kind, level=r.level, message=r.message,
                project_id=r.project_id) for r in rows]
    for r in rows: r.read = True
    Notification.query.filter(Notification.user_id == current_user.id, Notification.read == True,  # noqa: E712
                              Notification.created_at < datetime.utcnow() - timedelta(days=7)
                              ).delete(synchronize_session=False)
    db.session.commit()
    return jsonify(notifications=out)


# ── Settings ──────────────────────────────────────────────────
def _rescore_user(user_id) -> int:
    scorer = _scorer_for(user_id)
    n = 0
    for p in Project.query.filter_by(user_id=user_id).all():
        for v in p.versions:
            bd = scorer.breakdown(v.metrics())
            v.quality_score = bd['total']; v.breakdown_json = json.dumps(bd); n += 1
    db.session.commit()
    return n


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    s = _user_settings(current_user.id)
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'general':
            s.watched_folder        = request.form.get('watched_folder', './projects')
            s.notifications_enabled = 'notifications_enabled' in request.form
            s.auto_backup           = 'auto_backup' in request.form
            db.session.commit()
            flash('Settings saved.', 'success')
        elif action == 'scoring':
            try:
                w = {k: min(max(float(request.form.get(f'w_{k}', 0)), 0.0), 100.0)
                     for k in ('tempo', 'channels', 'patterns', 'samples', 'maturity')}
            except ValueError:
                w = None
            if not w or sum(w.values()) <= 0:
                flash('Enter at least one weight above zero.', 'error')
            else:
                s.score_weight_tempo, s.score_weight_channels = w['tempo'], w['channels']
                s.score_weight_patterns, s.score_weight_samples = w['patterns'], w['samples']
                s.score_weight_maturity = w['maturity']
                db.session.commit()
                if 'rescore' in request.form:
                    n = _rescore_user(current_user.id)
                    flash(f'Weights saved — {n} existing version(s) re-scored.', 'success')
                else:
                    flash('Weights saved. They apply to new versions.', 'success')
        elif action == 'toggle_watcher':
            if is_watching():
                stop_watcher(); s.watcher_active = False
                flash('Watcher stopped.', 'success')
            else:
                folders = _watched_folders_for_user(current_user.id)
                if not folders:
                    flash('No valid folders to watch. Add a project with a known file path first.', 'error')
                else:
                    start_watcher(list(folders), _make_capture_fn(bool(s.auto_backup)))
                    s.watcher_active = True
                    flash(f'Watcher started — monitoring {len(folders)} folder(s). '
                          f'Backups save to {_backup_root()}.', 'success')
            db.session.commit()
        elif action == 'demo':
            flash('Demo projects loaded (marked DEMO).' if _load_demo(current_user.id)
                  else 'Demo projects are already loaded.', 'success')
        elif action == 'remove_demo':
            n = 0
            for p in Project.query.filter_by(user_id=current_user.id, is_demo=True).all():
                db.session.delete(p); n += 1
            db.session.commit()
            flash(f'Removed {n} demo project(s).', 'success')
        elif action == 'password':
            cur = request.form.get('current_password', '')
            new = request.form.get('new_password', '')
            if current_user.check_password(cur) and len(new) >= 6:
                current_user.set_password(new); db.session.commit()
                flash('Password updated.', 'success')
            else:
                flash('Incorrect password or too short.', 'error')
        return redirect(url_for('settings'))

    try:    db_size = os.path.getsize(db.engine.url.database) // 1024
    except Exception: db_size = 0
    watched_flds = _watched_folders_for_user(current_user.id) if s.watcher_active else set()
    has_demo = Project.query.filter_by(user_id=current_user.id, is_demo=True).count() > 0
    return render_template('settings.html', s=s, db_size=db_size,
                           backup_root=_backup_root(), watched_folders=watched_flds,
                           has_demo=has_demo)


if __name__ == '__main__':
    app.run(debug=True, use_reloader=False, port=5000)
