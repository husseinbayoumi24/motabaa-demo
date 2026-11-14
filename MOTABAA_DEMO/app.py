"""app.py — Motabaa (مُتابع): UI + routing (Streamlit).

Design: one look per role (student = indigo, doctor = teal, dean = rose, admin = amber), each with its own home
dashboard and menu. The menu is responsive: computers (>= 992 px) get a top bar; phones and tablets get a side
drawer that opens and closes. No emoji anywhere in the interface.
Run:  streamlit run app.py
"""
import functools
import html
import io
import os
import re
import logging
import secrets
from datetime import date, time as dtime, timedelta

import pandas as pd
import streamlit as st

import config
import db
import edition
import importer
import mailer
from i18n import t

try:  # lightweight browser-geolocation component
    from streamlit_js_eval import get_geolocation
    HAS_GPS = True
except ImportError:
    HAS_GPS = False

FAVICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "favicon.png")
st.set_page_config(page_title=f"{config.APP_NAME_AR} | {config.APP_NAME_EN}",
                   page_icon=FAVICON if os.path.exists(FAVICON) else None, layout="wide",
                   initial_sidebar_state="auto")

# Material icon for every menu entry
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ICONS = {
    "assignments": "assignment", "section_attendance": "group", "account": "account_circle",
    "staff": "supervisor_account",
    "home": "home", "attendance": "location_on", "syllabus": "menu_book", "planner": "checklist",
    "chat": "chat", "complaints": "support_agent", "profile": "person",
    "syllabus_tracker": "edit_note", "attendance_control": "fact_check",
    "complaints_admin": "inbox", "analytics": "insights", "roster": "groups", "manage": "settings",
    "schedule": "calendar_month", "schedule_admin": "edit_calendar", "registered": "badge",
    "doctors_monitor": "monitoring", "dean_attendance": "bar_chart",
    "faculties": "domain", "faculty_dashboard": "dashboard",
}
# (accent 1, accent 2, max content width) per context
THEMES = {
    "auth":    ("#4f46e5", "#7c3aed", "560px"),
    "student": ("#4f46e5", "#7c3aed", "860px"),
    "doctor":  ("#0d9488", "#059669", "1120px"),
    "dean":    ("#be123c", "#7e22ce", "1300px"),
    "admin":   ("#d97706", "#1e293b", "1400px"),
    "faculty_admin": ("#0369a1", "#1e293b", "1400px"),
}


# ============================================================================
# Setup, theming, small UI helpers
# ============================================================================
logging.basicConfig(level=getattr(logging, config.env("LOG_LEVEL", "INFO").upper(), logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@st.cache_resource
def _boot():
    db.init_db()
    if not db.has_admin():
        with db.get_conn() as c:
            c.execute(
                "INSERT INTO users (email, password_hash, role, full_name) VALUES (?, ?, 'admin', ?)",
                ("admin@motabaa.edu", db.hash_password("Admin@1234"), "مدير النظام")
            )
    return True


_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|>~<$&:])")


def plain(text) -> str:
    """User-written text shown LITERALLY: no markdown, no links, no images, no HTML (stops injected
    phishing links / tracking pixels in chat messages, topics, complaints ...)."""
    return _MD_SPECIAL.sub(r"\\\1", str(text if text is not None else "")).replace("\n", "  \n")


_CSV_RISKY = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Spreadsheet-formula injection: a cell that starts with = + - @ would run as a formula in Excel."""
    def fix(v):
        return "'" + v if isinstance(v, str) and v.startswith(_CSV_RISKY) else v
    return df.apply(lambda col: col.map(fix) if (col.dtype == object or pd.api.types.is_string_dtype(col)) else col)


def client_ip() -> str:
    """Address of the browser as seen by the reverse proxy (X-Forwarded-For, first hop). Empty if unknown.
    Only trustworthy behind a proxy that overwrites the header (deploy/nginx.conf does)."""
    try:
        h = st.context.headers
        raw = h.get("X-Forwarded-For") or h.get("X-Real-Ip") or ""
        return raw.split(",")[0].strip()[:64]
    except Exception:
        return ""


def pw_error(p1, p2):
    """One place for the password rules. Returns an error message or None."""
    problem = config.password_problem(p1)
    if problem == "short":
        return t("pw_too_short", n=config.MIN_PASSWORD_LEN)
    if problem == "long":
        return t("pw_too_long")
    if problem == "weak":
        return t("pw_weak")
    if p1 != p2:
        return t("pw_mismatch")
    return None


def org_name() -> str:
    return university_name()


def init_state():
    st.session_state.setdefault("lang", "ar")
    st.session_state.setdefault("user", None)
    st.session_state.setdefault("onboard", None)


def pick_name(ar, en):
    """Arabic or English name according to the language buttons; falls back to whichever exists."""
    first, second = (ar, en) if st.session_state.get("lang", "ar") == "ar" else (en, ar)
    return (first or second or "").strip()


def fac_name(f):
    """Name of a faculty row (dict with name_ar / name_en)."""
    return pick_name(f.get("name_ar"), f.get("name_en")) if f else ""


def user_faculty(user):
    return pick_name(user.get("faculty_name_ar"), user.get("faculty_name_en"))


def university_name():
    u = db.get_university()
    return pick_name(u["name_ar"], u["name_en"])


def num_in(text, default=None):
    """Number typed with Arabic or English digits (and ٫ or , as the decimal mark) -> float, else default."""
    s = db.clean_digits(text).strip().replace("٫", ".").replace(",", ".")
    try:
        return float(s) if s else default
    except ValueError:
        return default


def map_start():
    """Starting coordinates of the map inputs: the faculty's own point, else a neutral default."""
    p = db.faculty_point()
    return (p["lat"], p["lon"]) if p else config.MAP_START


CSS = """
<style>
:root {--a1: __A1__; --a2: __A2__; --a1-soft: rgba(__A1RGB__, .12); --danger: #c62828; --warn: #b45309;}
.block-container {max-width: __MAXW__; padding-top: 1rem; padding-bottom: calc(4rem + env(safe-area-inset-bottom, 0px));}
footer {visibility: hidden;}
img, svg {max-width: 100%;}
[data-testid="stDataFrame"] {max-width: 100%;}
[data-baseweb="tab-list"] {overflow-x: auto; flex-wrap: nowrap;} [data-baseweb="tab"] {white-space: nowrap;}

/* hero + banners */
.hero {background: linear-gradient(135deg, var(--a1), var(--a2)); color: #fff; border-radius: 22px;
       padding: 1.4rem 1.5rem; margin-bottom: 1rem; box-shadow: 0 10px 28px rgba(0,0,0,.18);}
.hero h2 {margin: 0; padding: 0; color: #fff; font-size: 1.6rem;}
.hero p {margin: .3rem 0 0; opacity: .93;}
.chip {display: inline-block; background: rgba(255,255,255,.22); border: 1px solid rgba(255,255,255,.45);
       border-radius: 999px; padding: .15rem .75rem; margin: .6rem .4rem 0 0; font-size: .82rem;}
.alert-banner {background: #b3261e; color: #fff; padding: .85rem 1rem; border-radius: 14px; border: 2px solid #7f1a15;
               margin-bottom: .6rem; font-weight: 600;}
.alert-banner.soft {background: #b45309; border-color: #7c3a06;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 18px;}
[data-testid="stMetric"] {text-align: center;}
[data-baseweb="input"], [data-baseweb="select"] > div, [data-baseweb="textarea"] {border-radius: 12px !important;}

/* buttons: every button has a visible border and a distinct colour */
[data-testid^="stBaseButton-"] {border: 2px solid var(--a1) !important; border-radius: 12px !important;
       min-height: 2.75rem; font-weight: 700; transition: background .15s, box-shadow .15s;}
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"] {
       background: transparent !important; color: var(--a1) !important;}
[data-testid="stBaseButton-secondary"]:hover, [data-testid="stBaseButton-secondaryFormSubmit"]:hover {
       background: var(--a1-soft) !important;}
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"] {
       background: linear-gradient(135deg, var(--a1), var(--a2)) !important; color: #fff !important; border-color: var(--a2) !important;}
[data-testid^="stBaseButton-primary"]:hover {box-shadow: 0 4px 14px rgba(__A1RGB__, .45);}
[data-testid^="stBaseButton-"]:disabled {opacity: .45;}
[class*="st-key-del_"] button, [class*="st-key-stop_"] button, [class*="st-key-close_"] button {
       border-color: var(--danger) !important; color: var(--danger) !important; background: transparent !important;}
[class*="st-key-logout"] button {border-color: var(--warn) !important; color: var(--warn) !important; background: transparent !important;}

/* top menu pills (computers) */
.brand {font-size: 1.35rem; font-weight: 800; color: var(--a1);}
.st-key-topbar {border: 2px solid var(--a1); border-radius: 18px; padding: .55rem .9rem; margin-bottom: 1rem;
       background: var(--a1-soft);}
[data-testid="stBaseButton-pills"] {border-radius: 999px !important; background: transparent !important;
       color: var(--a1) !important; min-height: 2.4rem;}
[data-testid="stBaseButton-pillsActive"] {border-radius: 999px !important; background: var(--a1) !important;
       color: #fff !important; border-color: var(--a1) !important; min-height: 2.4rem;}

/* side drawer menu (phones and tablets) */
[data-testid="stSidebar"] {border-inline-end: 3px solid var(--a1);}
.sb-user {border: 2px solid var(--a1); border-radius: 16px; padding: .7rem .9rem; background: var(--a1-soft); margin: .2rem 0 1rem;}
.sb-user b {display: block; font-size: 1.02rem;} .sb-user span {font-size: .82rem; opacity: .8;}
.st-key-nav_side [role="radiogroup"] {gap: .45rem; width: 100%;}
.st-key-nav_side label {width: 100%; border: 2px solid rgba(128,128,128,.35); border-radius: 14px;
       padding: .65rem .9rem !important; margin: 0 !important; cursor: pointer; transition: border-color .15s, background .15s;}
.st-key-nav_side label:hover {border-color: var(--a1); background: var(--a1-soft);}
.st-key-nav_side label > div:first-child {display: none;}
.st-key-nav_side label:has(input:checked) {background: var(--a1); border-color: var(--a1);}
.st-key-nav_side label:has(input:checked) * {color: #fff !important; font-weight: 700;}
.st-key-nav_side label p {font-size: 1rem; margin: 0;}
[data-testid="stSidebarCollapsedControl"] button, [data-testid="stExpandSidebarButton"],
[data-testid="stSidebarCollapseButton"] button, [data-testid="stSidebarHeader"] button {
       border: 2px solid var(--a1) !important; border-radius: 12px !important; background: var(--a1-soft) !important;
       color: var(--a1) !important; min-width: 2.6rem; min-height: 2.6rem;}

/* one menu per screen size */
@media (min-width: 992px) {
  [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], [data-testid="stExpandSidebarButton"],
  [data-testid="collapsedControl"] {display: none !important;}
}
@media (max-width: 991px) {
  .st-key-topbar {display: none !important;}
  .block-container {padding-left: 1rem; padding-right: 1rem;}
}
/* tablets: three KPI tiles per row */
@media (min-width: 641px) and (max-width: 991px) {
  [class*="st-key-kpis"] [data-testid="stHorizontalBlock"] {flex-wrap: wrap; gap: .6rem;}
  [class*="st-key-kpis"] [data-testid="stColumn"], [class*="st-key-kpis"] [data-testid="column"] {
      min-width: calc(33.33% - .6rem) !important; flex: 1 1 calc(33.33% - .6rem) !important;}
}
/* phones */
@media (max-width: 640px) {
  .block-container {padding-left: .7rem; padding-right: .7rem;}
  .hero {padding: 1.1rem 1.1rem; border-radius: 18px;} .hero h2 {font-size: 1.3rem;}
  .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {width: 100%;}
  input, textarea, select {font-size: 16px !important;}
  [data-testid^="stBaseButton-"] {min-height: 2.9rem;}
  [class*="st-key-kpis"] [data-testid="stHorizontalBlock"] {flex-wrap: wrap; gap: .5rem;}
  [class*="st-key-kpis"] [data-testid="stColumn"], [class*="st-key-kpis"] [data-testid="column"] {
      min-width: calc(50% - .5rem) !important; flex: 1 1 calc(50% - .5rem) !important;}
}
__AUTH__
__RTL__
</style>
"""


