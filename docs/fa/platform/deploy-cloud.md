# استقرار ابری پلتفرم

> فایل‌ها: پوشهٔ `deploy/`. این پیکربندی روی دستگاه توسعه اجرا نشده است (Docker نصب نبود)؛
> پیش از استفادهٔ واقعی روی یک سرور آزمایشی اجرا و بررسی شود.

## اجزا

| سرویس | کار | در دسترس از اینترنت |
|---|---|---|
| `caddy` | HTTPS با گواهی خودکار، جلوی hub | ۴۴۳ و ۸۰ |
| `hub` | API، داشبورد، اپ موبایل، موتورها | فقط از طریق Caddy؛ درگاه ۷۰۰۰ برای TCP/TLS دستگاه‌ها |
| `emqx` | بروکر MQTT دستگاه‌ها (فقط TLS) | ۸۸۸۳ |
| `db` | PostgreSQL + TimescaleDB | خیر (شبکهٔ داخلی) |

## گام‌ها

1. روی سرور لینوکس (Ubuntu 24.04 یا مشابه) Docker و Docker Compose را نصب کنید.
2. مخزن را کپی کنید و در `deploy/`: `cp .env.example .env` و همهٔ مقادیر را با رمزهای قوی و
   تصادفی پر کنید (هرگز در گیت قرار نگیرد؛ `.gitignore` آن را کنار می‌گذارد).
3. گواهی TLS برای بروکر و درگاه TCP را در `deploy/certs/` بگذارید (`server.crt`، `server.key`).
4. `docker compose -f deploy/docker-compose.yml up -d --build`
5. در داشبورد EMQX حساب خود hub را بسازید (همان `HUB_PLATFORM_MQTT_USERNAME/PASSWORD`).
6. اولین مدیر: `docker compose exec hub hub-admin create-admin --username <نام>`
7. پشتیبان‌گیری: `HUB_BACKUP_DIR` را روی یک دیسک جدا تنظیم کنید و بازیابی را آزمایش کنید
   (`docs/fa/admin-guide.md`).

## نکات امنیتی

* پایگاه داده هیچ درگاهی رو به بیرون ندارد. hookهای بروکر (`/ingest/v1/broker/*`) را Caddy از
  اینترنت می‌بندد؛ EMQX از شبکهٔ داخلی به hub می‌رسد.
* MQTT بدون TLS (۱۸۸۳) خاموش است. هر دستگاه با اعتبارنامهٔ خودش وارد می‌شود و فقط Topicهای
  خودش را دارد.
* `HUB_SIGNUP_ENABLED` پیش‌فرض خاموش است؛ با دعوت‌نامه کار کنید یا آگاهانه روشن کنید.
* برای چند گره، `HUB_HA=on` و `HUB_PLATFORM_SHARED_GROUP` را تنظیم کنید (اشتراک مشترک MQTT).
