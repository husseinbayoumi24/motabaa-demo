"""Test setup. The SAME tests run against both editions:
   production:  MOTABAA_TEST_DATABASE_URL=postgresql://... pytest   (the schema is re-created from database/*.sql)
   demo:        pytest                                              (a throw-away SQLite file)
WARNING (production): the test database is wiped. Use a database that exists only for tests."""
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import edition  # noqa: E402

if edition.IS_DEMO:
    os.environ["MOTABAA_DEMO_DB"] = os.path.join(tempfile.mkdtemp(prefix="motabaa_test_"), "tests_demo.db")
    for v in ("MOTABAA_DATABASE_URL", "DATABASE_URL"):
        os.environ.pop(v, None)
else:
    url = os.environ.get("MOTABAA_TEST_DATABASE_URL")
    if not url:
        pytest.exit("Set MOTABAA_TEST_DATABASE_URL to a DISPOSABLE PostgreSQL database (it is wiped).", 2)
    os.environ["MOTABAA_DATABASE_URL"] = url

os.environ.setdefault("MOTABAA_BCRYPT_ROUNDS", "10")      # fast tests
import config  # noqa: E402
import db      # noqa: E402
import dbdriver  # noqa: E402

TABLES = ["attendance_attempts", "attendance", "alerts", "chat_messages", "assignments", "schedule", "course_settings",
          "lectures", "courses", "halls", "private_planner", "complaints", "password_resets", "activation_codes",
          "login_attempts", "audit_log", "users", "university_roster", "faculties", "settings"]


@pytest.fixture(scope="session", autouse=True)
def _database():
    if not edition.IS_DEMO:
        import psycopg
        with psycopg.connect(os.environ["MOTABAA_DATABASE_URL"], autocommit=True) as c:
            # drop only what this role owns (works for a normal, non-superuser owner of the test database)
            for (name,) in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' "
                                     "AND tableowner = current_user").fetchall():
                c.execute(f'DROP TABLE IF EXISTS "{name}" CASCADE')
            c.execute("DROP VIEW IF EXISTS v_attendance_matrix CASCADE")
            c.execute("DROP EXTENSION IF EXISTS citext CASCADE")
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import migrate
        assert migrate.main() == 0
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def clean():
    with db.get_conn() as c:
        for tname in TABLES:
            if edition.IS_DEMO:
                c.execute(f"DELETE FROM {tname}")
            else:
                c.execute(f"DELETE FROM {tname}")
        if edition.IS_DEMO:
            c.execute("DELETE FROM sqlite_sequence")
    db._CACHE.clear()
    db.clear_scope()
    config.REQUIRE_EMAIL_VERIFY = False
    yield
    db.clear_scope()


PW = "Str0ng!Pass1"


def roster_df(rows):
    import pandas as pd
    return pd.DataFrame(rows, columns=["student_code", "full_name", "faculty_email", "academic_year",
                                       "department", "study_type", "section_no"])


class World:
    """Two faculties; faculty A has 2 doctors, 1 course (taught by dr1), 4 students in 2 sections, 1 hall."""

    def __init__(self):
        st, self.fa = db.add_faculty("كلية أ", "Faculty A", 4, "@a.edu", 30.0, 31.0, 50)
        st, self.fb = db.add_faculty("كلية ب", "Faculty B", 5, "@b.edu", 29.0, 30.0, 50)
        y1 = config.year_names(4)[0]
        self.y1 = y1
        db.set_scope(self.fa)
        assert db.create_staff("dr1@a.edu", "Dr One", PW, "doctor", must_change=False) == "ok"
        assert db.create_staff("dr2@a.edu", "Dr Two", PW, "doctor", must_change=False) == "ok"
        docs = {d["email"]: d["id"] for d in db.list_doctors()}
        self.dr1, self.dr2 = docs["dr1@a.edu"], docs["dr2@a.edu"]
        db.import_roster_report(roster_df([
            ["1001", "Ali", "ali@a.edu", y1, "Accounting", "Regular", 1],
            ["1002", "Mona", "mona@a.edu", y1, "Accounting", "Regular", 1],
            ["1003", "Omar", "omar@a.edu", y1, "Accounting", "Affiliated", 2],
            ["1004", "Sara", "sara@a.edu", y1, "Business", "Regular", 2]]))
        assert db.create_course("Accounting 1", self.dr1, "Dr One", y1, "First", 14)
        self.course = db.list_courses()[0]["course_id"]
        db.add_hall("Hall 1", "Amphitheater", 100, 30.0, 31.0, 40)
        self.hall = db.list_halls()[0]["hall_id"]
        db.clear_scope()
