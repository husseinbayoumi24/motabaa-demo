"""importer.py — read student / staff lists from almost any file the admin has.

Supported: Excel (.xlsx), CSV, TXT / TSV (comma, semicolon, tab or pipe separated) and JSON.
(Uploading database files is NOT supported: opening an untrusted binary file is an attack surface.)
Column headers may be English or Arabic and in any order;
values like "الفرقة الثالثة", "3", "third" are all understood.

Nothing here touches the application database: it only returns cleaned DataFrames + a per-row error
list, so the admin can review the problems before anything is imported.
The number of academic years and the allowed e-mail domain depend on the faculty, so the callers pass them in.
"""
import io
import json
import os
import re
import zipfile

import pandas as pd

import config

MAX_ROWS = 20000
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_AR = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})


def norm(value) -> str:
    """Lower-case, no diacritics/spaces/punctuation, Arabic letters unified, digits ASCII."""
    s = str(value if value is not None else "").strip().lower().translate(_DIGITS)
    s = re.sub(r"[\u064B-\u065F\u0670\u0640]", "", s).translate(_AR)
    return re.sub(r"[\s_\-\.\(\)/\\:]+", "", s)


# ---------------------------------------------------------------- reading files
MAX_XLSX_UNCOMPRESSED = 60 * 1024 * 1024      # an .xlsx is a zip: refuse "zip bombs" before pandas expands them


