import sqlite3
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from functools import wraps
from flask import Flask, render_template, request, jsonify, g, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from billing_codes import resolve_billing_codes

app = Flask(__name__)
app.secret_key = os.environ.get("XRAY_PILOT_SECRET", "change-me-before-real-use")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")
FOLLOWUP_DAYS = 3
URGENT_FOLLOWUP_DAYS = 5
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.environ.get("TWILIO_FROM_NUMBER")
CLINIC_SMS_NAME = os.environ.get("CLINIC_SMS_NAME", "Legacy X-ray")
CLINIC_TIMEZONE = ZoneInfo("America/Winnipeg")
CLOSING_SOON_BUFFER_MINUTES = 60
EXTENDED_WEDNESDAY_THRESHOLD = "17:30"  # Wednesday close later than this counts as "evening hours"


def get_clinic_hours_by_day(db, clinic_name):
    rows = db.execute(
        "SELECT * FROM clinic_hours WHERE clinic_name = ? ORDER BY day_of_week", (clinic_name,)
    ).fetchall()
    return {row["day_of_week"]: row for row in rows}


def format_time_12h(hhmm):
    hour, minute = map(int, hhmm.split(":"))
    period = "AM" if hour < 12 else "PM"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d} {period}" if minute else f"{hour12} {period}"


DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def compute_arrival_message(db, clinic_name, now=None):
    """Builds the one-way arrival-time SMS body. `now` is injectable for testing;
    defaults to the current time in the clinic's local timezone."""
    now = now or datetime.now(CLINIC_TIMEZONE)
    hours_by_day = get_clinic_hours_by_day(db, clinic_name)
    weekday = now.weekday()  # 0=Monday .. 6=Sunday
    today = hours_by_day.get(weekday)

    def is_closing_soon_or_closed(day_row, current_time):
        if not day_row or not day_row["is_open"]:
            return True
        close_hour, close_minute = map(int, day_row["close_time"].split(":"))
        close_dt = current_time.replace(hour=close_hour, minute=close_minute, second=0, microsecond=0)
        return current_time >= close_dt - timedelta(minutes=CLOSING_SOON_BUFFER_MINUTES)

    if today and not is_closing_soon_or_closed(today, now):
        # Version 1: plenty of time left today
        base = f"please plan to arrive later today before {format_time_12h(today['close_time'])} to help us minimize your wait."
        base_day_name = None  # base refers to today, not a named future day
    else:
        # Version 2: closed or closing soon -- find the next day that's actually open
        next_day = None
        next_day_name = None
        for offset in range(1, 8):
            candidate_weekday = (weekday + offset) % 7
            candidate = hours_by_day.get(candidate_weekday)
            if candidate and candidate["is_open"]:
                next_day = candidate
                next_day_name = DAY_NAMES[candidate_weekday]
                break
        if next_day:
            base = (
                f"we're closed or closing soon today. Please plan to arrive {next_day_name} "
                f"between {format_time_12h(next_day['open_time'])} and {format_time_12h(next_day['close_time'])}."
            )
            base_day_name = next_day_name
        else:
            base = "please contact us to arrange a convenient time for your imaging exam."
            base_day_name = None

    # Version 3: extended-hours nudge, layered on top of either base message above --
    # skipped if the base message already points at that same day, to avoid repeating it.
    extra = ""
    wednesday = hours_by_day.get(2)
    saturday = hours_by_day.get(5)
    if (weekday in (1, 2) and wednesday and wednesday["is_open"]
            and wednesday["close_time"] > EXTENDED_WEDNESDAY_THRESHOLD and base_day_name != "Wednesday"):
        extra = f" We're also open late Wednesday until {format_time_12h(wednesday['close_time'])} if that's more convenient."
    elif weekday == 4 and saturday and saturday["is_open"] and base_day_name != "Saturday":
        extra = (
            f" We're also open Saturday {format_time_12h(saturday['open_time'])}"
            f"-{format_time_12h(saturday['close_time'])} if that's more convenient."
        )

    return f"{CLINIC_SMS_NAME}: {base}{extra} Reply not required."


