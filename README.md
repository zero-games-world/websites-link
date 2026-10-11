# Dr. Arvind website

A responsive, original website built using HTML, CSS, JavaScript and a dependency-free Python 3 backend. The public homepage opens at `/`; patient sign-in is only required when sending an appointment enquiry. Patient appointments and general contact messages are saved to SQLite. The private, server-authorized staff portal at `/admin` provides booking summaries, filters, pagination, details, internal notes and booking-status management.

## Run locally

Requirements: Python 3.10 or newer. No third-party packages or build step are required.

From PowerShell, in this project directory:

```powershell
$env:ADMIN_PASSWORD = "replace-with-a-unique-password-at-least-12-characters"
C:\Path\To\Python\python.exe server.py
```

On Windows, double-click `start_website.bat` to start the server and open the site in your browser. To start the server quietly in the background whenever you sign in to Windows, run this once in PowerShell from the project directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\set_auto_start.ps1
```

After signing in to Windows, the website server starts quietly in the background and opens [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser. This starts after you sign in, not before the Windows sign-in screen. To turn auto-start off, run `powershell -ExecutionPolicy Bypass -File .\set_auto_start.ps1 -Disable`. The auto-start shortcut is stored in your Windows user Startup folder. Background startup logs are written to `data\auto_start.log` and `data\auto_start_error.log`.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Replace the example executable with your locally installed Python interpreter. To view the site without setting up staff access, start the server without `ADMIN_PASSWORD`; the staff interface remains unavailable until a strong password is configured.

The server creates `data\dr_mehta.sqlite3` when it starts. This database contains patient account details, contact details and appointment enquiries: keep it outside public/static files, do not commit it, and protect its file, write-ahead-log files, backups and host access. Patient accounts use a mobile number and a password of at least 12 characters; passwords are salted and hashed before storage. Patients can register or sign in at `/signin.html`; they must be signed in to submit appointment enquiries. General contact enquiries and the public homepage remain available without a patient account. Appointment enquiries can be general, follow-up or existing-patient requests; users provide a preferred date and time, not a confirmed slot. Optional email and clinic payment preference are saved with each request. Payment preferences for cash, UPI or card are only preferences; no online payment is collected. Other online wallets are unavailable until a real payment integration is configured. Establish a suitable retention and deletion policy before collecting real visitor information. A request is shown as received only after the database confirms it was saved. Forms do not automatically send emails, SMS or WhatsApp notifications.

## Staff portal

Set `ADMIN_PASSWORD` to a unique secret between 12 and 128 characters before launching the server if staff access is needed. Never put the secret in website files or commit it. The staff ID is `admin`. The server compares PBKDF2-derived password digests, applies per-IP login throttling, and stores only hashed session-token identifiers in memory. The session cookie is HttpOnly and SameSite=Strict, expires after eight hours, and is marked Secure when `COOKIE_SECURE=1`. Without `ADMIN_PASSWORD`, staff login is disabled.

Visit `/admin` and sign in using staff ID `admin` and the locally configured password. Booking data is returned only by authenticated admin API requests. The dashboard provides newest-first results, search by name/mobile/booking ID, date and status filters, payment-status review, pagination, and a private details view. Staff can set Pending, Confirmed, Completed or Cancelled, and add internal notes. Cancelling prompts for confirmation. A preferred date/time is not a confirmed slot; the server prevents two requests for the same date/time from both being confirmed. Request idempotency keys prevent network retries or repeated submits from creating duplicate records. Payment status remains “Not collected” because no payment gateway or clinic-payment reconciliation is connected.

This version uses one shared staff secret instead of individual accounts, role permissions and an audit trail. It is intended as a locally runnable project, not a security-reviewed public healthcare deployment. Before storing real patient data, host the site behind HTTPS; set `COOKIE_SECURE=1`; protect access to the host and database; keep encrypted backups; and replace the shared secret with an organization-approved access-control system. Manage production secrets through the host’s secret manager, not source code. The embedded privacy notice is a starting point: Dr. Arvind must approve its legal and operational accuracy before publication.

## Verified content and remaining details

The brand and telephone/WhatsApp contact details supplied for this project are **Dr. Arvind** and **7300641883**. The following details have not been supplied, so the website does not invent them: Dr. Arvind’s specialty, verified qualifications, experience, medical services, clinic address, consultation hours, confirmed photographs or testimonials. The doctor portrait, reception, clinic and healthcare imagery are licensed Unsplash stock photographs used only for illustration and are explicitly not identified as Dr. Arvind or his practice. Replace stock imagery with practice-approved photographs when available.

The appointment form and general contact form save submissions in SQLite, but appointment requests remain **pending until the doctor confirms availability**. The selected payment method is saved as a preference; online payment processing is not integrated. Existing databases are migrated to support completion status, payment status, idempotency keys and private staff notes while preserving existing records. Set up a strong admin password before collecting real enquiries. Email, SMS, WhatsApp message delivery, maps and automated notifications are not integrated. The WhatsApp button opens WhatsApp with the supplied, pre-filled enquiry text. The verified phone links open the device dialler.

## Deployment

Run the app on a trusted host with Python 3.10+ and persistent, access-controlled storage for `data`. Set `HOST` and `PORT` as needed. For a public deployment, use a production web server or reverse proxy with HTTPS, set `COOKIE_SECURE=1`, and prevent direct public access to the application server and database files. Unsplash images require an internet connection. No practice address is embedded in a map. Canonical links and a public-domain sitemap have not been configured because the website’s production domain has not been supplied.

## API endpoints

- `GET /api/health` — database connectivity.
- `GET /api/departments` — supported appointment-enquiry types.
- `GET /api/patient/me` — check the current patient session.
- `POST /api/patient/register` and `POST /api/patient/login` — create or access a patient account.
- `POST /api/patient/logout` — end the patient session.
- `POST /api/appointments` — validate and save an appointment request and clinic payment preference (patient sign-in required; does not process payment).
- `POST /api/contact` — validate and save a contact message.
- `POST /api/admin/login` and `POST /api/admin/logout` — staff session management.
- `GET /api/admin/session` — report whether the current staff session is authenticated (no booking data).
- `GET /api/admin/appointments` — filtered, paginated booking list and real summary counts (authorized staff only).
- `PATCH /api/admin/appointments` — update booking status and private internal notes (authorized staff only).

## Tests

Run the backend and static-link tests with your configured Python interpreter:

```powershell
C:\Path\To\Python\python.exe -m unittest discover -s tests -v
```

Tests use a temporary local database and an ephemeral HTTP server.
