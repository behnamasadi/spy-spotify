# spytify-linux

Records Spotify into tagged audio files on Linux, splitting automatically on
track changes, skipping adverts, and embedding cover art. The Linux
counterpart to Spytify (EspionSpotify), which is Windows-only.

```bash
./spytify.py                 # record to ~/Music/Spytify as 320k mp3
./spytify.py -n 40           # stop after 40 tracks
./spytify.py -f flac         # lossless
```

Start it, then play Spotify normally — the tool never controls playback, it
only listens. Ctrl-C finishes the track in progress and cleans up.

## Why Spytify itself cannot be ported

Spytify is not merely a .NET program that happens to run on Windows; every
load-bearing part of it is a Windows API. Mono does not help.

| Concern | Spytify (Windows) | Here (Linux) |
|---|---|---|
| Capture | WASAPI loopback on the whole output device | PipeWire tap on Spotify's own stream |
| Other apps' audio | must be muted to stay out of the recording | never captured in the first place |
| Track metadata | scraping the Spotify window title | MPRIS over D-Bus |
| Keeping the room quiet | a virtual sound card you install (VB-Cable) | a virtual cable created and removed automatically |
| UI | WinForms + MetroModernUI | command line |

The blocking dependencies in the original are `WasapiLoopbackCapture`,
the Windows audio-session API (`SimpleAudioVolume`), `combase.dll` /
`user32.dll` / `kernel32.dll` P/Invoke, `Process.MainWindowTitle` (returns
`""` on Linux), `MediaFoundationResampler`, WinForms, and the bundled
`libmp3lame.32/64.dll`. Roughly all of its ~12k lines, minus the file-naming
and tagging logic.

## Requirements

No Python packages, no pip, no build step. Everything below ships with a
standard PipeWire desktop:

- `pw-record`, `pw-dump`, `pw-cli`, `pw-metadata` (pipewire)
- `wpctl` (wireplumber)
- `busctl` (systemd)
- `ffmpeg` and `ffprobe`
- Python 3.8+

## Not interfering with anything else

By default Spotify is routed into its own virtual sound card — a null sink
named `SpytifyCable` — so it makes **no sound on your speakers at all** and
cannot clash with YouTube, calls, or anything else you are listening to. Your
master volume is never touched. Measured live, mid-recording:

```
spotify:output_FL  ->  SpytifyCable:playback_FL           (recorded, inaudible)
Brave:output_FL    ->  alsa_output.pci-...:playback_FL    (YouTube, normal)
```

The cable is created at startup and destroyed on exit, handing Spotify back to
your normal output. A stale cable left by a killed run is cleaned up on the
next start. Pass `--speakers` if you would rather hear Spotify.

Recording is independent of all of this, because the recorder is wired
directly to Spotify's stream:

```
spytify-linux:input_FL  <=  spotify:output_FL
spytify-linux:input_FR  <=  spotify:output_FR
```

Those are its only two inputs, so nothing else can leak in — not "quietly",
but structurally. The reverse also holds: recording does not disturb your
playback, and you can pause, seek or change the volume of anything else
freely.

## Volume

**Only Spotify's own volume slider affects the recording.** It scales the
stream before the recorder taps it, so a slider at 40% records at 40% —
measured as a 24 dB loss, because PipeWire volume is cubic and 0.4 becomes
0.064 of full scale.

The tool pins it to 100% at startup and at every track change, then restores
your setting on exit (`--no-force-volume` to opt out). It must be set through
MPRIS: setting the PipeWire node volume with `wpctl` appears to work but
Spotify re-applies its own value when playback resumes.

Your master volume, muting your speakers, and every other application's volume
have no effect on the recording whatsoever.

Spytify needed the same guard on Windows, pinning the audio session volume
to 1.0.

## Options

| Option | Meaning |
|---|---|
| `-o, --output DIR` | output directory (default `~/Music/Spytify`) |
| `-f, --format` | `mp3` `flac` `m4a` `ogg` `opus` `wav` (default `mp3`) |
| `-b, --bitrate` | kbps for lossy formats (default 320) |
| `-n, --max-tracks` | stop automatically after N tracks are saved |
| `--template` | path template; fields: `artist` `albumartist` `album` `title` `tracknum` `disc` |
| `--speakers` | hear Spotify on your normal output instead of the virtual cable |
| `--no-force-volume` | don't raise Spotify's volume to 100% |
| `--min-duration` | discard recordings shorter than N seconds (default 30) |
| `--max-lateness` | skip a track joined more than N seconds in (default 5) |
| `--keep-partial` | keep tracks even when joined part-way through |
| `--overwrite` | re-record tracks already on disk (default: skip them) |
| `--keep-ads` | record adverts too |
| `--no-art` | do not embed cover art |
| `--no-trim-silence` | keep the silent gap Spotify leaves at the end of a track |
| `--backfill DIR` | don't record; fill in missing year/genre on existing files |
| `--no-enrich` | skip the MusicBrainz year/genre lookup |
| `--contact` | contact address for the MusicBrainz User-Agent (optional) |
| `--ascii` | strip accents from filenames |
| `--player`, `--app` | override the MPRIS bus name / PipeWire stream match |

Default layout is `Album Artist/Album/NN - Title.ext`.

## How it works

1. `pw-dump` locates Spotify's `Stream/Output/Audio` node and reads its
   `object.serial`. `pw-record --target` wants the **serial**, not the node
   id — passing a node id does not error, it silently records your
   microphone instead.
