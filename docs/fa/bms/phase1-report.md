# فاز ۱ BMS — گزارش تحویل

پایه: کامیت `53afa30` (پیش از فاز). همهٔ اعداد و نتایج این گزارش از اجرای واقعی روی همین نسخه است. تغییرات هنوز کامیت نشده‌اند.
طرح و ممیزی اولیه: [phase1-audit-plan.md](phase1-audit-plan.md).

---

## ۱. معماری فعلی و هدف

معماری **modular monolith** حفظ شد و به Microservices تبدیل نشد. لایه‌های جدید به‌صورت ماژول اضافه شدند:

| لایه | پیش از فاز | پس از فاز ۱ |
|---|---|---|
| درایورها | ۱۰ پروتکل | به‌علاوهٔ «دستگاه مجازی» `calc` (`hub/drivers/calc.py`) |
| نرمال‌سازی | `Sample`: مقدار، زمان، کیفیت | به‌علاوهٔ **علت کیفیت** (`reason`) و **زمان دریافت** (`received_at`) |
| مدل اطلاعات ساختمان | — | `hub/building.py`، `config/building.yaml`، جدول‌های 0008 |
| موتور محاسبات | — | `hub/calc/` (واحدها، parser امن، موتور اجرا) |
| هشدار | موجود | بدون تغییر؛ تگ‌های محاسباتی هم هشدار می‌گیرند |
| تاریخچه | موجود | ستون `reason`، نسخه‌های فرمول، تاریخچهٔ خطاهای جمع‌آوری |
| API و UI | ۸۵ مسیر | به‌علاوهٔ `/api/model/*`، `/api/calc/*`، تاریخچه و بازگرداندن پیکربندی، صفحه‌های «ساختمان» و «تگ‌های محاسباتی» |
| امنیت | RBAC سایت/ناحیه | تفکیک تنظیم/فرمان، scope سازمان، `model.edit`، `calc.publish`، `maintenance.write` |

**مسیر داده پس از فاز:**

```
درایور ─► Hub._on_samples (افزودن received_at)
            ├─► کش زنده ─► WebSocket
            ├─► هشدار
            ├─► بافر ─► samples / tag_latest (با reason و received_at)
            └─► CalcEngine ─► تگ‌های محاسباتی ─► Hub._on_samples
```

**مسیر فرمان:**

```
کاربر ─► /api/control/write
       ─► require_writer (یکی از مجوزهای نوشتن)
       ─► ControlService
            can_write(write_class): tag.write، یا control.setpoint / control.command
       ─► سیاست، interlock، ممیزی ─► WriteGrant ─► درایور
```

---

## ۲. ماتریس قابلیت‌ها (پس از فاز)

