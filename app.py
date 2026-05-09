import os, json, shutil, random, logging
from datetime import datetime
from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, flash)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)
from models  import db, User, Project, Version, AppSettings
from engines import ChangeEngine, ScoringEngine, PredictionEngine
from watcher import start_watcher, stop_watcher, is_watching

# Configure logging for the main app
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')

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

with app.app_context(): db.create_all()

# ── Helpers ────────────────────────────────────────────────────
def _build_version(project, data=None):
    prev = project.latest_version
    vnum = (prev.version_num + 1) if prev else 1
    if data:
        tempo    = float(data.get('tempo', 120.0))
        channels = int(data.get('channel_count', 0))
        patterns = int(data.get('pattern_count', 0))
    else:
        base_t   = prev.tempo if prev else random.uniform(80, 140)
        tempo    = round(base_t + random.uniform(-2, 2), 1)
        channels = min((prev.channel_count if prev else 0) + random.randint(0, 3), 32)
        patterns = min((prev.pattern_count if prev else 0) + random.randint(0, 2), 20)
    vdata   = dict(tempo=tempo, channel_count=channels, pattern_count=patterns, version_num=vnum)
    score   = ScoringEngine().calculate(vdata)
    changes = {}
    if prev:
        changes = ChangeEngine().compute(
            dict(tempo=prev.tempo, channel_count=prev.channel_count, pattern_count=prev.pattern_count),
            vdata)
    v = Version(project_id=project.id, version_num=vnum, tempo=tempo,
                channel_count=channels, pattern_count=patterns,
                quality_score=score, changes_json=json.dumps(changes))
    db.session.add(v); db.session.commit()
    return v

def _parse_flp(fp):
    try:
        import pyflp; p = pyflp.parse(fp)
        return dict(tempo=float(p.tempo), channel_count=len(list(p.channels)),
                    pattern_count=len(list(p.patterns)))
    except Exception as e:
        logging.warning(f"Failed to parse {fp}: {e}")
        return dict(tempo=120.0, channel_count=0, pattern_count=0)

def _load_demo(uid):
    demos = [
        dict(name='Summer Anthem',    genre='Afrobeats',  description='Upbeat track with traditional percussion',
             vs=[(102,4,3),(102,7,5),(104,10,7),(104,13,9),(105,16,11),(105,18,12)]),
        dict(name='Midnight Groove',  genre='Amapiano',   description='Deep bass log drum exploration',
             vs=[(112,3,2),(112,6,4),(113,9,6),(113,12,8)]),
        dict(name='Afro Fusion Vol.1',genre='Afro-fusion',description='Traditional meets electronic',
             vs=[(95,5,4),(95,9,6),(96,14,8),(96,18,11),(97,20,13),(97,22,14),(97,24,15)]),
    ]
    sc = ScoringEngine(); ch = ChangeEngine()
    for pd in demos:
        proj = Project(user_id=uid, name=pd['name'], genre=pd['genre'],
                       description=pd['description'],
                       filepath=f"./projects/{pd['name'].lower().replace(' ','_')}.flp")
        db.session.add(proj); db.session.flush()
        prev = None
        for i,(t,c,p) in enumerate(pd['vs'],1):
            vd = dict(tempo=t,channel_count=c,pattern_count=p,version_num=i)
            changes = ch.compute(dict(tempo=prev.tempo,channel_count=prev.channel_count,
                                      pattern_count=prev.pattern_count),vd) if prev else {}
            v = Version(project_id=proj.id,version_num=i,tempo=t,channel_count=c,
                        pattern_count=p,quality_score=sc.calculate(vd),
                        changes_json=json.dumps(changes))
            db.session.add(v); prev = v
    db.session.commit()

# ── Auth ───────────────────────────────────────────────────────
@app.route('/login', methods=['GET','POST'])
def auth_login():
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        ident = request.form.get('identifier','').strip()
        pw    = request.form.get('password','')
        user  = User.query.filter((User.username==ident)|(User.email==ident)).first()
        if user and user.check_password(pw):
            login_user(user, remember=bool(request.form.get('remember')))
            return redirect(request.args.get('next') or url_for('dashboard'))
        flash('Invalid username or password.', 'error')
    return render_template('auth/login.html')

