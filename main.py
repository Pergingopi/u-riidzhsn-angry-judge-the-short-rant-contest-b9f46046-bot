#!/usr/bin/env python3
"""Angry Judge: The Short Rant Contest — write one short rant to entry.txt.

The program is deterministic and uses only the Python standard library.
It writes the rant with an atomic temp-file-plus-``os.replace`` dance in
the output directory, fsyncs the file and its directory, and then reads
the result back to prove the bytes on disk are exactly the bytes intended.

The entry itself is a single fixed string: a rant about a smoke detector
that only ever chirps at 3 a.m. No trailing newline is written, because
the gate counts characters 1..400 and a stray newline would burn one.

Layout of the module, top to bottom:

* :func:`resolve_output_dir` — where to write, with a safe fallback.
* :func:`validate_entry` — every gate rule, checked before any I/O.
* :func:`cleanup_stale_temps` — best-effort removal of old temp files.
* :func:`write_atomic` — the temp-file write, fsync and rename.
* :func:`verify_output` — read-back check plus the 0644 mode check.
* :func:`main` — glue: resolve, validate, write, verify, report.
"""

import glob
import os
import sys
import tempfile
import time

ENTRY = (
    "The smoke detector only dies at 3 a.m. Never at noon, when I'm awake "
    "and holding a screwdriver. No \u2014 it waits until I'm asleep, then "
    "chirps once, politely, like a tiny coward: loud enough to ruin my "
    "sleep, too quiet to find. So at 3 a.m. I'm standing on a chair in the "
    "dark, waving a broom at the ceiling, negotiating with a twelve-dollar "
    "plastic disc about my right to sleep. It won."
)

#: Name of the file the judge reads.
OUTPUT_FILENAME = "entry.txt"

#: Fallback output directory when MAKEFUN_OUTPUT_DIR is unset or blank.
DEFAULT_OUTPUT_DIR = "/output"

#: Gate from the rules file: the entry must be 1..400 characters.
MIN_CHARS = 1
MAX_CHARS = 400

#: Required permission bits on the finished entry.txt.
OUTPUT_MODE = 0o644

#: Prefix/suffix used for our temp files, so stale ones are recognisable.
TMP_PREFIX = ".entry-"
TMP_SUFFIX = ".tmp"

#: A temp file older than this is stale and safe to remove (seconds).
STALE_TMP_MAX_AGE_SECONDS = 3600

#: How many times to retry removing a temp file before giving up.
UNLINK_RETRIES = 3

#: Pause between unlink retries (seconds).
UNLINK_RETRY_DELAY_SECONDS = 0.05


def resolve_output_dir():
    """Return the directory entry.txt goes into.

    ``MAKEFUN_OUTPUT_DIR`` wins when it names a real directory path. An
    unset variable, an empty string, or a value that is only whitespace
    falls back to :data:`DEFAULT_OUTPUT_DIR` instead of failing the run
    over a typo in the environment. A path that exists but is not a
    directory is a hard error: creating files under a regular file can
    never succeed, so fail loudly now rather than mid-write.
    """
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR")
    out_dir = raw.strip() if isinstance(raw, str) else ""
    if not out_dir:
        out_dir = DEFAULT_OUTPUT_DIR
    if os.path.exists(out_dir) and not os.path.isdir(out_dir):
        raise ValueError("output path %r exists and is not a directory" % out_dir)
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.isdir(out_dir):
        raise ValueError("output directory %r could not be created" % out_dir)
    return out_dir


def validate_entry(text):
    """Check *text* against every rule in the gate; raise on the first miss.

    Raises :class:`ValueError` with a message naming the broken rule, so
    the caller can report it instead of writing an entry the scorer
    would reject with a bare ``Valid: 0``. The checks, in order:

    1. The entry must be a string.
    2. It must not be empty or whitespace-only.
    3. Its length must be within ``MIN_CHARS``..``MAX_CHARS``.
    4. It must contain no control characters (anything below U+0020
       except newline/tab, plus U+007F DEL).
    5. It must not end with a newline: a trailing newline is a wasted
       counted character and the entry is defined without one.
    """
    if not isinstance(text, str):
        raise TypeError("entry text must be str, got %s" % type(text).__name__)
    if not text or text.isspace():
        raise ValueError("entry text is empty or only whitespace")
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        raise ValueError(
            "entry text must be %d..%d characters, got %d"
            % (MIN_CHARS, MAX_CHARS, len(text))
        )
    for ch in text:
        code = ord(ch)
        if code < 0x20 and ch not in ("\n", "\t"):
            raise ValueError(
                "entry text contains a control character: U+%04X" % code
            )
        if code == 0x7F:
            raise ValueError("entry text contains a control character: U+007F")
    if text.endswith("\n"):
        raise ValueError("entry text must not end with a newline")
    return text


