"""dbdriver.py — SQLite connection layer (DEMO edition).

The DEMO edition uses a local SQLite file and can NEVER touch PostgreSQL: it refuses to start when a
PostgreSQL connection string is present in the environment. db.py talks to the database only through:
    acquire()  /  IntegrityError  /  prepare()   (the same three names the PostgreSQL driver offers).
"""
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager

import config

log = logging.getLogger("motabaa.db")

NAME = "sqlite"
SCHEMA_VERSION = 1
IntegrityError = sqlite3.IntegrityError

if sqlite3.sqlite_version_info < (3, 35):          # RETURNING / ON CONFLICT are used throughout
    raise RuntimeError(f"SQLite 3.35+ is required (found {sqlite3.sqlite_version}).")

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.abspath(config.env("DEMO_DB", os.path.join(HERE, "demo_data", "motabaa_demo.db")))
_tl = threading.local()


def _guard():
    """Fail safe: a demo must never be pointed at a real (PostgreSQL) database or at a non-demo file."""
    for name in ("MOTABAA_DATABASE_URL", "DATABASE_URL", "PGHOST", "PGDATABASE"):
        if os.getenv(name):
            raise RuntimeError(f"Refusing to start the DEMO: environment variable {name} is set. "
                               "The demo edition uses its own SQLite file and never touches PostgreSQL.")
    if not DB_PATH.endswith(".db") or os.path.basename(DB_PATH) in ("motabaa.db", "motabaa_faculty.db"):
        raise RuntimeError("The demo database file must be named *_demo.db (never a production file name).")


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    for p in ("PRAGMA foreign_keys = ON", "PRAGMA synchronous = NORMAL", "PRAGMA temp_store = MEMORY"):
        conn.execute(p)
    return conn


def _py(v):
    """numpy scalars (from pandas rows) -> plain Python values."""
    return v.item() if type(v).__module__ == "numpy" and hasattr(v, "item") else v


class Conn:
    """Same tiny interface as the PostgreSQL driver's wrapper: execute() / executemany()."""
    __slots__ = ("raw",)

    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        return self.raw.execute(sql, tuple(_py(p) for p in params))

    def executemany(self, sql, seq):
        return self.raw.executemany(sql, [tuple(_py(p) for p in row) for row in seq])


@contextmanager
def acquire():
    """One transaction on this thread's connection (commit on success, rollback on any error)."""
    raw = getattr(_tl, "conn", None)
    if raw is None:
        raw = _tl.conn = _connect()
    try:
        yield Conn(raw)
        raw.commit()
    except BaseException:
        raw.rollback()
        raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY, description TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS faculties (
    faculty_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name_ar      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name_en      TEXT NOT NULL DEFAULT '',
    years_count  INTEGER NOT NULL DEFAULT 4 CHECK (years_count BETWEEN 1 AND 7),
    email_domain TEXT NOT NULL DEFAULT '',
    latitude     REAL CHECK (latitude  BETWEEN -90  AND 90),
    longitude    REAL CHECK (longitude BETWEEN -180 AND 180),
    radius_m     INTEGER NOT NULL DEFAULT 50 CHECK (radius_m BETWEEN 1 AND 5000),
    notes        TEXT NOT NULL DEFAULT '',
    is_active    INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    CHECK ((latitude IS NULL) = (longitude IS NULL)));
CREATE TABLE IF NOT EXISTS university_roster (
    student_code  TEXT PRIMARY KEY,
    faculty_id    INTEGER NOT NULL REFERENCES faculties(faculty_id) ON DELETE RESTRICT,
    full_name     TEXT NOT NULL,
    faculty_email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    academic_year TEXT NOT NULL,
    department    TEXT NOT NULL,
    study_type    TEXT NOT NULL DEFAULT 'Regular' CHECK (study_type IN ('Regular','Affiliated')),
    section_no    INTEGER NOT NULL DEFAULT 1 CHECK (section_no >= 1));
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    student_code  TEXT UNIQUE REFERENCES university_roster(student_code) ON DELETE RESTRICT,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('student','doctor','dean','faculty_admin','admin')),
    full_name     TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0,1)),
    session_epoch INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    staff_code    TEXT,
    title         TEXT,
    specialization TEXT,
    phone         TEXT,
    recovery_email TEXT COLLATE NOCASE,
    faculty_id    INTEGER REFERENCES faculties(faculty_id) ON DELETE RESTRICT,
    CHECK ((role = 'student') = (student_code IS NOT NULL)),
    CHECK ((role = 'admin')   = (faculty_id IS NULL)));
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_staff_code ON users(staff_code) WHERE staff_code IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_recovery   ON users(recovery_email) WHERE recovery_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_users_faculty ON users(faculty_id, role);
CREATE TABLE IF NOT EXISTS courses (
    course_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    faculty_id    INTEGER NOT NULL REFERENCES faculties(faculty_id) ON DELETE RESTRICT,
    course_name   TEXT NOT NULL,
    doctor_name   TEXT NOT NULL,
    doctor_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    academic_year TEXT NOT NULL,
    semester      TEXT NOT NULL,
    planned_lectures INTEGER NOT NULL DEFAULT 14 CHECK (planned_lectures > 0));
