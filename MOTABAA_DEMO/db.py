"""db.py — data layer for Motabaa (identical in the PRODUCTION and DEMO editions).

The database engine is hidden behind dbdriver.py (production and demo use different engines).
Rules followed everywhere in this file:
  * ALL SQL uses ? placeholders (never string formatting of user input) -> no SQL injection. The few
    f-strings only join constant fragments defined in this file.
  * Portable SQL only (CASE instead of SUM(bool), RETURNING, ON CONFLICT, CAST) so ONE file serves both editions.
  * Faculty isolation is FAIL-CLOSED: a query that needs a faculty raises ScopeError when none was selected.
  * Every write that belongs to a doctor/student checks ownership here, not only in the UI.
  * This module does not import streamlit, so it can be tested on its own.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

import bcrypt
import pandas as pd
from geopy.distance import geodesic

import config
import dbdriver

log = logging.getLogger("motabaa.db")
IntegrityError = dbdriver.IntegrityError
FMT = "%Y-%m-%d %H:%M:%S"
_EMAIL_ANY = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_RESERVED_CODE = re.compile(r"^(DR|DN|FA|AD)\d+$", re.IGNORECASE)   # chat/staff identities: never a student code


def _ts(dt=None) -> str:
    return (dt or config.now()).strftime(FMT)


# bcrypt is CPU-heavy but releases the GIL: allow at most one hash per CPU core at a time, so a login
# storm queues up instead of starving every other request.
_BCRYPT_SLOTS = threading.BoundedSemaphore(max(1, os.cpu_count() or 1))
_DUMMY_HASH = bcrypt.hashpw(b"dummy", bcrypt.gensalt(rounds=config.BCRYPT_ROUNDS))  # equalises login timing

# ----------------------------------------------------------------------------
# Connection + faculty scope
# ----------------------------------------------------------------------------
_local = threading.local()


class ScopeError(RuntimeError):
    """A faculty-owned query ran without a faculty selected (fail-closed isolation)."""


def set_scope(faculty_id):
    """Faculty all following queries of this thread are limited to. None = deliberately EVERY faculty
    (nothing in the application uses it; kept for scripts and tests)."""
    _local.fid = int(faculty_id) if faculty_id else None


def clear_scope():
    """Forget the faculty (app.py calls this at the start of EVERY rerun)."""
    if hasattr(_local, "fid"):
        del _local.fid


def scope():
    if not hasattr(_local, "fid"):
        raise ScopeError("no faculty selected for this request")
    return _local.fid


def _peek_scope():
    """The current faculty or None — never raises (audit log, optional context)."""
    return getattr(_local, "fid", None)


def _sc():
    """(fid, fid) — the two parameters of `(? IS NULL OR col = ?)`. Raises when no faculty was selected."""
    f = scope()
    return (f, f)


_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def clean_digits(value) -> str:
    """Arabic-Indic / Persian digits -> 0-9, so codes typed on an Arabic keyboard work."""
    return str(value if value is not None else "").translate(_DIGITS)


@contextmanager
def get_conn():
    """This thread's transaction. Nested use is safe (only the outermost block commits / rolls back)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        yield conn
        return
    with dbdriver.acquire() as conn:
        _local.conn = conn
        try:
            yield conn
        finally:
            _local.conn = None


_CACHE: dict = {}      # per PROCESS: with several processes a change shows up everywhere within the TTL (15-30 s)


def _cached(key, ttl, fn):
    """Tiny per-process cache for rarely-changing data (faculties, university name)."""
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    val = fn()
    _CACHE[key] = (time.monotonic(), val)
    return val


def _rows(sql, params=()):
    with get_conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def _row(sql, params=()):
    with get_conn() as c:
        r = c.execute(sql, params).fetchone()
        return dict(r) if r else None


def _df(sql, params=()) -> pd.DataFrame:
    with get_conn() as c:
        cur = c.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [list(dict(r).values()) for r in cur.fetchall()]
    return pd.DataFrame(rows, columns=cols)


# SQL fragments shared by the analytics queries (constants, never user input).
def _present(col="status"):
    return f"SUM(CASE WHEN {col} = 'Present' THEN 1 ELSE 0 END)"


def _absent(col="status"):
    return f"SUM(CASE WHEN {col} = 'Absent' THEN 1 ELSE 0 END)"


def _rate(col="status"):
    return f"ROUND(100.0 * {_present(col)} / COUNT(*), 1)"


def init_db():
    """Start-up: check the schema (production) / create it (demo) and tidy old temporary rows."""
    dbdriver.prepare()
    housekeeping()


def has_admin() -> bool:
    return _row("SELECT 1 AS x FROM users WHERE role = 'admin' LIMIT 1") is not None


def housekeeping():
    """Delete expired temporary rows so the small tables never grow without limit."""
    now = config.now()
    day, week = _ts(now - timedelta(days=1)), _ts(now - timedelta(days=7))
    with get_conn() as c:
        c.execute("DELETE FROM login_attempts WHERE updated_at < ? AND (locked_until IS NULL OR locked_until < ?)",
                  (day, _ts(now)))
        c.execute("DELETE FROM attendance_attempts WHERE lecture_id IN "
                  "(SELECT lecture_id FROM lectures WHERE attendance_closed = 1)")
        c.execute("DELETE FROM password_resets WHERE created_at < ?", (week,))
        c.execute("DELETE FROM activation_codes WHERE created_at < ?", (week,))


def _maybe_housekeeping():
    if secrets.randbelow(300) == 0:
        try:
            housekeeping()
        except Exception:                                   # never break a request for housekeeping
            log.exception("housekeeping failed")


# ----------------------------------------------------------------------------
# Passwords & authentication
# ----------------------------------------------------------------------------
def hash_password(pw: str) -> str:
    with _BCRYPT_SLOTS:
        return bcrypt.hashpw(pw.encode("utf-8")[:72], bcrypt.gensalt(rounds=config.BCRYPT_ROUNDS)).decode()


def _check_password(pw: str, hashed: str) -> bool:
    try:
        with _BCRYPT_SLOTS:
            return bcrypt.checkpw(pw.encode("utf-8")[:72], hashed.encode())
    except ValueError:
        return False


_PREFIX = {"doctor": "DR", "dean": "DN", "faculty_admin": "FA", "admin": "AD"}      # one definition, used everywhere


def _user_dict(row):
    role = row["role"]
    code = row["student_code"] if role == "student" else f"{_PREFIX[role]}{row['id']}"
    return {
        "id": row["id"], "role": role, "email": row["email"], "code": code,
        "student_code": row["student_code"], "full_name": row["full_name"],
        "academic_year": row["academic_year"], "department": row["department"],
        "study_type": row["study_type"], "must_change": bool(row["must_change_password"]),
        "section_no": row["section_no"], "staff_code": row["staff_code"], "title": row["title"],
        "specialization": row["specialization"], "recovery_email": row["recovery_email"],
        "faculty_id": row["faculty_id"], "faculty_name_ar": row["faculty_name_ar"],
        "faculty_name_en": row["faculty_name_en"], "faculty_active": row["faculty_active"] != 0,
        "epoch": row["session_epoch"],
    }


_USER_SQL = """
SELECT u.id, u.email, u.role, u.student_code, u.password_hash, u.must_change_password, u.session_epoch,
       COALESCE(u.full_name, r.full_name) AS full_name,
       r.academic_year, r.department, r.study_type, r.section_no,
       u.staff_code, u.title, u.specialization, u.recovery_email,
       f.faculty_id, f.name_ar AS faculty_name_ar, f.name_en AS faculty_name_en, f.is_active AS faculty_active
FROM users u LEFT JOIN university_roster r ON r.student_code = u.student_code
             LEFT JOIN faculties f ON f.faculty_id = COALESCE(u.faculty_id, r.faculty_id)
"""


def login_key(identifier) -> str:
    """What the person typed in the login box, normalised: Arabic digits -> 0-9, trimmed, lower-case."""
    return clean_digits(identifier).strip().lower()[:200]


# ---- throttling (login, activation): per key, lock grows each time, old failures are forgotten -------------
def _is_locked(key: str) -> bool:
    r = _row("SELECT locked_until FROM login_attempts WHERE key = ?", (key,))
    return bool(r and r["locked_until"] and r["locked_until"] > _ts())


def _bump_fail(key: str, limit: int):
    """Count one failure for `key`; at `limit` failures lock it (10, 20, 40 ... minutes, max 2 h)."""
    now = config.now()
    with get_conn() as c:
        r = c.execute("SELECT fails, locks, locked_until, updated_at FROM login_attempts WHERE key = ?",
                      (key,)).fetchone()
        fails, locks = (r["fails"], r["locks"]) if r else (0, 0)
        if r and r["updated_at"] < _ts(now - timedelta(hours=1)) and not (r["locked_until"] and r["locked_until"] > _ts(now)):
            fails = 0                                     # an hour without failures: start counting again
        fails += 1
        lock = None
        if fails >= limit:
            locks += 1
            minutes = min(config.LOGIN_LOCK_MAX_MIN, config.LOGIN_LOCK_MIN * 2 ** (locks - 1))
            lock, fails = _ts(now + timedelta(minutes=minutes)), 0
        c.execute("""INSERT INTO login_attempts(key, fails, locks, locked_until, updated_at) VALUES (?,?,?,?,?)
                     ON CONFLICT(key) DO UPDATE SET fails = excluded.fails, locks = excluded.locks,
                                                    locked_until = excluded.locked_until,
                                                    updated_at = excluded.updated_at""",
                  (key, fails, locks, lock, _ts(now)))
    _maybe_housekeeping()


