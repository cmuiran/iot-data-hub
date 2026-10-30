# راهنمای استفاده از API، WebSocket و MQTT

فهرست کامل همهٔ مسیرها (۲۲۲ عملیات، با دسترسی لازم برای هر کدام) در
[`docs/api-reference.md`](../api-reference.md) است. آن فهرست از روی خود کد ساخته می‌شود:
`python scripts/api_reference.py`. نسخهٔ تعاملی، با شکل کامل درخواست و پاسخ، روی هر سرور در حال
اجرا در **`/api/docs`** (Swagger) و `/api/openapi.json` است.

## فهرست

1. [اصول کلی](#۱-اصول-کلی)
2. [ورود و توکن](#۲-ورود-و-توکن)
3. [دسترسی‌ها و سازمان‌ها](#۳-دسترسیها-و-سازمانها)
4. [نمونه‌های رایج](#۴-نمونههای-رایج)
5. [فرمان به دستگاه](#۵-فرمان-به-دستگاه-لایهٔ-ایمنی)
6. [WebSocket داشبورد](#۶-websocket-داشبورد-apiws)
7. [جریان زندهٔ اپ](#۷-جریان-زندهٔ-اپ-server-sent-events)
8. [MQTT](#۸-mqtt-سه-کاربرد-جدا)
9. [اتصال دستگاه با HTTP و TCP](#۹-اتصال-دستگاه-با-http-و-tcp)
10. [پیام‌های غیرفرمانی به دستگاه](#۱۰-پیامهای-غیرفرمانی-به-دستگاه)
11. [کلیدها، تایمرها، پیامک و تماس از Hub](#۱۱-کلیدها-تایمرها-پیامک-و-تماس-از-hub)

---

## ۱. اصول کلی

| | |
|---|---|
| نشانی پایه | `https://<سرور>/api/...`؛ نسخه‌دار: `/api/v1/...` (همان مسیرها) |
| قالب | JSON با UTF-8؛ زمان‌ها ISO 8601 به UTC (`2026-10-28T09:00:00Z`) |
| احراز هویت | سرآیند `Authorization: Bearer <access_token>` |
| حجم بدنه | حداکثر ۱ مگابایت؛ بارگذاری Firmware حداکثر ۱۶ مگابایت؛ بیشتر از آن ← `413` |
| خطا | `{"error": "<کد>", "message": "...", ...}`؛ خطای اعتبارسنجی: `422` با `details` |
| محدودیت نرخ | ورود، ثبت‌نام، ادعای دستگاه و ورود دستگاه‌ها محدودند ← `429` با `Retry-After` |
| کد وضعیت | `200` و `201` موفق؛ `204` بدون بدنه؛ `401` توکن نامعتبر؛ `403` بدون دسترسی؛ `404` نیست یا مال دیگری است |

دادهٔ سازمان دیگر همیشه `404` می‌دهد، نه `403`، تا وجودش هم لو نرود.

## ۲. ورود و توکن

```bash
curl -s https://hub.example.com/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "sara@example.com", "password": "<رمز>"}'
```

پاسخ:

```json
{"access_token": "eyJ…", "refresh_token": "r1…", "token_type": "bearer",
 "expires_in": 900, "must_change_password": false}
```

* **`access_token`** کوتاه‌عمر است (پیش‌فرض ۱۵ دقیقه). آن را در هر درخواست بفرستید.
* **`refresh_token`** برای گرفتن توکن تازه است: `POST /api/auth/refresh` با
  `{"refresh_token": "..."}`. هر بار توکن تازه برمی‌گردد و توکن قبلی باطل می‌شود؛ پس همیشه
  آخرین را نگه دارید.
* **ورود دومرحله‌ای:** اگر پاسخ ورود `{"mfa_required": true, "mfa_token": "..."}` بود، کد ۶ رقمی
  را بفرستید: `POST /api/auth/mfa/verify` با `{"mfa_token": "...", "code": "123456"}`.
* **خروج:** `POST /api/auth/logout` با `{"refresh_token": "..."}`.
* **کاربر فعلی و دسترسی‌هایش:** `GET /api/auth/me`.

نمونه در JavaScript، با تمدید خودکار توکن:

```js
let access = null, refresh = localStorage.getItem("refresh");
async function api(method, path, body) {
  const call = () => fetch(path, { method,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${access}` },
    body: body === undefined ? undefined : JSON.stringify(body) });
  let r = await call();
  if (r.status === 401 && refresh) {                       // توکن تمام شده: تمدید و تکرار
    const t = await fetch("/api/auth/refresh", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refresh }) }).then((x) => x.json());
    access = t.access_token; refresh = t.refresh_token;
    localStorage.setItem("refresh", refresh);
    r = await call();
  }
  if (!r.ok) throw await r.json();
  return r.status === 204 ? null : r.json();
}
```

## ۳. دسترسی‌ها و سازمان‌ها

* **دسترسی‌های سراسری** (داشبورد مهندسی) به سایت و ناحیه محدود می‌شوند: `data.read`،
  `tag.write`، `control.setpoint`، `control.command`، `config.edit`، `user.manage`،
  `audit.read`، `org.manage` و …
* **دسترسی‌های سازمانی** (پلتفرم) هر بار با سازمانِ داخل مسیر دوباره بررسی می‌شوند:

| دسترسی | نقش‌هایی که دارند | برای |
|---|---|---|
| `product.read` | مدیر محصول، اپراتور تولید، پشتیبانی | دیدن محصولات و دستگاه‌ها |
| `product.manage` | مدیر محصول | تعریف محصول، نسخه، Firmware، برند |
| `device.register` | مدیر محصول، اپراتور تولید | ثبت دستگاه، برچسب، مرحله، بازنشانی |
| `org.members` | مدیر محصول، مالک خانه | اعضا، دعوت، صورتحساب، تنظیمات خانه |
| `device.view` | اعضای خانه | دیدن دستگاه‌ها |
| `device.operate` | مالک و اعضای دارای کنترل | فرمان |
| `device.share` | مالک خانه | اشتراک، انتقال، پیامک، تیکت |
| `automation.manage` | مالک و عضو | صحنه، اتوماسیون، تایمر |

ستون «Permission» در فهرست مسیرها همین را برای هر مسیر نشان می‌دهد.

## ۴. نمونه‌های رایج

**سازنده: محصول، انتشار، ثبت سری، برچسب**

```bash
H="Authorization: Bearer $TOKEN"; J="Content-Type: application/json"
# ۱) محصول
curl -s -X POST $HUB/api/platform/products -H "$H" -H "$J" -d '{"org_id":"maker","code":"CTRL-8R",
  "name":"کنترلر ۸ رله","identifier_pattern":"CTRL-8R-{0000}"}'
# ۲) قالب (پیش‌نویس) و انتشار نسخهٔ ۱
curl -s -X PUT  $HUB/api/platform/products/$PID/draft -H "$H" -H "$J" -d @template.json
curl -s -X POST $HUB/api/platform/products/$PID/versions/1/publish -H "$H"
# ۳) بررسی تکراری بودن و ثبت ۱۰۰۰ دستگاه
curl -s -X POST $HUB/api/platform/products/$PID/devices/check -H "$H" -H "$J" -d '{"start":1,"count":1000}'
curl -s -X POST $HUB/api/platform/products/$PID/devices -H "$H" -H "$J" \
  -d '{"version":1,"protocol":"native","start":1,"count":1000,"batch":"1405-08-01","warranty_months":18,"enabled":true}'
# ۴) برچسب‌ها (CSV) همراه اعتبارنامهٔ دستگاه برای خط تولید؛ کدها فقط همین یک بار دیده می‌شوند
curl -s -X POST $HUB/api/platform/products/$PID/labels -H "$H" -H "$J" \
  -d '{"batch":"1405-08-01","credentials":true}' -o labels.csv
```

**خریدار: افزودن دستگاه، دیدن، کنترل**

```bash
curl -s -X POST $HUB/api/platform/claim -H "$H" -H "$J" -d '{"identifier":"CTRL-8R-0001","code":"7K3M-9QX2-HD4T"}'
curl -s $HUB/api/platform/my/devices -H "$H"
curl -s $HUB/api/platform/my/devices/$DEV/view -H "$H"           # پارامترها + مقدار زنده + قابل کنترل بودن
curl -s -X POST $HUB/api/control/write -H "$H" -H "$J" \
  -d "{\"tag\":\"platform.devices.$DEV.relay_1\",\"value\":true,\"reason\":\"app\"}"
```

**مهندس: مقدار و تاریخچهٔ یک تگ**

```bash
curl -s "$HUB/api/tags/kish.substation.meter_01.active_power/history?start=2026-10-27T00:00:00Z&max_points=500" -H "$H"
```

## ۵. فرمان به دستگاه (لایهٔ ایمنی)

همهٔ فرمان‌ها، چه از اپ، چه از داشبورد، چه از اتوماسیون، فقط از راه `POST /api/control/write`
(یا فرمان چندپارامتری `POST /api/platform/my/devices/{id}/actions/{key}`) می‌روند. این لایه پیش
از ارسال این‌ها را بررسی می‌کند: دسترسی، فعال بودن کنترل برای آن تگ، محدودهٔ مجاز، قفل ایمنی، و
محدودیت تعداد. همه هم در ممیزی ثبت می‌شوند.

```json
{"tag": "platform.devices.d…​.relay_1", "value": true, "reason": "app"}
```

پاسخ (`WriteResult`):

| `status` | معنی |
|---|---|
| `applied` | اجرا شد و دستگاه مقدار تازه را برگرداند |
| `pending_confirmation` | تأیید نفر دوم لازم است (`/api/control/pending`) |
| `rejected` | لایهٔ ایمنی رد کرد؛ `result` دلیل را می‌گوید (`rejected_range`، `rejected_rbac`، …) |
| `failed` | ارسال نشد |
| `unverified` | ارسال شد ولی مقدار برگشتی تأیید نشد (`timeout` یا `readback_mismatch`) |

چرخهٔ هر فرمان به دستگاه پلتفرم: `created → sent → delivered → executed → confirmed` یا
`failed` یا `expired`. فهرست آن: `GET /api/platform/my/devices/{id}/commands`.

## ۶. WebSocket داشبورد (`/api/ws`)

برای داشبورد مهندسی و هر برنامهٔ نمایش زنده. داده‌ها به حوزهٔ `data.read` کاربر محدودند.

1. اتصال: `wss://<سرور>/api/ws`. هدر `Origin` باید از خود سرور باشد، یا در
   `HUB_ALLOWED_ORIGINS` آمده باشد.
2. اولین پیام، **حداکثر تا ۱۰ ثانیه**: `{"type": "auth", "token": "<access_token>"}`. توکن هرگز
   در URL نمی‌آید تا در لاگ‌ها نماند.
3. سرور یک **عکس فوری** می‌فرستد: `{"type": "snapshot", "tags": [...], "devices": [...], "alarms": [...]}`.
4. بعد رویدادها پشت سر هم می‌آیند:

| `type` | محتوا |
|---|---|
| `samples` | `{"samples": [{"tag", "value", "ts", "quality"}]}` |
| `status` | `{"device_id", "state": "online"\|"offline"\|"error", "last_seen", "last_error"}` |
| `alarm` | رخداد هشدار (فعال شدن، پاک شدن، تأیید) |
| `write` | یک فرمان و نتیجه‌اش |

5. محدود کردن به چند تگ: `{"type": "subscribe", "tags": ["kish.substation.meter_01.active_power"]}`.
   فهرست خالی یعنی همه.
6. زنده ماندن اتصال: `{"type": "ping"}` ← `{"type": "pong"}`.

کدهای بستن: `4401` احراز نشد یا نشست تمام شد (دوباره وارد شوید)؛ `4403` بدون `data.read` یا
شبکهٔ غیرمجاز؛ `1013` کلاینت کُند است یا این گره پشتیبان (HA) است (دوباره وصل شوید).
نشست هر ۶۰ ثانیه دوباره بررسی می‌شود؛ پس از خروج کاربر، اتصال قطع می‌شود.

```js
const ws = new WebSocket(`wss://${location.host}/api/ws`);
ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token: access }));
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.type === "snapshot") console.log("اکنون", m.tags.length, "تگ");
  if (m.type === "samples") m.samples.forEach((s) => console.log(s.tag, s.value, s.quality));
};
ws.onclose = (e) => { if (e.code === 4401) {/* refresh token و اتصال دوباره */} };
setInterval(() => ws.readyState === 1 && ws.send('{"type":"ping"}'), 30000);
```

```python
import asyncio, json, websockets

