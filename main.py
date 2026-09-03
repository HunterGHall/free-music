import os
import time
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox

import yt_dlp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR = os.path.join(BASE_DIR, "music")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac")


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


class MusicPlayer:
    def __init__(self, root):
        self.root = root
        self.root.title("Free Music")
        self.root.geometry("460x270")
        self.root.resizable(False, False)

        self.tracks = find_tracks()
        self.index = 0
        self.repeat = False

        # playback state
        self.state = "stopped"       # stopped | playing | paused
        self.proc = None             # ffplay process
        self.pos = 0.0               # resume position in seconds
        self.play_start = 0.0        # monotonic time mapping to pos 0
        self.duration = None         # current track length in seconds
        self.generation = 0          # bumped on every deliberate stop/switch

        # ----- download row -----
        dl = tk.Frame(root)
        dl.pack(fill="x", padx=12, pady=(14, 6))
        tk.Label(dl, text="URL:").pack(side="left")
        self.url_var = tk.StringVar()
        entry = tk.Entry(dl, textvariable=self.url_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self.download())
        self.dl_btn = tk.Button(dl, text="Download", width=10, command=self.download)
        self.dl_btn.pack(side="left")

        # ----- now playing -----
        self.now_playing = tk.StringVar(value=self._track_label())
        tk.Label(root, textvariable=self.now_playing, wraplength=430,
                 font=("Segoe UI", 11)).pack(pady=(14, 4))

        self.status = tk.StringVar(value="Stopped")
        tk.Label(root, textvariable=self.status, fg="gray").pack()

        # ----- controls -----
        btns = tk.Frame(root)
        btns.pack(pady=16)
        self.play_btn = tk.Button(btns, text="Play", width=9, command=self.toggle_play)
        self.play_btn.grid(row=0, column=0, padx=3)
        tk.Button(btns, text="Stop", width=7, command=self.stop).grid(
            row=0, column=1, padx=3)
        tk.Button(btns, text="Next Song", width=10, command=self.next_song).grid(
            row=0, column=2, padx=3)
        self.repeat_btn = tk.Button(btns, text="Repeat: Off", width=11,
                                    command=self.toggle_repeat)
        self.repeat_btn.grid(row=0, column=3, padx=3)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        if not self.tracks:
            self.status.set("music/ is empty - paste a URL and click Download")

    # ---------------- helpers ----------------
    def _track_label(self):
        if not self.tracks:
            return "No tracks in music/"
        return f"{self.index + 1}/{len(self.tracks)}  -  " \
               f"{os.path.basename(self.tracks[self.index])}"

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
            self.duration = _ffprobe_duration(path)
        try:
            self.proc = subprocess.Popen(
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
                 "-ss", str(self.pos), path])
        except FileNotFoundError:
            messagebox.showerror(
                "ffplay not found",
                "ffplay (part of ffmpeg) must be installed and on PATH.")
            return
        self.play_start = time.monotonic() - self.pos
        self.state = "playing"
        self.play_btn.config(text="Pause")
        self.status.set("Playing")
        threading.Thread(target=self._watch, args=(self.proc, gen),
                         daemon=True).start()

    def _watch(self, proc, gen):
        proc.wait()
        self.root.after(0, self._on_track_end, gen)

    def _on_track_end(self, gen):
        # Deliberate stop/pause/skip bumps generation; this only runs on a
        # track that played through to the end.
        if gen != self.generation:
            return
        self.proc = None
        self.duration = None
        if self.repeat:
            self._play_from(0)
        else:
            self.index = (self.index + 1) % len(self.tracks)
            self.now_playing.set(self._track_label())
            self._play_from(0)

    # ---------------- button actions ----------------
    def toggle_play(self):
        if not self.tracks:
            return
        if self.state == "playing":
            elapsed = time.monotonic() - self.play_start
            self._kill()
            self.pos = max(0.0, elapsed)
            self.state = "paused"
            self.play_btn.config(text="Play")
            self.status.set(f"Paused at {self._fmt(self.pos)}")
        elif self.state == "paused":
            self._play_from(self.pos)
        else:
            self.duration = None
            self._play_from(0)

    def stop(self):
        self._kill()
        self.pos = 0.0
        self.duration = None
        self.state = "stopped"
        self.play_btn.config(text="Play")
        self.status.set("Stopped")

    def next_song(self):
        if not self.tracks:
            return
        was_active = self.state in ("playing", "paused")
        self._kill()
        self.pos = 0.0
        self.duration = None
        self.index = (self.index + 1) % len(self.tracks)
        self.now_playing.set(self._track_label())
        if was_active:
            self._play_from(0)
        else:
            self.state = "stopped"
            self.play_btn.config(text="Play")
            self.status.set("Stopped")

    def toggle_repeat(self):
        self.repeat = not self.repeat
        self.repeat_btn.config(text=f"Repeat: {'On' if self.repeat else 'Off'}")

    # ---------------- download ----------------
    def download(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo("Download", "Paste a YouTube (or other) URL first.")
            return
        self.dl_btn.config(state="disabled")
        self.status.set("Downloading...")
        threading.Thread(target=self._download_worker, args=(url,),
                         daemon=True).start()

    def _download_worker(self, url):
        os.makedirs(MUSIC_DIR, exist_ok=True)

        def hook(d):
            if d["status"] == "downloading":
                pct = d.get("_percent_str", "").strip()
                self.root.after(0, lambda: self.status.set(f"Downloading {pct}"))
            elif d["status"] == "finished":
                self.root.after(0, lambda: self.status.set("Converting to mp3..."))

        ydl_opts = {
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
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
            title = info.get("title", "track")
            self.root.after(0, self._download_done, title, None)
        except Exception as e:
            self.root.after(0, self._download_done, None, str(e))

    def _download_done(self, title, err):
        self.dl_btn.config(state="normal")
        if err:
            self.status.set("Download failed")
            messagebox.showerror("Download failed", err)
            return
        self.url_var.set("")
        was_empty = not self.tracks
        self.tracks = find_tracks()
        if was_empty:
            self.index = 0
        self.now_playing.set(self._track_label())
        self.status.set(f"Downloaded: {title}")

    # ---------------- misc ----------------
    @staticmethod
    def _fmt(sec):
        sec = int(sec)
        return f"{sec // 60}:{sec % 60:02d}"

    def on_close(self):
        self._kill()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    MusicPlayer(root)
    root.mainloop()
