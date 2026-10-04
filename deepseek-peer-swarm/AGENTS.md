# Working on this harness

This is the standalone general-purpose peer swarm. Keep changes within this
repository. Keep all ten peers equal; do not
introduce a privileged manager, fixed architect role, or hidden model coordinator.

Use the project's virtual environment (`.venv/Scripts/python.exe` on Windows).
Run `python -m pytest -q` after meaningful backend changes. Preserve exact
tool-call/result pairings and DeepSeek `reasoning_content` in private histories.

Never print, commit, expose in API responses, or copy real API keys. Runtime state
lives under LOCALAPPDATA/DeepSeekPeerSwarm, outside the source tree. Test with
temporary state directories and fake providers. Model calls require user keys
and should only occur for an explicitly started task.

Permission checks belong in execution code, not only prompts. Shell permission is
broad OS access; never describe the built-in path checks as a process sandbox.
Keep pause/cancel behavior, uncertain action recovery, token reservations, and
stale-response revision checks intact. Never replay interrupted external actions.

The vanilla browser frontend requires no build pipeline or CDN. Escape user/model
text before inserting HTML; never store keys in browser storage. The server must
bind to loopback and retain mutation tokens, origin checks and host validation.
