-- Rollback of 0015_maker_home (python -m hub.storage.migrate --rollback 0015_maker_home).
-- Rooms, activation dates and product pictures are lost.

ALTER TABLE products DROP CONSTRAINT products_image_typed;
ALTER TABLE products DROP COLUMN image_type;
ALTER TABLE products DROP COLUMN image;
ALTER TABLE device_access DROP COLUMN room;
ALTER TABLE platform_devices DROP COLUMN activated_at;
