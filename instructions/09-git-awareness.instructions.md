# Git Awareness

## Read-Only Commands (Initial)
- `/git`: repo, branch, and summary status.
- `/files`: changed file list.
- `/diff`: chunked diff summary for Telegram limits.

## Design Goals
- Help users verify changes before further prompts.
- Keep responses concise and scannable on mobile.
- Avoid large raw dumps by default.

## Future Write Commands
- `/commit "message"`
- `/revert`
These should remain disabled until safety and approval flow are mature.
