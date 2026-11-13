"""Private, best-effort local transport. No prompt or answer payloads."""
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import time

MAX_RECORDS = 128
MAX_MESSAGE = 2048
RECORD_FORMAT = 'hermes-herdr-v1'
MAX_PRUNE_ENTRIES = 512


def no_symlink_path(path):
    if not os.path.isabs(path):
        raise ValueError('path must be absolute')
    current = '/'
    for part in path.split('/'):
        if not part:
            continue
        if part in ('.', '..'):
            raise ValueError('non-canonical path')
        current = os.path.join(current, part)
        if stat.S_ISLNK(os.lstat(current).st_mode):
            raise ValueError('symlink path rejected')
    return current


def runtime_dir(home=None, create=False):
    path = os.environ.get('HERMES_HERDR_RUNTIME', '')
    if not path:
        if not home or not os.path.isabs(home):
            raise ValueError('an absolute Hermes home is required')
        parent = no_symlink_path(os.path.join(home, 'cache', 'scratch'))
        parent_st = os.lstat(parent)
        if (not stat.S_ISDIR(parent_st.st_mode) or parent_st.st_uid != os.getuid()
                or parent_st.st_mode & 0o022):
            raise ValueError('Hermes scratch directory is not private')
        # Keep the path short enough for macOS's 104-byte sockaddr_un limit.
        path = os.path.join(parent, 'hb')
        if create:
            try:
                os.mkdir(path, 0o700)
            except FileExistsError:
                pass
    if not os.path.isabs(path):
        raise ValueError('HERMES_HERDR_RUNTIME must name a private absolute directory')
    path = no_symlink_path(path)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise ValueError('runtime directory must be owned by this user and mode 0700')
    return path


def process_identity(pid):
    # lstart identifies PID incarnation, not just a possibly recycled PID.
    try:
        result = subprocess.run(['/bin/ps', '-o', 'lstart=', '-p', str(pid)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=.15, check=True)
        value = result.stdout.decode('ascii').strip()
        return value if value else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def read_json(path, limit=4096):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or st.st_mode & 0o022 or st.st_size > limit):
            raise ValueError('unsafe record')
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise ValueError('oversized record')
        parsed = json.loads(data.decode('utf-8-sig'))
        if not isinstance(parsed, dict):
            raise ValueError('record must be a JSON object')
        return parsed
    finally:
        os.close(fd)


def peer_identity_enabled(home):
    """Use an explicit environment override or a private per-profile opt-in."""
    override = os.environ.get('HERMES_HERDR_PEER_IDENTITY')
    if override is not None:
        return override == '1'
    try:
        path = no_symlink_path(os.path.join(home, 'plugins', 'hermes_herdr_bridge', 'peer-identity.json'))
        return read_json(path, limit=1024).get('enabled') is True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def crumb_path(home, tty):
    name = re.sub(r'[^A-Za-z0-9._-]', '-', tty.strip().strip('/'))[:120]
    return os.path.join(home, 'terminal-sessions', 'tty-' + name)


def record_session(rec, home):
    """The session shown by this live registration's own terminal, or None."""
    try:
        if rec['home'] != no_symlink_path(home) or not isinstance(rec['pid'], int):
            return None
        if rec['start'] != process_identity(rec['pid']) or not rec['start']:
            return None
        tty_st = os.stat(rec['tty'], follow_symlinks=False)
        if not stat.S_ISCHR(tty_st.st_mode) or tty_st.st_rdev != rec['ttydev']:
            return None
        directory = os.path.join(home, 'terminal-sessions')
        directory_stat = os.lstat(directory)
        if (not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != os.getuid()
                or directory_stat.st_mode & 0o022):
            return None
        crumb = read_json(crumb_path(home, rec['tty']))
        ts, session = crumb.get('ts'), crumb.get('session_id')
        if (isinstance(session, str) and crumb.get('cwd') == rec['cwd']
                and isinstance(ts, (int, float)) and not isinstance(ts, bool)
                and rec['created'] <= ts <= time.time() + 2):
            return session
        return None
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def matching_record(rec, home, session):
    return record_session(rec, home) == session


def socket_path(root, nonce):
    # The registration retains the full nonce; a shorter socket leaf avoids
    # macOS path-length failures. bind() collisions fail without deleting peers.
    return os.path.join(root, nonce[:24] + '.s')


def publish(home, event, budget=.25):
    """Publish one session snapshot to each matching live registration."""
    return publish_many(home, {event['session']: event}, budget)


def publish_many(home, events, budget=.25):
    """Send each registration the snapshot of the session its own terminal shows.

    One registry scan (one ps per registration) serves every session. Each
    viewer of a session receives that session's shared state.
    """
    try:
        root = runtime_dir(home)
        deadline = time.monotonic() + budget
        names = []
        with os.scandir(root) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    return False
                if re.fullmatch(r'[0-9a-f]{32}\.json', entry.name):
                    names.append(entry.name)
                    if len(names) > MAX_RECORDS:
                        return False
        matches = []
        for name in names:
            if time.monotonic() >= deadline:
                return False
            try:
                rec = read_json(os.path.join(root, name))
                if rec.get('nonce') != name[:-5]:
                    continue
                if 'identity' in rec:
                    continue  # identity registrations never use breadcrumb attribution
                session = record_session(rec, home)
                if session in events:
                    matches.append((rec, events[session]))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
        if not matches:
            return False
        delivered = False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            for rec, event in matches:
                if time.monotonic() >= deadline:
                    break
                data = json.dumps(dict(event, nonce=rec['nonce']), separators=(',', ':')).encode()
                if len(data) > MAX_MESSAGE:
                    continue
                try:
                    sock.sendto(data, socket_path(root, rec['nonce']))
                    delivered = True
                except OSError:
                    continue  # one stale or busy viewer must not block the others
        finally:
            sock.close()
        return delivered
    except (OSError, ValueError, KeyError, TypeError):
        return False


