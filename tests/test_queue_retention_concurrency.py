"""Owned public concurrent append/retention fixtures; no production journals."""
import errno
import fcntl
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from claude_note import config, models, queue_manager, session_tracker, state_io


class ConcurrentQueueRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='cn-public-queue-concurrency-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(mock.patch.multiple(config, QUEUE_DIR=self.root/'queue', STATE_DIR=self.root/'state'))
        config.QUEUE_DIR.mkdir()
        self.path = config.QUEUE_DIR/'2000-01-01.jsonl'
        self.old = models.QueuedEvent('public-old', '2000-01-01T00:00:00Z', 'UserPromptSubmit',
                                      'public-session', '', '', {'prompt':'Public captured original'})
        self.new = models.QueuedEvent('public-new', '2026-10-24T00:00:00Z', 'UserPromptSubmit',
                                      'public-session', '', '', {'prompt':'Public concurrent append'})
        self.raw = (self.new.to_json()+'\n').encode()
        self.path.write_bytes((self.old.to_json()+'\n').encode())

    def test_waiting_writer_retries_after_retention_unlinks_captured_inode(self):
        state = models.SessionState(self.old.session_id, self.old.ts, self.old.ts,
                                    processed_event_ids=[self.old.event_id])
        session_tracker.save_session_state(state)
        cleanup_locked, writer_waiting = threading.Event(), threading.Event()
        failures, successes = [], []
        parse, lock = models.QueuedEvent.from_json, fcntl.flock

        def inspect_while_locked(line):
            cleanup_locked.set()
            if not writer_waiting.wait(3):
                raise AssertionError('Public writer did not reach its opened inode lock')
            return parse(line)

        def wait_on_owned_inode(fd, operation):
            if threading.current_thread().name == 'public-append-writer':
                writer_waiting.set()
            return lock(fd, operation)

        def clean():
            try:
                queue_manager.cleanup_old_queue_files()
            except BaseException as exc:
                failures.append(exc)

        def append():
            try:
                state_io.append_line(self.path, self.raw)
                successes.append(True)
            except BaseException as exc:
                failures.append(exc)

        cleaner = threading.Thread(target=clean, name='public-retention-owner')
        writer = threading.Thread(target=append, name='public-append-writer')
        with mock.patch.object(models.QueuedEvent, 'from_json', side_effect=inspect_while_locked), \
             mock.patch.object(state_io.fcntl, 'flock', side_effect=wait_on_owned_inode):
            cleaner.start()
            self.assertTrue(cleanup_locked.wait(3))
            writer.start()
            cleaner.join(3)
            writer.join(3)
        self.assertFalse(cleaner.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(successes, [True])
        self.assertTrue(self.path.exists(), 'A successful append must remain at the current queue path')
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_path_replacement_before_lock_retries_and_preserves_replacement(self):
        lock = fcntl.flock
        replacement = b'Public replacement record\n'
        archived = self.root/'retained-original.jsonl'
        changed = False

        def replace_before_lock(fd, operation):
            nonlocal changed
            if not changed:
                changed = True
                self.path.rename(archived)
                self.path.write_bytes(replacement)
            return lock(fd, operation)

        with mock.patch.object(state_io.fcntl, 'flock', side_effect=replace_before_lock):
            state_io.append_line(self.path, self.raw)
        self.assertEqual(self.path.read_bytes(), replacement+self.raw)
        self.assertEqual(archived.read_bytes(), (self.old.to_json()+'\n').encode())

    def test_continuous_unlink_before_lock_has_bounded_safe_failure(self):
        lock, attempts = fcntl.flock, []

        def remove_before_lock(fd, operation):
            attempts.append(fd)
            self.path.unlink()
            return lock(fd, operation)

        with mock.patch.object(state_io.fcntl, 'flock', side_effect=remove_before_lock), \
             mock.patch.object(state_io.os, 'write', side_effect=AssertionError('Unlinked inode must receive no event bytes')):
            with self.assertRaises(OSError) as caught:
                state_io.append_line(self.path, self.raw)
        self.assertEqual(caught.exception.errno, errno.EAGAIN)
        self.assertEqual(len(attempts), 3)
        self.assertFalse(self.path.exists())

    def test_replacement_after_fsync_retries_same_event_without_deleting_old_copy(self):
        fsync = os.fsync
        archived = self.root/'retained-after-write.jsonl'
        original = self.path.read_bytes()
        replacement = (self.old.to_json()+'\n').encode()
        changed = False

        def replace_after_write(fd):
            nonlocal changed
            fsync(fd)
            if not changed:
                changed = True
                self.path.rename(archived)
                self.path.write_bytes(replacement)

        with mock.patch.object(state_io.os, 'fsync', side_effect=replace_after_write):
            state_io.append_line(self.path, self.raw)
        self.assertEqual(self.path.read_bytes(), replacement+self.raw)
        self.assertEqual(archived.read_bytes(), original+self.raw)
        self.assertEqual(list(queue_manager.read_events(self.path))[-1].event_id, self.new.event_id)


if __name__ == '__main__':
    unittest.main()
