# API reference

Generated from the application by `scripts/api_reference.py` (230 operations). Interactive version with request and response schemas: **`/api/docs`** (Swagger) and `/api/openapi.json` on a running hub. How to call it, the WebSocket, the event stream, MQTT and TCP: `docs/fa/api-guide.md`.

Permission: the role permission the route needs (organization permissions such as `product.read` are checked again against the organization in the path); `signed in` = any user; `public` = no session.

## Sign-in and sessions

| Method | Path | Permission | What it does |
|---|---|---|---|
| `POST` | `/api/auth/change-password` | signed in | Change Password |
| `POST` | `/api/auth/login` | public | Password step. With MFA enabled the answer is ``{mfa_required, mfa_token}``: send the token with a 6-digit code (or a recovery code) to /api/auth/mfa/verify. |
| `POST` | `/api/auth/logout` | public | Logout |
| `GET` | `/api/auth/me` | signed in | Me |
| `POST` | `/api/auth/mfa/disable` | signed in | Mfa Disable |
| `POST` | `/api/auth/mfa/enable` | signed in | Confirm with the first code. The recovery codes are shown only this once. |
| `POST` | `/api/auth/mfa/setup` | signed in | Start enrolment: show ``otpauth_uri`` as a QR code (or type ``secret`` into the app). |
| `POST` | `/api/auth/mfa/verify` | public | Mfa Verify |
| `POST` | `/api/auth/refresh` | public | Refresh |
| `GET` | `/api/auth/sessions` | signed in | My Sessions |
| `DELETE` | `/api/auth/sessions/{session_id}` | signed in | End Session |

## Self-service accounts, homes

| Method | Path | Permission | What it does |
|---|---|---|---|
| `POST` | `/api/account/delete` | signed in | The user deletes the account: the devices of the personal home are released (their maker sees them as sold, a new owner can claim them after a reset), every session and role ends, name / e-mail / password are erased. … |
| `POST` | `/api/account/homes` | signed in | Another home of the user (a villa, the office): the user owns it and invites others. |
| `PATCH` | `/api/account/homes/{org_id}` | signed in | Rename Home |
| `GET` | `/api/account/invitations` | signed in | My Invitations |
| `POST` | `/api/account/invitations/accept` | signed in | Accept |
| `GET` | `/api/account/organizations` | signed in | My Organizations |
| `DELETE` | `/api/account/organizations/{org_id}` | signed in | Leave |
| `POST` | `/api/account/password/forgot` | public | Forgot |
| `POST` | `/api/account/password/reset` | public | Reset |
| `POST` | `/api/account/signup` | public | Signup |
| `POST` | `/api/account/verify` | public | Verify |
| `POST` | `/api/account/verify/resend` | public | Resend |

## Platform: organizations, products, versions, devices, labels

