"""Small, dependency-free web server and SQLite-backed request API."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import webbrowser
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlparse
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
DATA_DIR = ROOT / "data"
DATABASE = DATA_DIR / "dr_mehta.sqlite3"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
try:
    CLINIC_TIMEZONE = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    CLINIC_TIMEZONE = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")
MAX_BODY = 16_384
PHONE_PATTERN = re.compile(r"^(?:\+91[\s-]?)?[6-9]\d{9}$")
SERVICES = (
    "General appointment enquiry",
    "Follow-up appointment enquiry",
    "Existing patient enquiry",
)
APPOINTMENT_TIMES = (
    "09:00",
    "09:30",
    "10:00",
    "10:30",
    "11:00",
    "11:30",
    "12:00",
    "12:30",
    "13:00",
    "13:30",
    "14:00",
    "14:30",
    "15:00",
    "15:30",
    "16:00",
    "16:30",
    "17:00",
    "17:30",
    "18:00",
)
PASSWORD_ITERATIONS = 310_000
ADMIN_PASSWORD_SALT = secrets.token_bytes(16)
ADMIN_LOGIN_WINDOW_SECONDS = 15 * 60
ADMIN_LOGIN_MAX_ATTEMPTS = 5
ADMIN_LOGIN_FAILURES: dict[str, list[float]] = {}
ADMIN_LOGIN_LOCK = threading.Lock()
SESSIONS: dict[str, float] = {}
SESSIONS_LOCK = threading.Lock()
PATIENT_SESSIONS: dict[str, tuple[float, int]] = {}
PATIENT_SESSIONS_LOCK = threading.Lock()


def connect_database() -> sqlite3.Connection:
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextmanager
def database() -> Iterator[sqlite3.Connection]:
    with closing(connect_database()) as connection:
        with connection:
            yield connection


def initialize_database() -> None:
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE,
                name TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'patient' CHECK (role IN ('patient', 'admin')),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                service TEXT NOT NULL,
                appointment_date TEXT NOT NULL,
                appointment_time TEXT NOT NULL,
                message TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                payment_method TEXT NOT NULL DEFAULT 'Cash at clinic',
                payment_status TEXT NOT NULL DEFAULT 'not collected',
                internal_notes TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'confirmed', 'completed', 'cancelled')),
                patient_id INTEGER REFERENCES patients(id),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS contact_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        patient_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(patients)").fetchall()
        }
        if "username" not in patient_columns:
            connection.execute("ALTER TABLE patients ADD COLUMN username TEXT")
        if "role" not in patient_columns:
            connection.execute(
                "ALTER TABLE patients ADD COLUMN role TEXT NOT NULL DEFAULT 'patient' CHECK (role IN ('patient', 'admin'))"
            )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_patients_username ON patients(username) WHERE username IS NOT NULL"
        )
        appointment_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(appointments)").fetchall()
        }
        if "patient_id" not in appointment_columns:
            connection.execute(
                "ALTER TABLE appointments ADD COLUMN patient_id INTEGER REFERENCES patients(id)"
            )
        if "payment_method" not in appointment_columns:
            connection.execute(
                "ALTER TABLE appointments ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'Cash at clinic'"
            )
        if "email" not in appointment_columns:
            connection.execute(
                "ALTER TABLE appointments ADD COLUMN email TEXT NOT NULL DEFAULT ''"
            )
        if "payment_status" not in appointment_columns:
            connection.execute(
                "ALTER TABLE appointments ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'not collected'"
            )
        if "internal_notes" not in appointment_columns:
            connection.execute(
                "ALTER TABLE appointments ADD COLUMN internal_notes TEXT NOT NULL DEFAULT ''"
            )
        if "idempotency_key" not in appointment_columns:
            connection.execute("ALTER TABLE appointments ADD COLUMN idempotency_key TEXT")
        appointment_schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'appointments'"
        ).fetchone()["sql"]
        if "'completed'" not in appointment_schema:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """CREATE TABLE appointments_migrated (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        phone TEXT NOT NULL,
                        service TEXT NOT NULL,
                        appointment_date TEXT NOT NULL,
                        appointment_time TEXT NOT NULL,
                        message TEXT NOT NULL DEFAULT '',
                        email TEXT NOT NULL DEFAULT '',
                        payment_method TEXT NOT NULL DEFAULT 'Cash at clinic',
                        payment_status TEXT NOT NULL DEFAULT 'not collected',
                        internal_notes TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT,
                        status TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'confirmed', 'completed', 'cancelled')),
                        patient_id INTEGER REFERENCES patients(id),
                        created_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    """INSERT INTO appointments_migrated
                       (id, name, phone, service, appointment_date, appointment_time,
                        message, email, payment_method, payment_status, internal_notes,
                        idempotency_key, status, patient_id, created_at)
                       SELECT id, name, phone, service, appointment_date, appointment_time,
                              message, email, payment_method, payment_status, internal_notes,
                              idempotency_key, status, patient_id, created_at
                       FROM appointments"""
                )
                connection.execute("DROP TABLE appointments")
                connection.execute(
                    "ALTER TABLE appointments_migrated RENAME TO appointments"
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS appointments_patient_id_idx ON appointments(patient_id)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS appointments_idempotency_key_idx "
            "ON appointments(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        seed_admin_account()


def seed_admin_account() -> None:
    username = "admin"
    password = "7300641883"
    admin_phone = "1234567890"
    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    with database() as connection:
        existing = connection.execute(
            "SELECT id FROM patients WHERE username = ?",
            (username,),
        ).fetchone()
        if existing is not None:
            connection.execute(
                "UPDATE patients SET name = COALESCE(name, 'Administrator'), phone = ?, password_salt = ?, password_hash = ?, role = 'admin' WHERE id = ?",
                (admin_phone, salt.hex(), password_hash.hex(), existing["id"]),
            )
            return
        connection.execute(
            """
            INSERT INTO patients (id, username, name, phone, password_salt, password_hash, role, created_at)
            VALUES (0, ?, ?, ?, ?, ?, 'admin', ?)
            """,
            (
                username,
                "Administrator",
                admin_phone,
                salt.hex(),
                password_hash.hex(),
                now_utc(),
            ),
        )


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: object, field: str, limit: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Enter a valid {field.lower()}.")
    result = " ".join(value.split())
    if required and not result:
        raise ValueError(f"{field} is required.")
    if len(result) > limit:
        raise ValueError(f"{field} must be {limit} characters or fewer.")
    return result


def validate_phone(value: object) -> str:
    phone = clean_text(value, "Mobile number", 16)
    if not PHONE_PATTERN.fullmatch(phone):
        raise ValueError("Enter a valid 10-digit Indian mobile number.")
    normalized = phone.replace(" ", "").replace("-", "")
    return normalized[3:] if normalized.startswith("+91") else normalized


def read_json(handler: SimpleHTTPRequestHandler) -> dict[str, object]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise ValueError("Invalid request size.") from exc
    if length < 1 or length > MAX_BODY:
        raise ValueError("Request must contain valid data and be under 16 KB.")
    try:
        payload = json.loads(handler.rfile.read(length))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Request must contain valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Request must contain a JSON object.")
    return payload


def admin_login_retry_after(client_ip: str) -> int:
    now = time.monotonic()
    with ADMIN_LOGIN_LOCK:
        for address, attempts in tuple(ADMIN_LOGIN_FAILURES.items()):
            recent_attempts = [
                attempt
                for attempt in attempts
                if now - attempt < ADMIN_LOGIN_WINDOW_SECONDS
            ]
            if recent_attempts:
                ADMIN_LOGIN_FAILURES[address] = recent_attempts
            else:
                ADMIN_LOGIN_FAILURES.pop(address, None)
        recent = [
            attempt
            for attempt in ADMIN_LOGIN_FAILURES.get(client_ip, [])
            if now - attempt < ADMIN_LOGIN_WINDOW_SECONDS
        ]
        if recent:
            ADMIN_LOGIN_FAILURES[client_ip] = recent
        else:
            ADMIN_LOGIN_FAILURES.pop(client_ip, None)
        if len(recent) < ADMIN_LOGIN_MAX_ATTEMPTS:
            return 0
        return max(1, int(ADMIN_LOGIN_WINDOW_SECONDS - (now - recent[0]) + 0.999))


def record_admin_login_failure(client_ip: str) -> None:
    now = time.monotonic()
    with ADMIN_LOGIN_LOCK:
        recent = [
            attempt
            for attempt in ADMIN_LOGIN_FAILURES.get(client_ip, [])
            if now - attempt < ADMIN_LOGIN_WINDOW_SECONDS
        ]
        recent.append(now)
        ADMIN_LOGIN_FAILURES[client_ip] = recent


def clear_admin_login_failures(client_ip: str) -> None:
    with ADMIN_LOGIN_LOCK:
        ADMIN_LOGIN_FAILURES.pop(client_ip, None)


class HospitalHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(SITE), **kwargs)

    def log_message(self, format_string: str, *args: object) -> None:
        # Admin search terms can contain patient names or phone numbers.
        if args and self.command == "GET" and urlparse(self.path).path == "/api/admin/appointments":
            safe_request_line = f"{self.command} /api/admin/appointments {self.request_version}"
            args = (safe_request_line, *args[1:])
        super().log_message(format_string, *args)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https://images.unsplash.com "
            "data:; style-src 'self' 'unsafe-inline'; script-src 'self' "
            "'unsafe-inline'; connect-src 'self'; frame-src "
            "https://www.google.com; base-uri 'self'; form-action 'self'",
        )
        super().end_headers()

    def send_json(
        self,
        status: HTTPStatus,
        data: dict[str, object],
        headers: dict[str, str] | None = None,
    ) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            try:
                with database() as connection:
                    connection.execute("SELECT 1").fetchone()
                self.send_json(HTTPStatus.OK, {"ok": True, "database": "connected"})
            except sqlite3.Error:
                self.send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"ok": False, "message": "The database is unavailable."},
                )
            return
        if path == "/api/departments":
            self.send_json(HTTPStatus.OK, {"services": SERVICES})
            return
        if path == "/api/patient/me":
            patient = self.current_patient()
            if patient is None:
                self.send_json(HTTPStatus.OK, {"authenticated": False})
                return
            self.send_json(
                HTTPStatus.OK,
                {
                    "authenticated": True,
                    "patient": {"name": patient["name"], "phone": patient["phone"]},
                },
            )
            return
        if path == "/api/admin/session":
            self.send_json(HTTPStatus.OK, {"authenticated": self.is_admin()})
            return
        if path == "/api/admin/appointments":
            if not self.is_admin():
                self.send_json(HTTPStatus.UNAUTHORIZED, {"message": "Sign in to continue."})
                return
            self.list_appointments()
            return
        if path == "/admin":
            if self.is_admin():
                self.path = "/admin.html"
            else:
                patient = self.current_patient()
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/" if patient is not None else "/signin.html")
                self.end_headers()
                return
        elif path == "/signin":
            self.path = "/signin.html"
        elif path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self.same_origin():
            self.send_json(HTTPStatus.FORBIDDEN, {"message": "Cross-origin requests are not allowed."})
            return
        try:
            payload = read_json(self)
            if path == "/api/appointments":
                patient = self.current_patient()
                if patient is None:
                    self.send_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"message": "Please sign in to your patient account before requesting an appointment."},
                    )
                    return
                self.create_appointment(payload, patient)
            elif path == "/api/contact":
                self.create_contact_message(payload)
            elif path == "/api/patient/register":
                self.register_patient(payload)
            elif path == "/api/patient/login":
                self.login_patient(payload)
            elif path == "/api/patient/logout":
                self.logout_patient()
            elif path == "/api/admin/login":
                self.login(payload)
            elif path == "/api/admin/logout":
                self.logout()
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"message": "Endpoint not found."})
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"message": str(exc)})
        except sqlite3.Error:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"message": "We couldn't save your request. Please call Dr. Arvind."},
            )

    def do_PATCH(self) -> None:
        if not self.same_origin():
            self.send_json(HTTPStatus.FORBIDDEN, {"message": "Cross-origin requests are not allowed."})
            return
        if urlparse(self.path).path != "/api/admin/appointments":
            self.send_json(HTTPStatus.NOT_FOUND, {"message": "Endpoint not found."})
            return
        if not self.is_admin():
            self.send_json(HTTPStatus.UNAUTHORIZED, {"message": "Sign in to continue."})
            return
        try:
            payload = read_json(self)
            appointment_id = payload.get("id")
            status = payload.get("status")
            if not isinstance(appointment_id, int) or isinstance(appointment_id, bool):
                raise ValueError("Choose a valid appointment.")
            if status is not None and status not in {
                "pending",
                "confirmed",
                "completed",
                "cancelled",
            }:
                raise ValueError("Choose a valid appointment status.")
            has_notes = "internal_notes" in payload
            internal_notes = (
                clean_text(payload.get("internal_notes"), "Internal notes", 2000, required=False)
                if has_notes
                else None
            )
            if status is None and not has_notes:
                raise ValueError("Choose an appointment update.")
            with database() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT appointment_date, appointment_time FROM appointments WHERE id = ?",
                    (appointment_id,),
                ).fetchone()
                if current is None:
                    self.send_json(HTTPStatus.NOT_FOUND, {"message": "Appointment not found."})
                    return
                if status == "confirmed":
                    requested_at = datetime.combine(
                        date.fromisoformat(current["appointment_date"]),
                        datetime.strptime(current["appointment_time"], "%H:%M").time(),
                        tzinfo=CLINIC_TIMEZONE,
                    )
                    if requested_at <= datetime.now(CLINIC_TIMEZONE):
                        self.send_json(
                            HTTPStatus.CONFLICT,
                            {"message": "This preferred appointment time has already passed."},
                        )
                        return
                    conflict = connection.execute(
                        """SELECT id FROM appointments
                           WHERE appointment_date = ? AND appointment_time = ?
                             AND status IN ('confirmed', 'completed') AND id != ? LIMIT 1""",
                        (
                            current["appointment_date"],
                            current["appointment_time"],
                            appointment_id,
                        ),
                    ).fetchone()
                    if conflict is not None:
                        self.send_json(
                            HTTPStatus.CONFLICT,
                            {"message": "Another appointment is already confirmed for this preferred time."},
                        )
                        return
                updates: list[str] = []
                values: list[object] = []
                if status is not None:
                    updates.append("status = ?")
                    values.append(status)
                if has_notes:
                    updates.append("internal_notes = ?")
                    values.append(internal_notes)
                values.append(appointment_id)
                cursor = connection.execute(
                    f"UPDATE appointments SET {', '.join(updates)} WHERE id = ?",
                    values,
                )
            if cursor.rowcount == 0:
                self.send_json(HTTPStatus.NOT_FOUND, {"message": "Appointment not found."})
                return
            self.send_json(HTTPStatus.OK, {"message": "Appointment details updated."})
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"message": str(exc)})
        except sqlite3.Error:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"message": "We couldn't update the appointment. Please try again."},
            )

    def register_patient(self, payload: dict[str, object]) -> None:
        name = clean_text(payload.get("name"), "Full name", 100)
        phone = validate_phone(payload.get("phone"))
        password = payload.get("password")
        if not isinstance(password, str) or len(password) < 12 or len(password) > 128:
            raise ValueError("Choose a password between 12 and 128 characters.")

        salt = secrets.token_bytes(16)
        password_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
        )
        try:
            with database() as connection:
                cursor = connection.execute(
                    """INSERT INTO patients (name, phone, password_salt, password_hash, role, created_at)
                       VALUES (?, ?, ?, ?, 'patient', ?)""",
                    (
                        name,
                        phone,
                        salt.hex(),
                        password_hash.hex(),
                        now_utc(),
                    ),
                )
        except sqlite3.IntegrityError:
            self.send_json(
                HTTPStatus.CONFLICT,
                {"message": "An account already uses this mobile number. Please sign in instead."},
            )
            return
        self.start_patient_session(cursor.lastrowid)
        self.send_patient_json(
            HTTPStatus.CREATED,
            {"authenticated": True, "message": "Your patient account has been created."},
        )

    def login_patient(self, payload: dict[str, object]) -> None:
        username_value = payload.get("username")
        phone_value = payload.get("phone")
        identifier = username_value if isinstance(username_value, str) and username_value.strip() else phone_value
        identifier = identifier.strip() if isinstance(identifier, str) else identifier
        password = payload.get("password")
        if not isinstance(password, str) or not password or len(password) > 128:
            raise ValueError("Enter your username or mobile number and password.")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("Enter your username or mobile number and password.")
        try:
            phone = validate_phone(identifier)
        except ValueError:
            phone = None
        with database() as connection:
            patient = connection.execute(
                """
                SELECT id, username, name, phone, password_salt, password_hash, role
                FROM patients
                WHERE username = ?
                   OR phone = ?
                ORDER BY CASE WHEN username = ? THEN 0 ELSE 1 END, id ASC
                LIMIT 1
                """,
                (identifier, phone or identifier, identifier),
            ).fetchone()
        if patient is None:
            self.send_json(
                HTTPStatus.UNAUTHORIZED,
                {"message": "The username or password is incorrect."},
            )
            return
        salt = bytes.fromhex(patient["password_salt"])
        actual_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
        ).hex()
        if not hmac.compare_digest(actual_hash, patient["password_hash"]):
            self.send_json(
                HTTPStatus.UNAUTHORIZED,
                {"message": "The username or password is incorrect."},
            )
            return
        self.start_patient_session(patient["id"])
        response: dict[str, object] = {
            "authenticated": True,
            "message": "You are signed in.",
        }
        if patient["role"] == "admin":
            response["is_admin"] = True
            response["redirect"] = "/admin"
        self.send_patient_json(HTTPStatus.OK, response)

    def start_patient_session(self, patient_id: int) -> None:
        token = secrets.token_urlsafe(32)
        session_key = hashlib.sha256(token.encode()).hexdigest()
        expires_at = time.monotonic() + 8 * 60 * 60
        with PATIENT_SESSIONS_LOCK:
            PATIENT_SESSIONS.update(
                {
                    key: session
                    for key, session in PATIENT_SESSIONS.items()
                    if session[0] > time.monotonic()
                }
            )
            PATIENT_SESSIONS[session_key] = (expires_at, patient_id)
        cookie = f"patient_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"
        if os.environ.get("COOKIE_SECURE") == "1":
            cookie += "; Secure"
        self._patient_cookie = cookie

    def send_patient_json(
        self,
        status: HTTPStatus,
        data: dict[str, object],
    ) -> None:
        cookie = getattr(self, "_patient_cookie", None)
        self.send_json(status, data, {"Set-Cookie": cookie} if cookie else None)

    def logout_patient(self) -> None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie["patient_session"].value if "patient_session" in cookie else None
        except (KeyError, ValueError):
            token = None
        if token:
            with PATIENT_SESSIONS_LOCK:
                PATIENT_SESSIONS.pop(hashlib.sha256(token.encode()).hexdigest(), None)
        self.send_json(
            HTTPStatus.OK,
            {"message": "You are signed out."},
            {
                "Set-Cookie": (
                    "patient_session=; HttpOnly; SameSite=Strict; "
                    "Path=/; Max-Age=0"
                )
            },
        )

    def current_patient(self) -> sqlite3.Row | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            morsel = cookie.get("patient_session")
            if morsel is None:
                return None
        except (KeyError, ValueError):
            return None
        session_key = hashlib.sha256(morsel.value.encode()).hexdigest()
        with PATIENT_SESSIONS_LOCK:
            session = PATIENT_SESSIONS.get(session_key)
            if session is None or session[0] <= time.monotonic():
                PATIENT_SESSIONS.pop(session_key, None)
                return None
            patient_id = session[1]
        with database() as connection:
            return connection.execute(
                "SELECT id, username, name, phone, role FROM patients WHERE id = ?",
                (patient_id,),
            ).fetchone()

    def create_appointment(
        self,
        payload: dict[str, object],
        patient: sqlite3.Row,
    ) -> None:
        service = clean_text(payload.get("service"), "Appointment type", 80)
        if service not in SERVICES:
            raise ValueError("Choose the general appointment enquiry option.")
        appointment_date = clean_text(payload.get("date"), "Appointment date", 10)
        try:
            requested_day = date.fromisoformat(appointment_date)
        except ValueError as exc:
            raise ValueError("Choose a valid appointment date.") from exc
        if requested_day.isoformat() != appointment_date:
            raise ValueError("Choose a valid appointment date.")
        appointment_time = clean_text(payload.get("time"), "Preferred time", 40)
        if appointment_time not in APPOINTMENT_TIMES:
            raise ValueError("Choose one of the listed preferred times.")
        email = clean_text(payload.get("email", ""), "Email", 254, required=False)
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("Enter a valid email address.")
        payment_method = clean_text(payload.get("payment_method"), "Payment method", 40)
        if payment_method not in {"Cash at clinic", "UPI at clinic", "Card at clinic"}:
            raise ValueError("Choose cash, UPI or card payment at the clinic.")
        message = clean_text(payload.get("message", ""), "Message", 1000, required=False)
        raw_idempotency_key = clean_text(
            payload.get("idempotency_key"), "Request key", 36
        )
        try:
            idempotency_key = str(UUID(raw_idempotency_key))
        except ValueError as exc:
            raise ValueError("Refresh the booking form and try again.") from exc
        with database() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """SELECT id, patient_id, service, appointment_date, appointment_time,
                          email, payment_method, message
                   FROM appointments WHERE idempotency_key = ?""",
                (idempotency_key,),
            ).fetchone()
            if previous is not None:
                same_request = (
                    previous["patient_id"] == patient["id"]
                    and previous["service"] == service
                    and previous["appointment_date"] == appointment_date
                    and previous["appointment_time"] == appointment_time
                    and previous["email"] == email
                    and previous["payment_method"] == payment_method
                    and previous["message"] == message
                )
                if not same_request:
                    self.send_json(
                        HTTPStatus.CONFLICT,
                        {"message": "This request key was already used for a different appointment."},
                    )
                    return
                self.send_json(
                    HTTPStatus.CREATED,
                    {
                        "saved": True,
                        "reference": f"DM-{previous['id']:06d}",
                        "message": "This appointment request was already saved.",
                    },
                )
                return
            now_at_clinic = datetime.now(CLINIC_TIMEZONE)
            if requested_day < now_at_clinic.date():
                raise ValueError("Appointment date cannot be in the past.")
            requested_time = datetime.strptime(appointment_time, "%H:%M").time()
            requested_at_clinic = datetime.combine(
                requested_day,
                requested_time,
                tzinfo=CLINIC_TIMEZONE,
            )
            if requested_at_clinic <= now_at_clinic:
                raise ValueError("That preferred time has passed. Choose a future time.")
            cursor = connection.execute(
                """INSERT INTO appointments
                   (name, phone, service, appointment_date, appointment_time,
                    message, email, payment_method, idempotency_key, patient_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    patient["name"],
                    patient["phone"],
                    service,
                    appointment_date,
                    appointment_time,
                    message,
                    email,
                    payment_method,
                    idempotency_key,
                    patient["id"],
                    now_utc(),
                ),
            )
        self.send_json(
            HTTPStatus.CREATED,
            {
                "saved": True,
                "reference": f"DM-{cursor.lastrowid:06d}",
                "message": (
                    "Your appointment request was saved. Dr. Arvind will contact you "
                    "to confirm availability. Payment preference: "
                    f"{payment_method}. No payment was collected online."
                ),
            },
        )

    def create_contact_message(self, payload: dict[str, object]) -> None:
        name = clean_text(payload.get("name"), "Full name", 100)
        phone = validate_phone(payload.get("phone"))
        email = clean_text(payload.get("email", ""), "Email", 254, required=False)
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise ValueError("Enter a valid email address.")
        message = clean_text(payload.get("message"), "Message", 2000)
        with database() as connection:
            connection.execute(
                """INSERT INTO contact_messages (name, phone, email, message, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, phone, email, message, now_utc()),
            )
        self.send_json(
            HTTPStatus.CREATED,
            {
                "saved": True,
                "message": "Your message was saved for Dr. Arvind to review.",
            },
        )

    def login(self, payload: dict[str, object]) -> None:
        configured_password = os.environ.get("ADMIN_PASSWORD", "")
        if not 12 <= len(configured_password) <= 128:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"message": "Admin access is not configured. Set ADMIN_PASSWORD to a unique password of at least 12 characters."},
            )
            return
        client_ip = self.client_address[0]
        retry_after = admin_login_retry_after(client_ip)
        if retry_after:
            self.send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {"message": "Too many unsuccessful sign-in attempts. Please wait before trying again."},
                {"Retry-After": str(retry_after)},
            )
            return
        username = payload.get("username")
        password = payload.get("password")
        supplied_password = password if isinstance(password, str) and len(password) <= 128 else ""
        expected_hash = hashlib.pbkdf2_hmac(
            "sha256", configured_password.encode("utf-8"), ADMIN_PASSWORD_SALT,
            PASSWORD_ITERATIONS,
        )
        actual_hash = hashlib.pbkdf2_hmac(
            "sha256", supplied_password.encode("utf-8"), ADMIN_PASSWORD_SALT,
            PASSWORD_ITERATIONS,
        )
        username_matches = isinstance(username, str) and hmac.compare_digest(username, "admin")
        if not username_matches or not isinstance(password, str) or not hmac.compare_digest(
            actual_hash, expected_hash
        ):
            record_admin_login_failure(client_ip)
            self.send_json(HTTPStatus.UNAUTHORIZED, {"message": "Incorrect password."})
            return
        clear_admin_login_failures(client_ip)
        token = secrets.token_urlsafe(32)
        session_key = hashlib.sha256(token.encode()).hexdigest()
        with SESSIONS_LOCK:
            current_time = time.monotonic()
            SESSIONS.update(
                {
                    key: expires_at
                    for key, expires_at in SESSIONS.items()
                    if expires_at > current_time
                }
            )
            SESSIONS[session_key] = current_time + 8 * 60 * 60
        cookie = f"hospital_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800"
        if os.environ.get("COOKIE_SECURE") == "1":
            cookie += "; Secure"
        self.send_json(
            HTTPStatus.OK,
            {"message": "Signed in."},
            {"Set-Cookie": cookie},
        )

    def logout(self) -> None:
        token = self.session_token()
        if token:
            with SESSIONS_LOCK:
                SESSIONS.pop(hashlib.sha256(token.encode()).hexdigest(), None)
        self.send_json(
            HTTPStatus.OK,
            {"message": "Signed out."},
            {
                "Set-Cookie": (
                    "hospital_session=; HttpOnly; SameSite=Strict; "
                    "Path=/; Max-Age=0"
                )
            },
        )

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        parsed = urlparse(origin)
        return parsed.scheme in {"http", "https"} and parsed.netloc == self.headers.get("Host")

    def session_token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            morsel = cookie.get("hospital_session")
            return morsel.value if morsel else None
        except (KeyError, ValueError):
            return None

    def is_admin(self) -> bool:
        patient = self.current_patient()
        if patient is not None and patient["role"] == "admin":
            return True
        token = self.session_token()
        if not token:
            return False
        with SESSIONS_LOCK:
            session_key = hashlib.sha256(token.encode()).hexdigest()
            expires_at = SESSIONS.get(session_key, 0)
            if expires_at <= time.monotonic():
                SESSIONS.pop(session_key, None)
                return False
            return True

    def list_appointments(self) -> None:
        try:
            query_values = parse_qs(urlparse(self.path).query, keep_blank_values=True)

            def query_value(name: str, default: str = "") -> str:
                return query_values.get(name, [default])[0]

            search = query_value("search").strip()[:100]
            selected_date = query_value("date")
            status_filter = query_value("status", "all")
            payment_filter = query_value("payment_status", "all")
            try:
                page = max(1, int(query_value("page", "1")))
                page_size = min(100, max(1, int(query_value("page_size", "25"))))
            except ValueError:
                raise ValueError("Choose a valid page.")
            if selected_date:
                try:
                    if date.fromisoformat(selected_date).isoformat() != selected_date:
                        raise ValueError
                except ValueError as exc:
                    raise ValueError("Choose a valid appointment date filter.") from exc
            if status_filter not in {
                "all",
                "pending",
                "confirmed",
                "completed",
                "cancelled",
            }:
                raise ValueError("Choose a valid booking status filter.")
            if payment_filter not in {"all", "not collected"}:
                raise ValueError("Choose a valid payment status filter.")
            filters: list[str] = []
            parameters: list[object] = []
            if search:
                like = f"%{search.lower()}%"
                filters.append(
                    """(LOWER(name) LIKE ? OR phone LIKE ?
                       OR LOWER('DM-' || printf('%06d', id)) LIKE ?
                       OR CAST(id AS TEXT) LIKE ?)"""
                )
                parameters.extend((like, f"%{search}%", like, f"%{search}%"))
            if selected_date:
                filters.append("appointment_date = ?")
                parameters.append(selected_date)
            if status_filter != "all":
                filters.append("status = ?")
                parameters.append(status_filter)
            if payment_filter != "all":
                filters.append("payment_status = ?")
                parameters.append(payment_filter)
            where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
            today = datetime.now(CLINIC_TIMEZONE).date().isoformat()
            with database() as connection:
                summary_row = connection.execute(
                    """SELECT COUNT(*) AS total,
                              SUM(CASE WHEN appointment_date = ? THEN 1 ELSE 0 END) AS today,
                              SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                              SUM(CASE WHEN status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed,
                              SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled
                       FROM appointments""",
                    (today,),
                ).fetchone()
                total = connection.execute(
                    f"SELECT COUNT(*) FROM appointments {where_clause}",
                    parameters,
                ).fetchone()[0]
                total_pages = max(1, (total + page_size - 1) // page_size)
                page = min(page, total_pages)
                offset = (page - 1) * page_size
                rows = connection.execute(
                    f"""SELECT id, name, phone, service, appointment_date,
                               appointment_time, payment_method, payment_status,
                               email, message, status, internal_notes, created_at
                        FROM appointments {where_clause}
                        ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?""",
                    (*parameters, page_size, offset),
                ).fetchall()
            appointments = []
            for row in rows:
                appointment = dict(row)
                appointment["booking_id"] = f"DM-{row['id']:06d}"
                appointments.append(appointment)
            self.send_json(
                HTTPStatus.OK,
                {
                    "appointments": appointments,
                    "summary": {
                        "total": summary_row["total"] or 0,
                        "today": summary_row["today"] or 0,
                        "pending": summary_row["pending"] or 0,
                        "confirmed": summary_row["confirmed"] or 0,
                        "cancelled": summary_row["cancelled"] or 0,
                    },
                    "pagination": {
                        "page": page,
                        "page_size": page_size,
                        "total": total,
                        "total_pages": total_pages,
                    },
                },
            )
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"message": str(exc)})
        except sqlite3.Error:
            self.send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"message": "Appointments could not be loaded."},
            )


if __name__ == "__main__":
    initialize_database()
    server = ThreadingHTTPServer((HOST, PORT), HospitalHandler)
    target_url = (
        f"http://127.0.0.1:{PORT}"
        if HOST in {"127.0.0.1", "0.0.0.0", "localhost", "::"}
        else f"http://{HOST}:{PORT}"
    )
    if os.environ.get("AUTO_OPEN_BROWSER", "1") != "0":
        try:
            webbrowser.open(target_url, new=2)
        except Exception:
            pass
    print(f"Dr. Arvind website running at {target_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