@app.route('/register', methods=['GET','POST'])
def auth_register():
    if current_user.is_authenticated: return redirect(url_for('dashboard'))
    if request.method == 'POST':
        uname = request.form.get('username','').strip()
        email = request.form.get('email','').strip().lower()
        pw    = request.form.get('password','')
        cpw   = request.form.get('confirm_password','')
        errs  = []
        if len(uname) < 3:                          errs.append('Username must be at least 3 characters.')
        if '@' not in email:                        errs.append('A valid email address is required.')
        if len(pw) < 6:                             errs.append('Password must be at least 6 characters.')
        if pw != cpw:                               errs.append('Passwords do not match.')
        if User.query.filter_by(username=uname).first(): errs.append('Username already taken.')
        if User.query.filter_by(email=email).first():    errs.append('Email already registered.')
        if errs:
            for e in errs: flash(e,'error')
        else:
            u = User(username=uname, email=email); u.set_password(pw)
            db.session.add(u); db.session.commit()
            db.session.add(AppSettings(user_id=u.id)); db.session.commit()
            login_user(u)
            flash('Account created. Welcome to PLMS.','success')
            return redirect(url_for('dashboard'))
    return render_template('auth/register.html')

@app.route('/logout')
@login_required
def auth_logout(): logout_user(); return redirect(url_for('auth_login'))

# ── Dashboard ──────────────────────────────────────────────────
@app.route('/')
@login_required
def dashboard():
    projects = Project.query.filter_by(user_id=current_user.id)\
                   .order_by(Project.created_at.desc()).all()
    total_v  = sum(p.version_count for p in projects)
    scored   = [p.latest_score for p in projects if p.latest_score > 0]
    avg      = round(sum(scored)/len(scored),1) if scored else 0.0
    return render_template('dashboard.html', projects=projects,
                           total_projects=len(projects), total_versions=total_v,
                           avg_score=avg, watcher_active=is_watching())

# ── Projects ───────────────────────────────────────────────────
@app.route('/project/new', methods=['POST'])
@login_required
def create_project():
    name = request.form.get('name','').strip()
    if not name: flash('Project name is required.','error'); return redirect(url_for('dashboard'))
    proj = Project(user_id=current_user.id, name=name,
                   description=request.form.get('description','').strip(),
                   filepath=request.form.get('filepath','').strip(),
                   genre=request.form.get('genre','').strip())
    db.session.add(proj); db.session.commit()
    flash(f'Project "{name}" created.','success')
    return redirect(url_for('project_detail', project_id=proj.id))

@app.route('/project/<int:project_id>')
@login_required
def project_detail(project_id):
    proj  = Project.query.filter_by(id=project_id, user_id=current_user.id).first_or_404()
    preds = PredictionEngine().predict_all(proj.versions) if len(proj.versions) >= 3 else {}
    chart = dict(labels=[f'v{v.version_num}' for v in proj.versions],
                 scores=[v.quality_score for v in proj.versions],
                 channels=[v.channel_count for v in proj.versions],
                 tempos=[v.tempo for v in proj.versions],
                 patterns=[v.pattern_count for v in proj.versions])
    return render_template('project.html', project=proj,
                           predictions=preds, chart_data=json.dumps(chart),
                           watcher_active=is_watching())

@app.route('/project/<int:project_id>/delete', methods=['POST'])
@login_required
def delete_project(project_id):
    proj = Project.query.filter_by(id=project_id, user_id=current_user.id).first_or_404()
    name = proj.name; db.session.delete(proj); db.session.commit()
    flash(f'Project "{name}" deleted.','success'); return redirect(url_for('dashboard'))

