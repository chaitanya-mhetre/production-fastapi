#!/usr/bin/env bash
# Runs ON the EC2 host (invoked by the deploy workflow through SSM). Not yet exercised against
# real AWS: see README.md in this folder.
#
#   deploy.sh <image-tag>
#
# 1. render env from SSM Parameter Store   2. pull image   3. run migrations (expand-only)
# 4. roll api-a then api-b (Nginx keeps serving from the other one)   5. restart workers
set -euo pipefail

TAG="${1:?usage: deploy.sh <image-tag>}"
cd /opt/slotwise
REGISTRY="$(cat registry)"                 # written by the bootstrap script
export IMAGE="${REGISTRY}/slotwise:${TAG}"

aws ssm get-parameters-by-path --path /slotwise/prod --with-decryption \
  --query 'Parameters[].[Name,Value]' --output text \
  | awk '{ sub(".*/", "", $1); print $1 "=" $2 }' > .env.prod
chmod 600 .env.prod

aws ecr get-login-password | docker login --username AWS --password-stdin "${REGISTRY}"
docker pull "${IMAGE}"
echo "${TAG}" > previous_tag.new

# Migrations are backwards compatible (expand/contract), so old and new code can run side by side.
docker compose -f docker-compose.prod.yml run --rm migrate

for svc in api-a api-b; do
  docker compose -f docker-compose.prod.yml up -d --no-deps "${svc}"
  for _ in $(seq 1 30); do
    if docker compose -f docker-compose.prod.yml exec -T "${svc}" \
         python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/readyz')"; then
      break
    fi
    sleep 2
  done
done
docker compose -f docker-compose.prod.yml up -d --no-deps worker beat relay nginx

mv previous_tag.new current_tag
echo "deployed ${TAG}"
