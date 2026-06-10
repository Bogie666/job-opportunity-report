#!/usr/bin/env python3
from __future__ import annotations
import json, re, shutil, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
import rebuild_lean_briefs as r

CENTRAL=timezone(timedelta(hours=-5))

BUCKET_TERMS={
 'duct/IAQ':['duct','iaq','air quality','air scrubber','uv','purifier','duct cleaning','seal'],
 'blower/plenum':['blower','plenum','cleaning','clean'],
 'coil':['coil','evap','evaporator','condenser clean','cleaning'],
 'drain/pan':['drain','pan','condensate','float','safety switch','p-trap'],
 'equipment replacement':['replacement','replace furnace','replace condenser','replace air handler','replace heat pump','new system','install furnace','install condenser','full system'],
 'insulation/attic':['insulation','attic','blown insulation'],
 'electrical handoff':['electrical','panel','surge','gfci','wiring'],
 'plumbing handoff':['plumbing','water','sink','hose bib','leak','valve'],
 'access/safety':['platform','access','safety'],
 'data cleanup':['model','serial','label','equipment record'],
}

def money(x):
 try: return f"${float(x):,.0f}"
 except Exception: return "$0"

def clean_label(s, max_len=90):
 s=re.sub(r'\s+', ' ', str(s or '')).strip()
 s=re.sub(r'^[\-•*\s]+', '', s)
 return s[:max_len].rstrip()

def text_of_est(e):
 parts=[e.get('name') or '', e.get('summary') or '']
 for it in e.get('items') or []:
  sku=it.get('sku') or {}
  parts.extend([sku.get('displayName') or '', sku.get('name') or '', it.get('description') or ''])
 return ' '.join(parts).lower()

def text_of_invoice(inv):
 parts=[]
 for it in inv.get('items') or []:
  parts.extend([it.get('skuName') or '', it.get('displayName') or '', it.get('description') or ''])
 return ' '.join(parts).lower()

def _recent(date_s, days=365):
 try:
  d=datetime.fromisoformat((date_s or '')[:10]).replace(tzinfo=timezone.utc)
  return (datetime.now(timezone.utc)-d).days <= days
 except Exception:
  return False

def recent_addressed(finding, dossier):
 bucket=finding.get('bucket') or ''
 terms=BUCKET_TERMS.get(bucket, [])
 if not terms: return False, ''
 est_hits=[]
 for e in dossier.get('estimates') or []:
  blob=text_of_est(e)
  if any(t in blob for t in terms):
   status=((e.get('status') or {}).get('name') or '').lower()
   date=(e.get('soldOn') or e.get('createdOn') or '')[:10]
   if not _recent(date):
    continue
   est_hits.append(f"{status or 'estimate'} {money(e.get('subtotal'))} {clean_label(e.get('name') or bucket)} ({date})")
 inv_hits=[]
 for inv in dossier.get('past_invoices') or []:
  blob=text_of_invoice(inv)
  date=(inv.get('invoiceDate') or inv.get('date') or inv.get('createdOn') or '')[:10]
  if any(t in blob for t in terms) and _recent(date):
   total=inv.get('total') or inv.get('subTotal') or inv.get('subtotal') or 0
   inv_hits.append(f"invoice {money(total)} ({date})")
 # Treat recent sold/invoice as potentially addressed; open estimate = not addressed, but valuable reopen target.
 sold_or_invoice=[x for x in est_hits if x.startswith('sold')] + inv_hits
 if sold_or_invoice:
  return True, '; '.join(sold_or_invoice[:2])
 if est_hits:
  return False, 'Related recent estimate exists but not confirmed sold: ' + '; '.join(est_hits[:2])
 return False, 'No matching recent invoice/estimate found in pulled data'

def tier(score):
 return '🔴 PRIORITY' if score>=70 else ('🟡 ELEVATED' if score>=45 else '⚪️ STANDARD')

def adjust_score(md, findings):
 if not findings: return md
 m=re.search(r'\*\*SCORE:\*\* (\d+) ([^\n]+?) Top signals: ([^\n]+)', md)
 if not m: return md
 old=int(m.group(1)); new=min(100, old+5)
 sig=m.group(3)
 first=findings[0].get('bucket') or 'photo trigger'
 if 'historical photo trigger' not in sig.lower():
  sig = (sig + ' · historical photo trigger').strip()
 repl=f"**SCORE:** {new} {tier(new)} Top signals: {sig}"
 return md[:m.start()] + repl + md[m.end():]

