# Daily Unbooked-Call Analysis

Automated daily pipeline: pull yesterday's inbound calls from the phone system,
transcribe the genuine unbooked leads, classify *why* each didn't book, reconcile
against ServiceTitan to catch phantom-recoveries, and email a PE-style morning brief.

## Pipeline
```
list_calls -> prefilter noise -> transcribe (Whisper) -> classify (6 buckets)
           -> reconcile outcomes (vs ServiceTitan jobs) -> build brief -> SendGrid
```

## Run
```bash
set -a && . /workspace/.secrets/hermes.env && set +a
set -a && . /workspace/context/company/secrets/sendgrid.env && set +a
cd /workspace/apps/lex-servicetitan-reporting

# yesterday, send live:
python -m unbooked_analysis.run_daily --tenant lex_portfolio

# specific day, no send (writes artifacts + prints subject):
python -m unbooked_analysis.run_daily --tenant lex_portfolio --date 2026-07-28 --dry-run

# cheap test (first N calls):
python -m unbooked_analysis.run_daily --tenant lex_portfolio --date 2026-07-28 --dry-run --limit 5
```

## Reason taxonomy (mutually exclusive — exactly one bucket per call)
- **A** not_a_true_lead — existing cust / warranty / recall / membership / spam / vendor *(noise, excluded from leak rate)*
- **B** process_failure 🔴 — AI callback-punt, CSR never booked, dropped, rang out/vm *(OUR fault, recoverable)*
- **C** capacity — wanted sooner than offered / no slot in window
- **D** customer_declined — price objection / booked-then-canceled / chose competitor
- **E** geo_scope — out of area / out of scope

## Outcome layer (reconciliation)
Every unbooked call gets a final outcome so we don't over-report losses:
- **booked_on_call** — job attached directly to the call
- **recovered** — no appt on the call, but a job booked / outbound callback happened within the
  window (default 72h). Solves the "AI punted, staff called back and booked a fresh job that was
  never linked to the original call" false-leak problem.
- **still_unbooked** — the true leak. Only B–E still_unbooked count as leaks.

Reconciliation is 3-tier: (1) direct `leadCallId` job link, (2) customer's jobs created after the
call within window, (3) phone fallback (outbound callback — flagged low-confidence).
Outcome ALWAYS comes from ServiceTitan job data, even when the call source is 8x8.

## Call-source abstraction
`sources/base.py` defines `CallSource` (list_calls / get_recording / find_recovery).
- `sources/servicetitan.py` — live (Telecom API v2 + JPM jobs)
- `sources/eightbyeight.py` — STUB; wire when LEX moves to 8x8. Booking outcome still reconciles
  against ServiceTitan.

## Tenants
`tenants.py` registry. v1: single entry `lex_portfolio` covering all brands in ST tenant
1498628772 (no brand filter — all unbooked, campaign tagged). Add Champions tenants as new
registry entries for the rollout.

## Cost (measured)
~4–6 real unbooked leads/day after noise pre-filter, ~24 min audio/day → Whisper ≈ **$0.14/day
(~$4/mo)** for LEX/Lyons/ETX combined.

## Cron
Fires daily 09:00 America/Chicago. Managed via Hermes cronjob (see cron listing).
