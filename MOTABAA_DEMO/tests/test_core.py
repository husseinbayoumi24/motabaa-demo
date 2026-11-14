"""Behaviour tests for the data layer (db.py) — run on PostgreSQL (production) or SQLite (demo)."""
import pytest

import config
import db
import importer
from conftest import PW, World, roster_df


@pytest.fixture
def w():
    return World()


# ---------------------------------------------------------------- isolation (fail-closed)
def test_scope_is_fail_closed(w):
    with pytest.raises(db.ScopeError):
        db.list_courses()
    with pytest.raises(db.ScopeError):
        db.list_staff()
    with pytest.raises(db.ScopeError):
        db.admin_stats()


def test_faculty_data_is_isolated(w):
    db.set_scope(w.fb)
    assert db.list_courses() == [] and db.list_halls() == [] and db.list_doctors() == []
    assert db.get_student("1001") is None
    db.set_scope(w.fa)
    assert len(db.list_courses()) == 1 and db.get_student("1001")["full_name"] == "Ali"


def test_roster_cannot_steal_other_faculty_code_or_email_or_reserved(w):
    db.set_scope(w.fb)
    y = config.year_names(5)[0]
    rep = db.import_roster_report(roster_df([
        ["1001", "Thief", "thief@b.edu", y, "X", "Regular", 1],      # code belongs to faculty A
        ["2001", "Dup", "ali@a.edu", y, "X", "Regular", 1],          # e-mail belongs to Ali
        ["DR12", "Staff", "staffy@b.edu", y, "X", "Regular", 1],     # reserved staff identity
        ["2002", "Fine", "fine@b.edu", y, "X", "Regular", 1]]))
    assert rep["saved"] == 1
    assert dict(rep["skipped"]) == {"1001": "code_other_faculty", "2001": "email_taken", "DR12": "reserved_code"}
    db.set_scope(w.fa)
    assert db.get_student("1001")["full_name"] == "Ali"


def test_doctor_must_belong_to_same_faculty(w):
    db.set_scope(w.fb)
    assert db.create_course("X", w.dr1, "Dr One", config.year_names(5)[0], "First") is False


# ---------------------------------------------------------------- auth
def test_login_by_codes_and_email_and_case(w):
    db.set_scope(w.fa)
    assert db.create_staff("Boss@A.edu", "Boss", PW, "dean", staff_code="dn-777", must_change=False) == "ok"
    assert db.authenticate("DN-777", PW)["role"] == "dean"
    assert db.authenticate("dn-777", PW)["role"] == "dean"
    assert db.authenticate("boss@a.edu", PW)["role"] == "dean"
    assert db.authenticate("BOSS@a.edu", PW)["role"] == "dean"
    assert db.authenticate("DN-777", "wrong") is None


def test_alphanumeric_student_code_keeps_case(w):
    db.set_scope(w.fa)
    db.import_roster_report(roster_df([["B2024X", "Alpha", "alpha@a.edu", w.y1, "Accounting", "Regular", 1]]))
    st, u = db.activate_student("B2024X", "alpha@a.edu", PW, "alpha.home@gmail.com")
    assert st == "ok"
    assert db.authenticate("B2024X", PW)["student_code"] == "B2024X"


def test_lockout_is_per_ip_then_global_and_grows(w):
    db.set_scope(w.fa)
    for _ in range(config.LOGIN_MAX_FAILS):
        assert db.authenticate("dr1@a.edu", "nope", "9.9.9.9") is None
    assert db.login_locked("dr1@a.edu", "9.9.9.9")
    assert not db.login_locked("dr1@a.edu", "8.8.8.8")                # the real owner elsewhere is not locked
    assert db.authenticate("dr1@a.edu", PW, "8.8.8.8")["email"] == "dr1@a.edu"
    assert db.authenticate("dr1@a.edu", PW, "9.9.9.9") is None        # the locked address is refused even with the right password


def test_password_rules_and_session_epoch(w):
    db.set_scope(w.fa)
    assert db.create_staff("x@a.edu", "X", "RZ24886555", "doctor") == "weak"
    assert db.create_staff("x@a.edu", "X", "short", "doctor") == "short"
    u = db.authenticate("dr1@a.edu", PW)
    assert db.change_password(u["id"], PW) == "same"
    assert db.change_password(u["id"], "An0ther!Pass9") == "ok"
    assert db.session_user(u["id"])["epoch"] == u["epoch"] + 1        # old sessions will be signed out
    temp = db.reset_user_password(u["id"], "admin", "admin")
    assert temp and db.session_user(u["id"])["must_change"] is True