CREATE INDEX IF NOT EXISTS idx_courses_doctor   ON courses(doctor_id);
CREATE INDEX IF NOT EXISTS idx_courses_fac_year ON courses(faculty_id, academic_year);
CREATE TABLE IF NOT EXISTS lectures (
    lecture_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id         INTEGER NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    kind              TEXT NOT NULL DEFAULT 'Lecture' CHECK (kind IN ('Lecture','Section')),
    group_label       TEXT NOT NULL DEFAULT '',
    lecture_number    INTEGER NOT NULL CHECK (lecture_number > 0),
    topic_covered     TEXT NOT NULL,
    date_created      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    attendance_closed INTEGER NOT NULL DEFAULT 0 CHECK (attendance_closed IN (0,1)),
    attendance_code   TEXT,
    code_expires_at   TEXT,
    UNIQUE (course_id, kind, group_label, lecture_number));
CREATE INDEX IF NOT EXISTS idx_lectures_closed ON lectures(course_id, attendance_closed, kind);
CREATE TABLE IF NOT EXISTS attendance (
    attendance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_code  TEXT NOT NULL REFERENCES university_roster(student_code) ON DELETE RESTRICT,
    lecture_id    INTEGER NOT NULL REFERENCES lectures(lecture_id) ON DELETE CASCADE,
    status        TEXT NOT NULL CHECK (status IN ('Present','Absent')),
    timestamp     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    note          TEXT NOT NULL DEFAULT '',
    UNIQUE (student_code, lecture_id));
CREATE INDEX IF NOT EXISTS idx_att_lecture ON attendance(lecture_id);
CREATE TABLE IF NOT EXISTS attendance_attempts (
    student_code TEXT NOT NULL REFERENCES university_roster(student_code) ON DELETE CASCADE,
    lecture_id   INTEGER NOT NULL REFERENCES lectures(lecture_id) ON DELETE CASCADE,
    fails        INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    PRIMARY KEY (student_code, lecture_id));
CREATE TABLE IF NOT EXISTS private_planner (
    task_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    student_code  TEXT NOT NULL REFERENCES university_roster(student_code) ON DELETE CASCADE,
    task_title    TEXT NOT NULL,
    is_completed  INTEGER NOT NULL DEFAULT 0 CHECK (is_completed IN (0,1)),
    date_created  TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE INDEX IF NOT EXISTS idx_planner_student ON private_planner(student_code);
CREATE TABLE IF NOT EXISTS complaints (
    complaint_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    student_code  TEXT REFERENCES university_roster(student_code) ON DELETE SET NULL,
    type          TEXT NOT NULL,
    content       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending','Under Review','Resolved')),
    timestamp     TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    kind          TEXT NOT NULL DEFAULT 'Complaint' CHECK (kind IN ('Complaint','Suggestion')),
    response      TEXT NOT NULL DEFAULT '',
    faculty_id    INTEGER REFERENCES faculties(faculty_id) ON DELETE RESTRICT);
CREATE INDEX IF NOT EXISTS idx_complaints_fac  ON complaints(faculty_id, status);
CREATE INDEX IF NOT EXISTS idx_complaints_stat ON complaints(status);
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_code   TEXT NOT NULL,
    receiver_code TEXT NOT NULL,
    course_id     INTEGER NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    message_text  TEXT NOT NULL,
    timestamp     TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE INDEX IF NOT EXISTS idx_chat_lookup ON chat_messages(course_id, sender_code, receiver_code);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id  INTEGER NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    lecture_id INTEGER NOT NULL REFERENCES lectures(lecture_id) ON DELETE CASCADE,
    alert_type TEXT NOT NULL DEFAULT 'quiz',
    priority   TEXT NOT NULL DEFAULT 'high',
    timestamp  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE (lecture_id, alert_type));
CREATE INDEX IF NOT EXISTS idx_alerts_course ON alerts(course_id);
CREATE TABLE IF NOT EXISTS course_settings (
    course_id INTEGER PRIMARY KEY REFERENCES courses(course_id) ON DELETE CASCADE,
    chat_open INTEGER NOT NULL DEFAULT 1 CHECK (chat_open IN (0,1)));
