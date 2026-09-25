// "Booking rush": many clients hitting availability and racing to book the same few slots.
// NOT yet run: results go to docs/benchmarks.md only after a real run, with hardware noted.
//
//   k6 run -e BASE=http://localhost:58088 -e TOKEN=<tenant admin JWT> \
//          -e SERVICE=<uuid> -e STAFF=<uuid> -e CUSTOMER=<uuid> -e DAY=2030-01-07 \
//          loadtest/k6/booking_rush.js
//
// Use a pro-plan tenant: the free plan's rate limit (120/min) would dominate the results.
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";

const created = new Counter("bookings_created");
const conflicts = new Counter("bookings_conflicted");

export const options = {
  scenarios: {
    browse: { executor: "constant-arrival-rate", rate: 50, timeUnit: "1s", duration: "2m",
              preAllocatedVUs: 50, exec: "browse" },
    rush: { executor: "ramping-arrival-rate", startRate: 5, timeUnit: "1s", preAllocatedVUs: 100,
            stages: [{ target: 50, duration: "1m" }, { target: 50, duration: "1m" }], exec: "book" },
  },
  thresholds: {
    "http_req_duration{scenario:rush}": ["p(95)<300"],
    "http_req_failed{scenario:browse}": ["rate<0.01"],
  },
};

const headers = { Authorization: `Bearer ${__ENV.TOKEN}`, "Content-Type": "application/json" };

export function browse() {
  const r = http.get(`${__ENV.BASE}/v1/availability?service_id=${__ENV.SERVICE}&date=${__ENV.DAY}`,
                     { headers });
  check(r, { "availability 200": (res) => res.status === 200 });
}

export function book() {
  // Deliberately few distinct slots, so most requests collide: exercises the exclusion constraint.
  const hour = 10 + Math.floor(Math.random() * 4);
  const body = JSON.stringify({ service_id: __ENV.SERVICE, staff_id: __ENV.STAFF,
                                customer_id: __ENV.CUSTOMER, start: `${__ENV.DAY}T${hour}:00:00+05:30` });
  const r = http.post(`${__ENV.BASE}/v1/bookings`, body,
                      { headers: { ...headers, "Idempotency-Key": `k6-${__VU}-${__ITER}` } });
  if (r.status === 201) created.add(1);
  if (r.status === 409) conflicts.add(1);
  check(r, { "201 or 409 (never 500)": (res) => res.status === 201 || res.status === 409 });
}