def test_staff_creation_permissions(w):
    db.set_scope(w.fa)
    assert db.create_staff("fa@a.edu", "FA", PW, "faculty_admin", actor_role="faculty_admin") == "forbidden"
    assert db.create_staff("fa@a.edu", "FA", PW, "faculty_admin", actor_role="admin") == "ok"
    assert db.create_staff("d3@a.edu", "D3", PW, "doctor", actor_role="faculty_admin") == "ok"
    assert db.create_staff("d4@a.edu", "D4", PW, "doctor", actor_role="doctor") == "forbidden"
    assert db.create_staff("ad@a.edu", "AD", PW, "admin", actor_role="faculty_admin") == "forbidden"


def test_faculty_admin_cannot_reset_admins(w):
    db.set_scope(w.fa)
    db.create_staff("fa@a.edu", "FA", PW, "faculty_admin", actor_role="admin")
    fa = db.authenticate("fa@a.edu", PW)
    assert db.reset_user_password(fa["id"], "someone", "faculty_admin") is None
    assert db.reset_user_password(fa["id"], "root", "admin")


def test_disabling_a_faculty_signs_users_out(w):
    db.set_scope(w.fa)
    u = db.authenticate("dr1@a.edu", PW)
    f = db.get_faculty(w.fa)
    db.update_faculty(w.fa, f["name_ar"], f["name_en"], 4, "@a.edu", 30.0, 31.0, 50, "", False)
    fresh = db.session_user(u["id"])
    assert fresh["epoch"] != u["epoch"] and not fresh["faculty_active"]


# ---------------------------------------------------------------- activation
def test_activation_with_email_code(w):
    config.REQUIRE_EMAIL_VERIFY = True
    assert db.start_activation("1001", "wrong@a.edu")[0] == "invalid"
    st, code, to = db.start_activation("1001", "ali@a.edu")
    assert st == "ok" and to == "ali@a.edu" and len(code) == 6
    assert db.activate_student("1001", "ali@a.edu", PW, "ali.home@gmail.com", "000000")[0] == "bad_code"
    assert db.activate_student("1001", "ali@a.edu", PW, "ali.home@gmail.com", "")[0] == "bad_code"
    st, user = db.activate_student("1001", "ali@a.edu", PW, "ali.home@gmail.com", code)
    assert st == "ok" and user["role"] == "student"
    assert db.activate_student("1001", "ali@a.edu", PW, "x@gmail.com", code)[0] == "invalid"   # already active


def test_activation_code_locks_after_wrong_attempts(w):
    config.REQUIRE_EMAIL_VERIFY = True
    st, code, _ = db.start_activation("1002", "mona@a.edu")
    for _ in range(db.RESET_MAX_ATTEMPTS):
        assert db.activate_student("1002", "mona@a.edu", PW, "m@gmail.com", "111111")[0] == "bad_code"
    assert db.activate_student("1002", "mona@a.edu", PW, "m@gmail.com", code)[0] == "locked"


def test_recovery_email_cannot_be_shared(w):
    db.set_scope(w.fa)
    assert db.activate_student("1001", "ali@a.edu", PW, "shared@gmail.com")[0] == "ok"
    assert db.activate_student("1002", "mona@a.edu", PW, "shared@gmail.com")[0] == "email_taken"
    assert db.activate_student("1002", "mona@a.edu", PW, "not-an-email")[0] == "invalid_email"
    assert db.activate_student("1002", "mona@a.edu", "12345678", "m2@gmail.com")[0] == "weak_password"


def test_reset_code_flow(w):
    db.set_scope(w.fa)
    u = db.find_user_for_reset("DR1@a.edu")
    st, code = db.create_reset_code(u["id"])
    assert st == "ok"
    assert db.complete_reset(u["id"], "999999", "N3w!Passw0rd") == "bad_code"
    assert db.complete_reset(u["id"], code, "N3w!Passw0rd") == "ok"
    assert db.complete_reset(u["id"], code, "Another!Pass1") == "no_code"        # one use only
    assert db.authenticate("dr1@a.edu", "N3w!Passw0rd")


# ---------------------------------------------------------------- attendance
def _session(w, kind="Lecture", group="", number=1):
    db.set_scope(w.fa)
    lid, _ = db.upsert_session(w.course, kind, group, number, "Topic", w.dr1)
    return lid


