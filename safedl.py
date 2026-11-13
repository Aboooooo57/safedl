#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""safedl - downloads that survive bad connections, and repair of broken downloads.

  safedl get URL [-o FILE | -d DIR]   download; resumes after every drop, never marks a
                                      half-finished file as done, checks it at the end.
                                      Run it again (even with a NEW link for the same file)
                                      to continue a paused download or fix a finished one.
  safedl repair FILE URL              find the missing (zero-filled) parts of a broken
                                      download and fetch only those, in place.
  safedl check FILE                   report missing parts and, for videos, play-test it.

Needs only curl (built into macOS). ffmpeg is used for the video play-test if installed.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
BLOCK = 1 << 20              # scan / repair granularity (1 MiB)
ZERO_RUN = b"\0" * 1024      # a block holding this many zeros in a row counts as "not downloaded"
CHUNK = 256 * 1024
MAX_FAILS = 60               # consecutive attempts without progress before pausing
VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm", ".ts", ".wmv", ".flv", ".mp3", ".m4a"}
CURL_NET = ["--connect-timeout", "20", "--speed-limit", "10240", "--speed-time", "45", "-A", UA]


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
        sys.exit(f"Could not reach the server: {r.stderr.strip()}")
    # the last block is the real answer (skips proxy "Connection established" and redirects)
    blocks = [b for b in re.split(r"\r?\n\r?\n", r.stdout) if b.strip().startswith("HTTP/")]
    lines = blocks[-1].strip().splitlines()
    status = int(lines[0].split()[1])
    h = {}
    for ln in lines[1:]:
        if ":" in ln:
            k, v = ln.split(":", 1)
            h[k.strip().lower()] = v.strip()
    if status >= 400:
        sys.exit(expired_message(status))
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", h.get("content-disposition", ""), re.I)
    name = unquote(m.group(1)) if m else unquote(Path(urlparse(url).path).name) or "download"
    size = int(h["content-length"]) if h.get("content-length", "").isdigit() else None
    return Remote(size, h.get("etag") or h.get("last-modified"),
                  h.get("accept-ranges", "").lower() == "bytes", Path(name).name)


# ---------------------------------------------------------------- checks
def find_holes(path, size):
    """Byte ranges (inclusive) that look never-downloaded: zero-filled blocks or a short tail."""
    holes, cur, i = [], None, 0
    with open(path, "rb") as f:
        while True:
            b = f.read(BLOCK)
            if not b:
                break
            bad = ZERO_RUN in b
            if bad and cur is None:
                cur = i
            elif not bad and cur is not None:
                holes.append([cur * BLOCK, i * BLOCK - 1]); cur = None
            i += 1
    if cur is not None:
        holes.append([cur * BLOCK, min(i * BLOCK, size) - 1])
    have = os.path.getsize(path)
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
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", "0", "-f", "null", "-"],
                       capture_output=True, text=True)
    errors = [ln for ln in r.stderr.splitlines() if ln.strip()]
    return (r.returncode == 0 and not errors), errors[:5]


def report(path, size):
    holes = find_holes(path, size)
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


# ---------------------------------------------------------------- repair
class Progress:
    def __init__(self, total):
        self.total, self.done, self.t0, self.last = total, 0, time.time(), 0

    def add(self, n):
        self.done += n
        now = time.time()
        if now - self.last >= 2 or self.done >= self.total:
            self.last = now
            speed = self.done / max(now - self.t0, 0.001)
            eta = (self.total - self.done) / speed if speed else 0
            print(f"\r  {human(self.done)} / {human(self.total)}  {human(speed)}/s  "
                  f"~{int(eta // 60)}m{int(eta % 60):02d}s left   ", end="", flush=True)


def fetch_range(url, path, inode, start, end, prog, hdr_file):
    """Write bytes start..end of url into path at the same offsets, resuming after drops."""
    pos, fails = start, 0
    while pos <= end:
        if os.stat(path).st_ino != inode:
            sys.exit("\nThe file was replaced or moved while repairing. Stopped so nothing is "
                     "written into the wrong file. Run the command again.")
        p = subprocess.Popen(["curl", "-sS", "--fail", "-L", *CURL_NET, "-D", hdr_file,
                              "-r", f"{pos}-{end}", url], stdout=subprocess.PIPE)
        checked, before = False, pos
        with open(path, "r+b") as out:
            out.seek(pos)
            while pos <= end:
                data = p.stdout.read(min(CHUNK, end - pos + 1))
                if not data:
                    break
                if not checked:   # make sure the server really sent the range we asked for
                    hdr = Path(hdr_file).read_text(errors="replace")
                    last = [b for b in re.split(r"\r?\n\r?\n", hdr) if b.strip().startswith("HTTP/")][-1]
                    m = re.search(r"content-range:\s*bytes\s+(\d+)-", last, re.I)
                    if " 206" not in last.splitlines()[0] or not m or int(m.group(1)) != pos:
                        p.kill()
                        sys.exit("\nThe server ignored the byte-range request, so a partial repair "
                                 "is impossible. Use `safedl get` to download the whole file again.")
                    checked = True
                out.write(data)
                pos += len(data)
                prog.add(len(data))
        p.kill() if pos > end else None
        p.stdout.close()
        rc = p.wait()
        if pos > end:
            break
        if rc == 22:                                  # HTTP error: link dead?
            print()
            probe(url)                                # exits with a clear message on 403/410
        fails = 0 if pos > before else fails + 1
        if fails >= MAX_FAILS:
            sys.exit("\nToo many failed attempts in a row. Check the connection and run the same "
                     "command again - finished parts are kept.")
        print(f"\r  connection dropped at {human(pos)} - retrying (attempt {fails + 1})        ",
              end="", flush=True)
        time.sleep(min(30, 2 + 2 * fails))


