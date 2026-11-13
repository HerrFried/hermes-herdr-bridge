#!/usr/bin/env python3
"""Opt-in interactive launcher; never modifies the vendor executable."""
import json
import os
import queue
import signal
import stat
import subprocess
import sys
import threading
import time

import peer
import relay

HEARTBEAT_SECONDS = 60 * 60
CORRELATION_CHECK_SECONDS = 1.0
LEGACY_COMMAND_TIMEOUT = 1.0
LEGACY_RETRY_DELAY = .1
LEGACY_CLOSE_WAIT = 2 * LEGACY_COMMAND_TIMEOUT + .25


def interactive_args(args):
    # Fail closed on commands, unrecognised flags, profile and one-shot modes.
    value_flags = {'--resume', '--skills'}
    switches = {'--continue', '-c'}
    it = iter(args)
    for arg in it:
        if arg in switches:
            continue
        if arg in value_flags:
            value = next(it, None)
            if value is None or value.startswith('-'):
                return False
            continue
        return False
    return True


def legacy_enabled():
    return (os.getenv('HERDR_ENV') == '1' and all(os.getenv(x) for x in
            ('HERDR_PANE_ID', 'HERDR_SOCKET_PATH', 'HERDR_BIN_PATH')))


class Reporter:
    def __init__(self, nonce):
        self.desktop = bool(os.getenv('HC_TERMINAL_ID'))
        self.legacy = legacy_enabled()
        self.source = 'hermes-bridge-' + nonce
        self.seq = 0
        self.used = False
        self.items = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._worker, daemon=True)

    def start(self):
        if self.legacy:
            self.thread.start()

    def _queue(self, state):
        try:
            self.items.put_nowait(state)
        except queue.Full:
            try: self.items.get_nowait()
            except queue.Empty: pass
            try: self.items.put_nowait(state)
            except queue.Full: pass

    def _worker(self):
        empty = object()
        pending = empty
        while True:
            state = self.items.get() if pending is empty else pending
            pending = empty
            if state is None:
                return
            if not isinstance(state, str):
                continue
            for attempt in range(2):
                self.seq += 1
                action = 'release-agent' if state == 'release' else 'report-agent'
                args = [os.environ['HERDR_BIN_PATH'], 'pane', action, os.environ['HERDR_PANE_ID'],
                        '--source', self.source, '--agent', 'hermes', '--seq', str(self.seq)]
                if state != 'release':
                    args.extend(['--state', state])
                try:
                    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, timeout=LEGACY_COMMAND_TIMEOUT, check=False)
                    succeeded = result.returncode == 0
                except (OSError, subprocess.SubprocessError):
                    succeeded = False
                if state == 'release':
                    break  # not retried: exit and clears stay bounded
                if succeeded or attempt:
                    break
                # A newer state or release supersedes the failed report.
                try:
                    pending = self.items.get(timeout=LEGACY_RETRY_DELAY)
                except queue.Empty:
                    continue
                break

    def report(self, state):
        self.used = True
        if self.desktop:
            try: os.write(1, ('\x1b]7501;state=' + state + ':app=hermes\x1b\\').encode())
            except OSError: pass
        if self.legacy:
            self._queue('idle' if state in ('done', 'error', 'idle') else state)

    def clear(self):
        """Withdraw our reported state on ambiguity; a later fresh turn may report again."""
        if not self.used:
            return
        self.used = False
        if self.desktop:
            try: os.write(1, b'\x1b]7501;state=clear\x1b\\')
            except OSError: pass
        if self.legacy:
            # The long-lived worker releases without exiting; never block the listener.
            self._queue('release')

    def close(self):
        if self.used and self.desktop:
            try: os.write(1, b'\x1b]7501;state=clear\x1b\\')
            except OSError: pass
        if self.legacy and self.thread.is_alive():
            # Release is coalesced with any pending report; then the worker stops.
            if self.used:
                self._queue('release')
            self._queue_stop_after_release()

    def _queue_stop_after_release(self):
        deadline = time.monotonic() + LEGACY_CLOSE_WAIT
        while self.thread.is_alive() and not self.items.empty() and time.monotonic() < deadline:
            time.sleep(.01)  # let the worker take release before the stop sentinel
        try: self.items.put(None, timeout=max(0, deadline - time.monotonic()))
        except queue.Full: pass
        self.thread.join(max(0, deadline - time.monotonic()))  # bounded


ACTIVE = ('working', 'blocked')
STATES = ('idle', 'working', 'blocked', 'done', 'error', 'unknown')
LEASE_SECONDS = 10.0  # the observer republishes every 2 s


