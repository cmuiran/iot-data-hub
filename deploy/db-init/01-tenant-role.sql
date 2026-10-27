-- Runs once when the database volume is created (docker-entrypoint-initdb.d), as the
-- PostgreSQL superuser: the role the hub uses for tenant requests (migration 0009).
CREATE ROLE hub_tenant NOLOGIN;
GRANT hub_tenant TO hub_app;