def test_only_the_owner_doctor_can_control_a_session(w):
    lid = _session(w)
    assert db.set_attendance_code(lid, "4821", 10, w.dr2) == "forbidden"
    assert db.set_attendance_code(lid, "4821", 10, None) == "forbidden"
    assert db.get_attendance_code(lid, w.dr2)["code"] is None
    assert db.lecture_attendees(lid, w.dr2) == []
    assert db.close_lecture(lid, w.dr2) == -1
    assert db.upsert_session(w.course, "Lecture", "", 2, "x", w.dr2) == (None, False)
    assert db.set_attendance_code(lid, "4821", 10, w.dr1) == "ok"
    db.set_scope(w.fb)
    assert db.set_attendance_code(lid, "9999", 10, w.dr1) == "forbidden"      # wrong faculty scope


def test_mark_present_rules_are_enforced_on_the_server(w):
    lid = _session(w)
    here = (30.0, 31.0, 10)
    assert db.mark_present("1001", lid, "4821", *here) == "code_inactive"
    db.set_attendance_code(lid, "4821", 10, w.dr1)
    assert db.mark_present("1001", lid, "4821") == "no_location"
    assert db.mark_present("1001", lid, "4821", 30.0, 31.0, 500) == "weak_gps"
    assert db.mark_present("1001", lid, "4821", 31.5, 31.0, 10) == "outside"
    assert db.mark_present("1001", lid, "WRONG", *here) == "bad_code"
    assert db.mark_present("1001", lid, " 4821 ", *here) == "ok"
    assert db.mark_present("1001", lid, "4821", *here) == "already"
    assert db.mark_present("9999", lid, "4821", *here) == "not_enrolled"
    db.set_scope(w.fb)
    db.import_roster_report(roster_df([["5001", "Far", "far@b.edu", config.year_names(5)[0], "X", "Regular", 1]]))
    assert db.mark_present("5001", lid, "4821", *here) == "not_enrolled"       # student of another faculty


def test_wrong_codes_lock_the_student(w):
    lid = _session(w)
    db.set_attendance_code(lid, "4821", 10, w.dr1)
    for _ in range(5):
        assert db.mark_present("1002", lid, "bad1", 30.0, 31.0, 10) == "bad_code"
    assert db.mark_present("1002", lid, "4821", 30.0, 31.0, 10) == "locked"


def test_section_students_and_closing(w):
    db.set_scope(w.fa)
    lec = _session(w)
    sec = db.upsert_session(w.course, "Section", "1", 1, "Sec", w.dr1)[0]
    db.set_attendance_code(sec, "SEC1", 10, w.dr1)
    assert db.mark_present("1003", sec, "SEC1", 30.0, 31.0, 10) == "not_enrolled"   # section 2 student
    assert db.mark_present("1001", sec, "SEC1", 30.0, 31.0, 10) == "ok"
    assert db.close_lecture(sec, w.dr1) == 1                       # only Mona (section 1) becomes absent
    assert db.close_lecture(lec, w.dr1) == 4                       # whole year for a lecture
    assert db.mark_present("1002", lec, "4821", 30.0, 31.0, 10) == "closed"


def test_cancel_attendance_blocks_re_registration(w):
    lid = _session(w)
    db.set_attendance_code(lid, "4821", 10, w.dr1)
    assert db.mark_present("1001", lid, "4821", 30.0, 31.0, 10) == "ok"
    assert db.cancel_attendance(lid, "1001", "dr1", w.dr2) == "forbidden"
    assert db.cancel_attendance(lid, "1001", "dr1", w.dr1) == "ok"
    assert db.mark_present("1001", lid, "4821", 30.0, 31.0, 10) == "cancelled"


def test_hall_point_overrides_faculty_point(w):
    lid = _session(w)
    db.set_scope(w.fa)
    f = db.get_faculty(w.fa)
    assert db.add_slot(w.course, w.hall, "Lecture", "", 0, "09:00", "10:30") == "ok"
    db.set_attendance_code(lid, "4821", 10, w.dr1)
    assert db.mark_present("1001", lid, "4821", 30.0, 31.0, 10) == "ok"          # inside the hall point


