# PLMS — Production Lifecycle Management System

> Intelligent version tracking, quality scoring, and predictive insight for FL Studio music production at Kweza Arts.

---

## Overview

PLMS is a full-stack web application built in Python and Flask that wraps around FL Studio's production workflow. It automatically captures every project save, assigns a quality score, computes what changed between versions, and predicts where your track is headed — all through a clean, professional browser-based dashboard.

Built as an undergraduate final year project for **Kweza Arts**, a creative music organisation based in Malawi.

---

## Features

| Feature | Description |
|---|---|
| **Version Capture** | Automatically snapshots every FL Studio `.flp` save with timestamp and metadata |
| **Change Engine** | Computes measurable differences in tempo, channels, patterns, samples, playlist clips/tracks, arrangement length and structural complexity |
| **Compare Any Two Versions** | Pick any two versions: metric table, pattern/track additions and removals, score breakdown side by side |
| **Version Notes** | Annotate any version; notes are searchable and exported |
| **Search & Filter** | Filter history by text, `v3`, score range, date range, origin, has-note |
| **Export** | CSV and PDF version reports (respect the active filters) |
| **Notifications** | In-app toasts for capture, score, prediction, rollback and any failure |
| **Backup Integrity** | SHA-256 per capture and per backup; verified before every rollback; one-click *Verify backups* |
| **Quality Scoring** | Assigns a 0–100 score to each version using configurable production metrics |
| **Prediction Engine** | Uses linear regression to forecast how a track will evolve across future versions |
| **Version Timeline** | Horizontal scrollable DAW-inspired timeline showing every saved state |
| **Safe Rollback** | Confirmation modal, safety copy of the current file, checksum-verified restore, recorded as a new version, full audit trail |
| **Authentication** | Secure multi-user login with private workspaces per producer |
| **Demo Mode** | Sample projects clearly marked DEMO (no files, no rollback/upload); removable in Settings |

---

## Tech Stack

- **Backend** — Python 3.10+, Flask 3, SQLAlchemy, Flask-Login
- **Database** — SQLite (zero-config, file-based)
- **Analysis** — NumPy (least-squares trend, R²), built-in FLP event parser (`flp_parser.py`)
- **FLP Parsing** — pyflp (reads FL Studio project metadata)
- **File Watcher** — watchdog (real-time `.flp` save detection)
- **Frontend** — Jinja2 templates, Chart.js, Lucide Icons
- **Auth & security** — Werkzeug password hashing, Flask-Login sessions, Flask-WTF CSRF protection
- **Reports** — ReportLab (PDF)

---

## Getting Started

### Requirements

- Python 3.10 or higher
- pip
- FL Studio 20+ *(optional — app works fully without it)*

### Installation

```bash
# 1. Clone the repository
git clone https://github.com/your-username/plms-kweza-arts.git
cd plms-kweza-arts

# 2. Create and activate a virtual environment
python -m venv venv

# Windows
venv\Scripts\activate

# Mac / Linux
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Start the application
python app.py
```

Then open your browser at **http://localhost:5000**

> On first run, PLMS automatically creates `plms.db` — no manual database setup needed.

---

## Project Structure

```
plms/
├── app.py                  # Flask application — all routes
├── models.py               # SQLAlchemy models (User, Project, Version, AppSettings)
├── engines.py              # ChangeEngine, ScoringEngine, PredictionEngine
├── watcher.py              # watchdog file watcher for .flp saves
├── requirements.txt        # Python dependencies
│
├── templates/
│   ├── base.html           # Sidebar layout (authenticated shell)
│   ├── dashboard.html      # Projects overview
│   ├── project.html        # Project detail, timeline, charts
│   ├── settings.html       # Settings and configuration
│   └── auth/
│       ├── login.html      # Sign in page
│       └── register.html   # Create account page
│
└── static/
    ├── css/style.css       # Complete dark musical theme stylesheet
    └── js/app.js           # Charts, modals, toasts, rollback, sliders
```

---

## Usage

### Creating a Project

1. Register an account at `/register`
2. Click **New Project** in the dashboard or sidebar
3. Enter a name, genre, and the path to your `.flp` file
4. Use **Simulate Save** to add demo versions, or connect the file watcher

### Connecting FL Studio (Live Watcher)

1. Go to **Settings → General** and set the watched folder path to your FL Studio projects directory
2. Go to **Settings → File Watcher** and click **Start Watcher**
3. Save any `.flp` file inside FL Studio — a new version appears automatically within seconds

### Loading Demo Data

Go to **Settings → Database → Load Demo Projects** to instantly populate three realistic projects (*Summer Anthem*, *Midnight Groove*, *Afro Fusion Vol.1*) with full version histories.

