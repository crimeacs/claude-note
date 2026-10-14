"""Importer-to-publication boundary checks; synthetic local fixtures only."""
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

from claude_note import cli, config, egress, importers, import_publication, queue_manager, transcript_reader


class ImportPublicationIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='cn-publication-integration-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        stack = self.enterContext(ExitStack())
        stack.enter_context(mock.patch.multiple(config, STATE_DIR=self.root/'state', QUEUE_DIR=self.root/'queue'))
        stack.enter_context(mock.patch.multiple(importers, TRANSCRIPTS_DIR=self.root/'transcripts',
            SEEN_FILE=self.root/'transcripts/seen.json', CLAUDE_PROJECTS=self.root/'claude',
            CODEX_SESSIONS=self.root/'codex', HEARTBEAT=self.root/'import.ok',
            WATCH_DIRS=[], CURSOR_DB=self.root/'absent.vscdb'))
        self.origin = self.root/'origin.md'
        self.origin.write_text('Public synthetic origin.')
        self.messages = [{'role':'user','text':'Synthetic question'}, {'role':'assistant','text':'Synthetic answer'}]

    def events(self):
        return list(queue_manager.read_all_events())

    def native(self):
        path = importers.CLAUDE_PROJECTS/'project/session-native.jsonl'
        path.parent.mkdir(parents=True)
        records = [
            {'type':'user','sessionId':'session-native','cwd':'/synthetic','message':{'content':'Original synthetic question'}},
            {'type':'assistant','message':{'content':[{'type':'text','text':'Original synthetic answer'}]}}]
        raw = ''.join(json.dumps(row)+'\n' for row in records).encode()
        path.write_bytes(raw)
        old=time.time()-1800;os.utime(path,(old,old))
        return path, raw

    def test_native_snapshot_uses_already_captured_bytes_after_source_mutates(self):
        path,raw=self.native()
        original=transcript_reader.read_transcript
        def read(source,*args,**kwargs):
            content=original(source,*args,**kwargs)
            path.write_bytes(raw.replace(b'Original synthetic answer',b'Later synthetic answer'))
            return content
        with mock.patch.object(transcript_reader,'read_transcript',side_effect=read):
            self.assertEqual(importers.sweep_claude_code({},0),1)
        events=self.events()
        self.assertEqual(len(events),2)
        self.assertTrue(all(event.transcript_path==str(path) for event in events))
        record=import_publication.validate_event(events[0])
        self.assertEqual(Path(record['snapshot_path']).read_bytes(),raw)
        self.assertEqual(record['captured_revision'],hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(path.read_bytes(),raw)

    def test_native_seen_loss_reuses_first_pair_and_snapshot(self):
        self.native()
        self.assertEqual(importers.sweep_claude_code({},0),1)
        first=[event.to_json() for event in self.events()]
        self.assertEqual(importers.sweep_claude_code({},0),0)
        self.assertEqual([event.to_json() for event in self.events()],first)
        self.assertEqual(len([path for path in (config.STATE_DIR/'import-publications').glob('*.json') if len(path.stem)==64]),1)

    def test_normalized_seen_loss_and_second_origin_reuse_first_canonical_bytes(self):
        self.assertTrue(importers.write_session({},'chatgpt','same-conversation',self.messages,origin_sources=[self.origin]))
        first=[event.to_json() for event in self.events()]
        record=import_publication.validate_event(self.events()[0])
        snapshot=Path(record['snapshot_path']);before=snapshot.read_bytes()
        second=self.root/'second.md';second.write_text('Other public synthetic origin.')
        self.assertFalse(importers.write_session({},'chatgpt','same-conversation',self.messages,origin_sources=[second]))
        self.assertEqual([event.to_json() for event in self.events()],first)
        self.assertEqual(snapshot.read_bytes(),before)
        self.assertTrue({str(self.origin),str(second)} <= set(egress.recorded_derivations(snapshot)))

    def test_unchanged_new_origin_freezes_restrictions_on_existing_snapshot(self):
        seen={}
        self.assertTrue(importers.write_session(seen,'chatgpt','same-conversation',self.messages,origin_sources=[self.origin]))
        record=import_publication.validate_event(self.events()[0]);snapshot=Path(record['snapshot_path'])
        private=self.root/'private.md';private.write_text('---\nprivate: true\n---\nPrivate synthetic origin.')
        self.assertFalse(importers.write_session(seen,'chatgpt','same-conversation',self.messages,origin_sources=[private]))
        private.write_text('Authored controls removed.')
        with self.assertRaises(egress.EgressDenied):
            egress.prepare('Derived synthetic content.',audience='model',sources=[str(snapshot)],rules={'default':['model']})
        self.assertEqual(len(self.events()),2)

    def test_metadata_race_is_retryable_without_wrong_session_visibility(self):
        path,raw=self.native()
        original=transcript_reader.read_transcript
        def read(source,*args,**kwargs):
            path.write_bytes(raw.replace(b'session-native',b'session-other'))
            return original(source,*args,**kwargs)
        problems=[];seen={}
        with mock.patch.object(transcript_reader,'read_transcript',side_effect=read):
            self.assertEqual(importers.sweep_claude_code(seen,0,problems=problems),0)
        self.assertEqual(len(problems),1)
        self.assertNotIn('claude-code:'+path.stem,seen)
        self.assertEqual(self.events(),[])

    def test_status_reports_partial_counts_without_resetting_corrupt_state(self):
        from claude_note import version_checker, session_tracker
        path,raw=self.native()
        self.assertEqual(importers.sweep_claude_code({},0),1)
        state_path=session_tracker.get_state_file('session-native')
        state_path.parent.mkdir(parents=True,exist_ok=True)
        state_path.write_text('{"future_field":true}')
        before=state_path.read_bytes()
        (config.QUEUE_DIR/('import-'+'f'*64+'.jsonl')).write_text('Malformed synthetic batch.\n')
        with mock.patch.object(version_checker,'get_update_status',return_value={'update_available':False}), \
             mock.patch.multiple(config,INDEX_PATH=self.root/'absent-index.json',INBOX_PATH=self.root/'absent-inbox.md'), \
             mock.patch('sys.stdout',new_callable=io.StringIO) as stdout:
            result=cli.cmd_status(SimpleNamespace(json=False))
        self.assertEqual(result,1)
        self.assertIn('1 unreadable queue files, 1 incompatible session states',stdout.getvalue())
        self.assertIn('Events:    2',stdout.getvalue())
        self.assertEqual(state_path.read_bytes(),before)


if __name__ == '__main__':
    unittest.main()
