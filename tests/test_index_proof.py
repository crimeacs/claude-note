"""Read-only indexed revision proof on disposable SQLite databases only."""

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_note import index_proof, setup_recall


class IndexProofTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='claude-note-proof-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / 'index.sqlite'
        self.configuration = self.root / 'index.yml'
        self.configuration.write_text('collections: {}\n')
        self.body = '# Synthetic\n\nExact captured revision for read-only proof.\n'
        self.revision = hashlib.sha256(self.body.encode()).hexdigest()
        self.references = ({'collection': 'synthetic', 'path': 'first.md', 'revision': self.revision},)
        with sqlite3.connect(self.database) as db:
            db.executescript('CREATE TABLE documents(id INTEGER PRIMARY KEY,collection TEXT,path TEXT,title TEXT,hash TEXT,active INTEGER);'
                'CREATE TABLE content(hash TEXT PRIMARY KEY,doc TEXT);'
                "CREATE VIRTUAL TABLE documents_fts USING fts5(filepath,title,body,tokenize='porter unicode61');")
            db.execute('INSERT INTO documents VALUES(1,?,?,?,?,1)', ('synthetic', 'first.md', 'Synthetic', self.revision))
            db.execute('INSERT INTO content VALUES(?,?)', (self.revision, self.body))
            db.execute('INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(1,?,?,?)',
                       ('synthetic/first.md', 'Synthetic', self.body))

    def verify(self, *, vector=False, references=None, qmd=''):
        return index_proof.verify(database=str(self.database), configuration=str(self.configuration),
            qmd=qmd, references=self.references if references is None else references, vector=vector)

    def mutate(self, sql, parameters=()):
        with sqlite3.connect(self.database) as db:
            db.execute(sql, parameters)

    def test_lexical_exact_hash_and_fts_proof_without_qmd_or_writes(self):
        before = self.database.read_bytes()
        with mock.patch.object(setup_recall, 'qmd_sdk', side_effect=AssertionError('Lexical needs no SDK')):
            self.assertEqual(self.verify(), {'lexical': True, 'vector': False, 'reason': ''})
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse(Path(str(self.database) + '-wal').exists())

    def test_active_document_hash_path_and_collection_are_exact(self):
        operations = [('UPDATE documents SET active=0', (), 'proof_revision_inactive'),
                      ('UPDATE documents SET hash=?', ('a' * 64,), 'proof_revision_mismatch'),
                      ('UPDATE documents SET path=?', ('different.md',), 'proof_revision_absent'),
                      ('UPDATE documents SET collection=?', ('other',), 'proof_revision_absent')]
        for sql, args, reason in operations:
            with self.subTest(sql=sql):
                self.mutate(sql, args)
                with mock.patch.object(setup_recall, 'qmd_sdk', return_value=None):
                    self.assertEqual(self.verify(), {'lexical': False, 'vector': False, 'reason': reason})
                self.mutate('UPDATE documents SET active=1,hash=?,path=?,collection=?',
                            (self.revision, 'first.md', 'synthetic'))

    def test_content_hash_cannot_be_asserted_by_metadata_alone(self):
        self.mutate('UPDATE content SET doc=?', ('Synthetic wrong body',))
        self.assertFalse(self.verify()['lexical'])

    def test_missing_or_stale_fts_body_never_proves_lexical_success(self):
        self.mutate('DELETE FROM documents_fts WHERE rowid=1')
        self.assertFalse(self.verify()['lexical'])
        self.mutate('INSERT INTO documents_fts(rowid,filepath,title,body) VALUES(1,?,?,?)',
                    ('synthetic/first.md', 'Synthetic', 'Different indexed body'))
        self.assertFalse(self.verify()['lexical'])

    def test_indexed_content_and_fts_fields_are_bounded_before_fetch(self):
        for sql, size in [('UPDATE content SET doc=?', index_proof.MAX_BODY_BYTES + 1),
                          ('UPDATE documents SET title=?', 16385),
                          ('UPDATE documents_fts SET title=?', 16385),
                          ('UPDATE documents_fts SET filepath=?', 16385),
                          ('UPDATE documents_fts SET body=?', index_proof.MAX_BODY_BYTES + 1)]:
            with self.subTest(sql=sql):
                before = self.database.read_bytes()
                self.mutate(sql, ('x' * size,))
                self.assertFalse(self.verify()['lexical'])
                self.database.write_bytes(before)

    def test_missing_vector_runtime_preserves_proved_lexical_stage(self):
        result = self.verify(vector=True)
        self.assertTrue(result['lexical'])
        self.assertFalse(result['vector'])
        self.assertTrue(result['reason'])

    def test_empty_invalid_duplicate_or_traversal_references_fail_closed(self):
        for refs in ((), self.references * 2, ({**self.references[0], 'path': '../escape.md'},),
                     ({**self.references[0], 'revision': 'bad'},),
                     ({**self.references[0], 'path': '/absolute.md'},)):
            with self.subTest(refs=refs):
                result = self.verify(references=refs)
                self.assertFalse(result['lexical'] or result['vector'])

    def test_unsupported_fts_schema_is_not_migrated(self):
        with sqlite3.connect(self.database) as db:
            db.executescript('DROP TABLE documents_fts; CREATE TABLE documents_fts(filepath TEXT,title TEXT,body TEXT);')
        before = self.database.read_bytes()
        self.assertFalse(self.verify()['lexical'])
        self.assertEqual(self.database.read_bytes(), before)

    def test_sqlite_allocator_cap_is_required_before_schema_reads(self):
        for value in (None, (0,), (-1,), (True,), (index_proof.SQLITE_HEAP_BYTES + 1,)):
            database = mock.Mock()
            database.execute.return_value.fetchone.return_value = value
            with self.subTest(value=value), mock.patch.object(index_proof.sqlite3, 'connect', return_value=database):
                with self.assertRaisesRegex(ValueError, 'proof_sqlite_bound_unavailable'):
                    index_proof._lexical(self.database, self.references, time.monotonic() + 2)
                self.assertEqual(database.execute.call_count, 1)
                self.assertTrue(database.execute.call_args.args[0].startswith('PRAGMA hard_heap_limit='))

    def test_arbitrary_dependency_exception_cannot_supply_a_reason_code_or_private_text(self):
        secret = 'synthetic-private-dependency-sentinel'
        error = ValueError(secret)
        error.code = 'proof_revision_inactive'
        with mock.patch.object(index_proof, '_run', side_effect=error):
            answer = self.verify()
        self.assertEqual(answer, {'lexical': False, 'vector': False, 'reason': 'proof_internal_failure'})
        self.assertNotIn(secret, json.dumps(answer))

    def test_lexical_child_sanitizes_dependency_exception_fields_and_text(self):
        payload = {'database': str(self.database), 'references': self.references, 'remaining': 1}
        error = sqlite3.OperationalError('synthetic-private-sqlite-sentinel')
        error.code = 'proof_revision_inactive'
        source = mock.Mock(buffer=io.BytesIO(json.dumps(payload).encode()))
        output = io.StringIO()
        with mock.patch.object(index_proof.sys, 'stdin', source), \
                mock.patch.object(index_proof.sys, 'stdout', output), \
                mock.patch.object(index_proof, '_lexical', side_effect=error):
            index_proof._lexical_main()
        self.assertEqual(json.loads(output.getvalue()),
                         {'lexical': False, 'vector': False, 'reason': 'proof_internal_failure'})

    def test_observer_protocol_rejects_unregistered_reason_text(self):
        script = 'print(' + repr(json.dumps({'lexical': False, 'vector': False,
            'reason': 'synthetic-private-observer-sentinel'})) + ')'
        with self.assertRaisesRegex(ValueError, '^proof_output_invalid$'):
            index_proof._run({}, [sys_executable(), '-I', '-c', script], time.monotonic() + 2)

    def test_native_free_observer_reason_contracts(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is optional for observer reason contracts')
        environment = {key: value for key, value in os.environ.items()
                       if key not in {'NODE_OPTIONS', 'NODE_PATH', 'BUN_OPTIONS', 'CLAUDE_NOTE_TEST_QMD_SDK'}}
        child = subprocess.run([node, '--test', str(Path(__file__).with_name('index-proof.test.mjs'))],
                               capture_output=True, text=True, env=environment, timeout=5)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)

    def test_large_text_rows_fail_closed_with_bounded_python_and_native_rss(self):
        self.mutate('UPDATE content SET doc=CAST(zeroblob(?) AS TEXT)', (64 * 1024 * 1024,))
        reader = '''
import json,resource,sys,time
sys.path.insert(0,sys.argv[1])
from claude_note.index_proof import _lexical
from pathlib import Path
scale=1 if sys.platform=='darwin' else 1024
before=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*scale
status='unexpected_success'
try:
 _lexical(Path(sys.argv[2]), tuple(json.loads(sys.argv[3])), time.monotonic()+8)
except Exception as exc:
 status=type(exc).__name__
after=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*scale
print(json.dumps({'growth':after-before,'status':status}))
'''
        source = str(Path(index_proof.__file__).resolve().parents[1])
        child = subprocess.run([sys_executable(), '-B', '-I', '-c', reader, source,
            str(self.database), json.dumps(self.references)], capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        python_result = json.loads(child.stdout)
        self.assertNotEqual(python_result['status'], 'unexpected_success')
        self.assertLess(python_result['growth'], 24 * 1024 * 1024)
        self.assertFalse(self.verify()['lexical'])
        qmd = shutil.which('qmd')
        node = (setup_recall.wrapper_node(qmd) if qmd else None) or shutil.which('node')
        sdk = setup_recall.qmd_sdk(qmd) if qmd else None
        if node and sdk:
            script = '''
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {dirname,join} from 'node:path';
const [sdk,observer,database,references]=process.argv.slice(1);
const requireSDK=createRequire(pathToFileURL(sdk));
const Database=requireSDK('better-sqlite3');
await import(pathToFileURL(join(dirname(sdk),'store.js')).href);
await import(pathToFileURL(join(dirname(sdk),'llm.js')).href);
const {prove}=await import(pathToFileURL(observer).href);
const before=process.resourceUsage().maxRSS*1024;
const result=await prove({sdk,database,references:JSON.parse(references),vector:false});
const after=process.resourceUsage().maxRSS*1024;
const db=new Database(':memory:');
const heap=db.pragma('hard_heap_limit',{simple:true}); db.close();
console.log(JSON.stringify({result,growth:after-before,heap}));
'''
            child = subprocess.run([node, '--input-type=module', '-e', script,
                str(sdk), str(Path(index_proof.__file__).with_suffix('.mjs')), str(self.database),
                json.dumps(self.references)], capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            native = json.loads(child.stdout)
            self.assertFalse(native['result']['lexical'] or native['result']['vector'])
            self.assertTrue(0 < native['heap'] <= index_proof.SQLITE_HEAP_BYTES)
            self.assertLess(native['growth'], 24 * 1024 * 1024)
            # FTS xColumn materializes its value even for a length query.
            # Corrupt a standalone shadow row while leaving ordinary content
            # small, then require the metadata-only shadow check to reject it.
            self.mutate('UPDATE content SET doc=?', (self.body,))
            for column in ('c0', 'c1', 'c2'):
                with self.subTest(fts_shadow_column=column):
                    self.mutate('UPDATE documents_fts_content SET c0=?,c1=?,c2=? WHERE id=1',
                                ('synthetic/first.md', 'Synthetic', self.body))
                    self.mutate('UPDATE documents_fts_content SET ' + column + '=CAST(zeroblob(?) AS TEXT) WHERE id=1',
                                (64 * 1024 * 1024,))
                    child = subprocess.run([node, '--input-type=module', '-e', script,
                        str(sdk), str(Path(index_proof.__file__).with_suffix('.mjs')), str(self.database),
                        json.dumps(self.references)], capture_output=True, text=True, timeout=10)
                    self.assertEqual(child.returncode, 0, child.stderr)
                    native = json.loads(child.stdout)
                    self.assertFalse(native['result']['lexical'] or native['result']['vector'])
                    self.assertLess(native['growth'], 24 * 1024 * 1024)
                    self.assertFalse(self.verify()['lexical'])

    def test_native_observer_deadline_and_output_bound(self):
        deadline = time.monotonic() + .1
        with self.assertRaises(TimeoutError):
            index_proof._run({}, [sys_executable(), '-I', '-c', 'import time; time.sleep(60)'], deadline)
        self.assertLess(time.monotonic() - deadline, 1)
        with self.assertRaises(ValueError):
            index_proof._run({}, [sys_executable(), '-I', '-c', 'print("x"*100000)'], time.monotonic() + 2)

    def test_child_environment_clears_runtime_injection_without_changing_model_selector(self):
        script = ('import os,json; assert all(k not in os.environ for k in ["NODE_OPTIONS","NODE_PATH","BUN_OPTIONS"]); '
                  'assert os.environ["QMD_EMBED_MODEL"]=="synthetic-model"; '
                  'print(json.dumps({"lexical":True,"vector":False,"reason":""}))')
        with mock.patch.dict(os.environ, {'NODE_OPTIONS': '--bad', 'NODE_PATH': '/synthetic',
            'BUN_OPTIONS': '--bad', 'QMD_EMBED_MODEL': 'synthetic-model'}):
            answer = index_proof._run({}, [sys_executable(), '-I', '-c', script], time.monotonic() + 2)
        self.assertTrue(answer['lexical'])

    def test_native_synthetic_vector_contracts(self):
        qmd = shutil.which('qmd')
        node = (setup_recall.wrapper_node(qmd) if qmd else None) or shutil.which('node')
        sdk = setup_recall.qmd_sdk(qmd) if qmd else None
        if not node or not sdk:
            self.skipTest('Installed pinned optional QMD SDK is required for native synthetic contracts')
        environment = {key: value for key, value in os.environ.items()
                       if key not in {'NODE_OPTIONS', 'NODE_PATH', 'BUN_OPTIONS'}}
        environment['CLAUDE_NOTE_TEST_QMD_SDK'] = str(sdk)
        test = Path(__file__).with_name('index-proof.test.mjs')
        child = subprocess.run([node, '--test', str(test)], capture_output=True, text=True,
                               env=environment, timeout=20)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)

    def test_vector_process_protocol_proves_real_prebuilt_vectors_read_only(self):
        qmd = shutil.which('qmd')
        node = (setup_recall.wrapper_node(qmd) if qmd else None) or shutil.which('node')
        sdk = setup_recall.qmd_sdk(qmd) if qmd else None
        if not node or not sdk:
            self.skipTest('Installed pinned optional QMD SDK is required for native proof')
        script = '''
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {dirname,join} from 'node:path';
const [sdk,database,hash]=process.argv.slice(1);
const requireSDK=createRequire(pathToFileURL(sdk));
const Database=requireSDK('better-sqlite3');
const {getEmbeddingFingerprint}=await import(pathToFileURL(join(dirname(sdk),'store.js')).href);
const db=new Database(database); requireSDK('sqlite-vec').load(db);
db.exec('CREATE TABLE content_vectors(hash TEXT,seq INTEGER,pos INTEGER,model TEXT,embed_fingerprint TEXT,total_chunks INTEGER,PRIMARY KEY(hash,seq)); CREATE VIRTUAL TABLE vectors_vec USING vec0(hash_seq TEXT PRIMARY KEY,embedding float[2] distance_metric=cosine)');
for(let seq=0;seq<2;seq++){
 db.prepare('INSERT INTO content_vectors VALUES(?,?,?,?,?,2)').run(hash,seq,seq*100,'synthetic-model',getEmbeddingFingerprint('synthetic-model'));
 db.prepare('INSERT INTO vectors_vec(hash_seq,embedding) VALUES(?,?)').run(hash+'_'+seq,new Float32Array([1,.5+seq]));
} db.close();
'''
        child = subprocess.run([node, '--input-type=module', '-e', script, str(sdk), str(self.database), self.revision],
                               capture_output=True, text=True, timeout=5)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.configuration.write_text('models:\n  embed: synthetic-model\n')
        before = self.database.read_bytes()
        started = time.monotonic()
        self.assertEqual(self.verify(vector=True, qmd=qmd), {'lexical': True, 'vector': True, 'reason': ''})
        self.assertLess(time.monotonic() - started, index_proof.TIMEOUT_SECONDS)
        self.assertEqual(self.database.read_bytes(), before)


def sys_executable():
    import sys
    return sys.executable


if __name__ == '__main__':
    unittest.main()
