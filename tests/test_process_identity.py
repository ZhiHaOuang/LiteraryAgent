import ctypes
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lg_cli.process_identity import _ProcBSDInfo, darwin_process_owner
from lg_cli.run_store import RunStore, process_owner


class ProcessIdentityTests(unittest.TestCase):
    def test_real_self_identity_and_run_resume_guard(self):
        identity = process_owner()
        self.assertIsNotNone(identity)
        self.assertEqual(identity, process_owner(os.getpid()))
        self.assertTrue(identity['startTicks'].isdigit())
        self.assertRegex(identity['bootId'], r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
        with tempfile.TemporaryDirectory() as raw:
            store = RunStore(Path(raw))
            handle = store.create(workflow='test', command='outline', request='test', provider='test', model='test')
            self.assertEqual(store.load(handle.run_id)['owner'], identity)
            for confirmed in (False, True):
                with self.assertRaisesRegex(ValueError, 'still executing'):
                    store.assert_resumable(handle.run_id, confirm_stopped=confirmed)

    def test_real_child_termination_removes_identity(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            identity = process_owner(child.pid)
            self.assertIsNotNone(identity)
            self.assertEqual(identity['pid'], child.pid)
            self.assertEqual(identity['bootId'], process_owner()['bootId'])
            self.assertNotEqual(identity['startTicks'], process_owner()['startTicks'])
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertIsNone(process_owner(child.pid))

    def test_real_unreaped_zombie_is_not_a_live_owner(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            deadline = time.monotonic() + 3
            while process_owner(pid) is not None:
                if time.monotonic() >= deadline:
                    self.fail('Unreaped exited child remained a live owner')
                time.sleep(.01)
        finally:
            os.waitpid(pid, 0)

    def test_darwin_sdk_layout_and_exact_microseconds(self):
        self.assertEqual(ctypes.sizeof(_ProcBSDInfo), 136)
        self.assertEqual(_ProcBSDInfo.pbi_start_tvsec.offset, 120)
        self.assertEqual(_ProcBSDInfo.pbi_start_tvusec.offset, 128)
        info = _ProcBSDInfo(pbi_pid=42, pbi_status=2, pbi_start_tvsec=1700000000, pbi_start_tvusec=123456)
        with patch('lg_cli.process_identity._bsd_info', return_value=info), \
                patch('lg_cli.process_identity._boot_id', return_value='a' * 8 + '-aaaa-aaaa-aaaa-' + 'a' * 12):
            first = darwin_process_owner(42)
            info.pbi_start_tvusec += 1
            second = darwin_process_owner(42)
            self.assertEqual(int(second['startTicks']) - int(first['startTicks']), 1)
            self.assertNotEqual(first, second, 'PID reuse with a new exact start time cannot match')
            info.pbi_status = 5
            self.assertIsNone(darwin_process_owner(42))

    def test_darwin_permission_or_unknown_identity_never_means_dead(self):
        for error in (PermissionError('denied'), OSError('unknown'), ValueError('short read')):
            with self.subTest(error=type(error).__name__), patch('lg_cli.process_identity._bsd_info', side_effect=error):
                with self.assertRaises(ValueError): darwin_process_owner(42)
        with patch('lg_cli.process_identity._bsd_info', side_effect=ProcessLookupError()):
            self.assertIsNone(darwin_process_owner(42))
        with patch('lg_cli.process_identity._bsd_info', return_value=_ProcBSDInfo(pbi_pid=43)):
            with self.assertRaises(ValueError): darwin_process_owner(42)
        for pid in (0, -1, True, '42', 2**40):
            with self.assertRaises(ValueError): darwin_process_owner(pid)
