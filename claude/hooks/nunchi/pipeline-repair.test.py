#!/usr/bin/env python3
"""Behavioral regressions for collection receipts, scope routing and Jev review."""
import contextlib,importlib.util,io,json,os,sqlite3,subprocess,tempfile,unittest
from pathlib import Path
HERE=Path(__file__).resolve().parent

def module(name):
 s=importlib.util.spec_from_file_location(name,HERE/(name+'.py'));m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
r=module('feed-receipt');j=module('journal-feed');v=module('jev-review')

class Pipeline(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.p=Path(self.tmp.name);self.home=self.p/'nunchi';self.home.mkdir(mode=0o700);self.bot=self.p/'bot';self.bot.mkdir(mode=0o700)
 def file(self,name,text):
  p=self.p/name;p.parent.mkdir(mode=0o700,parents=True,exist_ok=True);p.write_text(text);p.chmod(0o600);return p
 def receipt(self,*args):
  with contextlib.redirect_stdout(io.StringIO()):return r.main(list(map(str,args)))
 def test_growing_session_and_failed_attempt(self):
  source=self.file('session with spaces.jsonl','first');ledger=self.home/'receipts';fp=r.fingerprint(source)[1]
  self.assertEqual(self.receipt('due',ledger,source),0)
  self.assertEqual(self.receipt('failed',ledger,source,fp),0)
  self.assertEqual(self.receipt('due',ledger,source),3)
  source.write_text('second');fp=r.fingerprint(source)[1]
  self.assertEqual(self.receipt('due',ledger,source),0)
  self.receipt('stored',ledger,source,fp)
  self.assertEqual(self.receipt('due',ledger,source),3)
  source.write_text('third')
  self.assertEqual(self.receipt('stored',ledger,source,fp),4)
  self.assertEqual(self.receipt('due',ledger,source),0)
 def test_symlink_legacy_receipt_rejected(self):
  source=self.file('session','a');other=self.file('other','a');seen=self.p/'seen';seen.symlink_to(other)
  with self.assertRaises(OSError):self.receipt('due',self.home/'r',source,seen)
 def test_route_never_guesses_private_or_cross_scope(self):
  root=self.bot/'memory-audiences';root.mkdir(mode=0o700)
  scope='private-'+'a'*32;(root/scope).mkdir(mode=0o700)
  self.assertEqual(j.route({'memory_audience':'private','memory_scope':scope},root),root/scope/'nunchi')
  for job in [{},{'memory_audience':'shared','memory_scope':scope},{'memory_audience':'private','memory_scope':'../escape'}]:
   with self.assertRaises(ValueError):j.route(job,root)
  (root/scope).rmdir();(root/scope).symlink_to(self.home)
  with self.assertRaises(ValueError):j.route({'memory_audience':'private','memory_scope':scope},root)
 def test_journal_storage_failure_is_not_acknowledged(self):
  scope='private-'+'a'*32;target=self.bot/'memory-audiences'/scope/'nunchi';target.mkdir(mode=0o700,parents=True);target.parent.chmod(0o700)
  db=target/'facts.db';db.write_text('bad');db.chmod(0o600)
  jobs=self.bot/'distill-journal';jobs.mkdir(mode=0o700)
  p=jobs/'job.json';p.write_text(json.dumps({'memory_audience':'private','memory_scope':scope,'status':'extraction_done','thread_id':'fixture','updated_at':'2026-09-22T00:00:00Z','extraction_output':{'honcho':[{'kind':'preference','text':'The user prefers concise technical summaries.','subject':'user'}]}}));p.chmod(0o600)
  result=j.run(self.home,self.bot);self.assertEqual(result['failed'],1);self.assertFalse((self.home/'journal-receipts.jsonl').exists())
  db.unlink();result=j.run(self.home,self.bot);self.assertEqual(result['mirrored_jobs'],1)
  c=sqlite3.connect(db);self.assertEqual(c.execute('select count(*) from peer_facts').fetchone()[0],1);c.close()
  self.assertEqual(j.run(self.home,self.bot)['mirrored_jobs'],0)
 def source(self):
  c=sqlite3.connect(self.home/'facts.db');c.execute('create table peer_facts(id integer primary key,kind text,fact text,valid_to text)');c.commit();c.close();(self.home/'facts.db').chmod(0o600)
  return self.home/'facts.db'
 def add(self,db,n=1):
  with sqlite3.connect(db) as c:
   for i in range(n):c.execute('insert into peer_facts(kind,fact) values(?,?)',('decision','The CI regression tests completed successfully after the patch.'))
 def test_jev_baseline_budget_dedup_and_source_unchanged(self):
  db=self.source();self.add(db);key=self.file('key','dummy');calls=[]
  def call(sentence,key,q):calls.append(sentence);return {'choice':'task-progress','confidence':.9}
  self.assertEqual(v.run(self.home,self.bot,key,call)['initialized'],1);self.assertEqual(calls,[])
  self.add(db,7);before=db.read_bytes();self.assertEqual(v.run(self.home,self.bot,key,call)['attempts'],5)
  self.assertEqual(v.run(self.home,self.bot,key,call)['attempts'],2);self.assertEqual(v.run(self.home,self.bot,key,call)['attempts'],0)
  self.assertEqual(db.read_bytes(),before)
  with sqlite3.connect(self.home/'jev-review.db') as c:
   self.assertEqual(c.execute('select count(*) from reviews where choice="task-progress" and kind="decision"').fetchone()[0],7)
 def test_jev_failed_call_not_replayed_and_no_memory_write(self):
  db=self.source();key=self.file('key','dummy');v.run(self.home,self.bot,key);self.add(db);before=db.read_bytes()
  def fail(*args):raise TimeoutError()
  self.assertEqual(v.run(self.home,self.bot,key,fail)['failed'],1)
  self.assertEqual(v.run(self.home,self.bot,key,fail)['attempts'],0);self.assertEqual(db.read_bytes(),before)
 def test_privacy_and_response_schema(self):
  self.assertIsNone(v.sanitize('HUG legal case details in a Codex session must be retained.'))
  self.assertIsNone(v.sanitize('My family prefers to use Codex to manage medical records.'))
  self.assertIsNone(v.sanitize('Personal preferences without an operational reference.'))
  s=v.sanitize('nosuk CI deploy to https://example.com/private and 100.2.3.4 completed.')
  self.assertNotIn('example.com',s);self.assertNotIn('nosuk',s);self.assertNotIn('100.2',s)
  probs={k:1/10 for k in v.KINDS};raw={'model':v.MODEL,'answers':{'kind':{'type':'choice','choice':'fact','confidence':.9,'probabilities':probs}}}
  v.validate(raw);raw['answers']['kind']['confidence']=float('nan')
  with self.assertRaises(ValueError):v.validate(raw)

if __name__=='__main__':unittest.main()
