#!/usr/bin/env python3
"""
spytify-linux - record Spotify to tagged audio files on Linux.

The Linux counterpart to Spytify (EspionSpotify). Instead of Windows WASAPI
loopback + window-title scraping, this taps Spotify's PipeWire stream directly
and reads track metadata from MPRIS over D-Bus.

External programs used (no Python packages required):
  pw-record, pw-dump   - PipeWire capture and object discovery
  busctl               - D-Bus property reads (MPRIS)
  ffmpeg               - encoding, tagging, cover art
  pw-cli, pw-metadata  - the virtual cable Spotify is routed into
"""

import argparse
import array
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

RATE = 48000
CHANNELS = 2
SAMPLE_BYTES = 2                              # s16le
BPS = RATE * CHANNELS * SAMPLE_BYTES          # bytes per second of audio
CHUNK = BPS // 10                             # 100 ms reads
LAG_SECONDS = 6.0                             # how far behind the encoder runs
SILENCE_FLOOR = 33                            # |s16| below this is silence (~-60 dBFS)
SILENCE_FRAME = 480                           # 10 ms of frames, per scan step
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

FORMATS = {
    # ext: (codec args, muxer, supports embedded cover art)
    "mp3":  (["-c:a", "libmp3lame"], "mp3", True),
    "flac": (["-c:a", "flac"], "flac", True),
    "m4a":  (["-c:a", "aac"], "ipod", True),
    "ogg":  (["-c:a", "libvorbis"], "ogg", False),
    "opus": (["-c:a", "libopus"], "opus", False),
    "wav":  (["-c:a", "pcm_s16le"], "wav", False),
}
LOSSLESS_CODECS = {"flac", "pcm_s16le"}


def trim_trailing_silence(data, max_trim):
    """Drop digital silence from the end of a track.

    Spotify leaves a gap between a song and whatever follows it, and that gap
    lands at the end of the finished file. Only the last `max_trim` bytes are
    examined, so a quiet ending is never cut into.
    """
    frame = SILENCE_FRAME * CHANNELS * SAMPLE_BYTES
    limit = min(max_trim, len(data))
    limit -= limit % frame
    cut = 0
    while cut + frame <= limit:
        chunk = data[len(data) - cut - frame:len(data) - cut]
        samples = array.array("h")
        samples.frombytes(chunk)
        if max(max(samples), -min(samples)) > SILENCE_FLOOR:
            break
        cut += frame
    return data[:len(data) - cut] if cut else data


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# --------------------------------------------------------------------------
# D-Bus / MPRIS
# --------------------------------------------------------------------------

