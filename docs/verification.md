# v0.2 release verification

Historical record for 877c6b4. See [v0.3 verification](hardening.md) for the current release.

Verified in the cloud workspace on 2026-11-17, using PostgreSQL 16, Python 3.12
and the checked-in Compose topology.

| Check | Result |
|---|---|
| Ruff | Passed for app, examples, tests, scripts and migrations |
| Automated suite | 30 passed; real PostgreSQL and independent HTTP provider |
| Container build | Passed with TLS verification enabled and the cloud CA supplied as an ephemeral BuildKit secret |
| Live workflow | Two ordered steps succeeded; payloads, event sequence, resumed cursor and API/worker metrics checked |
| API browser | Chromium submitted a two-step run through Swagger and observed both steps succeed; no page errors or external asset requests |
| Browser viewports | 1440 × 1000 and 390 × 844 screenshots retained |
| Local model | Zero-shot 3/9 correct; few-shot 4/9 correct; each run completed 9/9 workflows |
| Storage migration | Existing 47 runs preserved when copying the stopped anonymous PostgreSQL store into named storage; original volume and backup retained |
| Compose teardown/startup | A completed run snapshot and its independent provider effect remained identical after down and up, without deleting volumes |

Raw model, browser and persistence observations are in [evidence](../evidence/).
The model evaluation is a nine-case synthetic development set, not an unseen
benchmark. The execution tests do not establish production throughput or uptime.

GitHub Actions runs lint, the automated suite and a live Compose workflow on
pushes and pull requests. Use the successful run matching the source commit when
reviewing a release; a local pass alone is not a remote CI result.

The website entry is prepared separately for review. Source publication does not
deploy the website or expose this local API.
