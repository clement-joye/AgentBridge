# Data Retention

## Objective
Limit retained local state to what is operationally useful while reducing risk from stale or sensitive metadata.

## Retention Scope
- `chat_last_sessions`
- `active_sessions` (stale/error/closed records)
- Future audit/event tables

## Default Policy (Suggested)
- Last session snapshots: 30 days
- Inactive or errored active-session records: 7 days
- Audit logs: 90 days

## Implementation Plan
1. Add config keys:
   - `retention.enabled = true`
   - `retention.last_sessions_days = 30`
   - `retention.active_sessions_days = 7`
   - `retention.audit_days = 90`
2. Add DB cleanup methods with cutoff timestamps.
3. Run cleanup on startup and periodically (for example every 6 hours).
4. Add `/cleanup` admin command (optional) for manual execution.
5. Add metrics/log output for deleted row counts.

## Safety Constraints
- Never delete currently bound active session rows.
- Keep retention disabled only for explicit debugging scenarios.
- Log cleanup operations for traceability.

## Verification
- Unit tests for cutoff logic and exclusion of live bindings.
- Integration test to confirm old seeded data is purged.
- Operational check that DB size remains bounded over time.
