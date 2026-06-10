# Agent Operating Guide — Job Opportunity Report Card Pipeline

Audience: an automated agent (or human operator) running and maintaining this pipeline.
Last major change: 2026-06-10, branch `claude/practical-newton-tupxym` — LLM narrative
synthesis with deterministic validation replaced the rule-templated report card.

## What this system does

Pulls tomorrow's scheduled ServiceTitan jobs (HVAC by default), scores each for sales
opportunity, renders a Customer Opportunity Report Card for the top jobs, and emails
them in a navy/gold HTML layout so techs and dispatch can prep before the visit.
Everything is **read-only against ServiceTitan** — no production records are modified.

## Core architecture (do not violate this)

**Facts by rules, narrative by LLM, validated by rules.**

1. **Facts (deterministic, Python only).** Equipment ages (installedOn → brand-aware
   serial decoder → booking-note text), home age (ST custom fields, verified/filled by
   free CAD lookup), estimate dollars and intent states, memberships, tiered
   replacement flags. An LLM must NEVER compute or invent a number, date, age, or ID.
2. **Narrative (LLM).** `src/report_card_llm.py` sends the facts context to OpenRouter
   and gets back structured JSON section slots: call reason, buying behavior, visual
   inspection bullets, 1–3 primary opportunities with priority, coaching notes, and the
   overall grade block.
3. **Validation (deterministic).** Every dollar figure, 19xx/20xx year, and 6+ digit ID
   in the LLM output must already exist in the facts context. Equipment in the
   hallucination watchlist (water heater, panel, EV charger, softener, boiler) may only
   be mentioned if on file, or with "not on file, verify on arrival" if only evidenced
   by history. OPEN/EXPIRED estimates are "quoted, never closed" — never "customer
   interest". One correction retry, then offending lines are scrubbed; if the output is
   structurally unusable, the deterministic fallback fills the same slots.
4. **Rendering (deterministic).** One renderer (`render_markdown`) serves both paths,
   so the locked format is identical regardless of how slots were filled. The header
   facts, the equipment-age line, and every ⚠ FLAG bullet are rendered by Python
   directly — the model cannot drop, soften, or alter them.

## File map

| File | Role |
|---|---|
| `src/client.py` | ServiceTitan OAuth client (reads env vars, caches token in `data/token_cache.json`) |
| `src/servicetitan_dossier.py` | Pulls jobs + builds cached per-job dossier JSON bundles |
| `src/report_card_facts.py` | Shared fact extraction: equipment rollups, membership summaries, `analyze_estimate_history` (supersession, price ceiling, repeat declines, always-buys), `build_llm_context`, `cross_trade_signals` |
| `src/opportunity_flags.py` | **The rules that stay rules**: tiered equipment-age thresholds + home-age tiers, shared by renderer and scorer |
| `src/report_card_llm.py` | LLM section synthesis + grounded-facts validator + scrub + fallback signal |
| `src/customer_opportunity_report_card.py` | `_derive()` facts → sections (LLM or `deterministic_sections`) → `render_markdown()` |
| `src/render_report_card_email.py` | Markdown → inline-styled HTML (Gmail strips `<style>` blocks — keep styles inline) |
| `src/serial_decoder.py` | Brand-aware serial → year-of-manufacture (validated 97% against install dates) |
| `src/cad_home_age.py` | Free CAD home-age lookup (cache-first, network) |
| `scripts/pull_hvac_tomorrow.py` | Step 1: pull tomorrow's HVAC jobs |
| `scripts/fetch_job_photos.py` | Step 2: download photos, build contact sheets |
| `scripts/score_and_filter_hvac_briefs.py` | Step 3: deterministic scoring, picks top-N (the gate for LLM spend) |
| `scripts/analyze_selected_photos.py` / `analyze_photo_sheets_openrouter.py` | Step 4: vision review of contact sheets |
| `scripts/log_outcomes.py` | Step 7 (weekly): realized-revenue feedback loop |

## Daily run

