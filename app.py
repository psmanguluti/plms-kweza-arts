import os, json, shutil, random, logging, struct, math
from pathlib import Path
from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, flash)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)
from models  import db, User, Project, Version, AppSettings
from engines import ChangeEngine, ScoringEngine, PredictionEngine
from watcher import start_watcher, stop_watcher, is_watching

logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] %(levelname)s: %(message)s')

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI']        = 'sqlite:///plms.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY']                     = 'plms-kweza-arts-2024-secret'
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view    = 'auth_login'
login_manager.login_message = ''

@login_manager.user_loader
def load_user(uid): return User.query.get(int(uid))

with app.app_context():
    db.create_all()
    # Migrate existing DBs: add source_path and sample_count if missing
    with db.engine.connect() as conn:
        for col, tbl, defval in [
            ('source_path',  'projects',  '""'),
            ('sample_count', 'versions',  '0'),
        ]:
            try:
                conn.execute(
                    f'ALTER TABLE {tbl} ADD COLUMN {col} '
                    f'VARCHAR(500) DEFAULT {defval}'
                )
                logging.info(f'[DB] Migrated: {tbl}.{col} added.')
            except Exception:
                pass  # Already exists


# ── Backup folder ──────────────────────────────────────────────
def _backup_root() -> str:
    """
    Store backups in ~/Documents/PLMS_Backups — well away from FL Studio's
    own folder so watchdog doesn't pick them up and FL can't see them
    as project files to scan/crash on.
    """
    root = os.path.join(Path.home(), 'Documents', 'PLMS_Backups')
    os.makedirs(root, exist_ok=True)
    return root


# ── FLP Binary Parser ──────────────────────────────────────────
def _parse_flp_bytes(data: bytes) -> dict:
    """
    Parse FL Studio .flp from raw bytes.
    Returns tempo, channel_count, pattern_count, sample_count.

    Channel types (event 21, byte value):
      0 = Sampler / AudioClip  → sample_count
      1 = Native plugin
      2 = Layer
      3 = Instrument plugin
      4 = Automation clip
    Types 1, 3 = synth/plugin channels.
    """
    try:
        if data[:4] != b'FLhd':
            return dict(tempo=120.0, channel_count=0,
                        pattern_count=0, sample_count=0)

        hdr_tracks = struct.unpack_from('<H', data, 10)[0]
        if data[14:18] != b'FLdt':
            return dict(tempo=120.0, channel_count=hdr_tracks,
                        pattern_count=0, sample_count=0)

        pos = 22; end = len(data)
        tempo       = 120.0
        chan_count  = 0
        sample_count= 0
        pat_nums    = set()
        pending_type= None   # channel type seen before NewChan fires

        while pos < end - 1:
            etype = data[pos]; pos += 1

            if etype < 64:        # BYTE
                val = data[pos]; pos += 1
                if etype == 21:   # Channel type flag
                    pending_type = val

            elif etype < 128:     # WORD
                pos += 2
                if etype == 64:   # NewChan — a new channel block starts
                    chan_count += 1
                    if pending_type == 0:   # Sampler / AudioClip
                        sample_count += 1
                    pending_type = None

            elif etype < 192:     # DWORD
                val = struct.unpack_from('<I', data, pos)[0]; pos += 4
                if etype == 156:              # Master tempo * 1000
                    tempo = val / 1000.0
                if etype == 132:              # Playlist item → pattern ref
                    pat_nums.add(val & 0xFFFF)

            else:                 # TEXT / DATA
                length = 0; shift = 0
                while pos < end:
                    b = data[pos]; pos += 1
                    length |= (b & 0x7F) << shift
                    if not (b & 0x80): break
                    shift += 7
                pos = min(pos + length, end)

        if chan_count == 0 and hdr_tracks > 0:
            chan_count = hdr_tracks

        return dict(
            tempo         = round(tempo, 1),
            channel_count = chan_count,
            pattern_count = len(pat_nums) if pat_nums else 1,
            sample_count  = sample_count,
        )
    except Exception as e:
        logging.warning(f'[FLP Parser] {e}')
        return dict(tempo=120.0, channel_count=0,
                    pattern_count=0, sample_count=0)