CREATE TABLE IF NOT EXISTS assignments (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id     INTEGER NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    doctor_id     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('Quiz','Project')),
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    link          TEXT NOT NULL DEFAULT '',
    due_at        TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE INDEX IF NOT EXISTS idx_assign_course ON assignments(course_id, due_at);
CREATE TABLE IF NOT EXISTS halls (
    hall_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    faculty_id INTEGER NOT NULL REFERENCES faculties(faculty_id) ON DELETE RESTRICT,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'Amphitheater' CHECK (kind IN ('Amphitheater','Section Room')),
    capacity   INTEGER NOT NULL DEFAULT 0 CHECK (capacity >= 0),
    latitude   REAL CHECK (latitude  BETWEEN -90  AND 90),
    longitude  REAL CHECK (longitude BETWEEN -180 AND 180),
    radius_m   INTEGER NOT NULL DEFAULT 30 CHECK (radius_m BETWEEN 1 AND 5000),
    UNIQUE (faculty_id, name),
    CHECK ((latitude IS NULL) = (longitude IS NULL)));
CREATE INDEX IF NOT EXISTS idx_halls_faculty ON halls(faculty_id);
CREATE TABLE IF NOT EXISTS schedule (
    slot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    course_id    INTEGER NOT NULL REFERENCES courses(course_id) ON DELETE CASCADE,
    hall_id      INTEGER NOT NULL REFERENCES halls(hall_id) ON DELETE RESTRICT,
    session_type TEXT NOT NULL CHECK (session_type IN ('Lecture','Section')),
    group_label  TEXT NOT NULL DEFAULT '',
    day_of_week  INTEGER NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    start_time   TEXT NOT NULL CHECK (length(start_time) = 5),
    end_time     TEXT NOT NULL CHECK (length(end_time) = 5),
    CHECK (start_time < end_time));
CREATE INDEX IF NOT EXISTS idx_schedule_hall   ON schedule(hall_id, day_of_week);
CREATE INDEX IF NOT EXISTS idx_schedule_course ON schedule(course_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    timestamp  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    faculty_id INTEGER);
CREATE INDEX IF NOT EXISTS idx_audit_fac ON audit_log(faculty_id, id);
CREATE TABLE IF NOT EXISTS password_resets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash  TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    used       INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE INDEX IF NOT EXISTS idx_resets_user ON password_resets(user_id, created_at);
CREATE TABLE IF NOT EXISTS activation_codes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    student_code TEXT NOT NULL REFERENCES university_roster(student_code) ON DELETE CASCADE,
    code_hash    TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    used         INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0,1)),
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')));
CREATE INDEX IF NOT EXISTS idx_activation_student ON activation_codes(student_code, created_at);
CREATE TABLE IF NOT EXISTS login_attempts (
    key          TEXT PRIMARY KEY,
    fails        INTEGER NOT NULL DEFAULT 0,
    locks        INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    updated_at   TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_roster_year_dept ON university_roster(academic_year, department);
CREATE INDEX IF NOT EXISTS idx_roster_section   ON university_roster(academic_year, section_no);
CREATE INDEX IF NOT EXISTS idx_roster_fac_year  ON university_roster(faculty_id, academic_year);
CREATE VIEW IF NOT EXISTS v_attendance_matrix AS
SELECT r.student_code, r.full_name, r.department, r.academic_year, r.study_type, c.faculty_id,
       c.course_id, c.course_name, l.kind, l.group_label,
       l.lecture_id, l.lecture_number,
       SUBSTR(CAST(l.date_created AS TEXT), 1, 10) AS lecture_date,
       COALESCE(a.status, 'Absent')                AS status
FROM courses c
JOIN university_roster r ON r.academic_year = c.academic_year AND r.faculty_id = c.faculty_id
JOIN lectures l          ON l.course_id = c.course_id AND l.attendance_closed = 1
                        AND (l.kind = 'Lecture' OR l.group_label = CAST(r.section_no AS TEXT))
LEFT JOIN attendance a   ON a.lecture_id = l.lecture_id AND a.student_code = r.student_code;
INSERT OR IGNORE INTO schema_version(version, description) VALUES (1, 'initial demo schema');
"""


def prepare():
    """Create the demo database on first start (SQLite, WAL) and mark it as a DEMO database."""
    _guard()
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")      # readers never block the writer; persists in the file
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('demo_edition', '1')")
        conn.commit()
    finally:
        conn.close()
    log.info("SQLite demo database ready: %s", DB_PATH)