```bash
# 1. Pull all HVAC scheduled jobs for tomorrow → data/tech_briefs/<STAMP>/
python scripts/pull_hvac_tomorrow.py

# 2. Photos + contact sheets → data/tech_briefs/<STAMP>_photos/
python scripts/fetch_job_photos.py data/tech_briefs/<STAMP>

# 3. Score; Follow Up call types excluded (already-sold return visits)
python scripts/score_and_filter_hvac_briefs.py data/tech_briefs/<STAMP> \
  --manifest data/tech_briefs/<STAMP>_photos/manifest.json \
  --threshold 40 --top-n 8 --exclude "Follow Up"

# 4. Vision on the selected subset ONLY (cost control), grounded with ST context
python scripts/analyze_selected_photos.py \
  --manifest data/tech_briefs/<STAMP>_photos/manifest.json \
  --selected data/tech_briefs/<STAMP>_scoring/selected.json \
  --briefs-dir data/tech_briefs/<STAMP>

# 5. Render each selected job's card (LLM path auto-enables when key present)
python src/customer_opportunity_report_card.py data/tech_briefs/<STAMP>/<JOB>.json \
  --vision-summary data/tech_briefs/<STAMP>_photos/vision/<JOB_ID>.json \
  --out out/<JOB>.md

# 6. Email delivery — scripts/deliver_report_cards.py is NOT yet committed; render
#    HTML per card with src/render_report_card_email.py and send via SendGrid.

# 7. ~1 week later, every scoring run (cron-able):
python scripts/log_outcomes.py data/tech_briefs/<STAMP>_scoring
```

## Environment

Required: `SERVICETITAN_CLIENT_ID/SECRET/APP_KEY/TENANT_ID`, `SERVICETITAN_TIMEZONE`,
`SENDGRID_API_KEY/FROM_EMAIL/FROM_NAME`, `OPENROUTER_API_KEY`. See `.env.example`.

Knobs:
- `LEX_REPORT_CARD_MODEL` — report-card narrative model (falls back to `LEX_BRIEF_MODEL`, then `openai/gpt-4o`).
- `LEX_REPORT_CARD_USE_LLM=0` — force deterministic fallback even with a key. CLI: `--no-llm`.
- `LEX_BRIEF_VISION_MODEL` — vision model.
- `LEX_DISABLE_FREE_CAD_HOME_AGE=1` — skip the CAD network lookup (useful offline/tests).

Credential loading is **explicit**: entrypoints call `report_card_facts.load_default_env()`
(loads `/workspace/openclaw/MOVING/credentials/MASTER.env` then repo `.env`). Importing
modules no longer loads credentials as a side effect — if an API call fails with missing
env, the entrypoint probably didn't call `load_default_env()`.

## The rules that intentionally stay rules

Tiered replacement flags (`src/opportunity_flags.py`), per equipment class:

| Class | Flag | Urgent ("past typical service life") |
|---|---|---|
| HVAC (condenser, heat pump, coil, air handler, mini-split, RTU) | 10 yrs | 15 yrs |
| Furnace | 15 yrs | 20 yrs |
| Tank water heater | 8 yrs | 12 yrs |
| Tankless water heater | 15 yrs | 20 yrs |

Home-age tiers: 10–15 (builder-grade first-failure window), 15–30 (first replacement
cycle, duct/IAQ), 30–45 (panel capacity, second HVAC cycle, supply lines), 45+ (full
cross-trade). These drive both a CUSTOMER PROFILE trigger line and scorer points.

