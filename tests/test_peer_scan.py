"""Deterministic adversarial tests for peer.tree_endpoints: a fake kernel replaces the
libproc layer so every truncation, failed inspection and churn case is exact."""
import ctypes
import os
import pathlib
import socket
import struct
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import peer  # noqa: E402

UID = os.getuid()
GW = ('127.0.0.1', 9000)


def tcp_info(lport, fport=GW[1], laddr='127.0.0.1', faddr=GW[0]):
    raw = bytearray(peer.SOCKET_FDINFO_SIZE)
    struct.pack_into('i', raw, peer._SOI + 136 + 8 + 8 + 4 + 4, socket.AF_INET)
    struct.pack_into('i', raw, peer._SOI_KIND, peer.SOCKINFO_TCP)
    struct.pack_into('ii', raw, peer._SOI_PROTO, socket.htons(fport), socket.htons(lport))
    raw[peer._SOI_PROTO + 32 + 12:peer._SOI_PROTO + 48] = socket.inet_aton(faddr)
    raw[peer._SOI_PROTO + 48 + 12:peer._SOI_PROTO + 64] = socket.inet_aton(laddr)
    return bytes(raw)


class Kernel:
    """procs: pid -> {'ppid', 'start', 'fds': {fd: port or 'file'}}; hooks may mutate."""

    def __init__(self):
        self.procs = {}
        self.fail_fdinfo = set()   # (pid, fd)
        self.fail_listfds = set()  # pid
        self.on_list = {}          # pid -> callable run when its fds are listed (churn)
        self.child_overflow = set()

    def add(self, pid, ppid, start=None, **fds):
        self.procs[pid] = {'ppid': ppid, 'start': start or pid * 1000, 'uid': UID,
                           'fds': {int(k[2:]): v for k, v in fds.items()}}

    def bsd(self, pid):
        p = self.procs.get(pid)
        return None if p is None else (p['ppid'], p['uid'], p['start'])

    def children(self, pid):
        if pid in self.child_overflow:
            return None
        return [c for c, p in self.procs.items() if p['ppid'] == pid]

    def listfds(self, pid):
        hook = self.on_list.pop(pid, None)
        if hook:
            hook()
        p = self.procs.get(pid)
        if p is None or pid in self.fail_listfds:
            return None
        return b''.join(struct.pack('iI', fd, peer.PROX_FDTYPE_SOCKET if v != 'file' else 1)
                        for fd, v in p['fds'].items())

    def fdinfo(self, pid, fd):
        p = self.procs.get(pid)
        if p is None or (pid, fd) in self.fail_fdinfo or fd not in p['fds']:
            return None
        return tcp_info(p['fds'][fd])

    def install(self, case):
        for name, fn in (('_bsdinfo_unchecked', self.bsd), ('children', self.children),
                         ('_listfds', self.listfds), ('_fdinfo', self.fdinfo)):
            patch = mock.patch.object(peer, name, side_effect=fn)
            patch.start(); case.addCleanup(patch.stop)
        patch = mock.patch.object(peer, 'self_test', return_value=True)
        patch.start(); case.addCleanup(patch.stop)


def ep(port):
    return ('127.0.0.1', port, GW[0], GW[1])


