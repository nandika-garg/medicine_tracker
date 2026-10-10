from flask import Flask, render_template, request, redirect, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import sqlite3

app = Flask(__name__)
app.secret_key = "change-this-to-any-random-string"  # needed for sessions/flash messages
app.config["DATABASE"] = "tracker.db"

MISSED_DOSE_GRACE_MINUTES = 30  # how long to wait before marking a dose "missed"

def get_db():
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    return conn


def login_required(view):
    """Simple decorator: redirect to /login if no one is logged in."""
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/login")
        return view(*args, **kwargs)
    wrapped.__name__ = view.__name__
    return wrapped


# ---------------- AUTH ----------------

@app.route("/")
def home():
    return redirect("/schedule")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        patient_name = request.form["patient_name"]
        email = request.form["email"]
        password = request.form["password"]

        conn = get_db()
        existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            conn.close()
            flash("An account with that email already exists.")
            return redirect("/register")

        conn.execute(
            "INSERT INTO users (patient_name, email, password_hash) VALUES (?, ?, ?)",
            (patient_name, email, generate_password_hash(password, method="pbkdf2:sha256"))
        )
        conn.commit()
        conn.close()
        flash("Account created. Please log in.")
        return redirect("/login")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["patient_name"] = user["patient_name"]
            return redirect("/schedule")

        flash("Incorrect email or password.")
        return redirect("/login")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ---------------- MEDICINES ----------------

@app.route("/add-medicine", methods=["GET", "POST"])
@login_required
def add_medicine():
    if request.method == "POST":
        name = request.form["name"]
        dosage = request.form["dosage"]
        frequency = request.form["frequency"]
        icon = request.form.get("icon", "💊")

        dose_times = request.form.getlist("dose_times")
        dose_times = ",".join(dose_times)

        stock = request.form["stock"]
        threshold = request.form["threshold"]

        conn = get_db()
        conn.execute(
            """
            INSERT INTO medicines
            (user_id, name, dosage, frequency, stock, threshold, dose_times, icon)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session["user_id"], name, dosage, frequency, stock, threshold, dose_times, icon)
        )
        conn.commit()
        conn.close()

        return redirect("/schedule")

    return render_template("add_medicine.html")


@app.route("/take-dose/<int:medicine_id>", methods=["POST"])
@login_required
def take_dose(medicine_id):
    conn = get_db()
    medicine = conn.execute(
        "SELECT * FROM medicines WHERE id = ? AND user_id = ?",
        (medicine_id, session["user_id"])
    ).fetchone()

    if medicine and medicine["stock"] > 0:
        conn.execute("UPDATE medicines SET stock = stock - 1 WHERE id = ?", (medicine_id,))

        now = datetime.now()
        conn.execute(
            """
            INSERT INTO dose_logs (medicine_id, scheduled_time, taken_at, status)
            VALUES (?, ?, datetime('now'), 'taken')
            """,
            (medicine_id, now.strftime("%H:%M"))
        )
        conn.commit()

    conn.close()
    return redirect("/schedule")


@app.route("/refill/<int:medicine_id>", methods=["POST"])
@login_required
def refill(medicine_id):
    amount = int(request.form["refill_amount"])

    conn = get_db()
    medicine = conn.execute(
        "SELECT * FROM medicines WHERE id = ? AND user_id = ?",
        (medicine_id, session["user_id"])
    ).fetchone()

    if medicine and amount > 0:
        conn.execute(
            "UPDATE medicines SET stock = stock + ? WHERE id = ?",
            (amount, medicine_id)
        )
        conn.commit()

    conn.close()
    return redirect("/schedule")


@app.route("/delete-medicine/<int:medicine_id>", methods=["POST"])
@login_required
def delete_medicine(medicine_id):
    conn = get_db()
    conn.execute(
        "UPDATE medicines SET active = 0 WHERE id = ? AND user_id = ?",
        (medicine_id, session["user_id"])
    )
    conn.commit()
    conn.close()
    return redirect("/schedule")


# ---------------- SCHEDULE (with missed-dose detection) ----------------

@app.route("/schedule")
@login_required
def schedule():
    conn = get_db()
    medicines = conn.execute(
    "SELECT * FROM medicines WHERE user_id = ? AND active = 1", (session["user_id"],)
).fetchall()

    missed_today = []
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")

    for med in medicines:
        if not med["dose_times"]:
            continue

        for time_str in med["dose_times"].split(","):
            time_str = time_str.strip()
            if not time_str:
                continue

            try:
                scheduled_dt = datetime.strptime(f"{today_str} {time_str}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue

            # only check times that are already in the past, with grace period
            if now < scheduled_dt + timedelta(minutes=MISSED_DOSE_GRACE_MINUTES):
                continue

            # has this exact scheduled dose already been logged today (taken or missed)?
            already_logged = conn.execute(
                """
                SELECT id FROM dose_logs
                WHERE medicine_id = ? AND scheduled_time = ?
                AND date(taken_at) = ?
                """,
                (med["id"], time_str, today_str)
            ).fetchone()

            if not already_logged:
                conn.execute(
                    """
                    INSERT INTO dose_logs (medicine_id, scheduled_time, taken_at, status)
                    VALUES (?, ?, datetime('now'), 'missed')
                    """,
                    (med["id"], time_str)
                )
                missed_today.append(f"{med['icon']} {med['name']} ({time_str})")

    conn.commit()
    conn.close()

    return render_template(
        "schedule.html",
        medicines=medicines,
        missed_today=missed_today,
        patient_name=session.get("patient_name")
    )


# ---------------- HISTORY ----------------

@app.route("/history")
@login_required
def history():
    conn = get_db()
    logs = conn.execute(
        """
        SELECT dose_logs.taken_at, dose_logs.status, dose_logs.scheduled_time,
               medicines.name, medicines.dosage, medicines.icon
        FROM dose_logs
        JOIN medicines ON dose_logs.medicine_id = medicines.id
        WHERE medicines.user_id = ?
        ORDER BY dose_logs.taken_at DESC
        """,
        (session["user_id"],)
    ).fetchall()
    conn.close()

    return render_template("history.html", logs=logs)


if __name__ == "__main__":
    app.run(debug=True, port=5001)