| Method | Path | Permission | What it does |
|---|---|---|---|
| `GET` | `/api/platform/calendar` | signed in | Calendar |
| `GET` | `/api/platform/catalog` | signed in | Products any owner may add behind an edge hub, with the protocols they speak. |
| `POST` | `/api/platform/claim` | signed in | Add a purchased device to the caller's account (default: the personal organization). |
| `GET` | `/api/platform/devices` | product.read | ``lifecycle=stock``: not with a buyer yet (registered or ready). |
| `GET` | `/api/platform/devices.csv` | product.read | The devices (at most 50 000) with their stage, connection, firmware and warranty — never their owner. |
| `GET` | `/api/platform/devices/{device_id}` | product.read | Get Device |
| `PATCH` | `/api/platform/devices/{device_id}` | device.register | Update Device |
| `POST` | `/api/platform/devices/{device_id}/reset` | device.register | Reset Device |
| `GET` | `/api/platform/devices/{device_id}/shares` | device.share | List Shares |
| `POST` | `/api/platform/devices/{device_id}/shares` | device.share | Share Device |
| `DELETE` | `/api/platform/devices/{device_id}/shares/{access_id}` | signed in | Revoke Share |
| `GET` | `/api/platform/firmware/{release_id}/jobs` | product.manage | Jobs |
| `POST` | `/api/platform/firmware/{release_id}/rollout` | product.manage | Rollout |
| `POST` | `/api/platform/firmware/{release_id}/withdraw` | product.manage | Withdraw |
| `POST` | `/api/platform/invoices/{invoice_id}/status` | org.manage | Invoice Status |
| `GET` | `/api/platform/organizations` | product.read | List Organizations |
| `POST` | `/api/platform/organizations` | org.manage | Create Organization |
| `GET` | `/api/platform/organizations/{org_id}/automations` | device.view | List Automations |
| `POST` | `/api/platform/organizations/{org_id}/automations` | automation.manage | Create Automation |
| `PUT` | `/api/platform/organizations/{org_id}/automations/{automation_id}` | automation.manage | Update Automation |
| `DELETE` | `/api/platform/organizations/{org_id}/automations/{automation_id}` | automation.manage | Delete Automation |
| `GET` | `/api/platform/organizations/{org_id}/automations/{automation_id}/runs` | device.view | Automation Runs |
| `POST` | `/api/platform/organizations/{org_id}/automations/{automation_id}/test` | automation.manage | Dry run now: are the conditions met, what would be written. Nothing is sent. |
| `GET` | `/api/platform/organizations/{org_id}/billing` | org.members | The plan, an estimate of this month (activated devices now: nothing is stored), the issued invoices (one per past Jalali month, made by ``close_month``), every plan to compare. Payment itself happens outside the platf… |
| `GET` | `/api/platform/organizations/{org_id}/branding` | product.read | Branding |
| `PUT` | `/api/platform/organizations/{org_id}/branding` | product.manage | Save Branding |
| `PUT` | `/api/platform/organizations/{org_id}/branding/logo` | product.manage | Put Logo |
| `DELETE` | `/api/platform/organizations/{org_id}/branding/logo` | product.manage | Delete Logo |
| `GET` | `/api/platform/organizations/{org_id}/contacts` | device.view | Who the home alerts by SMS or call, and whether SMS commands are set up. |
| `POST` | `/api/platform/organizations/{org_id}/contacts` | device.view | Create Contact |
| `PUT` | `/api/platform/organizations/{org_id}/contacts/{contact_id}` | device.view | Update Contact |
| `DELETE` | `/api/platform/organizations/{org_id}/contacts/{contact_id}` | device.view | Delete Contact |
| `GET` | `/api/platform/organizations/{org_id}/groups` | device.view | Groups |
| `POST` | `/api/platform/organizations/{org_id}/groups` | device.view | Create Group |
| `PUT` | `/api/platform/organizations/{org_id}/groups/{group_id}` | device.view | Update Group |
| `DELETE` | `/api/platform/organizations/{org_id}/groups/{group_id}` | device.view | Delete Group |
| `GET` | `/api/platform/organizations/{org_id}/holidays` | device.view | Holidays of a Jalali year: the fixed solar ones, the platform's (e.g. the lunar holidays of that year) and the organization's own. |
| `POST` | `/api/platform/organizations/{org_id}/holidays` | automation.manage | Add Holiday |
| `GET` | `/api/platform/organizations/{org_id}/invitations` | org.members | List Invitations |
| `POST` | `/api/platform/organizations/{org_id}/invitations` | org.members | Invite |
| `DELETE` | `/api/platform/organizations/{org_id}/invitations/{invitation_id}` | signed in | Revoke Invitation |
| `GET` | `/api/platform/organizations/{org_id}/members` | org.members | List Members |
| `DELETE` | `/api/platform/organizations/{org_id}/members/{user_role_id}` | org.members | Remove Member |
| `PUT` | `/api/platform/organizations/{org_id}/plan` | org.manage | Set Plan |
| `POST` | `/api/platform/organizations/{org_id}/presence` | device.view | The app reports that its user arrived home or left (its place against the home's, while the app is open): the home's presence automations run. |
| `GET` | `/api/platform/organizations/{org_id}/scenes` | device.view | List Scenes |
| `POST` | `/api/platform/organizations/{org_id}/scenes` | automation.manage | Create Scene |
| `PUT` | `/api/platform/organizations/{org_id}/scenes/{scene_id}` | automation.manage | Update Scene |
| `DELETE` | `/api/platform/organizations/{org_id}/scenes/{scene_id}` | automation.manage | Delete Scene |
| `POST` | `/api/platform/organizations/{org_id}/scenes/{scene_id}/run` | device.view | Run now, with the caller's own rights (a viewer's writes are refused one by one). |
| `GET` | `/api/platform/organizations/{org_id}/settings` | device.view | Home Settings |
| `PUT` | `/api/platform/organizations/{org_id}/settings` | device.view | Save Home Settings |
| `PUT` | `/api/platform/organizations/{org_id}/sms-pin` | device.view | The PIN an SMS command must carry (``شب 4821``); kept only as a hash. |
| `GET` | `/api/platform/products` | product.read | List Products |
| `POST` | `/api/platform/products` | product.manage | Create Product |
| `GET` | `/api/platform/products/{product_id}` | product.read | Get Product |
| `PATCH` | `/api/platform/products/{product_id}` | product.manage | The product's details (the code never changes; the identifier pattern only until the first device is registered). Empty category / description clear them. |
| `POST` | `/api/platform/products/{product_id}/devices` | device.register | Register Devices |
| `POST` | `/api/platform/products/{product_id}/devices/check` | device.register | Before registering a range: its first and last identifier, and the ones already taken (by any product: identifiers are unique on the platform). |
| `POST` | `/api/platform/products/{product_id}/devices/lifecycle` | device.register | Move a whole batch (or a list) to a stage, e.g. a shipped batch to "sold"; devices a buyer activated keep their stage. |
| `PUT` | `/api/platform/products/{product_id}/draft` | product.manage | Create or replace the product's draft version (validated like device YAML). |
| `POST` | `/api/platform/products/{product_id}/firmware` | product.manage | Body: the image itself (application/octet-stream, at most 16 MB). |
| `GET` | `/api/platform/products/{product_id}/firmware` | product.manage | List Firmware |
| `PUT` | `/api/platform/products/{product_id}/image` | product.manage | The product picture: the raw PNG / JPEG / WebP bytes (at most 512 KB) as the body. |
| `DELETE` | `/api/platform/products/{product_id}/image` | product.manage | Delete Product Image |
| `GET` | `/api/platform/products/{product_id}/image` | signed in | The picture, for the maker and for whoever has a device of the product (RLS). |
| `POST` | `/api/platform/products/{product_id}/labels` | device.register | CSV for printing labels: identifier, serial, claim code, QR text (and optionally the device credential). Shown once: issuing again replaces the earlier codes. |
| `GET` | `/api/platform/products/{product_id}/versions` | product.read | List Versions |
| `GET` | `/api/platform/products/{product_id}/versions/{version}` | product.read | Get Version |
| `POST` | `/api/platform/products/{product_id}/versions/{version}/publish` | product.manage | Publish Version |
| `POST` | `/api/platform/products/{product_id}/versions/{version}/upgrade` | product.manage | Upgrade |
| `GET` | `/api/platform/products/{product_id}/versions/{version}/upgrade-preview` | product.manage | What moving the devices (of one version, or all) to ``version`` changes and breaks. |
| `GET` | `/api/platform/protocols` | signed in | Per protocol, the fields a binding gives: connection defaults and per-tag addresses (from the protocol's own models, so the forms of the maker panel and of the app follow the hub's validation). The same for everyone: … |
| `GET` | `/api/platform/stats` | product.read | The maker's dashboard: devices by lifecycle, online now, firmware versions, warranty. |
| `GET` | `/api/platform/tickets` | product.read | Tickets |
| `GET` | `/api/platform/tickets/{ticket_id}` | product.read | Ticket |
| `POST` | `/api/platform/tickets/{ticket_id}/messages` | product.read | The maker answers and/or moves the ticket on (a warranty claim: approved / rejected). |
| `GET` | `/api/platform/trend` | product.read | Per week: devices registered, activated by buyers, tickets opened; and the activated devices offline for more than three days now (likely faulty). |

