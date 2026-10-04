# Publication review - October 4, 2026

The public source copy contains application code, tests, documentation, launch
scripts, an automated check workflow and three screenshots from synthetic data.
Local environments, runtime data and unrelated projects are excluded.

## Findings and changes

1. **Source and runtime separation.** Workspaces, runtime databases, generated
   output and backups must stay outside the public source package. Screenshots
   use temporary synthetic state and generic workspace paths.
2. **Incomplete runtime credential redaction.** Public work-board results, agent
   memory, settings and exports could return configured credentials embedded in
   model/user text. Public output now receives recursive, non-mutating redaction;
   relevant text is also sanitized before ordinary persistence. Settings loading
   accepts only supported fields. Exact private provider histories and original
   command arguments remain private and unmodified.
3. **Accounting inconsistency after storage failure.** Failed reservation and
   settlement transactions could leave modified in-memory counters that a later
   save persisted without matching ledger records. The changes restore counters
   when the transaction fails, with regressions exercising the actual workers
   and subsequent restart recovery.
4. **Unsafe portfolio verification workflow.** The earlier browser script could
   opt into a paid live task. The new script uses a disposable loopback instance
   and fake provider, with no live-server option. It generates labeled synthetic
   screenshots with generic workspace paths.
5. **Publication hygiene.** Documentation is portable. Ignore rules cover key
   vaults, databases, backups and runtime files. A reusable publication check
   rejects unexpected top-level folders, and Windows CI runs the checks.

No real hard-coded API credential was identified in the retained source during
this review. Credential-shaped values in tests are synthetic. The package
contains no runtime databases, credential vaults, saved access tokens or real
task transcripts. Runtime state outside the source folder was not opened.

## Before publishing

- The source package now includes the MIT license. The suggested repository name
  is `deepseek-peer-swarm`; no remote repository has been created.
- Publish this clean source copy, after rerunning the publication check. Do not
  upload the parent working directory or local runtime storage.
- Review any new screenshots and examples for personal paths, task content and
  contacts. If later importing an existing Git history, scan that history too.

The [verification record](VERIFICATION.md) documents tests and practical limits.