def _parse_flp(fp: str) -> dict:
    try:
        with open(fp, 'rb') as f:
            return _parse_flp_bytes(f.read())
    except Exception as e:
        logging.warning(f'[FLP read] {fp}: {e}')
        return dict(tempo=120.0, channel_count=0,
                    pattern_count=0, sample_count=0)


# ── Watcher callback ───────────────────────────────────────────
def _make_capture_fn(do_backup: bool):
    """
    Three-tier matching:
      1. Exact match on project.source_path
      2. Exact match on project.filepath
      3. Filename-only match (catches path changes)
    Backups go to ~/Documents/PLMS_Backups/<project_name>/ — NOT inside
    the FL Studio folder, which was causing FL to crash.
    """
    def on_flp_change(filepath):
        with app.app_context():
            fp_norm = filepath.lower().replace('\\', '/')
            fp_base = os.path.basename(fp_norm)

            proj = Project.query.filter(
                db.func.lower(Project.source_path) == fp_norm
            ).first()
            if not proj:
                proj = Project.query.filter(
                    db.func.lower(Project.filepath) == fp_norm
                ).first()
            if not proj:
                for p in Project.query.all():
                    sp  = (p.source_path or '').lower().replace('\\', '/')
                    fp2 = (p.filepath    or '').lower().replace('\\', '/')
                    if (os.path.basename(sp) == fp_base or
                            os.path.basename(fp2) == fp_base):
                        proj = p; break

            if not proj:
                logging.info(f'[Watcher] No project matched: {filepath}')
                return

            # Keep source_path current
            if (proj.source_path or '').lower().replace('\\', '/') != fp_norm:
                proj.source_path = filepath
                db.session.commit()

            parsed = _parse_flp(filepath)
            v = _build_version(proj, data=parsed)
            logging.info(
                f'[Watcher] v{v.version_num} for "{proj.name}" '
                f'(score {v.quality_score:.1f})'
            )

            if do_backup and os.path.exists(filepath):
                # Safe backup location — away from FL Studio's folder
                bdir = os.path.join(_backup_root(), _safe_name(proj.name))
                os.makedirs(bdir, exist_ok=True)
                bp = os.path.join(bdir,
                    f'{_safe_name(proj.name)}_v{v.version_num:03d}.flp')
                shutil.copy2(filepath, bp)
                v.backup_path = bp
                db.session.commit()

    return on_flp_change


def _safe_name(s: str) -> str:
    """Sanitise a project name for use as a folder/file name."""
    return ''.join(c if c.isalnum() or c in ' _-' else '_' for c in s).strip()


def _watched_folders_for_user(user_id):
    folders = set()
    s = AppSettings.query.filter_by(user_id=user_id).first()
    if s and s.watched_folder:
        f = os.path.abspath(s.watched_folder)
        if os.path.isdir(f):
            folders.add(f)
    for p in Project.query.filter_by(user_id=user_id).all():
        for path in [p.source_path, p.filepath]:
            if path:
                parent = os.path.dirname(os.path.abspath(path))
                if os.path.isdir(parent):
                    folders.add(parent)
    return folders


# ── Context processor ──────────────────────────────────────────
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
                    fn = _make_capture_fn(bool(s.auto_backup))
                    start_watcher(list(_watched_folders_for_user(current_user.id)), fn)
                    active = True
                except Exception as e:
                    logging.error(f'[App] Restart failed: {e}')
                    s.watcher_active = False
                    db.session.commit()
    return dict(watcher_active=active)


