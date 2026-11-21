#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""safedl - downloads that survive bad connections, and repair of broken downloads.

  safedl get URL [-o FILE | -d DIR] [-c N]   download over N connections (default 4);
                                             resumes after every drop, never marks a
                                             half-finished file as done, checks it at the end.
                                             Run it again (even with a NEW link for the same
                                             file) to continue a paused download or fix a
                                             finished one.
  safedl repair FILE URL [-c N]              find the missing (zero-filled) parts of a broken
                                             download and fetch only those, in place.
  safedl check FILE                          report missing parts and, for videos, play-test it.

Needs only curl (built into macOS). ffmpeg is used for the video play-test if installed.
"""
import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
BLOCK = 1 << 20              # scan / repair granularity (1 MiB)
ZERO_RUN = b"\0" * 1024      # a block holding this many zeros in a row counts as "not downloaded"
ZERO_RUN_RE = re.compile(rb"\0{1024,}")
CHUNK = 256 * 1024
MAX_FAILS = 60               # consecutive attempts without progress before giving up
DEFAULT_CONNS, MAX_CONNS = 4, 16
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".ts", ".wmv", ".flv", ".mp3", ".m4a"}
CURL_NET = ["--connect-timeout", "20", "--speed-limit", "10240", "--speed-time", "45", "-A", UA]


class Fatal(Exception):
    """An error that stops the command with a message for the user."""


class RangeIgnored(Fatal):
    """The server answered a byte-range request with something other than that range."""


def say(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def notify(text):
    subprocess.run(["osascript", "-e", f'display notification "{text}" with title "safedl"'],
                   capture_output=True)


def http_blocks(text):
    """Header blocks of each HTTP response in a curl header dump (proxy CONNECT, redirects...)."""
    return [b for b in re.split(r"\r?\n\r?\n", text) if b.strip().startswith("HTTP/")]


# ---------------------------------------------------------------- server info
class Remote:
    def __init__(self, size, etag, ranges, filename):
        self.size, self.etag, self.ranges, self.filename = size, etag, ranges, filename


def expired_message(status):
    return (f"The server answered HTTP {status}. The link has probably expired, or it is tied to an "
            "IP address that changed (VPN reconnected?). Get a fresh link and run the same command "
            "with it - everything already downloaded is kept.")


def probe(url):
    r = subprocess.run(["curl", "-sS", "-I", "-L", "--max-time", "60", *CURL_NET, url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise Fatal(f"Could not reach the server: {r.stderr.strip()}")
    lines = http_blocks(r.stdout)[-1].strip().splitlines()
    status = int(lines[0].split()[1])
    h = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            h[k.strip().lower()] = v.strip()
    if status >= 400:
        raise Fatal(expired_message(status))
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", h.get("content-disposition", ""), re.I)
    name = unquote(m.group(1)) if m else unquote(Path(urlparse(url).path).name) or "download"
    size = int(h["content-length"]) if h.get("content-length", "").isdigit() else None
    return Remote(size, h.get("etag") or h.get("last-modified"),
                  h.get("accept-ranges", "").lower() == "bytes", Path(name).name)


# ---------------------------------------------------------------- checks
def padding_size(buf, start):
    """If the zeros at buf[start:] are the payload of padding a file format declares on purpose,
    return the declared payload size, else None. Knows MKV/WebM Void elements and MP4 free/skip."""
    for vlen in range(1, 9):                     # MKV/WebM: 0xEC, EBML size (vlen bytes), zeros
        h = start - 1 - vlen
        if h < 0:
            break
        first = buf[h + 1]
        if buf[h] == 0xEC and first and 9 - first.bit_length() == vlen:
            return int.from_bytes(buf[h + 1:h + 1 + vlen], "big") & ((1 << (7 * vlen)) - 1)
    h = start - 8                                # MP4: 32-bit size, b"free"/b"skip", zeros
    if h >= 0 and buf[h + 4:h + 8] in (b"free", b"skip"):
        return int.from_bytes(buf[h:h + 4], "big") - 8
    return None


def block_is_missing(buf, carry):
    """Does this block hold a long zero run that no declared padding explains?
    `carry` = declared padding bytes continuing from the previous block.
    Returns (missing, carry for the next block)."""
    if ZERO_RUN not in buf:
        return False, 0
    missing, nxt = False, 0
    for m in ZERO_RUN_RE.finditer(buf):
        s, n = m.start(), m.end() - m.start()
        declared = carry if s == 0 and carry else padding_size(buf, s)
        if declared is not None and n <= declared + 4:   # +4: next header may start with zeros
            if m.end() == len(buf):
                nxt = max(0, declared - n)
            continue
        missing = True
    return missing, nxt


def find_holes(path, size):
    """Byte ranges (inclusive) that look never-downloaded: zero-filled blocks or a short tail."""
    holes, cur, i, carry = [], None, 0, 0
    with open(path, "rb") as f:
        while True:
            b = f.read(BLOCK)
            if not b:
                break
            bad, carry = block_is_missing(b, carry)
            if bad and cur is None:
                cur = i
            elif not bad and cur is not None:
                holes.append([cur * BLOCK, i * BLOCK - 1]); cur = None
            i += 1
    have = os.path.getsize(path)
    if cur is not None:
        holes.append([cur * BLOCK, have - 1])
    if size and have < size:                      # truncated file: the tail is missing too
        if holes and holes[-1][1] >= have - 1:
            holes[-1][1] = size - 1
        else:
            holes.append([have, size - 1])
    return [(s, e) for s, e in holes]


def play_test(path):
    """Decode every frame with ffmpeg. Returns (ok, first error lines) or (None, why) if skipped."""
    if Path(path).suffix.lower() not in VIDEO_EXT:
        return None, "not a video/audio file"
    if not shutil.which("ffmpeg"):
        return None, "ffmpeg not installed (brew install ffmpeg)"
    say("Play-testing every frame with ffmpeg (about a minute per GB)...")
    # video and audio only: the null output cannot take subtitle/attachment tracks
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                        "-map", "0:v?", "-map", "0:a?", "-f", "null", "-"],
                       capture_output=True, text=True)
    errors = [ln for ln in r.stderr.splitlines() if ln.strip()]
    return (r.returncode == 0 and not errors), errors[:5]


def report(path, size, genuine=()):
    """genuine: ranges just verified against the server - zeros there are really in the file."""
    holes = [h for h in find_holes(path, size)
             if not any(s <= h[0] and h[1] <= e for s, e in genuine)]
    missing = sum(e - s + 1 for s, e in holes)
    if holes:
        say(f"Missing data: {human(missing)} in {len(holes)} place(s): "
            + ", ".join(f"{human(s)}-{human(e + 1)}" for s, e in holes[:6]))
    else:
        say("No missing parts found.")
    ok, info = play_test(path)
    if ok is None:
        say(f"Play-test skipped: {info}")
    elif ok:
        say("Play-test passed: every frame decodes.")
    else:
        say("Play-test FAILED:\n    " + "\n    ".join(info))
    return holes, ok


# ---------------------------------------------------------------- range engine
class Progress:
    def __init__(self, total):
        self.total, self.done, self.t0, self.last = total, 0, time.time(), 0
        self.lock, self.width = threading.Lock(), 0

    def add(self, n):
        with self.lock:
            self.done += n
            now = time.time()
            if now - self.last >= 2 or self.done >= self.total:
                self.last = now
                speed = self.done / max(now - self.t0, 0.001)
                eta = (self.total - self.done) / speed if speed else 0
                line = (f"  {human(self.done)} / {human(self.total)}  {human(speed)}/s  "
                        f"~{int(eta // 60)}m{int(eta % 60):02d}s left")
                self.width = max(self.width, len(line))   # pad over a longer previous line
                print("\r" + line.ljust(self.width), end="", flush=True)

    def note(self, msg):
        with self.lock:
            print(f"\r  {msg}".ljust(70), flush=True)


def check_range_reply(hdr_file, pos):
    """Raise RangeIgnored unless the server's reply is '206' starting exactly at pos."""
    deadline = time.time() + 3
    while True:   # curl writes the header dump just before the body; wait for the real reply
        try:
            blocks = http_blocks(Path(hdr_file).read_text(errors="replace"))
        except FileNotFoundError:
            blocks = []
        if blocks and "connection established" not in blocks[-1].splitlines()[0].lower():
            break
        if time.time() > deadline:
            raise RangeIgnored("Could not read the server's reply headers.")
        time.sleep(0.05)
    last = blocks[-1]
    m = re.search(r"content-range:\s*bytes\s+(\d+)-", last, re.I)
    if " 206" not in last.splitlines()[0] or not m or int(m.group(1)) != pos:
        raise RangeIgnored("The server ignored the byte-range request, so partial downloading is "
                           "impossible with this server.")