| قابلیت | وضعیت | شاهد |
|---|---|---|
| سلسله‌مراتب Organization → Campus → Building → Floor → Zone → Room | ✅ | `tests/hub/test_building_config.py` |
| System → Equipment → Point → Tag | ✅ | همان |
| مشخصات تجهیز (سازنده، مدل، سریال، محل، وضعیت، اتصال، مدارک) | ✅ | همان؛ `tests/api/test_bms_phase1.py` |
| سوابق تعمیرات (فقط افزودنی، با ممیزی) | ✅ | `test_maintenance_records*` |
| Point با واحد، نوع، منبع، زمان، کیفیت، محدوده و مجوز (از تگ) | ✅ | `test_building_model_visibility` |
| فیلدهای Haystack و Brick (ذخیره و اعتبارسنجی شکلی) | ✅ | `test_model_validation` |
| خروجی Haystack JSON یا Brick TTL | ❌ | فاز بعد |
| تگ محاسباتی: عملیات چهارگانه، avg، min، max، sum، if | ✅ | `tests/calc/test_expr.py` |
| اختلاف دما، جمع توان فیدرها، راندمان، مصرف ویژه | ✅ | همان |
| انرژی از توان (`integrate`) و شمارندهٔ ساعت کار (`runtime`)، ادامه پس از راه‌اندازی مجدد | ✅ | `tests/calc/test_engine.py`؛ پیش‌نمایش زنده: `grid_energy` پس از ری‌استارت از ۱٫۱۹ به ۳٫۴۸ کیلووات‌ساعت ادامه داد |
| تجمیع در سطح اتاق، طبقه و ساختمان (`sum_of`، `avg_of`، …) | ✅ | `test_building_aggregates` |
| واحدها: تبدیل، رد ترکیب ناسازگار، دمای مطلق در برابر اختلاف دما | ✅ | `tests/calc/test_units.py`، `test_expr.py` |
| داده نامعتبر یا مفقود، کهنگی، تقسیم بر صفر | ✅ | `test_preview_reports_stale_and_bad_inputs`، `test_math_error_is_a_bad_sample` |
| جلوگیری از وابستگی دوری (با نمایش مسیر چرخه) | ✅ | `test_cycles_are_refused_with_their_path` |
| نسخهٔ فرمول با زمان اعتبار | ✅ | `test_store_syncs_building_model_and_calc_versions`، `test_preview_then_publish_a_calculated_tag` |
| پیش‌نمایش پیش از انتشار (بدون ذخیره) | ✅ | همان؛ بررسی در مرورگر |
| بدون eval و بدون اجرای کد دلخواه | ✅ | `test_no_eval_in_the_calc_package`؛ fuzz با ۳۰۰۰ ورودی |
| علت کیفیت و زمان دریافت، جدا از مقدار | ✅ | `test_reason_and_receive_time_are_stored`، `test_tags_show_write_class_reason_and_receive_time` |
| تاریخچهٔ خطاهای جمع‌آوری | ✅ | `test_collection_errors*` |
| تفکیک مجوز مشاهده / تنظیم / فرمان | ✅ | `test_setpoint_operator_adjusts_setpoints_but_cannot_command` |
| محدودسازی داده به سازمان | ✅ | `test_roles_granted_for_an_organization` |
| ثبت تغییرات حساس (مدل، فرمول، تعمیرات، بازگرداندن) | ✅ | `auth_audit`: `config_changed`، `config_restored`، `maintenance_recorded` |
| مسیر بازگشت پیکربندی | ✅ | `test_config_history_and_restore`، `test_a_change_that_fails_to_apply_leaves_no_trace` |
| مسیر بازگشت پایگاه داده (0008) | ✅ | `test_up_down_up`، `test_rollback_rules` |
| اسرار | ✅ | اسرار جدیدی اضافه نشد؛ مدارک فقط پیوند یا مسیر نگه می‌دارند |

---

## ۳. مدل اطلاعات ساختمان

جزئیات در [docs/building-model.md](../../building-model.md) آمده است (انگلیسی، طبق قاعدهٔ مستندات پروژه). نکات اصلی:

* مدل **روی تگ‌ها سوار است**؛ نام هیچ تگی تغییر نکرد. پس تاریخچه، هشدار، داشبورد و سیاست‌های فرمان دست نخوردند.
* «دستگاه» (اتصال ارتباطی) و «تجهیز» (دارایی) جدا هستند. یک PLC می‌تواند نقاط چند تجهیز را بخواند.
* نمونهٔ واقعی برای سایت کیش در `config/building.yaml` تعریف شده است:
  * ۱ سازمان؛
  * ۱ پردیس، ۳ ساختمان، ۴ طبقه، ۱ زون، ۴ اتاق؛
  * ۶ سیستم؛
  * ۷ تجهیز.
* قواعد اعتبارسنجی:
  * ترتیب سطح‌ها (به همین دلیل چرخه در درخت ممکن نیست)؛
  * یکتایی شناسه‌ها؛
  * وجود تگ؛
  * هر تگ فقط نقطهٔ یک تجهیز است؛
  * تجهیز باید در ساختمانِ سیستمِ خودش باشد؛
  * سیستم حریق فقط خواندنی است؛
  * پیوند مدارک فقط `http(s)` یا مسیر نسبی بدون `..` است.

## ۴. کد و مهاجرت‌ها

**فایل‌های جدید:**

* هسته:
  * `hub/building.py`
  * `hub/calc/{__init__,units,expr,engine}.py`
  * `hub/drivers/calc.py`
* API:
  * `backend/api/model.py`
  * `backend/api/calc.py`
* پایگاه داده:
  * `db/migrations/0008_building_model.sql`
  * `db/migrations/down/0008_building_model.sql`
* پیکربندی نمونه:
  * `config/building.yaml`
  * `config/devices/calc_kish.yaml` (۸ تگ محاسباتی نمونه)
* رابط کاربری:
  * `frontend/js/views/building.js`
  * `frontend/js/views/calc.js`
* مستندات:
  * `docs/building-model.md`
  * `docs/calculated-tags.md`
* تست‌ها:
  * `tests/calc/*`
  * `tests/hub/test_building_config.py`
  * `tests/db/test_building_migration.py`
  * `tests/api/test_bms_phase1.py`

**فایل‌های تغییرکرده (مهم‌ترین‌ها):**

