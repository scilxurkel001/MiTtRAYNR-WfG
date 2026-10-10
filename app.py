import os
import platform
import ctypes
import tkinter as tk
from tkinter import ttk, messagebox
import numpy as np
import math
import threading
import sounddevice as sd

# ======== Conversion between slider value and frequency (logarithmic mapping) ========
def slider_to_freq(val: float) -> float:
    return 20.0 * (1000.0 ** (val / 1000.0))

def freq_to_slider(freq: float) -> int:
    freq = max(20.0, min(20000.0, freq))
    return int(1000.0 * math.log(freq / 20.0) / math.log(1000.0))

# ======================= High-DPI Support ========================
def setup_high_dpi():
    if platform.system() == 'Windows':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except:
                pass
def get_scale_factor():
    if platform.system() == 'Windows':
        try:
            return ctypes.windll.user32.GetDpiForSystem() / 96.0
        except:
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
            ctypes.windll.user32.ReleaseDC(0, hdc)
            return dpi / 96.0
            # Linux and macOS typically handle DPI scaling automatically, but we can attempt to read GDK_SCALE for Linux
    try:
        # Some Linux environments use GDK_SCALE for scaling factor, default to 1.0 if not set
        return float(os.environ.get('GDK_SCALE', 1.0))
    except:
        return 1.0

# ======================== Audio Engine ========================
class AudioEngine:
    def __init__(self, sample_rate: int = 44100):
        self.sample_rate = sample_rate
        self.phase = 0.0
        self.sweep_time = 0.0
        self.playing = False
        self.lock = threading.Lock()
        self.stream = None
        
        self.waveform = "sine"
        self.mode = "tone"
        self.frequency = 440.0
        self.sweep_start = 200.0
        self.sweep_end = 2000.0
        self.sweep_duration = 2.0
        self.log_sweep = False
        self.volume = 0.5

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

    def _callback(self, outdata, frames, time_info, status):
        with self.lock:
            if not self.playing:
                outdata[:] = 0
                return
            dt = np.arange(frames, dtype=np.float64) / self.sample_rate
            t = dt + self.sweep_time
            if self.mode == "tone":
                freqs = np.full(frames, self.frequency, dtype=np.float64)
            else:
                if self.mode == "sweep_repeat":
                    t_mod = t % self.sweep_duration
                else:
                    t_mod = np.clip(t, 0.0, self.sweep_duration)
                freqs = self._freqs_array(t_mod)
            
            phases = self.phase + 2.0 * np.pi * np.cumsum(freqs) / self.sample_rate
            self.phase = phases[-1] % (2.0 * np.pi)
            
            if self.waveform == "sine":
                wave = np.sin(phases)
            elif self.waveform == "square":
                wave = np.sign(np.sin(phases))
            elif self.waveform == "triangle":
                p = phases % (2.0 * np.pi)
                wave = np.where(p < np.pi, -1.0 + 2.0 * p / np.pi, 3.0 - 2.0 * p / np.pi)
            elif self.waveform == "sawtooth":
                p = phases % (2.0 * np.pi)
                wave = -1.0 + p / np.pi
            else:
                wave = np.zeros(frames)
                
            outdata[:] = (wave * self.volume).reshape(-1, 1)
            self.sweep_time += frames / self.sample_rate
            if self.mode == "sweep_once" and self.sweep_time >= self.sweep_duration:
                self.playing = False

    def start(self):
        with self.lock:
            self.phase = 0.0
            self.sweep_time = 0.0
            self.playing = True
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
        self.stream = sd.OutputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32",
            callback=self._callback, blocksize=1024,
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
            if self.mode == "tone":
                return self.frequency
            t = self.sweep_time
            if self.mode == "sweep_repeat":
                t %= self.sweep_duration
            else:
                t = min(t, self.sweep_duration)
            return self._freq_at(t)