def inject_css(ctx):
    a1, a2, maxw = THEMES[ctx]
    rgb = ",".join(str(int(a1[i:i + 2], 16)) for i in (1, 3, 5))
    auth = ('[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], '
            '[data-testid="stExpandSidebarButton"], [data-testid="collapsedControl"] {display:none !important;}'
            ) if ctx == "auth" else ""
    # Arabic: right-to-left text inside the page and the drawer (the page layout itself stays native)
    rtl = (".block-container, [data-testid='stSidebar'] {direction: rtl; text-align: right;} .stChatMessage {direction: rtl;}"
           ) if st.session_state.lang == "ar" else ""
    css = (CSS.replace("__A1RGB__", rgb).replace("__A1__", a1).replace("__A2__", a2).replace("__MAXW__", maxw)
              .replace("__AUTH__", auth).replace("__RTL__", rtl))
    st.markdown(css, unsafe_allow_html=True)


def hero(title, sub, chips=()):
    chips_html = "".join(f'<span class="chip">{html.escape(c)}</span>' for c in chips)
    st.markdown(f'<div class="hero"><h2>{html.escape(title)}</h2><p>{html.escape(sub)}</p>{chips_html}</div>',
                unsafe_allow_html=True)


def kpis(items):
    """items: [(unused_icon, label, value)] -> a row of bordered metric tiles."""
    n = st.session_state.get("_kpi_n", 0)                  # a page may show several rows: keys must be unique
    st.session_state["_kpi_n"] = n + 1
    with st.container(key=f"kpis_{n}"):
        for col, (_icon, label, value) in zip(st.columns(len(items)), items):
            with col, st.container(border=True):
                st.metric(label, value)


def nav_label(key):
    label = t(f"menu_{key}")
    return f":material/{ICONS[key]}: {label}" if config.MENU_ICONS else label


def go(page):
    st.session_state.page = page


def quick_actions(keys):
    st.markdown(f"#### {t('quick_actions')}")
    for col, k in zip(st.columns(len(keys)), keys):
        col.button(nav_label(k), key=f"qa_{k}", on_click=go, args=(k,), width="stretch")


def pick_course(courses, key):
    if not courses:
        st.info(t("no_courses"))
        return None
    by_id = {c["course_id"]: c for c in courses}
    cid = st.selectbox(
        t("course"), list(by_id), key=key,
        format_func=lambda i: f"{by_id[i]['course_name']} — {by_id[i]['academic_year']}",
    )
    return by_id[cid]


def tr_cols(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns={c: t(f"col_{c}") for c in df.columns})


def show_alerts(year):
    """Banners: automatic quiz alert (every 4th lecture) + quizzes/projects due within 3 days."""
    for x in db.active_alerts(year):
        st.markdown(f'<div class="alert-banner"><b>{html.escape(t("alert_prefix"))}:</b> {html.escape(x["course_name"])} — '
                    f'{html.escape(t("quiz_alert"))}</div>', unsafe_allow_html=True)
    for x in db.due_soon(year, 3):
        st.markdown(f'<div class="alert-banner soft"><b>{html.escape(t("due_prefix"))}:</b> {html.escape(x["course_name"])} — '
                    f'{html.escape(t("asg_" + x["kind"]))}: {html.escape(x["title"])} · {html.escape(x["due_at"])}</div>',
                    unsafe_allow_html=True)


def student_card(user):
    """Name / code / year / study type — exactly the data attendance sends."""
    with st.container(border=True):
        st.markdown(f"**{user['full_name']}**")
        c1, c2, c3 = st.columns(3)
        c1.caption(t("student_code")); c1.write(user["student_code"])
        c2.caption(t("academic_year")); c2.write(user["academic_year"])
        c3.caption(t("study_type")); c3.write(t(f"study_{user['study_type']}"))


# ---------------------------------------------------------------- navigation
def set_lang(lang):
    st.session_state.lang = lang


def do_logout():
    for k in list(st.session_state.keys()):
        if k in ("user", "page", "onboard", "nav_top", "nav_side", "just_marked", "staff_import_result", "last_seen", "_admin_fid", "admin_fid_pick") \
                or k.startswith(("gps_req_", "fp_")):
            st.session_state.pop(k, None)


def _nav_changed(key):
    v = st.session_state.get(key)
    if v:                       # pills can be de-selected -> ignore None
        st.session_state.page = v


def lang_buttons(suffix):
    c1, c2 = st.columns(2)
    for col, code, label in ((c1, "ar", "العربية"), (c2, "en", "English")):
        col.button(label, key=f"lang_{code}_{suffix}", on_click=set_lang, args=(code,), width="stretch",
                   type="primary" if st.session_state.lang == code else "secondary")


def brand_html(user):
    """App name, plus the faculty's name for everybody who belongs to a faculty."""
    fac = user_faculty(user) if user.get("faculty_id") else ""
    sub = f'<small style="font-weight:400;opacity:.75"> — {html.escape(fac)}</small>' if fac else ""
    return f'<span class="brand">{html.escape(t("app_name"))}{sub}</span>'


def top_bar(user, pages):
    """Computer menu: brand | pills | account popover."""
    with st.container(key="topbar"):
        c1, c2, c3 = st.columns([3, 8, 2], vertical_alignment="center")
        c1.markdown(brand_html(user), unsafe_allow_html=True)
        with c2:
            st.pills("menu", list(pages), format_func=nav_label, key="nav_top",
                     selection_mode="single", on_change=_nav_changed, args=("nav_top",),
                     label_visibility="collapsed")
        with c3.popover(user["full_name"].split()[0], width="stretch"):
            st.caption(f"{t('role_' + user['role'])} • {user['email']}")
            st.markdown(f"**{t('language')}**")
            lang_buttons("top")
            st.button(t("logout"), key="logout_top", on_click=do_logout, width="stretch")


def side_bar(user, pages):
    """Phone / tablet menu: a side drawer that opens and closes with the toggle button."""
    with st.sidebar:
        st.markdown(brand_html(user), unsafe_allow_html=True)
        st.markdown(f'<div class="sb-user"><b>{html.escape(user["full_name"])}</b>'
                    f'<span>{html.escape(t("role_" + user["role"]))}</span></div>', unsafe_allow_html=True)
        st.radio("menu", list(pages), format_func=nav_label, key="nav_side",
                 on_change=_nav_changed, args=("nav_side",), label_visibility="collapsed")
        st.divider()
        st.markdown(f"**{t('language')}**")
        lang_buttons("side")
        st.button(t("logout"), key="logout_side", on_click=do_logout, width="stretch")


# ============================================================================
# Authentication & onboarding
# ============================================================================
def page_auth():
    _, c2 = st.columns([3, 2])
    with c2:
        lang_buttons("auth")
    uni = university_name()
    hero(t("app_name_full"), t("welcome_to", name=uni) if uni else t("welcome_title"))
    st.caption(t("auth_sub"))
    with st.container(border=True):
        tab_login, tab_first, tab_forgot = st.tabs([t("login"), t("first_time"), t("forgot_password")])
        with tab_login:
            with st.form("login_form", border=False):
                ident = st.text_input(t("login_id"), placeholder=t("login_id_ph"), help=t("login_id_hint"))
                pw = st.text_input(t("password"), type="password")
                go_ = st.form_submit_button(t("login"), type="primary", width="stretch")
            if go_:
                ip = client_ip()
                if db.login_locked(ident, ip):              # cheap check first: no bcrypt while locked
                    st.error(t("login_locked", n=config.LOGIN_LOCK_MIN))
                else:
                    user = db.authenticate(ident, pw, ip)
                    if user and user["role"] != "admin" and not user["faculty_active"]:
                        st.error(t("faculty_inactive"))
                    elif user:
                        st.session_state.user = user
                        st.session_state.last_seen = config.now()
                        st.rerun()
                    else:
                        st.error(t("login_locked", n=config.LOGIN_LOCK_MIN) if db.login_locked(ident, ip)
                                 else t("bad_credentials"))
        if not db.has_admin():
            st.info(t("setup_needed"))
        with tab_first:
            onboarding_flow()
        with tab_forgot:
            forgot_flow()


def _clear_forgot():
    for k in ("fp_step", "fp_user_id", "fp_hint"):
        st.session_state.pop(k, None)


def forgot_flow():
    """Forgot password: type the recovery e-mail -> a 6-digit code is e-mailed -> type code + new password."""
    if st.session_state.get("fp_step", 1) == 1:
        st.caption(t("fp_hint"))
        with st.form("fp_request", border=False):
            email = st.text_input(t("fp_email"), placeholder="name@gmail.com")
            go_ = st.form_submit_button(t("fp_send"), type="primary", width="stretch")
        if go_:
            email = email.strip().lower()
            if not _EMAIL_RE.match(email):
                st.error(t("fp_bad_email"))
                return
            if not mailer.is_configured():                  # a global fact, says nothing about the address
                st.error(t("fp_not_configured"))
                return
            user = db.find_user_for_reset(email)
            if user:
                status, code = db.create_reset_code(user["id"])
                if status == "ok":
                    mailer.send_reset_code(email, code, db.RESET_TTL_MIN, org_name())   # failures are logged
            # same answer whether or not the address exists (no account enumeration)
            st.session_state.fp_step, st.session_state.fp_user_id = 2, (user or {}).get("id", 0)
            st.rerun()
        return

    st.info(t("fp_sent_generic"))
    with st.form("fp_complete", border=False):
        code = st.text_input(t("fp_code"), max_chars=6)
        p1 = st.text_input(t("new_password"), type="password")
        p2 = st.text_input(t("confirm_password"), type="password")
        go_ = st.form_submit_button(t("fp_reset"), type="primary", width="stretch")
    if go_:
        err = pw_error(p1, p2)
        if err:
            st.error(err)
        else:
            uid = st.session_state.get("fp_user_id", 0)
            res = db.complete_reset(uid, code, p1) if uid else "bad_code"
            if res == "ok":
                _clear_forgot()
                st.success(t("fp_ok"))
            else:
                st.error(t(f"fp_{res}"))
    if st.button(t("cancel"), key="fp_cancel"):
        _clear_forgot()
        st.rerun()


def onboarding_flow():
    """First time: (1) student code + university e-mail  ->  (2) e-mailed code + password.
    Every failure looks the same from the outside, so the form cannot be used to find out who is a student."""
    ob = st.session_state.onboard
    ip = client_ip()
    if ob is None:
        with st.form("verify_form", border=False):
            code = st.text_input(t("student_code"))
            email = st.text_input(t("univ_email"), placeholder="name@university.edu")
            go_ = st.form_submit_button(t("verify"), type="primary", width="stretch")
        if go_:
            code, email = db.clean_digits(code).strip(), email.strip().lower()
            if not code or not re.match(config.EMAIL_REGEX, email, re.IGNORECASE):
                st.error(t("onb_invalid"))
                return
            if db.activation_locked(code, ip):
                st.error(t("login_locked", n=config.LOGIN_LOCK_MIN))
                return
            if config.REQUIRE_EMAIL_VERIFY:
                if not mailer.is_configured():
                    st.error(t("fp_not_configured"))
                    return
                status, plain_code, to = db.start_activation(code, email)
                if status == "ok":
                    mailer.send_activation_code(to, plain_code, db.RESET_TTL_MIN, org_name())
                else:
                    db.activation_failed(code, ip)
                st.session_state.onboard = {"student_code": code, "faculty_email": email, "verify": True}
                st.rerun()
            status, row = db.verify_roster(code, email)
            if status != "ok":
                db.activation_failed(code, ip)
                st.error(t("onb_invalid"))
                return
            st.session_state.onboard = {**row, "verify": False}
            st.rerun()
        return

    if ob["verify"]:
        st.info(t("onb_code_sent"))
    else:
        st.success(t("welcome_student", name=ob["full_name"]))
        st.markdown(
            f"**{t('faculty')}:** {plain(pick_name(ob['faculty_name_ar'], ob['faculty_name_en']))}  \n"
            f"**{t('academic_year')}:** {ob['academic_year']}  \n"
            f"**{t('department')}:** {plain(ob['department'])}  \n"
            f"**{t('study_type')}:** {t('study_' + ob['study_type'])}"
        )
    with st.form("password_form", border=False):
        vcode = st.text_input(t("onb_code"), max_chars=6) if ob["verify"] else ""
        r1 = st.text_input(t("recovery_email"), placeholder="name@gmail.com", help=t("recovery_hint"))
        r2 = st.text_input(t("recovery_email_confirm"), placeholder="name@gmail.com")
        p1 = st.text_input(t("new_password"), type="password")
        p2 = st.text_input(t("confirm_password"), type="password")
        go_ = st.form_submit_button(t("create_account"), type="primary", width="stretch")
    if go_:
        r1, r2 = r1.strip().lower(), r2.strip().lower()
        err = pw_error(p1, p2)
        if not _EMAIL_RE.match(r1):
            st.error(t("fp_bad_email"))
        elif r1 != r2:
            st.error(t("recovery_mismatch"))
        elif err:
            st.error(err)
        elif db.activation_locked(ob["student_code"], ip):
            st.error(t("login_locked", n=config.LOGIN_LOCK_MIN))
        else:
            status, user = db.activate_student(ob["student_code"], ob["faculty_email"], p1, r1, vcode)
            if status == "ok" and user:
                st.session_state.onboard = None
                st.session_state.user = user
                st.session_state.last_seen = config.now()
                st.rerun()
            if status in ("invalid", "bad_code"):
                db.activation_failed(ob["student_code"], ip)
            st.error(t("onb_invalid") if status == "invalid" else t("fp_bad_email") if status == "invalid_email"
                     else t("pw_weak") if status == "weak_password" else t(f"onb_{status}"))
    if st.button(t("cancel")):
        st.session_state.onboard = None
        st.rerun()


