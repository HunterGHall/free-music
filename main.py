import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import yt_dlp

import pane  # importing this runs pane's CLR bootstrap - must happen before any System.* import
from System.Windows import MessageBox

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR = os.path.join(BASE_DIR, "music")
COVERS_DIR = os.path.join(MUSIC_DIR, "covers")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


# ---------------- library scanning ----------------

def find_tracks():
    """All playable files in music/ (created if missing)."""
    os.makedirs(MUSIC_DIR, exist_ok=True)
    return [
        os.path.join(MUSIC_DIR, name)
        for name in sorted(os.listdir(MUSIC_DIR))
        if name.lower().endswith(AUDIO_EXTS)
    ]


def _ffprobe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


_duration_cache = {}  # (path, mtime) -> seconds, so a rescan doesn't re-probe unchanged files


def _track_duration(path):
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return 0
    if key not in _duration_cache:
        _duration_cache[key] = _ffprobe_duration(path) or 0
    return _duration_cache[key]


def _track_fields(path):
    """(title, artist, duration) for a playlist row - artist comes from an
    "Artist - Title.ext" filename, same convention download_spotify()/
    download_youtube() below save under; anything else is shown as a plain
    title with no artist."""
    name = os.path.splitext(os.path.basename(path))[0]
    if " - " in name:
        artist, title = name.split(" - ", 1)
    else:
        artist, title = "", name
    return title, artist, _track_duration(path)


def _cover_path_for(audio_path):
    name = os.path.splitext(os.path.basename(audio_path))[0]
    return os.path.join(COVERS_DIR, name + ".jpg")


def _cover_for(audio_path):
    cover = _cover_path_for(audio_path)
    return cover if os.path.isfile(cover) else None


def _delete_track_files(path):
    try:
        os.remove(path)
    except OSError:
        pass
    cover = _cover_path_for(path)
    if os.path.isfile(cover):
        try:
            os.remove(cover)
        except OSError:
            pass


def _sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip() or "track"


# ---------------- downloading ----------------

def is_spotify_url(url):
    return "spotify.com" in url or url.startswith("spotify:")


