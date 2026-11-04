from markupsafe import Markup
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
                     PatternAnalyzer, structural_complexity, progress_indicators)
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
    'versions': [('sample_count', 'INTEGER', 'NULL'), ('playlist_items', 'INTEGER', 'NULL'),
                 ('tracks_used', 'INTEGER', 'NULL'), ('arrangement_bars', 'FLOAT', 'NULL'),
                 ('complexity', 'FLOAT', 'NULL'), ('arrangement_json', 'TEXT', "''"),
                 ('parse_ok', 'BOOLEAN', '1'), ('parse_error', 'VARCHAR(200)', "''"),
                 ('plugins_json', 'TEXT', "''"),
                 ('breakdown_json', 'TEXT', "''"), ('file_hash', 'VARCHAR(64)', "''"),
                 ('backup_hash', 'VARCHAR(64)', "''"), ('origin', 'VARCHAR(20)', "'capture'"),
                 ('restored_from', 'INTEGER', 'NULL')],
    'app_settings': [('score_weight_samples', 'FLOAT', '15'), ('show_experimental', 'BOOLEAN', '0')],
    'rollback_events': [('restored_path', 'VARCHAR(500)', "''"), ('restored_hash', 'VARCHAR(64)', "''")],
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


def _show_experimental(user_id) -> bool:
    """Quality scoring and prediction are outside the agreed specification (Section 6 of the
    review). They are hidden unless the user switches them on in Settings."""
    s = AppSettings.query.filter_by(user_id=user_id).first()
    return bool(s and s.show_experimental)


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
    if not v.available:
        notify(proj.user_id, proj.id, 'capture',
               f'"{proj.name}": v{v.version_num} {label}, but its details are unavailable '
               f'({v.parse_error or "file could not be read"}). It is saved and can be restored.', 'info')
    else:
        notify(proj.user_id, proj.id, 'capture', f'"{proj.name}": v{v.version_num} {label}.')
    if not _show_experimental(proj.user_id) or not v.available or v.quality_score is None:
        return
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