2. `pw-record` streams raw s16le 48 kHz stereo into the process.
3. MPRIS is polled every 250 ms for the current track.
4. Audio is held in a 6-second buffer so a boundary can be cut at the exact
   sample where the new track began. MPRIS `Position` says how late the change
   was noticed, and those samples are moved onto the new track instead of
   being left on the end of the previous one — which is why recordings do not
   start with clipped intros (measured: 0.00 s leading silence).
5. Digital silence is trimmed from the end of each track. Spotify leaves a gap
   before whatever plays next, which otherwise lands in the file — measured at
   3.1 s on one song. Only the buffered tail is examined, so a genuinely quiet
   ending is never cut into.
6. `ffmpeg` encodes each track, writing `.part` first and renaming on success,
   so an interrupted run never leaves a half file that looks complete.

`ffmpeg` and `pw-record` are started with `start_new_session=True`. A terminal
sends Ctrl-C to the entire foreground process group, which would otherwise
kill `ffmpeg` mid-encode and destroy the final track.

Spotify tears down its PipeWire node whenever playback stops, so capture is
supervised and restarted automatically, and the cable routing is re-applied
each time (the node gets a new id). A pause mid-track continues the same file
rather than splitting it, and the paused seconds are not recorded.

## Tags written

| Tag | Source |
|---|---|
| title, artist, album, album artist | MPRIS |
| track number, disc number | MPRIS |
| comment | the Spotify track URL |
| cover art | `mpris:artUrl`, downloaded and embedded (640×640 JPEG) |
| date (year) | MusicBrainz |
| genre | MusicBrainz |

Spotify's MPRIS interface publishes no year and no genre, so those two are
looked up from MusicBrainz by album artist + album.

### MusicBrainz

No account, no API key and no registration — the web service answers anonymous
requests. It asks for a descriptive `User-Agent` and about one request per
second, both of which this tool honours. Results are cached per album.

Nothing personal is sent: the default User-Agent is just `spytify-linux/1.0`.
Pass `--contact you@example.com` if you would rather identify yourself, as
MusicBrainz prefers and which reduces throttling.

An account (free, at `musicbrainz.org/register`) is only needed to *edit* the
database, keep collections, or submit AcoustID fingerprints — not for lookups.

Practical notes, all found by testing against real recordings:

- Spotify album names carry suffixes MusicBrainz does not know. `By the Way
  (Deluxe Edition)` returns *nothing* while `By the Way` scores 100, so
  edition and soundtrack suffixes are stripped before searching. The stripping
  is conservative: `(What's the Story) Morning Glory?` is left alone.
- `Various Artists` is not an artist. Compilations are searched by album
  alone.
- If an album yields no genre, the artist's own MusicBrainz tags are used as a
  fallback. This matches by name, so it can occasionally attribute the wrong
  artist's genre.
- MusicBrainz answers `503` when throttling. Requests are retried with
  backoff and honour `Retry-After`; a throttled result is never cached, so the
  album is retried rather than blanked for the session. Bulk `--backfill` runs
  use a slower pace (3 s apart, longer backoff) because it throttles bursts
  much harder than the steady trickle of a recording session.
- Enrichment is best-effort and never blocks or fails a recording. Failures
  are counted and reported at the end of a run.

### Backfilling

`--backfill` repairs files already recorded, without re-recording them:

```bash
./spytify.py --backfill ~/Music/Spytify
```

It looks up only the files missing a year or genre and rewrites the tags with
`ffmpeg -c copy`. The audio is **bit-identical** afterwards (verified by
comparing the decoded stream's MD5) and artwork is preserved. Safe to re-run;
each pass retries whatever the last one could not resolve.

## Adverts

Free accounts get adverts between tracks. They are identified by their
`/com/spotify/ad/...` MPRIS trackid and skipped by default. In a 40-track run,
35 adverts were skipped correctly.

## What about the Spotify Web API?

Not used, and not needed — MPRIS already provides everything Spytify asked the
Web API for, locally and without authentication.

This matters as of the February 2026 developer changes: a Development Mode app
now requires its *owner* to hold an active Spotify Premium subscription, is
limited to five authorised users, and loses access to a number of endpoints.
Reading playback state has never required Premium, but registering the app now
effectively does.

## Results from a real run

40 consecutive tracks, unattended, 2 h 52 m:

| | |
|---|---|
| Tracks recorded | 40 of 40 |
| Adverts skipped | 35 |
| Encode failures | 0 |
| Discarded as too short | 0 |
| Trailing silence trimmed | 34 tracks |
| Core tags + artwork | 100% |
| Year and genre | 26 of 42 after backfill |

The year/genre gap is MusicBrainz coverage, not a failure of the tool: the
remainder are royalty-free and "epic cover" channels and regional releases
that the database simply does not carry.

## Limitations

- Records in real time. There is no faster-than-playback rip.
- Requires PipeWire. On plain PulseAudio, `parec -d <sink>.monitor` works but
  captures the whole device rather than Spotify alone.
- Cover art is embedded for `mp3`, `flac` and `m4a` only.
- Year and genre depend on MusicBrainz having the album; obscure releases will
  be left without them.
- The artist-level genre fallback matches by name and can pick the wrong
  artist for common names.
- Briefly audible on resume: Spotify creates a new stream node when playback
  restarts, and there is a short window before the cable routing is re-applied.
- No GUI. `tkinter` is not in the system Python on Ubuntu, and the tool
  deliberately has no dependencies.

## Licence

Same as the parent repository.
