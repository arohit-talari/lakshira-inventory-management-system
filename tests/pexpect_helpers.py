"""
Tier 2 shared harness -- drives the real interactive inventory.py CLI
through a real pty via pexpect, the same way a human at a keyboard would.

Unlike Tier 1 (pure functions, no I/O), these tests exercise the actual
questionary arrow-key menus and hit the real TEST Google Sheet -- they are
slower (seconds, not milliseconds) and depend on live sheet state, so they
belong in their own slower test tier, not mixed into test_*_helpers.py.

Requires inventory.py's MODE to be "test" -- every test here asserts that
before spawning, and refuses to run otherwise, since these scripts type
real answers into real prompts and confirm real writes.
"""
import re
import time

import gspread
import pexpect
import pytest

import inventory as inv

DOWN = "\x1b[B"
UP = "\x1b[A"
ENTER = "\r"

DEFAULT_TIMEOUT = 35  # generous: covers real ECB-rate and Sheets API round-trips,
                       # with headroom for occasional slow responses during a long
                       # sequential run of many live-API-dependent tests

_CONNECT_ERROR = "Could not connect to Google Sheets"
_SPAWN_RETRIES = 4
_SPAWN_RETRY_DELAY = 15  # seconds -- Google's Sheets/OAuth quota window is ~100s;
                          # a full retry budget of ~45s plus natural time spent
                          # asserting between tests is generally enough to clear it


def require_test_mode():
    if inv.MODE != "test":
        pytest.skip("inventory.MODE is not 'test' -- refusing to run interactive "
                     "scripts against a non-test sheet.")


def spawn_app(timeout=DEFAULT_TIMEOUT):
    """Spawn python3 inventory.py in a real pty.

    Running many of these back-to-back (one full process + fresh OAuth
    handshake per test) reliably trips Google's Sheets/OAuth rate limit
    partway through a full test-file run -- reproduced consistently while
    building this suite, not a one-off flake. Retries the spawn itself
    (not individual API calls inside the app, which is out of scope here)
    since the failure is visible immediately as the app's own connection
    error message, printed before the main menu ever appears.
    """
    require_test_mode()
    last_output = ""
    for attempt in range(1, _SPAWN_RETRIES + 1):
        child = pexpect.spawn("python3 inventory.py", timeout=timeout,
                               encoding="utf-8", dimensions=(60, 160))
        try:
            index = child.expect([_CONNECT_ERROR, "Select an option \\(1-11\\):"], timeout=timeout)
        except pexpect.exceptions.EOF:
            index = 0  # crashed before printing the menu -- treat as the connect error
        if index == 1:
            return child
        last_output = strip_ansi(child.before or "")
        close(child)
        if attempt < _SPAWN_RETRIES:
            time.sleep(_SPAWN_RETRY_DELAY)
    pytest.fail(f"inventory.py failed to connect after {_SPAWN_RETRIES} attempts "
                f"(Google Sheets/OAuth rate limit likely still in effect): {last_output!r}")


def strip_ansi(text):
    """Strip ANSI escape codes so captured output can be asserted on by
    plain substring/regex matching without embedding color codes in every
    test."""
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)


def expect_clean(child, pattern, timeout=None):
    """expect() then return the ANSI-stripped text consumed before the
    match, so assertions read against plain text."""
    kwargs = {} if timeout is None else {"timeout": timeout}
    child.expect(pattern, **kwargs)
    return strip_ansi(child.before + (child.after if isinstance(child.after, str) else ""))


def close(child):
    if child.isalive():
        child.close(force=True)


_QUOTA_RETRIES = 4
_QUOTA_RETRY_DELAY = 20  # seconds -- Google's read-quota window is per-minute


def retry_on_quota(fn, *args, **kwargs):
    """Retry a direct gspread call (fixture setup reading/writing the sheet
    outside the spawned app, e.g. tests/fixtures_data.py) on a 429 read/
    write-quota error. Distinct from spawn_app()'s own retry: that one
    covers the OAuth-connection rate limit hit when spawning many fresh
    `python3 inventory.py` processes back-to-back; this covers the separate
    per-minute Sheets API read/write quota, which fixture setup can hit on
    its own even between spawns since each fixture does several direct
    reads (idempotency check, SKU generation) outside any spawned process."""
    last_error = None
    for attempt in range(1, _QUOTA_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            last_error = e
            if "Quota exceeded" not in str(e) or attempt == _QUOTA_RETRIES:
                raise
            time.sleep(_QUOTA_RETRY_DELAY)
    raise last_error
