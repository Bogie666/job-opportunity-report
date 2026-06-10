# Job Opportunity Report

LEX Air / Lyons internal tool: pull each scheduled ServiceTitan job, score it for opportunity potential, render a Customer Opportunity Report Card, and email the highest-value cards in a navy/gold HTML layout so techs can prep before the visit.

## What it does

1. **Pull** — every job scheduled for tomorrow (HVAC only by default) is pulled from ServiceTitan read-only with full dossier: equipment, memberships, estimates, invoices, past jobs, notes, photos.
2. **Score** — a deterministic scorer ranks each job on tiered equipment-age flags, home-age tiers, membership status, open/sold estimate dollars, photo availability, and booking-note repair signals. Follow Up call types are excluded by default (those are jobs already returning to perform sold work). The scorer is the gate that decides which jobs get LLM report-card spend.
3. **Render** — top-N jobs get a Customer Opportunity Report Card. Architecture: *facts by rules, narrative by LLM, validated by rules*.
   - Python computes every fact: equipment ages (brand-aware serial decoder), home age (CAD), estimate intent states and dollars, memberships, tiered replacement flags. These are never produced by a model.
   - An LLM (`src/report_card_llm.py`) fills the narrative slots — buying behavior, opportunity selection/framing, coaching notes, score rationale — as structured JSON for this specific customer.
   - A deterministic validator audits the JSON against the facts context: every dollar figure, year, and ID must exist in the context; equipment mentions are checked against the on-file/likely-but-verify whitelist. One correction retry, then offending lines are scrubbed; if unusable, a generic rule-based fallback fills the same slots, so the pipeline never blocks on the LLM.
   - The locked section layout, header facts, equipment-age line, and ⚠ FLAG bullets are always rendered by Python — the model can neither drop nor alter them.
4. **Email** — combined navy/gold HTML email goes to the team, with each card attached as `.md` and `.html`.
5. **Learn** — `scripts/log_outcomes.py` runs about a week behind each scoring run and appends realized revenue (invoices + estimates sold on the job) per scored job to `data/outcomes/outcomes.csv`, so scorer weights can be calibrated against reality and report-card conversion can be measured.

## Project layout

```
src/
  client.py                          — ServiceTitan OAuth client
  servicetitan_dossier.py            — fetch scheduled jobs + build cached job dossiers
  report_card_facts.py               — shared fact extraction used by report cards
  opportunity_flags.py               — tiered equipment-age + home-age trigger rules
  report_card_llm.py                 — LLM narrative synthesis + grounded-facts validator
  customer_opportunity_report_card.py — report card derivation + renderer (locked navy/gold format)
  render_report_card_email.py        — inline-styled HTML email renderer
  serial_decoder.py                  — brand-aware HVAC serial → year decoder
  cad_home_age.py                    — free-source home-age resolver

scripts/
  pull_hvac_tomorrow.py              — one-shot: pull all HVAC scheduled jobs
  fetch_job_photos.py                — download attachments + build contact sheets
  analyze_photo_sheets_openrouter.py — vision review (OpenRouter), grounded with per-job ST context
  analyze_selected_photos.py         — vision review on a hand-picked subset
  score_and_filter_hvac_briefs.py    — score jobs and pick top-N for emailing
  log_outcomes.py                    — append realized revenue per scored job (calibration loop)
  pull_job_by_number.py              — pull a single job by number

tests/
  test_customer_opportunity_report_card.py
  test_opportunity_flags.py
  test_report_card_llm.py
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

## Report card rules (locked layout 2026-06-09 · tiered triggers 2026-06-10)

- Visual layout: navy `#1a3a5c` header + gold `#DAA520` action bar + white cards. Inline styles only (Gmail strips `<style>` blocks).
- Equipment age line is always present in CUSTOMER PROFILE, with the age source (installed date / serial decode + confidence / booking note).
- Tiered replacement flags (`src/opportunity_flags.py`), rendered red:

  | Equipment class | Flag at | Urgent (past service life) at |
  |---|---|---|
  | HVAC (condenser, heat pump, coil, air handler, mini-split, RTU) | 10 yrs | 15 yrs |
  | Furnace | 15 yrs | 20 yrs |
  | Tank water heater | 8 yrs | 12 yrs |
  | Tankless water heater | 15 yrs | 20 yrs |

- Home-age tier triggers: 10–15 yrs (builder-grade first failure window), 15–30 (first replacement cycle, duct/IAQ), 30–45 (panel capacity, second HVAC cycle, supply lines), 45+ (full cross-trade inspection justified).
- Flags are deterministic and always rendered by Python; the LLM receives them as mandatory context and builds framing around them but cannot drop, soften, or alter them.
- LLM narrative is audited: every dollar figure, year, and estimate/invoice/job ID must exist in the facts context; equipment not on file may only appear as "not on file, verify on arrival"; OPEN/EXPIRED estimates are "quoted, never closed", never "customer interest".
- Photo vision findings must cite specific visible evidence; empty findings are allowed (and preferred over fabricated ones). Historical photos render as verify-on-arrival prompts.
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

# 4. Run vision on the selected subset only, grounded with per-job ST context
python scripts/analyze_selected_photos.py \
  --manifest data/tech_briefs/<STAMP>_photos/manifest.json \
  --selected data/tech_briefs/<STAMP>_scoring/selected.json \
  --briefs-dir data/tech_briefs/<STAMP>

# 5. Render report cards (LLM synthesis when OPENROUTER_API_KEY is set; --no-llm forces fallback)
python src/customer_opportunity_report_card.py data/tech_briefs/<STAMP>/<JOB>.json \
  --vision-summary data/tech_briefs/<STAMP>_photos/vision/<JOB_ID>.json --out out/<JOB>.md

# 6. Email — see deliver step in scripts/deliver_report_cards.py (not yet committed)

# 7. ~1 week later: log realized outcomes for scorer calibration
python scripts/log_outcomes.py data/tech_briefs/<STAMP>_scoring
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

Optional knobs:

```
LEX_REPORT_CARD_MODEL     — OpenRouter model for report-card synthesis (default: LEX_BRIEF_MODEL or openai/gpt-4o)
LEX_REPORT_CARD_USE_LLM=0 — force the deterministic fallback even when a key is present
LEX_BRIEF_VISION_MODEL    — OpenRouter model for photo vision (default: openai/gpt-4o)
```

Never commit `.env`, `secrets/`, or anything under `data/` — ServiceTitan dossiers contain real customer PII.

## Tests

```bash
python -m pytest tests/ -q
```

## License

Internal LEX Air / Lyons use only. Not for redistribution.
