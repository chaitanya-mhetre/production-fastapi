# ADR 0001: The database, not the application, prevents double booking

**Status:** accepted

**Context.** Two requests for the same staff member and time can arrive concurrently. A read-then-insert
check ("is it free? then insert") is a race: both requests read "free".

**Options.**
1. App-level lock (Redis `SET NX` per staff/slot): fast, but a second system to keep correct; locks can expire mid-request.
2. `SERIALIZABLE` isolation: correct, but retries on serialization failures across the whole transaction.
3. `SELECT … FOR UPDATE` on the staff row: serialises all bookings for one staff member; simple but coarse.
4. **Exclusion constraint** `EXCLUDE USING gist (staff_id WITH =, period WITH &&) WHERE (active)`.

**Decision.** Option 4. Postgres guarantees no two active bookings overlap, atomically, with no extra
infrastructure. The reserved `period` includes the service buffer, so buffers are enforced too.

**Consequences.** Error code `23P01` must be mapped to `409 slot_taken`. Needs `btree_gist`.
Cancelled bookings drop out of the constraint through the partial `WHERE` clause.