# ============================================================================
# STUDENT  (indigo, narrow, mobile-first)
# ============================================================================
def page_home_student(user):
    hero(t("greeting", name=user["full_name"].split()[0]), t("home_student_sub"),
         [user_faculty(user), user["academic_year"], user["department"], t(f"study_{user['study_type']}")])
    rows = db.student_attendance_summary(user["student_code"], user["academic_year"])
    total = sum(r["total"] for r in rows)
    present = sum(r["present"] for r in rows)
    pct = round(100 * present / total, 1) if total else None
    open_tasks = sum(1 for x in db.list_tasks(user["student_code"]) if not x["is_completed"])
    kpis([("how_to_reg", t("kpi_attendance"), f"{pct}%" if pct is not None else "—"),
          ("checklist", t("kpi_open_tasks"), open_tasks),
          ("menu_book", t("kpi_courses"), len(rows))])
    if pct is not None and pct < config.ATTENDANCE_THRESHOLD:
        st.warning(t("below_threshold", n=config.ATTENDANCE_THRESHOLD))
    quick_actions(["attendance", "planner", "chat"])


def page_profile(user):
    hero(user["full_name"], user["email"],
         [user["academic_year"], user["department"], t(f"study_{user['study_type']}"),
          f"{t('section_no')} {user['section_no']}"])
    student_card(user)
    for kind in ("Lecture", "Section"):
        st.markdown(f"#### {t('attendance_per_course')} — {t('att_kind_' + kind)}")
        rows = db.student_attendance_summary(user["student_code"], user["academic_year"], kind)
        if not rows:
            st.info(t("no_courses"))
        for r in rows:
            with st.container(border=True):
                st.write(f"**{r['course_name']}**")
                if r["total"] == 0:
                    st.caption(t("no_lectures_yet"))
                    continue
                pct = round(100 * r["present"] / r["total"], 1)
                st.progress(min(pct / 100, 1.0))
                st.caption(f"{r['present']}/{r['total']} — {pct}%")
                if pct < config.ATTENDANCE_THRESHOLD:
                    st.warning(t("below_threshold", n=config.ATTENDANCE_THRESHOLD))
    with st.expander(t("menu_account")):
        account_settings(user)


def read_gps(clicked, key):
    """Real browser GPS (Geolocation API through streamlit-js-eval; phones need HTTPS).
    Returns (lat, lon, accuracy_m) when the browser answers, else None. One flag + one component per `key`."""
    if not HAS_GPS:
        st.error(t("gps_missing"))
        return None
    flag = f"gps_req_{key}"
    if clicked:
        st.session_state[flag] = True
    if st.session_state.get(flag):
        loc = get_geolocation(component_key=f"geo_{key}")      # None until the browser answers
        if loc and "coords" in loc:
            st.session_state[flag] = False
            c = loc["coords"]
            return (c["latitude"], c["longitude"], c.get("accuracy"))
        if loc and "error" in loc:
            st.session_state[flag] = False
            st.error(t("gps_denied"))
        else:
            st.info(t("gps_waiting"))
    return None


def acquire_position(kind, locs):
    """ONE TAP: the button reads the REAL GPS of the phone. Nothing to type or choose about the location."""
    clicked = st.button(t("att_one_tap"), type="primary", width="stretch", key=f"att_btn_{kind}")
    return read_gps(clicked, kind)


@st.fragment
def attendance_flow(user, kind):
    """Student side of one attendance kind: attendance code (told by the doctor) + GPS check, then sent."""
    db.set_scope(user["faculty_id"])          # fragments re-run without main(): set the faculty again
    course = pick_course(db.list_courses(year=user["academic_year"]), f"att_course_{kind}")
    if not course:
        return
    group = str(user["section_no"]) if kind == "Section" else ""
    sess = db.latest_session(course["course_id"], kind, group)
    if not sess:
        st.info(t("att_no_lecture"))
        return
    label = t("session_Lecture") if kind == "Lecture" else f"{t('session_Section')} {group}"
    with st.container(border=True):
        st.markdown(f"**{label} {sess['lecture_number']}** — {plain(sess['topic_covered'])}")

    rec = db.attendance_record(user["student_code"], sess["lecture_id"])
    if rec and rec["status"] == "Present":              # receipt of what was sent
        st.session_state.pop("just_marked", None)
        st.success(t("att_receipt"))
        student_card(user)
        st.caption(f"{t('att_time')}: {rec['timestamp']}")
        return
    if rec and rec["note"] == "left_early":
        st.warning(t("att_cancelled"))
        return
    if sess["attendance_closed"]:
        st.warning(t("att_closed"))
        return

    locs = db.venue_points(course["course_id"], kind, group, user["faculty_id"])
    if not locs:
        st.error(t("att_no_point"))
        return
    st.caption(t("att_will_send"))
    student_card(user)
    code = st.text_input(t("att_code"), max_chars=10, key=f"att_code_{kind}", placeholder=t("att_code_placeholder"))
    pos = acquire_position(kind, locs)
    st.caption(t("att_privacy"))
    if pos is None:
        return
    lat, lon, accuracy = pos
    if not code.strip():
        st.error(t("att_code_required"))
        return
    # the SERVER checks the position (accuracy + distance), the code and every other rule; nothing is stored
    res = db.mark_present(user["student_code"], sess["lecture_id"], code, lat, lon, accuracy)
    if res == "ok":
        st.session_state.just_marked = True
        st.rerun()
    st.error(t("att_weak_gps", n=int(accuracy or 0)) if res == "weak_gps" else t(f"att_{res}"))


def page_attendance(user):
    hero(t("menu_attendance"), t("home_student_sub"))
    kind = st.radio(t("attendance_kind"), ["Lecture", "Section"], horizontal=True, key="att_kind",
                    format_func=lambda k: t(f"att_kind_{k}"))
    attendance_flow(user, kind)


def page_syllabus(user):
    hero(t("menu_syllabus"), user["academic_year"])
    courses = db.list_courses(year=user["academic_year"])
    if not courses:
        st.info(t("no_courses"))
    for c in courses:
        with st.container(border=True):
            st.markdown(f"### {c['course_name']}")
            st.caption(f"{t('doctor')}: {c['doctor_name']}")
            lec = db.latest_lecture(c["course_id"])
            if not lec:
                st.caption(t("no_lectures_yet"))
                continue
            st.markdown(f"**{t('current_lecture')}:** {lec['lecture_number']} — {plain(lec['topic_covered'])}")
            with st.expander(t("all_lectures")):
                df = pd.DataFrame(db.list_lectures(c["course_id"]))[
                    ["lecture_number", "topic_covered", "date_created"]]
                st.dataframe(tr_cols(df), width="stretch", hide_index=True)


def page_planner(user):
    """Strictly private: every call passes the SESSION's student_code to the db layer."""
    hero(t("menu_planner"), t("planner_private"))
    with st.container(border=True):
        with st.form("task_form", clear_on_submit=True, border=False):
            title = st.text_input(t("new_task"))
            add = st.form_submit_button(t("add"), type="primary", width="stretch")
        if add and title.strip():
            db.add_task(user["student_code"], title)
            st.rerun()
    for tk in db.list_tasks(user["student_code"]):
        with st.container(border=True):
            c1, c2 = st.columns([8, 2], vertical_alignment="center")
            done = c1.checkbox(tk["task_title"], value=bool(tk["is_completed"]), key=f"task_{tk['task_id']}")
            if done != bool(tk["is_completed"]):
                db.set_task_done(user["student_code"], tk["task_id"], done)
                st.rerun()
            if c2.button(t("delete"), key=f"del_{tk['task_id']}"):
                db.delete_task(user["student_code"], tk["task_id"])
                st.rerun()


@functools.lru_cache(maxsize=4)
def _avatar(color):
    """A plain coloured circle as the chat avatar (no emoji, no stock icons)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((4, 4, 92, 92), fill=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_chat(msgs, me, my_name="", other_name=""):
    """Chat bubbles. Your messages get an indigo circle, the other person's a teal one."""
    for m in msgs:
        is_me = m["sender_code"] == me
        with st.chat_message("user" if is_me else "assistant", avatar=_avatar("#4f46e5" if is_me else "#0d9488")):
            st.markdown(plain(m["message_text"]))
            st.caption(m["timestamp"])


def page_chat_student(user):
    hero(t("menu_chat"), t("home_doctor_sub"))
    course = pick_course(db.list_courses(year=user["academic_year"]), "chat_course")
    if not course:
        return
    if not course["doctor_id"]:
        st.info(t("no_doctor_assigned"))
        return
    dr = db.doctor_code(course["doctor_id"])
    is_open = db.is_chat_open(course["course_id"])
    st.button(t("refresh"), key="chat_refresh")
    render_chat(db.get_conversation(course["course_id"], user["code"], dr), user["code"], user["full_name"], course["doctor_name"])
    if not is_open:
        st.info(t("chat_closed"))
    msg = st.chat_input(t("type_message"), disabled=not is_open, max_chars=1000)
    if msg and db.send_message(user["code"], dr, course["course_id"], msg):
        st.rerun()


def assignment_card(r):
    with st.container(border=True):
        st.markdown(f"**{t('asg_' + r['kind'])}: {r['title']}**")
        st.caption(f"{r['course_name']} — {r['doctor_name']}")
        st.write(f"{t('due')}: {r['due_at']}")
        if r["description"]:
            st.markdown(plain(r["description"]))
        if r["link"].startswith(("http://", "https://")):
            st.link_button(t("open_link"), r["link"])


def page_assignments_student(user):
    hero(t("menu_assignments"), t("assignments_sub"))
    rows = db.list_assignments(year=user["academic_year"])
    if not rows:
        st.info(t("no_assignments"))
        return
    now = config.now().strftime("%Y-%m-%d %H:%M")
    st.markdown(f"#### {t('upcoming')}")
    upcoming = [r for r in rows if r["due_at"] >= now]
    if not upcoming:
        st.caption(t("no_assignments"))
    for r in upcoming:
        assignment_card(r)
    past = [r for r in rows if r["due_at"] < now][::-1]
    if past:
        with st.expander(f"{t('past')} ({len(past)})"):
            for r in past:
                assignment_card(r)


