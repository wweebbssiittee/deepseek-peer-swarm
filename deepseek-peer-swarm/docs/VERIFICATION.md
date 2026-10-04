# Verification - October 4, 2026

Environment: Windows, Python 3.13, the existing project virtual environment.
The clean source checkout was tested without reading the application's real
runtime database or key vault and without paid model calls.

- Automated suite: **271 passed from the extracted GitHub source archive**. Two third-party test-client deprecation
  warnings; no failures or skipped tests in the final run.
- Dependency consistency: `python -m pip check` passed.
- Publication scan: passed for source, tests, documentation and CI configuration.
  Three included screenshots were also visually reviewed.
- Source packaging: 52 files, including MIT license and GitHub workflow. ZIP
  integrity and each archived file's SHA-256 were checked against the source;
  a checksum file accompanies the archive. The extracted source also passed
  the publication scan. Python dependencies came from the existing environment.
- A separate privacy review inspected all 49 text files and all three screenshots
  directly from the archive. No real credentials, identifying contact details,
  personal home paths or private task content were identified. Credential-looking
  test values are synthetic. ZIP metadata is normalized; PNG files contain only
  image chunks, with no text/EXIF metadata or trailing data.
- Installed Chrome via Playwright: desktop and 390px mobile layouts, ten peer
  cards, task creation, pause/resume/stop, chat, board/activity, permission changes,
  persisted run limits, approval buttons, budget display and sound controls passed.
  No JavaScript errors or external browser requests occurred.
- The browser fixture generated 49 synthetic provider responses. All model usage
  and costs were synthetic/zero; file, network and command actions were not used.
  Approval UI checks used intercepted responses. Actual command denial, approval,
  cancellation and timeouts are covered separately in automated toolbox tests.

## Reproduce

From the project root, after installing the pinned requirements:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts\check_publication.py
```

Optional browser verification uses Playwright as a development-only dependency
and an installed Chrome executable:

```powershell
.\.venv\Scripts\python.exe -m pip install playwright
.\.venv\Scripts\python.exe scripts\browser_smoke.py --demo
```

Use `--browser` for a different Chrome location. The script creates and removes
its own temporary state, substitutes ten fake credentials and a fake provider,
and blocks external browser requests. It never connects to an existing swarm
instance. Screenshots go to the ignored `.browser-check/` directory; exposed
workspace paths are replaced with `demo-workspace` in the browser fixture only.

## Regression coverage added during publication review

- Recursive redaction for settings, board/results, memory, events, status errors,
  task responses, exports, cost records and A2A responses.
- Redaction of JSON-escaped credentials, overlapping values and keys rotated in
  the same settings update; unknown settings fields are not exposed.
- Exact private model histories and executable approval payloads remain intact.
- Failed reservation/settlement commits restore live accounting state before
  error handlers save it. Restart recovery does not replay provider calls.
- Environment-provided credentials are removed from tests; browser fixture
  isolation and removal of its temporary state are checked.
- The publication guard checks tracked files even when subsequently ignored and
  reports credential/path findings without displaying the matched values.
- Source-archive scanning falls back to the filesystem when Git is not installed.
- Unrelated top-level folders are rejected without storing local project names
  in the publication rules.

## Limits of this verification

This verifies local behavior with fake providers and mocked HTTP transports.
It does not establish real DeepSeek response quality, current model availability,
provider prices, live billing, deployment behavior, or autonomous task success.
The pricing snapshot's original verification date is retained; this review did
not revalidate provider prices. GitHub Actions has been configured but has not
run remotely. No repository history existed in the reviewed source folder.

The publication scanner is a heuristic, not proof that arbitrary text or images
contain no personal information. Private runtime histories remain local data and
must never be published. See the [publication review](PUBLICATION_REVIEW.md).