def source_alive(source):
    """True unless the publishing gateway process is demonstrably gone or replaced."""
    if not isinstance(source, str):
        return True  # no liveness evidence either way
    pid, _, start = source.partition(':')
    if not pid.isdigit() or not start:
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    identity = relay.process_identity(int(pid))
    return identity is None or identity == start  # ps failure is not proof of death


def listen(sock, rec, stop, reporter, regpath, identity):
    """Show the latest authoritative snapshot of the one session this launch is bound to.

    The first valid snapshot binds the launch. A breadcrumb has no writer
    identity, so a later different session (/new, /resume, /branch,
    compression) is never followed: reporting clears until relaunch.
    """
    identity_mode = rec.get('identity') == relay.IDENTITY_MODE
    session = bound_session = source = rival = last_state = None
    shown = None
    last_seq = -1
    confirmed_at = 0.0
    invalidated_at = 0
    next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
    next_correlation_check = time.monotonic() + CORRELATION_CHECK_SECONDS

    def own_peers(peers):
        if not isinstance(peers, list) or not peers or len(peers) > 64:
            return False
        tree = peer.tree_endpoints(os.getpid(), rec.get('start_us'))
        if tree is None:
            return False
        try:
            return all(tuple(p) in tree for p in peers)
        except TypeError:
            return False

    def withdraw():
        nonlocal shown
        if shown is not None:
            reporter.clear()
        shown = None

    def invalidate():
        nonlocal session, invalidated_at
        invalidated_at = time.monotonic_ns()
        withdraw()
        session = None

    while not stop.is_set():
        now = time.monotonic()
        if now >= next_heartbeat:
            relay.touch_registration(regpath, identity)
            next_heartbeat = now + HEARTBEAT_SECONDS
        if session is not None and now >= next_correlation_check:
            next_correlation_check = now + CORRELATION_CHECK_SECONDS
            if not identity_mode and not relay.matching_record(rec, rec['home'], session):
                invalidate()
            elif shown is not None and now - confirmed_at > LEASE_SECONDS:
                withdraw()  # the authority stopped confirming this snapshot
            elif shown in ACTIVE and not source_alive(source):
                withdraw()  # the gateway running this turn is gone; its end never comes
        try:
            data = sock.recv(relay.MAX_MESSAGE + 1)
            if len(data) > relay.MAX_MESSAGE:
                continue
            event = json.loads(data.decode('utf-8'))
            if (not isinstance(event, dict) or event.get('nonce') != rec['nonce']
                    or event.get('type') != 'state' or event.get('state') not in STATES
                    or type(event.get('seq')) is not int or event['seq'] <= invalidated_at):
                continue
            new_session, src = event.get('session'), event.get('src')
            if identity_mode:
                # Gateway-proven attribution, re-verified here: every claimed connection
                # must belong to this launcher's own process tree right now.
                if event.get('mode') != 'peer' or not isinstance(new_session, str):
                    continue
                if event['state'] != 'unknown' and not own_peers(event.get('peers')):
                    continue
            elif event.get('mode') is not None:
                continue
            elif not isinstance(new_session, str) or not relay.matching_record(rec, rec['home'], new_session):
                continue
            if not identity_mode and bound_session is not None and new_session != bound_session:
                if session is not None:
                    invalidate()
                continue
            if rival is not None and source_alive(rival):
                withdraw()  # two live authorities for one session: trust neither
                continue
            rival = None
            if session is not None and src != source and not source_alive(src):
                continue  # delayed datagram from a gateway that is already gone
            if session is not None and src != source and source_alive(source):
                rival = src
                withdraw()
                continue
            if event['seq'] < last_seq:
                continue  # older snapshot: delayed or duplicated datagram
            state = event['state']
            if event['seq'] == last_seq and src == source and state != last_state:
                continue  # one version has one content; never apply a conflicting copy
            if session is None:
                if state == 'unknown':
                    continue
                session = bound_session = new_session
            elif identity_mode:
                session = new_session or session
            last_seq, source, last_state, confirmed_at = event['seq'], src, state, time.monotonic()
            if state == shown or (state == 'unknown' and shown is None):
                continue
            if state == 'unknown':
                withdraw()
            else:
                shown = state
                reporter.report(state)
        except (OSError, ValueError, TypeError, UnicodeError):
            continue


