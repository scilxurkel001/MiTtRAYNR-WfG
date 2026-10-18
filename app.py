import os
import sys
import platform
import ctypes
import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
import math
import threading
import sounddevice as sd
import soundfile as sf


def resource_path(rel_path: str) -> str:
    """Resolve a bundled resource path for both source runs and PyInstaller."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel_path)

# ============== Conversion between slider value and frequency ==============
def slider_to_freq(val: float) -> float:
    return 20.0 * (1000.0 ** (val / 1000.0))

def freq_to_slider(freq: float) -> int:
    freq = max(20.0, min(20000.0, freq))
    return int(1000.0 * math.log(freq / 20.0) / math.log(1000.0))

# ======================= High-DPI Support =======================
def setup_high_dpi():
    if platform.system() == 'Windows':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

def get_scale_factor():
    if platform.system() == 'Windows':
        try:
            return ctypes.windll.user32.GetDpiForSystem() / 96.0
        except Exception:
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
            ctypes.windll.user32.ReleaseDC(0, hdc)
            return dpi / 96.0
    try:
        return float(os.environ.get('GDK_SCALE', 1.0))
    except Exception:
        return 1.0

# ========================= Audio Engine =========================
class AudioEngine:
    SCOPE_BUF_SIZE = 8192  

    def __init__(self, sample_rate: int = 44100, blocksize: int = 1024):
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.phase = 0.0
        self.sweep_time = 0.0
        self.playing = False
        self.lock = threading.Lock()
        self.stream = None
        self.waveforms = ["sine"]
        self.mode = "tone"
        self.frequency = 440.0
        self.sweep_start = 200.0
        self.sweep_end = 2000.0
        self.sweep_duration = 2.0
        self.log_sweep = False
        self.volume = 0.5

        # Random frequency hopping
        self.hop_start = 200.0
        self.hop_end = 2000.0
        self.hop_interval = 1.0          # seconds between hops (min 0.1 s)
        self.hop_freq = 440.0            # currently active hop frequency
        self.hop_next_time = 0.0         # engine time for the next hop
        
        # Oscilloscope ring buffer
        self.scope_buffer = np.zeros(self.SCOPE_BUF_SIZE, dtype=np.float32)
        self.scope_lock = threading.Lock()
        self.scope_write_pos = 0

        # Audio track playback (e.g. the bundled demo track)
        self.track_data = None          # mono float32 samples
        self.track_pos = 0              # playback cursor (samples)
        self.track_name = ""
        self.track_samplerate = 0       # native sample rate of the track

    def load_track(self, path: str, name: str = "") -> None:
        """Decode an audio file into a mono float32 buffer for playback."""
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        if data.shape[1] > 1:
            data = data.mean(axis=1)
        else:
            data = data[:, 0]
        with self.lock:
            self.track_data = np.ascontiguousarray(data, dtype=np.float32)
            self.track_name = name or os.path.basename(path)
            self.track_samplerate = int(sr)
            self.track_pos = 0

    def _freq_at(self, t: float) -> float:
        progress = t / self.sweep_duration
        if self.log_sweep:
            return self.sweep_start * (self.sweep_end / self.sweep_start) ** progress
        return self.sweep_start + (self.sweep_end - self.sweep_start) * progress

    def _freqs_array(self, t_arr: np.ndarray) -> np.ndarray:
        progress = t_arr / self.sweep_duration
        if self.log_sweep:
            return self.sweep_start * (self.sweep_end / self.sweep_start) ** progress
        return self.sweep_start + (self.sweep_end - self.sweep_start) * progress

    @staticmethod
    def _wave_shape(name: str, phases: np.ndarray) -> np.ndarray:
        if name == "sine":
            return np.sin(phases)
        if name == "square":
            return np.sign(np.sin(phases))
        if name == "triangle":
            p = phases % (2.0 * np.pi)
            return np.where(p < np.pi, -1.0 + 2.0 * p / np.pi, 3.0 - 2.0 * p / np.pi)
        if name == "sawtooth":
            p = phases % (2.0 * np.pi)
            return -1.0 + p / np.pi
        return np.zeros(len(phases))

    def _new_hop(self, now: float):
        lo, hi = self.hop_start, self.hop_end
        if lo > hi:
            lo, hi = hi, lo
        lo = max(20.0, lo)
        hi = max(20.0, hi)
        self.hop_freq = float(np.random.uniform(lo, hi)) if hi > lo else lo
        interval = max(0.1, self.hop_interval)
        self.hop_next_time = now + interval

    def _write_scope(self, wave: np.ndarray):
        n = len(wave)
        buf_len = self.SCOPE_BUF_SIZE
        with self.scope_lock:
            if n >= buf_len:
                self.scope_buffer[:] = wave[-buf_len:]
                self.scope_write_pos = 0
            else:
                pos = self.scope_write_pos
                end = pos + n
                if end <= buf_len:
                    self.scope_buffer[pos:end] = wave
                else:
                    first = buf_len - pos
                    self.scope_buffer[pos:] = wave[:first]
                    self.scope_buffer[:n - first] = wave[first:]
                self.scope_write_pos = end % buf_len

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            if not self.playing:
                outdata[:] = 0
                return
            if self.mode == "track":
                # Loop the decoded audio track continuously.
                if self.track_data is None or len(self.track_data) == 0:
                    outdata[:] = 0
                    return
                n = len(self.track_data)
                pos = self.track_pos
                if pos + frames <= n:
                    chunk = self.track_data[pos:pos + frames]
                else:
                    first = n - pos
                    chunk = np.concatenate(
                        [self.track_data[pos:], self.track_data[:frames - first]]
                    )
                self.track_pos = (pos + frames) % n
                wave = chunk * self.volume
                self._write_scope(wave.astype(np.float32))
                outdata[:] = wave.reshape(-1, 1)
                return
            dt = np.arange(frames, dtype=np.float64) / self.sample_rate
            t = dt + self.sweep_time
            if self.mode == "tone":
                freqs = np.full(frames, self.frequency, dtype=np.float64)
            elif self.mode == "random_hop":
                # Hold the current hop frequency across this block, then choose
                # a new random frequency once the interval has elapsed.
                freqs = np.full(frames, self.hop_freq, dtype=np.float64)
                if t[-1] >= self.hop_next_time:
                    self._new_hop(t[-1])
            else:
                if self.mode == "sweep_repeat":
                    t_mod = t % self.sweep_duration
                else:
                    t_mod = np.clip(t, 0.0, self.sweep_duration)
                freqs = self._freqs_array(t_mod)

            # Calculate phase increment and update phase
            phases = self.phase + 2.0 * np.pi * np.cumsum(freqs) / self.sample_rate
            self.phase = phases[-1] % (2.0 * np.pi)

            # Generate and combine all selected waveforms
            names = self.waveforms or ["sine"]
            if len(names) == 1:
                wave = self._wave_shape(names[0], phases)
            else:
                wave = np.zeros(frames, dtype=np.float64)
                for name in names:
                    wave += self._wave_shape(name, phases)
                wave /= len(names)

            wave = wave * self.volume
            self._write_scope(wave.astype(np.float32))
            outdata[:] = wave.reshape(-1, 1)
            self.sweep_time += frames / self.sample_rate
            if self.mode == "sweep_once" and self.sweep_time >= self.sweep_duration:
                self.playing = False

    def start(self):
        with self.lock:
            self.phase = 0.0
            self.sweep_time = 0.0
            self.track_pos = 0
            self.playing = True
            if self.mode == "random_hop":
                self._new_hop(0.0)
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
        
        # Use dynamic self.sample_rate and self.blocksize
        self.stream = sd.OutputStream(
            samplerate=self.sample_rate, 
            channels=1, 
            dtype="float32",
            callback=self._callback, 
            blocksize=self.blocksize,
        )
        self.stream.start()

    def stop(self):
        with self.lock:
            self.playing = False
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def current_freq(self) -> float:
        with self.lock:
            if self.mode == "track":
                return 0.0
            if self.mode == "tone":
                return self.frequency
            if self.mode == "random_hop":
                return self.hop_freq
            t = self.sweep_time
            if self.mode == "sweep_repeat":
                t %= self.sweep_duration
            else:
                t = min(t, self.sweep_duration)
            return self._freq_at(t)

    def get_scope_data(self) -> np.ndarray:
        with self.scope_lock:
            pos = self.scope_write_pos
            buf = self.scope_buffer
            return np.concatenate([buf[pos:], buf[:pos]]).astype(np.float32)

# ========================= GUI Application =========================
class App:
    SCOPE_W = 700
    SCOPE_H = 220
    OSC_SPLIT_X = 412          # right edge of the oscilloscope (waveform) panel
    FFT_BARS = 96              # number of spectrum bars in the FFT panel
    FFT_DB_MIN = -80.0         # bottom of the FFT amplitude scale (dB)
    FFT_DB_MAX = 0.0           # top of the FFT amplitude scale (dB)

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("MiTtRAYNR - Waveform Generator")
        self.root.resizable(False, False)
        self.engine = AudioEngine()
        self.track_error = None
        self._load_demo_track()
        self._build_ui()
        self._update_display()
        self._update_scope()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _load_demo_track(self):
        path = resource_path(os.path.join("assets", "demo-audio.opus"))
        try:
            self.engine.load_track(path, "Demo Track")
        except Exception as exc:
            self.track_error = str(exc)

    def _build_ui(self):
        p = dict(padx=10, pady=6)
        
        # ---------- Waveform Selection (multi-select, 1-4) ----------
        frm_wave = ttk.LabelFrame(self.root, text=" Waveform (select 1-4) ")
        frm_wave.grid(row=0, column=0, sticky="nsew", **p)
        self.wave_vars: dict = {}
        self.wave_cbs: list = []
        self._last_wave = "sine"
        for i, name in enumerate(["sine", "square", "triangle", "sawtooth"]):
            var = tk.BooleanVar(value=(name == "sine"))
            self.wave_vars[name] = var
            cb = ttk.Checkbutton(frm_wave, text=name.capitalize(), variable=var,
                                 command=self._on_wave_change)
            cb.grid(row=i, column=0, sticky="w", padx=10, pady=2)
            self.wave_cbs.append(cb)

        # ---------- Demo Track (bundled audio) ----------
        ttk.Separator(frm_wave, orient="horizontal").grid(
            row=4, column=0, sticky="ew", padx=6, pady=4)
        self.track_var = tk.BooleanVar(value=False)
        self.track_cb = ttk.Checkbutton(frm_wave, text="Demo Track",
                                        variable=self.track_var,
                                        command=self._on_track_change)
        self.track_cb.grid(row=5, column=0, sticky="w", padx=10, pady=2)

        # ---------- Mode Selection ----------
        frm_mode = ttk.LabelFrame(self.root, text=" Mode ")
        frm_mode.grid(row=0, column=1, sticky="nsew", **p)
        self.mode_var = tk.StringVar(value="tone")
        self.mode_rbs: list = []
        self.tone_widgets: list = []
        self.sweep_widgets: list = []
        self.hop_widgets: list = []
        for i, (label, val) in enumerate([
            ("Continuous Tone", "tone"),
            ("Single Sweep", "sweep_once"),
            ("Repeated Sweep", "sweep_repeat"),
            ("Random Frequency Hop", "random_hop")
        ]):
            rb = ttk.Radiobutton(frm_mode, text=label, variable=self.mode_var,
                                 value=val, command=self._on_mode_change)
            rb.grid(row=i, column=0, sticky="w", padx=10, pady=2)
            self.mode_rbs.append(rb)

        # ---------- Tone Frequency ----------
        frm_tone = ttk.LabelFrame(self.root, text=" Tone Frequency ")
        frm_tone.grid(row=1, column=0, columnspan=2, sticky="ew", **p)
        frm_tone.columnconfigure(0, weight=1)
        self.tone_slider = tk.Scale(frm_tone, from_=0, to=1000, orient="horizontal",
                                    showvalue=False, command=self._on_tone_slider)
        self.tone_slider.set(freq_to_slider(440))
        self.tone_slider.grid(row=0, column=0, sticky="ew", padx=4)
        self.tone_widgets.append(self.tone_slider)
        self.tone_var = tk.StringVar(value="440.0 Hz")
        ent = ttk.Entry(frm_tone, textvariable=self.tone_var, width=12)
        ent.grid(row=0, column=1, padx=4)
        ent.bind("<Return>", self._on_tone_entry)
        self.tone_widgets.append(ent)

        # ---------- Sweep Parameters ----------
        frm_sw = ttk.LabelFrame(self.root, text=" Sweep Parameters ")
        frm_sw.grid(row=2, column=0, columnspan=2, sticky="ew", **p)
        frm_sw.columnconfigure(1, weight=1)
        ttk.Label(frm_sw, text="Start:").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        self.sw_start_sl = tk.Scale(frm_sw, from_=0, to=1000, orient="horizontal",
                                    showvalue=False, command=self._on_sw_start)
        self.sw_start_sl.set(freq_to_slider(200))
        self.sw_start_sl.grid(row=0, column=1, sticky="ew", padx=4)
        self.sweep_widgets.append(self.sw_start_sl)
        self.sw_start_var = tk.StringVar(value="200.0 Hz")
        e1 = ttk.Entry(frm_sw, textvariable=self.sw_start_var, width=12)
        e1.grid(row=0, column=2, padx=4)
        e1.bind("<Return>", self._on_sw_start_entry)
        self.sweep_widgets.append(e1)

        ttk.Label(frm_sw, text="End:").grid(row=1, column=0, sticky="e", padx=4, pady=3)
        self.sw_end_sl = tk.Scale(frm_sw, from_=0, to=1000, orient="horizontal",
                                  showvalue=False, command=self._on_sw_end)
        self.sw_end_sl.set(freq_to_slider(2000))
        self.sw_end_sl.grid(row=1, column=1, sticky="ew", padx=4)
        self.sweep_widgets.append(self.sw_end_sl)
        self.sw_end_var = tk.StringVar(value="2000.0 Hz")
        e2 = ttk.Entry(frm_sw, textvariable=self.sw_end_var, width=12)
        e2.grid(row=1, column=2, padx=4)
        e2.bind("<Return>", self._on_sw_end_entry)
        self.sweep_widgets.append(e2)

        ttk.Label(frm_sw, text="Duration:").grid(row=2, column=0, sticky="e", padx=4, pady=3)
        self.sw_dur_sl = tk.Scale(frm_sw, from_=1, to=300, orient="horizontal",
                                  showvalue=False, command=self._on_sw_dur)
        self.sw_dur_sl.set(20)
        self.sw_dur_sl.grid(row=2, column=1, sticky="ew", padx=4)
        self.sweep_widgets.append(self.sw_dur_sl)
        self.sw_dur_var = tk.StringVar(value="2.0 s")
        ttk.Label(frm_sw, textvariable=self.sw_dur_var, width=10).grid(row=2, column=2, padx=4)
        self.log_var = tk.BooleanVar(value=False)
        rb_lin = ttk.Radiobutton(frm_sw, text="Linear", variable=self.log_var,
                                 value=False, command=self._sync)
        rb_lin.grid(row=3, column=0, columnspan=2, sticky="w", padx=12, pady=2)
        self.sweep_widgets.append(rb_lin)
        rb_log = ttk.Radiobutton(frm_sw, text="Logarithmic", variable=self.log_var,
                                 value=True, command=self._sync)
        rb_log.grid(row=3, column=2, sticky="w", padx=4, pady=2)
        self.sweep_widgets.append(rb_log)

        # ---------- Random Hop Parameters ----------
        frm_hop = ttk.LabelFrame(self.root, text=" Random Hop Parameters ")
        frm_hop.grid(row=3, column=0, columnspan=2, sticky="ew", **p)
        frm_hop.columnconfigure(1, weight=1)
        ttk.Label(frm_hop, text="Min:").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        self.hop_min_sl = tk.Scale(frm_hop, from_=0, to=1000, orient="horizontal",
                                   showvalue=False, command=self._on_hop_min)
        self.hop_min_sl.set(freq_to_slider(200))
        self.hop_min_sl.grid(row=0, column=1, sticky="ew", padx=4)
        self.hop_widgets.append(self.hop_min_sl)
        self.hop_min_var = tk.StringVar(value="200.0 Hz")
        h1 = ttk.Entry(frm_hop, textvariable=self.hop_min_var, width=12)
        h1.grid(row=0, column=2, padx=4)
        h1.bind("<Return>", self._on_hop_min_entry)
        self.hop_widgets.append(h1)

        ttk.Label(frm_hop, text="Max:").grid(row=1, column=0, sticky="e", padx=4, pady=3)
        self.hop_max_sl = tk.Scale(frm_hop, from_=0, to=1000, orient="horizontal",
                                   showvalue=False, command=self._on_hop_max)
        self.hop_max_sl.set(freq_to_slider(2000))
        self.hop_max_sl.grid(row=1, column=1, sticky="ew", padx=4)
        self.hop_widgets.append(self.hop_max_sl)
        self.hop_max_var = tk.StringVar(value="2000.0 Hz")
        h2 = ttk.Entry(frm_hop, textvariable=self.hop_max_var, width=12)
        h2.grid(row=1, column=2, padx=4)
        h2.bind("<Return>", self._on_hop_max_entry)
        self.hop_widgets.append(h2)

        ttk.Label(frm_hop, text="Interval:").grid(row=2, column=0, sticky="e", padx=4, pady=3)
        self.hop_int_sl = tk.Scale(frm_hop, from_=100, to=10000, orient="horizontal",
                                   showvalue=False, command=self._on_hop_int)
        self.hop_int_sl.set(1000)
        self.hop_int_sl.grid(row=2, column=1, sticky="ew", padx=4)
        self.hop_widgets.append(self.hop_int_sl)
        self.hop_int_var = tk.StringVar(value="1000 ms")
        h3 = ttk.Entry(frm_hop, textvariable=self.hop_int_var, width=12)
        h3.grid(row=2, column=2, padx=4)
        h3.bind("<Return>", self._on_hop_int_entry)
        self.hop_widgets.append(h3)

        # ---------- Volume ----------
        frm_vol = ttk.LabelFrame(self.root, text=" Volume ")
        frm_vol.grid(row=4, column=0, columnspan=2, sticky="ew", **p)
        frm_vol.columnconfigure(0, weight=1)
        self.vol_slider = tk.Scale(frm_vol, from_=0, to=100, orient="horizontal",
                                   showvalue=False, command=self._on_vol)
        self.vol_slider.set(50)
        self.vol_slider.grid(row=0, column=0, sticky="ew", padx=4)
        self.vol_var = tk.StringVar(value="50 %")
        ttk.Label(frm_vol, textvariable=self.vol_var, width=8).grid(row=0, column=1, padx=4)

        # ---------- Audio Settings ----------
        frm_audio = ttk.LabelFrame(self.root, text=" Audio Settings ")
        frm_audio.grid(row=5, column=0, columnspan=2, sticky="ew", **p)
        
        ttk.Label(frm_audio, text="Sample Rate (Hz):").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        self.sr_var = tk.StringVar(value="44100")
        self.sr_combo = ttk.Combobox(frm_audio, textvariable=self.sr_var, width=10,
                                     values=["22050", "44100", "48000", "96000"], state="readonly")
        self.sr_combo.grid(row=0, column=1, padx=4, pady=3)

        ttk.Label(frm_audio, text="Buffer Size (Frames):").grid(row=0, column=2, sticky="e", padx=4, pady=3)
        self.bs_var = tk.StringVar(value="1024")
        self.bs_combo = ttk.Combobox(frm_audio, textvariable=self.bs_var, width=10,
                                     values=["64", "128", "256", "512", "1024", "2048", "4096"], state="readonly")
        self.bs_combo.grid(row=0, column=3, padx=4, pady=3)

        # ============== Oscilloscope ==============
        frm_scope = ttk.LabelFrame(self.root, text=" Oscilloscope ")
        frm_scope.grid(row=6, column=0, columnspan=2, sticky="nsew", **p)
        frm_scope.columnconfigure(0, weight=1)
        self.scope_canvas = tk.Canvas(frm_scope, width=self.SCOPE_W, height=self.SCOPE_H,
                                      bg="#0a0f0a", highlightthickness=1,
                                      highlightbackground="#2a3a2a")
        self.scope_canvas.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        self.scope_canvas.bind("<Configure>", self._on_scope_resize)

        frm_scope_ctrl = ttk.Frame(frm_scope)
        frm_scope_ctrl.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))
        ttk.Label(frm_scope_ctrl, text="Time Scale:").pack(side="left", padx=4)
        self.scope_cycles_var = tk.StringVar(value="24")
        scope_cycles_spin = ttk.Spinbox(frm_scope_ctrl, from_=1, to=10, width=4,
                                        textvariable=self.scope_cycles_var,
                                        command=self._redraw_scope_grid)
        scope_cycles_spin.pack(side="left", padx=2)
        ttk.Label(frm_scope_ctrl, text="cycles").pack(side="left", padx=2)
        self.scope_info_var = tk.StringVar(value="CH1  1.0V/div  |  —")
        ttk.Label(frm_scope_ctrl, textvariable=self.scope_info_var,
                  foreground="#6fbf6f").pack(side="right", padx=6)

        self._draw_scope_grid()
        self.scope_line = self.scope_canvas.create_line(
            [0] * (self.SCOPE_W * 2), fill="#39ff6e", width=1.6, smooth=False
        )
        self._create_fft_bars()

        # ---------- Control ----------
        frm_ctrl = ttk.Frame(self.root)
        frm_ctrl.grid(row=7, column=0, columnspan=2, sticky="ew", **p) 
        self.freq_lbl = ttk.Label(frm_ctrl, text="Current Frequency: 440.0 Hz",
                                  font=("TkDefaultFont", 13, "bold"))
        self.freq_lbl.pack(side="left", padx=8)
        self.status_var = tk.StringVar(value="⏹ Stopped")
        ttk.Label(frm_ctrl, textvariable=self.status_var).pack(side="left", padx=12)
        self.btn_stop = ttk.Button(frm_ctrl, text="■ Stop", command=self._stop)
        self.btn_stop.pack(side="right", padx=4)
        self.btn_start = ttk.Button(frm_ctrl, text="▶ Start", command=self._start)
        self.btn_start.pack(side="right", padx=4)

        # ---------- Note ----------
        frm_info = ttk.Frame(self.root)
        frm_info.grid(row=8, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))
        ttk.Label(
            frm_info,
            text="Err... so this is a waveform generator. \n"
                 "You can generate sine, square, triangle, and sawtooth waves, as well as frequency sweeps. \n"
                 "Tick \"Demo Track\" to loop the bundled audio track (only the volume control stays active). \n"
                 "Adjust the parameters above and click Start to play the sound. \n"
                 "MiTtRAYNR = Maybe it's Time to Retaliate Against Your Noisy Roommate.",
            foreground="gray", justify="left"
        ).pack(anchor="w")
        ttk.Label(
            frm_info,
            text="⚠ Warning: When playing for the first time, \n"
                 "please ensure the volume is set to the lowest level to prevent sudden high volume from damaging your hearing! \n"
                 "Even if you want to retaliate against your roommate, please consider your own hearing!",
            foreground="red", font=("TkDefaultFont", 10, "bold"), justify="left"
        ).pack(anchor="w")

        self._on_mode_change()

    # ---------- Scope geometry ----------
    def _scope_geometry(self):
        """Return the canvas's current (w, h, split) in real pixels.

        The canvas is stretched by the grid layout, so the drawn content must
        follow its actual width instead of the initial request size; otherwise a
        blank strip remains on the right-hand side.
        """
        w = self.scope_canvas.winfo_width()
        if w <= 1:
            w = self.SCOPE_W
        h = self.scope_canvas.winfo_height()
        if h <= 1:
            h = self.SCOPE_H
        split = min(self.OSC_SPLIT_X, max(1, w - 60))
        return w, h, split

    def _on_scope_resize(self, _event=None):
        self._draw_scope_grid()

    # ---------- Create FFT Bar Rectangles ----------
    def _create_fft_bars(self):
        """Pre-create the spectrum bar rectangles so per-frame updates are cheap.

        Only the item ids are created here; positions are computed each frame
        from the canvas's live geometry (see ``_draw_spectrum``).
        """
        self.fft_bars = []
        for _ in range(self.FFT_BARS):
            rid = self.scope_canvas.create_rectangle(
                0, 0, 0, 0, fill="#39ff6e", outline=""
            )
            self.fft_bars.append(rid)

    # ---------- Paint Oscilloscope Grid ----------
    def _draw_scope_grid(self):
        c = self.scope_canvas
        c.delete("grid")
        w, h, split = self._scope_geometry()

        # --- Oscilloscope panel (left): time-domain grid ---
        c.create_rectangle(0, 0, split, h, outline="#2a3a2a", tags="grid")
        for v in [-1.0, -0.5, 0.0, 0.5, 1.0]:
            y = int(h / 2 - v * (h / 2 - 4))
            color = "#3a5a3a" if v == 0 else "#1a2a1a"
            c.create_line(0, y, split, y, fill=color, width=1, tags="grid")
        for i in range(1, 10):
            x = int(split * i / 10)
            c.create_line(x, 0, x, h, fill="#1a2a1a", width=1, tags="grid")
        c.create_text(6, h / 2 - (h / 2 - 4) - 2, text="+1.0", fill="#4a7a4a",
                      anchor="w", font=("Consolas", 8), tags="grid")
        c.create_text(6, h / 2 + (h / 2 - 4) + 2, text="-1.0", fill="#4a7a4a",
                      anchor="w", font=("Consolas", 8), tags="grid")

        # --- FFT panel (right): dB amplitude grid ---
        c.create_rectangle(split, 0, w, h, outline="#2a3a2a", tags="grid")
        span = self.FFT_DB_MAX - self.FFT_DB_MIN
        for db in (-60, -40, -20, 0):
            frac = (db - self.FFT_DB_MIN) / span
            y = int(h - 4 - frac * (h - 8))
            c.create_line(split, y, w, y, fill="#1a2a1a", width=1, tags="grid")
            c.create_text(split + 3, y - 1, text=f"{db}", fill="#4a7a4a",
                          anchor="sw", font=("Consolas", 7), tags="grid")
        for i in range(1, 5):
            x = int(split + (w - split) * i / 5)
            c.create_line(x, 0, x, h, fill="#1a2a1a", width=1, tags="grid")
        c.create_line(split, 0, split, h, fill="#3a5a3a", width=1, tags="grid")
        c.create_text(split + 4, 3, text="FFT  (dB)", fill="#4a7a4a",
                      anchor="nw", font=("Consolas", 8), tags="grid")
        # Keep the grid behind the trace and spectrum bars.
        c.tag_lower("grid")

    def _redraw_scope_grid(self):
        self._draw_scope_grid()

    def _update_scope(self):
        if self.engine.playing:
            data = self.engine.get_scope_data()
            self._draw_scope_wave(data)
            self._draw_spectrum(data)
            if self.engine.mode == "track":
                self.scope_info_var.set("CH1  1.0V/div  |  Demo Track")
            else:
                try:
                    cycles = int(self.scope_cycles_var.get())
                except ValueError:
                    cycles = 24
                freq = self.engine.current_freq()
                time_per_div = (cycles / freq) / 10 if freq > 0 else 0
                if time_per_div >= 1e-3:
                    t_str = f"{time_per_div * 1000:.2f} ms/div"
                else:
                    t_str = f"{time_per_div * 1e6:.1f} µs/div"
                self.scope_info_var.set(f"CH1  1.0V/div  |  {t_str}  |  {freq:.1f} Hz")
        else:
            _w, h, split = self._scope_geometry()
            y = h // 2
            pts = [0, y, split, y]
            self.scope_canvas.coords(self.scope_line, *pts)
            self._clear_spectrum()
            self.scope_info_var.set("CH1  1.0V/div  |  —")
        self.root.after(16, self._update_scope)

    def _draw_spectrum(self, data: np.ndarray):
        """Compute an FFT of the most recent samples and update the bar graph."""
        if data is None or len(data) < 16:
            self._clear_spectrum()
            return
        n = len(data)
        # Remove DC offset so a constant bias doesn't dominate the spectrum.
        seg = data - float(np.mean(data))
        # Hanning window reduces spectral leakage between bins.
        window = np.hanning(n)
        spectrum = np.abs(np.fft.rfft(seg * window))
        if not np.any(spectrum):
            self._clear_spectrum()
            return
        # Normalise so the strongest component sits near the top of the scale.
        spectrum /= (np.max(spectrum) + 1e-12)
        db = 20.0 * np.log10(spectrum + 1e-12)

        # Only show the audible band (approx. 20 Hz to 18 kHz, clipped by Nyquist).
        freqs = np.fft.rfftfreq(n, d=1.0 / self.engine.sample_rate)
        nyquist = self.engine.sample_rate / 2.0
        f_hi = min(18000.0, nyquist)
        lo_bin = int(np.searchsorted(freqs, 20.0))
        hi_bin = int(np.searchsorted(freqs, f_hi))

        n_bars = self.FFT_BARS
        w, h, split = self._scope_geometry()
        x0, x1 = split, w
        top = 4
        bottom = h - 4
        span = self.FFT_DB_MAX - self.FFT_DB_MIN
        usable = (x1 - x0) - 4
        bar_w = usable / n_bars

        # Logarithmically spaced bin edges so low frequencies get more detail.
        if hi_bin <= lo_bin or bar_w <= 0:
            self._clear_spectrum()
            return
        edges = np.unique(np.geomspace(
            max(lo_bin, 1), hi_bin, num=n_bars + 1
        ).astype(int))
        # If the band collapses to fewer bars than we draw, pad the edges.
        if len(edges) < 2:
            self._clear_spectrum()
            return

        for i, rid in enumerate(self.fft_bars):
            bx0 = x0 + 2 + i * bar_w + 0.5
            bx1 = bx0 + bar_w - 1.0
            frac = i / n_bars
            start = edges[min(int(frac * (len(edges) - 1)), len(edges) - 2)]
            end = edges[min(int((i + 1) / n_bars * (len(edges) - 1)), len(edges) - 1)]
            if end <= start:
                peak_db = db[start] if start < len(db) else self.FFT_DB_MIN
            else:
                peak_db = float(np.max(db[start:end]))
            level = (peak_db - self.FFT_DB_MIN) / span
            level = min(1.0, max(0.0, level))
            y_top = bottom - level * (bottom - top)
            self.scope_canvas.coords(rid, bx0, y_top, bx1, bottom)

    def _clear_spectrum(self):
        _w, h, _split = self._scope_geometry()
        bottom = h - 4
        for rid in self.fft_bars:
            self.scope_canvas.coords(rid, 0, bottom, 0, bottom)

    def _draw_scope_wave(self, data: np.ndarray):
        _w, h, split = self._scope_geometry()
        w = split
        try:
            cycles = int(self.scope_cycles_var.get())
        except ValueError:
            cycles = 24
        freq = self.engine.current_freq() if self.engine.playing else 440.0
        freq = max(freq, 1.0)
        samples_per_cycle = self.engine.sample_rate / freq
        total_samples = int(samples_per_cycle * cycles)
        total_samples = min(total_samples, len(data))
        if total_samples < 2:
            return
        recent = data[-total_samples:]
        if total_samples > w:
            indices = np.linspace(0, total_samples - 1, w, dtype=int)
            y_vals = recent[indices]
            x_vals = np.arange(w)
        else:
            x_vals = np.linspace(0, w - 1, total_samples)
            y_vals = recent
        y_mid = h / 2
        y_scale = h / 2 - 4
        ys = y_mid - y_vals * y_scale
        coords = np.empty(len(x_vals) * 2, dtype=np.int32)
        coords[0::2] = x_vals.astype(np.int32)
        coords[1::2] = ys.astype(np.int32)
        self.scope_canvas.coords(self.scope_line, *coords.tolist())

    # ---------- Handle Events ----------
    def _on_tone_slider(self, val):
        f = slider_to_freq(float(val))
        self.tone_var.set(f"{f:.1f} Hz")
        self._sync()

    def _on_tone_entry(self, _=None):
        try:
            f = float(self.tone_var.get().replace("Hz", "").strip())
            f = max(20.0, min(20000.0, f))
            self.tone_slider.set(freq_to_slider(f))
            self.tone_var.set(f"{f:.1f} Hz")
            self._sync()
        except ValueError:
            pass

    def _on_sw_start(self, val):
        f = slider_to_freq(float(val))
        self.sw_start_var.set(f"{f:.1f} Hz")
        self._sync()

    def _on_sw_start_entry(self, _=None):
        try:
            f = float(self.sw_start_var.get().replace("Hz", "").strip())
            f = max(20.0, min(20000.0, f))
            self.sw_start_sl.set(freq_to_slider(f))
            self.sw_start_var.set(f"{f:.1f} Hz")
            self._sync()
        except ValueError:
            pass

    def _on_sw_end(self, val):
        f = slider_to_freq(float(val))
        self.sw_end_var.set(f"{f:.1f} Hz")
        self._sync()

    def _on_sw_end_entry(self, _=None):
        try:
            f = float(self.sw_end_var.get().replace("Hz", "").strip())
            f = max(20.0, min(20000.0, f))
            self.sw_end_sl.set(freq_to_slider(f))
            self.sw_end_var.set(f"{f:.1f} Hz")
            self._sync()
        except ValueError:
            pass

    def _on_sw_dur(self, val):
        d = float(val) / 10.0
        self.sw_dur_var.set(f"{d:.1f} s")
        self._sync()

    def _on_hop_min(self, val):
        f = slider_to_freq(float(val))
        self.hop_min_var.set(f"{f:.1f} Hz")
        self._sync()

    def _on_hop_min_entry(self, _=None):
        try:
            f = float(self.hop_min_var.get().replace("Hz", "").strip())
            f = max(20.0, min(20000.0, f))
            self.hop_min_sl.set(freq_to_slider(f))
            self.hop_min_var.set(f"{f:.1f} Hz")
            self._sync()
        except ValueError:
            pass

    def _on_hop_max(self, val):
        f = slider_to_freq(float(val))
        self.hop_max_var.set(f"{f:.1f} Hz")
        self._sync()

    def _on_hop_max_entry(self, _=None):
        try:
            f = float(self.hop_max_var.get().replace("Hz", "").strip())
            f = max(20.0, min(20000.0, f))
            self.hop_max_sl.set(freq_to_slider(f))
            self.hop_max_var.set(f"{f:.1f} Hz")
            self._sync()
        except ValueError:
            pass

    def _on_hop_int(self, val):
        self.hop_int_var.set(f"{int(float(val))} ms")
        self._sync()

    def _on_hop_int_entry(self, _=None):
        try:
            ms = float(self.hop_int_var.get().replace("ms", "").strip())
            ms = max(100.0, min(10000.0, ms))
            self.hop_int_sl.set(int(ms))
            self.hop_int_var.set(f"{int(ms)} ms")
            self._sync()
        except ValueError:
            pass

    def _on_wave_change(self):
        # Enforce at least one and at most four selected waveforms
        selected = [n for n, v in self.wave_vars.items() if v.get()]
        if len(selected) == 0:
            messagebox.showwarning("Waveform Selection",
                                   "Please select at least one waveform.")
            # Restore the waveform that was just deselected
            self.wave_vars[self._last_wave].set(True)
        elif len(selected) > 4:
            messagebox.showwarning("Waveform Selection",
                                   "You can select at most four waveforms.")
            self.wave_vars[self._last_wave].set(False)
        else:
            self._last_wave = selected[-1]
        self._sync()

    def _on_track_change(self):
        if self.track_var.get() and self.track_error is not None:
            messagebox.showerror("Demo Track Error",
                                 f"Failed to load the demo track!\n\n{self.track_error}")
            self.track_var.set(False)
        self._on_mode_change()

    def _on_vol(self, val):
        self.vol_var.set(f"{val} %")
        self._sync()

    def _on_mode_change(self):
        track = self.track_var.get()
        mode = self.mode_var.get()
        is_tone = (not track) and mode == "tone"
        is_sweep = (not track) and mode in ("sweep_once", "sweep_repeat")
        is_hop = (not track) and mode == "random_hop"
        for w in self.tone_widgets:
            w.config(state="normal" if is_tone else "disabled")
        for w in self.sweep_widgets:
            w.config(state="normal" if is_sweep else "disabled")
        for w in self.hop_widgets:
            w.config(state="normal" if is_hop else "disabled")
        # While the demo track is selected, only the volume control stays active.
        for cb in self.wave_cbs:
            cb.config(state="disabled" if track else "normal")
        for rb in self.mode_rbs:
            rb.config(state="disabled" if track else "normal")
        self.sr_combo.config(state="disabled" if track else "readonly")
        self.bs_combo.config(state="disabled" if track else "readonly")
        self._sync()

    def _sync(self):
        e = self.engine
        e.waveforms = [n for n, v in self.wave_vars.items() if v.get()] or ["sine"]
        track = self.track_var.get()
        e.mode = "track" if track else self.mode_var.get()
        e.frequency = slider_to_freq(self.tone_slider.get())
        e.sweep_start = slider_to_freq(self.sw_start_sl.get())
        e.sweep_end = slider_to_freq(self.sw_end_sl.get())
        e.sweep_duration = self.sw_dur_sl.get() / 10.0
        e.log_sweep = self.log_var.get()
        e.hop_start = slider_to_freq(self.hop_min_sl.get())
        e.hop_end = slider_to_freq(self.hop_max_sl.get())
        e.hop_interval = max(0.1, self.hop_int_sl.get() / 1000.0)
        e.volume = self.vol_slider.get() / 100.0

        need_restart = False
        if track and e.track_samplerate:
            # Play the track at its native sample rate to avoid pitch shift.
            if e.track_samplerate != e.sample_rate:
                e.sample_rate = e.track_samplerate
                need_restart = True
        else:
            try:
                new_sr = int(self.sr_var.get())
                if new_sr != e.sample_rate:
                    e.sample_rate = new_sr
                    need_restart = True
            except ValueError:
                pass

        try:
            new_bs = int(self.bs_var.get())
            if new_bs != e.blocksize:
                e.blocksize = new_bs
                need_restart = True
        except ValueError:
            pass
            
        # If the engine is currently playing, restart it to apply new settings
        if need_restart and e.playing:
            self._start()

    def _start(self):
        self.engine.stop()
        self._sync()
        try:
            self.engine.start()
            self.status_var.set("▶ Playing")
        except Exception as exc:
            # Catch exceptions related to audio device initialization and show an error message
            messagebox.showerror("Audio Device Error", 
                f"Failed to initialize audio stream!\n\nError message:\n{exc}")
            self.status_var.set("⏹ Stopped")

    def _stop(self):
        self.engine.stop()
        self.status_var.set("⏹ Stopped")

    def _update_display(self):
        if self.engine.playing:
            if self.engine.mode == "track":
                self.freq_lbl.config(text=f"Now Playing: {self.engine.track_name}")
            else:
                f = self.engine.current_freq()
                self.freq_lbl.config(text=f"Current Frequency: {f:.1f} Hz")
        else:
            if "Playing" in self.status_var.get():
                self.status_var.set("⏹ Stopped")
            self.freq_lbl.config(text="Current Frequency: —")
        self.root.after(16, self._update_display)

    def _on_close(self):
        self.engine.stop()
        self.root.destroy()

# ========================== Main ==========================
if __name__ == "__main__":
    setup_high_dpi()
    root = tk.Tk()
    app = App(root)
    root.mainloop()
