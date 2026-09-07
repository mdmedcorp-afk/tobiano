import sqlite3
import os
import re
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, g, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("XRAY_PILOT_SECRET", "change-me-before-real-use")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "orders.db")
FOLLOWUP_DAYS = 3


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
    db.commit()
    db.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("doctor_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pin = (request.form.get("pin") or "").strip()
        db = get_db()
        doc = None
        for row in db.execute("SELECT * FROM doctors").fetchall():
            if check_password_hash(row["pin_hash"], pin):
                doc = row
                break
        if doc:
            session["doctor_id"] = doc["id"]
            session["doctor_name"] = doc["name"]
            session["doctor_clinic"] = doc["clinic_name"]
            session["doctor_fax"] = doc["fax_number"]
            session["doctor_address"] = doc["address"]
            return redirect(url_for("index"))
        error = "PIN not recognized. Check with your clinic or contact us."
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
    patient_ref = (data.get("patient_ref") or "").strip()
    patient_name = (data.get("patient_name") or "").strip()
    patient_dob = (data.get("patient_dob") or "").strip()
    patient_sex = (data.get("patient_sex") or "").strip()
    patient_phin = re.sub(r"\D", "", data.get("patient_phin") or "")
    patient_phone = re.sub(r"\D", "", data.get("patient_phone") or "")
    studies = data.get("studies") or []
    fax_override = (data.get("fax_number") or "").strip()

    if not patient_name:
        return jsonify({"error": "Patient name is required"}), 400
    if not patient_dob or not re.match(r"^\d{4}-\d{2}-\d{2}$", patient_dob):
        return jsonify({"error": "Date of birth is required, format YYYY-MM-DD"}), 400
    if patient_phin and not re.match(r"^\d{9}$", patient_phin):
        return jsonify({"error": "PHIN should be 9 digits"}), 400
    if patient_phone and not re.match(r"^\d{10}$", patient_phone):
        return jsonify({"error": "Cell phone should be 10 digits"}), 400
    if not studies:
        return jsonify({"error": "At least one study is required"}), 400

    fax_number = fax_override or session.get("doctor_fax") or ""
    if fax_number and not re.match(r"^\d{10}$", re.sub(r"\D", "", fax_number)):
        return jsonify({"error": "Fax number should be 10 digits"}), 400

    studies_text = "; ".join(studies)
    db = get_db()
    cur = db.execute(
        """INSERT INTO orders
           (doctor_id, referring_doc, clinic_name, doctor_address, fax_number, patient_ref,
            patient_name, patient_dob, patient_sex, patient_phin, patient_phone,
            studies, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session.get("doctor_id"),
            session.get("doctor_name"),
            session.get("doctor_clinic"),
            session.get("doctor_address"),
            fax_number,
            patient_ref,
            patient_name,
            patient_dob,
            patient_sex,
            patient_phin,
            patient_phone,
            studies_text,
            datetime.utcnow().isoformat(),
        ),
    )
    db.commit()
    return jsonify({"status": "ok", "order_id": cur.lastrowid})


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


@app.route("/admin")
def admin():
    db = get_db()
    rows = db.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    cutoff = (datetime.utcnow() - timedelta(days=FOLLOWUP_DAYS)).isoformat()
    return render_template("admin.html", orders=rows, cutoff=cutoff)


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
def update_order_status(order_id):
    status = (request.form.get("status") or "").strip()
    note = (request.form.get("followup_note") or "").strip()
    valid_statuses = {"issued", "completed", "went_elsewhere", "cancelled"}
    if status not in valid_statuses:
        return "Invalid status", 400
    db = get_db()
    db.execute(
        "UPDATE orders SET status = ?, followup_note = ?, followup_at = ? WHERE id = ?",
        (status, note, datetime.utcnow().isoformat(), order_id),
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