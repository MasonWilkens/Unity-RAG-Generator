from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from unity_rag.semantic import build_semantic
from unity_rag.workspace import writer_lock


class Provider:
    config = {'provider':'fixture','model':'stream-v1'}
    def __init__(self):
        self.calls=[]
    def passages(self,texts):
        self.calls.append(len(texts))
        return [[len(text),1] for text in texts]


class StreamingSemanticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace=Path(self.temp.name)
        self.database=self.workspace/'inventory.sqlite3'
        self.report={'snapshot':'fixed'}
        self.provider=Provider()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE evidence (id TEXT PRIMARY KEY, kind TEXT, data TEXT)')
            for i in range(23):
                item={'id':str(i),'kind':'code_member','title':'Method','text':'value '+str(i)}
                db.execute('INSERT INTO evidence VALUES (?,?,?)',(str(i),item['kind'],json.dumps(item)))
        self.opened=patch('unity_rag.retrieval._open',side_effect=lambda _:(sqlite3.connect(self.database),self.report))
        self.status=patch('unity_rag.retrieval.retrieval_status',return_value=self.report)
        self.opened.start();self.status.start()
        self.addCleanup(self.opened.stop);self.addCleanup(self.status.stop)

    def build(self,**options):
        return build_semantic(self.workspace,self.provider,**options)

    def active(self):
        with closing(sqlite3.connect(self.workspace/'semantic.sqlite3')) as db, db:
            return (db.execute('SELECT * FROM active').fetchall(),db.execute('SELECT * FROM vectors ORDER BY chunk,hash').fetchall(),
                    db.execute('SELECT * FROM cache ORDER BY config,hash').fetchall())

    def cancel(self,phase='batch_complete',**options):
        def observe(current,counts):
            if current==phase:
                raise KeyboardInterrupt('cancel test')
        with self.assertRaises(KeyboardInterrupt):
            self.build(observe=observe,**options)

    def test_batch_limits_resume_and_clean_equivalence(self):
        self.cancel(batch_size=3)
        self.assertEqual(self.provider.calls,[3])
        self.assertEqual(self.active(),([],[],[]))
        self.assertFalse(list(self.workspace.glob('semantic-stage-*')))
        result=self.build(batch_size=2)
        self.assertEqual((result['embedded'],result['cache_hits']),(20,3))
        self.assertEqual(max(self.provider.calls[1:]),2)
        finished=self.active()
        self.assertEqual(self.build(batch_size=8)['embedded'],0)
        # Remove only generated cache files in this explicitly owned temporary workspace.
        for filename in ('semantic.sqlite3','embedding-checkpoints.sqlite3'):
            (self.workspace/filename).unlink()
        clean=self.build(batch_size=4)
        self.assertEqual(clean['embedded'],23)
        self.assertEqual(self.active()[1:],finished[1:])

    def test_cancel_before_publication_keeps_old_active_and_reuses_all_checkpoints(self):
        self.build()
        old=self.active()
        self.provider.config={**self.provider.config,'model':'v2'}
        self.cancel(phase='publish',batch_size=5)
        self.assertEqual(self.active(),old)
        self.provider.calls=[]
        result=self.build()
        self.assertEqual((result['embedded'],result['cache_hits']),(0,23))
        self.assertFalse(self.provider.calls)

    def test_corrupt_checkpoint_is_recomputed(self):
        self.cancel(batch_size=3)
        with closing(sqlite3.connect(self.workspace/'embedding-checkpoints.sqlite3')) as db, db:
            digest=db.execute('SELECT hash FROM embeddings ORDER BY hash LIMIT 1').fetchone()[0]
            db.execute('UPDATE embeddings SET vector=? WHERE hash=?',('[null]',digest))
        result=self.build()
        self.assertEqual(result['invalid_cache_entries'],1)
        self.assertEqual((result['cache_hits'],result['embedded']),(2,21))

    def test_model_and_content_changes_do_not_reuse_wrong_embeddings(self):
        self.cancel(batch_size=3)
        self.provider.config={**self.provider.config,'model':'different'}
        self.assertEqual(self.build()['cache_hits'],0)
        with closing(sqlite3.connect(self.database)) as db, db:
            item=json.loads(db.execute("SELECT data FROM evidence WHERE id='0'").fetchone()[0])
            item['text']='changed text'
            db.execute("UPDATE evidence SET data=? WHERE id='0'",(json.dumps(item),))
        result=self.build()
        self.assertEqual((result['cache_hits'],result['embedded']),(22,1))

    def test_invalid_batch_and_competing_writer_fail_before_inference(self):
        for size in (0,-1,129,True,2.5,'4'):
            with self.assertRaisesRegex(ValueError,'batch size'):
                self.build(batch_size=size)
        with writer_lock(self.workspace,filename='.semantic.lock'):
            with self.assertRaisesRegex(ValueError,'already running'):
                self.build()
        self.assertFalse(self.provider.calls)

    def test_bad_batch_leaves_no_partial_checkpoint(self):
        self.provider.passages=lambda texts:[[1,0], [float('nan'),1]]
        with self.assertRaisesRegex(ValueError,'non-finite'):
            self.build(batch_size=2)
        with closing(sqlite3.connect(self.workspace/'embedding-checkpoints.sqlite3')) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0],0)
        self.assertEqual(self.active(),([],[],[]))

    def test_changed_retrieval_does_not_publish_but_keeps_reusable_work(self):
        with patch('unity_rag.retrieval.retrieval_status',return_value={'snapshot':'changed'}):
            with self.assertRaisesRegex(ValueError,'changed during'):
                self.build()
        self.assertEqual(self.active(),([],[],[]))
        self.assertEqual(self.build()['cache_hits'],23)

    def test_python_vector_memory_does_not_retain_full_corpus(self):
        import tracemalloc
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('DELETE FROM evidence')
            for i in range(2000):
                item={'id':str(i),'kind':'code_member','title':str(i),'text':'code '+str(i)}
                db.execute('INSERT INTO evidence VALUES (?,?,?)',(str(i),item['kind'],json.dumps(item)))
        self.provider.passages=lambda texts:[[1.0]*384 for _ in texts]
        tracemalloc.start()
        try:
            result=self.build(batch_size=4)
            peak=tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(result['embedded'],2000)
        # Isolates the streaming vector pipeline; fixture metadata and native model memory are excluded.
        self.assertLess(peak,8*1024*1024)

    def test_process_exit_releases_lock_and_retains_completed_batch(self):
        import subprocess
        import sys
        program = """
import os,sqlite3,sys
from pathlib import Path
from unity_rag import retrieval
from unity_rag.semantic import build_semantic
workspace=Path(sys.argv[1])
retrieval._open=lambda _: (sqlite3.connect(workspace/'inventory.sqlite3'),{'snapshot':'fixed'})
retrieval.retrieval_status=lambda _: {'snapshot':'fixed'}
class Provider:
    config={'provider':'fixture','model':'stream-v1'}
    def passages(self,texts): return [[len(t),1] for t in texts]
def observe(phase,counts):
    if phase=='batch_complete': os._exit(23)
build_semantic(workspace,Provider(),batch_size=3,observe=observe)
"""
        process=subprocess.run([sys.executable,'-c',program,str(self.workspace)],capture_output=True,timeout=30)
        self.assertEqual(process.returncode,23,process.stderr.decode(errors='replace'))
        self.assertEqual(self.active(),([],[],[]))
        self.assertEqual(self.build()['cache_hits'],3)
