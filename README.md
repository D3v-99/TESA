# TESA Paywall & Access-Control Tester

A small Streamlit app for **authorized** testing of your own video SaaS
platform (a Phoenix LiveView app behind a paywall). It sends a handful of
*unauthenticated* GET requests to check whether endpoints that should require
login are actually protected, and reports the results as a simple
vulnerability dashboard.

It is a read-only access-control checker: no exploitation, no brute force, no
destructive actions. **Only use it against a domain you own or have explicit
written permission to test.**

## What it checks

1. **UUID Discovery** — fetches `/posts` and extracts UUIDs from the HTML (info only).
2. **Preview Image Leak** — `GET /posts/{uuid}/preview.jpg` → vulnerable if `200`.
3. **Paywall / Unlock Bypass** — `GET /unlock/{uuid}` (no redirects) → vulnerable if `200`, secure if it redirects to log-in or returns `401/403`.
4. **Direct Media Access** — tries `/uploads/videos/{uuid}.mp4`, `.m3u8`, and `/media/{uuid}.mp4` → vulnerable if any returns `200`.

## Install

```bash
pip install streamlit requests beautifulsoup4
```

(Optionally use a virtual environment first: `python -m venv .venv && source .venv/bin/activate`.)

## Run

```bash
streamlit run app.py
```

Streamlit opens the app in your browser (default: http://localhost:8501).

## Use

1. Read the authorization banner.
2. Enter your target base URL (e.g. `https://tesa.co.ke`).
3. Tick **"I confirm I am authorized to test this URL"** — this enables the button.
4. Click **Run Tests** and read the report. Remediation advice is at the bottom.

Findings are HTTP-status signals: confirm each one manually before acting.