def send_arrival_sms(to_number, message_body):
    """Sends a one-way SMS with the given message body. No reply is expected or processed."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM_NUMBER):
        raise RuntimeError("Twilio is not configured (missing env vars)")
    from twilio.rest import Client  # imported here so the app runs fine without twilio installed
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    client.messages.create(to=f"+1{to_number}", from_=TWILIO_FROM_NUMBER, body=message_body)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_id INTEGER,
            referring_doc TEXT NOT NULL,
            clinic_name TEXT,
            fax_number TEXT,
            patient_ref TEXT,
            studies TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'issued',
            followup_note TEXT,
            followup_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
    for column_def in [
        "patient_name TEXT",
        "patient_dob TEXT",
        "patient_sex TEXT",
        "patient_phin TEXT",
        "patient_phone TEXT",
        "doctor_address TEXT",
        "clinical_info TEXT",
        "suggested_time TEXT",
        "sms_sent_at TEXT",
        "preferred_clinic TEXT",
        "sms_consent INTEGER DEFAULT 1",
        "clinic_notified_at TEXT",
        "billing_codes TEXT",
    ]:
        try:
            db.execute(f"ALTER TABLE orders ADD COLUMN {column_def}")
        except sqlite3.OperationalError:
            pass  # column already exists from a prior run
    db.execute("""
        CREATE TABLE IF NOT EXISTS doctors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            clinic_name TEXT,
            fax_number TEXT,
            pin_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    try:
        db.execute("ALTER TABLE doctors ADD COLUMN address TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists from a prior run
    db.execute("""
        CREATE TABLE IF NOT EXISTS clinic_hours (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clinic_name TEXT NOT NULL,
            day_of_week INTEGER NOT NULL,
            is_open INTEGER NOT NULL DEFAULT 1,
            open_time TEXT NOT NULL DEFAULT '08:00',
            close_time TEXT NOT NULL DEFAULT '17:00',
            UNIQUE(clinic_name, day_of_week)
        )
    """)
    existing = db.execute("SELECT COUNT(*) FROM clinic_hours").fetchone()[0]
    if existing == 0:
        for clinic in ["Legacy X-ray", "Park City X-ray"]:
            for day in range(7):  # 0=Monday .. 6=Sunday
                is_open = 1 if day < 5 else 0  # default: open Mon-Fri, closed Sat/Sun
                db.execute(
                    "INSERT INTO clinic_hours (clinic_name, day_of_week, is_open, open_time, close_time) VALUES (?, ?, ?, ?, ?)",
                    (clinic, day, is_open, "08:00", "17:00"),
                )
    db.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            ip TEXT PRIMARY KEY,
            failed_count INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT
        )
    """)
    db.commit()
    db.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("doctor_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def get_client_ip():
    return request.headers.get("X-Real-IP", request.remote_addr or "unknown")


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pin = (request.form.get("pin") or "").strip()
        db = get_db()
        ip = get_client_ip()
        now = datetime.utcnow()

        attempt_row = db.execute("SELECT * FROM login_attempts WHERE ip = ?", (ip,)).fetchone()
        if attempt_row and attempt_row["locked_until"]:
            locked_until = datetime.fromisoformat(attempt_row["locked_until"])
            if now < locked_until:
                minutes_left = int((locked_until - now).total_seconds() // 60) + 1
                return render_template(
                    "login.html",
                    error=f"Too many attempts. Try again in {minutes_left} minute(s).",
                )

        doc = None
        for row in db.execute("SELECT * FROM doctors").fetchall():
            if check_password_hash(row["pin_hash"], pin):
                doc = row
                break

        if doc:
            db.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))
            db.commit()
            session["doctor_id"] = doc["id"]
            session["doctor_name"] = doc["name"]
            session["doctor_clinic"] = doc["clinic_name"]
            session["doctor_fax"] = doc["fax_number"]
            session["doctor_address"] = doc["address"]
            return redirect(url_for("index"))

        failed_count = (attempt_row["failed_count"] if attempt_row else 0) + 1
        if failed_count >= MAX_LOGIN_ATTEMPTS:
            locked_until = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()
            db.execute(
                "INSERT INTO login_attempts (ip, failed_count, locked_until) VALUES (?, ?, ?) "
                "ON CONFLICT(ip) DO UPDATE SET failed_count = ?, locked_until = ?",
                (ip, failed_count, locked_until, failed_count, locked_until),
            )
            db.commit()
            error = f"Too many attempts. Try again in {LOCKOUT_MINUTES} minute(s)."
        else:
            db.execute(
                "INSERT INTO login_attempts (ip, failed_count, locked_until) VALUES (?, ?, NULL) "
                "ON CONFLICT(ip) DO UPDATE SET failed_count = ?, locked_until = NULL",
                (ip, failed_count, failed_count),
            )
            db.commit()
            remaining = MAX_LOGIN_ATTEMPTS - failed_count
            error = f"PIN not recognized. {remaining} attempt(s) remaining before lockout."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        doctor_name=session.get("doctor_name"),
        doctor_clinic=session.get("doctor_clinic"),
        doctor_fax=session.get("doctor_fax"),
    )
@app.route("/api/submit", methods=["POST"])
@login_required
def submit_order():
    data = request.get_json(force=True) or {}
    clinical_info = (data.get("clinical_info") or "").strip()
    preferred_clinic = (data.get("preferred_clinic") or "").strip()
    studies = data.get("studies") or []
    fax_override = (data.get("fax_number") or "").strip()

    if not clinical_info:
        return jsonify({"error": "Clinical information is required"}), 400
    if not studies:
        return jsonify({"error": "At least one study is required"}), 400

    fax_number = fax_override or session.get("doctor_fax") or ""
    if fax_number and not re.match(r"^\d{10}$", re.sub(r"\D", "", fax_number)):
        return jsonify({"error": "Fax number should be 10 digits"}), 400

    studies_text = "; ".join(studies)
    all_codes = []
    for study_line in studies:
        for code_info in resolve_billing_codes(study_line):
            code_str = f"{code_info['code']} ({code_info['description']})"
            if code_str not in all_codes:
                all_codes.append(code_str)
    billing_codes_text = "; ".join(all_codes)

    db = get_db()
    cur = db.execute(
        """INSERT INTO orders
           (doctor_id, referring_doc, clinic_name, doctor_address, fax_number,
            clinical_info, preferred_clinic, studies, billing_codes, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
        (
            session.get("doctor_id"),
            session.get("doctor_name"),
            session.get("doctor_clinic"),
            session.get("doctor_address"),
            fax_number,
            clinical_info,
            preferred_clinic,
            studies_text,
            billing_codes_text,
            datetime.utcnow().isoformat(),
        ),
    )
    db.commit()
    return jsonify({"status": "ok", "order_id": cur.lastrowid})