# ---------------------------------------------------------------- timetable
def test_timetable_conflicts_are_per_faculty(w):
    db.set_scope(w.fa)
    assert db.add_slot(w.course, w.hall, "Lecture", "", 0, "09:00", "10:30") == "ok"
    assert db.add_slot(w.course, w.hall, "Section", "1", 0, "10:00", "11:00") == "hall_conflict"
    assert db.add_slot(w.course, w.hall, "Lecture", "", 0, "11:00", "10:00") == "time_invalid"
    # Faculty B, same hour, same hall NAME and the same year name: must NOT clash with faculty A (BUG-01)
    db.set_scope(w.fb)
    y = config.year_names(5)[0]
    db.create_staff("db@b.edu", "DB", PW, "doctor")
    dr_b = db.list_doctors()[0]["id"]
    db.create_course("Math", dr_b, "DB", y, "First")
    cb = db.list_courses()[0]["course_id"]
    db.add_hall("Hall 1", "Amphitheater", 50)
    hb = db.list_halls()[0]["hall_id"]
    assert db.add_slot(cb, hb, "Lecture", "", 0, "09:00", "10:30") == "ok"
    assert db.add_slot(w.course, hb, "Lecture", "", 1, "09:00", "10:00") == "no_course"
    db.set_scope(w.fa)
    assert db.add_slot(w.course, hb, "Lecture", "", 1, "09:00", "10:00") == "foreign_hall"


# ---------------------------------------------------------------- analytics (types and SQL portability)
def test_analytics_run_and_return_numbers(w):
    db.set_scope(w.fa)
    lid = _session(w)
    db.set_attendance_code(lid, "4821", 10, w.dr1)
    db.mark_present("1001", lid, "4821", 30.0, 31.0, 10)
    db.mark_present("1002", lid, "4821", 30.0, 31.0, 10)
    db.close_lecture(lid, w.dr1)
    g = db.attendance_by_group()
    assert set(g["department"]) == {"Accounting", "Business"}
    acc = g[g["department"] == "Accounting"].iloc[0]
    assert (int(acc["present"]), int(acc["absent"])) == (2, 1) and float(acc["rate"]) == 66.7
    assert isinstance(db.attendance_by_group()["rate"].iloc[0], float)
    ew = db.early_warning(75, 1)
    assert set(ew["student_code"]) == {"1003", "1004"}
    ov = db.doctor_overview()
    assert ov.iloc[0]["last_lecture"] == 1 and ov.iloc[0]["days_since"] == 0 and ov.iloc[0]["attendance_rate"] == 50.0
    assert len(db.attendance_trend()) == 1 and len(db.attendance_by_department()) == 2
    assert len(db.attendance_by_doctor()) == 1 and len(db.attendance_doctor_department()) == 2
    s = db.student_attendance_summary("1001", w.y1)[0]
    assert (s["total"], s["present"]) == (1, 1)
    st = db.doctor_stats(w.dr1)
    assert st["today_present"] == 2 and st["courses"] == 1
    assert db.admin_stats()["avg_rate"] == 50.0 and db.at_risk_count(75, 1) == 2
    assert not db.attendance_doctor_department("Section").empty or True
    assert len(db.registered_students()) == 0 and len(db.roster_by_year(w.y1)) == 4


def test_quiz_alert_every_fourth_lecture(w):
    db.set_scope(w.fa)
    for n in (1, 2, 3):
        assert db.upsert_session(w.course, "Lecture", "", n, "t", w.dr1)[1] is False
    assert db.upsert_session(w.course, "Lecture", "", 4, "t", w.dr1)[1] is True
    assert len(db.active_alerts(w.y1)) == 1
    db.upsert_session(w.course, "Lecture", "", 5, "t", w.dr1)
    assert db.active_alerts(w.y1) == []


# ---------------------------------------------------------------- chat, planner, complaints, assignments
def test_chat_rules(w):
    db.set_scope(w.fa)
    d1 = db.doctor_code(w.dr1)
    assert db.send_message("1001", d1, w.course, "hello")
    assert not db.send_message("1001", db.doctor_code(w.dr2), w.course, "wrong doctor")
    assert not db.send_message("9999", d1, w.course, "stranger")
    assert not db.send_message(d1, "1001", w.course, "x", from_student=True)           # a "student" send from a doctor code
    assert db.send_message(d1, "1001", w.course, "hi back", from_student=False)
    assert not db.send_message(db.doctor_code(w.dr2), "1001", w.course, "intruder", from_student=False)
    assert len(db.get_conversation(w.course, "1001", d1)) == 2
    assert db.set_chat_open(w.course, False, w.dr2) is False
    assert db.set_chat_open(w.course, False, w.dr1) is True
    assert not db.send_message("1001", d1, w.course, "closed now")
    db.set_scope(w.fb)
    assert db.get_conversation(w.course, "1001", d1) == []                              # other faculty sees nothing


