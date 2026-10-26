from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import json

db = SQLAlchemy()


class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(80),  unique=True, nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role          = db.Column(db.String(20),  default='producer')
    created_at    = db.Column(db.DateTime,    default=datetime.utcnow)
    projects      = db.relationship('Project',     backref='owner', lazy=True,
                                    cascade='all, delete-orphan')
    settings      = db.relationship('AppSettings', backref='user',  uselist=False,
                                    cascade='all, delete-orphan')

    def set_password(self, p):   self.password_hash = generate_password_hash(p)
    def check_password(self, p): return check_password_hash(self.password_hash, p)

    @property
    def initials(self): return self.username[:2].upper()


class Project(db.Model):
    __tablename__ = 'projects'
    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    name        = db.Column(db.String(200), nullable=False)
    # filepath    = server-side copy (inside projects/<id>/)
    filepath    = db.Column(db.String(500), default='')
    # source_path = the REAL path on the user's machine where FL Studio saves
    # This is what the watcher matches against. Set when project is created
    # with a typed path, or updated each time a manual upload overwrites it.
    source_path = db.Column(db.String(500), default='')
    description = db.Column(db.Text,        default='')
    genre       = db.Column(db.String(100), default='')
    is_demo     = db.Column(db.Boolean,     default=False)
    created_at  = db.Column(db.DateTime,    default=datetime.utcnow)
    versions    = db.relationship('Version', backref='project', lazy=True,
                                  cascade='all, delete-orphan',
                                  order_by='Version.version_num')
    rollbacks   = db.relationship('RollbackEvent', backref='project', lazy=True,
                                  cascade='all, delete-orphan',
                                  order_by='RollbackEvent.id.desc()')

    @property
    def version_count(self):  return len(self.versions)
    @property
    def latest_version(self): return self.versions[-1] if self.versions else None
    @property
    def latest_score(self):
        lv = self.latest_version; return lv.quality_score if lv else 0.0
    @property
    def score_trend(self):
        if len(self.versions) < 2: return 'neutral'
        d = self.versions[-1].quality_score - self.versions[-2].quality_score
        return 'up' if d > 2 else ('down' if d < -2 else 'neutral')
    @property
    def score_class(self):
        s = self.latest_score
        return 'score-high' if s >= 70 else ('score-mid' if s >= 40 else 'score-low')

    @property
    def watch_filename(self):
        """Lowercase filename used for fuzzy watcher matching."""
        import os
        p = self.source_path or self.filepath
        return os.path.basename(p).lower() if p else ''