def _download_image(url, dest_path):
    """Best-effort - cover art is a nice-to-have, never fails the download."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        with open(dest_path, "wb") as f:
            f.write(data)
    except Exception:
        pass


def _fetch_spotify_metadata(url):
    """(title, artist, cover_url) for a Spotify track link, via Spotify's
    public oEmbed endpoint (no API credentials needed) plus a light scrape
    of the embed page for the artist name, which oEmbed alone doesn't
    include. artist is "" if it can't be found."""
    oembed_url = "https://open.spotify.com/oembed?url=" + urllib.parse.quote(url, safe="")
    req = urllib.request.Request(oembed_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    title = data.get("title", "")
    cover_url = data.get("thumbnail_url")

    artist = ""
    match = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]+)", url)
    if match:
        try:
            embed_req = urllib.request.Request(
                f"https://open.spotify.com/embed/track/{match.group(1)}",
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(embed_req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
            artist_match = re.search(r'"artists":\[\{"name":"([^"]+)"', html)
            if artist_match:
                artist = artist_match.group(1)
        except Exception:
            pass
    return title, artist, cover_url


def _final_path(ydl, info):
    """The actual post-processed (mp3) file path for a yt-dlp download."""
    downloads = info.get("requested_downloads")
    if downloads:
        return downloads[-1]["filepath"]
    base, _ext = os.path.splitext(ydl.prepare_filename(info))
    return base + ".mp3"


def _ydl_opts(hook, **extra):
    return {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(MUSIC_DIR, "%(title)s.%(ext)s"),
        "progress_hooks": [hook],
        "quiet": True,
        "no_warnings": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        **extra,
    }


def download_youtube(url, hook):
    """Downloads a YouTube (or any other yt-dlp-supported site) URL as mp3,
    grabbing its own thumbnail as cover art - no Spotify involved. Returns
    the track title."""
    with yt_dlp.YoutubeDL(_ydl_opts(hook)) as ydl:
        info = ydl.extract_info(url, download=True)
    path = _final_path(ydl, info)
    thumb = info.get("thumbnail")
    if thumb:
        _download_image(thumb, _cover_path_for(path))
    return info.get("title") or os.path.splitext(os.path.basename(path))[0]


def download_spotify(url, hook):
    """Spotify has no downloadable audio, only metadata + cover art: reads
    the title/artist/cover from Spotify, then finds and downloads the
    matching audio from YouTube. Returns "Artist - Title"."""
    title, artist, cover_url = _fetch_spotify_metadata(url)
    if not title:
        raise RuntimeError("Could not read that Spotify link - check it's a track URL.")

    query = f"{artist} {title} audio".strip() if artist else f"{title} audio"
    with yt_dlp.YoutubeDL(_ydl_opts(hook, noplaylist=True)) as ydl:
        result = ydl.extract_info(f"ytsearch1:{query}", download=True)
    entries = result.get("entries") if isinstance(result, dict) else None
    if entries is not None:
        if not entries:
            raise RuntimeError(f'No YouTube match found for "{query}"')
        entry = entries[0]
    else:
        entry = result
    downloaded = _final_path(ydl, entry)

    display_name = f"{artist} - {title}" if artist else title
    final_path = os.path.join(MUSIC_DIR, _sanitize_filename(display_name) + ".mp3")
    if os.path.abspath(downloaded) != os.path.abspath(final_path):
        if os.path.exists(final_path):
            os.remove(final_path)
        os.rename(downloaded, final_path)

    if cover_url:
        _download_image(cover_url, _cover_path_for(final_path))
    return display_name


# ---------------- app ----------------

class FreeMusicApp:
    def __init__(self, window):
        self.tracks = find_tracks()
        self.index = 0
        self.state = "stopped"       # stopped | playing | paused
        self.proc = None             # ffplay process
        self.pos = 0.0                # resume position in seconds
        self.play_start = 0.0        # monotonic time mapping to pos 0
        self.duration = None          # current track length in seconds
        self.generation = 0          # bumped on every deliberate stop/switch
        self.volume = 70

        self.url_box = pane.text_box(placeholder="Paste a YouTube or Spotify link...")
        self._wire_enter_to_download(self.url_box)
        self.download_btn = pane.button(
            "Download", style="primary", on_click=self.download, margin=(8, 0, 0, 0))
        download_row = pane.grid(
            [(0, 0, self.url_box), (0, 1, self.download_btn)],
            columns=["*", "auto"],
        )

        self.status = pane.text("", color="secondary")

        self.player = pane.music_player(
            on_play_pause=self.on_play_pause,
            on_previous=self.on_previous,
            on_next=self.on_next,
            on_seek=self.on_seek,
            on_volume_change=self.on_volume_change,
            on_repeat_change=self.on_repeat_change,
            width=320,
        )
        self.playlist = pane.playlist(
            on_select=self.on_playlist_select,
            on_remove=self.on_playlist_remove,
            width=320,
            height=220,
        )

        self.root = pane.stack(
            download_row, self.status, self.player.control, self.playlist.control,
            spacing=14, margin=20,
        )
        window.Content = self.root
        window.Closing += self._on_window_closing

        self._rebuild_playlist_rows()
        if self.tracks:
            self._load_index(0, autoplay=False)
        else:
            self.player.set_track("No tracks in music/", duration=0)
            self._set_status("music/ is empty - paste a link and click Download")

        threading.Thread(target=self._ticker, daemon=True).start()

    def _wire_enter_to_download(self, box):
        from System.Windows.Input import Key

        def handler(sender, args):
            if args.Key == Key.Enter:
                self.download()

        box.KeyDown += handler

    def _on_window_closing(self, sender, args):
        self._kill()

    def _set_status(self, message):
        self.status.Text = message

    # ---------------- playlist bookkeeping ----------------
    def _rebuild_playlist_rows(self):
        # A 4th (path) element rides along unused by playlist()'s own
        # title/artist/duration display - see on_playlist_select() below,
        # which gets it back verbatim as the key to find the row again.
        rows = [(*_track_fields(p), p) for p in self.tracks]
        self.playlist.set_tracks(rows)
        self.playlist.set_current_index(self.index if self.tracks else -1)

    def _load_index(self, index, *, autoplay):
        if not self.tracks:
            return
        self.index = index % len(self.tracks)
        self.pos = 0.0
        path = self.tracks[self.index]
        title, artist, duration = _track_fields(path)
        self.duration = duration or None
        self.player.set_track(title, artist, duration=duration)
        self.player.set_artwork(_cover_for(path))
        self.playlist.set_current_index(self.index)
        if autoplay:
            self._play_from(0)
        else:
            self.state = "stopped"
            self.player.set_playing(False)
            self._set_status("Stopped")

    # ---------------- playback engine ----------------
    def _elapsed(self):
        if self.state == "playing":
            return max(0.0, time.monotonic() - self.play_start)
        return self.pos

    def _kill(self):
        """Stop ffplay without triggering auto-advance."""
        self.generation += 1
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def _play_from(self, offset):
        if not self.tracks:
            return
        self.generation += 1
        gen = self.generation
        path = self.tracks[self.index]
        self.pos = max(0.0, offset)
        if self.duration is None:
            self.duration = _track_duration(path)
        try:
            self.proc = subprocess.Popen([
                "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                "-volume", str(int(self.volume)),
                "-ss", str(self.pos), path,
            ])
        except FileNotFoundError:
            MessageBox.Show(
                "ffplay (part of ffmpeg) must be installed and on PATH.", "ffplay not found")
            return
        self.play_start = time.monotonic() - self.pos
        self.state = "playing"
        self.player.set_playing(True)
        self._set_status("Playing")
        threading.Thread(target=self._watch, args=(self.proc, gen), daemon=True).start()

    def _restart_at(self, offset):
        self._kill()
        self._play_from(offset)

    def _watch(self, proc, gen):
        proc.wait()
        pane.invoke(lambda: self._on_track_end(gen))

    def _on_track_end(self, gen):
        # Deliberate stop/pause/skip bumps generation; this only runs on a
        # track that played through to the end on its own.
        if gen != self.generation:
            return
        self.proc = None
        self.duration = None
        mode = self.player.repeat_mode()
        if mode == "one":
            self._play_from(0)
        elif mode == "all":
            self._load_index(self.index + 1, autoplay=True)
        elif self.index < len(self.tracks) - 1:  # "off", but more tracks left
            self._load_index(self.index + 1, autoplay=True)
        else:  # "off" and this was the last track - stop, don't wrap
            self.state = "stopped"
            self.player.set_playing(False)
            self._set_status("Stopped")

    def _ticker(self):
        """Reports real playback position to the seek slider once a second,
        for as long as the window is open."""
        while True:
            time.sleep(1)
            if self.state != "playing":
                continue
            elapsed = self._elapsed()
            if self.duration:
                elapsed = min(elapsed, self.duration)
            pane.invoke(lambda e=elapsed: self.player.set_position(e))

    # ---------------- music_player callbacks ----------------
    def on_play_pause(self, is_playing):
        if not self.tracks:
            self.player.set_playing(False)
            return
        if is_playing:
            self._play_from(self.pos)
        else:
            self.pos = self._elapsed()
            self._kill()
            self.state = "paused"
            self._set_status("Paused")

    def on_next(self):
        if not self.tracks:
            return
        was_active = self.state in ("playing", "paused")
        self._kill()
        self._load_index(self.index + 1, autoplay=was_active)

    def on_previous(self):
        if not self.tracks:
            return
        was_active = self.state in ("playing", "paused")
        self._kill()
        self._load_index(self.index - 1, autoplay=was_active)

    def on_seek(self, value):
        if not self.tracks:
            return
        if self.state == "playing":
            self._restart_at(value)
        else:
            self.pos = value

    def on_volume_change(self, value):
        self.volume = value
        if self.state == "playing":
            self._restart_at(self._elapsed())

    def on_repeat_change(self, mode):
        pass  # _on_track_end() reads player.repeat_mode() itself when a track finishes

    # ---------------- playlist callbacks ----------------
    def on_playlist_select(self, track):
        path = track[3]
        index = self.tracks.index(path)
        self._kill()
        self._load_index(index, autoplay=True)

    def on_playlist_remove(self, index):
        if not (0 <= index < len(self.tracks)):
            return
        path = self.tracks[index]
        is_current = index == self.index
        was_active = is_current and self.state in ("playing", "paused")
        if is_current:
            self._kill()
        _delete_track_files(path)
        del self.tracks[index]

        if not self.tracks:
            self.index = 0
            self.pos = 0.0
            self.duration = None
            self.state = "stopped"
            self.player.set_playing(False)
            self.player.set_track("No tracks in music/", duration=0)
            self.player.set_artwork(None)
            self._rebuild_playlist_rows()
            self._set_status("music/ is empty - paste a link and click Download")
            return

        if index < self.index:
            self.index -= 1
        else:
            self.index = min(self.index, len(self.tracks) - 1)

        self._rebuild_playlist_rows()
        if is_current:
            self._load_index(self.index, autoplay=was_active)

    # ---------------- download ----------------
    def download(self):
        url = self.url_box.Text.strip()
        if not url:
            self._set_status("Paste a YouTube or Spotify link first.")
            return
        self.download_btn.IsEnabled = False
        self._set_status("Downloading...")
        threading.Thread(target=self._download_worker, args=(url,), daemon=True).start()

    def _download_worker(self, url):
        os.makedirs(MUSIC_DIR, exist_ok=True)
        os.makedirs(COVERS_DIR, exist_ok=True)

        def hook(d):
            if d["status"] == "downloading":
                pct = d.get("_percent_str", "").strip()
                pane.invoke(lambda p=pct: self._set_status(f"Downloading {p}"))
            elif d["status"] == "finished":
                pane.invoke(lambda: self._set_status("Converting to mp3..."))

        try:
            if is_spotify_url(url):
                title = download_spotify(url, hook)
            else:
                title = download_youtube(url, hook)
            pane.invoke(lambda: self._download_done(title, None))
        except Exception as e:
            pane.invoke(lambda err=str(e): self._download_done(None, err))

    def _download_done(self, title, err):
        self.download_btn.IsEnabled = True
        if err:
            self._set_status("Download failed")
            MessageBox.Show(err, "Download failed")
            return
        self.url_box.Text = ""

        current_path = self.tracks[self.index] if self.tracks else None
        was_empty = current_path is None
        self.tracks = find_tracks()
        if current_path in self.tracks:
            self.index = self.tracks.index(current_path)  # a new file can shift alphabetical order
        self._rebuild_playlist_rows()
        if was_empty and self.tracks:
            self._load_index(0, autoplay=False)
        self._set_status(f"Downloaded: {title}")


def build(window):
    FreeMusicApp(window)


if __name__ == "__main__":
    pane.run(build, title="Free Music", width=380, height=680)
