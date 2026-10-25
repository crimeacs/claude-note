"""SQLite diagnostics from public temporary data; no owner sources or services."""

import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from claude_note import importers


class ImporterSQLiteErrorTests(unittest.TestCase):
    def natural_length_error(self, connection):
        connection.execute('CREATE TABLE public_values(key TEXT PRIMARY KEY,value TEXT)')
        connection.executemany('INSERT INTO public_values VALUES(?,?)',
                               [('healthy', 'public-small'), ('oversized', 'PUBLIC_PAYLOAD_' * 400)])
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 512)
        self.assertEqual(connection.execute('SELECT count(key) FROM public_values').fetchone(), (2,))
        with self.assertRaises(sqlite3.DataError) as caught:
            connection.execute('SELECT value FROM public_values WHERE key=?', ('oversized',)).fetchone()
        self.assertEqual(caught.exception.sqlite_errorcode, 18)
        self.assertEqual(connection.execute('SELECT value FROM public_values WHERE key=?',
                                            ('healthy',)).fetchone(), ('public-small',))
        return caught.exception

    def test_natural_sqlite_toobig_retains_authored_primary_metadata(self):
        with sqlite3.connect(':memory:') as connection:
            error = self.natural_length_error(connection)
        detail = importers._source_problem('cursor', 'public-fixture.sqlite', error)
        self.assertEqual(detail, 'cursor: "public-fixture.sqlite": DataError: '
                         'sqlite_errorcode=18; sqlite_primary_name=SQLITE_TOOBIG')
        self.assertNotIn('PUBLIC_PAYLOAD_', detail)

    def test_sqlite_metadata_ignores_raw_name_and_private_exception_args(self):
        class Unprintable:
            def __str__(self):
                raise AssertionError('PRIVATE_NAME_MUST_NOT_BE_READ')
        error = sqlite3.DataError('PRIVATE_EXCEPTION_PAYLOAD')
        error.sqlite_errorcode = 18
        error.sqlite_errorname = Unprintable()
        detail = importers._source_problem('cursor', 'public-fixture.sqlite', error)
        self.assertEqual(detail, 'cursor: "public-fixture.sqlite": DataError: '
                         'sqlite_errorcode=18; sqlite_primary_name=SQLITE_TOOBIG')
        self.assertNotIn('PRIVATE', detail)

    def test_other_supported_primary_code_uses_fixed_name(self):
        error = sqlite3.OperationalError('PRIVATE_BUSY_DETAIL')
        error.sqlite_errorcode = 5
        error.sqlite_errorname = 'PRIVATE_WRONG_NAME'
        self.assertEqual(importers._source_problem('cursor', 'public-fixture.sqlite', error),
                         'cursor: "public-fixture.sqlite": OperationalError: '
                         'sqlite_errorcode=5; sqlite_primary_name=SQLITE_BUSY')

    def test_unrecognized_extended_and_noninteger_codes_remain_redacted(self):
        class UnsafeInt(int):
            def __str__(self):
                raise AssertionError('PRIVATE_COERCION')
            def __int__(self):
                raise AssertionError('PRIVATE_COERCION')
        values = [None, True, False, 'PRIVATE_CODE', b'PRIVATE_CODE', 18.0, [], object(),
                  UnsafeInt(18), -1, 0, 29, 100, 101, 999, (1 << 8) | 18, 1 << 1000]
        for code in values:
            with self.subTest(code_type=type(code).__name__):
                error = sqlite3.DataError('PRIVATE_EXCEPTION_PAYLOAD')
                error.sqlite_errorcode = code
                error.sqlite_errorname = 'PRIVATE_ERROR_NAME'
                detail = importers._source_problem('cursor', 'public-fixture.sqlite', error)
                self.assertEqual(detail, 'cursor: "public-fixture.sqlite": DataError')
                self.assertNotIn('PRIVATE', detail)

    def test_custom_sqlite_subclass_does_not_read_metadata_properties(self):
        reads = []
        class CustomSQLiteError(sqlite3.DataError):
            @property
            def sqlite_errorcode(self):
                reads.append('code')
                raise RuntimeError('PRIVATE_CODE_PROPERTY')
            @property
            def sqlite_errorname(self):
                reads.append('name')
                raise RuntimeError('PRIVATE_NAME_PROPERTY')
        detail = importers._source_problem('cursor', 'public-fixture.sqlite',
                                          CustomSQLiteError('PRIVATE_EXCEPTION_PAYLOAD'))
        self.assertEqual(detail, 'cursor: "public-fixture.sqlite": CustomSQLiteError')
        self.assertEqual(reads, [])
        self.assertNotIn('PRIVATE', detail)

    def test_custom_metaclass_equality_and_metadata_properties_are_not_read(self):
        reads = []
        class EqualityTrap(type):
            def __eq__(cls, other):
                reads.append('class_equality')
                raise AssertionError('PRIVATE_METACLASS_EQUALITY')
        class CustomSQLiteMetaclassError(sqlite3.DataError, metaclass=EqualityTrap):
            @property
            def sqlite_errorcode(self):
                reads.append('code')
                raise RuntimeError('PRIVATE_CODE_PROPERTY')
            @property
            def sqlite_errorname(self):
                reads.append('name')
                raise RuntimeError('PRIVATE_NAME_PROPERTY')
        detail = importers._source_problem('cursor', 'public-fixture.sqlite',
                                          CustomSQLiteMetaclassError('PRIVATE_EXCEPTION_PAYLOAD'))
        self.assertEqual(detail, 'cursor: "public-fixture.sqlite": CustomSQLiteMetaclassError')
        self.assertEqual(reads, [])
        self.assertNotIn('PRIVATE', detail)

    def test_non_sqlite_exception_cannot_supply_sqlite_metadata(self):
        error = ValueError('PRIVATE_EXCEPTION_PAYLOAD')
        error.sqlite_errorcode = 18
        error.sqlite_errorname = 'PRIVATE_ERROR_NAME'
        self.assertEqual(importers._source_problem('cursor', 'public-fixture.sqlite', error),
                         'cursor: "public-fixture.sqlite": ValueError')

    def test_sqlite_error_without_code_preserves_existing_redaction(self):
        self.assertEqual(importers._source_problem('cursor', 'public-fixture.sqlite',
                         sqlite3.DataError('PRIVATE_EXCEPTION_PAYLOAD')),
                         'cursor: "public-fixture.sqlite": DataError')

    def test_actual_cursor_value_limit_does_not_block_healthy_composer(self):
        with tempfile.TemporaryDirectory(prefix='public-sqlite-composers-') as temporary:
            path = Path(temporary) / 'public-cursor.sqlite'
            healthy = {'composerId': 'healthy', 'lastUpdatedAt': int(time.time() * 1000),
                       'conversation': [{'type': 1, 'text': 'Public question'},
                                        {'type': 2, 'text': 'Public answer'}]}
            oversized = {**healthy, 'composerId': 'oversized',
                         'conversation': [{'type': 1, 'text': 'PRIVATE_SYNTHETIC_PAYLOAD_' * 400}]}
            with sqlite3.connect(path) as connection:
                connection.execute('CREATE TABLE cursorDiskKV(key TEXT PRIMARY KEY,value TEXT)')
                connection.executemany('INSERT INTO cursorDiskKV VALUES(?,?)', [
                    ('composerData:oversized', json.dumps(oversized)),
                    ('composerData:healthy', json.dumps(healthy))])
            original = path.read_bytes()
            real_connect = sqlite3.connect
            class LengthLimitedConnection:
                def __init__(self, *args, **kwargs):
                    self.inner = real_connect(*args, **kwargs)
                def execute(self, *args, **kwargs):
                    return self.inner.execute(*args, **kwargs)
                def setlimit(self, category, limit):
                    return self.inner.setlimit(category, 512)
                def close(self):
                    self.inner.close()
            prior_key = 'cursor:' + hashlib.sha256(b'cursor:oversized').hexdigest()[:32]
            seen, problems, report = {prior_key: 'prior-public-revision'}, [], {}
            with mock.patch.object(importers.sqlite3, 'connect', side_effect=LengthLimitedConnection), \
                    mock.patch.object(importers, '_cursor_workspace_folders', return_value={}), \
                    mock.patch.object(importers, 'write_session', return_value=True) as write:
                queued = importers.sweep_cursor(seen, 0, db_path=path, problems=problems, report=report)
            self.assertEqual(queued, 1)
            self.assertEqual(write.call_count, 1)
            self.assertEqual(write.call_args.args[2], 'healthy')
            self.assertEqual(report['composer_failures'], 1)
            self.assertEqual(report['database_scans_completed'], 1)
            self.assertEqual(report['failed_items'], 1)
            self.assertEqual(seen[prior_key], 'prior-public-revision')
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(len(problems), 1)
            self.assertIn('DataError: sqlite_errorcode=18; sqlite_primary_name=SQLITE_TOOBIG', problems[0])
            self.assertNotIn('PRIVATE_SYNTHETIC_PAYLOAD', problems[0])


if __name__ == '__main__':
    unittest.main()
