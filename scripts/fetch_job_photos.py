#!/usr/bin/env python3
from __future__ import annotations
import json, os, re, sys, time
from pathlib import Path
from io import BytesIO
import requests
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from client import ServiceTitanClient
from servicetitan_dossier import load_env

for env_path in [
    '/workspace/openclaw/MOVING/credentials/MASTER.env',
    '/workspace/apps/openclaw-credential-archive/20260526T032211Z/secrets/MOVING/credentials/MASTER.env',
    '/workspace/.secrets/hermes.env',
]:
    load_env(env_path)
load_env(str(ROOT / '.env'), override=True)

IMAGE_EXTS = {'.jpg','.jpeg','.png','.webp','.gif','.bmp','.tif','.tiff'}
MAX_PER_JOB = int(os.environ.get('PHOTO_MAX_PER_JOB','24'))
MAX_PRIOR_JOBS = int(os.environ.get('PHOTO_MAX_PRIOR_JOBS','6'))

def safe(s):
    return re.sub(r'[^A-Za-z0-9_.-]+','_', str(s or ''))[:80] or 'file'

def list_attachments(c, job_id):
    r = c.get(f'/forms/v2/tenant/{{tenant}}/jobs/{job_id}/attachments', {'pageSize':100, 'includeTotal':'true'})
    if r.status_code != 200:
        return {'job_id': job_id, 'status': r.status_code, 'items': [], 'error': r.text[:200]}
    body = r.json() if r.headers.get('content-type','').startswith('application/json') else {}
    return {'job_id': job_id, 'status': 200, 'items': body.get('data') or body.get('items') or []}

def att_name(att):
    return att.get('fileName') or att.get('filename') or att.get('name') or att.get('title') or f"attachment_{att.get('id')}"

def att_id(att):
    return att.get('id') or att.get('attachmentId')

def looks_image(att):
    name = att_name(att).lower()
    ct = (att.get('contentType') or att.get('mimeType') or '').lower()
    return ct.startswith('image/') or Path(name).suffix.lower() in IMAGE_EXTS

def download(c, attachment_id):
    path = f'/forms/v2/tenant/{{tenant}}/jobs/attachment/{attachment_id}'
    url = c.cfg.api_base + path.replace('{tenant}', c.cfg.tenant_id)
    r = requests.get(url, headers=c._headers(), timeout=45)
    return r

def make_sheet(images, out_path):
    thumbs=[]
    for rec in images:
        try:
            im=Image.open(rec['local_path']).convert('RGB')
            im.thumbnail((320,240))
            canvas=Image.new('RGB',(340,300),'white')
            canvas.paste(im, ((340-im.width)//2, 34))
            d=ImageDraw.Draw(canvas)
            d.rectangle((0,0,339,299), outline=(0,0,0), width=2)
            label=f"#{rec['index']} att {rec['attachment_id']} job {rec['job_id']}"
            d.text((8,8), label, fill=(0,0,0))
            source='CURRENT' if rec.get('source')=='current' else 'HISTORICAL'
            d.text((8,260), source, fill=(180,0,0) if source=='CURRENT' else (0,0,180))
            d.text((8,280), (rec.get('filename') or '')[:42], fill=(0,0,0))
            thumbs.append(canvas)
        except Exception as e:
            rec['sheet_error']=str(e)
    if not thumbs:
        return None
    cols=2
    rows=(len(thumbs)+cols-1)//cols
    sheet=Image.new('RGB',(cols*340,rows*300),(245,245,245))
    for i,t in enumerate(thumbs):
        sheet.paste(t, ((i%cols)*340,(i//cols)*300))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path, quality=92)
    return str(out_path)

def main(run_dir):
    run=Path(run_dir)
    out=run.parent / (run.name + '_photos')
    out.mkdir(parents=True, exist_ok=True)
    c=ServiceTitanClient()
    manifest=[]
    errors=[]
    for jf in sorted(p for p in run.glob('*.json') if p.name!='index.json'):
        bundle=json.loads(jf.read_text())
        dossier=bundle.get('dossier') or bundle
        job=dossier['job']
        job_id=job['id']
        candidates=[]
        cur=list_attachments(c, job_id)
        if cur['status']!=200:
            errors.append({'job_id':job_id,'source':'current','status':cur['status'],'error':cur.get('error')})
        for att in cur['items']:
            if looks_image(att):
                candidates.append(('current', job_id, att))
        # historical prior jobs if current sparse
        for pj in (dossier.get('past_jobs') or [])[:MAX_PRIOR_JOBS]:
            if len(candidates)>=MAX_PER_JOB: break
            pid=pj.get('id')
            hist=list_attachments(c, pid)
            if hist['status']!=200:
                errors.append({'job_id':job_id,'source_job_id':pid,'source':'historical','status':hist['status'],'error':hist.get('error')})
                continue
            for att in hist['items']:
                if looks_image(att):
                    candidates.append(('historical', pid, att))
                    if len(candidates)>=MAX_PER_JOB: break
        job_images=[]
        job_dir=out / str(job_id)
        job_dir.mkdir(exist_ok=True)
        for idx,(source, source_job_id, att) in enumerate(candidates[:MAX_PER_JOB], start=1):
            aid=att_id(att)
            if not aid: continue
            name=att_name(att)
            ext=Path(name).suffix.lower() or '.jpg'
            local=job_dir / f'{idx:02d}_{source}_job{source_job_id}_att{aid}_{safe(name)}'
            if not local.suffix:
                local=local.with_suffix(ext)
            try:
                r=download(c, aid)
                if r.status_code!=200:
                    errors.append({'job_id':job_id,'attachment_id':aid,'status':r.status_code,'error':r.text[:200]})
                    continue
                local.write_bytes(r.content)
                # verify image and normalize extension if needed
                try:
                    with Image.open(local) as im:
                        fmt=im.format
                        width,height=im.size
                except UnidentifiedImageError:
                    local.unlink(missing_ok=True)
                    continue
                rec={'job_id':job_id,'job_number':job.get('jobNumber'),'source':source,'source_job_id':source_job_id,'attachment_id':aid,'index':idx,'filename':name,'local_path':str(local),'width':width,'height':height,'createdOn':att.get('createdOn') or att.get('createdDate') or att.get('modifiedOn')}
                job_images.append(rec)
            except Exception as e:
                errors.append({'job_id':job_id,'attachment_id':aid,'error':str(e)})
        sheet=make_sheet(job_images, out / f'{job_id}_contact_sheet.jpg') if job_images else None
        manifest.append({'job_id':job_id,'job_number':job.get('jobNumber'),'json':str(jf),'job_type':(bundle.get('meta') or {}).get('job_type'),'candidate_images':len(candidates),'downloaded_images':len(job_images),'contact_sheet':sheet,'images':job_images})
        print(job_id, 'candidates', len(candidates), 'downloaded', len(job_images), 'sheet', sheet)
    (out/'manifest.json').write_text(json.dumps({'run_dir':str(run),'photo_dir':str(out),'manifest':manifest,'errors':errors}, indent=2))
    print(out/'manifest.json')

if __name__=='__main__':
    if len(sys.argv)<2:
        raise SystemExit('usage: fetch_job_photos.py <tech_brief_run_dir>')
    main(sys.argv[1])
