"""Scoped immutable storage contracts using authored synthetic JSONL only."""

import concurrent.futures
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from claude_note import config, source_store as store, state_io


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class SourceStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="claude-note-source-store-public-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patch = mock.patch.object(config, "STATE_DIR", self.root / "state")
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(config, "QUEUE_DIR", self.root / "queue")
        patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.object(store, "TARGET_CHUNK_BYTES", 64)
        patch.start(); self.addCleanup(patch.stop)
        self.scope = sha(b"synthetic transcript path one")
        self.other = sha(b"synthetic transcript path two")

    def publish(self, raw, scope=None):
        return store.publish(raw, scope=scope or self.scope)

    def manifest(self, path):
        return json.loads(path.read_bytes())

    def test_preserves_all_bytes_lines_trailing_newline_and_real_chunk_anchors(self):
        for raw in (b"", b'{}', b'{}\n', b'{}\r\n{}\n', b'\n\n',
                    (b'{"text":"public synthetic record"}\n' * 12) + b'{"tail":true}'):
            path = self.publish(raw)
            self.assertEqual(path, store.manifest_path(sha(raw), self.scope))
            self.assertEqual(store.read(path, sha(raw), scope=self.scope), raw)
            metadata = store.verify(path, sha(raw), scope=self.scope)
            self.assertEqual(metadata["bytes"], len(raw))
            self.assertEqual(metadata["lines"], raw.count(b"\n") + bool(raw and not raw.endswith(b"\n")))
            segments = store.segments(path, sha(raw), scope=self.scope)
            joined, line = b"", 1
            for index, segment in enumerate(segments):
                body = Path(segment["path"]).read_bytes()
                self.assertEqual(segment["revision"], sha(body))
                self.assertEqual(segment["start_line"], line)
                self.assertEqual(segment["bytes"], len(body))
                if index < len(segments) - 1:
                    self.assertTrue(body.endswith(b"\n"))
                line += segment["lines"]; joined += body
            self.assertEqual(joined, raw)

    def test_append_growth_reuses_prefix_and_is_far_smaller_than_full_snapshots(self):
        raw, captured, paths = b"", [], []
        for i in range(40):
            raw += (json.dumps({"public_synthetic_row":i,"text":"bounded evidence"}) + "\n").encode()
            captured.append(raw); paths.append(self.publish(raw))
        chunk_dir = paths[-1].parent / "chunks"
        chunks = list(chunk_dir.glob("*.jsonl"))
        self.assertLess(sum(p.stat().st_size for p in chunks), sum(map(len, captured)) // 3)
        first = self.manifest(paths[10])["chunks"][0]
        self.assertEqual(first, self.manifest(paths[-1])["chunks"][0])
        for path, body in zip(paths, captured):
            self.assertEqual(store.read(path, sha(body), scope=self.scope), body)
        before = {str(p):p.stat().st_mtime_ns for p in chunk_dir.iterdir()}
        self.assertEqual(self.publish(raw), paths[-1])
        self.assertEqual(before, {str(p):p.stat().st_mtime_ns for p in chunk_dir.iterdir()})

    def test_identical_content_is_namespaced_by_source_scope(self):
        raw = b'{"public":"same text, different source controls"}\n' * 8
        first, second = self.publish(raw), self.publish(raw, self.other)
        self.assertNotEqual(first, second)
        a = store.segments(first, sha(raw), scope=self.scope)
        b = store.segments(second, sha(raw), scope=self.other)
        self.assertEqual(a[0]["revision"], b[0]["revision"])
        self.assertNotEqual(a[0]["path"], b[0]["path"])
        with self.assertRaises(store.SourceStoreError):
            store.verify(first, sha(raw), scope=self.other)

    def test_exact_replay_and_read_do_not_scan_or_allocate_when_disk_is_full(self):
        raw=b'{"public":"retained immutable replay"}\n'*8
        path=self.publish(raw)
        with mock.patch.object(store,"_free_bytes",return_value=0), \
             mock.patch.object(store,"_usage",side_effect=AssertionError("No global scan for exact replay")), \
             mock.patch.object(state_io,"create_bytes",side_effect=AssertionError("No artifact allocation")):
            self.assertEqual(self.publish(raw),path)
            self.assertEqual(store.read(path,sha(raw),scope=self.scope),raw)
            self.assertEqual(store.verify(path,sha(raw),scope=self.scope)["bytes"],len(raw))

    def test_large_single_record_is_preserved_and_verify_streams_without_reconstruction(self):
        raw = b'{"public":"' + b"a" * (2 * store.READ_BYTES + 7) + b'"}\n'
        path = self.publish(raw)
        self.assertEqual(len(self.manifest(path)["chunks"]), 1)
        observed, real = [], store._parts
        def parts(*args, **kwargs):
            for item in real(*args, **kwargs):
                observed.append(len(item)); yield item
        with mock.patch.object(store, "_parts", side_effect=parts), mock.patch.object(store, "read", side_effect=AssertionError("No reconstruction")):
            self.assertEqual(store.verify(path, sha(raw), scope=self.scope)["bytes"], len(raw))
        self.assertLessEqual(max(observed), store.READ_BYTES)
        self.assertEqual(store.read(path, sha(raw), scope=self.scope), raw)

    def test_chunk_corruption_and_missing_chunk_fail_every_reader_and_republication(self):
        raw = b'{"public":"original synthetic evidence"}\n' * 5
        path = self.publish(raw)
        segment = store.segments(path, sha(raw), scope=self.scope)[0]
        chunk = Path(segment["path"]); original = chunk.read_bytes()
        chunk.write_bytes(b"x" + original[1:])
        for reader in (store.read, store.verify, store.segments):
            with self.assertRaises(store.SourceStoreError):
                reader(path, sha(raw), scope=self.scope)
        with self.assertRaises(store.SourceStoreError):
            self.publish(raw)
        chunk.unlink()
        with self.assertRaises(store.SourceStoreError):
            self.publish(raw)
        self.assertFalse(chunk.exists())

    def test_strict_manifest_rejects_duplicates_unknown_fields_unsigned_types_totals_and_junk(self):
        raw = b'{"public":true}\n' * 9
        path = self.publish(raw); original = path.read_bytes(); value = self.manifest(path)
        variants = [original.replace(b'"version":1', b'"version":1,"version":1'), original + b"junk",
                    json.dumps({**value,"extra":"untrusted"}).encode(), json.dumps({**value,"bytes":True}).encode(),
                    json.dumps({**value,"lines":-1}).encode(), json.dumps({**value,"bytes":float(len(raw))}).encode(),
                    json.dumps({**value,"bytes":store.MAX_RAW+1}).encode(), json.dumps({**value,"scope":self.other}).encode(),
                    json.dumps({**value,"captured_revision":"f"*64}).encode(), b"{"+b" "*store.MAX_MANIFEST+b"}",
                    json.dumps(value).encode("utf-16")]
        for body in variants:
            path.write_bytes(body)
            with self.assertRaises((ValueError, OSError)):
                store.verify(path, sha(raw), scope=self.scope)
        path.write_bytes(original)
        changed = json.loads(original); changed["chunks"][0]["lines"] += 1; changed["lines"] += 1
        path.write_text(json.dumps(changed))
        with self.assertRaises(store.SourceStoreError):
            store.verify(path, sha(raw), scope=self.scope)

    def test_manifest_cannot_reorder_valid_chunks_or_join_partial_jsonl_lines(self):
        raw = b'{"public":1}\n'*5 + b'{"public":2}\n'*5
        path = self.publish(raw); value = self.manifest(path)
        value["chunks"] = list(reversed(value["chunks"]))
        path.write_text(json.dumps(value))
        with self.assertRaises(store.SourceStoreError):
            store.verify(path, sha(raw), scope=self.scope)
        # All individual and aggregate hashes match, but a nonfinal chunk ends mid-record.
        body = b'abc\ndef\n'; revision = sha(body); path = store.manifest_path(revision,self.scope)
        pieces = [b'abc', b'\ndef\n']
        for piece in pieces:
            state_io.create_bytes(store._chunk_path(sha(piece),self.scope),piece)
        value={"version":1,"scope":self.scope,"captured_revision":revision,"bytes":len(body),"lines":3,
               "chunks":[{"revision":sha(p),"bytes":len(p),"lines":store._lines(p)} for p in pieces]}
        state_io.create_bytes(path,json.dumps(value).encode())
        with self.assertRaises(store.SourceStoreError):
            store.verify(path,revision,scope=self.scope)

    def test_digest_paths_reject_foreign_paths_symlinks_hardlinks_and_special_files(self):
        raw = b'{"public":"confined"}\n'*4
        path = self.publish(raw)
        for invalid in ("../escape", "A"*64, "f"*63, True):
            with self.assertRaises(store.SourceStoreError):
                self.publish(raw,invalid)
        outside = self.root/'foreign.source.json'; outside.write_bytes(path.read_bytes())
        with self.assertRaises(store.SourceStoreError):
            store.read(outside,sha(raw),scope=self.scope)
        chunk=Path(store.segments(path,sha(raw),scope=self.scope)[0]["path"])
        outside_chunk=self.root/'foreign.jsonl'; outside_chunk.write_bytes(chunk.read_bytes())
        chunk.unlink();chunk.symlink_to(outside_chunk)
        with self.assertRaises((OSError,ValueError)):
            store.verify(path,sha(raw),scope=self.scope)
        chunk.unlink();os.link(outside_chunk,chunk)
        with self.assertRaises(store.SourceStoreError):
            store.verify(path,sha(raw),scope=self.scope)
        with self.assertRaises((ValueError,OSError)):
            self.publish(raw)
        chunk.unlink();os.mkfifo(chunk)
        with self.assertRaises(store.SourceStoreError):
            store.verify(path,sha(raw),scope=self.scope)

    def test_directory_symlink_cannot_escape_store(self):
        outside=self.root/'outside';outside.mkdir()
        objects=config.STATE_DIR/'sources'/'objects';objects.mkdir(parents=True)
        (objects/self.scope).symlink_to(outside,target_is_directory=True)
        with self.assertRaises((OSError,ValueError)):
            self.publish(b'{"public":true}\n')
        self.assertEqual(list(outside.iterdir()),[])

    def test_budget_counts_orphan_allocations_metadata_and_all_scopes_but_excludes_legacy(self):
        legacy=config.STATE_DIR/'sources'/'legacy.jsonl';legacy.parent.mkdir(parents=True);legacy.write_bytes(b"x"*500000)
        raw=b'{"public":"new scope"}\n'*6
        with mock.patch.object(store,"MAX_STORE_BYTES",300000):
            self.publish(raw)
        before=legacy.read_bytes()
        objects=store._root();orphan=objects/'unreferenced';orphan.write_bytes(b"z"*300000)
        with mock.patch.object(store,"MAX_STORE_BYTES",300000), self.assertRaises(store.SourceStoreError):
            self.publish(raw,self.other)
        self.assertEqual(legacy.read_bytes(),before)
        self.assertTrue(orphan.exists())
        self.assertFalse(store.manifest_path(sha(raw),self.other).exists())

    def test_headroom_and_metadata_limit_fail_before_any_new_chunk_or_manifest_write(self):
        raw=b'{"public":"reserve before effects"}\n'*4
        with mock.patch.object(store,"_free_bytes",return_value=store.MIN_FREE_BYTES), \
             mock.patch.object(state_io,"create_bytes",side_effect=AssertionError("No artifact writes")):
            with self.assertRaisesRegex(store.SourceStoreError,"headroom"):
                self.publish(raw)
        self.assertEqual(list(store._root().glob("**/*.jsonl")),[])
        with mock.patch.object(store,"MAX_STORE_ENTRIES",2),self.assertRaisesRegex(store.SourceStoreError,"budget|metadata"):
            self.publish(raw)
        self.assertEqual(list(store._root().glob("**/*.source.json")),[])

    def test_raw_manifest_and_segment_limits_are_checked_before_artifact_writes(self):
        for kwargs,raw in [({"MAX_RAW":10},b"x"*11),({"MAX_SEGMENTS":1,"TARGET_CHUNK_BYTES":1},b"a\nb\n"),({"MAX_MANIFEST":10},b"x")]:
            with mock.patch.multiple(store,**kwargs),mock.patch.object(state_io,"create_bytes",side_effect=AssertionError("No writes")):
                with self.assertRaises(store.SourceStoreError):
                    self.publish(raw)
        with self.assertRaises(store.SourceStoreError):
            self.publish("not bytes")

    def test_interrupted_publication_leaves_durable_chunks_then_idempotent_retry(self):
        raw=b'{"public":"crash-safe evidence"}\n'*8
        real=state_io.create_bytes
        def fail_manifest(path,body):
            if str(path).endswith('.source.json'):
                raise OSError("synthetic crash before manifest visibility")
            return real(path,body)
        with mock.patch.object(state_io,"create_bytes",side_effect=fail_manifest),self.assertRaises(OSError):
            self.publish(raw)
        expected=store.manifest_path(sha(raw),self.scope)
        self.assertFalse(expected.exists())
        chunks=list(expected.parent.glob('chunks/*.jsonl'));self.assertTrue(chunks)
        stamps={p:p.stat().st_mtime_ns for p in chunks}
        self.assertEqual(self.publish(raw),expected)
        self.assertEqual(stamps,{p:p.stat().st_mtime_ns for p in chunks})
        self.assertEqual(store.read(expected,sha(raw),scope=self.scope),raw)

    def test_only_intent_bound_exact_existing_hardlink_recovers_during_publish(self):
        raw=b'{"public":"recognized interrupted link"}\n'*5
        path=self.publish(raw);chunk=Path(store.segments(path,sha(raw),scope=self.scope)[0]["path"])
        temporary=chunk.parent/('.claude-note-'+uuid.uuid4().hex);os.link(chunk,temporary)
        intent={"version":1,"temporary":temporary.name,"revision":sha(chunk.read_bytes()),"size":chunk.stat().st_size}
        state_io.atomic_json(chunk.with_name(chunk.name+'.intent.json'),intent)
        with self.assertRaises(store.SourceStoreError):
            store.verify(path,sha(raw),scope=self.scope)
        self.assertEqual(self.publish(raw),path)
        self.assertFalse(temporary.exists());self.assertEqual(chunk.stat().st_nlink,1)
        self.assertEqual(store.read(path,sha(raw),scope=self.scope),raw)

    def test_global_lock_bounds_contention_and_serializes_different_scopes_budget(self):
        lock=state_io.open_lock(store._root()/'.store.lock')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with mock.patch.object(store,"LOCK_SECONDS",.02),self.assertRaises(TimeoutError):
                self.publish(b'{"public":true}\n')
        finally:os.close(lock)
        # Force the second publisher to observe the first's committed bytes.
        seen=[];real_reserve=store._reserve
        def one_publication(used,entries,sizes):
            seen.append(used)
            if len(seen)>1:
                raise store.SourceStoreError("synthetic aggregate cap")
            return real_reserve(used,entries,sizes)
        def run(scope):
            try:return self.publish(b'{"public":"concurrent"}\n'*8,scope)
            except store.SourceStoreError:return None
        with mock.patch.object(store,"_reserve",side_effect=one_publication),concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(run,[self.scope,self.other]))
        self.assertEqual(sum(p is not None for p in results),1)
        self.assertEqual(len(seen),2);self.assertGreater(seen[1],seen[0])

    def test_two_independent_processes_publish_same_bytes_without_partial_manifest(self):
        raw=b'{"public":"independent process"}\n'*200
        script='''import sys\nfrom pathlib import Path\nfrom claude_note import config,source_store\nconfig.STATE_DIR=Path(sys.argv[1])\nraw=bytes.fromhex(sys.argv[3])\nprint(source_store.publish(raw,scope=sys.argv[2]))\n'''
        env={'PATH':'/usr/bin:/bin','HOME':str(self.root/'home'),'XDG_CONFIG_HOME':str(self.root/'config'),
             'CLAUDE_NOTE_VAULT':str(self.root/'vault'),
             'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
        processes=[subprocess.Popen([sys.executable,'-c',script,str(config.STATE_DIR),self.scope,raw.hex()],env=env,
                                    stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
        outputs=[p.communicate(timeout=10) for p in processes]
        self.assertEqual([p.returncode for p in processes],[0,0])
        self.assertEqual(outputs[0],outputs[1]);self.assertEqual(outputs[0][1],b'')
        path=Path(outputs[0][0].decode().strip())
        self.assertEqual(store.read(path,sha(raw),scope=self.scope),raw)

    def test_auxiliary_admission_uses_explicit_state_root_and_counts_opaque_lineage(self):
        alternate=self.root/'alternate-state'
        ledger=alternate/'.claude-note'/'provenance'/'synthetic.json'
        with store.auxiliary_admission([50000],state_dir=alternate):
            state_io.atomic_bytes(ledger,b'x'*50000)
        # Deliberately opaque bytes: budget accounting must not parse bodies.
        with mock.patch.object(store,"_parts",side_effect=AssertionError("No ledger body reads")):
            used,entries=store._usage(alternate)
        self.assertGreaterEqual(used,50000);self.assertGreaterEqual(entries,3)
        self.assertFalse(store._root().exists())
        self.assertTrue((store._root(alternate)/'.store.lock').is_file())
        reached=[]
        with mock.patch.object(store,"MAX_STORE_BYTES",used+10000),self.assertRaisesRegex(store.SourceStoreError,"budget"):
            with store.auxiliary_admission([50000],state_dir=alternate):reached.append(True)
        self.assertEqual(reached,[])

    def test_auxiliary_denies_headroom_or_bad_sizes_before_yield_and_holds_global_lock(self):
        effects=[]
        with mock.patch.object(store,"_free_bytes",return_value=store.MIN_FREE_BYTES),self.assertRaisesRegex(store.SourceStoreError,"headroom"):
            with store.auxiliary_admission([1000]):effects.append('unsafe')
        self.assertEqual(effects,[])
        for sizes in ([True],[-1],[1.0],[store.MAX_RAW+1],[0]*(store.MAX_SEGMENTS+2),()):
            with mock.patch.object(store,"_store_lock") as lock,self.assertRaises(store.SourceStoreError):
                with store.auxiliary_admission(sizes):effects.append('unsafe')
            lock.assert_not_called()
        def publish_while_held():
            try:self.publish(b'{"public":"held lineage batch"}\n')
            except TimeoutError:return 'blocked'
            return 'overlap'
        with store.auxiliary_admission([4096]),mock.patch.object(store,"LOCK_SECONDS",.02), \
             concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(pool.submit(publish_while_held).result(timeout=2),'blocked')
        self.assertTrue(self.publish(b'{"public":"held lineage batch"}\n').is_file())

    def test_unsafe_lineage_metadata_blocks_new_publication_without_following_alias(self):
        outside=self.root/'outside-ledger';outside.write_bytes(b'opaque public fixture')
        directory=config.STATE_DIR/'.claude-note'/'provenance';directory.mkdir(parents=True)
        alias=directory/'unsafe.json';alias.symlink_to(outside)
        with self.assertRaises(store.SourceStoreError):
            with store.auxiliary_admission([100]):self.fail('unsafe lineage admitted')
        alias.unlink();os.link(outside,alias)
        with self.assertRaises(store.SourceStoreError):self.publish(b'{"public":true}\n')
        self.assertEqual(outside.read_bytes(),b'opaque public fixture')

    def test_auxiliary_shared_deadline_does_not_restart_store_wait_or_admit_after_scan(self):
        effects=[]
        with self.assertRaises(TimeoutError):
            with store.auxiliary_admission([100],deadline=time.monotonic()-1):effects.append(True)
        self.assertFalse(store._root().exists())
        lock=state_io.open_lock(store._root()/'.store.lock')
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            start=time.monotonic()
            with self.assertRaises(TimeoutError):
                with store.auxiliary_admission([100],deadline=start+.02):effects.append(True)
            self.assertLess(time.monotonic()-start,.3)
        finally:os.close(lock)
        tick=[100.0]
        def scan(_):tick[0]=102;return 0,0
        with mock.patch.object(store.time,"monotonic",side_effect=lambda:tick[0]), \
             mock.patch.object(store,"_usage",side_effect=scan),mock.patch.object(store,"_reserve"),self.assertRaises(TimeoutError):
            with store.auxiliary_admission([100],deadline=101):effects.append(True)
        self.assertEqual(effects,[])

    def test_import_and_queue_metadata_count_without_bodies_but_foreign_roots_skip_owner_queue(self):
        raw=b'{"public":"metadata budget"}\n'
        self.publish(raw)
        roots=[config.STATE_DIR/name for name in ('import-streams','import-intents','import-publications')]+[config.QUEUE_DIR]
        for directory in roots:
            directory.mkdir(parents=True,exist_ok=True)
            (directory/'opaque-public-metadata').write_bytes(b'x'*50000)
        with mock.patch.object(store,"_parts",side_effect=AssertionError("No metadata body reads")):
            used,_=store._usage()
            explicit,_=store._usage(config.STATE_DIR)
            alternate,_=store._usage(self.root/'unrelated-state')
        self.assertEqual(used,explicit);self.assertGreaterEqual(used,200000);self.assertEqual(alternate,0)
        with mock.patch.object(store,"MAX_STORE_BYTES",used+10000),self.assertRaisesRegex(store.SourceStoreError,"budget"):
            self.publish(raw,self.other)
        self.assertFalse(store.manifest_path(sha(raw),self.other).exists())

    def test_empty_auxiliary_context_keeps_lock_without_storage_or_headroom_charge(self):
        with mock.patch.object(store,"_usage",side_effect=AssertionError("No allocation scan")), \
             mock.patch.object(store,"_reserve",side_effect=AssertionError("No empty allocation")), \
             mock.patch.object(store,"_free_bytes",return_value=0):
            with store.auxiliary_admission([]):
                lock=state_io.open_lock(store._root()/'.store.lock')
                try:
                    with self.assertRaises(BlockingIOError):fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                finally:os.close(lock)


if __name__ == "__main__":
    unittest.main()
