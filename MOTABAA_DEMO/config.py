"""config.py — central settings for Motabaa.

Everything environment-specific lives here (or in environment variables), so you never have to edit
business logic to change a path or a limit. The university name, the faculties and each faculty's
details (years, GPS fallback point, e-mail domain) are NOT here: the system admin enters them from the
app (pages "Faculties" / "Faculty dashboard") and they are stored in the database.
"""
import os

APP_NAME_AR, APP_NAME_EN = "مُتابع", "Motabaa"


def env(name, default=""):
    """Setting from the environment: MOTABAA_<NAME> (new) or the old EDUTRACK_<NAME> (still accepted)."""
    return os.getenv(f"MOTABAA_{name}") or os.getenv(f"EDUTRACK_{name}") or default


# --- Time ---------------------------------------------------------------------
# ONE application time zone for everything (Python code, database defaults, comparisons). Egypt observes
# daylight saving time again, so a fixed server-local clock is not enough.
TZ = env("TZ", "Africa/Cairo")
os.environ["TZ"] = TZ                       # the C library follow it too
try:
    import time as _time
    _time.tzset()
except AttributeError:                      # Windows has no tzset(); now() below still uses the explicit zone
    pass


def now():
    """Current time in the application time zone as a NAIVE datetime (the database stores naive local times)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(TZ)).replace(tzinfo=None)

# --- Auth -------------------------------------------------------------------
# There is NO default administrator. The first system admin is created on purpose with
#     python scripts/create_admin.py
# (production) -- see README. Nothing secret is stored in this file.
# Any valid e-mail is accepted. A faculty may additionally require a domain (set by the admin per faculty).
EMAIL_REGEX = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_BYTES = 72                                       # bcrypt limit
# bcrypt work factor: 12 = ~0.3 s per login on a small server (secure default). 10 is OWASP's minimum and
# ~4x faster; lower it ONLY if logins are your bottleneck. Existing hashes keep working either way.
BCRYPT_ROUNDS = min(14, max(10, int(env("BCRYPT_ROUNDS", "12"))))
LOGIN_MAX_FAILS = 5                                           # wrong passwords (same identifier + same IP) before a lock
LOGIN_MAX_FAILS_ANY_IP = 25                                   # ... same identifier from ANY address (distributed guessing)
LOGIN_LOCK_MIN = 10                                           # first lock length (minutes); doubles on every repeat
LOGIN_LOCK_MAX_MIN = 120                                      # ... but never longer than this
SESSION_IDLE_MIN = int(env("SESSION_IDLE_MIN", "120"))        # a session idle for this long must log in again
# Students prove they own their university mailbox (an e-mailed code) before an account is created.
# Production: on by default (needs SMTP). The demo edition turns it off (no mail server).
import edition as _edition
REQUIRE_EMAIL_VERIFY = env("REQUIRE_EMAIL_VERIFY", "1" if _edition.REQUIRE_EMAIL_VERIFY_DEFAULT else "0") == "1"

# Passwords that are refused whatever their length (project defaults, keyboard walks, the obvious).
_WEAK_PASSWORDS = {"password", "password1", "12345678", "123456789", "1234567890", "qwertyui", "qwerty123",
                   "11111111", "00000000", "iloveyou", "admin123", "admin1234", "motabaa123", "motabaa@123",
                   "24886555", "rz24886555", "abcd1234", "p@ssw0rd", "letmein1", "welcome1"}


def password_problem(pw: str):
    """None when the password is acceptable, else a short code: short | long | weak."""
    if len(pw) < MIN_PASSWORD_LEN:
        return "short"
    if len(pw.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return "long"
    if pw.lower() in _WEAK_PASSWORDS or len(set(pw)) < 3:
        return "weak"
    return None


# --- GPS attendance ---------------------------------------------------------
# Each HALL has its own GPS point + radius, set by the admin (page "Schedules > Halls").
# Each FACULTY may also have a fallback point (set on its dashboard) for halls without coordinates.
# Optional global fallback via environment (leave unset: attendance then needs a hall point or a faculty point).
_LAT, _LON = env("LAT"), env("LON")
DEFAULT_COORDS = (float(_LAT), float(_LON)) if _LAT and _LON else None
MAP_START = (30.0444, 31.2357)                                # only the starting value of the coordinate inputs
MAX_DISTANCE_M = int(env("RADIUS_M", "50"))                   # fallback radius (metres)
# Reject a location whose reported accuracy is worse than this (metres) — e.g. IP / Wi-Fi guesses.
MAX_ACCURACY_M = int(env("MAX_ACCURACY_M", "100"))

# --- Academic data ----------------------------------------------------------
ATTENDANCE_THRESHOLD = 75                                     # % below which a student is flagged
DEFAULT_YEARS, MAX_YEARS = 4, 7                               # each faculty chooses its own number of years


def year_names(n):
    """['1st Year', '2nd Year', ...] — the stored values (shown as written)."""
    suf = {1: "st", 2: "nd", 3: "rd"}
    return [f"{i}{suf.get(i, 'th')} Year" for i in range(1, int(n) + 1)]


YEARS = year_names(DEFAULT_YEARS)                             # default list; a faculty's own list: db.faculty_years()
SEMESTERS = ["First", "Second"]
COMPLAINT_TYPES = ["Academic", "Administrative", "Technical", "Facilities", "Other"]
COMPLAINT_STATUSES = ["Pending", "Under Review", "Resolved"]
STUDY_TYPES = ["Regular", "Affiliated"]                       # منتظم / منتسب
MENU_ICONS = env("MENU_ICONS", "1") == "1"                      # small monochrome symbols in the menu (not emoji); set 0 to hide
STAFF_ROLES = ["doctor", "dean", "faculty_admin", "admin"]    # admin = super admin (whole platform); faculty_admin = one faculty
DAYS = [0, 1, 2, 3, 4, 5]                                     # Saturday .. Thursday

# --- E-mail (password-reset codes) ------------------------------------------
# Gmail / Google Workspace: use an "App password". Leave empty to disable (admin can still reset by hand).
SMTP_HOST = env("SMTP_HOST", "")
SMTP_PORT = int(env("SMTP_PORT", "465"))
SMTP_USER = env("SMTP_USER", "")
SMTP_PASSWORD = env("SMTP_PASSWORD", "")
SMTP_FROM = env("SMTP_FROM", "") or SMTP_USER
