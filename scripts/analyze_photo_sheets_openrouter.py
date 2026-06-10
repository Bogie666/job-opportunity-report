#!/usr/bin/env python3
from __future__ import annotations
import argparse, base64, json, os, sys, time
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from servicetitan_dossier import load_env

for env_path in [
    '/workspace/openclaw/MOVING/credentials/MASTER.env',
    '/workspace/apps/openclaw-credential-archive/20260526T032211Z/secrets/MOVING/credentials/MASTER.env',
    '/workspace/.secrets/hermes.env',
]:
    load_env(env_path)
load_env(str(ROOT / '.env'), override=True)

PROMPT = """You are reviewing a ServiceTitan HVAC photo contact sheet. The sheet contains
up to 24 small tiles. Each tile is labeled #index, attachment id, source job id, and
CURRENT or HISTORICAL. Most are HISTORICAL — describe them as prior evidence only,
never as current conditions.

CRITICAL RULES — read carefully:

1. Do NOT default to generic HVAC findings. Different jobs at different homes
   have different conditions. If you produce the same 3 findings on every job
   (e.g. joists through insulation + duct on insulation + rust in drain pan)
   you are wrong.

2. Each finding MUST cite specific visible tile content: a color, a label, a
   specific edge condition, an exposed component, a wall/joist count, water
   stain shape, rust pattern, etc. Generic phrasings like "rust visible",
   "ductwork issues", "biological growth" without a specific cue are NOT
   acceptable.

3. If a tile is too dark / blurry / cropped / shows only sky / shows only a
   close-up of equipment with no surrounding context, say so and skip it —
   do not invent attic conditions from a close-up of a condenser.

4. Confidence must be honest. If you can't be sure, mark confidence "low".
   Reserve "high" for cases where the tile clearly shows the issue and you
   can describe the exact visible evidence.

5. If you genuinely cannot identify any opportunities from the visible tiles,
   return findings=[] and explain in photo_source_note. Empty is better than
   fabricated.

6. If a JOB CONTEXT block is provided below, use it to GROUND your observations:
   it lists what ServiceTitan says is installed at this home. Use it to identify
   which system/zone a tile likely shows, and if a tile clearly contradicts the
   records (equipment visibly newer/older/different brand than on file), report
   that as a "data cleanup" finding citing the visible nameplate/label evidence.
   Never copy context items into findings without visible tile evidence.

What to look for ONLY if the tiles actually show it:
- Equipment condition: rust, corrosion, oil stains, soot, broken fins
- Drain pan: standing water, rust, missing float switch
- Ductwork: visible disconnect, crushed flex, holes, separated joints
- Blower / plenum / coil: dirt accumulation visible at a specific location
- Attic insulation depth: only when a wide attic view clearly shows joist
  tops exposed across the visible area
- Duct support: only when a flex duct is clearly resting on insulation or
  visibly sagging in the frame
- Safety / access: blocked path, ladder hazards, exposed wiring
- Plumbing/electrical clues only if visibly supported by the tile

Return STRICT JSON only:
{
  "job_id": "",
  "photo_source_note": "Brief description of what kind of photos these are and any limits on what can be assessed",
  "tile_observations": [
    {"index": 1, "what_is_visible": "specific factual description of the tile"}
  ],
  "findings": [
    {
      "indexes": [1],
      "finding": "specific visible issue with concrete evidence cited",
      "evidence": "what in the tile led you to this conclusion",
      "bucket": "duct/IAQ|blower/plenum|coil|drain/pan|equipment replacement|insulation/attic|duct support|electrical handoff|plumbing handoff|access/safety|data cleanup",
      "confidence": "high|medium|low",
      "verify_wording": "Prior photos showed ... -> verify ... today. If still present -> opportunity: ...",
      "why_it_matters": "sales/comfort/risk reason in one sentence"
    }
  ],
  "top_verify_today": "single best trigger -> check -> opportunity line, or empty if none warranted",
  "brief_insert": "2-4 concise bullets ready to insert into a tech brief, or empty if no real findings"
}

Be conservative. Empty findings is acceptable. Fabricated findings are not.
"""

def data_url(path: Path) -> str:
    b64 = base64.b64encode(path.read_bytes()).decode('ascii')
    return f"data:image/jpeg;base64,{b64}"