def section_text(vision, dossier):
 findings=[f for f in (vision.get('findings') or []) if f.get('confidence') in ('high','medium')]
 lines=[]
 lines.append('## 📷 PHOTO / VISION OPPORTUNITIES')
 lines.append(f"**Source:** {vision.get('photo_source_note') or 'No usable photos'}")
 if not findings:
  lines.append('- No photo evidence available for this job. Use history, estimates, and on-site verification only.')
  return '\n'.join(lines)
 for f in findings[:4]:
  addressed, evidence=recent_addressed(f,dossier)
  idx=', '.join('#'+str(i) for i in f.get('indexes') or [])
  verify=f.get('verify_wording') or f.get('finding') or ''
  opp=f.get('why_it_matters') or ''
  status_label = 'Addressed signal' if addressed else 'Gap'
  lines.append(f"- **Images:** {idx or 'not indexed'}")
  lines.append(f"  - **Verify:** {verify}")
  if opp:
   lines.append(f"  - **Why it matters / opportunity:** {opp}")
  lines.append(f"  - **Bucket / confidence:** {f.get('bucket')}; {f.get('confidence')}")
  lines.append(f"  - **{status_label}:** {evidence}")
 return '\n'.join(lines)

def insert_after(md, marker, insert):
 if not insert: return md
 pos=md.find(marker)
 if pos<0: return md + '\n\n' + insert
 next_pos=md.find('\n## ', pos+len(marker))
 if next_pos<0: return md + '\n\n' + insert
 return md[:next_pos].rstrip() + '\n\n' + insert + '\n' + md[next_pos:]

def replace_verify_top(md, vision):
 findings=[f for f in (vision.get('findings') or []) if f.get('confidence') in ('high','medium')]
 if not findings: return md
 f=findings[0]
 finding=(f.get('finding') or f.get('bucket') or 'photo trigger').strip().rstrip('.')
 verify=f.get('verify_wording') or ''
 opportunity='make a specific verified recommendation'
 m=re.search(r'If still present\s*[-→>]*\s*opportunity:\s*(.*)', verify, re.I)
 if m:
  opportunity=m.group(1).strip().rstrip('.')
 elif f.get('why_it_matters'):
  opportunity=f.get('why_it_matters').strip().rstrip('.')
 start=md.find('## 🔍 VERIFY TODAY')
 if start<0: return md
 end=md.find('\n## ', start+1)
 end=len(md) if end<0 else end
 newsec='## 🔍 VERIFY TODAY\n'
 newsec+=f"**Trigger:** Prior photos showed {finding} → check/photo-verify {f.get('bucket')} conditions today\n"
 newsec+=f"**If still present → opportunity:** {opportunity}"
 return md[:start]+newsec+md[end:]

def main(lean_dir, vision_summary, raw_dir=None):
 lean=Path(lean_dir); vision=json.loads(Path(vision_summary).read_text())
 raw=Path(raw_dir) if raw_dir else lean.parent / lean.name.replace('_lean','')
 out=lean.parent / (lean.name.replace('_lean','') + '_full')
 out.mkdir(exist_ok=True)
 by_job={str(v.get('job_id')):v for v in vision}
 written=[]
 for md_path in sorted(lean.glob('*.md')):
  job_id=md_path.name.split('_',1)[0]
  md=md_path.read_text()
  v=by_job.get(job_id) or {}
  jf=next(raw.glob(job_id+'*.json'), None)
  dossier={}
  if jf:
   b=json.loads(jf.read_text()); dossier=b.get('dossier') or b
  findings=[f for f in (v.get('findings') or []) if f.get('confidence') in ('high','medium')]
  md=adjust_score(md, findings)
  insert=section_text(v, dossier)
  md=insert_after(md, '## HISTORY / NOTES', insert)
  md=replace_verify_top(md, v)
  out_path=out/md_path.name
  out_path.write_text(md)
  written.append(str(out_path))
 print(out)
 for w in written: print(w)

if __name__=='__main__':
 if len(sys.argv)<3:
  raise SystemExit('usage: integrate_photo_vision.py <lean_dir> <vision_summary.json> [raw_dir]')
 main(*sys.argv[1:])