def _unwrap(v):
    """busctl --json=short wraps every value as {"type": .., "data": ..}."""
    if isinstance(v, dict):
        if "data" in v and "type" in v:
            return _unwrap(v["data"])
        return {k: _unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


def _as_float(v, default=0.0):
    try:
        return float(v)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_int(v, default=0):
    try:
        return int(v)            # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


class Mpris:
    def __init__(self, bus_name):
        self.bus = bus_name

    def _prop(self, name):
        try:
            out = subprocess.run(
                ["busctl", "--user", "--json=short", "get-property",
                 self.bus, MPRIS_PATH, PLAYER_IFACE, name],
                capture_output=True, text=True, timeout=4,
            )
            if out.returncode != 0:
                return None
            return _unwrap(json.loads(out.stdout))
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            return None

    def alive(self):
        try:
            out = subprocess.run(["busctl", "--user", "list", "--no-legend"],
                                 capture_output=True, text=True, timeout=4)
            return any(line.split()[:1] == [self.bus] for line in out.stdout.splitlines())
        except (subprocess.SubprocessError, OSError):
            return False

    def status(self):
        return self._prop("PlaybackStatus") or "Stopped"

    def position(self):
        """Seconds into the current track, or 0.0 if the player won't say."""
        return max(0.0, _as_float(self._prop("Position")) / 1e6)

    def volume(self):
        return _as_float(self._prop("Volume"), 1.0)

    def set_volume(self, value):
        try:
            out = subprocess.run(
                ["busctl", "--user", "set-property", self.bus, MPRIS_PATH,
                 PLAYER_IFACE, "Volume", "d", f"{value:.3f}"],
                capture_output=True, text=True, timeout=4)
            return out.returncode == 0
        except (subprocess.SubprocessError, OSError):
            return False

    def track(self):
        m = self._prop("Metadata")
        if not isinstance(m, dict) or not m:
            return None
        artists = m.get("xesam:artist") or []
        albumartists = m.get("xesam:albumArtist") or artists
        return Track(
            trackid=str(m.get("mpris:trackid") or ""),
            title=str(m.get("xesam:title") or ""),
            artist="; ".join(artists),
            albumartist="; ".join(albumartists),
            album=str(m.get("xesam:album") or ""),
            tracknum=_as_int(m.get("xesam:trackNumber")),
            discnum=_as_int(m.get("xesam:discNumber")),
            length=_as_float(m.get("mpris:length")) / 1e6,
            arturl=str(m.get("mpris:artUrl") or ""),
            url=str(m.get("xesam:url") or ""),
        )


class Track:
    def __init__(self, trackid, title, artist, albumartist, album,
                 tracknum, discnum, length, arturl, url):
        self.trackid = trackid
        self.title = title
        self.artist = artist
        self.albumartist = albumartist
        self.album = album
        self.tracknum = tracknum
        self.discnum = discnum
        self.length = length
        self.arturl = arturl
        self.url = url

    @property
    def is_ad(self):
        # Spotify ads carry a /com/spotify/ad/... trackid.
        if "/ad/" in self.trackid or ":ad:" in self.trackid:
            return True
        # A named item with neither artist nor album is a promo, not a song.
        return bool(self.title) and not self.artist and not self.album

    @property
    def is_incomplete(self):
        """Spotify briefly publishes half-empty metadata while switching tracks."""
        return not self.trackid or (not self.title and not self.artist)

    def __str__(self):
        return f"{self.artist} - {self.title}" if self.artist else (self.title or "<unknown>")


# --------------------------------------------------------------------------
# PipeWire
# --------------------------------------------------------------------------

def find_stream(app_match):
    """(object.serial, node id) of the app's stream, or (None, None) if idle.

    pw-record --target wants object.serial, NOT the node id - node ids get
    recycled and are not what the CLI expects. wpctl, confusingly, wants the
    node id, so both are returned.
    """
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=6)
        objs = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None, None
    needle = app_match.lower()
    for o in objs:
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        p = (o.get("info") or {}).get("props") or {}
        if p.get("media.class") != "Stream/Output/Audio":
            continue
        hay = f"{p.get('application.name', '')} {p.get('node.name', '')}".lower()
        if needle in hay:
            return p.get("object.serial"), o.get("id")
    return None, None