def _unlink_with_retry(path):
    """Remove *path*, retrying briefly; return True if it is gone."""
    for attempt in range(UNLINK_RETRIES):
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if attempt + 1 >= UNLINK_RETRIES:
                return False
            time.sleep(UNLINK_RETRY_DELAY_SECONDS)
    return False


def cleanup_stale_temps(out_dir):
    """Remove our own temp files that a crashed run left behind.

    Only files matching our prefix/suffix and older than
    :data:`STALE_TMP_MAX_AGE_SECONDS` are touched, so a temp file from a
    run that is still in flight is never deleted under it. Returns the
    number of files removed. Best effort by design: a failure here must
    not fail the entry, so callers may ignore a False return.
    """
    removed = 0
    cutoff = time.time() - STALE_TMP_MAX_AGE_SECONDS
    pattern = os.path.join(out_dir, TMP_PREFIX + "*" + TMP_SUFFIX)
    for candidate in glob.glob(pattern):
        try:
            if os.path.getmtime(candidate) >= cutoff:
                continue
        except OSError:
            continue
        if _unlink_with_retry(candidate):
            removed += 1
    return removed


def _fsync_dir(path):
    """Flush directory *path* so a rename inside it is durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(out_dir, data):
    """Write *data* to ``entry.txt`` in *out_dir* atomically.

    A temp file is created with ``O_EXCL`` semantics (via
    :func:`tempfile.mkstemp`), written, fsynced, renamed over the target
    with :func:`os.replace`, and the directory is fsynced. On any failure
    the temp file is removed with a retry and the original error is
    re-raised, so a half-written entry can never masquerade as a result.
    Returns the full path of the finished file.
    """
    target = os.path.join(out_dir, OUTPUT_FILENAME)
    fd, tmp_path = tempfile.mkstemp(
        prefix=TMP_PREFIX, suffix=TMP_SUFFIX, dir=out_dir
    )
    try:
        os.chmod(tmp_path, OUTPUT_MODE)
        with os.fdopen(fd, "wb") as f:
            fd = None  # Ownership moved to the file object; do not close twice.
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, OUTPUT_MODE)
        os.replace(tmp_path, target)
        os.chmod(target, OUTPUT_MODE)
        _fsync_dir(out_dir)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _unlink_with_retry(tmp_path)
        raise
    return target


def verify_output(target, data):
    """Prove *target* holds exactly *data* with mode 0644.

    Raises :class:`RuntimeError` when the read-back bytes differ, and
    :class:`ValueError` when the permission bits are wrong. This is the
    last line of defence before the program reports success.
    """
    with open(target, "rb") as f:
        back = f.read()
    if back != data:
        raise RuntimeError("read-back verification failed for %r" % target)
    st = os.stat(target)
    if st.st_mode & 0o777 != OUTPUT_MODE:
        raise ValueError(
            "entry.txt mode is %o, expected %o"
            % (st.st_mode & 0o777, OUTPUT_MODE)
        )


def main(argv=None):
    """Write the rant and report success; return 0, or 1 on any failure."""
    del argv  # No command-line arguments are defined.
    try:
        out_dir = resolve_output_dir()
        try:
            cleanup_stale_temps(out_dir)
        except OSError as exc:
            # Stale-temp cleanup is best effort; never fail the entry over it.
            print("warning: stale temp cleanup failed: %s" % exc, file=sys.stderr)
        text = validate_entry(ENTRY)
        data = text.encode("utf-8")
        target = write_atomic(out_dir, data)
        verify_output(target, data)
    except (TypeError, ValueError, RuntimeError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("wrote %s (%d characters, %d bytes)" % (target, len(text), len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