# ── Version builder ────────────────────────────────────────────
def _build_version(project, data=None):
    prev  = project.latest_version
    vnum  = (prev.version_num + 1) if prev else 1
    if data:
        tempo    = float(data.get('tempo',         120.0))
        channels = int(data.get('channel_count',   0))
        patterns = int(data.get('pattern_count',   1))
        samples  = int(data.get('sample_count',    0))
    else:
        base_t   = prev.tempo if prev else random.uniform(80, 140)
        tempo    = round(base_t + random.uniform(-2, 2), 1)
        channels = min((prev.channel_count if prev else 0) + random.randint(0, 3), 32)
        patterns = min((prev.pattern_count if prev else 0) + random.randint(0, 2), 20)
        samples  = int(channels * random.uniform(0.3, 0.6))

    vdata  = dict(tempo=tempo, channel_count=channels,
                  pattern_count=patterns, sample_count=samples,
                  version_num=vnum)
    score  = ScoringEngine().calculate(vdata)
    changes = {}
    if prev:
        prev_data = dict(tempo=prev.tempo, channel_count=prev.channel_count,
                         pattern_count=prev.pattern_count,
                         sample_count=getattr(prev, 'sample_count', 0))
        changes = ChangeEngine().compute(prev_data, vdata)

    v = Version(
        project_id    = project.id,
        version_num   = vnum,
        tempo         = tempo,
        channel_count = channels,
        pattern_count = patterns,
        quality_score = score,
        changes_json  = json.dumps(changes),
    )
    # sample_count stored via setattr in case old DB doesn't have it yet
    try:
        v.sample_count = samples
    except Exception:
        pass
    db.session.add(v)
    db.session.commit()
    return v


# ── Demo loader ────────────────────────────────────────────────
def _load_demo(uid):
    demos = [
        dict(name='Summer Anthem',    genre='Afrobeats',
             description='Upbeat track with traditional percussion',
             vs=[(102,4,3,2),(102,7,5,3),(104,10,7,5),(104,13,9,6),
                 (105,16,11,7),(105,18,12,8)]),
        dict(name='Midnight Groove',  genre='Amapiano',
             description='Deep bass log drum exploration',
             vs=[(112,3,2,1),(112,6,4,2),(113,9,6,4),(113,12,8,5)]),
        dict(name='Afro Fusion Vol.1',genre='Afro-fusion',
             description='Traditional meets electronic',
             vs=[(95,5,4,2),(95,9,6,4),(96,14,8,6),(96,18,11,8),
                 (97,20,13,9),(97,22,14,10),(97,24,15,11)]),
    ]
    sc = ScoringEngine(); ch = ChangeEngine()
    for pd in demos:
        proj = Project(user_id=uid, name=pd['name'], genre=pd['genre'],
                       description=pd['description'],
                       filepath=f"./projects/{pd['name'].lower().replace(' ','_')}.flp")
        db.session.add(proj); db.session.flush()
        prev = None
        for i, (t, c, p, s) in enumerate(pd['vs'], 1):
            vd = dict(tempo=t, channel_count=c, pattern_count=p,
                      sample_count=s, version_num=i)
            changes = ch.compute(
                dict(tempo=prev.tempo, channel_count=prev.channel_count,
                     pattern_count=prev.pattern_count,
                     sample_count=getattr(prev, 'sample_count', 0)), vd
            ) if prev else {}
            v = Version(project_id=proj.id, version_num=i, tempo=t,
                        channel_count=c, pattern_count=p,
                        quality_score=sc.calculate(vd),
                        changes_json=json.dumps(changes))
            try: v.sample_count = s
            except Exception: pass
            db.session.add(v); prev = v
    db.session.commit()


# ── Auth ───────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def auth_login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        ident = request.form.get('identifier', '').strip()
        pw    = request.form.get('password', '')
        user  = User.query.filter(
            (User.username == ident) | (User.email == ident)
        ).first()
        if user and user.check_password(pw):
            login_user(user, remember=bool(request.form.get('remember')))
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('auth/login.html')