def page_complaints_student(user):
    hero(t("menu_complaints"), t("complaints_sub"))
    with st.container(border=True):
        with st.form("complaint_form", clear_on_submit=True, border=False):
            kind = st.radio(t("cmp_kind"), ["Complaint", "Suggestion"], horizontal=True,
                            format_func=lambda k: t(f"cmp_{k}"))
            ctype = st.selectbox(t("complaint_type"), config.COMPLAINT_TYPES, format_func=lambda x: t(f"type_{x}"))
            content = st.text_area(t("complaint_content"), max_chars=2000)
            anon = st.checkbox(t("submit_anonymously"))
            go_ = st.form_submit_button(t("submit"), type="primary", width="stretch")
        if go_:
            if len(content.strip()) < 10:
                st.error(t("complaint_too_short"))
            else:
                db.add_complaint(None if anon else user["student_code"], ctype, content, kind)
                st.success(t("complaint_sent"))
    mine = db.my_complaints(user["student_code"])
    if mine:
        st.markdown(f"#### {t('my_submissions')}")
    for r in mine:
        with st.container(border=True):
            st.markdown(f"**{t('cmp_' + r['kind'])}** · "
                        f"{t('type_' + r['type'])} · {t('status_' + r['status'])}")
            st.markdown(plain(r["content"]))
            if r["response"]:
                st.info(f"{t('admin_response')}: {plain(r['response'])}")
            st.caption(r["timestamp"])


# ============================================================================
# DOCTOR / TA  (teal, wider, dashboard-style)
# ============================================================================
def page_home_doctor(user):
    s = db.doctor_stats(user["id"])
    hero(t("greeting", name=user["full_name"]), t("home_doctor_sub"), [t("role_doctor"), user_faculty(user)])
    kpis([("school", t("kpi_courses"), s["courses"]), ("groups", t("kpi_students"), s["students"]),
          ("menu_book", t("kpi_lectures"), s["lectures"]), ("how_to_reg", t("kpi_today_present"), s["today_present"])])
    quick_actions(["syllabus_tracker", "attendance_control", "section_attendance", "assignments"])


def page_syllabus_doctor(user):
    hero(t("menu_syllabus_tracker"), t("quiz_alert"))
    course = pick_course(db.list_courses(doctor_id=user["id"]), "doc_course")
    if not course:
        return
    latest = db.latest_lecture(course["course_id"])
    next_no = latest["lecture_number"] + 1 if latest else 1
    with st.container(border=True):
        with st.form("lecture_form", border=False):
            c1, c2 = st.columns([1, 3])
            num = c1.number_input(t("lecture_number"), min_value=1, value=next_no, step=1)
            topic = c2.text_input(t("topic_covered"))
            go_ = st.form_submit_button(t("save"), type="primary", width="stretch")
    if go_:
        if not topic.strip():
            st.error(t("topic_required"))
        else:
            _, quiz = db.upsert_lecture(course["course_id"], int(num), topic, user["id"])
            st.success(t("lecture_saved"))
            if quiz:   # lecture 4, 8, 12 ... -> automatic system-wide alert
                st.warning(t("quiz_posted"))
    rows = db.list_lectures(course["course_id"])
    if rows:
        df = pd.DataFrame(rows)[["lecture_number", "topic_covered", "date_created", "present_count"]]
        st.dataframe(tr_cols(df), width="stretch", hide_index=True)


@st.fragment(run_every="10s")
def live_attendees(lecture_id, user):
    """Refreshes itself every 10 s: doctor sees students appear as they tap the button."""
    db.set_scope(user["faculty_id"])          # a fragment re-run does not go through main()
    rows = db.lecture_attendees(lecture_id, user["id"])
    st.markdown(f"#### {t('live_attendance')}")
    c = st.columns(3)
    c[0].metric(t("present_total"), len(rows))
    c[1].metric(t("study_Regular"), sum(1 for r in rows if r["study_type"] == "Regular"))
    c[2].metric(t("study_Affiliated"), sum(1 for r in rows if r["study_type"] == "Affiliated"))
    if not rows:
        st.info(t("attendees_none"))
        return
    df = pd.DataFrame(rows)
    df["study_type"] = df["study_type"].map(lambda s: t(f"study_{s}"))
    st.dataframe(tr_cols(df[["full_name", "student_code", "academic_year", "study_type", "timestamp"]]),
                 width="stretch", hide_index=True)


def code_panel(lecture_id, doctor_id):
    """The doctor CHOOSES the attendance code (or generates one) and says it in class."""
    info = db.get_attendance_code(lecture_id, doctor_id)
    with st.container(border=True):
        st.markdown(f"#### {t('att_code_title')}")
        st.caption(t("att_code_hint_doctor"))
        if info["active"]:
            st.success(f"{t('att_code_current')}: **{info['code']}** — {t('att_code_until')} {info['expires_at'][11:16]}")
        else:
            st.warning(t("att_code_none"))
        with st.form(f"code_form_{lecture_id}", border=False):
            c1, c2 = st.columns([2, 1])
            new = c1.text_input(t("att_code_new"), max_chars=10)
            minutes = c2.selectbox(t("att_code_minutes"), [5, 10, 15, 30, 60, 120], index=2)
            b1, b2 = st.columns(2)
            set_ = b1.form_submit_button(t("att_code_set"), type="primary", width="stretch")
            rnd = b2.form_submit_button(t("att_code_random"), width="stretch")
        if set_ or rnd:
            code = f"{secrets.randbelow(10 ** 6):06d}" if rnd else new
            res = db.set_attendance_code(lecture_id, code, minutes, doctor_id)
            if res == "ok":
                st.rerun()
            st.error(t("att_closed") if res == "closed" else t("att_code_invalid"))
        if info["active"] and st.button(t("att_code_stop"), key=f"stop_{lecture_id}"):
            db.clear_attendance_code(lecture_id, doctor_id)
            st.rerun()


def left_early_panel(user, lecture_id):
    """A student leaves in the middle: the doctor types the student's code and the attendance is cancelled."""
    with st.container(border=True):
        st.markdown(f"#### {t('left_early_title')}")
        st.caption(t("left_early_hint"))
        code = st.text_input(t("student_code"), key=f"le_code_{lecture_id}").strip()
        stu = db.get_student(code) if code else None
        if code and not stu:
            st.error(t("student_not_found"))
        if stu:
            st.info(f"{plain(stu['full_name'])} — {stu['academic_year']} — {t('study_' + stu['study_type'])}")
        if st.button(t("left_early_btn"), key=f"le_btn_{lecture_id}", disabled=stu is None):
            res = db.cancel_attendance(lecture_id, code, user["email"], user["id"])
            (st.success if res == "ok" else st.error)(t("left_early_done") if res == "ok" else t(f"left_early_{res}"))


def attendance_panel(user, kind):
    """Doctor's control room for ONE kind: pick session -> set code -> watch live list -> cancel / close."""
    course = pick_course(db.list_courses(doctor_id=user["id"]), f"ctl_course_{kind}")
    if not course:
        return
    cid, group = course["course_id"], ""
    if kind == "Section":
        group = str(st.selectbox(t("section_no"), list(range(1, db.max_section(course["academic_year"]) + 1)),
                                 key=f"ctl_group_{cid}"))
        latest = db.latest_session(cid, "Section", group)
        with st.expander(t("new_section_session"), expanded=latest is None):
            with st.form(f"sec_form_{cid}_{group}", border=False):
                c1, c2 = st.columns([1, 3])
                num = c1.number_input(t("lecture_number"), min_value=1, step=1,
                                      value=(latest["lecture_number"] + 1) if latest else 1)
                topic = c2.text_input(t("topic_covered"))
                go_ = st.form_submit_button(t("save"), type="primary", width="stretch")
            if go_ and topic.strip():
                db.upsert_session(cid, "Section", group, int(num), topic, user["id"])
                st.rerun()
    rows = db.list_sessions(cid, kind, group)
    if not rows:
        st.info(t("no_lectures_yet"))
        return
    by_id = {r["lecture_id"]: r for r in rows}      # newest first
    lid = st.selectbox(t("pick_lecture"), list(by_id), key=f"ctl_sess_{kind}_{cid}_{group}",
                       format_func=lambda i: f"{by_id[i]['lecture_number']} — {by_id[i]['topic_covered']}")
    closed = bool(by_id[lid]["attendance_closed"])
    if not closed:
        code_panel(lid, user["id"])
    with st.container(border=True):
        live_attendees(lid, user)
    left_early_panel(user, lid)
    if closed:
        st.caption(t("att_closed"))
        return
    with st.container(border=True):
        st.warning(t("close_attendance_warning"))
        sure = st.checkbox(t("confirm"), key=f"sure_{lid}")
        if st.button(t("close_attendance"), disabled=not sure, key=f"close_{lid}", type="primary"):
            n = db.close_lecture(lid, user["id"])
            if n >= 0:
                st.success(t("attendance_closed_msg", n=n))
                st.rerun()


def page_attendance_doctor(user):
    hero(t("menu_attendance_control"), t("live_attendance"))
    attendance_panel(user, "Lecture")


def page_section_attendance(user):
    hero(t("menu_section_attendance"), t("live_attendance"))
    attendance_panel(user, "Section")


def page_assignments_doctor(user):
    hero(t("menu_assignments"), t("assignments_doctor_sub"))
    course = pick_course(db.list_courses(doctor_id=user["id"]), "asg_course")
    if course:
        with st.container(border=True):
            with st.form("asg_form", clear_on_submit=True, border=False):
                kind = st.radio(t("asg_kind"), ["Quiz", "Project"], horizontal=True, format_func=lambda k: t(f"asg_{k}"))
                title = st.text_input(t("asg_title"))
                desc = st.text_area(t("asg_desc"), max_chars=2000)
                link = st.text_input(t("asg_link"), placeholder="https://")
                c1, c2 = st.columns(2)
                d = c1.date_input(t("asg_due_date"), value=date.today() + timedelta(days=7))
                tm = c2.time_input(t("asg_due_time"), value=dtime(12, 0), step=900)
                go_ = st.form_submit_button(t("asg_publish"), type="primary", width="stretch")
            if go_:
                if not title.strip():
                    st.error(t("asg_title_required"))
                elif link.strip() and not link.strip().startswith(("http://", "https://")):
                    st.error(t("asg_link_invalid"))
                else:
                    db.add_assignment(course["course_id"], user["id"], kind, title, desc, link,
                                      f"{d.strftime('%Y-%m-%d')} {tm.strftime('%H:%M')}")
                    st.success(t("asg_published"))
    own = db.list_assignments(doctor_id=user["id"])
    for r in own:
        assignment_card(r)
        if st.button(t("delete"), key=f"del_asg_{r['assignment_id']}"):
            db.delete_assignment(r["assignment_id"], user["id"])
            st.rerun()


def page_chat_doctor(user):
    hero(t("menu_chat"), t("chat_open_toggle"))
    course = pick_course(db.list_courses(doctor_id=user["id"]), "dchat_course")
    if not course:
        return
    cid = course["course_id"]
    is_open = st.toggle(t("chat_open_toggle"), value=db.is_chat_open(cid), key=f"open_{cid}")
    if is_open != db.is_chat_open(cid):
        db.set_chat_open(cid, is_open, user["id"])
        st.rerun()
    partners = db.chat_partners(cid, user["code"])
    if not partners:
        st.info(t("no_messages"))
        return
    by_code = {p["code"]: p["name"] for p in partners}
    stu = st.selectbox(t("student"), list(by_code), format_func=lambda c: f"{by_code[c]} ({c})")
    st.button(t("refresh"), key="dchat_refresh")
    render_chat(db.get_conversation(cid, user["code"], stu), user["code"], user["full_name"], by_code[stu])
    reply = st.chat_input(t("type_message"), max_chars=1000)
    if reply and db.send_message(user["code"], stu, cid, reply, from_student=False):
        st.rerun()


# ============================================================================
# ADMIN  (amber/slate, full width, data-heavy)
# ============================================================================
def _open_faculty(fid):
    st.session_state["_admin_fid"] = fid
    st.session_state.page = "faculty_dashboard"


def creatable_roles(user):
    """Accounts the signed-in admin may create inside a faculty. Faculty admins are created ONLY by the
    super admin; a faculty admin can add doctors and deans."""
    return ["doctor", "dean", "faculty_admin"] if user["role"] == "admin" else ["doctor", "dean"]


def flash(msg):
    """Show a success message after the next rerun."""
    st.session_state["_flash"] = msg


