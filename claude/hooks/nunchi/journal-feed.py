#!/usr/bin/env python3
"""Mirror completed bridge extractions into their original audience's nunchi DB.

No model calls. No inference of missing audience routes. Each immutable job
gets a receipt only after ingest succeeds; growing/replaced output is retried.
Legacy unrouted journals remain the responsibility of the legacy feed.
"""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('receipts', HERE/'feed-receipt.py')
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)

def route(job, root):
    kind, scope = job.get('memory_audience'), job.get('memory_scope')
    if not ((kind == 'shared' and scope == 'shared') or
            (kind == 'private' and isinstance(scope,str) and re.fullmatch(r'private-[0-9a-f]{32}',scope))):
        raise ValueError('unrouted')
    target = root/scope
    for p in [target, *target.parents]:
        if p.is_symlink(): raise ValueError('unsafe_route')
    if not target.is_dir() or target.stat().st_uid != os.geteuid() or target.stat().st_mode & 0o077:
        raise ValueError('unsafe_route')
    return target/'nunchi'

def run(home, bot):
    home.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=r.private_open(home/'.journal-feed.lock',os.O_RDWR|os.O_CREAT)
    try:
        try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: return {'skipped':'locked'}
        receipt=home/'journal-receipts.jsonl';seen=failed=unrouted=budgeted=0
        files=sorted(list((bot/'distill-journal').glob('*.json'))+list((bot/'danso-distill-journal').glob('*.json')),key=lambda p:p.name)
        for path in files:
            if budgeted>=20:break
            fp=None
            try:
                key,fp,_=r.fingerprint(path)
                import contextlib,io
                with contextlib.redirect_stdout(io.StringIO()):
                    due=r.main(['due',str(receipt),str(path)])
                if due==3:continue
                if due!=0:raise ValueError('receipt_failed')
                budgeted+=1
                with os.fdopen(r.private_open(path,os.O_RDONLY)) as f:
                    if os.fstat(f.fileno()).st_size>2*1024*1024:raise ValueError('oversize_job')
                    job=json.load(f)
                if job.get('status')!='extraction_done':continue
                target=route(job,bot/'memory-audiences')
                raw=job.get('extraction_output');output=json.loads(raw) if isinstance(raw,str) else raw
                items=output['honcho']
                if not isinstance(items,list) or not all(isinstance(x,dict) and isinstance(x.get('text'),str) for x in items):raise ValueError('invalid_output')
                target.mkdir(mode=0o700,exist_ok=True)
                if target.is_symlink():raise ValueError('unsafe_target')
                db=target/'facts.db'
                if db.exists() or db.is_symlink():r.fingerprint(db)
                env=os.environ.copy();env.update(NUNCHI_HOME=str(target),NUNCHI_DB=str(db),NUNCHI_SNAPSHOT=str(target/'snapshot.md'),NUNCHI_NO_AUTO_SUPERSEDE='1')
                payload={'session_id':job['thread_id'],'distilled_at':(output.get('provenance') or {}).get('distilled_at') or job['updated_at'],'honcho':items}
                p=subprocess.run(['python3',str(HERE/'nunchi.py'),'ingest','-'],input=json.dumps(payload),text=True,capture_output=True,env=env,timeout=30)
                if p.returncode:raise ValueError('ingest_failed')
                subprocess.run(['python3',str(HERE/'nunchi.py'),'snapshot','--limit','25'],env=env,capture_output=True,timeout=30,check=True)
                if r.main(['stored',str(receipt),str(path),fp])!=0:raise ValueError('changed_job')
                seen+=1
            except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as e:
                if isinstance(e,ValueError) and str(e)=='unrouted':unrouted+=1
                else:failed+=1
                if fp is not None:
                    try:r.main(['failed',str(receipt),str(path),fp])
                    except (OSError,ValueError,TypeError):pass
        status={'schema':'ccc.nunchi.journal-feed.v1','finished_at':int(time.time()),'mirrored_jobs':seen,'failed':failed,'unrouted':unrouted}
        # Atomic status replacement with a unique owned scratch file.
        import tempfile
        fd,tmp=tempfile.mkstemp(prefix='.journal-status-',dir=home)
        try:
            with os.fdopen(fd,'w') as f:json.dump(status,f);f.flush();os.fsync(f.fileno())
            os.replace(tmp,home/'journal-feed.status.json')
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
        return status
    finally:os.close(lock)

if __name__=='__main__':
    state=Path(os.environ.get('CCC_STATE_DIR',str(Path.home()/'.claude/state')))
    enabled=os.environ.get('CCC_NUNCHI_MODE')
    if enabled is None:
        try:enabled=(state/'nunchi.mode').read_text().strip()
        except OSError:enabled='off'
    if enabled=='on':
        home=Path(os.environ.get('NUNCHI_HOME',str(Path.home()/'.nunchi')))
        bot=Path(os.environ.get('BOT_DATA_DIR',str(Path.home()/'.telegram_bot')))
        print(json.dumps(run(home,bot)))