def _build_version(project, data, file_hash='', origin='capture', restored_from=None, notes=''):
    """Create an immutable version row. When the file could not be parsed (parse_ok False)
    every property is stored as NULL ("unavailable"): no defaults are invented."""
    prev  = _latest_version(project.id)
    vnum  = (prev.version_num + 1) if prev else 1
    ok    = data.get('parse_ok', True)

    arr = (data.get('arrangement') or {}) if ok else {}
    has_arr = bool(ok and data.get('playlist_items'))
    props = {k: (data.get(k) if ok else None) for k in
             ('tempo', 'channel_count', 'pattern_count', 'sample_count',
              'playlist_items', 'tracks_used', 'arrangement_bars')}
    plugins = data.get('plugins') if ok else None

    vdata = dict(props, version_num=vnum, available=bool(ok), plugins=plugins,
                 has_arrangement=has_arr, arrangement=arr)
    # Experimental score: only when the three scored properties are all known
    score, bd = None, None
    if ok and None not in (props['tempo'], props['channel_count'], props['pattern_count']):
        bd = _scorer_for(project.user_id).breakdown(dict(props, sample_count=props['sample_count'] or 0,
                                                         version_num=vnum))
        score = bd['total']
    changes = ChangeEngine().compute(prev.metrics(), vdata) if prev else {}
    v = Version(project_id=project.id, version_num=vnum, parse_ok=bool(ok),
                parse_error=data.get('parse_error', '') if not ok else '',
                tempo=props['tempo'], channel_count=props['channel_count'],
                pattern_count=props['pattern_count'], sample_count=props['sample_count'],
                playlist_items=props['playlist_items'], tracks_used=props['tracks_used'],
                arrangement_bars=props['arrangement_bars'],
                plugins_json=json.dumps(plugins) if plugins is not None else '',
                arrangement_json=json.dumps(arr) if (has_arr and arr) else '',
                quality_score=score, breakdown_json=json.dumps(bd) if bd else '',
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


def _read_stable(path, tries=5):
    """Read a file, retrying when it is locked or changes while it is being read."""
    last = None
    for _ in range(tries):
        try:
            before = os.stat(path)
            with open(path, 'rb') as f:
                raw = f.read()
            after = os.stat(path)
            if (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) \
                    and len(raw) == after.st_size:
                return raw
            last = IOError(f'{path} changed while it was being read')
        except OSError as e:
            last = e
        time.sleep(0.4)
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
    exp = _show_experimental(current_user.id) if current_user.is_authenticated else False
    return dict(watcher_active=active, show_experimental=exp)


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
    scored   = [p.latest_score for p in projects if p.latest_score and p.latest_score > 0]
    avg      = round(sum(scored) / len(scored), 1) if scored else 0.0
    return render_template('dashboard.html', projects=projects,
                           total_projects=len(projects),
                           total_versions=total_v, avg_score=avg)


# ── Projects ───────────────────────────────────────────────────
@app.template_filter('basename')
def _basename_filter(path):
    return os.path.basename(str(path).replace('\\', '/'))


def _validate_flp_path(path: str):
    """Return a problem description, or None when `path` is an existing .flp file."""
    if not path.lower().endswith('.flp'):
        return 'The file path must end in .flp (an FL Studio project file).'
    if not os.path.isfile(path):
        return f'No file exists at "{path}". Check the path, or leave it blank and upload the file instead.'
    return None


@app.route('/project/new', methods=['POST'])
@login_required
def create_project():
    name = request.form.get('name', '').strip()
    if not name:
        flash('Project name is required.', 'error')
        return redirect(url_for('dashboard'))

    source_path = request.form.get('source_path', '').strip()
    if source_path:
        problem = _validate_flp_path(source_path)
        if problem:
            flash(problem, 'error')
            return redirect(url_for('dashboard'))
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
    if not v.available or None in (v.tempo, v.channel_count, v.pattern_count):
        return None
    bd = v.breakdown
    if bd:
        return dict(**bd, approximate=False)
    bd = _scorer_for(user_id).breakdown(dict(v.metrics(), sample_count=v.sample_total or 0))
    return dict(**bd, approximate=True)


def _latest_change(all_versions):
    """Plain-language comparison of the last two versions (for the 'Latest changes' card)."""
    if len(all_versions) < 2:
        return None
    return ChangeEngine().compare(all_versions[-2].metrics(), all_versions[-1].metrics())


@app.route('/project/<int:project_id>')
@login_required
def project_detail(project_id):
    proj = _owned_project(project_id)
    exp = _show_experimental(current_user.id)
    all_versions = list(proj.versions)
    versions = _filter_versions(all_versions, request.args)
    filters  = {k: request.args.get(k, '') for k in
                ('q', 'score_min', 'score_max', 'date_from', 'date_to', 'origin', 'has_notes')}
    preds, insights, breakdown = {}, dict(available=False, insights=[]), None
    latest = all_versions[-1] if all_versions else None
    if exp:
        preds    = PredictionEngine().predict_all(all_versions) if len(all_versions) >= 3 else {}
        insights = PatternAnalyzer().summarise(all_versions)
        breakdown = _breakdown_for(latest, current_user.id) if latest else None
    chart = dict(
        labels  =[f'v{v.version_num}' for v in all_versions],
        scores  =[v.quality_score for v in all_versions],
        channels=[v.channel_count for v in all_versions],
        tempos  =[v.tempo for v in all_versions],
        patterns=[v.pattern_count for v in all_versions],
        samples =[v.sample_total for v in all_versions],
        playlist=[v.playlist_items for v in all_versions],
        plugins =[len(v.plugins) if v.plugins is not None else None for v in all_versions],
        show_score=exp,
    )
    cmp_default = dict(a=all_versions[-2].version_num, b=latest.version_num) \
        if len(all_versions) >= 2 else None
    return render_template('project.html', project=proj, versions=versions,
                           all_count=len(all_versions), filters=filters,
                           filtered=any(filters.values()), predictions=preds,
                           insights=insights, breakdown=breakdown,
                           cmp_default=cmp_default, rollbacks=proj.rollbacks[:5],
                           latest_change=_latest_change(all_versions),
                           indicators=progress_indicators([v.metrics() for v in all_versions]),
                           chart_data=Markup(json.dumps(chart).replace('<', '\\u003c')))


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
    parsed.pop('arrangement', None)
    score = None
    if parsed['parse_ok'] and None not in (parsed['tempo'], parsed['channel_count'], parsed['pattern_count']):
        score = round(_scorer_for(current_user.id).calculate(dict(parsed, version_num=1)), 1)
    return jsonify(ok=True, filename=flp_file.filename,
                   size_kb=round(len(raw) / 1024, 1), quality_score=score,
                   show_score=_show_experimental(current_user.id), **parsed)


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
    return jsonify(ok=True, version_num=v.version_num, available=v.available,
                   parse_error=v.parse_error or '', tempo=v.tempo, channel_count=v.channel_count,
                   pattern_count=v.pattern_count, plugins=v.plugins,
                   quality_score=round(v.quality_score, 1) if v.quality_score is not None else None,
                   show_score=_show_experimental(current_user.id))


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
    exp = _show_experimental(current_user.id)
    result = ChangeEngine().compare(va.metrics(), vb.metrics(), include_score=exp)
    if exp:
        result['breakdown'] = dict(a=_breakdown_for(va, current_user.id),
                                   b=_breakdown_for(vb, current_user.id))
    result['notes'] = dict(a=va.notes or '', b=vb.notes or '')
    result['show_score'] = exp
    return jsonify(ok=True, **result)


# ── Compare screen: pick any two versions and read the differences ──
@app.route('/project/<int:project_id>/compare')
@login_required
def compare_page(project_id):
    proj = _owned_project(project_id)
    versions = list(proj.versions)
    default_a = versions[-2].version_num if len(versions) >= 2 else None
    default_b = versions[-1].version_num if versions else None
    return render_template('compare.html', project=proj, versions=versions,
                           default_a=request.args.get('a', default_a),
                           default_b=request.args.get('b', default_b))


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


def _u(x, fmt='{}'):
    """Value for display/export; None is shown as 'unavailable', never as a number."""
    return 'unavailable' if x is None else fmt.format(x)


def _plugins_text(v):
    return 'unavailable' if v.plugins is None else '; '.join(v.plugins)


def _export_rows(proj, versions, exp=False):
    for v in versions:
        row = [v.version_num, v.captured_at.strftime('%Y-%m-%d %H:%M'), v.origin or 'capture',
               v.restored_from or '', _u(v.tempo), _u(v.channel_count), _u(v.pattern_count),
               _u(v.sample_total), _u(v.playlist_items), _u(v.tracks_used),
               _u(v.arrangement_bars), _plugins_text(v), v.notes or '', v.file_hash or '']
        if exp:
            row.append(_u(v.quality_score))
        yield row


EXPORT_HEADER = ['version', 'captured_at', 'origin', 'restored_from', 'tempo_bpm', 'channels',
                 'patterns', 'samples', 'playlist_clips', 'playlist_tracks', 'arrangement_bars',
                 'plugins', 'notes', 'sha256']


@app.route('/project/<int:project_id>/export.csv')
@login_required
def export_csv(project_id):
    proj = _owned_project(project_id)
    exp = _show_experimental(current_user.id)
    versions = _filter_versions(list(proj.versions), request.args)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_HEADER + (['quality_score'] if exp else []))
    for row in _export_rows(proj, versions, exp):
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
    exp = _show_experimental(current_user.id)
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

    if exp:
        scored = [v for v in all_v if v.quality_score is not None]
        if scored:
            best = max(scored, key=lambda v: v.quality_score)
            summ = (f'Latest score: <b>{_u(all_v[-1].quality_score, "{:.1f}")}</b> (v{all_v[-1].version_num}) '
                    f'&nbsp;|&nbsp; Best: <b>{best.quality_score:.1f}</b> (v{best.version_num})')
            p = PredictionEngine().predict(all_v, 'quality_score')
            if p.get('available'):
                summ += (f' &nbsp;|&nbsp; Predicted next score: <b>{p["predicted_value"]:.0f}</b> '
                         f'({p["trend"]}, {p["confidence"]} confidence)')
            el += [Paragraph(summ, st['BodyText']), Spacer(1, 8)]

    head = ['Ver', 'Captured', 'Origin', 'BPM', 'Ch', 'Pat', 'Smp', 'Clips', 'Plugins', 'Notes']
    widths = [28, 78, 70, 40, 30, 30, 30, 34, 150, 180]
    if exp:
        head.insert(-1, 'Score'); widths.insert(-1, 36)
    data = [head]
    for v in versions:
        origin = (v.origin or 'capture') + (f' (v{v.restored_from})' if v.restored_from else '')
        row = [f'v{v.version_num}', v.captured_at.strftime('%Y-%m-%d %H:%M'), origin,
               _u(v.tempo, '{:.0f}'), _u(v.channel_count), _u(v.pattern_count), _u(v.sample_total),
               _u(v.playlist_items), Paragraph(escape(_plugins_text(v)), small)]
        if exp:
            row.append(_u(v.quality_score, '{:.1f}'))
        row.append(Paragraph(escape((v.notes or '')[:300]), small))
        data.append(row)
    t = Table(data, repeatRows=1, colWidths=widths)
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
        rb = [['When', 'Restored version', 'Saved as (new copy)', 'Status']]
        for r in proj.rollbacks[:20]:
            rb.append([r.created_at.strftime('%Y-%m-%d %H:%M'), f'v{r.to_version_num}',
                       Paragraph(escape(os.path.basename(r.restored_path) if r.restored_path else '—'), small),
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


# ── Rollback (R6/R7): restore as a NEW, uniquely named copy ───
# The producer's current .flp is never opened for writing, renamed or replaced, and no
# version row is changed. The restored copy is checked against the stored SHA-256.
def _restore_dir(proj) -> str:
    """Folder for the restored copy: next to the producer's file, else the server copy's
    folder, else a PLMS folder under the backup root. Never created inside a Backup folder."""
    for path in (proj.source_path, proj.filepath):
        d = os.path.dirname(os.path.abspath(path)) if path else ''
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    d = os.path.join(_backup_root(), f'{proj.id}_{_safe_name(proj.name)}', '_restored')
    os.makedirs(d, exist_ok=True)
    return d


def _restore_stem(proj) -> str:
    base = os.path.basename(proj.source_path or proj.filepath or '') or proj.name
    stem = os.path.splitext(base)[0]
    return _safe_name(stem)


def _restore_name(proj, vnum, now=None, taken=lambda p: False) -> str:
    """e.g. SongName_restored_v2_20261103.flp ; adds a time, then a counter, if that name is taken."""
    now = now or datetime.now()
    stem = f'{_restore_stem(proj)}_restored_v{vnum}'
    d = _restore_dir(proj)
    for suffix in (now.strftime('%Y%m%d'), now.strftime('%Y%m%d_%H%M%S')):
        name = f'{stem}_{suffix}.flp'
        if not os.path.exists(os.path.join(d, name)) and not taken(name):
            return name
    n = 2
    while True:
        name = f'{stem}_{now.strftime("%Y%m%d_%H%M%S")}_{n}.flp'
        if not os.path.exists(os.path.join(d, name)):
            return name
        n += 1


def _write_new_file(path: str, raw: bytes):
    """Create `path` exclusively: raises FileExistsError instead of ever overwriting."""
    with open(path, 'xb') as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def _rollback_checks(proj, ver):
    """Return (ok, message). Shared by the rollback screen preview and the action."""
    if proj.is_demo:
        return False, 'Demo projects have no real files to restore.'
    status = _verify_backup(ver)
    if status in ('no_backup', 'missing'):
        return False, (f'No backup file found for v{ver.version_num}. '
                       f'Enable auto-backup in settings to capture backups.')
    if status == 'corrupt':
        return False, (f'Backup for v{ver.version_num} failed its integrity check '
                       f'(the file changed since it was captured). Nothing was written.')
    return True, ''


@app.route('/project/<int:project_id>/rollback')
@login_required
def rollback_page(project_id):
    proj = _owned_project(project_id)
    versions = list(proj.versions)
    latest = versions[-1] if versions else None
    try:    sel = int(request.args.get('version', ''))
    except ValueError: sel = None
    rows = []
    for v in reversed(versions):
        ok, why = _rollback_checks(proj, v) if not proj.is_demo else (False, 'Demo project')
        if latest and v.version_num == latest.version_num:
            ok, why = False, 'This is the latest version.'
        rows.append(dict(v=v, ok=ok, why=why,
                         name=_restore_name(proj, v.version_num) if ok else ''))
    return render_template('rollback.html', project=proj, rows=rows, selected=sel,
                           restore_dir=_restore_dir(proj) if not proj.is_demo else '',
                           history=proj.rollbacks[:20])


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
    ok, why = _rollback_checks(proj, ver)
    if not ok:
        return fail(why)

    expected = ver.backup_hash or ver.file_hash
    with _capture_lock:
        raw = _read_stable(ver.backup_path)
        h = _sha256_bytes(raw)
        if expected and h != expected:
            return fail(f'Backup for v{ver.version_num} failed its integrity check. Nothing was written.')
        dest_dir = _restore_dir(proj)
        dest = None
        for _ in range(5):                       # exclusive create; retry if the name raced
            name = _restore_name(proj, ver.version_num)
            dest = os.path.join(dest_dir, name)
            try:
                _write_new_file(dest, raw)
                break
            except FileExistsError:
                dest = None
            except OSError as e:
                return fail(f'Could not write the restored copy: {e}')
        if dest is None:
            return fail('Could not find a free file name for the restored copy.')
        if _sha256_file(dest) != h:               # verify the copy, then remove only OUR new file
            try: os.remove(dest)
            except OSError: pass
            return fail('The restored copy did not match the stored checksum and was removed. '
                        'Your current file was not touched.')
        db.session.add(RollbackEvent(project_id=proj.id,
                                     from_version_num=latest.version_num if latest else None,
                                     to_version_num=ver.version_num, status='success',
                                     restored_path=dest, restored_hash=h,
                                     message=f'Restored v{ver.version_num} as {os.path.basename(dest)}'))
        db.session.commit()
    notify(proj.user_id, proj.id, 'rollback',
           f'"{proj.name}": v{ver.version_num} restored as {os.path.basename(dest)}.')
    return jsonify(success=True, restored_name=os.path.basename(dest), restored_path=dest,
                   restored_dir=dest_dir, sha256=h, verified=True,
                   message=(f'Restored v{ver.version_num} as "{os.path.basename(dest)}". '
                            f'Your current file and all versions are unchanged. '
                            f'Open the new copy in FL Studio.'))


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
        elif action == 'experimental':
            s.show_experimental = 'show_experimental' in request.form
            db.session.commit()
            flash('Quality scoring and prediction are now ' +
                  ('shown.' if s.show_experimental else 'hidden.'), 'success')
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
    app.run(debug=os.environ.get('PLMS_DEBUG') == '1', use_reloader=False, port=5000)