class Capture:
    """One pw-record process feeding raw s16le into a queue."""

    def __init__(self, serial):
        self.proc = subprocess.Popen(
            ["pw-record", f"--target={serial}",
             f"--rate={RATE}", f"--channels={CHANNELS}", "--format=s16",
             "--latency=20ms", "-P", '{ node.name=spytify-linux }', "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,   # Ctrl-C must not kill the capture directly
        )
        self.q = queue.Queue(maxsize=2048)   # ~200s of audio headroom
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _skip_wav_header(self):
        """pw-record emits a RIFF header before the PCM; walk it properly."""
        f = self.proc.stdout
        if f is None:
            return b""
        hdr = f.read(12)
        if len(hdr) < 12 or hdr[0:4] != b"RIFF" or hdr[8:12] != b"WAVE":
            return hdr  # not a header after all - treat as audio
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                return b""
            cid, size = ch[0:4], int.from_bytes(ch[4:8], "little")
            if cid == b"data":
                return b""
            f.read(size + (size & 1))

    def _read(self):
        try:
            leftover = self._skip_wav_header()
            if leftover:
                self.q.put(leftover)
            while not self.stop.is_set() and self.proc.stdout is not None:
                data = self.proc.stdout.read(CHUNK)
                if not data:
                    break
                try:
                    self.q.put(data, timeout=1)
                except queue.Full:
                    pass  # encoder fell behind; drop rather than grow unbounded
        except (OSError, ValueError):
            pass
        finally:
            self.q.put(None)  # EOF sentinel

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        self.stop.set()
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except (subprocess.SubprocessError, OSError):
            try:
                self.proc.kill()
            except OSError:
                pass


# --------------------------------------------------------------------------
# MusicBrainz (anonymous - no account, no API key; 1 req/sec, real User-Agent)
# --------------------------------------------------------------------------

class MusicBrainz:
    """Fills in year and genre, which Spotify's MPRIS data does not provide."""

    ENDPOINT = "https://musicbrainz.org/ws/2/release-group"
    MIN_INTERVAL = 1.5          # their published rate limit is 1 req/sec
    RETRY_DELAYS = (2.0, 4.0)   # MusicBrainz answers 503 when it throttles
    # Bulk mode goes slower and waits far longer. One-at-a-time lookups during
    # a recording are spaced out by the songs themselves; a backfill is a
    # burst, and MusicBrainz throttles bursts hard.
    BULK_INTERVAL = 3.0
    BULK_RETRY_DELAYS = (5.0, 15.0, 30.0)

    # Spotify album names carry edition suffixes that MusicBrainz has never
    # heard of - "By the Way (Deluxe Edition)" returns nothing at all, while
    # "By the Way" scores 100. Only parentheses containing one of these words
    # are stripped, so a title like "Sign o' the Times (Album)" survives.
    EDITION_RE = re.compile(
        r"\s*[\(\[][^)\]]*\b(deluxe|remaster(ed)?|edition|expanded|anniversary"
        r"|version|bonus|explicit|reissue|mono|stereo|original\s+(motion\s+picture\s+)?"
        r"(soundtrack|score|game\s+soundtrack)|soundtrack|score)\b[^)\]]*[\)\]]", re.I)
    VARIOUS_RE = re.compile(r"^\s*(various(\s+artists)?|va|soundtrack|cast)\s*$", re.I)
    TRAILING_RE = re.compile(
        r"\s*-\s*(deluxe|remaster(ed)?|expanded|anniversary|reissue)\b.*$", re.I)

    def __init__(self, enabled, contact=None, timeout=10.0, bulk=False):
        self.enabled = enabled
        self.timeout = timeout
        self.interval = self.BULK_INTERVAL if bulk else self.MIN_INTERVAL
        self.retry_delays = self.BULK_RETRY_DELAYS if bulk else self.RETRY_DELAYS
        self.cache = {}         # (albumartist, album) -> (year, genre)
        self.artist_cache = {}  # artist -> genre
        self.last_call = 0.0
        self.warned = False
        self.failures = 0
        self.hits = 0
        # The first request pays for DNS + TLS and can take several seconds,
        # so the timeout is generous; audio keeps buffering meanwhile.
        who = f" ( {contact} )" if contact else ""
        self.ua = f"spytify-linux/1.0{who}"

    def lookup(self, artist, album):
        if not self.enabled or not album:
            return None, None
        key = (artist.lower(), album.lower())
        if key in self.cache:
            return self.cache[key]

        # "Various Artists" is not an artist; searching for it finds nothing.
        search_artist = "" if self.VARIOUS_RE.match(artist or "") else artist
        year, genre, definitive = self._search(search_artist, self._strip_edition(album))
        if genre is None and artist and not self.VARIOUS_RE.match(artist):
            # The album may be a compilation MusicBrainz has never seen, but the
            # artist usually exists and carries genre tags of their own.
            genre = self._artist_genre(artist)
        # Only remember a real answer. A throttled or failed request must stay
        # retryable, or one 503 would blank an album for the whole session.
        if definitive:
            self.cache[key] = (year, genre)
            self.hits += 1
        else:
            self.failures += 1
            if not self.warned:
                self.warned = True
                log("  ! MusicBrainz unreachable or rate-limiting; year/genre "
                    "may be left blank (fill them in later with --backfill)")
        return year, genre

    @classmethod
    def _strip_edition(cls, album):
        cleaned = cls.TRAILING_RE.sub("", cls.EDITION_RE.sub("", album)).strip()
        return cleaned or album

    def _search(self, artist, album):
        query = f'release:"{_lucene(album)}"'
        if artist:
            query = f'artist:"{_lucene(artist)}" AND ' + query
        url = self.ENDPOINT + "?" + urllib.parse.urlencode(
            {"query": query, "limit": 1, "fmt": "json"})

        for attempt in range(len(self.retry_delays) + 1):
            wait = self.interval - (time.monotonic() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.ua})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.load(r)
                year, genre = self._best(data)
                return year, genre, True
            except urllib.error.HTTPError as e:
                if e.code in (503, 429) and attempt < len(self.retry_delays):
                    retry_after = 0.0
                    try:                      # honour the server's own advice
                        retry_after = float(e.headers.get("Retry-After") or 0)
                    except (TypeError, ValueError):
                        retry_after = 0.0
                    time.sleep(max(retry_after, self.retry_delays[attempt]))
                    continue
                return None, None, False
            except (urllib.error.URLError, OSError, ValueError,
                    json.JSONDecodeError, KeyError, TimeoutError):
                return None, None, False  # enrichment is best-effort, never fatal
        return None, None, False

    def _artist_genre(self, artist):
        key = artist.lower()
        if key in self.artist_cache:
            return self.artist_cache[key]
        genre = None
        url = ("https://musicbrainz.org/ws/2/artist?" + urllib.parse.urlencode(
            {"query": f'artist:"{_lucene(artist)}"', "limit": 1, "fmt": "json"}))
        for attempt in range(len(self.retry_delays) + 1):
            wait = self.interval - (time.monotonic() - self.last_call)
            if wait > 0:
                time.sleep(wait)
            self.last_call = time.monotonic()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": self.ua})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.load(r)
                artists = data.get("artists") or []
                if artists and _as_int(artists[0].get("score")) >= 90:
                    tags = sorted((artists[0].get("tags") or []),
                                  key=lambda t: -_as_int(t.get("count")))
                    if tags:
                        genre = tags[0]["name"].title()
                break
            except urllib.error.HTTPError as e:
                if e.code in (503, 429) and attempt < len(self.retry_delays):
                    time.sleep(self.retry_delays[attempt])
                    continue
                break
            except (urllib.error.URLError, OSError, ValueError,
                    json.JSONDecodeError, KeyError, TimeoutError):
                break
        self.artist_cache[key] = genre
        return genre

    @staticmethod
    def _best(data):
        groups = data.get("release-groups") or []
        if not groups or _as_int(groups[0].get("score")) < 80:
            return (None, None)
        rg = groups[0]
        date = str(rg.get("first-release-date") or "")
        year = date[:4] if len(date) >= 4 and date[:4].isdigit() else None
        tags = sorted((rg.get("tags") or []), key=lambda t: -_as_int(t.get("count")))
        return (year, tags[0]["name"].title() if tags else None)