def _check_xlsx(data: bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > 500 or sum(i.file_size for i in infos) > MAX_XLSX_UNCOMPRESSED:
                raise ValueError("spreadsheet too large")
    except zipfile.BadZipFile:
        raise ValueError("not an xlsx file") from None


def _ext(name):
    return os.path.splitext(name.lower())[1]


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1256", "latin-1"):    # Arabic Windows exports are often cp1256
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def list_sources(name: str, data: bytes):
    """Sheets of an Excel file; [''] for single-table formats."""
    ext = _ext(name)
    if ext in (".xlsx", ".xlsm"):
        _check_xlsx(data)
        return pd.ExcelFile(io.BytesIO(data), engine="openpyxl").sheet_names
    if ext in (".csv", ".txt", ".tsv", ".dat", ".json"):
        return [""]
    raise ValueError("unsupported")


def read_source(name: str, data: bytes, source: str = "") -> pd.DataFrame:
    """Return the chosen sheet/table as a DataFrame of strings (empty cell -> '')."""
    ext = _ext(name)
    if ext in (".xlsx", ".xlsm"):
        _check_xlsx(data)
        df = pd.read_excel(io.BytesIO(data), sheet_name=source or 0, dtype=str, engine="openpyxl",
                           nrows=MAX_ROWS + 1)
    elif ext == ".json":
        obj = json.loads(_decode(data))
        if isinstance(obj, dict):                    # {"students": [...]} -> the first list found
            obj = next((v for v in obj.values() if isinstance(v, list)), [obj])
        if not isinstance(obj, list) or len(obj) > MAX_ROWS:
            raise ValueError("bad json")
        df = pd.DataFrame(obj)
    elif ext in (".csv", ".txt", ".tsv", ".dat"):
        text = _decode(data)
        try:
            df = pd.read_csv(io.StringIO(text), sep=None, engine="python", dtype=str, nrows=MAX_ROWS + 1)   # sniffs , ; tab |
        except Exception:
            df = pd.read_csv(io.StringIO(text), dtype=str, nrows=MAX_ROWS + 1)
    else:
        raise ValueError("unsupported")
    df = df.head(MAX_ROWS).fillna("").astype(str)
    df.columns = [str(c).strip() for c in df.columns]
    return df


# ---------------------------------------------------------------- header mapping
_STUDENT_FIELDS = {
    "student_code": ["student_code", "code", "id", "student_id", "كود", "كود الطالب", "الكود", "رقم الطالب", "رقم الجلوس"],
    "full_name": ["full_name", "name", "student_name", "الاسم", "اسم الطالب", "الاسم الكامل", "اسم"],
    "faculty_email": ["faculty_email", "email", "mail", "university_email", "البريد", "البريد الالكتروني",
                      "البريد الجامعي", "الايميل", "ايميل", "الجيميل", "جيميل"],
    "academic_year": ["academic_year", "year", "level", "الفرقة", "السنة", "السنة الدراسية", "المستوى", "الفرقه"],
    "department": ["department", "dept", "القسم", "الشعبة", "التخصص", "شعبة"],
    "study_type": ["study_type", "type", "نوع الدراسة", "نظام الدراسة", "النظام", "منتظم/منتسب", "نوع"],
    "section_no": ["section_no", "section", "group", "السكشن", "رقم السكشن", "المجموعة", "سكشن"],
}
_STAFF_FIELDS = {
    "staff_code": ["staff_code", "code", "id", "كود", "الكود", "كود الدكتور", "رقم وظيفي", "الرقم الوظيفي"],
    "full_name": ["full_name", "name", "الاسم", "اسم", "الاسم الكامل", "اسم الدكتور"],
    "title": ["title", "rank", "degree", "الدرجة العلمية", "الدرجة", "اللقب", "الوظيفة", "دكتور ايه", "درجة"],
    "specialization": ["specialization", "speciality", "specialty", "field", "التخصص", "تخصص", "القسم", "department"],
    "email": ["email", "mail", "e-mail", "البريد", "البريد الالكتروني", "الايميل", "ايميل", "الجيميل", "جيميل"],
    "role": ["role", "type", "الدور", "الصفة", "نوع الحساب"],
    "phone": ["phone", "mobile", "tel", "الهاتف", "الموبايل", "التليفون", "رقم الهاتف", "رقم الموبايل"],
    "password": ["password", "pass", "كلمة المرور", "كلمة السر", "الباسورد", "الرقم السري"],
}


def _map_headers(df, fields):
    lookup = {norm(alias): canon for canon, aliases in fields.items() for alias in aliases}
    rename, used = {}, set()
    for col in df.columns:
        canon = lookup.get(norm(col))
        if canon and canon not in used:
            rename[col] = canon
            used.add(canon)
    return df.rename(columns=rename)[list(rename.values())]


# ---------------------------------------------------------------- value parsers
_YEAR_WORDS = [(1, ["اول", "first", "1st"]), (2, ["ثان", "second", "2nd"]),
               (3, ["ثالث", "third", "3rd"]), (4, ["رابع", "fourth", "4th"]),
               (5, ["خامس", "fifth", "5th"]), (6, ["سادس", "sixth", "6th"]), (7, ["سابع", "seventh", "7th"])]


def parse_year(value, years=None) -> str:
    """'3', 'الفرقة الثالثة', 'third', '٣' ... -> '3rd Year'. `years` = the faculty's year names ('' if the
    value is not one of them, e.g. year 5 in a 4-year faculty)."""
    years = years or config.YEARS
    s = norm(value)
    digits = re.findall(r"\d+", s)
    if len(digits) == 1 and len(digits[0]) == 1:
        n = int(digits[0])
        return years[n - 1] if 1 <= n <= len(years) else ""
    if digits:
        return ""
    for n, words in _YEAR_WORDS:
        if n <= len(years) and any(w in s for w in words):
            return years[n - 1]
    return ""


def parse_study_type(value) -> str:
    s = norm(value)
    if "منتسب" in s or "affil" in s or "انتساب" in s or "distance" in s:
        return "Affiliated"
    if s == "" or "منتظم" in s or "regular" in s or "نظامي" in s:
        return "Regular"
    return ""


_DOCTOR_WORDS = {"دكتور", "الدكتور", "دكتوره", "استاذ", "استاذدكتور", "معيد", "مدرس", "مدرسمساعد", "doctor", "dr", "ta",
                 "instructor", "professor", "lecturer", "teachingassistant", "assistant"}
_DEAN_WORDS = {"عميد", "العميد", "dean"}
_ADMIN_WORDS = {"admin", "ادمن", "مدير", "مديرالنظام", "administrator", "facultyadmin", "مديركليه"}


def parse_role(value) -> str:
    """EXACT words only: doctor | dean | admin (always rejected by import) | '' (unknown / EMPTY = error).
    The old substring matching turned 'وكيل عميد' into dean and 'Data Analyst' into doctor."""
    s = norm(value)
    if s in _DEAN_WORDS:
        return "dean"
    if s in _ADMIN_WORDS:
        return "admin"
    if s in _DOCTOR_WORDS:
        return "doctor"
    return ""


def _dups(series):
    s = series.str.lower()
    return s.duplicated(keep=False) & (s != "")


# ---------------------------------------------------------------- normalisers
_EMAIL_ANY = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_students(df: pd.DataFrame, years=None, email_domain=""):
    """-> (clean_df, missing_required_columns). clean_df has the canonical columns plus `errors`
    (comma-separated codes: code, name, email, year, dept, type, section, dup).
    years = the faculty's year names; email_domain = required e-mail domain of the faculty ('' = any)."""
    d = _map_headers(df, _STUDENT_FIELDS)
    missing = [c for c in ("student_code", "full_name", "faculty_email", "academic_year", "department") if c not in d]
    if missing:
        return None, missing
    for opt, default in (("study_type", ""), ("section_no", "")):
        if opt not in d:
            d[opt] = default
    d = d.apply(lambda s: s.str.strip())
    d["student_code"] = d["student_code"].str.translate(_DIGITS).str.replace(r"\.0$", "", regex=True)
    d["faculty_email"] = d["faculty_email"].str.lower().str.translate(_DIGITS)
    d["academic_year"] = d["academic_year"].map(lambda v: parse_year(v, years))
    d["study_type"] = d["study_type"].map(parse_study_type)
    sec = d["section_no"].str.translate(_DIGITS).str.extract(r"(\d+)")[0]
    d["section_bad"] = (d["section_no"] != "") & sec.isna()
    d["section_no"] = sec.fillna("1").astype(int).clip(lower=1)

    err = pd.DataFrame(index=d.index)
    err["code"] = (d["student_code"] == "") | d["student_code"].str.match(r"^(DR|DN|FA|AD)\d+$", case=False)
    err["name"] = d["full_name"] == ""
    err["email"] = ~d["faculty_email"].str.match(config.EMAIL_REGEX, case=False)
    if email_domain:
        err["email"] |= ~d["faculty_email"].str.endswith(email_domain.lower())
    err["year"] = d["academic_year"] == ""
    err["dept"] = d["department"] == ""
    err["type"] = d["study_type"] == ""
    err["section"] = d["section_bad"]
    err["dup"] = _dups(d["student_code"]) | _dups(d["faculty_email"])
    d["errors"] = err.apply(lambda r: ",".join(k for k, v in r.items() if v), axis=1)
    return d.drop(columns=["section_bad"]), []


def normalize_staff(df: pd.DataFrame):
    d = _map_headers(df, _STAFF_FIELDS)
    missing = [c for c in ("full_name", "email") if c not in d]
    if missing:
        return None, missing
    for opt in ("staff_code", "title", "specialization", "role", "phone", "password"):
        if opt not in d:
            d[opt] = ""
    d = d.apply(lambda s: s.str.strip())
    d["email"] = d["email"].str.lower()
    d["role"] = d["role"].map(parse_role)
    err = pd.DataFrame(index=d.index)
    err["name"] = d["full_name"] == ""
    err["email"] = ~d["email"].str.match(_EMAIL_ANY)
    err["role"] = d["role"].isin(["", "admin"])          # a system admin is never created from a faculty file
    err["pw"] = (d["password"] != "") & d["password"].map(lambda p: config.password_problem(p) is not None)
    err["dup"] = _dups(d["email"]) | (_dups(d["staff_code"]))
    d["errors"] = err.apply(lambda r: ",".join(k for k, v in r.items() if v), axis=1)
    return d, []
