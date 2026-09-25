#!/usr/bin/env bash
# End-to-end walkthrough against `make run` (API on :18081). Requires jq.
#   uv run python -m slotwise.cli create-superadmin --email root@example.com --password root-password-1
set -euo pipefail
API=${API:-http://localhost:18081}
j() { curl -sf -H "Content-Type: application/json" "$@"; }

SA=$(j -X POST $API/v1/auth/login -d '{"email":"root@example.com","password":"root-password-1"}' | jq -r .access_token)
j -X POST $API/v1/tenants -H "Authorization: Bearer $SA" -d '{"slug":"demo-clinic","name":"Demo Clinic",
  "plan":"pro","admin_email":"admin@demo.example.com","admin_password":"admin-password-1","admin_name":"Admin"}' >/dev/null
TOKEN=$(j -X POST $API/v1/auth/login -d '{"email":"admin@demo.example.com","password":"admin-password-1",
  "tenant_slug":"demo-clinic"}' | jq -r .access_token)
H="Authorization: Bearer $TOKEN"

SVC=$(j -X POST $API/v1/services -H "$H" -d '{"name":"Consultation","duration_min":30,"price_paise":50000,"buffer_min":10}' | jq -r .id)
STAFF=$(j -X POST $API/v1/staff -H "$H" -d "{\"display_name\":\"Dr Rao\",\"service_ids\":[\"$SVC\"]}" | jq -r .id)
j -X PUT $API/v1/staff/$STAFF/working-hours -H "$H" \
  -d '{"items":[{"weekday":0,"start_time":"09:00","end_time":"17:00"}]}' >/dev/null
CUST=$(j -X POST $API/v1/customers -H "$H" -d '{"name":"Asha","phone":"9876543210","email":"asha@example.com"}' | jq -r .id)

echo "Free slots:"; j "$API/v1/availability?service_id=$SVC&date=2030-01-07" -H "$H" | jq -r '.[0:3][] | .start'
BODY="{\"service_id\":\"$SVC\",\"staff_id\":\"$STAFF\",\"customer_id\":\"$CUST\",\"start\":\"2030-01-07T10:00:00+05:30\"}"
j -X POST $API/v1/bookings -H "$H" -H "Idempotency-Key: walkthrough-0001" -d "$BODY" | jq '{id,status,version}'
echo "Same key again (replayed, no second booking):"
curl -s -D - -o /dev/null -X POST $API/v1/bookings -H "$H" -H "Idempotency-Key: walkthrough-0001" \
  -H "Content-Type: application/json" -d "$BODY" | grep -i idempotent-replayed