def identity_records(home, budget=.25):
    """Live identity-mode registrations of this home (bounded scan), or None."""
    try:
        root = runtime_dir(home)
        home = no_symlink_path(home)
        deadline = time.monotonic() + budget
        out = []
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index > MAX_PRUNE_ENTRIES or time.monotonic() >= deadline:
                    return None
                if not re.fullmatch(r'[0-9a-f]{32}\.json', entry.name):
                    continue
                try:
                    rec = read_json(entry.path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if (rec.get('identity') == IDENTITY_MODE and rec.get('nonce') == entry.name[:-5]
                        and rec.get('home') == home and type(rec.get('pid')) is int
                        and type(rec.get('start_us')) is int and isinstance(rec.get('start'), str)):
                    out.append(rec)
                    if len(out) > MAX_RECORDS:
                        return None
        return out
    except (OSError, ValueError):
        return None


def send_to(home, rec, event):
    """Send one event to one registration's socket; True if handed to the kernel."""
    try:
        root = runtime_dir(home)
        data = json.dumps(dict(event, nonce=rec['nonce']), separators=(',', ':')).encode()
        if len(data) > MAX_MESSAGE:
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.sendto(data, socket_path(root, rec['nonce']))
            return True
        finally:
            sock.close()
    except (OSError, ValueError, KeyError, TypeError):
        return False


def touch_registration(path, original):
    """Refresh a live registration without touching a replacement or symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        try:
            current = os.fstat(fd)
            if (not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
                    or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)):
                return False
            os.utime(fd, None)
            return True
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return False


def unlink_if_same(path, original):
    try:
        current = os.lstat(path)
        if (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino):
            os.unlink(path)
    except OSError:
        pass


def prune_stale(root, home):
    """Boundedly remove only our own records whose process is definitely gone.

    Socket paths are intentionally left alone: a new live record may share a
    truncated socket name, and record ownership alone does not prove its owner.
    """
    removed = 0
    try:
        root = no_symlink_path(root)
        st = os.lstat(root)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            return 0
        home = no_symlink_path(home)
        deadline = time.monotonic() + .25
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_PRUNE_ENTRIES or time.monotonic() >= deadline:
                    break
                if not re.fullmatch(r'[0-9a-f]{32}\.json', entry.name):
                    continue
                try:
                    before = os.lstat(entry.path)
                    record = read_json(entry.path)
                    pid = record.get('pid')
                    if (record.get('format') != RECORD_FORMAT or record.get('home') != home
                            or record.get('nonce') != entry.name[:-5]
                            or type(pid) is not int or pid <= 1
                            or not isinstance(record.get('start'), str) or not record['start']):
                        continue
                    try:
                        os.kill(pid, 0)
                        continue  # live or PID reused: never delete
                    except ProcessLookupError:
                        pass
                    except (OSError, OverflowError):
                        continue  # permission or invalid PID does not establish death
                    after = os.lstat(entry.path)
                    if (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino):
                        os.unlink(entry.path)
                        removed += 1
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
    except (OSError, ValueError):
        pass
    return removed


IDENTITY_MODE = 'peer-v1'


def register(home, tty, cwd, identity_start_us=None):
    """identity_start_us: opt into gateway peer identity (experimental); the
    launcher's microsecond start time pins its process tree against PID reuse."""
    root = runtime_dir(home, create=True)
    home = no_symlink_path(home)
    prune_stale(root, home)
    nonce = secrets.token_hex(16)
    sockpath = socket_path(root, nonce)
    if len(os.fsencode(sockpath)) >= 104:
        raise ValueError('runtime path too long for macOS Unix socket')
    start = process_identity(os.getpid())
    if not start:
        raise ValueError('cannot verify launcher process identity')
    rec = dict(format=RECORD_FORMAT, nonce=nonce, pid=os.getpid(), start=start, tty=tty,
               ttydev=os.stat(tty, follow_symlinks=False).st_rdev,
               home=home, cwd=cwd, created=time.time())
    if identity_start_us is not None:
        rec.update(identity=IDENTITY_MODE, start_us=identity_start_us)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    path = os.path.join(root, nonce + '.json')
    owned = []
    try:
        sock.bind(sockpath)
        owned.append((sockpath, os.lstat(sockpath)))
        os.chmod(sockpath, 0o600)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            owned.append((path, os.fstat(fd)))
            os.write(fd, json.dumps(rec, separators=(',', ':')).encode())
        finally:
            os.close(fd)
        sock.settimeout(.2)
        return rec, sock, path, sockpath
    except Exception:
        sock.close()
        # Never unlink a pre-existing socket or registration on collision.
        for owned_path, identity in owned:
            unlink_if_same(owned_path, identity)
        raise