def _lucene(text):
    """Escape the Lucene syntax MusicBrainz search uses."""
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", text)


# --------------------------------------------------------------------------
# Output files
# --------------------------------------------------------------------------

def sanitize(s, strip_diacritics=False):
    s = s or ""
    if strip_diacritics:
        s = "".join(c for c in unicodedata.normalize("NFKD", s)
                    if not unicodedata.combining(c))
    s = s.replace("/", "-").replace("\0", "")
    s = re.sub(r'[<>:"\\|?*]', "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s or "Unknown"


def build_path(base, template, track, ext, strip_diacritics):
    fields = {
        "artist": sanitize(track.artist, strip_diacritics),
        "albumartist": sanitize(track.albumartist, strip_diacritics),
        "album": sanitize(track.album, strip_diacritics),
        "title": sanitize(track.title, strip_diacritics),
        "tracknum": f"{track.tracknum:02d}",
        "disc": f"{track.discnum}",
    }
    try:
        rel = template.format(**fields)
    except (KeyError, IndexError, ValueError):
        rel = f"{fields['artist']} - {fields['title']}"
    parts = [sanitize(p, strip_diacritics) for p in rel.split("/") if p.strip()]
    return os.path.join(base, *parts) + "." + ext


def unique_path(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem} ({n}){ext}"):
        n += 1
    return f"{stem} ({n}){ext}"


def fetch_art(url, cache):
    if not url:
        return None
    if url in cache:
        return cache[url]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "spytify-linux"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = r.read()
        fd, path = tempfile.mkstemp(suffix=".jpg", prefix="spytify-art-")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        cache[url] = path
        while len(cache) > 24:                     # bounded, delete oldest
            _, old = cache.popitem(last=False)
            if old:
                try:
                    os.unlink(old)
                except OSError:
                    pass
        return path
    except (urllib.error.URLError, OSError, ValueError):
        cache[url] = None
        return None


class Writer:
    """An ffmpeg process encoding one track to a temp file."""

    def __init__(self, track, path, args, bitrate, art_path, base=".",
                 year=None, genre=None):
        self.track = track
        self.path = path
        self.base = base
        self.bytes_written = 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.tmp = path + ".part"

        codec, muxer, supports_art = args
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "s16le", "-ar", str(RATE), "-ac", str(CHANNELS), "-i", "pipe:0"]
        use_art = supports_art and art_path
        if use_art:
            cmd += ["-i", art_path]
        cmd += list(codec)
        if bitrate and codec[codec.index("-c:a") + 1] not in LOSSLESS_CODECS:
            cmd += ["-b:a", f"{bitrate}k"]
        if muxer == "mp3":
            cmd += ["-id3v2_version", "3"]
        if use_art:
            cmd += ["-map", "0:a", "-map", "1:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic",
                    "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
        for key, val in (("title", track.title), ("artist", track.artist),
                         ("album", track.album), ("album_artist", track.albumartist)):
            if val:
                cmd += ["-metadata", f"{key}={val}"]
        if track.tracknum:
            cmd += ["-metadata", f"track={track.tracknum}"]
        if track.discnum:
            cmd += ["-metadata", f"disc={track.discnum}"]
        if year:
            cmd += ["-metadata", f"date={year}"]
        if genre:
            cmd += ["-metadata", f"genre={genre}"]
        if track.url:
            cmd += ["-metadata", f"comment={track.url}"]
        cmd += ["-f", muxer, self.tmp]

        # start_new_session keeps ffmpeg out of the terminal's process group, so
        # Ctrl-C reaches only us and the final track still gets encoded.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     start_new_session=True)

    def write(self, data):
        if not data or self.proc.stdin is None:
            return
        try:
            self.proc.stdin.write(data)
            self.bytes_written += len(data)
        except (BrokenPipeError, OSError, ValueError):
            pass

    @property
    def duration(self):
        return self.bytes_written / BPS

    def finish(self, min_duration):
        # communicate() closes stdin and drains stderr; a plain wait() can
        # deadlock once ffmpeg fills the stderr pipe.
        try:
            _, err_bytes = self.proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _, err_bytes = self.proc.communicate()

        dur = self.duration
        if self.proc.returncode != 0 or not os.path.exists(self.tmp):
            err = (err_bytes or b"").decode(errors="replace").strip()
            log(f"  ! encode failed: {err[:200]}")
            self._discard()
            return None
        if dur < min_duration:
            log(f"  - discarded {self._label(self.path)} ({dur:.1f}s < {min_duration}s)")
            self._discard()
            return None
        final = unique_path(self.path)
        os.replace(self.tmp, final)
        log(f"  = saved {self._label(final)} ({dur:.0f}s)")
        return final

    def _label(self, path):
        try:
            return os.path.relpath(path, self.base)
        except ValueError:
            return path

    def _discard(self):
        try:
            os.unlink(self.tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

CABLE_NAME = "SpytifyCable"


class VirtualCable:
    """A virtual sound card for Spotify alone.

    Spotify is routed into a null sink, so it makes no sound on your speakers
    and cannot interfere with anything else you are listening to. The
    recording is unaffected: it taps Spotify's stream directly, not the sink.
    """

    def __init__(self, enabled):
        self.enabled = enabled
        self.node_id = None
        self.routed = set()

    @staticmethod
    def _find():
        try:
            out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=6)
            for o in json.loads(out.stdout):
                if o.get("type") != "PipeWire:Interface:Node":
                    continue
                p = (o.get("info") or {}).get("props") or {}
                if p.get("node.name") == CABLE_NAME and p.get("media.class") == "Audio/Sink":
                    return o.get("id")
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
            pass
        return None

    @staticmethod
    def _destroy(node_id):
        try:
            subprocess.run(["pw-cli", "destroy", str(node_id)],
                           capture_output=True, timeout=5)
        except (subprocess.SubprocessError, OSError):
            pass

    def start(self):
        if not self.enabled:
            return
        stale = self._find()          # left over from a run that was killed
        if stale is not None:
            self._destroy(stale)
            time.sleep(0.3)
        props = (f'{{ factory.name=support.null-audio-sink node.name={CABLE_NAME} '
                 f'node.description="Spytify Cable" media.class=Audio/Sink '
                 f'object.linger=true audio.position=[FL FR] }}')
        try:
            subprocess.run(["pw-cli", "create-node", "adapter", props],
                           capture_output=True, timeout=6)
        except (subprocess.SubprocessError, OSError):
            self.enabled = False
            return
        for _ in range(20):           # the node appears asynchronously
            time.sleep(0.15)
            self.node_id = self._find()
            if self.node_id is not None:
                break
        if self.node_id is None:
            log("! could not create the virtual cable; Spotify will stay on your speakers")
            self.enabled = False
        else:
            log(f"virtual cable ready - Spotify plays into '{CABLE_NAME}', "
                f"silently, and will not touch your speakers")

    def route(self, spotify_node_id):
        """Point one Spotify stream at the cable. Spotify makes a new node
        every time playback resumes, so this runs on each capture restart."""
        if not self.enabled or self.node_id is None or spotify_node_id is None:
            return
        if spotify_node_id in self.routed:
            return
        try:
            subprocess.run(["pw-metadata", str(spotify_node_id),
                            "target.object", CABLE_NAME],
                           capture_output=True, timeout=5)
            self.routed.add(spotify_node_id)
        except (subprocess.SubprocessError, OSError):
            pass

    def stop(self):
        for node_id in self.routed:   # hand Spotify back to the speakers
            try:
                subprocess.run(["pw-metadata", "-d", str(node_id), "target.object"],
                               capture_output=True, timeout=5)
            except (subprocess.SubprocessError, OSError):
                pass
        self.routed.clear()
        if self.node_id is not None:
            self._destroy(self.node_id)
            log("virtual cable removed; Spotify is back on your normal output")
            self.node_id = None


class Recorder:
    def __init__(self, args):
        self.a = args
        self.mpris = Mpris(args.player)
        self.fmt = FORMATS[args.format]
        self.art_cache = OrderedDict()
        self.mb = MusicBrainz(not args.no_enrich, args.contact)
        self.capture = None
        self.writer = None
        self.pending = bytearray()
        self.current_id = None
        self.running = True
        self.saved = 0
        self.armed = not args.start_at   # when False, wait for the anchor track
        self.lag_bytes = int(LAG_SECONDS * BPS)
        self.original_volume = None
        self.cable = VirtualCable(not args.speakers)

    def ensure_volume(self):
        """Spotify's volume slider scales its output, so a slider at 40%
        records at 40%. Setting it through MPRIS sticks; setting the PipeWire
        node volume does not - Spotify re-applies its own value on resume."""
        if self.a.no_force_volume:
            return
        current = self.mpris.volume()
        if current >= 0.999:
            return
        if self.original_volume is None:
            self.original_volume = current
        if self.mpris.set_volume(1.0):
            log(f"! Spotify's volume was {current:.2f} - raised to 1.00 for "
                f"recording (restored on exit; --no-force-volume to leave it)")

    def restore_volume(self):
        if self.original_volume is not None:
            self.mpris.set_volume(self.original_volume)
            log(f"restored Spotify's volume to {self.original_volume:.2f}")

    def stop(self, *_):
        self.running = False

    # -- capture supervision -------------------------------------------------

    def ensure_capture(self):
        if self.capture and self.capture.alive():
            return True
        if self.capture:
            self.capture.close()
            self.capture = None
        serial, node_id = find_stream(self.a.app)
        if serial is None:
            return False
        self.cable.route(node_id)
        self.ensure_volume()
        self.capture = Capture(serial)
        log(f"capturing PipeWire stream (serial {serial})")
        return True

    # -- track boundaries ----------------------------------------------------

    def open_writer(self, track):
        path = build_path(self.a.output, self.a.template, track,
                          self.a.format, self.a.ascii)
        if self.a.skip_existing and os.path.exists(path):
            log(f"~ skipping (already have) {os.path.relpath(path)}")
            return None
        self.ensure_volume()
        art = None if self.a.no_art else fetch_art(track.arturl, self.art_cache)
        year, genre = self.mb.lookup(track.albumartist, track.album)
        extra = " ".join(x for x in (year, genre) if x)
        log(f"> {track}" + (f"  [{extra}]" if extra else ""))
        return Writer(track, path, self.fmt, self.a.bitrate, art,
                      base=self.a.output, year=year, genre=genre)

    def close_writer(self):
        if not self.writer:
            return
        saved = self.writer.finish(self.a.min_duration)
        self.writer = None
        if not saved:
            return
        self.saved += 1
        if self.a.max_tracks:
            log(f"  ({self.saved} of {self.a.max_tracks})")
            if self.saved >= self.a.max_tracks:
                log(f"reached {self.a.max_tracks} tracks - stopping")
                self.running = False

    def flush(self, force=False):
        """Send everything except the trailing LAG_SECONDS to the encoder."""
        keep = 0 if force else self.lag_bytes
        if len(self.pending) > keep:
            cut = len(self.pending) - keep
            if self.writer:
                self.writer.write(bytes(self.pending[:cut]))
            del self.pending[:cut]

    def boundary(self, new_track, position):
        """Split `pending` at the point the new track actually began."""
        # position tells us how late we noticed; recover those bytes for the
        # new track instead of leaving them on the end of the old one.
        overshoot = min(int(position * BPS), len(self.pending))
        overshoot -= overshoot % (CHANNELS * SAMPLE_BYTES)     # keep frame alignment
        split = len(self.pending) - overshoot

        if self.writer:
            head = bytes(self.pending[:split])
            if not self.a.no_trim_silence:
                trimmed = trim_trailing_silence(head, self.lag_bytes)
                if len(trimmed) != len(head):
                    log(f"  . trimmed {(len(head) - len(trimmed)) / BPS:.1f}s of trailing silence")
                head = trimmed
            self.writer.write(head)
        tail = bytes(self.pending[split:])
        self.pending.clear()
        self.close_writer()

        if new_track is None or not self.running:
            return
        self.writer = self.open_writer(new_track)
        if self.writer and tail:
            self.writer.write(tail)

    # -- run -----------------------------------------------------------------

    def run(self):
        if not self.mpris.alive():
            log(f"! no MPRIS player named {self.a.player} - is Spotify running?")
            return 1
        self.cable.start()
        log(f"watching {self.a.player}; writing {self.a.format} to {self.a.output}")
        if self.a.start_at:
            log(f"waiting for a track titled like '{self.a.start_at}' before recording")

        last_poll = 0.0
        last_capture_try = 0.0

        while self.running:
            now = time.monotonic()

            if self.capture is None or not self.capture.alive():
                if now - last_capture_try > 1.0:
                    last_capture_try = now
                    self.ensure_capture()
                if self.capture is None:
                    # Spotify's node disappears whenever playback stops; wait
                    # for it to come back, and keep polling for track changes.
                    self.flush(force=True)
                    if now - last_poll >= self.a.poll:
                        last_poll = now
                        self.poll_track()
                    time.sleep(0.2)
                    continue

            try:
                data = self.capture.q.get(timeout=0.5)
            except queue.Empty:
                data = b""
            if data is None:                      # pw-record ended
                self.capture.close()
                self.capture = None
                continue
            if data:
                self.pending.extend(data)

            if now - last_poll >= self.a.poll:
                last_poll = now
                self.poll_track()

            self.flush()

        log("stopping...")
        if self.mb.failures:
            log(f"! MusicBrainz failed for {self.mb.failures} album lookup(s); "
                f"run  spytify.py --backfill {self.a.output}  to fill those in")
        self.flush(force=True)
        self.close_writer()
        self.restore_volume()
        self.cable.stop()
        if self.capture:
            self.capture.close()
        for p in self.art_cache.values():
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
        return 0

    def poll_track(self):
        track = self.mpris.track()
        if track is None or track.is_incomplete:
            # Don't commit to a decision on a half-published update - waiting
            # one more poll avoids mistaking a track change for an advert.
            return
        if track.trackid == self.current_id:
            return

        position = self.mpris.position()
        first_seen = self.current_id is None
        self.current_id = track.trackid

        # Wait for the track the user named before recording anything, so a run
        # starts at a known point in their list rather than wherever the queue
        # happens to be.
        if not self.armed:
            needle = self.a.start_at.casefold()
            if needle in track.title.casefold():
                self.armed = True
                log(f"found start track '{track}' - recording from here")
            else:
                log(f"  . waiting for '{self.a.start_at}' (now: {track})")
                self.boundary(None, position)
                return

        if track.is_ad and self.a.skip_ads:
            log("  (advertisement - not recording)")
            self.boundary(None, position)
            return

        # A real track change is noticed within a poll interval, so a large
        # position means we joined mid-song (started up, or resumed a seek) and
        # would only ever produce a truncated file.
        if position > self.a.max_lateness and not self.a.keep_partial:
            log(f"  ~ joined '{track}' {position:.0f}s in - waiting for the next track")
            self.boundary(None, position)
            return

        if first_seen and self.mpris.status() != "Playing":
            return
        self.boundary(track, position)


AUDIO_EXTS = tuple("." + e for e in FORMATS)


def read_tags(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                              "-show_format", path], capture_output=True, text=True, timeout=20)
        fmt = (json.loads(out.stdout) or {}).get("format") or {}
        return {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return {}


def retag(path, year, genre):
    """Rewrite tags without re-encoding; every stream, art included, is copied."""
    tmp = path + ".retag" + os.path.splitext(path)[1]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", path,
           "-map", "0", "-c", "copy", "-map_metadata", "0"]
    if year:
        cmd += ["-metadata", f"date={year}"]
    if genre:
        cmd += ["-metadata", f"genre={genre}"]
    cmd += [tmp]
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=120)
        if done.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
            return True
        return False
    except (subprocess.SubprocessError, OSError):
        return False
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def backfill(root, contact):
    """Fill in year/genre on files already recorded, without touching audio."""
    files = []
    for base, _dirs, names in os.walk(root):
        files += [os.path.join(base, n) for n in sorted(names)
                  if n.lower().endswith(AUDIO_EXTS)]
    if not files:
        log(f"no audio files under {root}")
        return 0

    mb = MusicBrainz(True, contact, bulk=True)
    todo = []
    for path in files:
        tags = read_tags(path)
        if tags.get("date") and tags.get("genre"):
            continue
        todo.append((path, tags))
    log(f"{len(files)} files, {len(todo)} missing year or genre")

    fixed = unchanged = 0
    for path, tags in todo:
        artist = tags.get("album_artist") or tags.get("artist") or ""
        album = tags.get("album") or ""
        year, genre = mb.lookup(artist, album)
        year = year if not tags.get("date") else None
        genre = genre if not tags.get("genre") else None
        if not year and not genre:
            unchanged += 1
            log(f"  ? {os.path.relpath(path, root)} - nothing found for {album!r}")
            continue
        if retag(path, year, genre):
            fixed += 1
            got = " ".join(x for x in (year, genre) if x)
            log(f"  + {os.path.relpath(path, root)}  [{got}]")
        else:
            unchanged += 1
            log(f"  ! failed to retag {os.path.relpath(path, root)}")
    log(f"done: {fixed} updated, {unchanged} still missing "
        f"({mb.failures} lookup(s) blocked by rate limiting - re-run to retry those)")
    return 0