@app.route("/order/<int:order_id>/complete", methods=["GET", "POST"])
@login_required
def complete_order(order_id):
    db = get_db()
    order = db.execute(
        "SELECT * FROM orders WHERE id = ? AND doctor_id = ?",
        (order_id, session.get("doctor_id")),
    ).fetchone()
    if not order:
        return "Requisition not found.", 404

    error = None
    if request.method == "POST":
        patient_name = (request.form.get("patient_name") or "").strip()
        patient_dob = (request.form.get("patient_dob") or "").strip()
        patient_sex = (request.form.get("patient_sex") or "").strip()
        patient_phin = re.sub(r"\D", "", request.form.get("patient_phin") or "")
        patient_phone = re.sub(r"\D", "", request.form.get("patient_phone") or "")
        sms_consent = 1 if request.form.get("sms_consent") else 0

        if not patient_name:
            error = "Patient name is required."
        elif not patient_dob or not re.match(r"^\d{4}-\d{2}-\d{2}$", patient_dob):
            error = "Date of birth is required, format YYYY-MM-DD."
        elif patient_phin and not re.match(r"^\d{9}$", patient_phin):
            error = "PHIN should be 9 digits."
        elif patient_phone and not re.match(r"^\d{10}$", patient_phone):
            error = "Cell phone should be 10 digits."
        else:
            db.execute(
                """UPDATE orders SET patient_name = ?, patient_dob = ?, patient_sex = ?,
                   patient_phin = ?, patient_phone = ?, sms_consent = ?, status = 'issued' WHERE id = ?""",
                (patient_name, patient_dob, patient_sex, patient_phin, patient_phone, sms_consent, order_id),
            )
            db.commit()
            return redirect(url_for("print_order", order_id=order_id))

    return render_template("order_complete.html", order=order, error=error)


