"""Real macOS process churn: a scan result is either None or complete."""
import os
import pathlib
import socket
import subprocess
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import peer  # noqa: E402

CHURN = r'''
import os, socket, sys, time
port = int(sys.argv[1])
keep = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
print(" ".join(str(c.getsockname()[1]) for c in keep), flush=True)
end = time.monotonic() + float(sys.argv[2])
while time.monotonic() < end:
    pid = os.fork()
    if pid == 0:
        try:
            s = socket.create_connection(("127.0.0.1", port)); time.sleep(.002); s.close()
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
'''


class ChurnTests(unittest.TestCase):
    def test_results_under_fork_churn_are_none_or_contain_stable_connections(self):
        server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen(512)
        server.setblocking(False)
        port = server.getsockname()[1]
        proc = subprocess.Popen(['/bin/sh', '-c', '"$0" -c "$1" "$2" 4; true', sys.executable, CHURN, str(port)],
                                stdout=subprocess.PIPE, text=True, start_new_session=True)
        accepted = []
        try:
            stable = {int(p) for p in proc.stdout.readline().split()}
            complete = failed = 0
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    while True:
                        accepted.append(server.accept()[0])
                except BlockingIOError:
                    pass
                tree = peer.tree_endpoints(proc.pid)
                if tree is None:
                    failed += 1
                    continue
                complete += 1
                seen = {e[1] for e in tree if e[3] == port}
                self.assertTrue(stable <= seen, (stable, seen))
            print('churn scans: complete=%d failed_closed=%d' % (complete, failed), flush=True)
            self.assertGreater(complete, 0)
        finally:
            import signal
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # only the group this test created
            except ProcessLookupError:
                pass
            proc.wait()
            proc.stdout.close()
            for c in accepted:
                c.close()
            server.close()


if __name__ == '__main__':
    unittest.main()