## Platform: the buyer's devices (app)

| Method | Path | Permission | What it does |
|---|---|---|---|
| `GET` | `/api/platform/my/devices` | device.view | Devices the caller's organizations own or received (device.view); ``org`` limits it to one home. |
| `PATCH` | `/api/platform/my/devices/{device_id}` | device.share | Configure My Device |
| `DELETE` | `/api/platform/my/devices/{device_id}` | device.share | Release My Device |
| `POST` | `/api/platform/my/devices/{device_id}/actions/{key}` | device.view | A command of the product template that sets several parameters (fixed values, or the ones the user entered): each goes through the control-safety layer in order; it stops at the first refused one. |
| `GET` | `/api/platform/my/devices/{device_id}/commands` | device.view | The commands of a device the caller's organizations own or received, newest first: created / sent / delivered / executed / confirmed, or failed / expired, with the times. |
| `GET` | `/api/platform/my/devices/{device_id}/history` | device.view | A tag's values over the last hours, for the app's chart (only tags the template shows on a chart). |
| `PUT` | `/api/platform/my/devices/{device_id}/labels/{key}` | device.view | The owner or an operator names an output or reading of the device for the home ("relay 1" -> "chandelier"), picks its icon and marks it a favourite for the home screen; no name, icon or mark = the maker's own name again. |
| `GET` | `/api/platform/my/devices/{device_id}/local-rules` | device.view | What the device runs by itself while cut off (for the app to show). |
| `POST` | `/api/platform/my/devices/{device_id}/pulse` | device.view | A momentary key (a door opener, a bell): on now and off after ``seconds``, both through the control-safety layer with the caller's rights. The hub turns it off even if the app is closed meanwhile. |
| `PUT` | `/api/platform/my/devices/{device_id}/room` | device.view | The room of the device in the caller's homes (each home has its own rooms); an empty room takes it out of every room. |
| `GET` | `/api/platform/my/devices/{device_id}/sms` | device.view | The SMS fallback (owner and operators): the SIM number and the signing key, which the app keeps on the phone to sign messages while there is no internet. |
| `PUT` | `/api/platform/my/devices/{device_id}/sms` | device.view | The owner switches the SMS fallback on: the hub makes the device's signing key (kept unless ``new_key``), the device gets it now over its own connection (``{"type": "config", "sms": {...}}``) and accepts only messages… |
| `DELETE` | `/api/platform/my/devices/{device_id}/sms` | device.view | Delete Sms |
| `POST` | `/api/platform/my/devices/{device_id}/tickets` | device.view | A support question or a warranty claim to the device's maker. The maker sees the device, the text and the sender's name (the buyer chose to write) — nothing else. |
| `POST` | `/api/platform/my/devices/{device_id}/timers` | device.view | "Turn relay 1 off in 30 minutes": a one-time automation that removes itself. |
| `GET` | `/api/platform/my/devices/{device_id}/timers` | device.view | Timers |
| `DELETE` | `/api/platform/my/devices/{device_id}/timers/{automation_id}` | device.view | Cancel Timer |
| `POST` | `/api/platform/my/devices/{device_id}/transfer` | device.share | Transfer My Device |
| `GET` | `/api/platform/my/devices/{device_id}/view` | device.view | Everything the app shows for one device: its tags as the product template defines them (names fa/en, kinds, units, ranges, UI hints) with the live values; ``writable`` already says whether this caller may operate the … |
| `POST` | `/api/platform/my/devices/{hub_id}/children` | device.share | Add Child |
| `DELETE` | `/api/platform/my/devices/{hub_id}/children/{child_id}` | device.share | Remove Child |
| `GET` | `/api/platform/my/energy` | device.view | kWh per day (Tehran days) and per device for the last ``days`` days, from kWh counters (daily rise) or else power readings (hourly averages); the current Jalali month's total and its cost with the home's tariff (tiers… |
| `GET` | `/api/platform/my/notifications` | device.view | What the bell shows, newest first: devices offline, commands that failed or got no answer, automation runs that failed (the last 48 hours). |
| `GET` | `/api/platform/my/stream` | device.view | Server-sent events for the caller's devices: ``samples`` (tag, value, ts, quality) and ``status`` (online / offline). Read with fetch() and the bearer token (EventSource cannot send headers); a keep-alive every 15 s; … |
| `GET` | `/api/platform/my/summary` | device.view | The home screen's tiles, from the devices' own tags: indoor temperature (average of the temperature readings), power now and energy since midnight (Tehran time, from kWh counters or else the power readings), alerts (o… |
| `GET` | `/api/platform/my/tickets` | device.view | My Tickets |
| `GET` | `/api/platform/my/tickets/{ticket_id}` | device.view | My Ticket |
| `POST` | `/api/platform/my/tickets/{ticket_id}/messages` | device.view | Reply Ticket |

## Public

| Method | Path | Permission | What it does |
|---|---|---|---|
| `GET` | `/api/public/brand/{org_id}` | public | What a maker shows of itself (no sign-in: the app's start page uses it with ``?brand=<org_id>``): name, accent colour, support contact, whether there is a logo. |
| `GET` | `/api/public/brand/{org_id}/logo` | public | Public Logo |

## Control (the control-safety layer)

| Method | Path | Permission | What it does |
|---|---|---|---|
| `GET` | `/api/control/pending` | signed in | Pending |
| `POST` | `/api/control/pending/{request_id}/confirm` | signed in | Confirm |
| `POST` | `/api/control/pending/{request_id}/reject` | signed in | Reject |
| `GET` | `/api/control/policy/{tag_path}` | signed in | Policy |
| `POST` | `/api/control/write` | signed in | Write |

## Live data, history, tags, devices (engineering dashboard)

| Method | Path | Permission | What it does |
|---|---|---|---|
| `GET` | `/api/alarms` | data.read | Active Alarms |
| `GET` | `/api/alarms/history` | data.read | Alarm History |
| `POST` | `/api/alarms/shelve` | alarm.ack | Shelve |
| `DELETE` | `/api/alarms/shelve/{source}` | alarm.ack | Unshelve |
| `GET` | `/api/alarms/shelves` | data.read | List Shelves |
| `POST` | `/api/alarms/{alarm_id}/ack` | alarm.ack | Ack Alarm |
| `GET` | `/api/devices` | data.read | Devices |
| `GET` | `/api/devices/{device_id}` | data.read | Device |
| `GET` | `/api/devices/{device_id}/errors` | data.read | Collection problems of the device over time (offline / read errors), newest first. |
| `GET` | `/api/tags` | data.read | Tags |
| `GET` | `/api/tags/{tag_path}/history` | data.read | History |
| `GET` | `/api/tags/{tag_path}/history.csv` | data.read | History Csv |
| `GET` | `/api/tags/{tag_path}/latest` | data.read | Tag Latest |

## Configuration, building model, calculated tags

| Method | Path | Permission | What it does |
|---|---|---|---|
| `POST` | `/api/calc/preview` | data.read | Calc Preview |
| `GET` | `/api/calc/tags` | data.read | Calc Tags |
| `GET` | `/api/calc/tags/{tag_path}/versions` | data.read | Every definition that was in force, newest first (valid_from = when it took effect). |
| `GET` | `/api/config/alarms` | config.edit | Get Alarms |
| `PUT` | `/api/config/alarms/rules` | config.edit | Put Alarm Rule |
| `DELETE` | `/api/config/alarms/rules/{tag_path}` | config.edit | Delete Alarm Rule |
| `GET` | `/api/config/building` | model.edit | Get Building |
| `PUT` | `/api/config/building` | model.edit | Replace the building model; validated against devices and tags like any change. |
| `GET` | `/api/config/control` | config.edit | Get Control |
| `PUT` | `/api/config/control/defaults` | config.edit | Put Control Defaults |
| `PUT` | `/api/config/control/tags/{tag_path}` | config.edit | Put Control Policy |
| `DELETE` | `/api/config/control/tags/{tag_path}` | config.edit | Delete Control Policy |
| `POST` | `/api/config/devices` | config.edit | Create Device |
| `POST` | `/api/config/devices/validate` | config.edit | Dry run: schema errors and the issues the device would cause. Nothing is written. |
| `GET` | `/api/config/devices/{device_id}` | config.edit | Get Device |
| `PUT` | `/api/config/devices/{device_id}` | config.edit | Replace Device |
| `PATCH` | `/api/config/devices/{device_id}` | config.edit | Set Enabled |
| `DELETE` | `/api/config/devices/{device_id}` | config.edit | Delete Device |
| `GET` | `/api/config/history` | config.edit | The configuration as it was before each change made through the API, newest first. |
| `POST` | `/api/config/history/{snapshot_id}/restore` | config.edit | Put a snapshot back (validated, applied live, audited; the current state is itself kept as a new snapshot first, so a restore can be undone). |
| `POST` | `/api/config/reload` | config.edit | Re-read config/ after a manual edit and apply it (only changed drivers restart). |
| `GET` | `/api/model/equipment` | data.read | Equipment List |
| `GET` | `/api/model/equipment/{equipment_id}` | data.read | Equipment |
| `GET` | `/api/model/equipment/{equipment_id}/maintenance` | data.read | Maintenance |
| `POST` | `/api/model/equipment/{equipment_id}/maintenance` | data.read | Add Maintenance |
| `GET` | `/api/model/locations` | data.read | Every visible location, parents before children (build the tree with ``parent``). |
| `GET` | `/api/model/locations/{location_id}` | data.read | Location |
| `GET` | `/api/model/points/{tag_path}` | data.read | Which equipment and role a tag belongs to (for the tag/device pages). |
| `GET` | `/api/model/systems` | data.read | Systems |

## Dashboards, reports, schedules

| Method | Path | Permission | What it does |
|---|---|---|---|
| `GET` | `/api/dashboards` | data.read | List Dashboards |
| `POST` | `/api/dashboards` | data.read | Create Dashboard |
| `GET` | `/api/dashboards/{dashboard_id}` | data.read | Get Dashboard |
| `PUT` | `/api/dashboards/{dashboard_id}` | data.read | Update Dashboard |
| `DELETE` | `/api/dashboards/{dashboard_id}` | data.read | Delete Dashboard |
| `POST` | `/api/reports` | data.read | Report |
| `POST` | `/api/reports/export` | data.read | Export |
| `GET` | `/api/schedules` | signed in | List Schedules |
| `POST` | `/api/schedules` | signed in | Create Schedule |
| `GET` | `/api/schedules/{schedule_id}` | signed in | Get Schedule |
| `PUT` | `/api/schedules/{schedule_id}` | signed in | Update Schedule |
| `DELETE` | `/api/schedules/{schedule_id}` | signed in | Delete Schedule |
| `POST` | `/api/schedules/{schedule_id}/run` | signed in | Run Now |
| `GET` | `/api/schedules/{schedule_id}/runs` | signed in | Runs |

## Users and roles

| Method | Path | Permission | What it does |
|---|---|---|---|
| `GET` | `/api/roles` | user.manage | Roles |
| `GET` | `/api/users` | user.manage | List Users |
| `POST` | `/api/users` | user.manage | Create User |
| `GET` | `/api/users/{user_id}` | user.manage | Get User |
| `PATCH` | `/api/users/{user_id}` | user.manage | Update User |
| `POST` | `/api/users/{user_id}/mfa/reset` | user.manage | The user lost the authenticator: remove MFA and end their sessions. |
| `POST` | `/api/users/{user_id}/password` | user.manage | Reset Password |
| `POST` | `/api/users/{user_id}/roles` | user.manage | Grant Role |
| `DELETE` | `/api/users/{user_id}/roles/{user_role_id}` | user.manage | Revoke Role |
| `GET` | `/api/users/{user_id}/sessions` | user.manage | User Sessions |
| `DELETE` | `/api/users/{user_id}/sessions` | user.manage | Log a user out everywhere (e.g. a lost laptop). |
| `POST` | `/api/users/{user_id}/unlock` | user.manage | Unlock |

## System, health, metrics

| Method | Path | Permission | What it does |
|---|---|---|---|
| `GET` | `/api/audit/auth` | audit.read | Auth Audit |
| `GET` | `/api/audit/verify` | audit.read | Check the hash chain of both audit tables now. |
| `GET` | `/api/audit/writes` | audit.read | Write Audit |
| `GET` | `/api/health` | public | No authentication: for load balancers and monitoring. Reveals no tag data. HTTP 503 on an HA standby node (or without database), so traffic goes to the active node. |
| `POST` | `/api/system/backup` | config.edit | Backup Now |
| `GET` | `/api/system/certificates` | config.edit | Certificates the hub uses, with their expiry. |
| `GET` | `/api/system/config` | config.edit | The configuration the hub runs with (validated at start; secrets are env var names). |
| `GET` | `/api/system/events` | config.edit | Backups, retention runs, failovers. |
| `GET` | `/api/system/health-checks` | config.edit | Results of the hub's own health checks (``run=true`` checks again now). |
| `GET` | `/api/system/info` | signed in | What the dashboard needs to know about this hub (any signed-in user). |
| `GET` | `/api/system/nodes` | config.edit | HA nodes with role and heartbeat. |
| `GET` | `/api/system/outputs` | config.edit | Data the hub passes on: northbound MQTT targets, Modbus server, OPC UA server. |
| `GET` | `/api/system/stats` | config.edit | Stats |
| `GET` | `/api/versions` | public | API versions this hub serves (no authentication; for integrations and upgrades). |

## Devices of the platform (device credential)

| Method | Path | Permission | What it does |
|---|---|---|---|
| `POST` | `/ingest/v1/broker/acl` | X-Hub-Hook-Token | Broker Acl |
| `POST` | `/ingest/v1/broker/auth` | X-Hub-Hook-Token | Broker Auth |
| `POST` | `/ingest/v1/devices/{device_id}/ack` | device credential (Bearer) | Ack |
| `GET` | `/ingest/v1/devices/{device_id}/commands` | device credential (Bearer) | Commands |
| `GET` | `/ingest/v1/devices/{device_id}/firmware/{release_id}` | device credential (Bearer) | The image of a release the device has an open job for. |
| `POST` | `/ingest/v1/devices/{device_id}/ota` | device credential (Bearer) | ``{"job_id": ..., "status": "downloading" | "installed" | "failed", "version": ...}`` |
| `GET` | `/ingest/v1/devices/{device_id}/rules` | device credential (Bearer) | The device fetches its rules (with its credential) after it starts or reconnects. |
| `POST` | `/ingest/v1/devices/{device_id}/state` | device credential (Bearer) | State |
| `POST` | `/ingest/v1/devices/{device_id}/telemetry` | device credential (Bearer) | Telemetry |

## Other

| Method | Path | Permission | What it does |
|---|---|---|---|
| `GET` | `/api/notifications/log` | audit.read | Notification Log |
| `POST` | `/api/notifications/test/{channel}` | config.edit | Notification Test |
| `POST` | `/api/sms/inbound` | public | The SMS provider forwards each received SMS here (``?token=HUB_SMS_INBOUND_TOKEN``; form or JSON with ``from`` and ``message``). A contact of a home marked for commands runs one of the home's scenes by its keyword and… |

## WebSocket

| | Path | Permission | What it does |
|---|---|---|---|
| `WS` | `/api/ws` | data.read | live samples, device status, alarms and writes (the engineering dashboard) |
