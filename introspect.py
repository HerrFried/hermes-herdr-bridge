"""Experimental read-only introspection of the running gateway's per-connection
subscriptions (private Hermes seams; see PEER-IDENTITY-EVALUATION.md).

Never writes to, patches or schedules anything on gateway objects. Every
function returns None when the expected shape is missing (fail closed).
Only nonsecret metadata is read: session IDs and TCP endpoints.
"""
import collections
import ipaddress
import os
import sys

MAX_CONNECTIONS = 4096
MAX_LOGICAL_CACHE = 4096
_logical = collections.OrderedDict()  # (profile, session) -> logical owner


def _ip(value):
    """Normalized textual IP (IPv4-mapped IPv6 -> IPv4), or None."""
    try:
        addr = ipaddress.ip_address(str(value).split('%', 1)[0])
    except ValueError:
        return None
    mapped = getattr(addr, 'ipv4_mapped', None)
    return str(mapped or addr)


def _profile(value):
    try:
        return os.path.realpath(os.fspath(value))
    except TypeError:
        return None


def runner():
    """The live gateway runner, only inside the gateway process that serves the API."""
    module = sys.modules.get('gateway.run_api')
    active = getattr(module, '_ACTIVE_RUNNER', None)
    if active is None:
        return None
    gil = getattr(sys, '_is_gil_enabled', None)
    if callable(gil) and not gil():
        return None  # copies of loop-owned dicts are only safe under the GIL
    return active


def listener(active):
    """(ip, port) of the gateway API listener socket, or None."""
    try:
        sock = active.session_api.socket
        name = sock.getsockname()
        ip, port = _ip(name[0]), name[1]
        return (ip, port) if ip and type(port) is int and port > 0 else None
    except (AttributeError, OSError, TypeError, IndexError):
        return None


def authorities(active):
    found = []
    registry = getattr(active, 'session_authorities', None)
    try:
        candidates = list(registry or ()) + [getattr(active, 'session_authority', None)]
    except TypeError:
        return None
    for authority in candidates:
        if authority is not None and authority not in found:
            found.append(authority)
    return found


def _copy(mapping):
    for _ in range(3):
        try:
            return list(mapping.items())
        except RuntimeError:  # resized by the loop thread while copying: retry
            continue
    raise RuntimeError('unstable mapping')


def subscriptions():
    """{(local_ip, local_port, remote_ip, remote_port) of the *client* socket:
    frozenset((profile_home, session_id))}, or None when anything is unexpected.

    The 4-tuple is what the viewer process's own kernel socket reports, so it can
    be matched exactly against libproc output for that process.
    """
    active = runner()
    if active is None:
        return None
    server = listener(active)
    auths = authorities(active)
    if server is None or not auths:
        return None
    out = {}
    try:
        for authority in auths:
            profile = _profile(authority.profile_id)
            events = authority.events
            if profile is None:
                return None
            for session_id, live in _copy(authority.sessions):
                if not isinstance(session_id, str):
                    return None
                for _sub, actor in _copy(live.subscribers):
                    transport = events.get(actor.transport_id)
                    if transport is None:
                        continue  # torn down: not a live viewer
                    scope = transport._ws.scope
                    # The gateway records the socket peer before Uvicorn's proxy middleware
                    # may rewrite scope['client'] (gateway/run_api.py, GatewayRuntimeAPI._serve).
                    client, served = scope.get('hermes.gateway_socket_peer'), scope.get('server')
                    if not client or not served:
                        return None
                    cip, cport = _ip(client[0]), client[1]
                    sip, sport = _ip(served[0]), served[1]
                    if (not cip or type(cport) is not int or cport <= 0
                            or (sip, sport) != server):
                        return None  # rewritten (proxy headers) or not our listener
                    key = (cip, cport, sip, sport)
                    out.setdefault(key, set()).add((profile, session_id))
                    if len(out) > MAX_CONNECTIONS:
                        return None
    except (AttributeError, KeyError, TypeError, IndexError, RuntimeError):
        return None
    return {key: frozenset(value) for key, value in out.items()}


def logical_owner(profile, session_id):
    """Root of the session's compression lineage per the owning authority, or None."""
    key = (profile, session_id)
    if key in _logical:
        _logical.move_to_end(key)
        return _logical[key]
    active = runner()
    if active is None:
        return None
    try:
        for authority in authorities(active) or ():
            if _profile(authority.profile_id) == profile:
                owner = authority.logical_owner(session_id)
                if not isinstance(owner, str) or not owner:
                    return None
                _logical[key] = owner
                while len(_logical) > MAX_LOGICAL_CACHE:
                    _logical.popitem(last=False)
                return owner
    except Exception:
        return None
    return None