def page_home_admin(user):
    """System dashboard: the university and one card per faculty (the admin opens a faculty's own dashboard)."""
    ov = db.faculty_overview()
    uni = university_name()
    hero(t("greeting", name=user["full_name"]), t("home_admin_sub"), [t("role_admin")] + ([uni] if uni else []))
    if not ov:
        st.info(t("no_faculties_yet"))
        st.button(nav_label("faculties"), key="home_add_fac", on_click=go, args=("faculties",), type="primary")
        return
    tot = lambda k: sum(r[k] for r in ov)
    kpis([("domain", t("kpi_faculties"), len(ov)), ("groups", t("kpi_roster"), tot("students")),
          ("verified_user", t("kpi_activated"), tot("activated")), ("school", t("kpi_doctors"), tot("doctors")),
          ("inbox", t("kpi_open_complaints"), tot("open_complaints"))])
    st.markdown(f"#### {t('faculties_list')}")
    for i in range(0, len(ov), 2):
        for col, f in zip(st.columns(2), ov[i:i + 2]):
            with col, st.container(border=True):
                st.markdown(f"**{fac_name(f)}**")
                st.caption(f"{t('faculty_id')}: {f['faculty_id']}" + ("" if f["is_active"] else f" — {t('faculty_disabled')}"))
                st.write(f"{t('kpi_roster')}: {f['students']} · {t('kpi_activated')}: {f['activated']} · "
                         f"{t('kpi_doctors')}: {f['doctors']} · {t('kpi_courses')}: {f['courses']} · "
                         f"{t('kpi_open_complaints')}: {f['open_complaints']}")
                st.button(t("open_dashboard"), key=f"open_fac_{f['faculty_id']}", on_click=_open_faculty,
                          args=(f["faculty_id"],), type="primary", width="stretch")
    quick_actions(["faculties"])


def faculty_form(f=None):
    """Add a faculty (f=None) or edit one. The faculty's ID is created by the system and never changes."""
    fid = f["faculty_id"] if f else "new"
    with st.form(f"fac_form_{fid}", border=False, clear_on_submit=f is None):
        c1, c2 = st.columns(2)
        name_ar = c1.text_input(t("faculty_name_ar"), value=(f or {}).get("name_ar", ""))
        name_en = c2.text_input(t("faculty_name_en"), value=(f or {}).get("name_en", ""))
        c3, c4 = st.columns(2)
        years = c3.number_input(t("faculty_years"), 1, config.MAX_YEARS, int((f or {}).get("years_count", config.DEFAULT_YEARS)))
        domain = c4.text_input(t("faculty_email_domain"), value=(f or {}).get("email_domain", ""),
                               placeholder="@faculty.university.edu", help=t("faculty_email_domain_hint"))
        st.caption(t("faculty_point_hint"))
        c5, c6, c7 = st.columns(3)
        lat = c5.text_input(t("latitude"), value="" if not f or f["latitude"] is None else f"{f['latitude']:.7f}")
        lon = c6.text_input(t("longitude"), value="" if not f or f["longitude"] is None else f"{f['longitude']:.7f}")
        radius = c7.number_input(t("radius_m"), 1, 500, int((f or {}).get("radius_m", 50)))
        notes = st.text_area(t("faculty_notes"), value=(f or {}).get("notes", ""), max_chars=500)
        active = st.checkbox(t("faculty_active"), value=bool(f["is_active"]) if f else True)
        go_ = st.form_submit_button(t("save"), type="primary", width="stretch")
    if not go_:
        return
    if not (name_ar.strip() or name_en.strip()):
        st.error(t("faculty_bad_name"))
        return
    la, lo = num_in(lat), num_in(lon)
    if (lat.strip() or lon.strip()) and (la is None or lo is None or not (-90 <= la <= 90 and -180 <= lo <= 180)):
        st.error(t("faculty_bad_point"))
        return
    if not (lat.strip() and lon.strip()):
        la = lo = None
    if f is None:
        status, new_id = db.add_faculty(name_ar, name_en, years, domain, la, lo, radius, notes, actor=st.session_state.user["email"])
        if status != "ok":
            st.error(t("faculty_exists"))
            return
        st.session_state["_admin_fid"] = new_id
        flash(t("faculty_added", id=new_id))
    else:
        res = db.update_faculty(f["faculty_id"], name_ar, name_en, years, domain, la, lo, radius, notes, active,
                                actor=st.session_state.user["email"])
        if res != "ok":
            st.error(t("faculty_exists") if res == "exists" else t("faculty_bad_name"))
            return
        flash(t("faculty_saved"))
    st.rerun()


def page_faculty_dashboard(user):
    """ONE faculty's dashboard (the faculty chosen in the selector above). Doctors / students / dean see the
    same app, limited to their own faculty."""
    f = db.get_faculty(db.scope())
    s = db.admin_stats()
    at_risk = db.at_risk_count(config.ATTENDANCE_THRESHOLD, 1)
    hero(fac_name(f), t("faculty_dashboard_sub"),
         [f"{t('faculty_id')} {f['faculty_id']}", f"{f['years_count']} {t('years_unit')}"]
         + ([] if f["is_active"] else [t("faculty_disabled")]))
    kpis([("groups", t("kpi_roster"), s["roster"]), ("verified_user", t("kpi_activated"), s["activated"]),
          ("school", t("kpi_doctors"), s["doctors"]), ("menu_book", t("kpi_courses"), s["courses"])])
    kpis([("meeting_room", t("kpi_halls"), s["halls"]), ("inbox", t("kpi_open_complaints"), s["open_complaints"]),
          ("insights", t("kpi_avg_rate"), f"{s['avg_rate']}%" if s["avg_rate"] is not None else "—"),
          ("warning", t("kpi_at_risk"), at_risk)])
    quick_actions(["roster", "staff", "schedule_admin", "analytics"])
    is_super = user["role"] == "admin"
    tab_charts, tab_info, *tab_more = st.tabs([t("tab_overview"), t("faculty_info")] + ([t("faculty_edit")] if is_super else []))
    with tab_charts:
        df = db.attendance_by_group("Lecture")
        if df.empty:
            st.info(t("no_data"))
        else:
            df["group"] = df["department"] + " — " + df["academic_year"]
            st.bar_chart(df.set_index("group")[["present", "absent"]])
            tdf = db.attendance_trend(None, "Lecture")
            if not tdf.empty:
                st.line_chart(tdf.set_index("lecture_date")["rate"])
    with tab_info:
        with st.container(border=True):
            rows = [(t("faculty_id"), f["faculty_id"]), (t("faculty_name_ar"), f["name_ar"]),
                    (t("faculty_name_en"), f["name_en"]), (t("faculty_years"), f["years_count"]),
                    (t("faculty_email_domain"), f["email_domain"] or t("any_email")),
                    (t("gps_point"), _fmt_point(f["latitude"], f["longitude"])), (t("radius_m"), f["radius_m"]),
                    (t("faculty_notes"), f["notes"] or "—")]
            st.dataframe(pd.DataFrame(rows, columns=[" ", "  "]), width="stretch", hide_index=True)
    if is_super:
        with tab_more[0]:
            faculty_form(f)


def page_faculties(user):
    """University name, the list of faculties (add / edit) and the system admins."""
    hero(t("menu_faculties"), t("faculties_sub"))
    tab_uni, tab_fac, tab_adm = st.tabs([t("tab_university"), t("tab_faculties"), t("tab_sysadmins")])

    with tab_uni:
        st.caption(t("university_hint"))
        u = db.get_university()
        with st.form("uni_form", border=False):
            c1, c2 = st.columns(2)
            n_ar = c1.text_input(t("uni_name_ar"), value=u["name_ar"], max_chars=200)
            n_en = c2.text_input(t("uni_name_en"), value=u["name_en"], max_chars=200)
            if st.form_submit_button(t("save"), type="primary", width="stretch"):
                db.set_university(n_ar, n_en)
                flash(t("uni_saved"))
                st.rerun()

    with tab_fac:
        facs = db.list_faculties()
        if facs:
            df = pd.DataFrame(facs)
            df["name"] = [fac_name(f) for f in facs]
            df["is_active"] = df["is_active"].map(lambda v: t("yes") if v else t("no"))
            df["email_domain"] = df["email_domain"].replace("", "—")
            st.dataframe(df[["faculty_id", "name", "years_count", "email_domain", "is_active"]].rename(columns={
                "faculty_id": t("faculty_id"), "name": t("faculty"), "years_count": t("faculty_years"),
                "email_domain": t("faculty_email_domain"), "is_active": t("faculty_active")}),
                width="stretch", hide_index=True)
        else:
            st.info(t("no_faculties_yet"))
        with st.container(border=True):
            st.markdown(f"#### {t('faculty_add')}")
            faculty_form(None)
        if facs:
            with st.container(border=True):
                st.markdown(f"#### {t('faculty_edit')}")
                by_id = {f["faculty_id"]: f for f in facs}
                pick = st.selectbox(t("faculty"), list(by_id), key="fac_edit_pick",
                                    format_func=lambda i: f"#{i} · {fac_name(by_id[i])}")
                faculty_form(by_id[pick])

    with tab_adm:
        st.caption(t("sysadmin_hint"))
        admins = db.list_system_admins()
        st.dataframe(tr_cols(pd.DataFrame(admins)[["staff_code", "full_name", "email", "created_at"]]),
                     width="stretch", hide_index=True)
        with st.container(border=True):
            st.markdown(f"#### {t('sysadmin_add')}")
            with st.form("sysadmin_form", clear_on_submit=True, border=False):
                name = st.text_input(t("full_name"))
                email = st.text_input(t("email"))
                pw = st.text_input(t("staff_initial_password"), type="password", help=t("staff_pw_hint"))
                go_ = st.form_submit_button(t("create_account"), type="primary", width="stretch")
            if go_:
                temp = pw or secrets.token_urlsafe(8)
                if not (name.strip() and _EMAIL_RE.match(email.strip())) or config.password_problem(temp):
                    st.error(t("staff_invalid"))
                elif db.create_staff(email, name, temp, "admin", must_change=True,
                                     actor=st.session_state.user["email"], actor_role="admin") == "ok":
                    st.success(t("staff_created"))
                    if not pw:
                        st.warning(t("reset_done"))
                        st.code(temp, language=None)
                else:
                    st.error(t("staff_exists"))


def page_complaints_admin(user):
    hero(t("menu_complaints_admin"), t("update_status"))
    c1, c2 = st.columns(2)
    who = c1.radio(t("filter"), ["all", "named", "anonymous"], horizontal=True, format_func=lambda k: t(f"filter_{k}"))
    kind = c2.radio(t("cmp_kind"), [None, "Complaint", "Suggestion"], horizontal=True,
                    format_func=lambda k: t("all") if k is None else t(f"cmp_{k}"))
    rows = db.list_complaints(who, kind)
    if not rows:
        st.info(t("no_data"))
        return
    view = pd.DataFrame([{
        "complaint_id": r["complaint_id"], "ckind": t(f"cmp_{r['kind']}"), "type": t(f"type_{r['type']}"),
        "content": r["content"],
        "student": f"{r['student_code']} — {r['full_name']}" if r["student_code"] else t("anonymous"),
        "status": t(f"status_{r['status']}"), "response": r["response"], "timestamp": r["timestamp"],
    } for r in rows])
    st.dataframe(tr_cols(view), width="stretch", hide_index=True)
    with st.container(border=True):
        st.markdown(f"#### {t('update_status')}")
        with st.form("status_form", border=False):
            c1, c2 = st.columns(2)
            cid = c1.selectbox(t("col_complaint_id"), [r["complaint_id"] for r in rows])
            new = c2.selectbox(t("col_status"), config.COMPLAINT_STATUSES, format_func=lambda x: t(f"status_{x}"))
            resp = st.text_area(t("admin_response"), max_chars=2000)
            if st.form_submit_button(t("save"), type="primary"):
                db.set_complaint_status(cid, new, resp, actor=st.session_state.user["email"])
                st.rerun()


