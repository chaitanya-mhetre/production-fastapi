// "Booking rush": many clients hitting availability and racing to book the same few slots.
// Results: docs/benchmarks.md. How to run: loadtest/README.md (bootstrap.py + run.sh).
//
//   k6 run -e BASE=http://localhost:58088 -e TOKEN=<tenant admin JWT> \
//          -e SERVICE=<uuid> -e STAFF=<uuid> -e CUSTOMER=<uuid> -e DAY=2030-01-07 \
//          loadtest/k6/booking_rush.js
//
// Optional: -e BROWSE_RPS=50 -e RUSH_RPS=50 -e DURATION=1m (per stage).
// Use a pro-plan tenant: the free plan's rate limit (120/min) would dominate the results.
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const created = new Counter("bookings_created");
const conflicts = new Counter("bookings_conflicted");
// Status breakdown, so "failed" can be split into rate limiting (429/503) vs real errors.
const byStatus = {};
for (const code of [422, 429, 503]) byStatus[code] = new Counter(`status_${code}`);
const server5xx = new Counter("status_5xx_other");
let logged422 = 0;

function track(res) {
  if (byStatus[res.status]) byStatus[res.status].add(1);
  else if (res.status >= 500) server5xx.add(1);
  if (res.status === 422 && logged422 < 3) {
    logged422 += 1;
    console.warn(`422: ${res.body}`);
  }
}

const BROWSE_RPS = Number(__ENV.BROWSE_RPS || 50);
const RUSH_RPS = Number(__ENV.RUSH_RPS || 50);
const DURATION = __ENV.DURATION || "1m";

export const options = {
  scenarios: {
    browse: { executor: "constant-arrival-rate", rate: BROWSE_RPS, timeUnit: "1s",
              duration: `${2 * parseInt(DURATION, 10)}${DURATION.replace(/[0-9]/g, "")}`,
              preAllocatedVUs: 50, exec: "browse" },
    rush: { executor: "ramping-arrival-rate", startRate: 5, timeUnit: "1s", preAllocatedVUs: 100,
            stages: [{ target: RUSH_RPS, duration: DURATION }, { target: RUSH_RPS, duration: DURATION }],
            exec: "book" },
  },
  thresholds: {
    "http_req_duration{scenario:rush}": ["p(95)<300"],
    "http_req_failed{scenario:browse}": ["rate<0.01"],
  },
};

// Unique per run: keys from an earlier run would otherwise replay (or 422 on a different body).
const RUN_ID = __ENV.RUN_ID || `${Date.now()}`;
const headers = { Authorization: `Bearer ${__ENV.TOKEN}`, "Content-Type": "application/json" };

export function browse() {
  const r = http.get(`${__ENV.BASE}/v1/availability?service_id=${__ENV.SERVICE}&date=${__ENV.DAY}`,
                     { headers });
  track(r);
  check(r, { "availability 200": (res) => res.status === 200 });
}

export function book() {
  // Deliberately few distinct slots, so most requests collide: exercises the exclusion constraint.
  const hour = 10 + Math.floor(Math.random() * 4);
  const body = JSON.stringify({ service_id: __ENV.SERVICE, staff_id: __ENV.STAFF,
                                customer_id: __ENV.CUSTOMER, start: `${__ENV.DAY}T${hour}:00:00+05:30` });
  const r = http.post(`${__ENV.BASE}/v1/bookings`, body,
                      { headers: { ...headers, "Idempotency-Key": `k6-${RUN_ID}-${__VU}-${__ITER}` } });
  track(r);
  if (r.status === 201) created.add(1);
  if (r.status === 409) conflicts.add(1);
  check(r, { "201 or 409 (never 500)": (res) => res.status === 201 || res.status === 409 });
}
