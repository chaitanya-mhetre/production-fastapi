# Deploying Slotwise to AWS

> **Status: designed and scripted, not yet executed.** The infrastructure (VPC, EC2, RDS, IAM, ECR)
> is owned by the separate `cloud-infra-lab` Terraform project. Nothing here has run against real AWS,
> and no public endpoint exists yet. Everything in this README is the plan, not a record of a deployment.

## Target layout: low-cost mode (one EC2 host)

```
Internet ─▶ Nginx on EC2 (TLS via Let's Encrypt) ─▶ api-a / api-b (uvicorn)
                                                     │
            worker (Celery) · beat · relay ──────────┤
                                                     ▼
                     RDS PostgreSQL 16 ─ ElastiCache Redis (or a Redis container) ─ S3 ─ SES
```

| Concern | Choice | Why |
|---|---|---|
| Compute | 1× EC2 (t4g.small) + Docker Compose | cheapest way to run the full topology; ECS Fargate is the scale-up path |
| Database | RDS PostgreSQL 16 (db.t4g.micro) | managed backups/patching; `btree_gist` + RLS are supported |
| Redis | a container on the host (low-cost) → ElastiCache later | broker + rate limits; losing it loses no data (Postgres is the source of truth) |
| Secrets | SSM Parameter Store (SecureString) under `/slotwise/prod/*` | rendered to `.env.prod` at deploy time, never in git or the image |
| Email | SES SMTP interface | same `SmtpMailer` code path as Mailpit locally |
| Uploads | S3 bucket, private, presigned POST | the API never proxies file bytes |
| CI → AWS auth | GitHub OIDC → IAM role | no long-lived AWS keys stored in GitHub |
| Logs | `awslogs` driver → CloudWatch Logs | JSON logs are queryable with Logs Insights |

**Cost estimate (TBD, verify with the AWS Pricing Calculator before running):** EC2 t4g.small + RDS
db.t4g.micro + 20 GB storage is expected to be a small monthly amount, but the real figure hasn't been
calculated. The plan is to tear everything down after the demo (`terraform destroy`).

## Database roles on RDS
The RDS master user plays the `slotwise` owner role (runs migrations). Create the two app roles once:
```sql
CREATE ROLE slotwise_app LOGIN PASSWORD '<from SSM>' NOBYPASSRLS;
CREATE ROLE slotwise_worker LOGIN PASSWORD '<from SSM>';
GRANT rds_superuser TO slotwise;          -- only if needed for extensions
-- BYPASSRLS on RDS: grant via the master user; if unavailable, give slotwise_worker
-- explicit "TO slotwise_worker USING (true)" policies instead (documented trade-off).
```

## Deploy flow (`.github/workflows/deploy.yml` → `deploy.sh`)
1. Build the image, push to ECR tagged with the commit SHA.
2. SSM `send-command` runs `/opt/slotwise/deploy.sh <sha>` on the host.
3. The script renders `.env.prod` from SSM, pulls the image, runs `alembic upgrade head`.
4. It restarts `api-a`, waits for `/readyz`, then `api-b`. Nginx keeps routing to whichever is up
   (`max_fails` + `proxy_next_upstream`), so the deploy is zero-downtime **in design** (not yet verified).
5. It restarts worker, beat and relay. Celery `acks_late` means tasks interrupted by the restart are redelivered.
6. The workflow runs `scripts/smoke.py` against the public URL as a post-deploy gate.

## Migrations: expand / contract
Old and new code run side by side during a rollout, so every migration must work with both:
- **Expand** (deploy N): add nullable columns / new tables / new indexes (`CREATE INDEX CONCURRENTLY`).
- Deploy code that writes both shapes and reads the new one.
- **Contract** (deploy N+1): drop the old column once nothing reads it.
Never rename or drop in the same deploy that stops using something.

## Rollback
Re-run the deploy workflow with `image_tag = <previous sha>` (stored in `/opt/slotwise/current_tag`
before each deploy). Because migrations are expand-only per deploy, the previous image still works
against the migrated schema. **Rollback hasn't been tested yet.** It's on the M7 checklist for when the
infrastructure exists.

## TLS
`certbot certonly --webroot` on the host; `nginx.prod.conf` = `nginx/nginx.conf` plus a `listen 443 ssl`
server block, HSTS, and an 80→443 redirect. The alternative is an ALB with an ACM certificate (costs more, and
removes certificate handling from the box).