async def main(token: str) -> None:
    async with websockets.connect("wss://hub.example.com/api/ws",
                                  origin="https://hub.example.com") as ws:
        await ws.send(json.dumps({"type": "auth", "token": token}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "samples":
                for s in msg["samples"]:
                    print(s["tag"], s["value"])
```

## ۷. جریان زندهٔ اپ (Server-Sent Events)

برای اپ خریدار: `GET /api/platform/my/stream` (اختیاری: `?org=<خانه>`). فقط دستگاه‌های خود کاربر
را می‌فرستد. چون EventSource نمی‌تواند هدر بفرستد، با `fetch` و توکن بخوانید:

```
retry: 3000

event: samples
data: [{"tag": "platform.devices.d…​.relay_1", "value": true, "ts": "…", "quality": "good"}]

event: status
data: {"type": "status", "device_id": "d…", "state": "online", "last_seen": "…"}

: keep-alive
```

* هر ۱۵ ثانیه یک خط `keep-alive` می‌آید.
* جریان پس از **۵ دقیقه** تمام می‌شود و برنامه باید با توکن تازه دوباره وصل شود. این‌طور کسی که
  خارج شده، دیگر داده نمی‌گیرد.
* نمونهٔ کامل این خواندن: تابع `live()` در `app/js/core.js`.

## ۸. MQTT: سه کاربرد جدا

### الف) دستگاه‌های پلتفرم ← بروکر پلتفرم ← Hub

Hub با یک اشتراک برای همهٔ دستگاه‌ها به بروکر پلتفرم (EMQX پیشنهادی) وصل می‌شود
(`HUB_PLATFORM_BROKER`، تعریف‌شده در `config/brokers.yaml`).

| موضوع (topic) | جهت | پیام |
|---|---|---|
| `d/<device_id>/telemetry` | دستگاه ← Hub | `{"ts": "…" یا میلی‌ثانیه, "values": {"relay_1": true, "power": 1.8}}` |
| `d/<device_id>/state` | دستگاه ← Hub | `{"state": "online", "firmware": "1.4.2"}`؛ به‌عنوان **LWT**: `{"state": "offline"}` |
| `d/<device_id>/ack` | دستگاه ← Hub | `{"command_id", "status": "received"\|"done"\|"failed", "value", "error"}` |
| `d/<device_id>/ota/status` | دستگاه ← Hub | `{"job_id", "status": "downloading"\|"installed"\|"failed", "version"}` |
| `d/<device_id>/cmd` | Hub ← دستگاه | `{"command_id", "tag", "value", "expires_at"}` |
| `d/<device_id>/ota` | Hub ← دستگاه | پیشنهاد Firmware: `{"type": "ota", "job_id", "release_id", "version", "sha256", "size", "url"}` |
| `d/<device_id>/rules` | Hub ← دستگاه | قواعد محلی (بخش ۱۰) |
| `d/<device_id>/config` | Hub ← دستگاه | تنظیمات، مثل رمز پیامک (بخش ۱۰) |

* **احراز:** نام کاربری = `device_id`، رمز = اعتبارنامهٔ دستگاه (از CSV برچسب‌ها).
* بروکر باید **قلاب‌های HTTP** را به Hub بفرستد، با سرآیند `X-Hub-Hook-Token: <HUB_BROKER_HOOK_TOKEN>`:
  * `POST /ingest/v1/broker/auth` با `{"username", "password", "clientid"}` ← `{"result": "allow"|"deny"}`؛
  * `POST /ingest/v1/broker/acl` با `{"username", "topic", "action": "publish"|"subscribe"}`.
  * هر دستگاه فقط به موضوع‌های خودش دسترسی دارد. پیکربندی آمادهٔ EMQX در
    `deploy/emqx/base.hocon` است.
* **بدون این قلاب‌ها MQTT احراز نمی‌شود**؛ در آن صورت از HTTP یا TCP استفاده کنید.
* QoS پیشنهادی ۱. پیشوند موضوع: `HUB_PLATFORM_TOPIC_PREFIX`. پیام حداکثر ۶۴ کیلوبایت.
* چند گره Hub با `HUB_PLATFORM_SHARED_GROUP` بار را بین خود تقسیم می‌کنند (`$share/...`).

```bash
# آزمایش با mosquitto (TLS در محیط واقعی الزامی است)
mosquitto_pub -h broker.example.com -p 8883 --capath /etc/ssl/certs -q 1 \
  -u "$DEVICE_ID" -P "$DEVICE_SECRET" \
  -t "d/$DEVICE_ID/telemetry" -m '{"values":{"relay_1":true,"power":1.8}}'
mosquitto_sub -h broker.example.com -p 8883 --capath /etc/ssl/certs -q 1 \
  -u "$DEVICE_ID" -P "$DEVICE_SECRET" -t "d/$DEVICE_ID/cmd"
```

```python
import json, aiomqtt

async def device(device_id: str, secret: str) -> None:
    will = aiomqtt.Will(f"d/{device_id}/state", json.dumps({"state": "offline"}), qos=1)
    async with aiomqtt.Client("broker.example.com", 8883, username=device_id, password=secret,
                              tls_params=aiomqtt.TLSParameters(), will=will) as c:
        await c.subscribe(f"d/{device_id}/cmd", qos=1)
        await c.publish(f"d/{device_id}/state", json.dumps({"state": "online"}), qos=1)
        async for msg in c.messages:
            cmd = json.loads(msg.payload)
            # … اجرای فرمان روی سخت‌افزار …
            await c.publish(f"d/{device_id}/ack", json.dumps(
                {"command_id": cmd["command_id"], "status": "done", "value": cmd["value"]}), qos=1)
```

پیاده‌سازی کامل و آزمایش‌شده: `simulators/native_device.py`، روش `run_mqtt`.

### ب) تجهیزات MQTT/JSON موجود ← Hub (درایور `mqtt`)

برای سنسور یا دروازه‌ای که از قبل روی بروکر خودش JSON منتشر می‌کند. Hub مشترک می‌شود و هر مقدار
را با `json_path` برمی‌دارد. پیکربندی در `config/devices/<device_id>.yaml`:

```yaml
device_id: env_01
protocol: mqtt
connection:
  broker_ref: plant_broker          # از config/brokers.yaml
  telemetry_topic: plant/env/01     # فیلتر مجاز است: plant/env/+
  command_topic: plant/env/01/set   # Hub برای فرمان {"<tag>": <مقدار>} منتشر می‌کند
  stale_after_s: 120
tags:
  - {name: temperature, data_type: float32, unit: °C, json_path: $.t}
  - {name: fan, data_type: bool, json_path: $.fan, access: read_write}
```

راه سریع ساخت این فایل: مهارت `add-device`.

### ج) ارسال داده به بروکر دیگر (Northbound)

Hub مقدارها را برای دفتر مرکزی، SCADA یا ابر منتشر می‌کند (`config/northbound.yaml`؛ نمونه در
`config/northbound.example.yaml`):

* `<topic_prefix>/<site>/<area>/<device_id>/<tag>` ← `{"v": 23.4, "ts": "…Z", "q": "good", "u": "degC"}`
  هنگام تغییر، و هر `heartbeat_s` یک بار دوباره؛
* `<topic_prefix>/status` (retained) ← `{"state": "online"|"offline", "node", "ts"}`.

این مسیر **فقط خروجی** است: هیچ پیامی از آن بروکر به دستگاه نوشته نمی‌شود. در قطع ارتباط، پیام‌ها
روی دیسک می‌مانند و بعد به ترتیب فرستاده می‌شوند. Kafka هم با همین فایل پشتیبانی می‌شود.

## ۹. اتصال دستگاه با HTTP و TCP

**HTTP.** همهٔ مسیرها با `Authorization: Bearer <اعتبارنامهٔ دستگاه>`:

| مسیر | کار |
|---|---|
| `POST /ingest/v1/devices/{id}/telemetry` | `{"ts", "values": {...}}` |
| `POST /ingest/v1/devices/{id}/state` | `{"state": "online", "firmware": "1.4.2"}` |
| `GET  /ingest/v1/devices/{id}/commands` | فرمان‌ها و پیام‌های در صف (هر چند ثانیه بپرسید؛ همین پرسیدن دستگاه را آنلاین نگه می‌دارد) |
| `POST /ingest/v1/devices/{id}/ack` | `{"command_id", "status", "value"}` |
| `GET  /ingest/v1/devices/{id}/firmware/{release_id}` | دریافت Firmware؛ پیش از نصب SHA-256 را بررسی کنید |
| `POST /ingest/v1/devices/{id}/ota` | `{"job_id", "status", "version"}` |
| `GET  /ingest/v1/devices/{id}/rules` | قواعد محلی (پس از هر بار روشن شدن) |

۲۰ تلاش ناموفق ورود از یک نشانی در ۱۰ دقیقه ← `429`.

**TCP (با TLS).** درگاه `HUB_TCP_INGEST_PORT`، هر پیام یک خط JSON:

```
دستگاه ← {"device_id": "d…", "secret": "<اعتبارنامه>"}        خط اول، تا ۱۰ ثانیه
Hub    ← {"ok": true}
دستگاه ← {"type": "telemetry", "values": {...}}
Hub    ← {"type": "command", "command_id": "...", "tag": "relay_1", "value": true, "expires_at": "..."}
دستگاه ← {"type": "ack", "command_id": "...", "status": "done", "value": true}
```

هر خط حداکثر ۶۴ کیلوبایت است. هر دستگاه یک نشست دارد و نشست تازه نشست قبلی را می‌بندد.

## ۱۰. پیام‌های غیرفرمانی به دستگاه

از همان راه فرمان‌ها می‌رسند: TCP، موضوع MQTT هم‌نام، یا صف HTTP.

* **قواعد محلی**
  * قالب: `{"type": "rules", "version": "…", "rules": [{"id", "name", "trigger", "conditions", "window", "actions"}], "utc_offset_min": 210, "holidays": ["2026-11-…"], "offline_after_s": 60, "server_time": "2026-10-29T08:30:00Z"}`.
  * دستگاه آخرین نسخه را نگه می‌دارد و **فقط وقتی بیش از `offline_after_s` ثانیه با Hub ارتباط
    ندارد** اجرا می‌کند.
  * ماشه‌ها: `time` (روز هفته: ۰ = شنبه)، `interval`، `value`. اقدام‌ها: `write` و `delay`.
  * **ساعت دستگاه.** قاعدهٔ زمانی فقط با ساعت درست کار می‌کند:
    * دستگاه با اینترنت ساعت را از NTP بگیرد (مثلاً `pool.ntp.org`)؛
    * اگر NTP ندارد، ساعت را از `server_time` (UTC) هر پیام قواعد تنظیم کند؛ `server_time` جزو
      `version` نیست؛
    * برای قطعی طولانی، ساعت RTC با باتری لازم است. دستگاهی که پس از روشن شدن هنوز ساعت درست
      ندارد، قاعدهٔ `time` را اجرا نکند (قاعدهٔ `value` و `interval` بی‌ساعت هم کار می‌کنند).
* **تنظیمات پیامک (پیام امضاشده)**
  * قالب: `{"type": "config", "sms": {"enabled": true, "key": "<۴۳ نویسهٔ base64url>", "device_id": "…"}}`.
    کلید را Hub برای هر دستگاه جدا و تصادفی (۳۲ بایت) می‌سازد؛ دستگاه آن را در حافظهٔ ماندگار نگه دارد.
  * پیامک ورودی: `HUB <شمارنده> relay_1=1 relay_3=0 <امضا>`.
    * امضا = ۸ بایت اول `HMAC-SHA256(key, "<device_id> <شمارنده> relay_1=1 relay_3=0")` به صورت hex (۱۶ نویسه).
    * شمارنده عددی رو به افزایش است (اپ ثانیه‌های زمان یونیکس را می‌گذارد). دستگاه آخرین شمارندهٔ
      پذیرفته را نگه می‌دارد و پیامی با شمارندهٔ کوچک‌تر یا برابر را رد می‌کند؛ پس پیام دیده‌شده
      دوباره قابل استفاده نیست.
    * با کلید تازه، شمارنده از صفر شروع می‌شود.
  * پیام با امضای نادرست، شمارندهٔ تکراری یا کلید ناشناخته کامل نادیده گرفته می‌شود و پاسخی ندارد.
  * کلید تازه: `PUT …/sms` با `{"new_key": true}` (برای گوشی گم‌شده).
* پیاده‌سازی مرجع هر دو: `receive()`، `run_local()` و `sms()` در `simulators/native_device.py`.

## ۱۱. کلیدها، تایمرها، پیامک و تماس از Hub

**نوع کلید هر خروجی** (برای همان خانه):
`PUT /api/platform/my/devices/{id}/labels/{key}` با `{"name", "icon", "favorite", "mode", "pulse_s"}`.
* `mode`: `toggle` (یک کلید)، `dual` (دو کلید روشن و خاموش) یا `momentary` (لحظه‌ای، مثل درب‌بازکن).
* `pulse_s`: مدت فشار در حالت لحظه‌ای (پیش‌فرض ۱ ثانیه).
* فشار لحظه‌ای: `POST /api/platform/my/devices/{id}/pulse` با `{"tag": "relay_5", "seconds": 1.5}`.
  هاب خروجی را روشن می‌کند و پس از این مدت خودش خاموش می‌کند، حتی اگر اپ بسته شود.
  هر دو فرمان از لایهٔ ایمنی می‌گذرند؛ اگر فاصلهٔ کمینهٔ دو فرمان (`min_interval_s`) بیشتر از
  مدت فشار باشد، خاموش‌شدن تا آن زمان صبر می‌کند.

**تایمر** `POST /api/platform/my/devices/{id}/timers`:
* یک بار: `{"tag", "value", "minutes"}`.
* تکرارشونده: `{"tag", "mode": "cycle", "on_s", "off_s", "count", "minutes"}`؛
  `count: 0` یعنی تا لغو، `minutes: 0` یعنی همین حالا.
  چرخه را هاب اجرا می‌کند؛ با لغو (`DELETE …/timers/{id}`) خروجی خاموش می‌ماند.
* در اتوماسیون هم همین اقدام هست: `{"type": "cycle", "device_id", "tag", "on_s", "off_s", "count"}`.

**بازهٔ تاریخ زمان‌بندی:** ماشه‌های `time`، `interval` و `sun` فیلدهای `start` و `end` می‌گیرند
(تاریخ شمسی، مثل `"1405/04/01"`).

**دوباره آنلاین شدن دستگاه:** `{"type": "offline", "device_id", "event": "online"}`.

**مخاطبان پیامک و تماس یک خانه:**
* `GET|POST /api/platform/organizations/{org}/contacts` و `PUT|DELETE …/contacts/{contact_id}`
  با `{"name", "phone", "sms", "call", "command"}` (حداکثر ۱۰ نفر).
* `PUT /api/platform/organizations/{org}/sms-pin` با `{"pin": "4821"}` یا `{"pin": null}`.
  فقط هش آن ذخیره می‌شود.
* خبر دادن در اتوماسیون: `{"type": "notify", "text": "…", "channels": ["email", "sms", "call"]}`.
  پیامک و تماس به مخاطبانی می‌رود که `sms` یا `call` دارند.

**اجرای صحنه با پیامک:**
* صحنه یک کلمه می‌گیرد: `"sms_keyword": "شب"` (در هر خانه یکتا).
* سرویس پیامک هر پیامک دریافتی را به `POST /api/sms/inbound?token=<HUB_SMS_INBOUND_TOKEN>`
  می‌فرستد (فرم یا JSON با `from` و `message`).
* مخاطبی که `command` دارد با «کلمه + رمز» صحنه را اجرا می‌کند: `شب 4821` (ترتیب مهم نیست).
  نتیجه با پیامک به همان شماره برمی‌گردد.
* رمز نادرست، شمارهٔ ناشناس یا بیش از ۵ پیامک در ۱۰ دقیقه بی‌پاسخ می‌ماند و در گزارش ممیزی
  ثبت می‌شود.

**درگاه پیامک** (متغیرهای محیطی):
* `HUB_SMS_PROVIDER`: `none` (خاموش)، `log` (آزمایشی) یا `kavenegar`.
* `HUB_SMS_API_KEY`، `HUB_SMS_SENDER` و `HUB_SMS_INBOUND_TOKEN`.
* کلید هرگز در لاگ نوشته نمی‌شود.