def page_analytics(user):
    hero(t("menu_analytics"), t("tab_warning"))
    kind = st.radio(t("attendance_kind"), ["Lecture", "Section"], horizontal=True, key="an_kind",
                    format_func=lambda k: t(f"att_kind_{k}"))
    tab1, tab2, tab3 = st.tabs([t("tab_by_group"), t("tab_trend"), t("tab_warning")])
    with tab1:
        df = db.attendance_by_group(kind)
        if df.empty:
            st.info(t("no_data"))
        else:
            df["group"] = df["department"] + " — " + df["academic_year"]
            st.bar_chart(df.set_index("group")[["present", "absent"]])
            st.dataframe(tr_cols(df[["academic_year", "department", "present", "absent", "rate"]]),
                         width="stretch", hide_index=True)
    with tab2:
        year = st.selectbox(t("academic_year"), [None] + db.faculty_years(),
                            format_func=lambda y: t("all_years") if y is None else y)
        tdf = db.attendance_trend(year, kind)
        if tdf.empty:
            st.info(t("no_data"))
        else:
            st.line_chart(tdf.set_index("lecture_date")["rate"])
    with tab3:
        c1, c2 = st.columns(2)
        thr = c1.slider(t("threshold"), 10, 100, config.ATTENDANCE_THRESHOLD)
        minl = c2.number_input(t("min_lectures"), min_value=1, value=3, step=1)
        wdf = db.early_warning(thr, int(minl), kind)
        if wdf.empty:
            st.success(t("no_warnings"))
        else:
            st.error(t("warning_count", n=wdf["student_code"].nunique()))
            wdf["study_type"] = wdf["study_type"].map(lambda v: t(f"study_{v}"))
            wdf["kind"] = wdf["kind"].map(lambda v: t(f"att_kind_{v}"))
            shown = tr_cols(wdf)
            st.dataframe(shown, width="stretch", hide_index=True)
            st.download_button(t("download_csv"), csv_safe(shown).to_csv(index=False).encode("utf-8-sig"),
                               "restriction_list.csv", "text/csv")


ROSTER_COLS = ["student_code", "full_name", "faculty_email", "academic_year", "department", "study_type"]
STUDY_MAP = {"regular": "Regular", "منتظم": "Regular", "affiliated": "Affiliated", "منتسب": "Affiliated"}


def roster_year_tabs():
    """Everyone who is in the system (uploaded roster), split into 1st/2nd/3rd/4th year."""
    st.markdown(f"#### {t('students_in_system')}")
    ys = db.faculty_years()
    data = {y: db.roster_by_year(y) for y in ys}
    for y, tab in zip(ys, st.tabs([f"{y} ({len(data[y])})" for y in ys])):
        with tab:
            if not data[y]:
                st.info(t("no_data"))
                continue
            df = pd.DataFrame(data[y])
            df["study_type"] = df["study_type"].map(lambda v: t(f"study_{v}"))
            df["registered"] = df["registered"].map(lambda v: t("yes") if v else t("no"))
            st.dataframe(tr_cols(df[["student_code", "full_name", "faculty_email", "department",
                                     "study_type", "section_no", "registered"]]), width="stretch", hide_index=True)


UPLOAD_TYPES = ["xlsx", "csv", "txt", "tsv", "json"]


def read_upload(label, key):
    """File uploader + sheet/table chooser. Returns a DataFrame of strings, or None."""
    up = st.file_uploader(label, type=UPLOAD_TYPES, key=key)
    if not up:
        return None
    data = up.getvalue()
    try:
        sources = importer.list_sources(up.name, data)
        source = st.selectbox(t("pick_sheet"), sources, key=f"{key}_src") if len(sources) > 1 else sources[0]
        return importer.read_source(up.name, data, source)
    except Exception:
        st.error(t("csv_unreadable"))
        return None


def error_text(codes):
    return "، ".join(t(f"err_{c}") for c in codes.split(",") if c)


def _template_downloads(df, base):
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    c1, c2 = st.columns(2)
    c1.download_button(f"{t('download')} CSV", df.to_csv(index=False).encode("utf-8-sig"), f"{base}.csv", "text/csv", key=f"{base}_csv")
    c2.download_button(f"{t('download')} Excel", buf.getvalue(), f"{base}.xlsx",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key=f"{base}_xlsx")


def page_roster(user):
    hero(t("menu_roster"), t("roster_hint2"))
    fac = db.get_faculty(db.scope())
    domain = (fac or {}).get("email_domain") or "@university.edu"
    template = pd.DataFrame([["20260001", "أحمد علي", f"ahmed.ali{domain}", db.faculty_years()[-1], "المحاسبة", "Regular", 1]],
                            columns=["student_code", "full_name", "faculty_email", "academic_year", "department",
                                     "study_type", "section_no"])
    _template_downloads(template, "students_template")
    df = read_upload(t("upload_file"), "roster_up")
    if df is not None:
        clean, missing = importer.normalize_students(df, db.faculty_years(), (fac or {}).get("email_domain", ""))
        if clean is None:
            st.error(t("csv_missing_cols", cols=", ".join(missing)))
        else:
            ok = clean["errors"] == ""
            st.write(t("rows_valid", ok=int(ok.sum()), bad=int((~ok).sum())))
            if (~ok).any():
                st.warning(t("rows_invalid_hint"))
                bad = clean[~ok].copy()
                bad["errors"] = bad["errors"].map(error_text)
                st.dataframe(bad, width="stretch", hide_index=True)
            st.dataframe(clean[ok].head(20), width="stretch", hide_index=True)
            if ok.any() and st.button(t("import_valid"), type="primary", key="roster_import_btn"):
                rep_ = db.import_roster_report(clean[ok], actor=user["email"])
                st.success(t("imported", n=rep_["saved"]))
                if rep_["skipped"]:
                    st.warning(t("skipped_rows", n=len(rep_["skipped"])))
                    st.dataframe(pd.DataFrame([(c, t("skip_" + why)) for c, why in rep_["skipped"]],
                                              columns=[t("col_student_code"), t("col_errors")]),
                                 width="stretch", hide_index=True)
    roster_year_tabs()


def page_staff(user):
    """Admin creates / imports the accounts of doctors, the dean and other admins."""
    hero(t("menu_staff"), t("staff_sub"))
    tab_list, tab_add, tab_import = st.tabs([t("staff_list"), t("staff_add"), t("staff_import")])

    with tab_list:
        rows = db.list_staff()
        df = pd.DataFrame(rows)
        if not df.empty:
            df["role"] = df["role"].map(lambda r: t(f"role_{r}"))
            st.dataframe(tr_cols(df[["staff_code", "full_name", "title", "specialization", "email", "phone", "role"]]),
                         width="stretch", hide_index=True)
            with st.container(border=True):
                st.markdown(f"#### {t('reset_title')}")
                by_id = {r["id"]: r for r in rows}
                uid = st.selectbox(t("staff_pick"), list(by_id), key="staff_reset_pick",
                                   format_func=lambda i: f"{by_id[i]['full_name']} ({by_id[i]['staff_code']})")
                if st.button(t("reset_btn"), key="staff_reset_btn", type="primary"):
                    temp = db.reset_user_password(uid, user["email"], user["role"])
                    if temp:
                        st.success(t("reset_done"))
                        st.code(temp, language=None)

    with tab_add:
        st.caption(t("staff_hint"))
        with st.form("staff_form", clear_on_submit=True, border=False):
            role = st.selectbox(t("role"), creatable_roles(user), format_func=lambda r: t(f"role_{r}"))
            name = st.text_input(t("full_name"))
            c1, c2 = st.columns(2)
            title = c1.text_input(t("staff_title"), placeholder=t("staff_title_ph"))
            spec = c2.text_input(t("specialization"))
            c3, c4 = st.columns(2)
            email = c3.text_input(t("email"))
            phone = c4.text_input(t("phone"))
            c5, c6 = st.columns(2)
            code = c5.text_input(t("staff_code"), help=t("staff_code_hint"))
            pw = c6.text_input(t("staff_initial_password"), type="password", help=t("staff_pw_hint"))
            go_ = st.form_submit_button(t("create_account"), type="primary", width="stretch")
        if go_:
            temp = pw or secrets.token_urlsafe(8)
            if not (name.strip() and _EMAIL_RE.match(email.strip())):
                st.error(t("staff_invalid"))
            elif config.password_problem(temp):
                st.error(t("pw_weak") if config.password_problem(temp) == "weak" else t("staff_invalid"))
            else:
                res = db.create_staff(email, name, temp, role, code, title, spec, phone, must_change=True,
                                      actor=user["email"], actor_role=user["role"])
                if res == "ok":
                    st.success(t("staff_created"))
                    if not pw:
                        st.warning(t("reset_done"))
                        st.code(temp, language=None)
                else:
                    st.error(t("staff_exists") if res == "exists" else t("staff_invalid"))

    with tab_import:
        st.caption(t("staff_import_hint"))
        _template_downloads(pd.DataFrame([["DR-101", "د. منى السعيد", "أستاذ دكتور", "محاسبة", "mona@university.edu",
                                           "01000000000", "doctor", ""]],
                                         columns=["staff_code", "full_name", "title", "specialization", "email",
                                                  "phone", "role", "password"]), "staff_template")
        df = read_upload(t("upload_file"), "staff_up")
        if df is not None:
            clean, missing = importer.normalize_staff(df)
            if clean is None:
                st.error(t("csv_missing_cols", cols=", ".join(missing)))
            else:
                ok = clean["errors"] == ""
                st.write(t("rows_valid", ok=int(ok.sum()), bad=int((~ok).sum())))
                if (~ok).any():
                    st.warning(t("rows_invalid_hint"))
                    bad = clean[~ok].drop(columns=["password"]).copy()
                    bad["errors"] = bad["errors"].map(error_text)
                    st.dataframe(bad, width="stretch", hide_index=True)
                st.dataframe(clean[ok].drop(columns=["password"]).head(20), width="stretch", hide_index=True)
                if ok.any() and st.button(t("import_valid"), type="primary", key="staff_import_btn"):
                    st.session_state.staff_import_result = db.import_staff(clean[ok], user["email"])
        res = st.session_state.pop("staff_import_result", None)      # temporary passwords are shown ONCE
        if res:
            out = pd.DataFrame(res)
            st.success(t("staff_import_done", n=int((out["status"] == "ok").sum())))
            if (out["temp_password"] != "").any():
                st.warning(t("staff_temp_once"))
                st.dataframe(out, width="stretch", hide_index=True)
                st.download_button(t("download_csv"), csv_safe(out).to_csv(index=False).encode("utf-8-sig"),
                                   "new_staff_accounts.csv", "text/csv", key="staff_result_csv")
            else:
                st.dataframe(out.drop(columns=["temp_password"]), width="stretch", hide_index=True)


def page_manage(user):
    hero(t("menu_manage"), t("create_staff"))
    with st.container(border=True):
        st.markdown(f"#### {t('create_staff')}")
        with st.form("staff_form", clear_on_submit=True, border=False):
            name = st.text_input(t("full_name"))
            email = st.text_input(t("email"))
            pw = st.text_input(t("password"), type="password")
            role = st.selectbox(t("role"), creatable_roles(user), format_func=lambda r: t(f"role_{r}"))
            go_ = st.form_submit_button(t("create_account"), type="primary", width="stretch")
        if go_:
            if not (name.strip() and _EMAIL_RE.match(email.strip())) or config.password_problem(pw):
                st.error(t("pw_weak") if config.password_problem(pw) == "weak" else t("staff_invalid"))
            else:
                res = db.create_staff(email, name, pw, role, actor=user["email"], actor_role=user["role"])
                (st.success if res == "ok" else st.error)(
                    t("staff_created") if res == "ok" else t("staff_exists") if res == "exists" else t("staff_invalid"))

    with st.container(border=True):
        st.markdown(f"#### {t('create_course')}")
        doctors = db.list_doctors()
        if not doctors:
            st.info(t("need_doctor_first"))
        else:
            by_id = {d["id"]: d for d in doctors}
            with st.form("course_form", clear_on_submit=True, border=False):
                cname = st.text_input(t("course_name"))
                did = st.selectbox(t("doctor"), list(by_id), format_func=lambda i: by_id[i]["full_name"])
                c1, c2, c3 = st.columns(3)
                year = c1.selectbox(t("academic_year"), db.faculty_years())
                sem = c2.selectbox(t("semester"), config.SEMESTERS)
                planned = c3.number_input(t("planned_lectures"), min_value=1, max_value=40, value=14)
                go_ = st.form_submit_button(t("save"), type="primary", width="stretch")
            if go_ and cname.strip():
                if db.create_course(cname, did, by_id[did]["full_name"], year, sem, int(planned)):
                    st.success(t("course_created"))
                else:
                    st.error(t("staff_invalid"))
    courses = db.list_courses()
    if courses:
        st.dataframe(tr_cols(pd.DataFrame(courses)[["course_name", "doctor_name", "academic_year", "semester", "planned_lectures"]]),
                     width="stretch", hide_index=True)


