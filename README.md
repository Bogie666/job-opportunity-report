# Job Opportunity Report

LEX Air / Lyons internal tool: pull each scheduled ServiceTitan job, score it for opportunity potential, render a Customer Opportunity Report Card, and email the highest-value cards in a navy/gold HTML layout so techs can prep before the visit.

## What it does

1. **Pull** — every job scheduled for tomorrow (HVAC only by default) is pulled from ServiceTitan read-only with full dossier: equipment, memberships, estimates, invoices, past jobs, notes, photos.
2. **Score** — a deterministic scorer ranks each job on equipment age, membership status, open/sold estimate dollars, photo availability, and booking-note repair signals. Follow Up call types are excluded by default (those are jobs already returning to perform sold work).
3. **Render** — top-N jobs get a Customer Opportunity Report Card with sections for Customer Profile, Buying Behavior, Visual Inspection Review, Primary Opportunities, AI Coaching Notes, and Overall Score. Equipment ages are decoded from serials with a brand-aware decoder. 10+ year HVAC components and water heaters get a red replacement flag.
4. **Email** — combined navy/gold HTML email goes to the team, with each card attached as `.md` and `.html`.

## Project layout

```
src/
  client.py                          — ServiceTitan OAuth client
  servicetitan_dossier.py            — fetch scheduled jobs + build cached job dossiers
  report_card_facts.py               — shared fact extraction used by report cards
  customer_opportunity_report_card.py — report card renderer (locked navy/gold format)
  render_report_card_email.py        — inline-styled HTML email renderer
  serial_decoder.py                  — brand-aware HVAC serial → year decoder
  cad_home_age.py                    — free-source home-age resolver

scripts/
  pull_hvac_tomorrow.py              — one-shot: pull all HVAC scheduled jobs
  fetch_job_photos.py                — download attachments + build contact sheets
  analyze_photo_sheets_openrouter.py — vision review (OpenRouter)
  analyze_selected_photos.py         — vision review on a hand-picked subset
  score_and_filter_hvac_briefs.py    — score jobs and pick top-N for emailing
  pull_job_by_number.py              — pull a single job by number

tests/
  test_customer_opportunity_report_card.py
  test_photo_vision_integration.py
  test_serial_decoder.py
```

## Serial decoder coverage

Brand-aware year-of-manufacture decoder, validated 120/124 (97%) against ServiceTitan install dates on the June 2026 HVAC schedule.

| Family | Includes | Format |
|---|---|---|
| Carrier | Carrier, Bryant, Payne, Heil, Tempstar, Comfortmaker, Day & Night, Arcoaire, ICP, KeepRite | WWYY |
| Goodman | Goodman, Amana, Daikin USA, Janitrol | YYMM |
| Trane | Trane, American Standard | YYWW |
| Lennox | Lennox, Armstrong Air, AirEase, Ducane, Concord, Magic-Pak | WWYY or plant+YY |
| Rheem | Rheem, Ruud, Weatherking, Eemax | letter+WWYY |
| ADP | ADP evaporator coils | position 3-4 = YY |

Unsupported brands (York/Coleman, Mitsubishi/Fujitsu/LG/Samsung minisplits, Nortek/Nordyne) render "verify nameplate year" rather than fabricating.

## Report card rules (locked 2026-06-09)

- Visual layout: navy `#1a3a5c` header + gold `#DAA520` action bar + white cards. Inline styles only (Gmail strips `<style>` blocks).
- Equipment age line is always present in CUSTOMER PROFILE.
- 10+ year HVAC components OR water heaters emit a red ⚠ FLAG: "{label} ~{age} yrs — over 10-yr replacement threshold; verify nameplate and discuss replacement planning."
- Photo vision findings must cite specific visible evidence; empty findings are allowed (and preferred over fabricated ones).
- Report cards reason from the actual job notes — if the customer called about specific recommended repairs, the visit is framed as a pricing/confirmation visit, not a generic age-driven discovery call.

## Daily run

```bash
# 1. Pull all HVAC scheduled jobs for tomorrow
python scripts/pull_hvac_tomorrow.py

# 2. Download photos and build contact sheets
python scripts/fetch_job_photos.py data/tech_briefs/<STAMP>

# 3. Score and pick top opportunities (Follow Up excluded by default)
python scripts/score_and_filter_hvac_briefs.py data/tech_briefs/<STAMP> \
  --manifest data/tech_briefs/<STAMP>_photos/manifest.json \
  --threshold 40 --top-n 8 --exclude "Follow Up"

# 4. Run vision on the selected subset only
python scripts/analyze_selected_photos.py \
  --manifest data/tech_briefs/<STAMP>_photos/manifest.json \
  --selected data/tech_briefs/<STAMP>_scoring/selected.json

# 5. Render and email — see deliver step in scripts/deliver_report_cards.py (not yet committed)
```

## Environment

Required env vars (load from your own secrets vault):

```
SERVICETITAN_CLIENT_ID
SERVICETITAN_CLIENT_SECRET
SERVICETITAN_APP_KEY
SERVICETITAN_TENANT_ID
SERVICETITAN_TIMEZONE
SENDGRID_API_KEY
SENDGRID_FROM_EMAIL
SENDGRID_FROM_NAME
OPENROUTER_API_KEY
```

Never commit `.env`, `secrets/`, or anything under `data/` — ServiceTitan dossiers contain real customer PII.

## Tests

```bash
python -m pytest tests/ -q
```

## License

Internal LEX Air / Lyons use only. Not for redistribution.
