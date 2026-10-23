# Web dashboard (frontend/)

The dashboard is served by `hub-api` itself at `/` (same host, port and certificate as the API).
Open `http://<hub>:8000/` (or `https://` with `HUB_TLS_CERT`). Persian (right-to-left) is the
default language; English is one click away on the sign-in page and under *My account*.

## 1. Design decisions

| Decision | Why |
|---|---|
| Plain JavaScript modules, no build step, no npm packages | runs on an isolated plant network; nothing to download, nothing to rebuild; Node is not needed on any PC |
| Everything served by `hub-api` | one address, one certificate, one firewall rule; the WebSocket is same-origin |
| Strict content security policy (`script-src 'self'; style-src 'self'`, no inline code) | an injected string cannot run as code; text is always inserted as text (`ui.h`), never as HTML |
| Hash routing (`#/alarms`) | no server-side routes needed; every page can be bookmarked |
| Calm grey screens, colour only for abnormal states (ISA-101 style) | an alarm or a faulted device stands out from across the control room |
| Plant calendar and time zone | Jalali (Solar Hijri) dates in Persian, Gregorian in English, always in `HUB_TIMEZONE` |
| System fonts (Vazirmatn when installed, else Segoe UI / Tahoma) | no font download; install Vazirmatn on operator PCs for the best Persian rendering |

## 2. Pages

| Page | Who sees it (permission) | What it does |
|---|---|---|
| Overview `#/overview` | `data.read` | sites → areas → device cards with live values, summary counts |
| Device `#/device/<id>` | `data.read` | every tag: live value, 30-minute trend, quality, command button |
| Alarms `#/alarms` | `data.read` (`alarm.ack` to act) | active / shelved / history, acknowledge (one or all), shelve with reason, horn and desktop notifications |
| Trends `#/trends` | `data.read` | up to 8 tags, ranges 1 h … 30 days or custom (Jalali date picker), min/max band, alarm limits, live follow, CSV |
| Reports `#/reports` | `data.read` | hourly / daily / monthly, avg/min/max/last/consumption, Excel and CSV |
| Dashboards `#/dashboards` | `data.read` | own and shared layouts: value, gauge, live trend, device state, alarms, command button, note |
| Control `#/control` | `tag.write` or `write.confirm` | writable tags, commands waiting for a second person, recent commands |
| Schedules `#/schedules` | `tag.write` | cron timing, steps (write / wait / wait for condition), run now, run history |
| Users `#/users` | `user.manage` | accounts, roles per site/area, temporary password, unlock, reset MFA, end sessions, disable |
| Devices `#/config/devices` | `config.edit` | enable/disable, edit as JSON with check-before-save, new device from a template, reload from files |
| Rules `#/config/rules` | `config.edit` | alarm limits per tag, control policy (two-person, rate, step, interlocks) |
| Audit `#/audit` | `audit.read` | command log, sign-in/administration log, hash-chain check |
| System `#/system` | `config.edit` | process, HA nodes, configuration check, certificates, backups, notification channels (with test), events |
| My account `#/account` | everyone | language, theme, password, two-factor sign-in, own sessions |

The menu shows only what the user's roles allow; the API checks every request again.

## 3. Sign-in and sessions

* Password → (six-digit code or recovery code when MFA is on) → forced password change or MFA
  enrolment when the account needs it. After a password change the server ends every session;
  the page signs in again with the new password by itself.
* The access token is kept in memory only, the refresh token in `sessionStorage`: a reload keeps
  the session, closing the tab ends it. Tokens never appear in URLs; the WebSocket receives the
  token in its first message.
* The access token is renewed a minute before it expires; the live stream reconnects with
  backoff (1 s … 30 s) and after the computer wakes up.
* Idle sign-out (`HUB_SESSION_IDLE_MINUTES`) and sign-out by an administrator show a message on
  the sign-in page.

## 4. Commands from the dashboard

Every command goes through `POST /api/control/write`, i.e. the control-safety layer. The dialog:

1. shows the tag's range, largest step, minimum interval, interlocks (with their current state)
   and whether a second person must confirm;
2. checks the typed value in the browser (number, integer, range, step; Persian digits accepted);
3. shows *old → new* for review; only **Send** submits;
4. shows the hub's answer: applied (with read-back), waiting for confirmation, refused (with
   the reason) or failed.

The browser checks are a convenience; the server makes the decision and writes the audit trail.

## 5. Alarm annunciation

* The horn (generated sound, no files) sounds for new unacknowledged alarms from the chosen
  severity (default *high*) and repeats every 12 s until they are acknowledged or **Silence** is
  pressed. Browsers allow sound only after one click on the page.
* Desktop notifications (optional, per browser) appear while the dashboard is in the background.
* These preferences are stored per browser (`localStorage`), not per user.

## 6. Translations

`frontend/i18n/fa.json` and `en.json` hold every user-facing text (≈ 830 keys). Rules, checked by
`tests/frontend/test_static.py`:

* both files have exactly the same keys and the same `{placeholders}`;
* every key used in the code exists; every enum value of the backend (device states, qualities,
  severities, protocols, write results, audit events, roles) has a label;
* no user-facing text in the code.

Site, area and tag names come from the configuration. Optional display names can be added under
`sites.*`, `areas.*`, `tag_names.*` and `tag_words.<tag>.true/false` (e.g. *Open / Closed* for a
door) in both files.

## 7. Settings

| Variable | Default | Effect |
|---|---|---|
| `HUB_FRONTEND_DIR` | the bundled `frontend/` | another folder with the same files, or `off` for API only |
| `HUB_TIMEZONE` | `Asia/Tehran` | all dates and report periods on the dashboard |
| `HUB_ALLOWED_ORIGINS` | – | only needed when the dashboard is served from a different host |

## 8. Checks without a browser runner

Node is not installed on the plant PCs, so there is no JavaScript unit-test runner. Instead:

* `tests/frontend/test_static.py` — translations, no `innerHTML`/`eval`/inline code, no external
  resources, tokens never in URLs, commands only via `/api/control/write`, device templates valid
  against `hub/models.py`;
* `tests/api/test_frontend_serving.py` — files served with the CSP, API rules unchanged,
  `HUB_FRONTEND_DIR=off`, `/api/system/info`.
* The Jalali conversion (`js/jalali.js`) was compared with the browser's own Persian calendar for
  every day from 2000 to 2040 (14 610 days, no difference).