def profile_home():
    """Resolve the default or explicitly pinned home without guessing a sticky profile."""
    suffix = os.environ.get('HERMES_DATA_DIR_SUFFIX', '')
    if os.sep in suffix or (os.altsep and os.altsep in suffix):
        raise ValueError('unsupported Hermes data-directory suffix')
    native = os.path.join(os.path.expanduser('~'), '.hermes' + suffix)
    raw = os.environ.get('HERMES_HOME', '').strip()
    home = os.path.expanduser(os.path.expandvars(raw)) if raw else native
    if not os.path.isabs(home) or not os.path.isabs(native):
        raise ValueError('absolute Hermes home required')
    home, native = os.path.normpath(home), os.path.normpath(native)
    if os.path.basename(os.path.dirname(home)) == 'profiles':
        return home  # Hermes treats a named profile home as pinned.
    native_real, home_real = os.path.realpath(native), os.path.realpath(home)
    root = native if os.path.commonpath((native_real, home_real)) == native_real else home
    active = os.path.join(root, 'active_profile')
    try:
        fd = os.open(active, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, encoding='utf-8-sig') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
                raise ValueError('unsafe sticky Hermes profile file')
            name = stream.read(4096).strip()
    except FileNotFoundError:
        name = ''
    except (OSError, UnicodeError) as exc:
        raise ValueError('cannot safely resolve sticky Hermes profile') from exc
    if name and name != 'default':
        raise ValueError('sticky named Hermes profile requires explicit profile home')
    return home


def run_bridge(target, args):
    home = profile_home()
    tty = os.ttyname(0)
    if os.ttyname(1) != tty:
        raise ValueError('stdin and stdout terminals differ')
    identity_start = None
    if relay.peer_identity_enabled(home):
        # Experimental, opt-in: read-only; never added to or removed from the child's environment.
        mine = peer.identity(os.getpid())
        if mine is None:
            raise ValueError('peer identity requested but kernel process identity is unavailable')
        identity_start = mine[2]
    rec, sock, regpath, sockpath = relay.register(home, tty, os.getcwd(), identity_start)
    ownership = [(path, os.lstat(path)) for path in (regpath, sockpath)]
    stop = threading.Event()
    reporter = Reporter(rec['nonce'])
    # Fork before starting threads. Child inherits the existing controlling tty unchanged.
    try:
        pid = os.fork()
        if pid == 0:
            try:
                sock.close()
                os.execv(target, [target] + args)
            except OSError as exc:
                os.write(2, ('hermes-herdr: cannot execute target: {}\n'.format(exc.strerror)).encode())
                os._exit(127)
        # Once fork succeeds, never fall back to exec: doing so would start a
        # second Hermes process while the first one is already running.
        thread = None
        try:
            reporter.start()
            thread = threading.Thread(target=listen, args=(sock, rec, stop, reporter, regpath, ownership[0][1]),
                                      daemon=True)
            thread.start()
        except Exception as exc:
            stop.set()
            sys.stderr.write('hermes-herdr: reporting disabled: {}\n'.format(exc))
        old = {}
        def forward(signum, frame):
            # SIGINT from the terminal is delivered to both child and parent already.
            if signum != signal.SIGINT:
                try: os.kill(pid, signum)
                except ProcessLookupError: pass
        try:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                old[sig] = signal.signal(sig, forward)
        except (ValueError, OSError):
            pass  # wait for the already-started child even without handlers
        try:
            while True:
                try:
                    _, status = os.waitpid(pid, 0)
                    break
                except InterruptedError:
                    continue
                except OSError:
                    return 1  # child status unavailable; never launch a replacement
        finally:
            for sig, handler in old.items():
                try: signal.signal(sig, handler)
                except (ValueError, OSError): pass
            stop.set()
            if thread is not None and thread.is_alive():
                thread.join(.8)
            try: reporter.close()
            except Exception: pass
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return os.WEXITSTATUS(status)
    finally:
        stop.set()
        sock.close()
        for path, original in ownership:
            relay.unlink_if_same(path, original)


def main():
    # Internal routing metadata must not leak into the actual Hermes process.
    target = os.environ.pop('HERMES_HERDR_REAL', None)
    if not target:
        sys.stderr.write('hermes-herdr: set HERMES_HERDR_REAL to the real launcher\n')
        return 127
    try:
        if os.path.samefile(target, __file__):
            sys.stderr.write('hermes-herdr: recursive target rejected\n')
            return 127
        if ((os.getenv('HC_TERMINAL_ID') or legacy_enabled()) and os.isatty(0) and os.isatty(1)
                and interactive_args(sys.argv[1:])):
            try:
                result = run_bridge(target, sys.argv[1:])
                if result < 0:
                    sig = -result
                    signal.signal(sig, signal.SIG_DFL)
                    os.kill(os.getpid(), sig)
                    return 128 + sig
                return result
            except (OSError, ValueError) as exc:
                sys.stderr.write('hermes-herdr: bridge disabled: {}\n'.format(exc))
        os.execv(target, [target] + sys.argv[1:])
    except OSError as exc:
        sys.stderr.write('hermes-herdr: cannot execute target: {}\n'.format(exc.strerror))
        return 127


if __name__ == '__main__':
    sys.exit(main())
