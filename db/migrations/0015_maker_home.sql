-- 0015_maker_home: what the maker panel and the consumer app need beyond phases 1-8.
-- Rollback: db/migrations/down/0015_maker_home.sql.
--
-- * platform_devices.activated_at: the first claim by a buyer; the warranty runs from it
--   (warranty_months), so the maker sees "under warranty until ..." without seeing the buyer.
-- * device_access.room: the room a home puts the device in ("living room"). On the access row,
--   not on the device: each home names its own rooms and the maker never sees them (the
--   device_access policy hides other organizations' rows).
-- * products.image: the product picture (PNG / JPEG / WebP, at most 512 KB) shown in the maker
--   panel and in the buyer's app.

ALTER TABLE platform_devices ADD COLUMN activated_at timestamptz;
UPDATE platform_devices SET activated_at = updated_at WHERE lifecycle = 'active';

ALTER TABLE device_access ADD COLUMN room text CHECK (length(room) BETWEEN 1 AND 40);

ALTER TABLE products ADD COLUMN image bytea
    CHECK (octet_length(image) BETWEEN 1 AND 524288);
ALTER TABLE products ADD COLUMN image_type text
    CHECK (image_type IN ('image/png', 'image/jpeg', 'image/webp'));
ALTER TABLE products ADD CONSTRAINT products_image_typed
    CHECK ((image IS NULL) = (image_type IS NULL));
