"""Read-only indexed revision proof on disposable SQLite databases only."""

import hashlib
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
        operations = [('UPDATE documents SET active=0', ()),
                      ('UPDATE documents SET hash=?', ('a' * 64,)),
                      ('UPDATE documents SET path=?', ('different.md',)),
                      ('UPDATE documents SET collection=?', ('other',))]
        for sql, args in operations:
            with self.subTest(sql=sql):
                self.mutate(sql, args)
                self.assertFalse(self.verify()['lexical'])
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
        node, qmd = shutil.which('node'), shutil.which('qmd')
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
        node, qmd = shutil.which('node'), shutil.which('qmd')
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
