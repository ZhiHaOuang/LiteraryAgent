"""Transient SQLite sidecar removal restarts the entire approval fingerprint."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from lg_cli.mcp_approval import project_fingerprint


class ProjectFingerprintTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.database = self.workspace / '.literarygiant' / 'story.sqlite3'
        self.database.parent.mkdir()
        self.wal = self.database.with_name(self.database.name + '-wal')
        self.connection = sqlite3.connect(str(self.database))
        self.addCleanup(self.connection.close)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA wal_autocheckpoint=0')
        self.connection.execute('CREATE TABLE example(value TEXT)')
        self.connection.commit()
        self.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def test_last_sqlite_connection_closes_after_exists_and_entire_pass_rereads_updated_db(self):
        self.assertTrue(self.wal.exists())
        self.assertEqual(self.wal.read_bytes(), b'')
        baseline = project_fingerprint(self.workspace)
        self.connection.close()
        self.assertFalse(self.wal.exists())
        self.assertEqual(project_fingerprint(self.workspace), baseline)
        self.connection = sqlite3.connect(str(self.database))
        self.addCleanup(self.connection.close)
        self.connection.execute('PRAGMA wal_autocheckpoint=0')
        original_database = self.database.read_bytes()
        self.connection.execute("INSERT INTO example VALUES ('updated committed value')")
        self.connection.commit()
        self.assertGreater(self.wal.stat().st_size, 0)
        self.assertEqual(self.database.read_bytes(), original_database)
        exists = Path.exists
        read_bytes = Path.read_bytes
        closed = []
        database_reads = []

        def last_connection_closes(path):
            present = exists(path)
            if path == self.wal and present and not closed:
                self.connection.close()
                closed.append(True)
                self.assertFalse(exists(self.wal))
            return present

        def read(path):
            data = read_bytes(path)
            if path == self.database:
                database_reads.append(data)
            return data

        with patch.object(Path, 'exists', last_connection_closes), patch.object(Path, 'read_bytes', read):
            result = project_fingerprint(self.workspace)
        self.assertEqual(closed, [True])
        self.assertEqual(len(database_reads), 2)
        self.assertEqual(database_reads[0], original_database)
        self.assertNotEqual(database_reads[1], original_database)
        self.assertEqual(database_reads[1], self.database.read_bytes())
        self.assertEqual(result, project_fingerprint(self.workspace))
        self.assertNotEqual(result, baseline)
        with sqlite3.connect(str(self.database)) as connection:
            self.assertEqual(connection.execute('SELECT value FROM example').fetchall(), [('updated committed value',)])

    def test_persistent_file_disappearance_is_bounded_and_never_returns_a_fingerprint(self):
        reads = {'database': 0, 'wal': 0}
        read_bytes = Path.read_bytes

        def read(path):
            if path == self.database:
                reads['database'] += 1
            if path == self.wal:
                reads['wal'] += 1
                raise FileNotFoundError('PRIVATE_TRANSIENT_PATH')
            return read_bytes(path)

        with patch.object(Path, 'read_bytes', read), self.assertRaisesRegex(ValueError, 'refresh') as failure:
            project_fingerprint(self.workspace)
        self.assertEqual(reads, {'database': 3, 'wal': 3})
        self.assertNotIn('PRIVATE_TRANSIENT_PATH', str(failure.exception))

    def test_permission_error_propagates_without_retry_or_partial_fingerprint(self):
        reads = {'database': 0, 'wal': 0}
        read_bytes = Path.read_bytes
        denied = PermissionError('Fingerprint state is not readable.')

        def read(path):
            if path == self.database:
                reads['database'] += 1
            if path == self.wal:
                reads['wal'] += 1
                raise denied
            return read_bytes(path)

        with patch.object(Path, 'read_bytes', read), self.assertRaises(PermissionError) as failure:
            project_fingerprint(self.workspace)
        self.assertIs(failure.exception, denied)
        self.assertEqual(reads, {'database': 1, 'wal': 1})


if __name__ == '__main__':
    unittest.main()