@app.route("/api/pending_count")
@login_required
def pending_count():
    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) as c FROM orders WHERE doctor_id = ? AND status = 'draft'",
        (session.get("doctor_id"),),
    ).fetchone()["c"]
    return jsonify({"count": count})


@app.route("/pending")
@login_required
def pending_orders():
    db = get_db()
    drafts = db.execute(
        "SELECT * FROM orders WHERE doctor_id = ? AND status = 'draft' ORDER BY created_at ASC",
        (session.get("doctor_id"),),
    ).fetchall()
    return render_template("pending.html", drafts=drafts)


@app.route("/order/<int:order_id>/print")
@login_required
def print_order(order_id):
    db = get_db()
    order = db.execute(
        "SELECT * FROM orders WHERE id = ? AND doctor_id = ?",
        (order_id, session.get("doctor_id")),
    ).fetchone()
    if not order:
        return "Requisition not found.", 404
    studies = [s.strip() for s in order["studies"].split(";") if s.strip()]
    return render_template("order_print.html", order=order, studies=studies)
@app.route("/admin/hours", methods=["GET", "POST"])
def admin_hours():
    db = get_db()
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    if request.method == "POST":
        for clinic in ["Legacy X-ray", "Park City X-ray"]:
            for day in range(7):
                field_prefix = f"{clinic}_{day}"
                is_open = 1 if request.form.get(f"{field_prefix}_open") else 0
                open_time = (request.form.get(f"{field_prefix}_start") or "08:00").strip()
                close_time = (request.form.get(f"{field_prefix}_end") or "17:00").strip()
                db.execute(
                    "UPDATE clinic_hours SET is_open = ?, open_time = ?, close_time = ? "
                    "WHERE clinic_name = ? AND day_of_week = ?",
                    (is_open, open_time, close_time, clinic, day),
                )
        db.commit()
        return redirect(url_for("admin_hours"))

    rows = db.execute("SELECT * FROM clinic_hours ORDER BY clinic_name, day_of_week").fetchall()
    hours_by_clinic = {}
    for row in rows:
        hours_by_clinic.setdefault(row["clinic_name"], []).append(row)

    return render_template("hours.html", hours_by_clinic=hours_by_clinic, day_names=day_names)


@app.route("/admin/dashboard")
def dashboard():
    db = get_db()
    cutoff = (datetime.utcnow() - timedelta(days=FOLLOWUP_DAYS)).isoformat()
    urgent_cutoff = (datetime.utcnow() - timedelta(days=URGENT_FOLLOWUP_DAYS)).isoformat()

    status_counts = {row["status"]: row["c"] for row in db.execute(
        "SELECT status, COUNT(*) as c FROM orders GROUP BY status"
    ).fetchall()}

    urgent_count = db.execute(
        "SELECT COUNT(*) as c FROM orders WHERE status = 'issued' AND created_at < ?", (urgent_cutoff,)
    ).fetchone()["c"]

    needs_followup_count = db.execute(
        "SELECT COUNT(*) as c FROM orders WHERE status = 'issued' AND created_at < ? AND created_at >= ?",
        (cutoff, urgent_cutoff),
    ).fetchone()["c"]

    flagged_orders = db.execute(
        "SELECT * FROM orders WHERE status = 'issued' AND created_at < ? ORDER BY created_at ASC",
        (cutoff,),
    ).fetchall()

    top_referrers = db.execute(
        "SELECT referring_doc, clinic_name, COUNT(*) as c FROM orders GROUP BY referring_doc, clinic_name ORDER BY c DESC LIMIT 8"
    ).fetchall()

    daily_volume = db.execute(
        """SELECT substr(created_at, 1, 10) as day, COUNT(*) as c
           FROM orders
           WHERE created_at >= ?
           GROUP BY day ORDER BY day ASC""",
        ((datetime.utcnow() - timedelta(days=14)).isoformat(),),
    ).fetchall()
    max_daily = max([row["c"] for row in daily_volume], default=1)

    total_orders = sum(status_counts.values())

    return render_template(
        "dashboard.html",
        status_counts=status_counts,
        needs_followup_count=needs_followup_count,
        urgent_count=urgent_count,
        urgent_cutoff=urgent_cutoff,
        flagged_orders=flagged_orders,
        top_referrers=top_referrers,
        daily_volume=daily_volume,
        max_daily=max_daily,
        total_orders=total_orders,
    )


