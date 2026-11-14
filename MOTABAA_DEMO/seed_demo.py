#!/usr/bin/env python3
"""Fill the DEMO database (SQLite) with FAKE data so every screen has something to show.

    python seed_demo.py            # create (or top up) demo_data/motabaa_demo.db
    python seed_demo.py --reset    # delete the demo database first and rebuild it

DEMO ONLY: every name, e-mail and number below is invented. This file exists only in the demo edition; it
refuses to run if a PostgreSQL connection string is set. Accounts and passwords: see DEMO_ACCOUNTS.md.
"""
import argparse
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import dbdriver  # noqa: E402

DEMO_PASSWORD = "Demo@Pass2026"
LAT, LON = 30.0100, 31.2000                    # invented point, used for every demo faculty/hall
NAMES = ["أحمد علي", "منى حسن", "عمر خالد", "سارة محمود", "يوسف إبراهيم", "نور الدين", "هبة سمير", "كريم عادل",
         "ليلى فؤاد", "مصطفى رضا", "دينا شريف", "طارق وائل", "ريم صلاح", "إسلام ناصر", "هدى مجدي", "باسم ماهر",
         "آية رمضان", "زياد عصام", "مي فتحي", "حسام الدين", "رنا سعيد", "كمال أنور", "جنى حاتم", "شادي نبيل"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="delete the demo database first")
    args = ap.parse_args()
    dbdriver._guard()
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(dbdriver.DB_PATH + suffix):
                os.remove(dbdriver.DB_PATH + suffix)
    db.init_db()
    if db.has_admin():
        print("The demo database already has data. Use --reset to rebuild it.")
        return
    random.seed(7)
    # system admin ------------------------------------------------------------------------------------------
    db.create_staff("admin@demo.test", "مدير النظام (تجريبي)", DEMO_PASSWORD, "admin", staff_code="AD-001", must_change=False)
    db.set_university("جامعة تجريبية", "Demo University")
    st, fid = db.add_faculty("كلية التجارة (تجريبية)", "Faculty of Commerce (demo)", 4, "@demo.test", LAT, LON, 80,
                             "بيانات وهمية للتجربة فقط")
    db.set_scope(fid)
    db.create_staff("fadmin@demo.test", "مدير الكلية (تجريبي)", DEMO_PASSWORD, "faculty_admin", staff_code="FA-001", must_change=False)
    db.create_staff("dean@demo.test", "عميد الكلية (تجريبي)", DEMO_PASSWORD, "dean", staff_code="DN-001", must_change=False)
    docs = {}
    for i, (nm, spec) in enumerate([("د. منى السعيد", "محاسبة"), ("د. خالد عبد الله", "إدارة أعمال"),
                                    ("د. هالة فؤاد", "اقتصاد")], start=1):
        db.create_staff(f"dr{i}@demo.test", nm, DEMO_PASSWORD, "doctor", staff_code=f"DR-10{i}", specialization=spec,
                        must_change=False)
    for d in db.list_doctors():
        docs[d["email"]] = d
    # halls ---------------------------------------------------------------------------------------------------
    db.add_hall("مدرج 1", "Amphitheater", 300, LAT, LON, 60)
    db.add_hall("مدرج 2", "Amphitheater", 250, LAT + 0.0004, LON, 60)
    db.add_hall("قاعة سكاشن 1", "Section Room", 60, LAT, LON + 0.0004, 40)
    halls = {h["name"]: h["hall_id"] for h in db.list_halls()}
    # students ------------------------------------------------------------------------------------------------
    years, depts = config.year_names(4), ["محاسبة", "إدارة أعمال", "اقتصاد"]
    rows = []
    for i, nm in enumerate(NAMES):
        code = str(2024001 + i)
        rows.append([code, nm, f"s{code}@demo.test", years[0] if i < 16 else years[1], depts[i % 3],
                     "Affiliated" if i % 7 == 6 else "Regular", 1 + (i % 2)])
    db.import_roster_report(pd.DataFrame(rows, columns=["student_code", "full_name", "faculty_email", "academic_year",
                                                        "department", "study_type", "section_no"]))
    config.REQUIRE_EMAIL_VERIFY = False
    for code, nm, email, *_ in rows[:18]:                 # 18 of the 24 activated; the rest can try "first time"
        db.activate_student(code, email, DEMO_PASSWORD, f"{code}.home@example.test")
    # courses -------------------------------------------------------------------------------------------------
    plan = [("مبادئ المحاسبة", "dr1@demo.test", years[0]), ("مبادئ الإدارة", "dr2@demo.test", years[0]),
            ("مبادئ الاقتصاد", "dr3@demo.test", years[0]), ("محاسبة التكاليف", "dr1@demo.test", years[1])]
    for name, mail, year in plan:
        db.create_course(name, docs[mail]["id"], docs[mail]["full_name"], year, "First", 14)
    courses = db.list_courses()
    # timetable -----------------------------------------------------------------------------------------------
    for i, c in enumerate(courses):
        db.add_slot(c["course_id"], halls["مدرج 1" if i % 2 == 0 else "مدرج 2"], "Lecture", "", i % 5, "09:00", "10:30")
        db.add_slot(c["course_id"], halls["قاعة سكاشن 1"], "Section", "1", (i + 1) % 5, "12:00", "13:00")
    # lectures with attendance in the past ----------------------------------------------------------------------
    for c in courses:
        d = [x for x in docs.values() if x["id"] == c["doctor_id"]][0]
        for n in range(1, 6):
            lid, _ = db.upsert_session(c["course_id"], "Lecture", "", n, f"موضوع المحاضرة {n}", d["id"])
            db.set_attendance_code(lid, "DEMO1234", 60, d["id"])
            for code, *_r in rows:
                stu = db.get_student(code)
                if stu["academic_year"] == c["academic_year"] and random.random() < 0.78:
                    db.mark_present(code, lid, "DEMO1234", LAT, LON, 10)
            if n < 5:                                       # the 5th stays open: live attendance is demonstrable
                db.close_lecture(lid, d["id"])
                with db.get_conn() as cn:
                    cn.execute("UPDATE lectures SET date_created = ? WHERE lecture_id = ?",
                               (db._ts(config.now() - pd.Timedelta(days=(6 - n) * 7)), lid))
    d1 = docs["dr1@demo.test"]
    db.add_assignment(courses[0]["course_id"], d1["id"], "Quiz", "كويز الفصل الأول", "حل المسائل 1 إلى 5", "",
                      db._ts(config.now() + pd.Timedelta(days=2))[:16])
    db.add_complaint("2024001", "Facilities", "التكييف في المدرج 1 لا يعمل.")
    db.add_complaint(None, "Academic", "اقتراح: إضافة ساعات مكتبية أسبوعية.", "Suggestion")
    db.set_scope(None)
    print("Demo database ready:", dbdriver.DB_PATH)
    print("See DEMO_ACCOUNTS.md for the demo logins.")


if __name__ == "__main__":
    main()
