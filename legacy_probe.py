#!/usr/bin/env python3
"""Opt-in probe of legacy reporting, executed inside a disposable Herdr pane."""
import json
import os
import secrets
import subprocess
import sys
import time

from launcher import Reporter, legacy_enabled


def current_pane():
    result = subprocess.run([os.environ['HERDR_BIN_PATH'], 'pane', 'current', '--current'],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=3, check=True)
    pane = json.loads(result.stdout)['result']['pane']
    if pane.get('pane_id') != os.environ['HERDR_PANE_ID']:
        raise ValueError('Herdr returned a different pane; refusing to report')
    return pane


def wait_for(predicate, label):
    deadline = time.monotonic() + 5
    last = None
    while time.monotonic() < deadline:
        last = current_pane()
        if predicate(last):
            print(label, flush=True)
            return
        time.sleep(.1)
    raise RuntimeError('{} not observed (last pane: {})'.format(label, last))


def main():
    if not legacy_enabled() or not os.isatty(0) or not os.isatty(1):
        print('Run this probe interactively in a Herdr-managed disposable pane.', file=sys.stderr)
        return 2
    try:
        initial = current_pane()
        if initial.get('agent'):
            raise ValueError('pane already has an agent; use an empty disposable pane')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print('Herdr preflight failed: {}'.format(exc), file=sys.stderr)
        return 2
    reporter = Reporter(secrets.token_hex(8))
    reporter.desktop = False  # Never report synthetic states to the desktop backend.
    print('Synthetic legacy probe; source={}'.format(reporter.source), flush=True)
    error = None
    try:
        reporter.start()
        for event, expected in (('working', 'working'), ('blocked', 'blocked'), ('done', 'idle')):
            reporter.report(event)
            wait_for(lambda p, s=expected: p.get('agent') == 'hermes' and p.get('agent_status') == s,
                     expected)
    except (Exception, KeyboardInterrupt) as exc:
        error = exc
    finally:
        reporter.close()
    try:
        if reporter.used:
            wait_for(lambda p: not p.get('agent'), 'released')
    except (Exception, KeyboardInterrupt) as exc:
        print('Release was not verified. Source: {}'.format(reporter.source), file=sys.stderr)
        error = error or exc
    if error:
        print('Herdr probe failed: {}'.format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