def repair(path, url, rem):
    path = Path(path)
    have = path.stat().st_size
    if rem.size is None:
        sys.exit("The server does not report the file size, so missing parts cannot be located.")
    if have > rem.size:
        sys.exit(f"Local file ({have} bytes) is bigger than the server's ({rem.size}). "
                 "It is a different file - download it again with `safedl get`.")
    if not rem.ranges:
        say("Warning: server does not advertise byte ranges; trying anyway.")
    holes = find_holes(path, rem.size)
    if not holes:
        say("No missing parts found.")
        return
    missing = sum(e - s + 1 for s, e in holes)
    need = rem.size - have + 64 * BLOCK
    if shutil.disk_usage(path.parent).free < need:
        sys.exit(f"Not enough free disk space: need about {human(need)}.")
    if have < rem.size:
        with open(path, "r+b") as f:
            f.truncate(rem.size)
    say(f"Re-downloading {human(missing)} in {len(holes)} place(s), keeping everything else.")
    inode, prog = path.stat().st_ino, Progress(missing)
    hdr_file = str(path.with_name(path.name + ".safedl-headers"))
    try:
        for s, e in holes:
            fetch_range(url, path, inode, s, e, prog, hdr_file)
    finally:
        Path(hdr_file).unlink(missing_ok=True)
    print()
    left = find_holes(path, rem.size)
    if left:
        sys.exit(f"Still missing {len(left)} part(s); run the command again.")
    say("All missing parts downloaded.")


# ---------------------------------------------------------------- full download
def download(url, part, rem):
    """Sequential single-connection download into `part`, resuming until it is complete."""
    fails = 0
    while True:
        have = part.stat().st_size if part.exists() else 0
        if rem.size is not None and have == rem.size:
            return
        if rem.size is not None and have > rem.size:
            say("Partial file is bigger than expected; starting over.")
            part.unlink(); have = 0
        cmd = ["curl", "-L", "--fail", "-#", *CURL_NET, "-o", str(part), url]
        if have and rem.ranges:
            cmd[2:2] = ["-C", "-"]
        rc = subprocess.call(cmd)
        now = part.stat().st_size if part.exists() else 0
        if rc == 0 and (rem.size is None or now == rem.size):
            return
        if rc == 22:
            probe(url)                       # exits with a clear message if the link died
        if rc == 33:                         # server refused resume
            say("Server refused to resume; starting over.")
            part.unlink(missing_ok=True)
        fails = 0 if now > have else fails + 1
        if fails >= MAX_FAILS:
            sys.exit("Too many failed attempts in a row. Run the same command again later - "
                     "the partial download is kept and will resume.")
        wait = min(30, 2 + 2 * fails)
        say(f"Connection dropped at {human(now)}"
            + (f" of {human(rem.size)}" if rem.size else "") + f" - resuming in {wait}s")
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
        repair(dest, a.url, rem)
        _, ok = report(dest, rem.size)
        notify(f"{dest.name}: {'ready' if ok is not False else 'still broken'}")
        sys.exit(0 if ok is not False else 1)

    if part.exists():
        old = json.loads(meta.read_text()) if meta.exists() else {}
        if old and (old.get("size") != rem.size or (old.get("etag") and old.get("etag") != rem.etag)):
            say("The file on the server changed since the last attempt; starting over.")
            part.unlink()
        else:
            say(f"Resuming from {human(part.stat().st_size)}.")
    meta.write_text(json.dumps({"url": a.url, "size": rem.size, "etag": rem.etag}))

    if rem.size:
        need = rem.size - (part.stat().st_size if part.exists() else 0) + 64 * BLOCK
        free = shutil.disk_usage(dest.parent).free
        if free < need:
            sys.exit(f"Not enough free disk space: need {human(need)}, have {human(free)}.")

    try:
        download(a.url, part, rem)
    except KeyboardInterrupt:
        sys.exit("\nPaused. Run the same command again to continue.")
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
    sys.exit(1 if ok is False else 0)


def main():
    ap = argparse.ArgumentParser(prog="safedl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("get", help="download (or continue / repair) a file")
    g.add_argument("url")
    g.add_argument("-o", "--output", help="output file (default: server's name in --dir)")
    g.add_argument("-d", "--dir", default="~/Downloads", help="folder (default ~/Downloads)")
    g.add_argument("--no-check", action="store_true", help="skip the ffmpeg play-test")
    r = sub.add_parser("repair", help="fetch only the missing parts of a broken download")
    r.add_argument("file")
    r.add_argument("url")
    c = sub.add_parser("check", help="find missing parts and play-test a file")
    c.add_argument("file")
    a = ap.parse_args()

    if not shutil.which("curl"):
        sys.exit("curl is required.")
    try:
        if a.cmd == "get":
            cmd_get(a)
        elif a.cmd == "repair":
            rem = probe(a.url)
            repair(a.file, a.url, rem)
            _, ok = report(a.file, rem.size)
            notify(f"{Path(a.file).name}: {'ready' if ok is not False else 'still broken'}")
            sys.exit(0 if ok is not False else 1)
        else:
            holes, ok = report(a.file, None)
            sys.exit(0 if not holes and ok is not False else 1)
    except KeyboardInterrupt:
        sys.exit("\nStopped. Run the same command again to continue - nothing is lost.")


if __name__ == "__main__":
    main()