def login_locked(identifier: str, ip: str = "") -> bool:
    """Is this identifier (from this address, or from anywhere) temporarily locked?"""
    key = login_key(identifier)
    return bool(key) and (_is_locked(key) or (bool(ip) and _is_locked(f"{key}|{ip}")))


def _note_login_failure(key: str, ip: str):
    _bump_fail(key, config.LOGIN_MAX_FAILS_ANY_IP)       # distributed guessing against one account
    if ip:
        _bump_fail(f"{key}|{ip}", config.LOGIN_MAX_FAILS)    # one address guessing: locked much sooner


def _find_login_row(raw: str):
    """student code -> staff code -> e-mail: three exact lookups on unique indexes (never a table scan).
    Student codes keep their original case (they may be alphanumeric)."""
    raw = clean_digits(raw).strip()
    variants = list(dict.fromkeys((raw, raw.upper(), raw.lower())))
    for where, params in (("u.student_code IN (" + ",".join("?" * len(variants)) + ")", variants),
                          ("u.staff_code IN (?, ?)", [raw.upper(), raw]),
                          ("u.email = ?", [raw.lower()])):
        row = _row(_USER_SQL + f" WHERE {where} LIMIT 1", params)       # `where` is built from constants above
        if row is not None:
            return row
    return None


def authenticate(identifier: str, password: str, ip: str = ""):
    """Log in with the STUDENT CODE (students) or the STAFF CODE / e-mail (doctors, dean, admin).
    Returns the user dict, or None. A locked identifier is rejected WITHOUT running bcrypt (cheap), and the
    same work is done whether or not the account exists."""
    key = login_key(identifier)
    if not key or login_locked(identifier, ip):
        return None
    row = _find_login_row(identifier)
    if row is None:
        with _BCRYPT_SLOTS:
            bcrypt.checkpw(b"dummy", _DUMMY_HASH)
        _note_login_failure(key, ip)
        return None
    if _check_password(password, row["password_hash"]):
        if ip:
            with get_conn() as c:
                c.execute("DELETE FROM login_attempts WHERE key = ?", (f"{key}|{ip}",))
        log_action(row["email"], "login", "", faculty_id=row["faculty_id"])
        return _user_dict(row)
    _note_login_failure(key, ip)
    return None


def session_user(user_id):
    """The CURRENT state of an account (None if it no longer exists): app.py compares it with the session on
    every rerun, so a password reset / role change / disabled faculty takes effect immediately (SEC-04)."""
    row = _row(_USER_SQL + " WHERE u.id = ?", (user_id,))
    return _user_dict(row) if row else None


def session_epoch(user_id) -> int:
    r = _row("SELECT session_epoch AS e FROM users WHERE id = ?", (user_id,))
    return r["e"] if r else -1


# ---- student activation ------------------------------------------------------------------------------------
def _code_hash(who, salt, code) -> str:
    """Salted PBKDF2 of a short e-mailed code (slows offline guessing if the table ever leaks)."""
    return hashlib.pbkdf2_hmac("sha256", f"{who}:{code}".encode(), salt.encode(), 60_000).hex()


def verify_roster(code: str, email: str):
    """First-time check. Returns (status, roster_row). The row carries the faculty's names.
    The UI shows ONE generic message for every status except ok (no account enumeration)."""
    code = clean_digits(code).strip()
    r = _row("""SELECT r.*, f.name_ar AS faculty_name_ar, f.name_en AS faculty_name_en, f.is_active AS faculty_active
                FROM university_roster r JOIN faculties f ON f.faculty_id = r.faculty_id
                WHERE r.student_code = ?""", (code,))
    if r is None:
        return "not_found", None
    if not r["faculty_active"]:
        return "faculty_inactive", None
    if r["faculty_email"].lower() != email.strip().lower():
        return "email_mismatch", None
    if _row("SELECT 1 AS x FROM users WHERE student_code = ?", (code,)):
        return "already_registered", None
    return "ok", r


def activation_locked(code: str, ip: str = "") -> bool:
    k = clean_digits(code).strip()
    return _is_locked(f"act:{k}") or (bool(ip) and _is_locked(f"actip:{ip}"))


def activation_failed(code: str, ip: str = ""):
    _bump_fail(f"act:{clean_digits(code).strip()}", 8)
    if ip:
        _bump_fail(f"actip:{ip}", 30)


def start_activation(code: str, email: str):
    """After verify_roster succeeded: create an e-mailed code for the student's UNIVERSITY mailbox.
    Returns (status, plain_code, to_email). status: ok | rate_limited | invalid"""
    status, row = verify_roster(code, email)
    if status != "ok":
        return "invalid", None, None
    sc = row["student_code"]
    with get_conn() as c:
        recent = c.execute("SELECT COUNT(*) AS n FROM activation_codes WHERE student_code = ? AND created_at > ?",
                           (sc, _ts(config.now() - timedelta(hours=1)))).fetchone()["n"]
        if recent >= RESET_MAX_PER_HOUR:
            return "rate_limited", None, None
        c.execute("UPDATE activation_codes SET used = 1 WHERE student_code = ? AND used = 0", (sc,))
        plain, salt = f"{secrets.randbelow(10 ** 6):06d}", secrets.token_hex(8)
        c.execute("INSERT INTO activation_codes(student_code, code_hash, expires_at) VALUES (?,?,?)",
                  (sc, f"{salt}${_code_hash(sc, salt, plain)}", _ts(config.now() + timedelta(minutes=RESET_TTL_MIN))))
    return "ok", plain, row["faculty_email"]


