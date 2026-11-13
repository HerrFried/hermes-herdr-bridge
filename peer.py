"""Experimental, macOS-only kernel-attested TCP endpoint ownership (read-only libproc).

Maps "which process owns the client end of this loopback TCP connection" without
root, injection or environment markers: the kernel reports each socket's
4-tuple for processes of the same user. Nothing here reads payloads.

Every entry point fails closed (returns None) when libproc is unavailable or a
startup self-test shows the struct layout this module assumes is wrong.
"""
import ctypes
import ctypes.util
import ipaddress
import os
import socket
import struct
import sys

PROC_PIDLISTFDS = 1
PROC_PIDFDSOCKETINFO = 3
PROX_FDTYPE_SOCKET = 2
PROC_PIDTBSDINFO = 3
MAX_FDS = 4096
MAX_DESCENDANTS = 256
SOCKET_FDINFO_SIZE = 792  # sizeof(struct socket_fdinfo) on macOS (arm64 and x86_64)
# Offsets inside struct socket_fdinfo: proc_fileinfo (24) + socket_info.
_SOI = 24
_SOI_KIND = _SOI + 136 + 8 + 8 + 4 + 4 + 4 + 2 * 8 + 4 + 24 + 24  # after vinfo_stat .. soi_snd
_SOI_PROTO = _SOI_KIND + 8  # soi_kind, rfu_1
SOCKINFO_TCP = 2
_lib = None
_verified = None


def _libproc():
    global _lib
    if _lib is None:
        if sys.platform != 'darwin':
            return None
        path = ctypes.util.find_library('proc') or '/usr/lib/libproc.dylib'
        try:
            lib = ctypes.CDLL(path, use_errno=True)
        except OSError:
            return None
        lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        lib.proc_pidfdinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        lib.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        _lib = lib
    return _lib


def _addr(raw, family):
    # in4in6_addr: 12 bytes padding then the IPv4 address; in6_addr: 16 bytes.
    if family == socket.AF_INET:
        return socket.inet_ntop(socket.AF_INET, raw[12:16])
    addr = ipaddress.ip_address(raw[:16])
    return str(addr.ipv4_mapped or addr)  # same normalization as introspect._ip


FD_ENTRY = 8  # struct proc_fdinfo: int32 fd, uint32 type
FD_SLACK = 64  # extra entries so an fd opened between size query and read is still seen


def _listfds(pid):
    """Complete raw proc_fdinfo array for pid, or None.

    PROC_PIDLISTFDS silently truncates to the buffer, so the result is accepted
    only when it is strictly smaller than the buffer (nothing could be cut off).
    """
    lib = _libproc()
    if lib is None:
        return None
    need = lib.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, None, 0)
    if need <= 0 or need % FD_ENTRY or need // FD_ENTRY > MAX_FDS:
        return None
    buf = ctypes.create_string_buffer(need + FD_SLACK * FD_ENTRY)
    n = lib.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, buf, len(buf))
    if n <= 0 or n % FD_ENTRY or n >= len(buf):
        return None
    return buf.raw[:n]


def _fdinfo(pid, fd):
    """Full struct socket_fdinfo bytes, or None (closed, raced, or unreadable)."""
    lib = _libproc()
    if lib is None:
        return None
    info = ctypes.create_string_buffer(SOCKET_FDINFO_SIZE)
    if lib.proc_pidfdinfo(pid, fd, PROC_PIDFDSOCKETINFO, info, SOCKET_FDINFO_SIZE) != SOCKET_FDINFO_SIZE:
        return None
    return info.raw


