import os, sys, tempfile, shutil
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_tmp = tempfile.mkdtemp(prefix='plms_test_')
os.environ['PLMS_DATABASE_URI'] = f'sqlite:///{_tmp}/test.db'
os.environ['PLMS_BACKUP_ROOT']  = os.path.join(_tmp, 'backups')
os.environ['PLMS_SECRET_KEY']   = 'test-secret'

import app as plms                      # noqa: E402
from models import db                   # noqa: E402

SAMPLES = os.path.join(ROOT, 'samples')


def sample(name):
    with open(os.path.join(SAMPLES, name), 'rb') as f:
        return f.read()


@pytest.fixture()
def app(monkeypatch, tmp_path):
    plms.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    monkeypatch.chdir(tmp_path)          # uploads write to ./projects
    with plms.app.app_context():
        db.drop_all(); db.create_all()
    yield plms.app
    with plms.app.app_context():
        db.session.remove()


@pytest.fixture()
def client(app):
    return app.test_client()


def register(client, name='alice'):
    return client.post('/register', data=dict(username=name, email=f'{name}@x.com',
                                              password='secret1', confirm_password='secret1'),
                       follow_redirects=True)


@pytest.fixture()
def alice(client):
    register(client, 'alice')
    return client


def make_project(client, name='Track', flp='sample_small.flp', source_path=''):
    data = dict(name=name, source_path=source_path)
    if flp:
        import io
        data['flp_file'] = (io.BytesIO(sample(flp)), 'track.flp')
    r = client.post('/project/new', data=data, content_type='multipart/form-data')
    assert r.status_code == 302
    return int(r.headers['Location'].rstrip('/').split('/')[-1])


def upload(client, pid, flp):
    import io
    r = client.post(f'/api/project/{pid}/upload_version',
                    data=dict(flp_file=(io.BytesIO(sample(flp)), 'track.flp')),
                    content_type='multipart/form-data')
    return r.get_json()