@app.route('/register', methods=['GET', 'POST'])
def auth_register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        uname = request.form.get('username', '').strip()
        email = request.form.get('email',    '').strip().lower()
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
    if flp_file and flp_file.filename.lower().endswith('.flp'):
        save_dir  = os.path.join('projects', str(proj.id))
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, flp_file.filename)
        flp_file.save(save_path)
        proj.filepath = save_path
        if not proj.source_path:
            proj.source_path = flp_file.filename
        db.session.commit()
        data = _parse_flp(save_path)
        _build_version(proj, data=data)
        flash(f'Project "{name}" created — initial analytics captured.', 'success')
        s = AppSettings.query.filter_by(user_id=current_user.id).first()
        if s and s.watcher_active and is_watching():
            try:
                fn = _make_capture_fn(bool(s.auto_backup))
                start_watcher(list(_watched_folders_for_user(current_user.id)), fn)
            except Exception: pass
    else:
        flash(f'Project "{name}" created.', 'success')

    return redirect(url_for('project_detail', project_id=proj.id))


@app.route('/project/<int:project_id>')
@login_required
def project_detail(project_id):
    proj  = Project.query.filter_by(id=project_id,
                                    user_id=current_user.id).first_or_404()
    preds = PredictionEngine().predict_all(proj.versions) \
            if len(proj.versions) >= 3 else {}
    # Chart uses ALL versions — JS windowing handles the display
    chart = dict(
        labels  =[f'v{v.version_num}'                         for v in proj.versions],
        scores  =[v.quality_score                              for v in proj.versions],
        channels=[v.channel_count                              for v in proj.versions],
        tempos  =[v.tempo                                      for v in proj.versions],
        patterns=[v.pattern_count                              for v in proj.versions],
        samples =[getattr(v, 'sample_count', 0)               for v in proj.versions],
    )
    return render_template('project.html', project=proj,
                           predictions=preds,
                           chart_data=json.dumps(chart))


@app.route('/project/<int:project_id>/delete', methods=['POST'])
@login_required
def delete_project(project_id):
    proj = Project.query.filter_by(id=project_id,
                                   user_id=current_user.id).first_or_404()
    name = proj.name
    db.session.delete(proj); db.session.commit()
    flash(f'Project "{name}" deleted.', 'success')
    return redirect(url_for('dashboard'))


# ── API: Analyse FLP (upload) ─────────────────────────────────
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
    parsed  = _parse_flp_bytes(raw)
    size_kb = round(len(raw) / 1024, 1)
    score   = ScoringEngine().calculate(dict(**parsed, version_num=1))
    return jsonify(ok=True, filename=flp_file.filename,
                   size_kb=size_kb, quality_score=round(score, 1), **parsed)


# ── API: Manual version upload ────────────────────────────────
@app.route('/api/project/<int:project_id>/upload_version', methods=['POST'])
@login_required
def api_upload_version(project_id):
    proj     = Project.query.filter_by(id=project_id,
                                       user_id=current_user.id).first_or_404()
    flp_file = request.files.get('flp_file')
    if not flp_file:
        return jsonify(ok=False, error='No file uploaded.')
    if not flp_file.filename.lower().endswith('.flp'):
        return jsonify(ok=False, error='Only .flp files are supported.')
    raw = flp_file.read()
    if len(raw) < 22:
        return jsonify(ok=False, error='File too small.')
    parsed = _parse_flp_bytes(raw)
    if proj.filepath and os.path.isdir(os.path.dirname(proj.filepath or '.')):
        with open(proj.filepath, 'wb') as f: f.write(raw)
    else:
        save_dir  = os.path.join('projects', str(proj.id))
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, flp_file.filename)
        with open(save_path, 'wb') as f: f.write(raw)
        proj.filepath = save_path
        db.session.commit()
    v = _build_version(proj, data=parsed)
    return jsonify(ok=True, version_num=v.version_num,
                   quality_score=round(v.quality_score, 1), **parsed)


# ── API: Watcher status ───────────────────────────────────────
@app.route('/api/watcher_status')
@login_required
def api_watcher_status():
    s = AppSettings.query.filter_by(user_id=current_user.id).first()
    db_on = bool(s and s.watcher_active)
    live  = is_watching()
    if db_on and not live:
        try:
            fn = _make_capture_fn(bool(s.auto_backup))
            start_watcher(list(_watched_folders_for_user(current_user.id)), fn)
            live = True
        except Exception:
            s.watcher_active = False
            db.session.commit()
            db_on = False
    return jsonify(active=(db_on and live))