@app.route("/admin")
def admin():
    db = get_db()
    rows = db.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    cutoff = (datetime.utcnow() - timedelta(days=FOLLOWUP_DAYS)).isoformat()
    urgent_cutoff = (datetime.utcnow() - timedelta(days=URGENT_FOLLOWUP_DAYS)).isoformat()
    error = request.args.get("error")
    return render_template("admin.html", orders=rows, cutoff=cutoff, urgent_cutoff=urgent_cutoff, error=error)
@app.route("/admin/orders/<int:order_id>/suggest", methods=["POST"])
def suggest_message(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        return "Order not found", 404
    clinic = order["preferred_clinic"] or "Legacy X-ray"
    message = compute_arrival_message(db, clinic)
    db.execute("UPDATE orders SET suggested_time = ? WHERE id = ?", (message, order_id))
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/orders/<int:order_id>/notify", methods=["POST"])
def notify_patient(order_id):
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        return "Order not found", 404
    message_text = (request.form.get("suggested_time") or "").strip()
    if not message_text:
        return redirect(url_for("admin"))
    if not order["patient_phone"] or not order["sms_consent"]:
        db.execute("UPDATE orders SET suggested_time = ? WHERE id = ?", (message_text, order_id))
        db.commit()
        return redirect(url_for("admin"))

    try:
        send_arrival_sms(order["patient_phone"], message_text)
        db.execute(
            "UPDATE orders SET suggested_time = ?, sms_sent_at = ? WHERE id = ?",
            (message_text, datetime.utcnow().isoformat(), order_id),
        )
    except Exception:
        db.execute("UPDATE orders SET suggested_time = ? WHERE id = ?", (message_text, order_id))
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
def update_order_status(order_id):
    status = (request.form.get("status") or "").strip()
    note = (request.form.get("followup_note") or "").strip()
    clinic_notified_at = (request.form.get("clinic_notified_at") or "").strip()
    valid_statuses = {"issued", "completed", "went_elsewhere", "not_interested", "unable_to_contact"}
    closure_statuses = {"went_elsewhere", "not_interested", "unable_to_contact"}

    if status not in valid_statuses:
        return "Invalid status", 400

    if status in closure_statuses:
        if not note:
            return redirect(url_for("admin", error=f"order_{order_id}_note_required"))
        if not clinic_notified_at:
            clinic_notified_at = datetime.utcnow().isoformat(timespec="minutes")

    db = get_db()
    db.execute(
        "UPDATE orders SET status = ?, followup_note = ?, followup_at = ?, clinic_notified_at = ? WHERE id = ?",
        (status, note, datetime.utcnow().isoformat(), clinic_notified_at or None, order_id),
    )
    db.commit()
    return redirect(url_for("admin"))


@app.route("/admin/doctors", methods=["GET", "POST"])
def admin_doctors():
    db = get_db()
    error = None
    just_added = None
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        clinic_name = (request.form.get("clinic_name") or "").strip()
        address = (request.form.get("address") or "").strip()
        fax_number = re.sub(r"\D", "", request.form.get("fax_number") or "")
        pin = (request.form.get("pin") or "").strip()
        if not name or not pin:
            error = "Name and PIN are required."
        elif not re.match(r"^\d{4}$", pin):
            error = "PIN must be exactly 4 digits."
        elif fax_number and not re.match(r"^\d{10}$", fax_number):
            error = "Fax number should be 10 digits."
        else:
            try:
                db.execute(
                    "INSERT INTO doctors (name, clinic_name, address, fax_number, pin_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, clinic_name, address, fax_number, generate_password_hash(pin), datetime.utcnow().isoformat()),
                )
                db.commit()
                just_added = {"name": name, "pin": pin}
            except sqlite3.IntegrityError:
                error = "Could not add doctor. Try again."
    doctors = db.execute("SELECT * FROM doctors ORDER BY name").fetchall()
    return render_template("admin_doctors.html", doctors=doctors, error=error, just_added=just_added)


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5050, debug=True)
else:
    init_db()