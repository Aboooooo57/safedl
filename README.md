# safedl

Downloads that survive bad connections, and repair of broken downloads.

Multi-connection download managers on an unstable network (dropping VPNs, filtered
links, flaky Wi-Fi) often pre-allocate the full file, lose half their connections,
and still report **"finished"**. You get a file of the right size that is partly
zeros, and a video that breaks halfway through. `safedl` avoids that, and fixes
files that already ended up that way.

- **One connection, endless resume.** If the connection drops or stalls
  (< 10 KB/s for 45 s), it waits and continues from the exact byte where it stopped.
- **Never looks done when it isn't.** The download is written to `file.part` and
  renamed only after every byte has arrived.
- **Repairs broken downloads in place.** It finds the never-downloaded (zero-filled)
  parts of a file from any downloader and fetches only those with HTTP Range requests,
  keeping everything that is already good. Truncated files are completed too.
- **Play-tests the result.** For video and audio files it decodes every frame with
  `ffmpeg` and reports any errors.
- **Clear messages for dead links.** When a link expires or is tied to an IP that
  changed (403/404/410), it tells you so. Run it again with a fresh link and it
  continues where it stopped.
- **Safety checks.** It checks free disk space first, stops if the file is replaced
  mid-repair, and refuses to write if the server ignores the requested byte range.
- Shows a macOS notification when it finishes.

## Install

Requires Python 3.9+ and `curl` (built into macOS and most Linux distributions).
`ffmpeg` is optional and enables the play-test.

```bash
curl -fsSL https://raw.githubusercontent.com/Aboooooo57/safedl/main/safedl.py -o ~/.local/bin/safedl
chmod +x ~/.local/bin/safedl
```

(Any folder on your `PATH` works instead of `~/.local/bin`.)

## Usage

```bash
safedl get "https://example.com/big-file.mp4"            # into ~/Downloads
safedl get "https://example.com/big-file.mp4" -d ~/Movies
safedl get "https://example.com/big-file.mp4" -o ~/Movies/film.mp4
```

Press Ctrl+C to pause. Run the same command again to resume. If the file already
exists, `get` checks it and downloads only what is missing.

```bash
safedl repair ~/Downloads/broken.mp4 "https://example.com/big-file.mp4"
```

This fetches only the missing parts of a file that another downloader left broken.
The server must support byte ranges, which almost all file hosts do.

```bash
safedl check ~/Downloads/film.mp4
```

This reports any missing parts and play-tests the file. It downloads nothing.

## How missing parts are detected

The file is scanned in 1 MiB blocks. A block that contains 1 KiB or more of
consecutive zero bytes counts as not downloaded and is fetched again. Compressed
media almost never contains runs of zeros that long, so false positives are rare.
When one does happen, it only costs a re-download of that block. The `ffmpeg`
play-test catches any damage that isn't zero-filled.

## License

MIT