def main():
    p = argparse.ArgumentParser(
        prog="spytify-linux",
        description="Record Spotify into tagged audio files on Linux (PipeWire + MPRIS).")
    p.add_argument("-o", "--output", default=os.path.expanduser("~/Music/Spytify"),
                   help="output directory (default: ~/Music/Spytify)")
    p.add_argument("-f", "--format", default="mp3", choices=sorted(FORMATS),
                   help="output format (default: mp3)")
    p.add_argument("-b", "--bitrate", type=int, default=320,
                   help="bitrate in kbps for lossy formats (default: 320)")
    p.add_argument("--template", default="{albumartist}/{album}/{tracknum} - {title}",
                   help="path template; fields: artist albumartist album title tracknum disc")
    p.add_argument("--player", default="org.mpris.MediaPlayer2.spotify",
                   help="MPRIS bus name to watch")
    p.add_argument("--app", default="spotify",
                   help="substring matching the PipeWire stream to capture")
    p.add_argument("--poll", type=float, default=0.25,
                   help="seconds between MPRIS polls (default: 0.25)")
    p.add_argument("--start-at", metavar="TITLE", default=None,
                   help="ignore tracks until one whose title contains TITLE "
                        "plays, then record from there")
    p.add_argument("-n", "--max-tracks", type=int, default=0,
                   help="stop automatically after this many tracks are saved")
    p.add_argument("--min-duration", type=float, default=30.0,
                   help="discard recordings shorter than this many seconds (default: 30)")
    p.add_argument("--max-lateness", type=float, default=5.0,
                   help="skip a track if we join it more than this many seconds in (default: 5)")
    p.add_argument("--keep-partial", action="store_true",
                   help="record tracks even when joined part-way through")
    p.add_argument("--no-force-volume", action="store_true",
                   help="don't raise Spotify's own volume to 100%% (it scales the recording)")
    p.add_argument("--speakers", action="store_true",
                   help="play Spotify through your normal output instead of the "
                        "virtual cable (you will hear it alongside everything else)")
    p.add_argument("--no-art", action="store_true", help="do not embed cover art")
    p.add_argument("--no-trim-silence", action="store_true",
                   help="keep the silent gap Spotify leaves at the end of a track")
    p.add_argument("--backfill", metavar="DIR", default=None,
                   help="don't record; fill in missing year/genre on files already "
                        "in DIR (audio is copied, never re-encoded)")
    p.add_argument("--no-enrich", action="store_true",
                   help="do not look up year/genre from MusicBrainz")
    p.add_argument("--contact", default=None,
                   help="contact address to put in the MusicBrainz User-Agent "
                        "(optional; nothing personal is sent without it)")
    p.add_argument("--ascii", action="store_true", help="strip accents from filenames")
    p.add_argument("--overwrite", dest="skip_existing", action="store_false",
                   help="re-record tracks that already exist on disk")
    p.add_argument("--keep-ads", dest="skip_ads", action="store_false",
                   help="record advertisements too")
    a = p.parse_args()

    if a.backfill:
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            print("error: ffmpeg/ffprobe not found in PATH", file=sys.stderr)
            return 1
        return backfill(a.backfill, a.contact)

    for tool in ("pw-record", "pw-dump", "busctl", "ffmpeg"):
        if shutil.which(tool) is None:
            print(f"error: required program '{tool}' not found in PATH", file=sys.stderr)
            return 1

    rec = Recorder(a)
    signal.signal(signal.SIGINT, rec.stop)
    signal.signal(signal.SIGTERM, rec.stop)
    try:
        return rec.run()
    finally:
        rec.cable.stop()          # never leave Spotify stranded on the cable


if __name__ == "__main__":
    sys.exit(main())