def load_job_contexts(briefs_dir: Path | None) -> dict[str, str]:
    """Map job_id -> compact JOB CONTEXT block built from the cached dossiers, so the
    vision model grounds tiles against records instead of analyzing blind."""
    if not briefs_dir or not briefs_dir.exists():
        return {}
    from report_card_facts import strip_html, years_old
    contexts: dict[str, str] = {}
    for jf in sorted(briefs_dir.glob('*.json')):
        if jf.name == 'index.json':
            continue
        try:
            bundle = json.loads(jf.read_text())
        except Exception:
            continue
        dossier = bundle.get('dossier') or bundle
        job = dossier.get('job') or {}
        job_id = str(job.get('id') or '')
        if not job_id:
            continue
        lines = []
        reason = strip_html(job.get('summary') or '')[:300]
        if reason:
            lines.append(f"- Reason for call: {reason}")
        eq_lines = []
        for eq in (dossier.get('installed_equipment') or [])[:10]:
            if not eq.get('active', True):
                continue
            t = eq.get('type')
            if isinstance(t, dict):
                t = t.get('name')
            label = str(t or eq.get('name') or 'Equipment')
            mfg = str(eq.get('manufacturer') or '').strip()
            age = years_old(eq.get('installedOn'))
            age_txt = f" ~{age}y" if age is not None else " (age unknown)"
            eq_lines.append(f"{label}{f' ({mfg})' if mfg else ''}{age_txt}")
        if eq_lines:
            lines.append(f"- Equipment on file: {'; '.join(eq_lines)}")
        for cf in ((dossier.get('location') or {}).get('customFields') or []):
            name = (cf.get('name') or '').lower()
            if ('age of home' in name or 'year built' in name) and cf.get('value'):
                lines.append(f"- Home built: {cf.get('value')}")
                break
        if lines:
            contexts[job_id] = (
                "\nJOB CONTEXT (from ServiceTitan records — grounding only, not findings):\n"
                + "\n".join(lines)
            )
    return contexts


def call_vision(key: str, image_path: Path, job_id: str, context_text: str = '') -> str:
    payload = {
        "model": os.environ.get('LEX_BRIEF_VISION_MODEL', 'openai/gpt-4o'),
        "messages": [{
            "role":"user",
            "content":[
                {"type":"text", "text": PROMPT + f"\nJob ID: {job_id}" + (context_text or '')},
                {"type":"image_url", "image_url":{"url": data_url(image_path)}}
            ]
        }],
        "temperature": 0.05,
    }
    r=requests.post('https://openrouter.ai/api/v1/chat/completions',
        headers={"Authorization": f"Bearer {key}", "Content-Type":"application/json", "HTTP-Referer":"https://lexairconditioning.com", "X-Title":"LEX Tech Brief Photo Vision"},
        data=json.dumps(payload), timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"OpenRouter {r.status_code}: {r.text[:500]}")
    return r.json()['choices'][0]['message']['content'].strip()

def main(photo_manifest: str, briefs_dir: str | None = None):
    key=os.environ.get('OPENROUTER_API_KEY')
    if not key:
        raise SystemExit('Missing OPENROUTER_API_KEY')
    mp=Path(photo_manifest)
    data=json.loads(mp.read_text())
    out_dir=mp.parent / 'vision'
    out_dir.mkdir(exist_ok=True)
    contexts = load_job_contexts(Path(briefs_dir) if briefs_dir else None)
    results=[]
    for rec in data['manifest']:
        sheet=rec.get('contact_sheet')
        job_id=str(rec['job_id'])
        if not sheet:
            no={"job_id":job_id,"photo_source_note":"No usable photos","findings":[],"top_verify_today":"No photo trigger found","brief_insert":"No photo evidence available for this job."}
            (out_dir/f'{job_id}.json').write_text(json.dumps(no, indent=2))
            results.append(no)
            print(job_id, 'no photos')
            continue
        sheet_path=(ROOT / sheet) if not Path(sheet).is_absolute() else Path(sheet)
        print('analyzing', job_id, sheet_path, '(with job context)' if job_id in contexts else '')
        text=call_vision(key, sheet_path, job_id, contexts.get(job_id, ''))
        # Strip fenced JSON if model wraps it.
        cleaned=text.strip()
        if cleaned.startswith('```'):
            cleaned=cleaned.split('```',2)[1]
            if cleaned.lstrip().startswith('json'):
                cleaned=cleaned.lstrip()[4:].strip()
        try:
            parsed=json.loads(cleaned)
        except Exception:
            parsed={"job_id":job_id,"raw":text,"parse_error":True}
        parsed.setdefault('job_id', job_id)
        (out_dir/f'{job_id}.json').write_text(json.dumps(parsed, indent=2))
        results.append(parsed)
        print(job_id, 'findings', len(parsed.get('findings') or []))
        time.sleep(1)
    (out_dir/'summary.json').write_text(json.dumps(results, indent=2))
    print(out_dir/'summary.json')

if __name__=='__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('photo_manifest')
    ap.add_argument('--briefs-dir', help='Dossier run dir (data/tech_briefs/<STAMP>) — adds per-job ST context to ground the vision review')
    args = ap.parse_args()
    main(args.photo_manifest, args.briefs_dir)
