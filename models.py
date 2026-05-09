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
    projects      = db.relationship('Project',    backref='owner', lazy=True,
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
    filepath    = db.Column(db.String(500), default='')
    description = db.Column(db.Text,        default='')
    genre       = db.Column(db.String(100), default='')
    created_at  = db.Column(db.DateTime,    default=datetime.utcnow)
    versions    = db.relationship('Version', backref='project', lazy=True,
                                  cascade='all, delete-orphan',
                                  order_by='Version.version_num')

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


class Version(db.Model):
    __tablename__ = 'versions'
    id            = db.Column(db.Integer, primary_key=True)
    project_id    = db.Column(db.Integer, db.ForeignKey('projects.id'), nullable=False)
    version_num   = db.Column(db.Integer, nullable=False)
    tempo         = db.Column(db.Float,   default=120.0)
    channel_count = db.Column(db.Integer, default=0)
    pattern_count = db.Column(db.Integer, default=0)
    quality_score = db.Column(db.Float,   default=0.0)
    changes_json  = db.Column(db.Text,    default='{}')
    backup_path   = db.Column(db.String(500), default='')
    notes         = db.Column(db.Text,    default='')
    captured_at   = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def changes(self):
        try:    return json.loads(self.changes_json)
        except: return {}
    @property
    def score_class(self):
        return 'score-high' if self.quality_score >= 70 else \
               ('score-mid' if self.quality_score >= 40 else 'score-low')
    def to_dict(self):
        return dict(id=self.id, version_num=self.version_num, tempo=self.tempo,
                    channel_count=self.channel_count, pattern_count=self.pattern_count,
                    quality_score=self.quality_score, changes=self.changes,
                    captured_at=self.captured_at.strftime('%Y-%m-%d %H:%M'))


class AppSettings(db.Model):
    __tablename__ = 'app_settings'
    id                       = db.Column(db.Integer, primary_key=True)
    user_id                  = db.Column(db.Integer, db.ForeignKey('users.id'),
                                         nullable=False, unique=True)
    watched_folder           = db.Column(db.String(500), default='./projects')
    watcher_active           = db.Column(db.Boolean,     default=False)
    notifications_enabled    = db.Column(db.Boolean,     default=True)
    auto_backup              = db.Column(db.Boolean,     default=True)
    score_weight_tempo       = db.Column(db.Float, default=25.0)
    score_weight_channels    = db.Column(db.Float, default=35.0)
    score_weight_patterns    = db.Column(db.Float, default=25.0)
    score_weight_maturity    = db.Column(db.Float, default=15.0)