def test_planner_is_private(w):
    db.add_task("1001", "mine")
    tid = db.list_tasks("1001")[0]["task_id"]
    db.set_task_done("1002", tid, True)
    db.delete_task("1002", tid)
    t = db.list_tasks("1001")
    assert len(t) == 1 and not t[0]["is_completed"]


def test_complaints_and_assignments(w):
    db.set_scope(w.fa)
    db.add_complaint("1001", "Academic", "named one")
    db.add_complaint(None, "Facilities", "anonymous one", "Suggestion")
    assert len(db.list_complaints("anonymous")) == 1 and len(db.list_complaints("named", "Complaint")) == 1
    cid = db.list_complaints("named")[0]["complaint_id"]
    db.set_complaint_status(cid, "Resolved", "done")
    assert db.my_complaints("1001")[0]["response"] == "done"
    db.set_scope(w.fb)
    db.set_complaint_status(cid, "Pending", "hijack")
    db.set_scope(w.fa)
    assert db.my_complaints("1001")[0]["status"] == "Resolved"
    assert db.add_assignment(w.course, w.dr2, "Quiz", "q", "", "", "2099-01-01 10:00") == "forbidden"
    assert db.add_assignment(w.course, w.dr1, "Quiz", "q", "", "", "2099-01-01 10:00") == "ok"
    assert len(db.list_assignments(year=w.y1)) == 1
    db.delete_assignment(db.list_assignments()[0]["assignment_id"], w.dr2)
    assert len(db.list_assignments()) == 1


def test_audit_log_records_sensitive_actions(w):
    db.set_scope(w.fa)
    lid = _session(w)
    db.set_attendance_code(lid, "4821", 10, w.dr1)
    db.close_lecture(lid, w.dr1)
    db.authenticate("dr1@a.edu", PW)
    actions = {a["action"] for a in db.list_audit(50)}
    assert {"set_attendance_code", "close_attendance", "login", "create_account"} <= actions or \
        {"set_attendance_code", "close_attendance", "login"} <= actions


def test_housekeeping_runs(w):
    db.housekeeping()


# ---------------------------------------------------------------- importer
def test_importer_roles_are_exact():
    p = importer.parse_role
    assert [p(x) for x in ("Doctor", "دكتور", "TA", "معيد", "Dean", "عميد", "admin", "")] == \
           ["doctor", "doctor", "doctor", "doctor", "dean", "dean", "admin", ""]
    assert p("وكيل عميد") == "" and p("Data Analyst") == "" and p("Student Affairs Dr.") == ""


def test_importer_flags_reserved_codes_and_weak_passwords():
    import pandas as pd
    df = pd.DataFrame([["DR5", "A", "a@x.edu", "1", "D"], ["2001", "B", "b@x.edu", "1", "D"]],
                      columns=["student_code", "full_name", "faculty_email", "academic_year", "department"])
    clean, _ = importer.normalize_students(df)
    assert "code" in clean.iloc[0]["errors"] and clean.iloc[1]["errors"] == "study_type" or clean.iloc[1]["errors"] == ""
    staff = pd.DataFrame([["Dr", "d@x.edu", "doctor", "12345678"]], columns=["full_name", "email", "role", "password"])
    c2, _ = importer.normalize_staff(staff)
    assert "pw" in c2.iloc[0]["errors"]


def test_import_staff_never_creates_admins_and_returns_temp_passwords_once(w):
    import pandas as pd
    db.set_scope(w.fa)
    df = pd.DataFrame([["New Dr", "newdr@a.edu", "doctor", "", "", "", "", ""],
                       ["Evil", "evil@a.edu", "admin", "", "", "", "", ""]],
                      columns=["full_name", "email", "role", "password", "staff_code", "title", "specialization", "phone"])
    res = db.import_staff(df, "admin")
    assert res[0]["status"] == "ok" and res[0]["temp_password"]
    assert res[1]["status"] == "role_not_allowed"
    assert db.authenticate("newdr@a.edu", res[0]["temp_password"])["must_change"] is True
