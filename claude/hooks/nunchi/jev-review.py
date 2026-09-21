#!/usr/bin/env python3
"""Opt-in advisory kind review. Never writes to the source memory database.

Run after collection from cron. First run establishes per-DB high-water marks;
only subsequently stored, sanitized technical candidates may leave the node.
One attempt per candidate,5/run,100/day across all scopes. Attempts are reserved
before network I/O. Missing key,timeout,invalid response never blocks ingest.
"""
import fcntl,hashlib,importlib.util,json,math,os,re,sqlite3,time,urllib.request
from pathlib import Path
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('receipts',HERE/'feed-receipt.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
MODEL='jev-1.13.0'
KINDS={'preference','decision','task-progress','correction','procedure','fact','observation','context','constraint','uncertain'}
DENY=re.compile(r'가족|아내|남편|자녀|아이|딸|아들|주민|생년|전화|계좌|카드|은행|대출|보증|소송|판결|법률|감사원|HUG|병원|환자|진단|의료|비밀|비밀번호|토큰\s*[:=]|api.?key|password|secret|bearer|cookie|credential|family|medical|patient|lawsuit|salary',re.I)
TECH=re.compile(r'\b(?:ccc|nunchi|jev|codex|claude|piri|danso|git|github|PR|CI|API|SQLite|JSON|systemd|cron|Docker|pytest|SSH)\b|배포|브리지|회귀.?테스트|수집기|스키마',re.I)
NAMES=re.compile(r'서서|노숙|진군|등애|순욱|방통|공명|곽가|육손|소교|대교|공융|서진온|\b(?:seoseo|nosuk|jingun|dungae|soonwook|bangtong|gongmyoung|gwakga|yukson|sogyo|daegyo|gongyung|seo-jin-on|jinwon-int|jinon86)\b',re.I)

def sanitize(text):
    if not isinstance(text,str) or not 20<=len(text)<=1200 or DENY.search(text) or not TECH.search(text):return None
    text=re.sub(r'https?://\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:\d{1,3}\.){3}\d{1,3}|(?:/[^\s,;]+)+|\b[A-Fa-f0-9]{16,}\b','[reference]',text)
    text=re.sub(r'\b\d[\d:./_-]{3,}\b','[number]',text)
    text=NAMES.sub('[node]',text)
    return text

def validate(raw):
    if raw.get('model')!=MODEL:raise ValueError('model')
    a=raw['answers']['kind'];p=a['probabilities'];c=a['confidence']
    if a.get('type')!='choice' or a.get('choice') not in KINDS or set(p)!=KINDS:raise ValueError('choice')
    if any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in [c,*p.values()]):raise ValueError('probability')
    if abs(sum(p.values())-1)>.02:raise ValueError('sum')
    return a

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

def request(sentence,key,question):
    data=json.dumps({'model':MODEL,'state':{'sentence':sentence},'questions':{'kind':question}},ensure_ascii=False).encode()
    req=urllib.request.Request('https://api.typesafe.ai/v1/systemone',data=data,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
    with urllib.request.build_opener(NoRedirect).open(req,timeout=10) as response:
        raw=response.read(65537)
        if len(raw)>65536:raise ValueError('oversize_response')
        return validate(json.loads(raw))

def run(home,bot,keypath,call=request):
    home.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock=r.private_open(home/'.jev-review.lock',os.O_CREAT|os.O_RDWR)
    try:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'skipped':'locked'}
        ledger=home/'jev-review.db'
        fd=r.private_open(ledger,os.O_CREAT|os.O_RDWR);os.close(fd)
        c=sqlite3.connect(ledger)
        c.executescript('CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY, inode TEXT NOT NULL, last_id INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS reviews(path TEXT,id INTEGER, fingerprint TEXT, day TEXT,status TEXT,kind TEXT,choice TEXT,confidence REAL,model TEXT,PRIMARY KEY(path,id,fingerprint));')
        question=json.loads((HERE/'jev-kind-question.json').read_text())
        paths=[home/'facts.db']+sorted((bot/'memory-audiences').glob('*/nunchi/facts.db'))
        used=0;initialized=0;skipped=0
        key=None
        for db in paths:
            if not db.exists():continue
            if db!=home/'facts.db' and not (db.parent.parent.name=='shared' or re.fullmatch(r'private-[0-9a-f]{32}',db.parent.parent.name)):continue
            r.fingerprint(db)
            st=db.stat();identity=f'{st.st_dev}:{st.st_ino}'
            source=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True);source.execute('pragma query_only=on')
            try:
                mx=source.execute('select coalesce(max(id),0) from peer_facts').fetchone()[0]
                prior=c.execute('select inode,last_id from cursors where path=?',(str(db),)).fetchone()
                if not prior or prior[0]!=identity or prior[1]>mx:
                    c.execute('insert or replace into cursors values(?,?,?)',(str(db),identity,mx));c.commit();initialized+=1;continue
                rows=source.execute('select id,kind,fact from peer_facts where id>? and valid_to is null order by id limit 200',(prior[1],)).fetchall()
                for fid,kind,text in rows:
                    sentence=sanitize(text)
                    if sentence is None:
                        c.execute('update cursors set last_id=? where path=?',(fid,str(db)));c.commit();skipped+=1;continue
                    day=time.strftime('%Y-%m-%d',time.gmtime())
                    today=c.execute('select count(*) from reviews where day=?',(day,)).fetchone()[0]
                    if used>=5 or today>=100:return {'attempts':used,'initialized':initialized,'filtered':skipped,'budget_stop':True}
                    if key is None:
                        with os.fdopen(r.private_open(keypath,os.O_RDONLY)) as f:
                            if os.fstat(f.fileno()).st_mode&0o077:raise ValueError('key_permissions')
                            key=f.read(4097).strip()
                        if not key or len(key)>4096:raise ValueError('key')
                    fp=hashlib.sha256((text+'\0'+MODEL+'\0'+json.dumps(question,sort_keys=True)).encode()).hexdigest()
                    c.execute('insert or ignore into reviews values(?,?,?,?,?,?,?,?,?)',(str(db),fid,fp,day,'reserved',kind,None,None,MODEL))
                    fresh=c.execute('select changes()').fetchone()[0]
                    c.execute('update cursors set last_id=? where path=?',(fid,str(db)));c.commit()
                    if not fresh:continue
                    used+=1
                    try:
                        a=call(sentence,key,question)
                        c.execute('update reviews set status=?,choice=?,confidence=? where path=? and id=? and fingerprint=?',('reviewed',a['choice'],a['confidence'],str(db),fid,fp))
                    except Exception:
                        c.execute('update reviews set status=? where path=? and id=? and fingerprint=?',('failed',str(db),fid,fp));c.commit()
                        return {'attempts':used,'failed':1,'filtered':skipped}
                    c.commit()
            finally:source.close()
        return {'attempts':used,'initialized':initialized,'filtered':skipped}
    finally:
        if 'c' in locals():c.close()
        os.close(lock)

if __name__=='__main__':
    if os.environ.get('NUNCHI_JEV_REVIEW')=='1':
        try:
            home=Path(os.environ.get('NUNCHI_HOME',str(Path.home()/'.nunchi')))
            bot=Path(os.environ.get('BOT_DATA_DIR',str(Path.home()/'.telegram_bot')))
            key=Path(os.environ.get('NUNCHI_JEV_KEY_FILE',str(Path.home()/'.secrets/typesafe-api-key')))
            print(json.dumps(run(home,bot,key)))
        except Exception:print(json.dumps({'status':'unavailable','memory_unchanged':True}))
