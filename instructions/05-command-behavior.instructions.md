# Command Behavior

## Bootstrap Commands
- `/start`: confirms bot availability for authorized users.
- `/help`: lists supported commands.
- `/ping`: returns hostname health check.

## Discovery and Selection
- `/last`: merged latest sessions from providers, top 10.
- `/use N`: bind chat to discovered session index.
- `/active`: show active session details.

## Messaging
- Non-command message routes to active session if present.
- If no active session exists, instruct `/last` then `/use N`.

## Lifecycle Commands
- `/status`: show runner/session status.
- `/close`: clear active session binding.
- `/close N` (future): close specific running process.
- `/abort` (future): interrupt current task while keeping binding.

## New Session Commands
- `/new`: start new session in active cwd.
- `/new N`: start new session in selected `/last` session cwd.
