# Security Policy

AI Resource Monitor is a **local-first** tool: it runs on your machine and stores everything under the local `data/` directory. No usage data or credentials are ever sent to a third party by this project.

## Credential handling

- **API keys are never written to `data/config.yaml`.** Any key placed there is silently ignored (a guard rejects it at load time). This is by design — configuration is for non-secret settings only.
- Keys are resolved from (in priority order):
  1. Environment variable `UPPER_PROVIDER_API_KEY` (e.g. `DEEPSEEK_API_KEY`), or
  2. The local credential store `data/credentials.json` (file mode `0600`).
- Keys are **never** written to the event ledger (`monitor.db`), never returned by any analytics API, and never sent to the frontend.
- Upstream error responses are passed through a sanitizer (`monitor/sanitize.py`) that strips `Authorization` / `Bearer` tokens / key-like substrings before they reach logs or the dashboard.
- Gateway liveness probes (e.g. `GET /gateway/<provider>/v1`) are recorded as `event_type='rejected'` with no token/cost data and do **not** pollute usage or error metrics.

## Scope

This project is intended for **personal / local** use. Do not deploy the dashboard or gateway on a public, untrusted network without your own authentication layer in front of it — the built-in server has none.

## Reporting a vulnerability

Please report security issues **privately** (do not open a public issue). Contact the maintainer via the repository's private advisory channel, or open a GitHub Security Advisory. Include reproduction steps and affected version (`monitor/__init__.py` → `__version__`).