# ============================================================================
# Shared: timetable
# ============================================================================
SLOT_MSG = {"ok": "slot_ok", "time_invalid": "time_invalid", "no_course": "slot_no_course",
            "foreign_hall": "slot_foreign_hall",
            "hall_conflict": "slot_hall_conflict", "doctor_conflict": "slot_doctor_conflict",
            "year_conflict": "slot_year_conflict"}


def render_schedule(rows):
    """Day tabs (Sat..Thu), one card per session."""
    if not rows:
        st.info(t("no_sessions"))
        return
    for d, tab in zip(config.DAYS, st.tabs([t(f"day_{d}") for d in config.DAYS])):
        with tab:
            day_rows = [r for r in rows if r["day_of_week"] == d]
            if not day_rows:
                st.caption(t("no_sessions"))
            for r in day_rows:
                with st.container(border=True):
                    is_lec = r["session_type"] == "Lecture"
                    grp = f" — {t('section_no')} {r['group_label']}" if not is_lec and r["group_label"] else ""
                    st.markdown(f"**{r['start_time']} – {r['end_time']}** · "
                                f"{t('session_' + r['session_type'])}{grp}")
                    st.write(f"{r['course_name']} — {r['doctor_name']}")
                    st.caption(f"{t('hall')}: {r['hall_name']} • {r['academic_year']}")


def schedule_filters():
    c1, c2, c3 = st.columns(3)
    year = c1.selectbox(t("academic_year"), [None] + db.faculty_years(), key="sf_year",
                        format_func=lambda y: t("all") if y is None else y)
    docs = {d["id"]: d["full_name"] for d in db.list_doctors()}
    did = c2.selectbox(t("doctor"), [None] + list(docs), key="sf_doc",
                       format_func=lambda i: t("all") if i is None else docs[i])
    halls = {h["hall_id"]: h["name"] for h in db.list_halls()}
    hid = c3.selectbox(t("hall"), [None] + list(halls), key="sf_hall",
                       format_func=lambda i: t("all") if i is None else halls[i])
    return {"year": year, "doctor_id": did, "hall_id": hid}


def page_schedule_view(user):
    """Read-only timetable. Student: own year. Doctor: own sessions. Dean: everything + filters."""
    hero(t("menu_schedule"), t("weekly_schedule"))
    if user["role"] == "student":
        rows = db.list_schedule(year=user["academic_year"])
    elif user["role"] == "doctor":
        rows = db.list_schedule(doctor_id=user["id"])
    else:
        rows = db.list_schedule(**schedule_filters())
    render_schedule(rows)


# ============================================================================
# DEAN  (rose/purple) — monitors doctors and attendance
# ============================================================================
STALE_DAYS = 14   # syllabus not updated for this long -> flagged


def page_home_dean(user):
    ov = db.doctor_overview()
    avg = db.admin_stats()["avg_rate"]
    at_risk = db.at_risk_count(config.ATTENDANCE_THRESHOLD, 1)
    hero(t("greeting", name=user["full_name"]), t("home_dean_sub"), [t("role_dean"), user_faculty(user)])
    kpis([("school", t("kpi_doctors"), int(ov["doctor_id"].nunique()) if not ov.empty else 0),
          ("trending_up", t("kpi_progress"), f"{round(ov['progress'].mean(), 1)}%" if not ov.empty else "—"),
          ("insights", t("kpi_avg_rate"), f"{avg}%" if avg is not None else "—"),
          ("warning", t("kpi_at_risk"), at_risk)])
    quick_actions(["doctors_monitor", "dean_attendance", "schedule"])


def page_doctors_monitor(user):
    hero(t("menu_doctors_monitor"), t("kpi_progress"))
    ov = db.doctor_overview()
    if ov.empty:
        st.info(t("no_courses"))
        return
    g = ov.groupby("doctor_name").agg(courses=("course_id", "count"), progress=("progress", "mean"),
                                      attendance=("attendance_rate", "mean"),
                                      days_since=("days_since", "min")).reset_index()
    g[["progress", "attendance"]] = g[["progress", "attendance"]].round(1)
    g = g.rename(columns={"doctor_name": t("col_doctor"), "courses": t("col_courses"),
                          "progress": t("col_progress"), "attendance": t("col_attendance"),
                          "days_since": t("col_days_since")})
    pc = st.column_config
    st.dataframe(g, width="stretch", hide_index=True, column_config={
        t("col_progress"): pc.ProgressColumn(t("col_progress"), min_value=0, max_value=100, format="%.0f%%"),
        t("col_attendance"): pc.ProgressColumn(t("col_attendance"), min_value=0, max_value=100, format="%.0f%%"),
    })
    for doc, part in ov.groupby("doctor_name"):
        last = part["days_since"].min()
        stale = bool(pd.isna(last) or last > STALE_DAYS)
        with st.expander(f"{doc} — {round(part['progress'].mean())}%" + (f"  ({t('stale_short')})" if stale else "")):
            if stale:
                st.warning(t("never_updated") if pd.isna(last) else t("stale_warning"))
            for _, r in part.iterrows():
                with st.container(border=True):
                    st.markdown(f"**{r['course_name']}** — {r['academic_year']}")
                    st.progress(min(float(r["progress"]) / 100, 1.0))
                    topic = f" • {r['last_topic']}" if r["last_topic"] else ""
                    st.caption(f"{t('col_last_lecture')}: {int(r['last_lecture'])} / {int(r['planned_lectures'])}{topic}")
                    if pd.notna(r["attendance_rate"]):
                        st.caption(f"{t('col_attendance')}: {r['attendance_rate']}%")


def page_dean_attendance(user):
    hero(t("menu_dean_attendance"), t("tab_warning"))
    kind = st.radio(t("attendance_kind"), ["Lecture", "Section"], horizontal=True, key="dean_kind",
                    format_func=lambda k: t(f"att_kind_{k}"))
    tabs = st.tabs([t("by_department"), t("by_doctor"), t("doctor_dept_matrix"), t("tab_warning")])
    with tabs[0]:
        year = st.selectbox(t("academic_year"), [None] + db.faculty_years(), key="dean_year",
                            format_func=lambda y: t("all_years") if y is None else y)
        df = db.attendance_by_department(year, kind)
        if df.empty:
            st.info(t("no_data"))
        else:
            st.bar_chart(df.set_index("department")["rate"])
            st.dataframe(tr_cols(df), width="stretch", hide_index=True)
    with tabs[1]:
        df = db.attendance_by_doctor(kind)
        if df.empty:
            st.info(t("no_data"))
        else:
            st.bar_chart(df.set_index("doctor_name")["rate"])
            st.dataframe(tr_cols(df), width="stretch", hide_index=True)
    with tabs[2]:
        df = db.attendance_doctor_department(kind)
        if df.empty:
            st.info(t("no_data"))
        else:
            st.dataframe(df.pivot_table(index="doctor_name", columns="department", values="rate", aggfunc="mean").round(1), width="stretch")
    with tabs[3]:
        thr = st.slider(t("threshold"), 10, 100, config.ATTENDANCE_THRESHOLD, key="dean_thr")
        wdf = db.early_warning(thr, 3, kind)
        if wdf.empty:
            st.success(t("no_warnings"))
        else:
            st.error(t("warning_count", n=wdf["student_code"].nunique()))
            wdf["study_type"] = wdf["study_type"].map(lambda v: t(f"study_{v}"))
            wdf["kind"] = wdf["kind"].map(lambda v: t(f"att_kind_{v}"))
            st.dataframe(tr_cols(wdf), width="stretch", hide_index=True)


# ============================================================================
# ADMIN additions: registered students + timetable management
# ============================================================================
def page_registered(user):
    hero(t("menu_registered"), t("reset_hint"))
    c1, c2 = st.columns([1, 2])
    year = c1.selectbox(t("academic_year"), [None] + db.faculty_years(), key="reg_year",
                        format_func=lambda y: t("all_years") if y is None else y)
    q = importer.norm(c2.text_input(t("reg_search"), key="reg_q"))
    rows = db.registered_students(year)
    if q:
        rows = [r for r in rows if q in importer.norm(r["full_name"]) or q in importer.norm(r["student_code"])]
    s = db.admin_stats()
    kpis([("verified_user", t("reg_count"), s["activated"]),
          ("person_off", t("reg_not_yet"), max(s["roster"] - s["activated"], 0))])
    if not rows:
        st.info(t("no_data"))
        return
    df = pd.DataFrame(rows)
    df["study_type"] = df["study_type"].map(lambda v: t(f"study_{v}"))
    st.dataframe(tr_cols(df[["full_name", "student_code", "academic_year", "department", "study_type",
                             "section_no", "email", "recovery_email", "created_at"]]), width="stretch", hide_index=True)
    with st.container(border=True):
        st.markdown(f"#### {t('reset_title')}")
        by_code = {r["student_code"]: r for r in rows}
        code = st.selectbox(t("reset_pick"), list(by_code), key="reset_pick",
                            format_func=lambda c: f"{by_code[c]['full_name']} ({c})")
        if st.button(t("reset_btn"), key="reset_btn", type="primary"):
            temp = db.reset_student_password(code, user["email"], user["role"])
            if temp:                       # shown ONCE, never stored in readable form
                st.success(t("reset_done"))
                st.code(temp, language=None)
    with st.expander(t("audit_log")):
        audit = db.list_audit()
        if audit:
            st.dataframe(pd.DataFrame(audit)[["timestamp", "actor", "action", "target"]],
                         width="stretch", hide_index=True)
        else:
            st.caption(t("no_data"))


def page_schedule_admin(user):
    hero(t("menu_schedule_admin"), t("weekly_schedule"))
    tab_view, tab_add, tab_halls = st.tabs([t("weekly_schedule"), t("add_session"), t("halls")])

    with tab_view:
        rows = db.list_schedule(**schedule_filters())
        render_schedule(rows)
        if rows:
            with st.container(border=True):
                by_id = {r["slot_id"]: r for r in rows}
                sid = st.selectbox(t("delete"), list(by_id), key="del_slot_sel", format_func=lambda i: (
                    f"{t('day_' + str(by_id[i]['day_of_week']))} {by_id[i]['start_time']} — "
                    f"{by_id[i]['course_name']} ({t('session_' + by_id[i]['session_type'])})"))
                if st.button(t("delete"), key="del_slot_btn"):
                    db.delete_slot(sid)
                    st.rerun()

    with tab_add:
        courses, halls = db.list_courses(), db.list_halls()
        if not courses:
            st.info(t("need_course_first"))
        elif not halls:
            st.info(t("need_hall_first"))
        else:
            stype = st.radio(t("session_type"), ["Lecture", "Section"], horizontal=True, key="new_slot_type",
                             format_func=lambda x: t(f"session_{x}"))
            by_c, by_h = {c["course_id"]: c for c in courses}, {h["hall_id"]: h for h in halls}
            with st.form("slot_form", border=False):
                cid = st.selectbox(t("course"), list(by_c), format_func=lambda i: (
                    f"{by_c[i]['course_name']} — {by_c[i]['doctor_name']} ({by_c[i]['academic_year']})"))
                hid = st.selectbox(t("hall"), list(by_h), format_func=lambda i: (
                    f"{by_h[i]['name']} ({t('kind_' + by_h[i]['kind'])}, {by_h[i]['capacity']})"
                    + ("" if by_h[i]["latitude"] is not None else f" — {t('no_gps_point')}")))
                c1, c2, c3 = st.columns(3)
                day = c1.selectbox(t("day"), config.DAYS, format_func=lambda d: t(f"day_{d}"))
                start = c2.time_input(t("start_time"), value=dtime(9, 0), step=900)
                end = c3.time_input(t("end_time"), value=dtime(10, 30), step=900)
                group = st.number_input(t("section_no"), 1, 30, 1) if stype == "Section" else ""
                go_ = st.form_submit_button(t("add_session"), type="primary", width="stretch")
            if go_:
                res = db.add_slot(cid, hid, stype, group, day, start.strftime("%H:%M"), end.strftime("%H:%M"))
                (st.success if res == "ok" else st.error)(t(SLOT_MSG[res]))

    with tab_halls:
        hall_forms()


def _fmt_point(lat, lon):
    return f"{lat:.6f}, {lon:.6f}" if lat is not None and lon is not None else "—"