class Fetcher:
    """Downloads byte ranges of one URL into one file, over several connections at once."""

    def __init__(self, url, path, prog, compare=False):
        self.url, self.path, self.prog = url, Path(path), prog
        self.inode = self.path.stat().st_ino
        self.stop = threading.Event()
        self.procs, self.plock = set(), threading.Lock()
        self.compare, self.same = compare, 0   # compare: count bytes that were already correct

    def fetch(self, start, end, on_data, hdr_file):
        """Write bytes start..end into the file at the same offsets, resuming after drops."""
        pos, fails = start, 0
        while pos <= end and not self.stop.is_set():
            if os.stat(self.path).st_ino != self.inode:
                raise Fatal("The file was replaced or moved while downloading. Stopped so nothing "
                            "is written into the wrong file. Run the command again.")
            p = subprocess.Popen(["curl", "-sS", "--fail", "-L", *CURL_NET, "-D", hdr_file,
                                  "-r", f"{pos}-{end}", self.url],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            with self.plock:
                self.procs.add(p)
            checked, before = False, pos
            try:
                with open(self.path, "r+b", buffering=0) as out:
                    out.seek(pos)
                    while pos <= end:
                        data = p.stdout.read(min(CHUNK, end - pos + 1))
                        if not data:
                            break
                        if not checked:
                            check_range_reply(hdr_file, pos)
                            checked = True
                        if self.compare:
                            if out.read(len(data)) == data:
                                with self.plock:
                                    self.same += len(data)
                            out.seek(pos)
                        out.write(data)
                        pos += len(data)
                        on_data(pos, len(data))
            finally:
                if p.poll() is None:
                    p.kill()
                p.stdout.close()
                rc = p.wait()
                with self.plock:
                    self.procs.discard(p)
            if pos > end or self.stop.is_set():
                break
            if rc == 22:
                probe(self.url)               # raises a clear message if the link died
            fails = 0 if pos > before else fails + 1
            if fails >= MAX_FAILS:
                raise Fatal("Too many failed attempts in a row. Check the connection and run the "
                            "same command again - finished parts are kept.")
            self.prog.note(f"connection dropped at {human(pos)} - retrying")
            self.stop.wait(min(30, 2 + 2 * fails))

    def run(self, chunks, conns, save=None):
        """chunks: list of [start, end, pos]; fetches pos..end of each. Updates pos as it goes."""
        q = queue.Queue()
        for c in chunks:
            if c[2] <= c[1]:
                q.put(c)
        errors = []

        def worker(n):
            hdr = str(self.path.with_name(f"{self.path.name}.safedl-h{n}"))
            try:
                while not self.stop.is_set():
                    try:
                        c = q.get_nowait()
                    except queue.Empty:
                        return

                    def on_data(pos, k, c=c):
                        c[2] = pos
                        self.prog.add(k)
                    self.fetch(c[2], c[1], on_data, hdr)
            except Fatal as e:
                errors.append(e)
                self.stop.set()
            finally:
                Path(hdr).unlink(missing_ok=True)
                done[n].set()

        # Wait on per-worker Events, not Thread.join(): a join() interrupted by Ctrl+C can make
        # the next join() on that thread return while it is still running.
        n_workers = max(1, min(conns, q.qsize()))
        done = [threading.Event() for _ in range(n_workers)]
        for i in range(n_workers):
            threading.Thread(target=worker, args=(i,), daemon=True).start()
        last = time.time()
        try:
            while not all(d.is_set() for d in done):
                next(d for d in done if not d.is_set()).wait(0.5)
                if save and time.time() - last >= 3:
                    save(); last = time.time()
        except KeyboardInterrupt:
            self.stop.set()
            with self.plock:
                for p in self.procs:
                    p.kill()
            for d in done:
                d.wait(60)
            raise
        finally:
            if save:
                save()
        if errors:
            raise errors[0]
        if any(c[2] <= c[1] for c in chunks):
            raise Fatal("Some parts are still missing; run the same command again.")


def make_chunks(ranges, conns):
    """Split byte ranges into pieces small enough to keep every connection busy until the end."""
    total = sum(e - s + 1 for s, e in ranges)
    size = max(4 * BLOCK, min(64 * BLOCK, total // (conns * 8) // BLOCK * BLOCK))
    chunks = []
    for s, e in ranges:
        a = s
        while a <= e:
            b = min(a + size - 1, e)
            chunks.append([a, b, a])
            a = b + 1
    return chunks


def check_space(folder, need):
    free = shutil.disk_usage(folder).free
    if free < need + 64 * BLOCK:
        raise Fatal(f"Not enough free disk space: need {human(need)}, have {human(free)}.")


# ---------------------------------------------------------------- repair
def repair(path, url, rem, conns):
    """Fetch the parts that look missing. Returns ranges whose zeros turned out to be genuine."""
    path = Path(path)
    have = path.stat().st_size
    if rem.size is None:
        raise Fatal("The server does not report the file size, so missing parts cannot be located.")
    if have > rem.size:
        raise Fatal(f"Local file ({have} bytes) is bigger than the server's ({rem.size}). "
                    "It is a different file - download it again with `safedl get`.")
    holes = find_holes(path, rem.size)
    if not holes:
        return []                     # report() says "No missing parts found."
    missing = sum(e - s + 1 for s, e in holes)
    check_space(path.parent, missing)
    if have < rem.size:
        with open(path, "r+b") as f:
            f.truncate(rem.size)
    chunks = make_chunks(holes, conns)
    n = min(conns, len(chunks))
    say(f"Re-downloading {human(missing)} in {len(holes)} place(s) over {n} connection(s), "
        "keeping everything else.")
    f = Fetcher(url, path, Progress(missing), compare=True)
    f.run(chunks, conns)        # raises unless every byte of every flagged part arrived
    print()
    # those parts now hold exactly the server's bytes, so zeros still there are really in the file
    if f.same == missing:
        say(f"Nothing was missing: the {human(missing)} that looked empty really is empty padding "
            "inside the file (the server sent the same bytes).")
    else:
        say(f"Missing parts downloaded ({human(missing - f.same)} was actually missing).")
    return find_holes(path, rem.size)


# ---------------------------------------------------------------- full download
def download_chunked(url, part, meta, rem, conns, old):
    """Multi-connection download into `part`; progress of every piece is saved in `meta`."""
    chunks = old.get("chunks") if part.exists() else None
    if not chunks:
        # a partial file from a plain single-connection run is valid from byte 0 up to its size
        have = part.stat().st_size if part.exists() else 0
        chunks = make_chunks([(0, rem.size - 1)], conns)
        for c in chunks:
            c[2] = min(c[1] + 1, max(c[0], have))
    remaining = sum(c[1] - c[2] + 1 for c in chunks if c[2] <= c[1])
    check_space(part.parent, remaining)
    part.touch()
    with open(part, "r+b") as f:
        f.truncate(rem.size)

    def save():
        tmp = meta.with_name(meta.name + ".tmp")
        tmp.write_text(json.dumps({"url": url, "size": rem.size, "etag": rem.etag, "chunks": chunks}))
        tmp.replace(meta)
    save()
    n = min(conns, sum(1 for c in chunks if c[2] <= c[1]))
    say(f"Downloading over {n} connection(s)"
        + (f" - resuming, {human(remaining)} left." if remaining < rem.size else "."))
    try:
        Fetcher(url, part, Progress(remaining)).run(chunks, conns, save)
    except RangeIgnored:
        print()
        say("This server does not allow partial downloads; switching to one plain download.")
        part.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        rem.ranges = False
        download_plain(url, part, rem)
    print()


def download_plain(url, part, rem):
    """Single-connection download into `part`, for servers without byte ranges or a known size."""
    fails = 0
    while True:
        have = part.stat().st_size if part.exists() else 0
        if rem.size is not None and have > rem.size:
            part.unlink(); have = 0
        cmd = ["curl", "-L", "--fail", "-#", *CURL_NET, "-o", str(part), url]
        if have and rem.ranges:
            cmd[2:2] = ["-C", "-"]
        elif have:
            part.unlink(); have = 0           # cannot resume on this server: start over
        rc = subprocess.call(cmd)
        now = part.stat().st_size if part.exists() else 0
        if rc == 0 and (rem.size is None or now == rem.size):
            return
        if rc == 22:
            probe(url)
        fails = 0 if now > have else fails + 1
        if fails >= MAX_FAILS:
            raise Fatal("Too many failed attempts in a row. Run the same command again later.")
        wait = min(30, 2 + 2 * fails)
        say(f"Connection dropped at {human(now)} - retrying in {wait}s")
        time.sleep(wait)


def cmd_get(a):
    rem = probe(a.url)
    dest = Path(a.output).expanduser() if a.output else Path(a.dir).expanduser() / rem.filename
    if dest.is_dir():
        dest = dest / rem.filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    meta = dest.with_name(dest.name + ".part.json")
    say(f"{rem.filename}: {human(rem.size) if rem.size else 'unknown size'} -> {dest}")

    if dest.exists():
        say("File already exists - checking it and fetching only what is missing.")
        genuine = repair(dest, a.url, rem, a.connections)
        _, ok = report(dest, rem.size, genuine)
        notify(f"{dest.name}: {'ready' if ok is not False else 'still broken'}")
        return 0 if ok is not False else 1

    old = {}
    if part.exists():
        try:
            old = json.loads(meta.read_text())
        except (OSError, ValueError):
            old = {}
        changed = old and (old.get("size") != rem.size
                           or (old.get("etag") and rem.etag and old["etag"] != rem.etag))
        if changed:
            say("The file on the server changed since the last attempt; starting over.")
            part.unlink(); old = {}

    try:
        if rem.size is not None and rem.ranges:
            download_chunked(a.url, part, meta, rem, a.connections, old)
        else:
            if part.exists() and old.get("chunks"):
                part.unlink()                 # a multi-connection partial cannot be resumed plainly
            if rem.size:
                check_space(dest.parent, rem.size - (part.stat().st_size if part.exists() else 0))
            meta.write_text(json.dumps({"url": a.url, "size": rem.size, "etag": rem.etag}))
            download_plain(a.url, part, rem)
    except KeyboardInterrupt:
        print()
        raise Fatal("Paused. Run the same command again to continue.")
    part.rename(dest)
    meta.unlink(missing_ok=True)
    say(f"Downloaded {dest}")
    ok = None
    if not a.no_check:
        ok, info = play_test(dest)
        if ok is False:
            say("Play-test FAILED:\n    " + "\n    ".join(info)
                + "\nRun `safedl get` again with the same link to repair it.")
        elif ok:
            say("Play-test passed: every frame decodes.")
    notify(f"{dest.name}: {'downloaded but broken' if ok is False else 'ready'}")
    return 1 if ok is False else 0


def connections(v):
    n = int(v)
    if not 1 <= n <= MAX_CONNS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_CONNS}")
    return n


def main():
    ap = argparse.ArgumentParser(prog="safedl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="download (or continue / repair) a file")
    g.add_argument("url")
    g.add_argument("-o", "--output", help="output file (default: server's name in --dir)")
    g.add_argument("-d", "--dir", default="~/Downloads", help="folder (default ~/Downloads)")
    g.add_argument("-c", "--connections", type=connections, default=DEFAULT_CONNS,
                   help=f"parallel connections (1-{MAX_CONNS}, default {DEFAULT_CONNS})")
    g.add_argument("--no-check", action="store_true", help="skip the ffmpeg play-test")
    r = sub.add_parser("repair", help="fetch only the missing parts of a broken download")
    r.add_argument("file")
    r.add_argument("url")
    r.add_argument("-c", "--connections", type=connections, default=DEFAULT_CONNS,
                   help=f"parallel connections (1-{MAX_CONNS}, default {DEFAULT_CONNS})")
    c = sub.add_parser("check", help="find missing parts and play-test a file")
    c.add_argument("file")
    a = ap.parse_args()

    if not shutil.which("curl"):
        sys.exit("curl is required.")
    try:
        if a.cmd == "get":
            sys.exit(cmd_get(a))
        elif a.cmd == "repair":
            rem = probe(a.url)
            genuine = repair(a.file, a.url, rem, a.connections)
            _, ok = report(a.file, rem.size, genuine)
            notify(f"{Path(a.file).name}: {'ready' if ok is not False else 'still broken'}")
            sys.exit(0 if ok is not False else 1)
        else:
            holes, ok = report(a.file, None)
            sys.exit(0 if not holes and ok is not False else 1)
    except Fatal as e:
        sys.exit(f"\n{e}")
    except KeyboardInterrupt:
        sys.exit("\nStopped. Run the same command again to continue - nothing is lost.")


if __name__ == "__main__":
    main()
