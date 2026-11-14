"""Opens EVERY page of EVERY role in the real Streamlit app (headless) and fails on any exception."""
import os

import pytest
from streamlit.testing.v1 import AppTest

import config
import db
from conftest import PW, ROOT, World

APP = os.path.join(ROOT, "app.py")


def _run(user, page=None, **state):
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["user"] = user
    at.session_state["last_seen"] = config.now()
    at.session_state["lang"] = state.pop("lang", "ar")
    if page:
        at.session_state["page"] = page
    for k, v in state.items():
        at.session_state[k] = v
    return at.run()


@pytest.fixture
def people():
    w = World()
    db.set_scope(w.fa)
    db.create_staff("dean@a.edu", "Dean", PW, "dean", must_change=False)
    db.create_staff("fa@a.edu", "FAdmin", PW, "faculty_admin", must_change=False)
    db.clear_scope()
    db.create_staff("root@x.org", "Root", PW, "admin", staff_code="AD-001", must_change=False)
    db.set_scope(w.fa)
    lid, _ = db.upsert_session(w.course, "Lecture", "", 1, "Intro", w.dr1)
    db.set_attendance_code(lid, "4821", 10, w.dr1)
    db.activate_student("1001", "ali@a.edu", PW, "ali.home@gmail.com")
    db.mark_present("1001", lid, "4821", 30.0, 31.0, 10)
    db.add_complaint("1001", "Academic", "markdown **bold** [x](http://evil.test)")
    db.send_message("1001", db.doctor_code(w.dr1), w.course, "![img](http://evil.test/p.png) hi")
    db.add_slot(w.course, w.hall, "Lecture", "", 0, "09:00", "10:00")
    db.clear_scope()
    users = {r: db.authenticate(c, PW) for r, c in
             (("student", "1001"), ("doctor", "dr1@a.edu"), ("dean", "dean@a.edu"),
              ("faculty_admin", "fa@a.edu"), ("admin", "AD-001"))}
    assert all(users.values())
    return w, users


@pytest.mark.parametrize("role", ["student", "doctor", "dean", "faculty_admin", "admin"])
@pytest.mark.parametrize("lang", ["ar", "en"])
def test_every_page_renders(people, role, lang):
    import app  # noqa: F401  (only to read PAGES without running main twice)
    w, users = people
    pages = list(app.PAGES[role])
    for page in pages:
        at = _run(users[role], page, lang=lang, _admin_fid=w.fa)
        assert not at.exception, f"{role}/{page}: {[e.value for e in at.exception]}"


def test_login_page_and_wrong_password(people):
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    at.text_input[0].set_value("1001"); at.text_input[1].set_value("bad")
    [b for b in at.button if b.key.startswith("FormSubmitter:login_form")][0].click().run()
    assert not at.exception and at.error


def test_session_is_dropped_after_password_change(people):
    w, users = people
    stale = dict(users["doctor"])
    db.set_scope(w.fa)
    db.change_password(stale["id"], "Brand-New!Pass7")
    db.clear_scope()
    at = _run(stale, "home")
    assert not at.exception
    assert at.session_state["user"] is None and any("سجّل" in w_.value or "log in" in w_.value.lower() for w_ in at.warning)


def test_idle_session_expires(people):
    w, users = people
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["user"] = users["doctor"]
    at.session_state["last_seen"] = config.now() - __import__("datetime").timedelta(minutes=config.SESSION_IDLE_MIN + 5)
    at.run()
    assert at.session_state["user"] is None


def test_user_text_is_shown_literally_and_csv_is_safe():
    import pandas as pd
    import app
    s = app.plain("**bold** [x](http://evil.test) ![i](http://evil.test/p.png) <b>x</b>\nline2")
    assert "\\[x\\]" in s and "\\!\\[i\\]" in s and "\\<b\\>" in s and "  \n" in s
    df = pd.DataFrame({"a": ["=HYPERLINK(\"http://evil\")", "+1", "-1", "@x", "ok"], "n": [1, 2, 3, 4, 5]})
    out = app.csv_safe(df)
    assert list(out["a"]) == ["'=HYPERLINK(\"http://evil\")", "'+1", "'-1", "'@x", "ok"] and list(out["n"]) == [1, 2, 3, 4, 5]
    assert app.pw_error("short", "short") and app.pw_error("RZ24886555", "RZ24886555") and app.pw_error("Str0ng!Pass1", "x")
    assert app.pw_error("Str0ng!Pass1", "Str0ng!Pass1") is None