class Version(db.Model):
    __tablename__ = 'versions'
    id               = db.Column(db.Integer, primary_key=True)
    project_id       = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    version_num      = db.Column(db.Integer, nullable=False)
    tempo            = db.Column(db.Float,   default=120.0)
    channel_count    = db.Column(db.Integer, default=0)
    pattern_count    = db.Column(db.Integer, default=0)
    sample_count     = db.Column(db.Integer, default=0)
    # arrangement (playlist) data — FR-08 / FR-10
    playlist_items   = db.Column(db.Integer, default=0)
    tracks_used      = db.Column(db.Integer, default=0)
    arrangement_bars = db.Column(db.Float,   default=0.0)
    complexity       = db.Column(db.Float,   default=0.0)
    arrangement_json = db.Column(db.Text,    default='')
    breakdown_json   = db.Column(db.Text,    default='')   # score breakdown as computed at capture
    quality_score    = db.Column(db.Float,   default=0.0)
    changes_json     = db.Column(db.Text,    default='{}')
    backup_path      = db.Column(db.String(500), default='')
    # integrity (NFR-05 / NFR-11): SHA-256 of captured file and of its backup copy
    file_hash        = db.Column(db.String(64),  default='')
    backup_hash      = db.Column(db.String(64),  default='')
    # 'capture' (watcher) | 'upload' | 'rollback' | 'demo'
    origin           = db.Column(db.String(20),  default='capture')
    restored_from    = db.Column(db.Integer,     nullable=True)
    notes            = db.Column(db.Text,        default='')
    captured_at      = db.Column(db.DateTime,    default=datetime.utcnow)

    @property
    def changes(self):
        try:    return json.loads(self.changes_json or '{}')
        except Exception: return {}

    @property
    def arrangement(self):
        try:    return json.loads(self.arrangement_json) if self.arrangement_json else {}
        except Exception: return {}

    @property
    def breakdown(self):
        try:    return json.loads(self.breakdown_json) if self.breakdown_json else None
        except Exception: return None

    @property
    def has_arrangement(self):
        return bool(self.arrangement)

    @property
    def sample_total(self):
        """sample_count may be text on databases migrated by older PLMS builds."""
        try:    return int(self.sample_count or 0)
        except (TypeError, ValueError): return 0

    @property
    def score_class(self):
        return 'score-high' if self.quality_score >= 70 else \
               ('score-mid' if self.quality_score >= 40 else 'score-low')

    def metrics(self) -> dict:
        """Dict form used by ChangeEngine / ScoringEngine."""
        return dict(version_num=self.version_num, tempo=self.tempo,
                    channel_count=self.channel_count, pattern_count=self.pattern_count,
                    sample_count=self.sample_total, playlist_items=self.playlist_items or 0,
                    tracks_used=self.tracks_used or 0,
                    arrangement_bars=self.arrangement_bars or 0.0,
                    quality_score=self.quality_score,
                    has_arrangement=self.has_arrangement, arrangement=self.arrangement)

    def to_dict(self):
        return dict(id=self.id, version_num=self.version_num, tempo=self.tempo,
                    channel_count=self.channel_count, pattern_count=self.pattern_count,
                    sample_count=self.sample_total, playlist_items=self.playlist_items or 0,
                    tracks_used=self.tracks_used or 0,
                    arrangement_bars=self.arrangement_bars or 0.0,
                    complexity=self.complexity or 0.0,
                    quality_score=self.quality_score, changes=self.changes,
                    origin=self.origin or 'capture', restored_from=self.restored_from,
                    notes=self.notes or '',
                    captured_at=self.captured_at.strftime('%Y-%m-%d %H:%M'))


class RollbackEvent(db.Model):
    """Audit trail of every rollback attempt (FR-24)."""
    __tablename__ = 'rollback_events'
    id                 = db.Column(db.Integer, primary_key=True)
    project_id         = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    from_version_num   = db.Column(db.Integer)           # latest version before rollback
    to_version_num     = db.Column(db.Integer)           # version restored
    new_version_num    = db.Column(db.Integer)           # version recorded for the restore
    safety_backup_path = db.Column(db.String(500), default='')
    status             = db.Column(db.String(12),  default='success')   # success | failed
    message            = db.Column(db.Text,        default='')
    created_at         = db.Column(db.DateTime,    default=datetime.utcnow)


class Notification(db.Model):
    """In-app notifications (FR-27..FR-31). Created by request threads and the watcher."""
    __tablename__ = 'notifications'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    project_id = db.Column(db.Integer, nullable=True)
    kind       = db.Column(db.String(30))     # capture | score | prediction | rollback | error
    level      = db.Column(db.String(10), default='success')   # success | info | error
    message    = db.Column(db.String(300))
    read       = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class AppSettings(db.Model):
    __tablename__ = 'app_settings'
    id                       = db.Column(db.Integer, primary_key=True)
    user_id                  = db.Column(db.Integer, db.ForeignKey('users.id'),
                                         nullable=False, unique=True)
    watched_folder           = db.Column(db.String(500), default='./projects')
    watcher_active           = db.Column(db.Boolean,     default=False)
    notifications_enabled    = db.Column(db.Boolean,     default=True)
    auto_backup              = db.Column(db.Boolean,     default=True)
    score_weight_tempo       = db.Column(db.Float, default=20.0)
    score_weight_channels    = db.Column(db.Float, default=25.0)
    score_weight_patterns    = db.Column(db.Float, default=25.0)
    score_weight_samples     = db.Column(db.Float, default=15.0)
    score_weight_maturity    = db.Column(db.Float, default=15.0)

    @property
    def weights(self) -> dict:
        return dict(tempo=self.score_weight_tempo, channels=self.score_weight_channels,
                    patterns=self.score_weight_patterns,
                    samples=self.score_weight_samples if self.score_weight_samples is not None else 15.0,
                    maturity=self.score_weight_maturity)