---

## Quality Scoring

Each version receives a score from **0 to 100** calculated across four configurable dimensions:

| Dimension | Default Weight | Criteria |
|---|---|---|
| Tempo | 25 | BPM within professional production range (60–180) |
| Channels | 35 | Track richness — more channels = more layered production |
| Patterns | 25 | Arrangement complexity and pattern count |
| Maturity | 15 | Version count — more saves = more refinement |

Weights are fully adjustable in **Settings → Scoring Weights** to suit different genres.

---

## Predictions

Once a project has **3 or more versions**, the prediction engine uses ordinary least-squares regression to forecast:

- Expected quality score for the next version
- Predicted channel count growth
- Tempo stability or drift

Trend labels: `Growing` `Improving` `Stable` `Tapering` `Declining`

---

## Screenshots

> *Coming soon — run the app locally and navigate to http://localhost:5000 after loading demo data.*

---

## Academic Context

| | |
|---|---|
| **Project Title** | Production Lifecycle Management System for Digital Audio Workstations (DAWs) |
| **Student** | Mzati Nakoma |
| **Banner ID** | CIS/12/1510782 |
| **Programme** | BSc Hons Computing Information Systems |
| **Supervisor** | Mrs Funsani |
| **Institution** | University of Greenwich |
| **Client** | Kweza Arts |

---

## Scope

**Included:**
- FL Studio project version tracking
- Metadata extraction and storage
- Computational analysis of project changes
- Scoring and prediction algorithms
- Timeline visualisation
- Rollback system
- Multi-user authentication

**Excluded:**
- Full DAW editing functionality
- Audio mixing or rendering tools
- Cloud collaboration platforms
- Mobile application

---

## Dependencies

```
flask>=3.0.3
flask-sqlalchemy>=3.1.1
flask-login>=0.6.3
werkzeug>=3.0.3
watchdog>=4.0.1
numpy>=2.0.0
scikit-learn>=1.5.0
pyflp>=2.2.1
```

> **Python 3.13 users:** Use the version ranges above (no pinned versions). If `pyflp` fails to install, the app still works fully — only live `.flp` parsing is unavailable.

---

## License

This project was developed for academic purposes at the University of Greenwich in partnership with Kweza Arts. All rights reserved.

---

*Every track tells a story. PLMS helps you understand it.*


---

## Configuration

| Environment variable | Purpose | Default |
|---|---|---|
| `PLMS_SECRET_KEY` | Flask session/CSRF secret | random key generated once and stored in `instance/secret_key` |
| `PLMS_DATABASE_URI` | SQLAlchemy URI | `sqlite:///plms.db` (in `instance/`) |
| `PLMS_BACKUP_ROOT` | Where version backups are written | `~/Documents/PLMS_Backups` |

Existing databases are upgraded automatically on start-up (new columns are added; nothing is deleted).

## Tests and benchmark

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests -q            # 66 tests: parser, engines, API, rollback, security, migration
python benchmarks/bench.py           # NFR-01 (capture < 2 s) and NFR-06 (500+ versions) evidence
```

`samples/` holds three real `.flp` files used as test fixtures.

## Requirement → code → test

| Requirement | Where | Tests |
|---|---|---|
| FR-01..05 capture & history | `app._capture_version`, `watcher.py`, `flp_parser.py` | `test_app.py` capture, watcher, dedupe |
| FR-06..10 compare, arrangement, complexity | `ChangeEngine.compare`, `/api/project/<id>/compare`, `structural_complexity` | `test_parser_engines.py`, `test_app.py::test_compare_*` |
| FR-11..14 scoring, breakdown | `ScoringEngine`, Settings → Scoring | `test_scoring_*`, `test_breakdown_sums_to_total` |
| FR-15..18 prediction, patterns | `PredictionEngine`, `PatternAnalyzer` | `test_prediction_*`, `test_patterns_*` |
| FR-19..22 timeline, compare view | `project.html` | `test_project_page_shows_*` |
| FR-23..26 rollback | `/api/rollback`, `RollbackEvent` | `test_rollback_*` |
| FR-27..31 notifications | `notify()`, `/api/notifications` | `test_notifications_*` |
| Annotation / search / export | `/api/version/<id>/notes`, `_filter_versions`, `export_csv/pdf` | `test_notes_*`, `test_filtering`, `test_*_export` |
| NFR-01, NFR-06 performance | `benchmarks/bench.py` | `test_performance.py` |
| NFR-04/05/11/12 security & integrity | CSRF, SHA-256, ownership checks | `test_users_cannot_touch_*`, `test_csrf_*`, `test_rollback_aborts_*`, `test_verify_*` |