# ── API ────────────────────────────────────────────────────────
@app.route('/api/rollback', methods=['POST'])
@login_required
def api_rollback():
    data    = request.get_json(silent=True) or {}
    proj    = Project.query.filter_by(id=data.get('project_id'),
                                      user_id=current_user.id).first_or_404()
    ver     = Version.query.filter_by(project_id=proj.id,
                                      version_num=data.get('version_num')).first_or_404()
    msg = f'Rolled back to version {ver.version_num}.'
    if ver.backup_path and os.path.exists(ver.backup_path):
        shutil.copy2(ver.backup_path, proj.filepath)
    else:
        msg += ' (demo mode — no backup file on disk)'
    return jsonify(success=True, message=msg)

@app.route('/api/project/<int:project_id>/latest')
@login_required
def api_latest(project_id):
    proj = Project.query.filter_by(id=project_id,
                                   user_id=current_user.id).first_or_404()
    lv   = proj.latest_version
    return jsonify({
        'version_count': proj.version_count,
        'latest_version': lv.version_num   if lv else 0,
        'latest_score':   lv.quality_score if lv else 0,
    })

# ── Settings ───────────────────────────────────────────────────
@app.route('/settings', methods=['GET','POST'])
@login_required
def settings():
    s = AppSettings.query.filter_by(user_id=current_user.id).first()
    if not s:
        s = AppSettings(user_id=current_user.id); db.session.add(s); db.session.commit()
    if request.method == 'POST':
        action = request.form.get('action','')
        if action == 'general':
            s.watched_folder        = request.form.get('watched_folder','./projects')
            s.notifications_enabled = 'notifications_enabled' in request.form
            s.auto_backup           = 'auto_backup' in request.form
            db.session.commit(); flash('General settings saved.','success')
        elif action == 'scoring':
            s.score_weight_tempo    = float(request.form.get('w_tempo',25))
            s.score_weight_channels = float(request.form.get('w_channels',35))
            s.score_weight_patterns = float(request.form.get('w_patterns',25))
            s.score_weight_maturity = float(request.form.get('w_maturity',15))
            db.session.commit(); flash('Scoring weights updated.','success')
        elif action == 'toggle_watcher':
            if is_watching():
                stop_watcher()
                s.watcher_active = False
                flash('Watcher stopped.','success')
            else:
                # Real callback that captures versions when an .flp changes
                def on_flp_change(filepath):
                    with app.app_context():
                        # Find project by exact filepath (case‑insensitive)
                        proj = Project.query.filter(db.func.lower(Project.filepath) == filepath.lower()).first()
                        if not proj:
                            logging.info(f"Watcher: No project found for {filepath}")
                            return
                        data = _parse_flp(filepath)
                        if not data or data['channel_count'] == 0 and data['pattern_count'] == 0:
                            logging.warning(f"Watcher: Failed to parse or empty metadata for {filepath}")
                            return
                        v = _build_version(proj, data=data)
                        logging.info(f"Watcher: Captured version {v.version_num} for project '{proj.name}' (score {v.quality_score})")
                        # Optional: auto‑backup if enabled
                        if s.auto_backup and os.path.exists(filepath):
                            backup_dir = os.path.join(os.path.dirname(filepath), 'plms_backups')
                            os.makedirs(backup_dir, exist_ok=True)
                            backup_path = os.path.join(backup_dir, f"{proj.name}_v{v.version_num}.flp")
                            shutil.copy2(filepath, backup_path)
                            v.backup_path = backup_path
                            db.session.commit()
                start_watcher(s.watched_folder, on_flp_change)
                s.watcher_active = True
                flash('Watcher started. Real‑time capture active.','success')
            db.session.commit()
        elif action == 'demo':
            _load_demo(current_user.id); flash('Demo projects loaded.','success')
        elif action == 'password':
            cur = request.form.get('current_password','')
            new = request.form.get('new_password','')
            if current_user.check_password(cur) and len(new) >= 6:
                current_user.set_password(new); db.session.commit()
                flash('Password updated.','success')
            else: flash('Incorrect password or new password too short.','error')
        return redirect(url_for('settings'))
    db_size = os.path.getsize('plms.db')//1024 if os.path.exists('plms.db') else 0
    return render_template('settings.html', s=s, watcher_active=is_watching(), db_size=db_size)

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False, port=5000)