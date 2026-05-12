# Safety and Permissions

## Mandatory Controls
- Telegram user allowlist
- Telegram chat allowlist
- Allowed repository roots
- Blocked path denylist
- Command timeout
- Process kill switch
- Audit logging

## Path Policy
- All session cwd values must pass allowed-root checks.
- Blocked paths always win over allowed roots.
- Non-existent paths are rejected.

## Approval Flow (Future)
For risky operations, bot can request explicit approval from Telegram.
Initial MVP should not allow destructive remote command approvals.

## Threat Model Focus
- Unauthorized Telegram access
- Prompt-injected path escalation
- Accidental destructive execution
- Exposure of local secrets and credentials