* `hub/models.py`
* `hub/config_loader.py`
* `hub/runtime.py`
* `hub/drivers/base.py` و درایورهای OPC UA، MQTT و IEC 104 (علت کیفیت)
* `hub/storage/{postgres,records,migrate}.py`
* `backend/{rbac,control,config_admin,auth,deps,services,schedules}.py`
* API‌های `config`، `control`، `data`، `schedules` و `users`
* فرانت‌اند: `store`، `main`، `ui`، `users`، `control`، `device`، `dashboard`، `schedules`، i18n فارسی و انگلیسی، CSS
* `docs/backend.md` (بخش‌های ۳۰ تا ۳۳)

**مهاجرت 0008:**

* پروتکل `calc`؛
* ستون‌های `tags.write_class`، `samples.reason`، `tag_latest.reason` و `tag_latest.received_at`؛
* جدول‌های:
  * `calc_versions`
  * `collection_errors`
  * `organizations` و `organization_sites`
  * `locations`
  * `building_systems`
  * `equipment` و `equipment_points`
  * `maintenance_records` (فقط افزودنی)
* `user_roles.org_id`؛
* نقش `setpoint_operator` و ۵ مجوز جدید؛
* رویدادهای ممیزی جدید.

**بازگشت:**

```
python -m hub.storage.migrate --rollback 0008_building_model
```

**وابستگی جدید:** ندارد (`requirements.lock` بدون تغییر و تأییدشده).

## ۵. تست‌ها و نتایج واقعی

| اجرا | نتیجه |
|---|---|
| پیش از فاز (پایه، `53afa30`) | ۷۴۱ قبول، ۱ رد (`test_skill_docs`)، ۲ رد شده (skip)؛ ۸ دقیقه |
| تست‌های جدید فاز | `tests/calc` ۵۷، `test_building_config` ۲۸، `test_building_migration` ۶، `test_bms_phase1` ۱۱، `tests/frontend` ۲۷ (۵ مورد جدید) |
| اجرای کامل پس از فاز (اجرای نهایی) | **۸۴۶ قبول، ۳ رد، ۲ رد شده (skip)**؛ ۱۳ دقیقه. ردها: ۱) `test_skill_docs` (خطای از پیش موجود)؛ ۲ و ۳) `test_alarm_list_and_ack` و `test_northbound_keeps_order_through_a_broker_outage`، که جداگانه ۳ بار از ۳ بار قبول شدند و در اجرای کامل قبلی هم قبول بودند؛ یعنی زمان‌بندی زیر بار، نه خطای منطقی (مورد ۱۲ بخش ۷) |
| ruff و bandit | بدون خطا |
| `validate_config` روی `config/` | ۰ خطا، ۱۰ هشدار (همان هشدارهای قبلی: متغیرهای محیطی تنظیم‌نشده) |
| `scripts/lock.py --check` | قفل وابستگی‌ها به‌روز است |

**بررسی در مرورگر** روی سرور پیش‌نمایش با شبیه‌سازها:

* هر ۱۲ دستگاه online بودند، از جمله دو دستگاه محاسباتی.
* صفحهٔ ساختمان: درخت مکان‌ها و صفحهٔ تجهیز با مقدار زنده، کیفیت، زمان دریافت و کلاس نوشتن.
* صفحهٔ تگ‌های محاسباتی: ۸ تگ نمونه با مقدار درست، مثل سهم خورشیدی ۳۶٫۴٪ و مصرف ویژه ۰٫۷۱۵ کیلووات‌ساعت بر مترمکعب.
* ویرایشگر: پیش‌نمایش فرمول نادرست با پیام «واحدها سازگار نیستند» و شمارهٔ نویسه نشان داده شد.
* انتشار و ویرایش فرمول کار کرد و نسخهٔ تازه ثبت شد.

**دو باگ واقعی** در همین بررسی پیدا و رفع شد و برای هر دو تست اضافه شد:

1. بارگذاری مجدد پیکربندی با دستگاه محاسباتی خطای ۵۰۰ می‌داد، چون دستگاه محاسباتی `connection` ندارد.
2. اگر اعمال تغییر شکست می‌خورد، فایل روی دیسک می‌ماند. اکنون خودکار به حالت قبل برمی‌گردد.

## ۶. مستندات API و مدل داده

* `docs/building-model.md`: اشیاء، قواعد، جدول‌ها، مسیرهای `/api/model/*`.
* `docs/calculated-tags.md`: زبان فرمول، واحدها، کیفیت، وابستگی‌ها، نسخه، پیش‌نمایش و انتشار، مسیرهای `/api/calc/*`.
* `docs/backend.md`:
  * §۳۰: نمای کلی؛
  * §۳۱: کیفیت داده؛
  * §۳۲: مجوزها؛
  * §۳۳: تاریخچه و بازگشت.