class TreeScanTests(unittest.TestCase):
    def setUp(self):
        self.k = Kernel(); self.k.install(self)
        self.k.add(100, 1, fd3=5001)
        self.k.add(101, 100, fd4=5002, fd5='file')

    def test_complete_scan_reports_every_connection(self):
        self.assertEqual(peer.tree_endpoints(100, 100000), {ep(5001): 100, ep(5002): 101})

    def test_live_child_unreadable_fd_list_fails_closed(self):
        # The review's reproduction: a live child whose socket scan fails.
        self.k.fail_listfds.add(101)
        self.assertIsNone(peer.tree_endpoints(100))

    def test_listed_child_with_unreadable_identity_fails_closed(self):
        # Failure to read bsdinfo is not proof that a listed process has exited.
        with mock.patch.object(peer, '_bsdinfo_unchecked', side_effect=lambda pid: None if pid == 101 else self.k.bsd(pid)):
            self.assertIsNone(peer.tree_endpoints(100))

    def test_child_identity_unreadable_at_recheck_fails_closed(self):
        reads = 0
        def bsd(pid):
            nonlocal reads
            if pid == 101:
                reads += 1
                if reads >= 3:
                    return None
            return self.k.bsd(pid)
        with mock.patch.object(peer, '_bsdinfo_unchecked', side_effect=bsd):
            self.assertIsNone(peer.tree_endpoints(100))

    def test_failed_socket_inspection_fails_closed(self):
        self.k.fail_fdinfo.add((101, 4))
        self.assertIsNone(peer.tree_endpoints(100))

    def test_non_socket_fd_failure_is_irrelevant(self):
        self.k.fail_fdinfo.add((101, 5))  # fd 5 is a file: never inspected
        self.assertEqual(peer.tree_endpoints(100), {ep(5001): 100, ep(5002): 101})

    def test_possibly_truncated_child_list_fails_closed(self):
        self.k.child_overflow.add(100)
        self.assertIsNone(peer.tree_endpoints(100))

    def test_transient_fd_close_during_scan_is_retried_not_trusted(self):
        # fd listed, then closed before inspection (real churn): the scan is discarded,
        # and a fresh complete scan is used.
        def close_fd():
            self.k.fail_fdinfo.add((101, 4))
            self.k.on_list[101] = lambda: self.k.fail_fdinfo.discard((101, 4))
        self.k.on_list[101] = close_fd
        self.assertEqual(peer.tree_endpoints(100), {ep(5001): 100, ep(5002): 101})

    def test_persistent_failure_exhausts_bounded_retries(self):
        self.k.fail_fdinfo.add((101, 4))
        with mock.patch.object(peer, '_scan_tree', wraps=peer._scan_tree) as scans:
            self.assertIsNone(peer.tree_endpoints(100))
        self.assertEqual(scans.call_count, peer.SCAN_ATTEMPTS)

    def test_child_forked_during_scan_fails_that_scan(self):
        def fork():
            if 102 not in self.k.procs:
                self.k.add(102, 101, fd6=5003)
        self.k.on_list[101] = fork
        # First scan misses 102 and is rejected; the retry sees it.
        self.assertEqual(peer.tree_endpoints(100), {ep(5001): 100, ep(5002): 101, ep(5003): 102})

    def test_pid_reused_after_its_scan_fails_that_scan(self):
        reads = {'n': 0}
        real = self.k.bsd
        def bsd(pid):
            value = real(pid)
            if pid == 101:
                reads['n'] += 1
                if reads['n'] == 3:  # end-of-scan recheck: 101 is now another process
                    self.k.procs[101]['start'] += 1
                    self.k.procs[101]['fds'] = {7: 5009}
                    value = real(pid)
            return value
        peer._bsdinfo_unchecked.side_effect = bsd
        with mock.patch.object(peer, '_scan_tree', wraps=peer._scan_tree) as scans:
            result = peer.tree_endpoints(100)
        self.assertEqual(scans.call_count, 2)  # the first, inconsistent scan was discarded
        self.assertEqual(result, {ep(5001): 100, ep(5009): 101})

    def test_exited_child_owns_nothing(self):
        del self.k.procs[101]
        self.assertEqual(peer.tree_endpoints(100), {ep(5001): 100})

    def test_other_uid_or_wrong_root_start_fails_closed(self):
        self.k.procs[101]['uid'] = UID + 1
        self.assertIsNone(peer.tree_endpoints(100))
        self.k.procs[101]['uid'] = UID
        self.assertIsNone(peer.tree_endpoints(100, 1))

    def test_duplicate_claim_fails_closed(self):
        self.k.procs[101]['fds'][4] = 5001
        self.assertIsNone(peer.tree_endpoints(100))


class FakeLib:
    def __init__(self, need, written):
        self.need, self.written = need, written

    def proc_pidinfo(self, pid, flavor, arg, buf, size):
        if buf is None:
            return self.need
        n = min(self.written, size)
        if n > 0:
            ctypes.memset(buf, 0, n)
        return n


class ListFdsTests(unittest.TestCase):
    def check(self, need, written):
        with mock.patch.object(peer, '_libproc', return_value=FakeLib(need, written)):
            return peer._listfds(1)

    def test_full_buffer_is_treated_as_truncated(self):
        cap = 80 + peer.FD_SLACK * peer.FD_ENTRY
        self.assertIsNone(self.check(80, cap))       # filled exactly: maybe cut off
        self.assertIsNone(self.check(80, cap * 2))   # silently truncated
        self.assertEqual(len(self.check(80, 80)), 80)

    def test_errors_and_misaligned_or_oversized_lists_fail(self):
        self.assertIsNone(self.check(0, 0))
        self.assertIsNone(self.check(-1, 0))
        self.assertIsNone(self.check(81, 81))
        self.assertIsNone(self.check(80, 81))
        self.assertIsNone(self.check((peer.MAX_FDS + 1) * peer.FD_ENTRY, 8))


class RealKernelTests(unittest.TestCase):
    def test_real_scan_includes_a_new_connection(self):
        server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(1)
        client = socket.create_connection(server.getsockname())
        accepted, _ = server.accept()
        try:
            tree = peer.tree_endpoints(os.getpid())
            self.assertIn(tuple(client.getsockname() + client.getpeername()), tree)
        finally:
            for s in (accepted, client, server):
                s.close()


if __name__ == '__main__':
    unittest.main()