# ======================== GUI Application ========================
class App:
    def __init__(self, root: tk.Tk):
        scale = get_scale_factor()
        self.root = root
        self.root.title("MiTtRAYNR - Waveform Generator")
        self.root.resizable(False, False)
        self.engine = AudioEngine()
        self._build_ui()
        self._update_display()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        scale = get_scale_factor()
        p = dict(padx=10, pady=6)
        
        frm_wave = ttk.LabelFrame(self.root, text=" Waveform ")
        frm_wave.grid(row=0, column=0, sticky="nsew", **p)
        self.wave_var = tk.StringVar(value="sine")
        for i, name in enumerate(["sine", "square", "triangle", "sawtooth"]):
            ttk.Radiobutton(frm_wave, text=name.capitalize(), variable=self.wave_var, value=name, command=self._sync).grid(row=i, column=0, sticky="w", padx=10, pady=2)

        frm_mode = ttk.LabelFrame(self.root, text=" Mode ")
        frm_mode.grid(row=0, column=1, sticky="nsew", **p)
        self.mode_var = tk.StringVar(value="tone")
        self.tone_widgets: list[tk.Widget] = []
        self.sweep_widgets: list[tk.Widget] = []
        for i, (label, val) in enumerate([("Continuous Tone", "tone"), ("Single Sweep", "sweep_once"), ("Repeated Sweep", "sweep_repeat")]):
            ttk.Radiobutton(frm_mode, text=label, variable=self.mode_var, value=val, command=self._on_mode_change).grid(row=i, column=0, sticky="w", padx=10, pady=2)

        frm_tone = ttk.LabelFrame(self.root, text=" Tone Frequency ")
        frm_tone.grid(row=1, column=0, columnspan=2, sticky="ew", **p)
        self.tone_slider = tk.Scale(frm_tone, from_=0, to=1000, orient="horizontal", showvalue=False, command=self._on_tone_slider)
        self.tone_slider.set(freq_to_slider(440))
        self.tone_slider.pack(side="left", fill="x", expand=True, padx=4)
        self.tone_widgets.append(self.tone_slider)
        self.tone_var = tk.StringVar(value="440.0 Hz")
        ent = ttk.Entry(frm_tone, textvariable=self.tone_var, width=12)
        ent.pack(side="right", padx=4)
        ent.bind("<Return>", self._on_tone_entry)
        self.tone_widgets.append(ent)

        frm_sw = ttk.LabelFrame(self.root, text=" Sweep Parameters ")
        frm_sw.grid(row=2, column=0, columnspan=2, sticky="ew", **p)
        frm_sw.columnconfigure(1, weight=1)
        
        ttk.Label(frm_sw, text="Start:").grid(row=0, column=0, sticky="e", padx=4, pady=3)
        self.sw_start_sl = tk.Scale(frm_sw, from_=0, to=1000, orient="horizontal", showvalue=False, command=self._on_sw_start)
        self.sw_start_sl.set(freq_to_slider(200))
        self.sw_start_sl.grid(row=0, column=1, sticky="ew", padx=4)
        self.sweep_widgets.append(self.sw_start_sl)
        self.sw_start_var = tk.StringVar(value="200.0 Hz")
        e1 = ttk.Entry(frm_sw, textvariable=self.sw_start_var, width=12)
        e1.grid(row=0, column=2, padx=4)
        e1.bind("<Return>", self._on_sw_start_entry)
        self.sweep_widgets.append(e1)

        ttk.Label(frm_sw, text="End:").grid(row=1, column=0, sticky="e", padx=4, pady=3)
        self.sw_end_sl = tk.Scale(frm_sw, from_=0, to=1000, orient="horizontal", showvalue=False, command=self._on_sw_end)
        self.sw_end_sl.set(freq_to_slider(2000))
        self.sw_end_sl.grid(row=1, column=1, sticky="ew", padx=4)
        self.sweep_widgets.append(self.sw_end_sl)
        self.sw_end_var = tk.StringVar(value="2000.0 Hz")
        e2 = ttk.Entry(frm_sw, textvariable=self.sw_end_var, width=12)
        e2.grid(row=1, column=2, padx=4)
        e2.bind("<Return>", self._on_sw_end_entry)
        self.sweep_widgets.append(e2)

        ttk.Label(frm_sw, text="Duration:").grid(row=2, column=0, sticky="e", padx=4, pady=3)
        self.sw_dur_sl = tk.Scale(frm_sw, from_=1, to=300, orient="horizontal", showvalue=False, command=self._on_sw_dur)
        self.sw_dur_sl.set(20)
        self.sw_dur_sl.grid(row=2, column=1, sticky="ew", padx=4)
        self.sweep_widgets.append(self.sw_dur_sl)
        self.sw_dur_var = tk.StringVar(value="2.0 s")
        ttk.Label(frm_sw, textvariable=self.sw_dur_var, width=10).grid(row=2, column=2, padx=4)

        self.log_var = tk.BooleanVar(value=False)
        rb_lin = ttk.Radiobutton(frm_sw, text="Linear", variable=self.log_var, value=False, command=self._sync)
        rb_lin.grid(row=3, column=0, columnspan=2, sticky="w", padx=12, pady=2)
        self.sweep_widgets.append(rb_lin)
        rb_log = ttk.Radiobutton(frm_sw, text="Logarithmic", variable=self.log_var, value=True, command=self._sync)
        rb_log.grid(row=3, column=2, sticky="w", padx=4, pady=2)
        self.sweep_widgets.append(rb_log)

        frm_vol = ttk.LabelFrame(self.root, text=" Volume ")
        frm_vol.grid(row=3, column=0, columnspan=2, sticky="ew", **p)
        self.vol_slider = tk.Scale(frm_vol, from_=0, to=100, orient="horizontal", showvalue=False, command=self._on_vol)
        self.vol_slider.set(50)
        self.vol_slider.pack(side="left", fill="x", expand=True, padx=4)
        self.vol_var = tk.StringVar(value="50 %")
        ttk.Label(frm_vol, textvariable=self.vol_var, width=8).pack(side="right", padx=4)

        frm_ctrl = ttk.Frame(self.root)
        frm_ctrl.grid(row=4, column=0, columnspan=2, sticky="ew", **p)
        self.freq_lbl = ttk.Label(frm_ctrl, text="Current Frequency: 440.0 Hz", font=("TkDefaultFont", 13, "bold"))
        self.freq_lbl.pack(side="left", padx=8)
        self.status_var = tk.StringVar(value="⏹ Stopped")
        ttk.Label(frm_ctrl, textvariable=self.status_var).pack(side="left", padx=12)
        self.btn_stop = ttk.Button(frm_ctrl, text="■ Stop", command=self._stop)
        self.btn_stop.pack(side="right", padx=4)
        self.btn_start = ttk.Button(frm_ctrl, text="▶ Start", command=self._start)
        self.btn_start.pack(side="right", padx=4)
        frm_info = ttk.Frame(self.root)
        frm_info.grid(row=5, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 6))
        
        ttk.Label(
            frm_info, 
            text="Err... so this is a waveform generator. \n You can generate sine, square, triangle, and sawtooth waves, as well as frequency sweeps. \n Adjust the parameters above and click Start to play the sound. \n MiTtRAYNR = Maybe it's Time to Retaliate Against Your Noisy Roommate.",
            foreground="gray",
            justify="left"
        ).pack(anchor="w")
        
        ttk.Label(
            frm_info, 
            text="⚠ Warning: When playing for the first time, \n please ensure the volume is set to the lowest level to prevent sudden high volume from damaging your hearing! \n Even if you want to retaliate against your roommate, please consider your own hearing!",
            foreground="red",
            font=("TkDefaultFont", 10, "bold"),
            justify="left"
        ).pack(anchor="w")

        self._on_mode_change()

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

    def _on_vol(self, val):
        self.vol_var.set(f"{val} %")
        self._sync()

    def _on_mode_change(self):
        is_tone = self.mode_var.get() == "tone"
        st_tone = "normal" if is_tone else "disabled"
        st_sw   = "disabled" if is_tone else "normal"
        for w in self.tone_widgets:
            w.config(state=st_tone)
        for w in self.sweep_widgets:
            w.config(state=st_sw)
        self._sync()

    def _sync(self):
        e = self.engine
        e.waveform       = self.wave_var.get()
        e.mode           = self.mode_var.get()
        e.frequency      = slider_to_freq(self.tone_slider.get())
        e.sweep_start    = slider_to_freq(self.sw_start_sl.get())
        e.sweep_end      = slider_to_freq(self.sw_end_sl.get())
        e.sweep_duration = self.sw_dur_sl.get() / 10.0
        e.log_sweep      = self.log_var.get()
        e.volume         = self.vol_slider.get() / 100.0

    def _start(self):
        self.engine.stop()
        self._sync()
        self.engine.start()
        self.status_var.set("▶ Playing")

    def _stop(self):
        self.engine.stop()
        self.status_var.set("⏹ Stopped")

    def _toggle(self):
        if self.engine.playing:
            self._stop()
        else:
            self._start()

    def _update_display(self):
        if self.engine.playing:
            f = self.engine.current_freq()
            self.freq_lbl.config(text=f"Current Frequency: {f:.1f} Hz")
        else:
            if "Playing" in self.status_var.get():
                self.status_var.set("⏹ Stopped")
            self.freq_lbl.config(text="Current Frequency: —")
        self.root.after(50, self._update_display)

    def _on_close(self):
        self.engine.stop()
        self.root.destroy()

# ========================== Main Entry Point ========================
if __name__ == "__main__":
    setup_high_dpi()
    root = tk.Tk()
    app = App(root)
    root.mainloop()