# ── API: Rollback ─────────────────────────────────────────────
@app.route('/api/rollback', methods=['POST'])
@login_required
def api_rollback():
    data = request.get_json(silent=True) or {}
    proj = Project.query.filter_by(id=data.get('project_id'),
                                   user_id=current_user.id).first_or_404()
    ver  = Version.query.filter_by(project_id=proj.id,
                                   version_num=data.get('version_num')).first_or_404()

    if not ver.backup_path or not os.path.exists(ver.backup_path):
        return jsonify(success=False,
                       message=f'No backup file found for v{ver.version_num}. '
                               f'Enable auto-backup in settings to capture backups.')

    restored = []
    # 1. Restore to server-side copy
    if proj.filepath and os.path.isdir(os.path.dirname(proj.filepath or '.')):
        shutil.copy2(ver.backup_path, proj.filepath)
        restored.append('server copy')

    # 2. Restore to the REAL FL Studio file (source_path)
    if proj.source_path and os.path.exists(os.path.dirname(proj.source_path or '.')):
        shutil.copy2(ver.backup_path, proj.source_path)
        restored.append('FL Studio file')

    if restored:
        return jsonify(success=True,
                       message=f'Rolled back to v{ver.version_num}. '
                               f'Restored: {", ".join(restored)}. '
                               f'Reopen the project in FL Studio to see the changes.')
    else:
        return jsonify(success=False,
                       message='Backup found but could not locate project file to restore.')


# ── API: Latest version ───────────────────────────────────────
@app.route('/api/project/<int:project_id>/latest')
@login_required
def api_latest(project_id):
    proj = Project.query.filter_by(id=project_id,
                                   user_id=current_user.id).first_or_404()
    lv   = proj.latest_version
    return jsonify(version_count=proj.version_count,
                   latest_version=lv.version_num   if lv else 0,
                   latest_score  =lv.quality_score if lv else 0)


# ── Settings ──────────────────────────────────────────────────
@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    s = AppSettings.query.filter_by(user_id=current_user.id).first()
    if not s:
        s = AppSettings(user_id=current_user.id)
        db.session.add(s); db.session.commit()

    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'general':
            s.watched_folder        = request.form.get('watched_folder', './projects')
            s.notifications_enabled = 'notifications_enabled' in request.form
            s.auto_backup           = 'auto_backup' in request.form
            db.session.commit()
            flash('Settings saved.', 'success')
        elif action == 'scoring':
            db.session.commit()
            flash('Scoring updated.', 'success')
        elif action == 'toggle_watcher':
            if is_watching():
                stop_watcher(); s.watcher_active = False
                flash('Watcher stopped.', 'success')
            else:
                fn      = _make_capture_fn(bool(s.auto_backup))
                folders = _watched_folders_for_user(current_user.id)
                if not folders:
                    flash('No valid folders to watch. '
                          'Add a project with a known file path first.', 'error')
                else:
                    start_watcher(list(folders), fn)
                    s.watcher_active = True
                    flash(f'Watcher started — monitoring {len(folders)} folder(s).'
                          f' Backups save to ~/Documents/PLMS_Backups.', 'success')
            db.session.commit()
        elif action == 'demo':
            _load_demo(current_user.id)
            flash('Demo projects loaded.', 'success')
        elif action == 'password':
            cur = request.form.get('current_password', '')
            new = request.form.get('new_password',     '')
            if current_user.check_password(cur) and len(new) >= 6:
                current_user.set_password(new); db.session.commit()
                flash('Password updated.', 'success')
            else:
                flash('Incorrect password or too short.', 'error')
        return redirect(url_for('settings'))

    db_size      = os.path.getsize('plms.db') // 1024 if os.path.exists('plms.db') else 0
    backup_root  = _backup_root()
    watched_flds = _watched_folders_for_user(current_user.id) if s.watcher_active else set()
    return render_template('settings.html', s=s, db_size=db_size,
                           backup_root=backup_root,
                           watched_folders=watched_flds)


if __name__ == '__main__':
    app.run(debug=True, use_reloader=False, port=5000)
