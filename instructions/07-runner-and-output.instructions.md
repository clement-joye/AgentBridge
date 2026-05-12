# Runner and Output

## Runner Responsibilities
- Start provider-specific process in correct `cwd`.
- Attach PTY (`pexpect`) where possible.
- Send user prompt text.
- Detect completion or timeout.
- Return concise final output suitable for Telegram.

## Output Policy
Default Telegram response should include:
- Completion marker (`Done.`)
- Summary
- Changed files list (when detectable)
- Useful next commands

## Trace Mode (future)
- `/trace off`: final summaries only.
- `/trace on`: include tool/command/file-change progress.

## Content Safety
- Do not expose private chain-of-thought.
- Prefer summaries of tool activity and results.