def _tcp_endpoints_unchecked(pid):
    """Every TCP 4-tuple pid owns, or None unless every socket fd was inspected."""
    raw_fds = _listfds(pid)
    if raw_fds is None:
        return None
    out = set()
    for i in range(len(raw_fds) // FD_ENTRY):
        fd, fdtype = struct.unpack_from('iI', raw_fds, i * FD_ENTRY)
        if fdtype != PROX_FDTYPE_SOCKET:
            continue
        raw = _fdinfo(pid, fd)
        if raw is None:
            return None  # an uninspected socket could be a hidden gateway connection
        family = struct.unpack_from('i', raw, _SOI + 136 + 8 + 8 + 4 + 4)[0]
        kind = struct.unpack_from('i', raw, _SOI_KIND)[0]
        if kind != SOCKINFO_TCP or family not in (socket.AF_INET, socket.AF_INET6):
            continue
        fport, lport = struct.unpack_from('ii', raw, _SOI_PROTO)
        # in_sockinfo: fport, lport, gencnt(8), flags(4), flow(4), vflag(1), ttl(1), pad(2), rfu(4)
        faddr = raw[_SOI_PROTO + 32:_SOI_PROTO + 48]
        laddr = raw[_SOI_PROTO + 48:_SOI_PROTO + 64]
        out.add((_addr(laddr, family), socket.ntohs(lport & 0xffff),
                 _addr(faddr, family), socket.ntohs(fport & 0xffff)))
    return out


def self_test():
    """Verify the assumed struct layout against a connection this process just made."""
    global _verified
    if _verified is not None:
        return _verified
    _verified = False
    try:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(('127.0.0.1', 0)); server.listen(1)
        client = socket.create_connection(server.getsockname(), timeout=2)
        accepted, _ = server.accept()
        try:
            want = client.getsockname() + client.getpeername()
            found = _tcp_endpoints_unchecked(os.getpid()) or set()
            mine = _bsdinfo_unchecked(os.getpid())
            _verified = (tuple(want) in found
                         and tuple(accepted.getsockname() + accepted.getpeername()) in found
                         and mine is not None and mine[0] == os.getppid() and mine[1] == os.getuid()
                         and mine == _bsdinfo_unchecked(os.getpid()) and mine[2] > 0)
        finally:
            accepted.close(); client.close(); server.close()
    except OSError:
        _verified = False
    return _verified


def tcp_endpoints(pid):
    """{(local_ip, local_port, remote_ip, remote_port)} for pid's TCP sockets, or None."""
    if not self_test():
        return None
    return _tcp_endpoints_unchecked(pid)


BSDINFO_SIZE = 136  # sizeof(struct proc_bsdinfo)


def _bsdinfo_unchecked(pid):
    lib = _libproc()
    if lib is None:
        return None
    buf = ctypes.create_string_buffer(BSDINFO_SIZE)
    if lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buf, BSDINFO_SIZE) != BSDINFO_SIZE:
        return None
    pid_, ppid, uid = struct.unpack_from('III', buf.raw, 12)
    sec, usec = struct.unpack_from('QQ', buf.raw, 120)
    if pid_ != pid:
        return None
    return ppid, uid, sec * 1000000 + usec


def identity(pid):
    """(ppid, uid, start_us) for a live process, or None. Microsecond start time
    distinguishes PID reuse, unlike `ps -o lstart`."""
    if not self_test():
        return None
    return _bsdinfo_unchecked(pid)


def children(pid):
    """Direct child pids, or None (error or possibly truncated list)."""
    lib = _libproc()
    if lib is None:
        return None
    buf = (ctypes.c_int * (MAX_DESCENDANTS + 1))()
    n = lib.proc_listchildpids(pid, buf, ctypes.sizeof(buf))
    if n < 0 or n > MAX_DESCENDANTS:
        return None  # a full buffer cannot prove the list is complete
    return [buf[i] for i in range(n) if buf[i] > 0]


def descendants(pid):
    """pid and its descendants (bounded), or None if the tree cannot be read."""
    seen, stack = [], [pid]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.append(current)
        if len(seen) > MAX_DESCENDANTS:
            return None
        kids = children(current)
        if kids is None:
            return None
        stack.extend(kids)
    return seen


SCAN_ATTEMPTS = 3


def _scan_tree(root, first):
    """One complete scan, or None if anything was unreadable or changed meanwhile."""
    tree = descendants(root)
    if tree is None:
        return None
    ident, out = {}, {}
    for pid in tree:
        before = _bsdinfo_unchecked(pid)
        if before is None:
            return None  # an unreadable identity does not prove the process exited
        if before[1] != os.getuid() or (pid != root and before[0] not in ident):
            return None
        ident[pid] = before
        endpoints = _tcp_endpoints_unchecked(pid)
        if endpoints is None or _bsdinfo_unchecked(pid) != before:
            return None  # incomplete, or exited/replaced while being read
        for endpoint in endpoints:
            if endpoint in out and out[endpoint] != pid:
                return None  # one connection claimed twice: inconsistent read
            out[endpoint] = pid
    # A process forked during the scan may have opened a connection we never read.
    after = descendants(root)
    if after is None or _bsdinfo_unchecked(root) != first:
        return None
    for pid in after:
        now = _bsdinfo_unchecked(pid)
        if now is None or ident.get(pid) != now:
            return None  # unreadable, new, or reused since its sockets were read
    return out


def tree_endpoints(root, root_start_us=None):
    """{4-tuple: owner pid} for TCP sockets owned by root and its descendants.

    Returns None (fail closed) unless one scan read the whole tree completely:
    every live process identified by (ppid, uid, microsecond start time) before
    and after its sockets are read, every socket fd inspected, no fd list or
    child list possibly truncated, and no new live descendant appearing during
    the scan. A failed scan is retried from scratch a bounded number of times
    (transient churn); a partial result is never used.
    """
    if not self_test():
        return None
    first = _bsdinfo_unchecked(root)
    if first is None or first[1] != os.getuid() or (root_start_us is not None and first[2] != root_start_us):
        return None
    for _ in range(SCAN_ATTEMPTS):
        result = _scan_tree(root, first)
        if result is not None:
            return result
    return None