* OpenAPI خودکار در `/api/docs` (با `--dev` یا `HUB_API_DOCS`).

## ۷. مشکلات و کارهای باقی‌مانده

| # | مورد | توضیح |
|---|---|---|
| 1 | تست `test_skill_docs` | همان خطای قبل از فاز: نبود مثال برای ۴ پروتکل FINS، MC، EtherNet/IP و IEC 104 در راهنمای افزودن دستگاه. در این فاز اصلاح نشد. |
| 2 | TimescaleDB | مهاجرت 0008 فقط روی PostgreSQL معمولی (pgserver) تست شد. افزودن و حذف ستون `reason` روی hypertable فشرده‌شده باید روی سرور TimescaleDB واقعی پیش از استقرار تست شود. |
| 3 | تست بار | تست ۳۰۰ دستگاه پس از این تغییرات دوباره اجرا نشد. `received_at` برای هر نمونه یک کپی شیء می‌سازد و موتور محاسبات هم بار اضافه دارد. فقط تست بار کوچک (۸ دستگاه) در مجموعهٔ تست اجرا شد. |
| 4 | فرمول | تابع `delta(tag, period)` پیاده نشد؛ مصرف دوره‌ای از گزارش‌ها (`delta`) گرفته می‌شود. پیش‌نمایش فقط روی مقادیر زنده است، نه روی بازهٔ تاریخچه. |
| 5 | وضعیت «پیش‌نویس» | جدول جدایی برای پیش‌نویس فرمول ساخته نشد. پیش‌نویس همان پیش‌نمایش است و انتشار همان ذخیرهٔ پیکربندی (با نسخه، ممیزی و امکان بازگرداندن). |
| 6 | ویرایش مدل ساختمان در UI | فقط از طریق YAML یا `PUT /api/config/building` ممکن است. صفحهٔ ساختمان فقط نمایش و ثبت سابقهٔ تعمیر دارد. |
| 7 | LDAP | نگاشت گروه‌ها به scope سازمان پشتیبانی نمی‌شود؛ فقط سایت و ناحیه. |
| 8 | Haystack و Brick | فقط فیلدها ذخیره می‌شوند. خروجی استاندارد برای فاز بعد است. |
| 9 | علت کیفیت در تاریخچه | `reason` در `samples` ذخیره می‌شود، ولی API تاریخچه (`/history`) هنوز آن را برنمی‌گرداند. |
| 10 | تست خودکار فرانت‌اند | Node روی سیستم نیست. JavaScript فقط با تست‌های استاتیک و بررسی دستی در مرورگر سنجیده شد. |
| 11 | CI | GitHub Actions هنوز در مخزن فعال نیست. |
| 12 | تست‌های ناپایدار | دو تست وابسته به زمان (هشدار و Store & Forward) در اجرای کامل گاهی رد می‌شوند و جداگانه همیشه قبول می‌شوند. باید زمان انتظارشان پایدارتر شود. |

## ۸. معیارهای پذیرش فاز ۱

| معیار | وضعیت |
|---|---|
| `validate_config` روی پیکربندی مخزن بدون خطا، با مدل ساختمان و تگ‌های محاسباتی | ✅ |
| خطاهای عمدی مدل ساختمان (۱۲ حالت) با پیام روشن رد می‌شوند | ✅ |
| مهاجرت بالا، پایین، دوباره بالا بدون خطا؛ رد بازگشت در شرایط ناامن | ✅ |
| کاربر یک ناحیه، تجهیز و مکان ناحیه‌های دیگر را نمی‌بیند (404) | ✅ |
| فرمول با واحد ناسازگار هنگام بارگذاری و پیش‌نمایش رد می‌شود | ✅ |
| هیچ eval یا اجرای کد در `hub/calc` نیست؛ fuzz بدون خطای غیرمنتظره | ✅ |
| وابستگی دوری رد می‌شود و مسیرش نشان داده می‌شود | ✅ |
| مقدار محاسبه‌شده با کیفیت درست در تاریخچه، داشبورد و API می‌آید؛ نسخهٔ فرمول ثبت می‌شود | ✅ |
| شمارندهٔ ساعت کار و انرژی پس از راه‌اندازی مجدد ادامه می‌دهند | ✅ |
| اپراتور تنظیم نمی‌تواند فرمان روشن/خاموش بدهد | ✅ |
| تغییر پیکربندی قابل بازگرداندن است و ممیزی می‌شود | ✅ |
| همهٔ تست‌های پیشین همچنان سبزند (به‌جز خطای از پیش موجود ۱) | به بخش ۵ مراجعه شود |