Flag text includes age provenance ("installed 2012" / "carrier serial decode, medium
confidence" / "booking notes") — keep it; it tells the tech how much to trust the trigger.

**Supersession classifier** (`classify_aged_equipment` in `opportunity_flags.py`):
ServiceTitan routinely keeps replaced equipment active, so every aged/undated unit is
classified before flagging using a signal ladder — (1) zone-token pairing with a recent
same-family install, (2) "# of systems" custom-field arithmetic, (3) tonnage pairing,
confirmed by a sold capital estimate near the install date, (4) a booking-note
counter-signal that pulls one unit back to ambiguous if the CSR noted an old system
still in service. Outcomes:
- `superseded` → "Stale record likely" verify note on the card, NO red flag, 0 scorer
  points (drivers show "N aged record(s) likely superseded, ignored").
- `ambiguous` → softened "⚠ FLAG (verify)" + reduced +10 scorer points.
- `remaining` → full red flag and full points.
Nothing is ever hidden — the equipment-age line still shows the old record with a
"likely already replaced" annotation, and the LLM receives `equipment_record_notes`
with an explicit rule (SYS_MSG rule 11) to never build replacement opportunities on
superseded units. If a card looks like it under-flagged old equipment, check the
"Stale record likely" lines first — that is usually the classifier working as intended.

Scoring weights (hand-set, pending calibration): urgent flag +35, flag +25, home tier
+5/+5/+10/+15, active membership +10, open estimates +5 each (cap 30), open ≥$5k +20,
sold ≥$10k +15, photos +10, repair keywords +15, booking-note-age-only +20/+30.

## Failure modes & expected behavior

- **No `OPENROUTER_API_KEY` / LLM call fails / JSON unparseable / validation
  unsatisfiable after retry+scrub** → card silently renders via the deterministic
  fallback. The reason is printed to **stderr** prefixed `report_card_llm:`. A run that
  produces all-fallback cards is degraded, not broken — check stderr and the model/key.
- **Vision returns `findings: []`** — that is correct behavior, not an error. Empty
  beats fabricated. Never retry prompting for "more findings".
- **Serial decoder returns None** for York/Coleman, mini-split brands
  (Mitsubishi/Fujitsu/LG/Samsung), Nortek/Nordyne → card says "verify nameplate year".
  Do not add naive decode rules; YYWW vs WWYY confusion produced wrong ages before.
- **ST install dates are routinely placeholder/wrong** (1900/1990 dates treated as
  missing; equipment predating the home build year raises a DATA QUALITY flag). Ages
  are always framed verify-on-arrival.
- **CAD lookup can fail or disagree with ST** — disagreement adds a DATA QUALITY flag
  and prefers the CAD year. Scorer never calls CAD (custom fields only, no network).
- **429s from ServiceTitan** are retried with backoff inside `client.py`.

## Hard rules for the agent

1. **Never commit anything under `data/`, `.env`, or `secrets/`** — dossiers contain
   real customer PII. `out/` artifacts also contain PII; treat as internal-only.
2. **Do not loosen the validator** in `report_card_llm.py` to make outputs "pass".
   If a model keeps failing validation, fix the prompt or switch models; an accurate
   fallback card beats a hallucinated LLM card every time.
3. **The card layout is locked** (sections, order, navy/gold HTML, inline styles).
   Change content quality, not structure, without explicit approval from Ryan.
4. **Keep email HTML inline-styled** — Gmail/Outlook strip `<style>` blocks. The red
   flag styling keys on list items containing the text `FLAG`.
5. **Follow Up call types stay excluded** from scoring/emailing by default.
6. **Run `python -m pytest tests/ -q` after any change.** Tests are offline-safe
   (LLM-path tests use stubs; pass `use_llm=False` for deterministic assertions).
7. When editing thresholds or weights, change `src/opportunity_flags.py` only — the
   renderer and scorer both read from it. Don't fork the numbers.

## The outcomes loop (important, easy to forget)

`scripts/log_outcomes.py <STAMP>_scoring` appends one row per scored job to
`data/outcomes/outcomes.csv`: score, drivers, selected-for-email flag, then realized
invoice totals and estimates sold on that job. Append-only, idempotent per
(run_stamp, job_id). Run it weekly for every stamp older than ~5 days. Once a few
hundred rows accumulate: compare score vs realized revenue, and selected vs
non-selected conversion — that is the evidence for retuning the weights above and the
ROI case for the whole report-card program.

## Known gaps / next work

- `scripts/deliver_report_cards.py` (render selected cards → combined HTML → SendGrid)
  is referenced in the README but not yet committed.
- Scorer weights are uncalibrated until outcomes data accumulates.
- `_call_issue`/`_repair_followup_items` regexes still exist for the *fallback* path
  only; the LLM path reads raw booking notes. Don't extend the regexes — improve the
  prompt instead.
- Reference card `templates/report-card/REFERENCE_424675208.*` predates the tiered
  thresholds (its water-heater flag wording would differ today); regenerate from a real
  dossier when convenient, structure is unchanged.
- `validate_sections` numeric audit is context-substring based; if a legitimate figure
  is ever rejected, the fix is to add that fact to `build_report_card_context`, not to
  weaken the audit.