def hall_forms():
    """Halls + the GPS point (and allowed radius) that attendance in that hall is checked against."""
    with st.container(border=True):
        st.markdown(f"#### {t('hall_add')}")
        st.caption(t("hall_coords_hint"))
        if st.session_state.get("_hall_fid") != db.scope():          # another faculty -> start from ITS point
            st.session_state["_hall_fid"] = db.scope()
            st.session_state["hall_lat"], st.session_state["hall_lon"] = map(float, map_start())
        cap = st.button(t("hall_capture"), key="hall_capture_btn")
        pos = read_gps(cap, "hall")
        if pos:
            st.session_state["hall_lat"], st.session_state["hall_lon"] = float(pos[0]), float(pos[1])
            st.info(t("hall_captured", acc=int(pos[2] or 0)))
        with st.form("hall_form", border=False):
            name = st.text_input(t("hall_name"))
            c1, c2 = st.columns(2)
            kind = c1.selectbox(t("hall_kind"), ["Amphitheater", "Section Room"], format_func=lambda k: t(f"kind_{k}"))
            cap_n = c2.number_input(t("capacity"), 0, 5000, 100, step=10)
            c3, c4, c5 = st.columns(3)
            lat = c3.number_input(t("latitude"), format="%.7f", key="hall_lat")
            lon = c4.number_input(t("longitude"), format="%.7f", key="hall_lon")
            radius = c5.number_input(t("radius_m"), 1, 500, 30, help=t("radius_hint"))
            go_ = st.form_submit_button(t("save"), type="primary", width="stretch")
        st.caption(t("radius_hint"))
        if go_ and name.strip():
            res = db.add_hall(name, kind, cap_n, lat, lon, radius)
            (st.success if res == "ok" else st.error)(t("hall_added") if res == "ok" else t("hall_exists"))
            if res == "ok" and radius < 15:
                st.warning(t("radius_warning", n=radius))

    halls = db.list_halls()
    if halls:
        with st.container(border=True):
            st.markdown(f"#### {t('hall_edit')}")
            by_id = {h["hall_id"]: h for h in halls}
            hid = st.selectbox(t("hall"), list(by_id), key="hall_edit_pick", format_func=lambda i: by_id[i]["name"])
            h = by_id[hid]
            with st.form(f"hall_edit_{hid}", border=False):
                name = st.text_input(t("hall_name"), value=h["name"])
                c1, c2 = st.columns(2)
                kind = c1.selectbox(t("hall_kind"), ["Amphitheater", "Section Room"],
                                    index=0 if h["kind"] == "Amphitheater" else 1, format_func=lambda k: t(f"kind_{k}"))
                cap_n = c2.number_input(t("capacity"), 0, 5000, int(h["capacity"]), step=10)
                c3, c4, c5 = st.columns(3)
                lat = c3.number_input(t("latitude"), value=float(h["latitude"] if h["latitude"] is not None else map_start()[0]), format="%.7f")
                lon = c4.number_input(t("longitude"), value=float(h["longitude"] if h["longitude"] is not None else map_start()[1]), format="%.7f")
                radius = c5.number_input(t("radius_m"), 1, 500, int(h["radius_m"]))
                go_ = st.form_submit_button(t("save"), type="primary", width="stretch")
            if go_:
                res = db.update_hall(hid, name, kind, cap_n, lat, lon, radius)
                if res == "ok":
                    st.success(t("hall_updated"))
                    if radius < 15:
                        st.warning(t("radius_warning", n=radius))
                else:
                    st.error(t("hall_exists"))
        usage = pd.DataFrame(db.halls_usage())
        usage["point"] = [_fmt_point(a, b) for a, b in zip(usage["latitude"], usage["longitude"])]
        usage["kind"] = usage["kind"].map(lambda k: t(f"kind_{k}"))
        usage["session_type"] = usage["session_type"].map(lambda v: t(f"session_{v}") if isinstance(v, str) else "")
        st.dataframe(usage[["name", "kind", "point", "radius_m", "course_name", "doctor_name", "session_type", "group_label"]]
                     .rename(columns={"name": t("hall_name"), "kind": t("hall_kind"), "point": t("gps_point"),
                                      "radius_m": t("radius_m"), "course_name": t("col_course_name"),
                                      "doctor_name": t("col_doctor"), "session_type": t("col_session_type"),
                                      "group_label": t("col_group_label")}), width="stretch", hide_index=True)
        missing = db.courses_without_location()
        if missing:
            st.warning(t("courses_without_location", n=len(missing)))


def account_settings(user):
    """Recovery e-mail + change password. Shown inside the student profile and as a page for staff."""
    uid = user["id"]
    st.markdown(f"**{t('recovery_email')}**")
    st.caption(t("recovery_hint"))
    with st.form(f"rec_form_{uid}", border=False):
        new = st.text_input(t("recovery_email"), value=user.get("recovery_email") or "")
        go_ = st.form_submit_button(t("save"), width="stretch")
    if go_:
        new = new.strip().lower()
        if _EMAIL_RE.match(new):
            if db.set_recovery_email(uid, new) == "ok":
                st.session_state.user["recovery_email"] = new
                st.success(t("saved"))
            else:
                st.error(t("onb_email_taken"))
        else:
            st.error(t("fp_bad_email"))
    st.markdown(f"**{t('change_password')}**")
    with st.form(f"pw_form_{uid}", border=False):
        old = st.text_input(t("current_password"), type="password")
        p1 = st.text_input(t("new_password"), type="password")
        p2 = st.text_input(t("confirm_password"), type="password")
        go_ = st.form_submit_button(t("change_password"), width="stretch")
    if go_:
        if not db.check_password_of(uid, old):
            st.error(t("bad_current_password"))
        elif pw_error(p1, p2):
            st.error(pw_error(p1, p2))
        elif db.change_password(uid, p1) == "same":
            st.error(t("pw_same"))
        else:
            st.session_state.user["epoch"] = db.session_epoch(uid)      # this session stays valid, the others end
            st.success(t("pw_changed"))


def page_account(user):
    hero(user["full_name"], user["email"], [t(f"role_{user['role']}")] + [x for x in (user.get("title"), user.get("specialization")) if x])
    if user.get("staff_code"):
        with st.container(border=True):
            st.caption(t("staff_code")); st.write(user["staff_code"])
    with st.container(border=True):
        account_settings(user)


def page_force_password(user):
    """After an admin reset, the user must pick their own password before doing anything else."""
    _, c2 = st.columns([3, 2])
    with c2:
        lang_buttons("force")
    hero(t("must_change_title"), t("must_change_hint"))
    with st.container(border=True):
        with st.form("force_pw", border=False):
            p1 = st.text_input(t("new_password"), type="password")
            p2 = st.text_input(t("confirm_password"), type="password")
            go_ = st.form_submit_button(t("save"), type="primary", width="stretch")
        if go_:
            if pw_error(p1, p2):
                st.error(pw_error(p1, p2))
            elif db.change_password(user["id"], p1) == "same":
                st.error(t("pw_same"))
            else:
                st.session_state.user["must_change"] = False
                st.session_state.user["epoch"] = db.session_epoch(user["id"])
                st.rerun()
    st.button(t("logout"), key="logout_force", on_click=do_logout)


# ============================================================================
# Router
# ============================================================================
PAGES = {
    "student": {"home": page_home_student, "attendance": page_attendance, "syllabus": page_syllabus,
                "assignments": page_assignments_student, "schedule": page_schedule_view,
                "planner": page_planner, "chat": page_chat_student, "complaints": page_complaints_student,
                "profile": page_profile},
    "doctor": {"home": page_home_doctor, "syllabus_tracker": page_syllabus_doctor,
               "attendance_control": page_attendance_doctor, "section_attendance": page_section_attendance,
               "assignments": page_assignments_doctor, "schedule": page_schedule_view, "chat": page_chat_doctor,
               "account": page_account},
    "dean": {"home": page_home_dean, "doctors_monitor": page_doctors_monitor,
             "dean_attendance": page_dean_attendance, "schedule": page_schedule_view, "account": page_account},
    "faculty_admin": {"home": page_faculty_dashboard, "complaints_admin": page_complaints_admin,
                      "analytics": page_analytics, "roster": page_roster, "registered": page_registered,
                      "staff": page_staff, "schedule_admin": page_schedule_admin, "manage": page_manage,
                      "account": page_account},
    "admin": {"home": page_home_admin, "faculty_dashboard": page_faculty_dashboard, "faculties": page_faculties,
              "complaints_admin": page_complaints_admin, "analytics": page_analytics,
              "roster": page_roster, "registered": page_registered, "staff": page_staff,
              "schedule_admin": page_schedule_admin, "manage": page_manage, "account": page_account},
}
# Admin pages that work on the university as a whole; every other admin page works on ONE faculty (the selector).
ADMIN_GLOBAL_PAGES = {"home", "faculties", "account"}


def _pick_faculty():
    st.session_state["_admin_fid"] = st.session_state["admin_fid_pick"]


def faculty_selector():
    """System admin: which faculty the page below works on. Returns its ID (None if there is no faculty yet)."""
    facs = db.list_faculties()
    if not facs:
        return None
    by_id = {f["faculty_id"]: f for f in facs}
    cur = st.session_state.get("_admin_fid")
    if cur not in by_id:
        cur = facs[0]["faculty_id"]
    st.session_state["_admin_fid"] = cur
    st.session_state["admin_fid_pick"] = cur          # keep the widget in step (it is not rendered on every page)
    st.selectbox(t("current_faculty"), list(by_id), key="admin_fid_pick", on_change=_pick_faculty,
                 format_func=lambda i: f"#{i} · {fac_name(by_id[i])}" + ("" if by_id[i]["is_active"] else f" ({t('faculty_disabled')})"))
    return cur


def main():
    init_state()
    _boot()
    st.session_state["_kpi_n"] = 0
    db.clear_scope()                         # a rerun never inherits the faculty of a previous run (fail-closed)
    user = st.session_state.user
    if user:                                 # every rerun: is this session still allowed? (SEC-04)
        idle = st.session_state.get("last_seen")
        fresh = db.session_user(user["id"])
        if idle and (config.now() - idle).total_seconds() > config.SESSION_IDLE_MIN * 60:
            reason = "session_expired"
        elif fresh is None or fresh["epoch"] != user["epoch"] or fresh["role"] != user["role"] \
                or (fresh["role"] != "admin" and not fresh["faculty_active"]):
            reason = "session_changed"
        else:
            reason = None
            st.session_state.user = user = {**fresh, "epoch": user["epoch"]}
            st.session_state.last_seen = config.now()
        if reason:
            do_logout()
            init_state()
            st.session_state.logout_reason = reason
            user = None
    inject_css(user["role"] if user else "auth")
    if edition.IS_DEMO:
        st.caption(t("demo_banner"))
    if not user:
        reason = st.session_state.pop("logout_reason", None)
        if reason:
            st.warning(t(reason))
        page_auth()
        return

    if user.get("must_change"):              # admin reset the password -> force a new one first
        page_force_password(user)
        return

    if user["role"] != "admin":
        if not user.get("faculty_id"):       # a staff / student account with no faculty cannot see any data
            st.error(t("no_faculty_account"))
            st.button(t("logout"), key="logout_nofac", on_click=do_logout)
            return
        db.set_scope(user["faculty_id"])     # EVERY query of this run is limited to the user's own faculty

    pages = PAGES[user["role"]]
    page = st.session_state.get("page")
    if page not in pages:
        page = next(iter(pages))
    st.session_state.page = page
    st.session_state["nav_top"] = page       # keep both menus in sync with the single source of truth
    st.session_state["nav_side"] = page

    top_bar(user, pages)       # shown on screens >= 992 px (CSS)
    side_bar(user, pages)      # drawer for phones and tablets (CSS)
    msg = st.session_state.pop("_flash", None)
    if msg:
        st.success(msg)
    if user["role"] == "admin" and page not in ADMIN_GLOBAL_PAGES:
        fid = faculty_selector()
        if fid is None:
            st.info(t("no_faculties_yet"))
            st.button(nav_label("faculties"), key="nofac_go", on_click=go, args=("faculties",), type="primary")
            return
        db.set_scope(fid)
    if user["role"] == "student":
        show_alerts(user["academic_year"])
    pages[page](user)


main()
