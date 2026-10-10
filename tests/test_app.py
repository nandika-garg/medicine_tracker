import sqlite3
from datetime import datetime, timedelta

import pytest

from app import app as flask_app
from database import init_db


# ---------- fixtures & helpers ----------

@pytest.fixture
def client(tmp_path):
    """Each test gets a brand-new, empty database in a temp folder,
    so tests never touch your real tracker.db."""
    db_file = str(tmp_path / "test.db")
    flask_app.config["DATABASE"] = db_file
    flask_app.config["TESTING"] = True
    init_db(db_file)
    with flask_app.test_client() as client:
        yield client


def query(sql, params=()):
    """Run a SELECT directly against the test database."""
    conn = sqlite3.connect(flask_app.config["DATABASE"])
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def register_and_login(client, email="a@test.com", password="pass123"):
    client.post("/register", data={
        "patient_name": "Grandma", "email": email, "password": password
    })
    return client.post("/login", data={"email": email, "password": password})


def add_medicine(client, name="Paracetamol", stock=5, threshold=2, dose_times=("08:00",)):
    client.post("/add-medicine", data={
        "name": name,
        "dosage": "500mg",
        "frequency": len(dose_times),
        "icon": "💊",
        "dose_times": list(dose_times),
        "stock": stock,
        "threshold": threshold,
    })


def stock_of(med_id=1):
    return query("SELECT stock FROM medicines WHERE id = ?", (med_id,))[0][0]


# ---------- authentication (FR1, FR2, NFR3) ----------

def test_register_and_login_succeeds(client):
    response = register_and_login(client)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/schedule")


def test_wrong_password_is_rejected(client):
    client.post("/register", data={
        "patient_name": "Grandma", "email": "a@test.com", "password": "right"
    })
    response = client.post(
        "/login", data={"email": "a@test.com", "password": "wrong"},
        follow_redirects=True,
    )
    assert b"Incorrect email or password" in response.data


def test_schedule_requires_login(client):
    response = client.get("/schedule")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_password_is_stored_hashed(client):
    register_and_login(client, password="mysecret")
    stored = query("SELECT password_hash FROM users")[0][0]
    assert stored != "mysecret"


# ---------- dose taking & stock (FR7, FR8, NFR4) ----------

def test_take_dose_decrements_stock(client):
    register_and_login(client)
    add_medicine(client, stock=5)
    client.post("/take-dose/1")
    assert stock_of() == 4


def test_take_dose_creates_history_entry(client):
    register_and_login(client)
    add_medicine(client)
    client.post("/take-dose/1")
    logs = query("SELECT status FROM dose_logs")
    assert logs == [("taken",)]


def test_cannot_take_dose_when_out_of_stock(client):
    register_and_login(client)
    add_medicine(client, stock=0)
    client.post("/take-dose/1")
    assert stock_of() == 0                        # never goes negative
    assert query("SELECT * FROM dose_logs") == []  # and nothing is logged


# ---------- refill (FR13) ----------

def test_refill_increases_stock(client):
    register_and_login(client)
    add_medicine(client, stock=1)
    client.post("/refill/1", data={"refill_amount": 10})
    assert stock_of() == 11


def test_refill_with_zero_is_ignored(client):
    register_and_login(client)
    add_medicine(client, stock=3)
    client.post("/refill/1", data={"refill_amount": 0})
    assert stock_of() == 3


# ---------- low stock (FR9) ----------

def test_low_stock_badge_appears_at_threshold(client):
    register_and_login(client)
    add_medicine(client, stock=2, threshold=2)
    response = client.get("/schedule")
    assert b"Low Stock" in response.data


def test_no_low_stock_badge_when_plenty(client):
    register_and_login(client)
    add_medicine(client, stock=50, threshold=2)
    response = client.get("/schedule")
    assert b"Low Stock" not in response.data


# ---------- privacy between users (NFR3) ----------

def test_user_cannot_take_another_users_medicine(client):
    register_and_login(client, email="a@test.com")
    add_medicine(client, stock=5)
    client.get("/logout")

    register_and_login(client, email="b@test.com")
    client.post("/take-dose/1")                   # medicine 1 belongs to user A
    assert stock_of() == 5                        # unchanged


def test_user_cannot_see_another_users_medicines(client):
    register_and_login(client, email="a@test.com")
    add_medicine(client, name="SecretPill")
    client.get("/logout")

    register_and_login(client, email="b@test.com")
    response = client.get("/schedule")
    assert b"SecretPill" not in response.data


# ---------- soft delete (NFR9) ----------

def test_deleted_medicine_leaves_schedule_but_keeps_history(client):
    register_and_login(client)
    add_medicine(client, name="OldPill")
    client.post("/take-dose/1")
    client.post("/delete-medicine/1")

    schedule = client.get("/schedule")
    assert b"OldPill" not in schedule.data        # gone from schedule

    history = client.get("/history")
    assert b"OldPill" in history.data             # but history survives


# ---------- missed doses (FR10, FR12) ----------

def test_overdue_dose_is_logged_as_missed_once(client):
    if datetime.now().hour == 0:
        pytest.skip("'1 hour ago' would be yesterday during 00:00-00:59")

    register_and_login(client)
    one_hour_ago = (datetime.now() - timedelta(hours=1)).strftime("%H:%M")
    add_medicine(client, dose_times=(one_hour_ago,))

    client.get("/schedule")
    client.get("/schedule")                       # loading twice must not duplicate

    logs = query("SELECT status FROM dose_logs")
    assert logs == [("missed",)]


def test_future_dose_is_not_marked_missed(client):
    if datetime.now().hour == 23:
        pytest.skip("'1 hour from now' would be tomorrow during 23:00-23:59")

    register_and_login(client)