def _consume_activation_code(c, student_code, code) -> str:
    row = c.execute("SELECT * FROM activation_codes WHERE student_code = ? AND used = 0 ORDER BY id DESC LIMIT 1",
                    (student_code,)).fetchone()
    if row is None:
        return "no_code"
    if row["attempts"] >= RESET_MAX_ATTEMPTS:
        return "locked"
    if row["expires_at"] < _ts():
        return "expired"
    salt, digest = row["code_hash"].split("$", 1)
    if not hmac.compare_digest(_code_hash(student_code, salt, clean_digits(code).strip()), digest):
        c.execute("UPDATE activation_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        return "bad_code"
    c.execute("UPDATE activation_codes SET used = 1 WHERE id = ?", (row["id"],))
    return "ok"


def _email_in_use(email: str, exclude_user_id=None) -> bool:
    """Is this address already somebody's login e-mail or recovery e-mail?"""
    e = email.strip().lower()
    return _row("SELECT 1 AS x FROM users WHERE (email = ? OR recovery_email = ?) AND id != ? LIMIT 1",
                (e, e, exclude_user_id if exclude_user_id is not None else -1)) is not None


def activate_student(code: str, email: str, password: str, recovery_email: str = "", verify_code=None):
    """Create the student's account (re-verifies everything on the server side).
    verify_code = the e-mailed code (required when config.REQUIRE_EMAIL_VERIFY).
    Returns (status, user): ok | invalid | invalid_email | weak_password | email_taken | bad_code | expired | locked | no_code"""
    status, row = verify_roster(code, email)
    if status != "ok":
        return "invalid", None
    if config.password_problem(password):
        return "weak_password", None
    rec = (recovery_email or email).strip().lower()
    if not _EMAIL_ANY.match(rec):
        return "invalid_email", None
    if rec != email.strip().lower() and _email_in_use(rec):
        return "email_taken", None
    sc = row["student_code"]
    try:
        with get_conn() as c:
            if config.REQUIRE_EMAIL_VERIFY:
                res = _consume_activation_code(c, sc, verify_code or "")
                if res != "ok":
                    return res, None              # the attempt counter was updated above: commit it
            c.execute(
                "INSERT INTO users(student_code, email, password_hash, role, recovery_email, faculty_id) "
                "VALUES (?,?,?,'student',?,?)",
                (sc, email.strip().lower(), hash_password(password), rec, row["faculty_id"]))
    except IntegrityError:
        return "invalid", None
    log_action(email.strip().lower(), "activate_account", sc, faculty_id=row["faculty_id"])
    return "ok", authenticate(sc, password)


# ---- staff accounts ---------------------------------------------------------------------------------------
def create_staff(email, full_name, password, role, staff_code=None, title="", specialization="",
                 phone="", must_change=True, faculty_id=None, actor="", actor_role=None):
    """Create a doctor / dean / faculty admin / system admin account (password stored as a bcrypt hash).
    Doctors, deans and faculty admins belong to ONE faculty (the current scope unless faculty_id is given); an
    `admin` is the SYSTEM admin and belongs to none. (Only a system admin may create faculty admins: app.py.)
    A staff code (DR-004 ...) is generated when none is given; staff codes are unique across the system (they
    can be used to log in). Returns ok | exists | bad_role | forbidden | no_faculty | short | long | weak"""
    if role not in config.STAFF_ROLES:
        return "bad_role"
    # who may create what (None = a trusted script such as scripts/create_admin.py)
    if actor_role is not None and ((role in ("admin", "faculty_admin") and actor_role != "admin")
                                   or actor_role not in ("admin", "faculty_admin")):
        return "forbidden"
    problem = config.password_problem(password)
    if problem:
        return problem
    fid = None if role == "admin" else (faculty_id if faculty_id is not None else scope())
    if role != "admin" and (fid is None or get_faculty(fid) is None):
        return "no_faculty"
    email = email.strip().lower()
    staff_code = clean_digits(staff_code).strip().upper() or None
    try:
        with get_conn() as c:
            new_id = c.execute(
                """INSERT INTO users(email, password_hash, role, full_name, staff_code, title, specialization,
                                     phone, recovery_email, must_change_password, faculty_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                (email, hash_password(password), role, full_name.strip(), staff_code,
                 title.strip(), specialization.strip(), phone.strip(), email, int(must_change), fid),
            ).fetchone()["id"]
            if not staff_code:
                c.execute("UPDATE users SET staff_code = ? WHERE id = ?", (f"{_PREFIX[role]}-{new_id:03d}", new_id))
    except IntegrityError:
        return "exists"
    if actor:
        log_action(actor, "create_account", f"{role}: {email}", faculty_id=fid)
    return "ok"


def list_staff():
    """Doctors, deans and faculty admins of the current faculty."""
    return _rows("""SELECT id, staff_code, full_name, title, specialization, email, phone, role, created_at
                    FROM users WHERE role IN ('doctor','dean','faculty_admin') AND (? IS NULL OR faculty_id = ?)
                    ORDER BY role, full_name""", _sc())


def list_system_admins():
    return _rows("""SELECT id, staff_code, full_name, email, created_at FROM users
                    WHERE role = 'admin' ORDER BY full_name""")


def import_staff(df, actor):
    """Create staff accounts from a normalised DataFrame (see importer.normalize_staff).
    Rows without a (valid) password get a generated temporary one. Returns a list of result dicts;
    the temp passwords are returned ONCE here and exist nowhere else in readable form."""
    results = []
    for r in df.to_dict("records"):
        if r["role"] not in ("doctor", "dean"):          # admins / unknown roles are never created by an import
            results.append({"email": r["email"], "full_name": r["full_name"], "role": r["role"],
                            "status": "role_not_allowed", "temp_password": ""})
            continue
        given = (r.get("password") or "").strip()
        use_given = bool(given) and config.password_problem(given) is None
        pw = given if use_given else secrets.token_urlsafe(9)
        status = create_staff(r["email"], r["full_name"], pw, r["role"], r.get("staff_code"),
                              r.get("title", ""), r.get("specialization", ""), r.get("phone", ""), must_change=True)
        results.append({"email": r["email"], "full_name": r["full_name"], "role": r["role"], "status": status,
                        "temp_password": pw if status == "ok" and not use_given else ""})
    log_action(actor, "import_staff", f"{sum(x['status'] == 'ok' for x in results)} created")
    return results


_UNSET = object()


def log_action(actor, action, target="", faculty_id=_UNSET):
    fid = _peek_scope() if faculty_id is _UNSET else faculty_id
    try:
        with get_conn() as c:
            c.execute("INSERT INTO audit_log(actor, action, target, faculty_id) VALUES (?,?,?,?)",
                      (str(actor)[:200], action, str(target)[:500], fid))
    except Exception:                                    # an audit failure must never block the user's action
        log.exception("audit log write failed")


def get_student(code):
    """A student of the CURRENT faculty by code (Arabic digits accepted)."""
    return _row("SELECT student_code, full_name, academic_year, department, study_type, section_no, faculty_id "
                "FROM university_roster WHERE student_code = ? AND (? IS NULL OR faculty_id = ?)",
                (clean_digits(code).strip(), *_sc()))


def max_section(year):
    return _row("SELECT COALESCE(MAX(section_no), 1) AS n FROM university_roster "
                "WHERE academic_year = ? AND (? IS NULL OR faculty_id = ?)", (year, *_sc()))["n"]


def list_doctors():
    return _rows("SELECT id, full_name, email FROM users WHERE role='doctor' AND (? IS NULL OR faculty_id = ?) "
                 "ORDER BY full_name", _sc())


# ----------------------------------------------------------------------------
# Roster
# ----------------------------------------------------------------------------
def import_roster_report(df: pd.DataFrame, actor="") -> dict:
    """Upsert roster rows INTO THE CURRENT FACULTY. Columns: student_code, full_name, faculty_email,
    academic_year, department, study_type (optional), section_no (optional).
    A code that already belongs to ANOTHER faculty, an e-mail already used by another student, or a code that
    looks like a staff identity (DR12 ...) is skipped (never overwritten).
    Returns {"saved": n, "skipped": [(student_code, reason), ...]}."""
    fid = scope()
    if fid is None or get_faculty(fid) is None:
        raise ValueError("no faculty selected")
    df = df.copy()
    if "study_type" not in df:
        df["study_type"] = "Regular"
    if "section_no" not in df:
        df["section_no"] = 1
    cols = ["student_code", "full_name", "faculty_email", "academic_year", "department", "study_type", "section_no"]
    skipped, data = [], []
    with get_conn() as c:
        foreign = {r["student_code"] for r in c.execute(
            "SELECT student_code FROM university_roster WHERE faculty_id != ?", (fid,)).fetchall()}
        owner = {r["faculty_email"].lower(): r["student_code"] for r in c.execute(
            "SELECT faculty_email, student_code FROM university_roster").fetchall()}
        for row in df[cols].itertuples(index=False):
            vals = [str(x).strip() for x in row[:6]]
            code, email = vals[0], vals[2].lower()
            if _RESERVED_CODE.match(code):
                skipped.append((code, "reserved_code"))
            elif code in foreign:
                skipped.append((code, "code_other_faculty"))
            elif owner.get(email, code) != code:
                skipped.append((code, "email_taken"))
            else:
                owner[email] = code
                data.append((*vals, int(row[6] or 1), fid))
        c.executemany(
            """INSERT INTO university_roster(student_code, full_name, faculty_email, academic_year, department,
                                             study_type, section_no, faculty_id)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(student_code) DO UPDATE SET
                 full_name=excluded.full_name, faculty_email=excluded.faculty_email,
                 academic_year=excluded.academic_year, department=excluded.department,
                 study_type=excluded.study_type, section_no=excluded.section_no
               WHERE university_roster.faculty_id = excluded.faculty_id""",
            data,
        )
    if actor:
        log_action(actor, "import_roster", f"{len(data)} saved, {len(skipped)} skipped")
    return {"saved": len(data), "skipped": skipped}


def import_roster(df: pd.DataFrame) -> int:
    return import_roster_report(df)["saved"]


# ----------------------------------------------------------------------------
# University + faculties (system admin)
# ----------------------------------------------------------------------------
def get_university() -> dict:
    """{'name_ar': ..., 'name_en': ...} — typed by the system admin (empty until he does)."""
    def load():
        rows = {r["key"]: r["value"] for r in _rows("SELECT key, value FROM settings WHERE key LIKE 'university_%'")}
        return {"name_ar": rows.get("university_name_ar", ""), "name_en": rows.get("university_name_en", "")}
    return _cached("university", 15, load)


def set_university(name_ar: str, name_en: str):
    with get_conn() as c:
        for k, v in (("university_name_ar", name_ar), ("university_name_en", name_en)):
            c.execute("INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                      (k, v.strip()[:200]))
    _CACHE.clear()


def list_faculties(active_only=False) -> list:
    rows = _cached("faculties", 15, lambda: _rows("SELECT * FROM faculties ORDER BY faculty_id"))
    return [dict(r) for r in rows if r["is_active"] or not active_only]


def get_faculty(fid):
    if fid is None:
        return None
    return next((f for f in list_faculties() if f["faculty_id"] == int(fid)), None)


def faculty_years(fid=None) -> list:
    """Year names of a faculty (default: the current scope) — 4 for commerce, 5 for pharmacy, 6 for medicine ..."""
    f = get_faculty(scope() if fid is None else fid)
    return config.year_names(f["years_count"] if f else config.DEFAULT_YEARS)


def faculty_point(fid=None):
    """Fallback GPS point of a faculty: {'lat','lon','radius'} or None (then the environment default, if any)."""
    f = get_faculty(scope() if fid is None else fid)
    if f and f["latitude"] is not None and f["longitude"] is not None:
        return {"lat": f["latitude"], "lon": f["longitude"], "radius": f["radius_m"]}
    if config.DEFAULT_COORDS:
        return {"lat": config.DEFAULT_COORDS[0], "lon": config.DEFAULT_COORDS[1], "radius": config.MAX_DISTANCE_M}
    return None


def _clean_domain(d: str) -> str:
    d = (d or "").strip().lower().lstrip("@")
    return f"@{d}" if d else ""


def add_faculty(name_ar, name_en="", years_count=4, email_domain="", latitude=None, longitude=None,
                radius_m=50, notes="", actor=""):
    """Returns (status, faculty_id): ok | exists | bad_name."""
    name_ar, name_en = (name_ar or "").strip(), (name_en or "").strip()
    if not (name_ar or name_en):
        return "bad_name", None
    try:
        with get_conn() as c:
            new_id = c.execute(
                """INSERT INTO faculties(name_ar, name_en, years_count, email_domain, latitude, longitude, radius_m, notes)
                   VALUES (?,?,?,?,?,?,?,?) RETURNING faculty_id""",
                (name_ar or name_en, name_en or name_ar, int(years_count), _clean_domain(email_domain),
                 latitude, longitude, int(radius_m), (notes or "").strip()[:500])).fetchone()["faculty_id"]
    except IntegrityError:
        return "exists", None
    _CACHE.clear()
    if actor:
        log_action(actor, "add_faculty", f"#{new_id} {name_ar or name_en}", faculty_id=new_id)
    return "ok", new_id


def update_faculty(fid, name_ar, name_en, years_count, email_domain, latitude, longitude, radius_m, notes,
                   is_active, actor=""):
    """Returns ok | bad_name | exists."""
    name_ar, name_en = (name_ar or "").strip(), (name_en or "").strip()
    if not (name_ar or name_en):
        return "bad_name"
    try:
        with get_conn() as c:
            c.execute(
                """UPDATE faculties SET name_ar=?, name_en=?, years_count=?, email_domain=?, latitude=?, longitude=?,
                          radius_m=?, notes=?, is_active=? WHERE faculty_id=?""",
                (name_ar or name_en, name_en or name_ar, int(years_count), _clean_domain(email_domain), latitude,
                 longitude, int(radius_m), (notes or "").strip()[:500], int(bool(is_active)), int(fid)))
            if not is_active:                       # a disabled faculty: its signed-in users are logged out at once
                c.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE faculty_id = ?", (int(fid),))
    except IntegrityError:
        return "exists"
    _CACHE.clear()
    if actor:
        log_action(actor, "update_faculty", f"#{fid}", faculty_id=int(fid))
    return "ok"


def faculty_overview() -> list:
    """One row per faculty with its main numbers — the system admin's dashboard."""
    return _rows(
        """SELECT f.faculty_id, f.name_ar, f.name_en, f.years_count, f.is_active,
                  (SELECT COUNT(*) FROM university_roster r WHERE r.faculty_id = f.faculty_id)            AS students,
                  (SELECT COUNT(*) FROM users u WHERE u.faculty_id = f.faculty_id AND u.role='student')   AS activated,
                  (SELECT COUNT(*) FROM users u WHERE u.faculty_id = f.faculty_id AND u.role='doctor')    AS doctors,
                  (SELECT COUNT(*) FROM courses c WHERE c.faculty_id = f.faculty_id)                      AS courses,
                  (SELECT COUNT(*) FROM complaints k WHERE k.faculty_id = f.faculty_id
                                                      AND k.status != 'Resolved')                         AS open_complaints
           FROM faculties f ORDER BY f.faculty_id""")


# ----------------------------------------------------------------------------
# Courses, lectures, syllabus, alerts
# ----------------------------------------------------------------------------
def create_course(name, doctor_id, doctor_name, year, semester, planned_lectures=14):
    """Create a course in the CURRENT faculty (the doctor must belong to it). Returns True when created."""
    fid = scope()
    if fid is None:
        return False
    with get_conn() as c:
        if doctor_id is not None and not c.execute(
                "SELECT 1 FROM users WHERE id = ? AND faculty_id = ? AND role = 'doctor'", (doctor_id, fid)).fetchone():
            return False
        cid = c.execute(
            """INSERT INTO courses(course_name, doctor_name, doctor_id, academic_year, semester, planned_lectures,
                                   faculty_id)
               VALUES (?,?,?,?,?,?,?) RETURNING course_id""",
            (name.strip(), doctor_name, doctor_id, year, semester, int(planned_lectures), fid),
        ).fetchone()["course_id"]
        c.execute("INSERT INTO course_settings(course_id, chat_open) VALUES (?, 1)", (cid,))
    return True


def list_courses(year=None, doctor_id=None):
    sql, where, params = "SELECT * FROM courses", ["(? IS NULL OR faculty_id = ?)"], list(_sc())
    if year is not None:
        where.append("academic_year = ?"); params.append(year)
    if doctor_id is not None:
        where.append("doctor_id = ?"); params.append(doctor_id)
    return _rows(sql + " WHERE " + " AND ".join(where) + " ORDER BY course_name", tuple(params))


def _doctor_owns_course(c, course_id, doctor_id) -> bool:
    return doctor_id is not None and c.execute(
        "SELECT 1 FROM courses WHERE course_id = ? AND doctor_id = ? AND faculty_id = ?",
        (course_id, doctor_id, scope())).fetchone() is not None


def latest_session(course_id, kind="Lecture", group_label=""):
    return _row("SELECT * FROM lectures WHERE course_id = ? AND kind = ? AND group_label = ? "
                "ORDER BY lecture_number DESC LIMIT 1", (course_id, kind, str(group_label or "")))


def latest_lecture(course_id):
    return latest_session(course_id, "Lecture", "")


def list_sessions(course_id, kind="Lecture", group_label=""):
    """group_label None = every group (sections of all groups)."""
    g = None if group_label is None else str(group_label)
    return _rows(
        """SELECT l.lecture_id, l.kind, l.group_label, l.lecture_number, l.topic_covered, l.date_created,
                  l.attendance_closed,
                  (SELECT COUNT(*) FROM attendance a
                    WHERE a.lecture_id = l.lecture_id AND a.status = 'Present') AS present_count
           FROM lectures l
           WHERE l.course_id = ? AND l.kind = ? AND (? IS NULL OR l.group_label = ?)
           ORDER BY l.group_label, l.lecture_number DESC""",
        (course_id, kind, g, g),
    )


def list_lectures(course_id):
    return list_sessions(course_id, "Lecture", "")


def upsert_session(course_id, kind, group_label, number, topic, doctor_id):
    """Create/update a lecture or a section session OF A COURSE THIS DOCTOR TEACHES. Lecture 4, 8, 12 ...
    auto-posts a quiz alert. Returns (lecture_id, quiz_alert_posted); lecture_id is None when not allowed."""
    if kind not in ("Lecture", "Section"):
        raise ValueError("kind")
    number = int(number)
    group = str(group_label or "") if kind == "Section" else ""
    quiz = kind == "Lecture" and number > 0 and number % 4 == 0
    with get_conn() as c:
        if not _doctor_owns_course(c, course_id, doctor_id):
            return None, False
        lid = c.execute(
            """INSERT INTO lectures(course_id, kind, group_label, lecture_number, topic_covered) VALUES (?,?,?,?,?)
               ON CONFLICT(course_id, kind, group_label, lecture_number)
               DO UPDATE SET topic_covered = excluded.topic_covered RETURNING lecture_id""",
            (course_id, kind, group, number, topic.strip())).fetchone()["lecture_id"]
        if quiz:
            c.execute("INSERT INTO alerts(course_id, lecture_id, alert_type) VALUES (?,?,'quiz') "
                      "ON CONFLICT(lecture_id, alert_type) DO NOTHING", (course_id, lid))
    return lid, quiz


def upsert_lecture(course_id, number, topic, doctor_id):
    return upsert_session(course_id, "Lecture", "", number, topic, doctor_id)


def active_alerts(year):
    """An alert is active only while its lecture is the course's LATEST lecture,
    so it disappears automatically once the doctor moves on to the next lecture."""
    return _rows(
        """SELECT a.alert_id, c.course_name, l.lecture_number, a.alert_type
           FROM alerts a
           JOIN courses c  ON c.course_id  = a.course_id
           JOIN lectures l ON l.lecture_id = a.lecture_id
           WHERE c.academic_year = ? AND (? IS NULL OR c.faculty_id = ?)
             AND l.lecture_number = (SELECT MAX(lecture_number) FROM lectures WHERE course_id = a.course_id AND kind = 'Lecture')
           ORDER BY a.timestamp DESC""",
        (year, *_sc()),
    )


# ----------------------------------------------------------------------------
# Attendance
# ----------------------------------------------------------------------------
def has_attended(student_code, lecture_id) -> bool:
    return _row("SELECT 1 AS x FROM attendance WHERE student_code=? AND lecture_id=?",
                (student_code, lecture_id)) is not None


def _owns(c, lecture_id, doctor_id) -> bool:
    """True only when the session belongs to a course of the CURRENT faculty taught by this doctor."""
    if doctor_id is None:
        return False
    return c.execute("SELECT 1 FROM lectures l JOIN courses k ON k.course_id = l.course_id "
                     "WHERE l.lecture_id = ? AND k.doctor_id = ? AND k.faculty_id = ?",
                     (lecture_id, doctor_id, scope())).fetchone() is not None


def set_attendance_code(lecture_id, code, minutes, doctor_id):
    """The DOCTOR chooses the code (4-10 letters/digits) and how long it stays valid.
    Returns ok | bad_code | forbidden | closed"""
    code = clean_digits(code).strip().upper()
    if not (4 <= len(code) <= 10 and code.isalnum()):
        return "bad_code"
    with get_conn() as c:
        if not _owns(c, lecture_id, doctor_id):
            return "forbidden"
        row = c.execute("SELECT attendance_closed FROM lectures WHERE lecture_id = ?", (lecture_id,)).fetchone()
        if row["attendance_closed"]:
            return "closed"
        c.execute("UPDATE lectures SET attendance_code = ?, code_expires_at = ? WHERE lecture_id = ?",
                  (code, _ts(config.now() + timedelta(minutes=int(minutes))), lecture_id))
    log_action(f"doctor#{doctor_id}", "set_attendance_code", f"session {lecture_id}, {int(minutes)} min")
    return "ok"


def clear_attendance_code(lecture_id, doctor_id):
    with get_conn() as c:
        if _owns(c, lecture_id, doctor_id):
            c.execute("UPDATE lectures SET attendance_code = NULL, code_expires_at = NULL WHERE lecture_id = ?",
                      (lecture_id,))


def get_attendance_code(lecture_id, doctor_id):
    """The current code — ONLY for the doctor who owns the session (students never read it)."""
    with get_conn() as c:
        if not _owns(c, lecture_id, doctor_id):
            return {"code": None, "expires_at": None, "active": False}
    r = _row("SELECT attendance_code AS code, code_expires_at AS expires_at FROM lectures WHERE lecture_id = ?",
             (lecture_id,)) or {}
    active = bool(r.get("code")) and (r.get("expires_at") or "") > _ts()
    return {"code": r.get("code"), "expires_at": r.get("expires_at"), "active": active}


def session_locations(course_id, kind, group_label=""):
    """GPS points where this session may be attended: the halls (with coordinates) the timetable assigns
    to this course/kind/group. Empty list -> the faculty-wide point is used instead."""
    return _rows(
        """SELECT DISTINCT h.name, h.latitude AS lat, h.longitude AS lon, h.radius_m AS radius
           FROM schedule s JOIN halls h ON h.hall_id = s.hall_id
           WHERE s.course_id = ? AND s.session_type = ?
             AND (? = 'Lecture' OR s.group_label = ? OR s.group_label = '')
             AND h.latitude IS NOT NULL AND h.longitude IS NOT NULL""",
        (course_id, kind, kind, str(group_label or "")),
    )


def venue_points(course_id, kind, group="", faculty_id=None):
    """GPS points set for this course's hall(s); falls back to the faculty-wide point."""
    locs = session_locations(course_id, kind, group)
    if locs:
        return locs
    p = faculty_point(faculty_id)
    return [{"name": "faculty", **p}] if p else []


def location_ok(lat, lon, locs) -> bool:
    return any(geodesic((L["lat"], L["lon"]), (lat, lon)).meters <= L["radius"] for L in locs)


def mark_present(student_code, lecture_id, code="", lat=None, lon=None, accuracy=None):
    """Student taps 'present'. EVERYTHING is checked here on the server (the browser only reports its position):
    enrolment, code, attempts lock, GPS accuracy and distance. Returns:
    ok | already | closed | not_enrolled | no_lecture | cancelled | code_inactive | bad_code | locked |
    no_location | weak_gps | outside | no_point"""
    lec = _row(
        """SELECT l.attendance_closed, l.kind, l.group_label, l.attendance_code, l.code_expires_at, l.course_id,
                  c.academic_year, c.faculty_id FROM lectures l JOIN courses c ON c.course_id = l.course_id
           WHERE l.lecture_id = ?""", (lecture_id,))
    if lec is None:
        return "no_lecture"
    stu = _row("SELECT academic_year, section_no, faculty_id FROM university_roster WHERE student_code=?", (student_code,))
    if stu is None or stu["academic_year"] != lec["academic_year"] or stu["faculty_id"] != lec["faculty_id"]:
        return "not_enrolled"
    if lec["kind"] == "Section" and str(stu["section_no"]) != lec["group_label"]:
        return "not_enrolled"
    old = _row("SELECT status, note FROM attendance WHERE student_code=? AND lecture_id=?", (student_code, lecture_id))
    if old:
        if old["status"] == "Present":
            return "already"
        return "cancelled" if old["note"] == "left_early" else "closed"
    if lec["attendance_closed"]:
        return "closed"
    now = config.now()
    if not lec["attendance_code"] or (lec["code_expires_at"] or "") <= _ts(now):
        return "code_inactive"
    # --- location (server side) -------------------------------------------------------------------------
    locs = venue_points(lec["course_id"], lec["kind"], lec["group_label"], lec["faculty_id"])
    if not locs:
        return "no_point"
    if lat is None or lon is None:
        return "no_location"
    if accuracy is not None and accuracy > config.MAX_ACCURACY_M:
        return "weak_gps"
    if not location_ok(float(lat), float(lon), locs):
        return "outside"
    # --- code + attempt counter -----------------------------------------------------------------------------
    with get_conn() as c:
        att = c.execute("SELECT fails, locked_until FROM attendance_attempts WHERE student_code=? AND lecture_id=?",
                        (student_code, lecture_id)).fetchone()
        if att and att["locked_until"] and att["locked_until"] > _ts(now):
            return "locked"
        if not hmac.compare_digest(clean_digits(code).strip().upper().encode(), lec["attendance_code"].encode()):
            fails = (att["fails"] if att else 0) + 1
            lock = _ts(now + timedelta(minutes=10)) if fails >= 5 else None
            c.execute("""INSERT INTO attendance_attempts(student_code, lecture_id, fails, locked_until) VALUES (?,?,?,?)
                         ON CONFLICT(student_code, lecture_id) DO UPDATE SET fails=excluded.fails,
                                                                             locked_until=excluded.locked_until""",
                      (student_code, lecture_id, 0 if lock else fails, lock))
            return "bad_code"
        # the session must still be open at the moment of writing (a doctor may close it at the same time)
        n = c.execute(
            """INSERT INTO attendance(student_code, lecture_id, status)
               SELECT ?, ?, 'Present' WHERE EXISTS
                   (SELECT 1 FROM lectures WHERE lecture_id = ? AND attendance_closed = 0)
               ON CONFLICT(student_code, lecture_id) DO NOTHING""",
            (student_code, lecture_id, lecture_id)).rowcount
        if n == 0:
            return "already" if has_attended(student_code, lecture_id) else "closed"
        c.execute("DELETE FROM attendance_attempts WHERE student_code=? AND lecture_id=?", (student_code, lecture_id))
    return "ok"


def cancel_attendance(lecture_id, student_code, actor, doctor_id):
    """A student left in the middle: the doctor types the student's code and the attendance is cancelled
    (status Absent, note 'left_early', and the student cannot register again). Returns ok | not_enrolled | forbidden."""
    lec = _row("SELECT l.kind, l.group_label, c.academic_year, c.faculty_id FROM lectures l "
               "JOIN courses c ON c.course_id=l.course_id WHERE l.lecture_id = ?", (lecture_id,))
    stu = _row("SELECT student_code, academic_year, section_no, faculty_id FROM university_roster WHERE student_code = ?",
               (clean_digits(student_code).strip(),))
    if not lec or not stu or lec["faculty_id"] != scope() \
            or stu["faculty_id"] != lec["faculty_id"] or stu["academic_year"] != lec["academic_year"] or (
            lec["kind"] == "Section" and str(stu["section_no"]) != lec["group_label"]):
        return "not_enrolled"
    with get_conn() as c:
        if not _owns(c, lecture_id, doctor_id):
            return "forbidden"
        c.execute("""INSERT INTO attendance(student_code, lecture_id, status, note) VALUES (?,?,'Absent','left_early')
                     ON CONFLICT(student_code, lecture_id) DO UPDATE SET status='Absent', note='left_early'""",
                  (stu["student_code"], lecture_id))
        c.execute("INSERT INTO audit_log(actor, action, target, faculty_id) VALUES (?, 'cancel_attendance', ?, ?)",
                  (actor, f"{stu['student_code']} @ session {lecture_id}", lec["faculty_id"]))
    return "ok"


def close_lecture(lecture_id, doctor_id) -> int:
    """Close attendance: everyone enrolled (for a section: only that section's students) without a record
    becomes 'Absent'. Returns the number of absences created (-1 if the session isn't the doctor's)."""
    with get_conn() as c:
        if not _owns(c, lecture_id, doctor_id):
            return -1
        n = c.execute(
            """INSERT INTO attendance(student_code, lecture_id, status)
               SELECT r.student_code, l.lecture_id, 'Absent'
               FROM lectures l
               JOIN courses c ON c.course_id = l.course_id
               JOIN university_roster r ON r.academic_year = c.academic_year AND r.faculty_id = c.faculty_id
               WHERE l.lecture_id = ?
                 AND (l.kind = 'Lecture' OR l.group_label = CAST(r.section_no AS TEXT))
               ON CONFLICT(student_code, lecture_id) DO NOTHING""",
            (lecture_id,)).rowcount
        c.execute("UPDATE lectures SET attendance_closed = 1, attendance_code = NULL, code_expires_at = NULL "
                  "WHERE lecture_id = ?", (lecture_id,))
        c.execute("DELETE FROM attendance_attempts WHERE lecture_id = ?", (lecture_id,))
    log_action(f"doctor#{doctor_id}", "close_attendance", f"session {lecture_id}: {n} absent")
    return n


def student_attendance_summary(student_code, year, kind="Lecture"):
    """Per-course attendance for ONE student and one kind (Lecture / Section)."""
    return _rows(
        """SELECT c.course_id, c.course_name,
                  COUNT(m.lecture_id) AS total,
                  COALESCE(SUM(CASE WHEN m.status = 'Present' THEN 1 ELSE 0 END), 0) AS present
           FROM courses c
           LEFT JOIN v_attendance_matrix m
                  ON m.course_id = c.course_id AND m.student_code = ? AND m.kind = ?
           WHERE c.academic_year = ?
             AND c.faculty_id = (SELECT faculty_id FROM university_roster WHERE student_code = ?)
           GROUP BY c.course_id, c.course_name ORDER BY c.course_name""",
        (student_code, kind, year, student_code),
    )


# ----------------------------------------------------------------------------
# Private planner  (every function requires the owner's student_code)
# ----------------------------------------------------------------------------
def add_task(student_code, title):
    with get_conn() as c:
        c.execute("INSERT INTO private_planner(student_code, task_title) VALUES (?,?)",
                  (student_code, title.strip()[:300]))


def list_tasks(student_code):
    return _rows("SELECT * FROM private_planner WHERE student_code=? ORDER BY is_completed, task_id DESC",
                 (student_code,))


def set_task_done(student_code, task_id, done):
    with get_conn() as c:   # student_code in WHERE = nobody can touch another student's task
        c.execute("UPDATE private_planner SET is_completed=? WHERE task_id=? AND student_code=?",
                  (int(bool(done)), task_id, student_code))


def delete_task(student_code, task_id):
    with get_conn() as c:
        c.execute("DELETE FROM private_planner WHERE task_id=? AND student_code=?", (task_id, student_code))


# ----------------------------------------------------------------------------
# Complaints
# ----------------------------------------------------------------------------
def add_complaint(student_code, ctype, content, kind="Complaint", faculty_id=None):
    """student_code=None -> anonymous (no identity is stored). kind: Complaint | Suggestion."""
    if kind not in ("Complaint", "Suggestion"):
        kind = "Complaint"
    fid = faculty_id if faculty_id is not None else _peek_scope()
    with get_conn() as c:
        c.execute("INSERT INTO complaints(student_code, type, content, kind, faculty_id) VALUES (?,?,?,?,?)",
                  (student_code, ctype, content.strip()[:2000], kind, fid))


def my_complaints(student_code):
    return _rows("SELECT complaint_id, kind, type, content, status, response, timestamp FROM complaints "
                 "WHERE student_code = ? ORDER BY complaint_id DESC", (student_code,))


_WHO = {"all": "1=1", "anonymous": "c.student_code IS NULL", "named": "c.student_code IS NOT NULL"}


def list_complaints(who="all", kind=None):
    """who: all | anonymous | named.  kind: None | Complaint | Suggestion."""
    return _rows(
        f"""SELECT c.complaint_id, c.kind, c.type, c.content, c.status, c.response, c.timestamp,
                   c.student_code, r.full_name
            FROM complaints c LEFT JOIN university_roster r ON r.student_code = c.student_code
            WHERE {_WHO[who]} AND (? IS NULL OR c.kind = ?) AND (? IS NULL OR c.faculty_id = ?)
            ORDER BY c.complaint_id DESC""",   # the condition comes from the constant dict above
        (kind, kind, *_sc()),
    )


def set_complaint_status(complaint_id, status, response=None, actor=""):
    if status not in config.COMPLAINT_STATUSES:
        return
    with get_conn() as c:
        if response is None:
            c.execute("UPDATE complaints SET status=? WHERE complaint_id=? AND (? IS NULL OR faculty_id = ?)",
                      (status, complaint_id, *_sc()))
        else:
            c.execute("UPDATE complaints SET status=?, response=? WHERE complaint_id=? AND (? IS NULL OR faculty_id = ?)",
                      (status, response.strip()[:2000], complaint_id, *_sc()))
    if actor:
        log_action(actor, "complaint_update", f"#{complaint_id} -> {status}")


# ----------------------------------------------------------------------------
# Chat (office hours)
# ----------------------------------------------------------------------------
def doctor_code(doctor_id) -> str:
    return f"DR{doctor_id}"


def _chat_course(c, course_id):
    return c.execute("SELECT course_id, doctor_id, academic_year, faculty_id FROM courses "
                     "WHERE course_id = ? AND faculty_id = ?", (course_id, scope())).fetchone()


def is_chat_open(course_id) -> bool:
    r = _row("SELECT chat_open FROM course_settings WHERE course_id=?", (course_id,))
    return bool(r["chat_open"]) if r else True


def set_chat_open(course_id, is_open, doctor_id):
    with get_conn() as c:
        if not _doctor_owns_course(c, course_id, doctor_id):
            return False
        c.execute("""INSERT INTO course_settings(course_id, chat_open) VALUES (?,?)
                     ON CONFLICT(course_id) DO UPDATE SET chat_open = excluded.chat_open""",
                  (course_id, int(bool(is_open))))
    return True


def send_message(sender, receiver, course_id, text, from_student=True) -> bool:
    """Server-side rules: the course must be in this faculty; a student writes only to THIS course's doctor
    (and only while the chat is open); a doctor answers only students of the course's year."""
    text = text.strip()[:1000]
    if not text:
        return False
    with get_conn() as c:
        course = _chat_course(c, course_id)
        if not course or course["doctor_id"] is None:
            return False
        dr = doctor_code(course["doctor_id"])
        student = receiver if not from_student else sender
        if (from_student and receiver != dr) or (not from_student and sender != dr):
            return False
        if not c.execute("SELECT 1 FROM university_roster WHERE student_code = ? AND academic_year = ? AND faculty_id = ?",
                         (student, course["academic_year"], course["faculty_id"])).fetchone():
            return False
        if from_student and not is_chat_open(course_id):
            return False
        c.execute("INSERT INTO chat_messages(sender_code, receiver_code, course_id, message_text) VALUES (?,?,?,?)",
                  (sender, receiver, course_id, text))
    return True


def get_conversation(course_id, a, b, limit=100):
    return _rows(
        """SELECT t.* FROM (
             SELECT m.* FROM chat_messages m JOIN courses k ON k.course_id = m.course_id
             WHERE m.course_id = ? AND k.faculty_id = ?
               AND ((m.sender_code=? AND m.receiver_code=?) OR (m.sender_code=? AND m.receiver_code=?))
             ORDER BY m.message_id DESC LIMIT ?
           ) AS t ORDER BY t.message_id""",
        (course_id, scope(), a, b, b, a, limit),
    )


def chat_partners(course_id, dr_code):
    """Students who have written to this doctor in this course."""
    return _rows(
        """SELECT DISTINCT m.sender_code AS code, COALESCE(r.full_name, m.sender_code) AS name
           FROM chat_messages m JOIN courses k ON k.course_id = m.course_id
                LEFT JOIN university_roster r ON r.student_code = m.sender_code
           WHERE m.course_id = ? AND m.receiver_code = ? AND k.faculty_id = ?""",
        (course_id, dr_code, scope()),
    )


# ----------------------------------------------------------------------------
# Analytics (all read from v_attendance_matrix)
# ----------------------------------------------------------------------------
def attendance_by_group(kind="Lecture") -> pd.DataFrame:
    return _df(
        f"""SELECT academic_year, department,
                  {_present()} AS present, {_absent()} AS absent, {_rate()} AS rate
           FROM v_attendance_matrix WHERE (? IS NULL OR kind = ?) AND (? IS NULL OR faculty_id = ?)
           GROUP BY academic_year, department ORDER BY academic_year, department""",
        (kind, kind, *_sc()),
    )


def attendance_trend(year=None, kind="Lecture") -> pd.DataFrame:
    return _df(
        f"""SELECT lecture_date, {_rate()} AS rate
           FROM v_attendance_matrix WHERE (? IS NULL OR academic_year = ?) AND (? IS NULL OR kind = ?)
             AND (? IS NULL OR faculty_id = ?)
           GROUP BY lecture_date ORDER BY lecture_date""",
        (year, year, kind, kind, *_sc()),
    )


def early_warning(threshold=75, min_lectures=1, kind=None) -> pd.DataFrame:
    """Students below `threshold`% in any course/kind (with at least `min_lectures` closed sessions)."""
    return _df(
        f"""SELECT student_code, full_name, academic_year, department, study_type, course_name, kind,
                  {_present()} AS present, COUNT(*) AS total, {_rate()} AS rate
           FROM v_attendance_matrix WHERE (? IS NULL OR kind = ?) AND (? IS NULL OR faculty_id = ?)
           GROUP BY student_code, full_name, academic_year, department, study_type, course_id, course_name, kind
           HAVING COUNT(*) >= ? AND 100.0 * {_present()} / COUNT(*) < ?
           ORDER BY rate, full_name""",
        (kind, kind, *_sc(), min_lectures, threshold),
    )


# ----------------------------------------------------------------------------
# Live attendance + dashboard statistics
# ----------------------------------------------------------------------------
def attendance_record(student_code, lecture_id):
    return _row("SELECT status, timestamp, note FROM attendance WHERE student_code=? AND lecture_id=?",
                (student_code, lecture_id))


def lecture_attendees(lecture_id, doctor_id):
    """Who has marked present so far (ONLY for the doctor who owns the session)."""
    with get_conn() as c:
        if not _owns(c, lecture_id, doctor_id):
            return []
    return _rows(
        """SELECT r.full_name, r.student_code, r.academic_year, r.study_type, r.section_no, a.timestamp
           FROM attendance a JOIN university_roster r ON r.student_code = a.student_code
           WHERE a.lecture_id = ? AND a.status = 'Present'
           ORDER BY a.timestamp DESC""",
        (lecture_id,),
    )


def doctor_stats(doctor_id):
    one = lambda sql, p=(doctor_id,): _row(sql, p)["n"]
    today = config.now().strftime("%Y-%m-%d 00:00:00")
    return {
        "courses": one("SELECT COUNT(*) AS n FROM courses WHERE doctor_id = ?"),
        "students": one("""SELECT COUNT(DISTINCT r.student_code) AS n FROM courses c
                           JOIN university_roster r ON r.academic_year = c.academic_year
                                                    AND r.faculty_id = c.faculty_id
                           WHERE c.doctor_id = ?"""),
        "lectures": one("""SELECT COUNT(*) AS n FROM lectures l JOIN courses c ON c.course_id = l.course_id
                           WHERE c.doctor_id = ? AND l.kind = 'Lecture'"""),
        "today_present": one("""SELECT COUNT(*) AS n FROM attendance a
                                JOIN lectures l ON l.lecture_id = a.lecture_id
                                JOIN courses c ON c.course_id = l.course_id
                                WHERE c.doctor_id = ? AND a.status = 'Present' AND a.timestamp >= ?""",
                             (doctor_id, today)),
    }


def at_risk_count(threshold=75, min_lectures=3) -> int:
    """Students below the attendance threshold in some course (cached 30 s: it scans the attendance history)."""
    return _cached(("at_risk", scope(), threshold, min_lectures), 30,
                   lambda: int(early_warning(threshold, min_lectures)["student_code"].nunique()))


def admin_stats():
    """Numbers of the CURRENT faculty."""
    sc = _sc()
    n = lambda sql: _row(sql, sc)["n"]
    avg = _cached(("avg_rate", scope()), 30, lambda: _row(
        f"SELECT {_rate()} AS n FROM v_attendance_matrix WHERE (? IS NULL OR faculty_id = ?)", sc)["n"])
    return {
        "roster": n("SELECT COUNT(*) AS n FROM university_roster WHERE (? IS NULL OR faculty_id = ?)"),
        "activated": n("SELECT COUNT(*) AS n FROM users WHERE role='student' AND (? IS NULL OR faculty_id = ?)"),
        "open_complaints": n("SELECT COUNT(*) AS n FROM complaints WHERE status != 'Resolved' "
                             "AND (? IS NULL OR faculty_id = ?)"),
        "doctors": n("SELECT COUNT(*) AS n FROM users WHERE role='doctor' AND (? IS NULL OR faculty_id = ?)"),
        "courses": n("SELECT COUNT(*) AS n FROM courses WHERE (? IS NULL OR faculty_id = ?)"),
        "halls": n("SELECT COUNT(*) AS n FROM halls WHERE (? IS NULL OR faculty_id = ?)"),
        "avg_rate": avg,
    }


# ----------------------------------------------------------------------------
# Password management, registered students, audit log
# ----------------------------------------------------------------------------
# NOTE: passwords are stored as bcrypt hashes and can NEVER be read back.
# "Forgot password" = admin generates a temporary one (shown once) and the user must change it.
# Every password change bumps users.session_epoch, which signs the account out of all OTHER open sessions.
def change_password(user_id, new_pw):
    """Returns ok | same | not_found | short | long | weak."""
    problem = config.password_problem(new_pw)
    if problem:
        return problem
    row = _row("SELECT password_hash FROM users WHERE id=?", (user_id,))
    if row is None:
        return "not_found"
    if _check_password(new_pw, row["password_hash"]):
        return "same"
    with get_conn() as c:
        c.execute("UPDATE users SET password_hash=?, must_change_password=0, session_epoch = session_epoch + 1 "
                  "WHERE id=?", (hash_password(new_pw), user_id))
    log_action(f"user#{user_id}", "change_password", "")
    return "ok"


def reset_user_password(user_id, actor, actor_role="admin"):
    """Admin-generated TEMPORARY password (shown once); the user must change it at next login.
    Only the faculty's own accounts can be reset from inside a faculty, and a faculty admin can never reset
    another faculty admin or the system admin."""
    u = _row("SELECT id, email, role FROM users WHERE id = ? AND (? IS NULL OR faculty_id = ?)", (user_id, *_sc()))
    if not u or (actor_role != "admin" and u["role"] in ("admin", "faculty_admin")):
        return None
    temp = secrets.token_urlsafe(9)
    with get_conn() as c:
        c.execute("UPDATE users SET password_hash=?, must_change_password=1, session_epoch = session_epoch + 1 "
                  "WHERE id=?", (hash_password(temp), user_id))
        c.execute("INSERT INTO audit_log(actor, action, target, faculty_id) VALUES (?, 'reset_password', ?, ?)",
                  (actor, u["email"], scope()))
    return temp


def reset_student_password(student_code, actor, actor_role="admin"):
    u = _row("SELECT id FROM users WHERE student_code=? AND role='student' AND (? IS NULL OR faculty_id = ?)",
             (student_code, *_sc()))
    return reset_user_password(u["id"], actor, actor_role) if u else None


def set_recovery_email(user_id, email):
    """Returns ok | taken (the address already belongs to another account)."""
    e = email.strip().lower()
    if not _EMAIL_ANY.match(e):
        return "invalid_email"
    if _email_in_use(e, user_id):
        return "taken"
    try:
        with get_conn() as c:
            c.execute("UPDATE users SET recovery_email = ? WHERE id = ?", (e, user_id))
    except IntegrityError:
        return "taken"
    return "ok"


def check_password_of(user_id, password) -> bool:
    row = _row("SELECT password_hash FROM users WHERE id = ?", (user_id,))
    return bool(row) and _check_password(password, row["password_hash"])


# ---- forgot password: emailed one-time code ---------------------------------
RESET_TTL_MIN, RESET_MAX_ATTEMPTS, RESET_MAX_PER_HOUR = 10, 5, 3


def find_user_for_reset(email):
    """The user whose login email OR recovery email equals `email` (None if nobody; a login e-mail wins)."""
    e = email.strip().lower()
    return _row("SELECT id, email, recovery_email, role FROM users WHERE email = ? OR recovery_email = ? "
                "ORDER BY CASE WHEN email = ? THEN 0 ELSE 1 END, id LIMIT 1", (e, e, e))


def create_reset_code(user_id):
    """Returns (status, code). status: ok | rate_limited. Older unused codes are invalidated."""
    with get_conn() as c:
        recent = c.execute("SELECT COUNT(*) AS n FROM password_resets WHERE user_id = ? AND created_at > ?",
                           (user_id, _ts(config.now() - timedelta(hours=1)))).fetchone()["n"]
        if recent >= RESET_MAX_PER_HOUR:
            return "rate_limited", None
        c.execute("UPDATE password_resets SET used = 1 WHERE user_id = ? AND used = 0", (user_id,))
        code = f"{secrets.randbelow(10 ** 6):06d}"
        salt = secrets.token_hex(8)
        c.execute("INSERT INTO password_resets(user_id, code_hash, expires_at) VALUES (?,?,?)",
                  (user_id, f"{salt}${_code_hash(user_id, salt, code)}",
                   _ts(config.now() + timedelta(minutes=RESET_TTL_MIN))))
    return "ok", code


def complete_reset(user_id, code, new_password):
    """Check the emailed code and set the new password. Returns:
    ok | bad_code | expired | locked | no_code | same | short | long | weak"""
    problem = config.password_problem(new_password)
    if problem:
        return problem
    with get_conn() as c:
        row = c.execute("SELECT * FROM password_resets WHERE user_id = ? AND used = 0 ORDER BY id DESC LIMIT 1",
                        (user_id,)).fetchone()
        if row is None:
            return "no_code"
        if row["attempts"] >= RESET_MAX_ATTEMPTS:
            return "locked"
        if row["expires_at"] < _ts():
            return "expired"
        salt, digest = row["code_hash"].split("$", 1)
        if not hmac.compare_digest(_code_hash(user_id, salt, clean_digits(code).strip()), digest):
            c.execute("UPDATE password_resets SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
            return "bad_code"
        old = c.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if old and _check_password(new_password, old["password_hash"]):
            return "same"
        c.execute("UPDATE password_resets SET used = 1 WHERE id = ?", (row["id"],))
        c.execute("UPDATE users SET password_hash = ?, must_change_password = 0, session_epoch = session_epoch + 1 "
                  "WHERE id = ?", (hash_password(new_password), user_id))
    log_action(f"user#{user_id}", "password_reset_by_code", "")
    return "ok"


def list_audit(limit=20):
    return _rows("SELECT * FROM audit_log WHERE (? IS NULL OR faculty_id = ?) ORDER BY id DESC LIMIT ?",
                 (*_sc(), limit))


def registered_students(year=None):
    return _rows(
        """SELECT r.student_code, r.full_name, r.academic_year, r.department, r.study_type, r.section_no,
                  u.email, u.recovery_email, u.created_at
           FROM users u JOIN university_roster r ON r.student_code = u.student_code
           WHERE u.role = 'student' AND (? IS NULL OR r.academic_year = ?) AND (? IS NULL OR r.faculty_id = ?)
           ORDER BY r.academic_year, r.full_name""",
        (year, year, *_sc()),
    )


def roster_by_year(year):
    return _rows(
        """SELECT r.student_code, r.full_name, r.faculty_email, r.department, r.study_type, r.section_no,
                  CASE WHEN u.id IS NOT NULL THEN 1 ELSE 0 END AS registered
           FROM university_roster r LEFT JOIN users u ON u.student_code = r.student_code
           WHERE r.academic_year = ? AND (? IS NULL OR r.faculty_id = ?) ORDER BY r.full_name""",
        (year, *_sc()),
    )


# ----------------------------------------------------------------------------
# Halls & timetable
# ----------------------------------------------------------------------------
def add_hall(name, kind, capacity, latitude=None, longitude=None, radius_m=30):
    fid = scope()
    if fid is None:
        return "no_faculty"
    try:
        with get_conn() as c:
            c.execute("INSERT INTO halls(faculty_id, name, kind, capacity, latitude, longitude, radius_m) VALUES (?,?,?,?,?,?,?)",
                      (fid, name.strip(), kind, int(capacity), latitude, longitude, int(radius_m)))
        return "ok"
    except IntegrityError:
        return "exists"


def update_hall(hall_id, name, kind, capacity, latitude, longitude, radius_m):
    try:
        with get_conn() as c:
            c.execute("UPDATE halls SET name=?, kind=?, capacity=?, latitude=?, longitude=?, radius_m=? "
                      "WHERE hall_id=? AND (? IS NULL OR faculty_id = ?)",
                      (name.strip(), kind, int(capacity), latitude, longitude, int(radius_m), hall_id, *_sc()))
        return "ok"
    except IntegrityError:
        return "exists"


def halls_usage():
    """Which course / doctor / session type uses each hall (for the admin's overview)."""
    return _rows(
        """SELECT h.hall_id, h.name, h.kind, h.latitude, h.longitude, h.radius_m,
                  c.course_name, c.doctor_name, s.session_type, s.group_label
           FROM halls h LEFT JOIN schedule s ON s.hall_id = h.hall_id
                        LEFT JOIN courses c ON c.course_id = s.course_id
           WHERE (? IS NULL OR h.faculty_id = ?)
           ORDER BY h.name, c.course_name""", _sc())


def courses_without_location():
    """Courses with timetable sessions whose hall has no GPS point yet (attendance would fall back
    to the faculty-wide point)."""
    return _rows(
        """SELECT DISTINCT c.course_name, c.doctor_name, s.session_type
           FROM schedule s JOIN courses c ON c.course_id = s.course_id JOIN halls h ON h.hall_id = s.hall_id
           WHERE (h.latitude IS NULL OR h.longitude IS NULL) AND (? IS NULL OR c.faculty_id = ?)
           ORDER BY c.course_name""", _sc())


def list_halls():
    return _rows("SELECT * FROM halls WHERE (? IS NULL OR faculty_id = ?) ORDER BY kind, name", _sc())


def add_slot(course_id, hall_id, session_type, group_label, day, start, end):
    """Insert a timetable slot after checking three kinds of clash.
    Returns: ok | time_invalid | no_course | foreign_hall | hall_conflict | doctor_conflict | year_conflict"""
    if start >= end:
        return "time_invalid"
    course = _row("SELECT doctor_id, academic_year, faculty_id FROM courses WHERE course_id=?", (course_id,))
    if course is None or course["faculty_id"] != scope():
        return "no_course"
    hall = _row("SELECT faculty_id FROM halls WHERE hall_id=?", (hall_id,))
    if hall is None or hall["faculty_id"] != course["faculty_id"]:
        return "foreign_hall"
    overlap = "s.day_of_week = ? AND s.start_time < ? AND s.end_time > ?"
    with get_conn() as c:
        if c.execute(f"SELECT 1 FROM schedule s WHERE s.hall_id = ? AND {overlap}",
                     (hall_id, day, end, start)).fetchone():
            return "hall_conflict"
        if course["doctor_id"] and c.execute(
                f"SELECT 1 FROM schedule s JOIN courses k ON k.course_id = s.course_id "
                f"WHERE k.doctor_id = ? AND {overlap}", (course["doctor_id"], day, end, start)).fetchone():
            return "doctor_conflict"
        # same faculty only (BUG-01: a lecture of another faculty must never block this one)
        if session_type == "Lecture" and c.execute(
                f"SELECT 1 FROM schedule s JOIN courses k ON k.course_id = s.course_id "
                f"WHERE k.faculty_id = ? AND k.academic_year = ? AND s.session_type = 'Lecture' AND {overlap}",
                (course["faculty_id"], course["academic_year"], day, end, start)).fetchone():
            return "year_conflict"
        c.execute(
            """INSERT INTO schedule(course_id, hall_id, session_type, group_label, day_of_week, start_time, end_time)
               VALUES (?,?,?,?,?,?,?)""",
            (course_id, hall_id, session_type, str(group_label or ""), day, start, end),
        )
    return "ok"


def list_schedule(year=None, doctor_id=None, hall_id=None):
    return _rows(
        """SELECT s.slot_id, s.day_of_week, s.start_time, s.end_time, s.session_type, s.group_label,
                  c.course_id, c.course_name, c.doctor_name, c.academic_year,
                  h.name AS hall_name, h.kind AS hall_kind
           FROM schedule s
           JOIN courses c ON c.course_id = s.course_id
           JOIN halls h   ON h.hall_id = s.hall_id
           WHERE (? IS NULL OR c.academic_year = ?)
             AND (? IS NULL OR c.doctor_id = ?)
             AND (? IS NULL OR s.hall_id = ?)
             AND (? IS NULL OR c.faculty_id = ?)
           ORDER BY s.day_of_week, s.start_time""",
        (year, year, doctor_id, doctor_id, hall_id, hall_id, *_sc()),
    )


def delete_slot(slot_id):
    with get_conn() as c:
        c.execute("DELETE FROM schedule WHERE slot_id = ? AND course_id IN "
                  "(SELECT course_id FROM courses WHERE faculty_id = ?)", (slot_id, scope()))


# ----------------------------------------------------------------------------
# Dean analytics: doctors' progress + attendance by department / doctor
# ----------------------------------------------------------------------------
def doctor_overview() -> pd.DataFrame:
    """One row per course: syllabus progress (last LECTURE / planned) and lecture attendance."""
    df = _df(
        f"""SELECT c.course_id, c.course_name, c.doctor_id, c.doctor_name, c.academic_year, c.planned_lectures,
                  COALESCE((SELECT MAX(lecture_number) FROM lectures l
                             WHERE l.course_id = c.course_id AND l.kind = 'Lecture'), 0) AS last_lecture,
                  (SELECT topic_covered FROM lectures l WHERE l.course_id = c.course_id AND l.kind = 'Lecture'
                    ORDER BY lecture_number DESC LIMIT 1) AS last_topic,
                  (SELECT MAX(date_created) FROM lectures l
                    WHERE l.course_id = c.course_id AND l.kind = 'Lecture') AS last_date,
                  (SELECT {_rate('m.status')}
                     FROM v_attendance_matrix m WHERE m.course_id = c.course_id AND m.kind = 'Lecture') AS attendance_rate
           FROM courses c WHERE (? IS NULL OR c.faculty_id = ?)
           ORDER BY c.doctor_name, c.course_name""", _sc()
    )
    df["progress"] = (100.0 * df["last_lecture"] / df["planned_lectures"]).clip(upper=100).round(1)
    last = pd.to_datetime(df["last_date"], errors="coerce")
    # TIMESTAMP(0) rounds to the nearest second, so a brand-new row can be "0.4 s in the future": clip at 0
    df["days_since"] = (pd.Timestamp(config.now()) - last).dt.days.clip(lower=0).astype("float64")   # NaN = never
    return df.drop(columns=["last_date"])


def attendance_by_department(year=None, kind="Lecture") -> pd.DataFrame:
    return _df(
        f"""SELECT department, {_present()} AS present, {_absent()} AS absent, {_rate()} AS rate
           FROM v_attendance_matrix WHERE (? IS NULL OR academic_year = ?) AND (? IS NULL OR kind = ?)
             AND (? IS NULL OR faculty_id = ?)
           GROUP BY department ORDER BY department""",
        (year, year, kind, kind, *_sc()),
    )


def attendance_by_doctor(kind="Lecture") -> pd.DataFrame:
    return _df(
        f"""SELECT c.doctor_name, {_present('m.status')} AS present, {_absent('m.status')} AS absent,
                  {_rate('m.status')} AS rate
           FROM v_attendance_matrix m JOIN courses c ON c.course_id = m.course_id
           WHERE (? IS NULL OR m.kind = ?) AND (? IS NULL OR m.faculty_id = ?)
           GROUP BY c.doctor_id, c.doctor_name ORDER BY c.doctor_name""",
        (kind, kind, *_sc()),
    )


def attendance_doctor_department(kind="Lecture") -> pd.DataFrame:
    return _df(
        f"""SELECT c.doctor_id, c.doctor_name, m.department, {_rate('m.status')} AS rate
           FROM v_attendance_matrix m JOIN courses c ON c.course_id = m.course_id
           WHERE (? IS NULL OR m.kind = ?) AND (? IS NULL OR m.faculty_id = ?)
           GROUP BY c.doctor_id, c.doctor_name, m.department""",
        (kind, kind, *_sc()),
    )


# ----------------------------------------------------------------------------
# Quizzes & projects (announced by the doctor, visible to the course's students)
# ----------------------------------------------------------------------------
def add_assignment(course_id, doctor_id, kind, title, description, link, due_at):
    if kind not in ("Quiz", "Project"):
        return "bad_kind"
    with get_conn() as c:
        if not _doctor_owns_course(c, course_id, doctor_id):
            return "forbidden"
        c.execute("INSERT INTO assignments(course_id, doctor_id, kind, title, description, link, due_at) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (course_id, doctor_id, kind, title.strip()[:200], description.strip()[:2000], link.strip()[:500], due_at))
    return "ok"


def list_assignments(year=None, doctor_id=None):
    return _rows(
        """SELECT a.assignment_id, a.kind, a.title, a.description, a.link, a.due_at, a.created_at,
                  c.course_name, c.doctor_name, c.academic_year
           FROM assignments a JOIN courses c ON c.course_id = a.course_id
           WHERE (? IS NULL OR c.academic_year = ?) AND (? IS NULL OR c.doctor_id = ?)
             AND (? IS NULL OR c.faculty_id = ?)
           ORDER BY a.due_at""",
        (year, year, doctor_id, doctor_id, *_sc()),
    )


def delete_assignment(assignment_id, doctor_id):
    with get_conn() as c:
        c.execute("DELETE FROM assignments WHERE assignment_id = ? AND doctor_id = ?", (assignment_id, doctor_id))


def due_soon(year, days=3):
    now = config.now()
    return _rows(
        """SELECT a.kind, a.title, a.due_at, c.course_name
           FROM assignments a JOIN courses c ON c.course_id = a.course_id
           WHERE c.academic_year = ? AND a.due_at >= ? AND a.due_at <= ? AND (? IS NULL OR c.faculty_id = ?)
           ORDER BY a.due_at""",
        (year, now.strftime("%Y-%m-%d %H:%M"), (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M"), *_sc()),
    )
