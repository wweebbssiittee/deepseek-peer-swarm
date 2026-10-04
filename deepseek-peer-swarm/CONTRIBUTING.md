# Contributing

Bug reports, focused fixes, tests and documented experiments are welcome.
For larger changes, open an issue first to explain the problem and approach.

## Development

Follow the setup instructions in [README.md](README.md) and the design constraints
in [AGENTS.md](AGENTS.md). Keep all ten peers equal, enforce permissions in execution
code and preserve exact private tool-call/result histories.

Git is needed by the repository integration tests. Before opening a pull request, run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\check_publication.py
.\.venv\Scripts\python.exe -m pip check
```

Use temporary state and fake providers for tests. Never include API keys, access
tokens, runtime databases, real task transcripts or personal paths in code,
issues, screenshots or test fixtures. Use the offline browser demo for UI checks.

Explain what changed, why, and how you checked it. Include a regression test for a
behavioral bug fix where practical. Contributions are under the project's
[MIT license](LICENSE); third-party code must retain any required attribution.
