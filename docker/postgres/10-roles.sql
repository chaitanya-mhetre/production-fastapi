-- Runs once when the postgres container is first created (docker-entrypoint-initdb.d).
-- In AWS these roles are created by infrastructure (Terraform), not by app migrations.
--
--   slotwise         owner: runs migrations, owns tables (table owners bypass RLS unless FORCE is set)
--   slotwise_app     the API: RLS is ENFORCED for this role
--   slotwise_worker  relay + Celery workers: BYPASSRLS, because they process queues across tenants
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'slotwise_app') THEN
    CREATE ROLE slotwise_app LOGIN PASSWORD 'slotwise_app' NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'slotwise_worker') THEN
    CREATE ROLE slotwise_worker LOGIN PASSWORD 'slotwise_worker' BYPASSRLS;
  END IF;
END
$$;

CREATE DATABASE slotwise_test OWNER slotwise;
