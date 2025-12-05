#!/usr/bin/env python3
"""
Downloader PRO - GUI premium (Dark Hybrid Discord+Steam)
- Pestañas arriba, iconos, animaciones, hover, ripple-like feedback
- Detección mixta (exponencial+binaria), descargas con resume, separación videos/imagenes
- Sonidos UI (hover, click, error, done) generados localmente
- Logs: download.log.txt + download.log.jsonl
- Guardado y carga de config (config.json)
- Manual start/finish fields (inicio/fin manual)
Requires:
    pip install requests tqdm ttkbootstrap
Run:
    python codigo.py
"""
# -----------------------------
# IMPORTS
# -----------------------------
import os
import sys
import time
import json
import math
import random
import threading
import traceback
import tempfile
import queue
import wave
import struct
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

try:
    from tqdm import tqdm
    TQDM_AVAILABLE = True
except:
    TQDM_AVAILABLE = False

# GUI libs
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

# prefer ttkbootstrap (user confirmed yes)
try:
    import ttkbootstrap as tb
    from ttkbootstrap.constants import *
    from ttkbootstrap.icons import Icon
    THEME_AVAILABLE = True
except Exception:
    THEME_AVAILABLE = False
    Icon = None

# -----------------------------
# GLOBALS / DEFAULTS
# -----------------------------
APP_NAME = "Downloader PRO"
CONFIG_FILE = "downloader_config.json"

DEFAULT_RELLENO = 4
DEFAULT_HILOS = 10
DEFAULT_HILOS_DET = 3
DEFAULT_REINTENTOS = 4

PAUSA_MIN = 0.2
PAUSA_MAX = 0.6
PAUSA_EMERGENCIA = 5
LIMITE_ERRORES = 10
TIMEOUT_BASE = (8, 30)
MAX_DETECT = 2000

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Linux; Android 11; SM-A505F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Mobile Safari/537.36",
]

# -----------------------------
# Utils: config persistence
# -----------------------------
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

# -----------------------------
# Sound generation (small WAVs)
# -----------------------------
def _gen_simple_tone(path, freq=800.0, duration=0.18, volume=14000, sweep_to=None):
    fr = 44100
    nframes = int(duration * fr)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(fr)
        for i in range(nframes):
            t = i / fr
            f = freq if sweep_to is None else freq + (sweep_to - freq) * (i / nframes)
            s = int(volume * math.sin(2 * math.pi * f * t))
            wf.writeframesraw(struct.pack("<h", s))
        wf.writeframes(b"")

def _ensure_ui_sounds():
    tmpdir = Path(tempfile.gettempdir())
    paths = {}
    p_done = tmpdir / "dl_done.wav"
    p_click = tmpdir / "dl_click.wav"
    p_hover = tmpdir / "dl_hover.wav"
    p_err = tmpdir / "dl_err.wav"
    if not p_done.exists():
        _gen_simple_tone(str(p_done), freq=700.0, duration=0.35, volume=14000, sweep_to=1150.0)
    if not p_click.exists():
        _gen_simple_tone(str(p_click), freq=900.0, duration=0.08, volume=9000)
    if not p_hover.exists():
        _gen_simple_tone(str(p_hover), freq=1200.0, duration=0.06, volume=6000)
    if not p_err.exists():
        _gen_simple_tone(str(p_err), freq=320.0, duration=0.14, volume=12000)
    paths['done'] = str(p_done)
    paths['click'] = str(p_click)
    paths['hover'] = str(p_hover)
    paths['err'] = str(p_err)
    return paths

_UI_SOUNDS = _ensure_ui_sounds()

def play_sound(path, async_play=True):
    try:
        if sys.platform.startswith("win"):
            import winsound
            flags = winsound.SND_FILENAME
            if async_play:
                flags |= winsound.SND_ASYNC
            winsound.PlaySound(path, flags)
        elif sys.platform == "darwin":
            if async_play:
                os.system(f"afplay {repr(path)} >/dev/null 2>&1 &")
            else:
                os.system(f"afplay {repr(path)} >/dev/null 2>&1")
        else:
            # try paplay / aplay
            cmd = f"paplay {repr(path)} >/dev/null 2>&1"  # prefer pipewire
            if async_play:
                cmd += " &"
            rc = os.system(cmd)
            if rc != 0:
                cmd2 = f"aplay {repr(path)} >/dev/null 2>&1"
                if async_play:
                    cmd2 += " &"
                os.system(cmd2)
    except Exception:
        pass

def play_ui(name):
    p = _UI_SOUNDS.get(name)
    if p:
        threading.Thread(target=play_sound, args=(p, True), daemon=True).start()

# -----------------------------
# Logging helpers
# -----------------------------
def append_log_txt(path, line):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def append_log_json(path, obj):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except Exception:
        pass

# -----------------------------
# Networking helpers (head, detect, download resume)
# -----------------------------
def head_ok(url, headers=None, timeout=8):
    headers = headers or {"User-Agent": random.choice(USER_AGENTS)}
    try:
        r = requests.head(url, headers=headers, timeout=timeout, allow_redirects=True)
        if r.status_code == 200:
            return True
        # fallback to quick GET
        r = requests.get(url, headers=headers, timeout=timeout, stream=True)
        return r.status_code == 200
    except Exception:
        return False

def detect_range_mixto(url_base, relleno=DEFAULT_RELLENO, max_busqueda=MAX_DETECT, quiet=False, hilos_det=DEFAULT_HILOS_DET):
    """Exponencial -> binaria -> ventana final verificando en paralelo."""
    if not quiet:
        print("🔍 Detección mixta: búsqueda exponencial...")
    n = 1
    prev = 0
    while n <= max_busqueda:
        s = str(n).zfill(relleno)
        if head_ok(f"{url_base}{s}.mp4") or head_ok(f"{url_base}{s}.jpg"):
            prev = n
            n *= 2
            continue
        else:
            break
    if n > max_busqueda:
        n = max_busqueda
    low = prev
    high = max(1, n)
    if low == 0:
        # try small block
        for t in range(1, min(8, max_busqueda)+1):
            s = str(t).zfill(relleno)
            if head_ok(f"{url_base}{s}.mp4") or head_ok(f"{url_base}{s}.jpg"):
                low = t
                break
        if low == 0:
            return 0
    if high < low:
        high = low
    if not quiet:
        print(f"🔎 Refinando por binaria entre {low} y {high}...")
    while low < high:
        mid = (low + high + 1) // 2
        s = str(mid).zfill(relleno)
        if head_ok(f"{url_base}{s}.mp4") or head_ok(f"{url_base}{s}.jpg"):
            low = mid
        else:
            high = mid - 1
        if low >= max_busqueda:
            low = max_busqueda
            break
    est_fin = low
    window_start = max(1, est_fin - 10)
    window_end = min(max_busqueda, est_fin + 10)
    if not quiet:
        print(f"🔬 Verificando ventana final {window_start}..{window_end} con hilos={hilos_det}...")
    found = set()
    def check(i):
        s = str(i).zfill(relleno)
        if head_ok(f"{url_base}{s}.mp4") or head_ok(f"{url_base}{s}.jpg"):
            return i
        return None
    with ThreadPoolExecutor(max_workers=hilos_det) as ex:
        futures = {ex.submit(check, i): i for i in range(window_start, window_end+1)}
        for fut in as_completed(futures):
            try:
                r = fut.result()
                if r:
                    found.add(r)
            except Exception:
                pass
    final = max(found) if found else est_fin
    if not quiet:
        print(f"✅ Detección estimada: {final}")
    return final

def download_with_resume(url, destino, headers, timeout=TIMEOUT_BASE, reintentos=DEFAULT_REINTENTOS, progress_callback=None):
    """Descarga con resume (temp .part). progress_callback(bytes_received, total_bytes) optional."""
    temp = destino + ".part"
    for intento in range(1, reintentos + 1):
        h = headers.copy()
        mode = "wb"
        pos = 0
        if os.path.exists(temp):
            pos = os.path.getsize(temp)
            if pos > 0:
                h["Range"] = f"bytes={pos}-"
                mode = "ab"
        try:
            r = requests.get(url, stream=True, headers=h, timeout=timeout, allow_redirects=True)
        except Exception:
            time.sleep(random.uniform(1.0, 2.5))
            continue
        if r.status_code in (403, 429):
            return (False, f"BLOCK_{r.status_code}")
        if r.status_code == 416:
            if os.path.exists(temp):
                try:
                    os.replace(temp, destino)
                except:
                    try:
                        os.rename(temp, destino)
                    except:
                        pass
                return (True, "RESUMED_RENAMED")
            return (False, f"HTTP_{r.status_code}")
        if r.status_code not in (200, 206):
            return (False, f"HTTP_{r.status_code}")
        total = None
        try:
            total = int(r.headers.get("content-length") or 0) + (pos or 0)
        except:
            total = None
        try:
            with open(temp, mode) as f:
                received = pos
                chunk_size = 8192
                for chunk in r.iter_content(chunk_size=chunk_size):
                    if chunk:
                        f.write(chunk)
                        received += len(chunk)
                        if progress_callback:
                            try:
                                progress_callback(received, total)
                            except Exception:
                                pass
            # rename
            try:
                os.replace(temp, destino)
            except:
                try:
                    os.rename(temp, destino)
                except:
                    pass
            return (True, "OK")
        except Exception:
            time.sleep(0.5)
            continue
    return (False, "FAILED_RETRIES")

def worker_job(base_no_ext, carpeta_base, reintentos, pausa_min, pausa_max):
    """Intenta mp4 primero, luego jpg. Logs y separa en subcarpetas."""
    nombre = os.path.basename(base_no_ext)
    carpeta_v = os.path.join(carpeta_base, "videos")
    carpeta_i = os.path.join(carpeta_base, "imagenes")
    os.makedirs(carpeta_v, exist_ok=True)
    os.makedirs(carpeta_i, exist_ok=True)

    log_txt = os.path.join(carpeta_base, "download.log.txt")
    log_json = os.path.join(carpeta_base, "download.log.jsonl")

    headers = {"User-Agent": random.choice(USER_AGENTS),
               "Referer": urlparse(base_no_ext).scheme + "://" + urlparse(base_no_ext).netloc,
               "Accept": "*/*"}

    url_mp4 = base_no_ext + ".mp4"
    url_jpg = base_no_ext + ".jpg"
    destino_mp4 = os.path.join(carpeta_v, nombre + ".mp4")
    destino_jpg = os.path.join(carpeta_i, nombre + ".jpg")

    if os.path.exists(destino_mp4):
        msg = f"SKIP (video exists): {destino_mp4}"
        append_log_txt(log_txt, msg); append_log_json(log_json, {"status":"skip","type":"video","path":destino_mp4})
        return msg
    if os.path.exists(destino_jpg):
        msg = f"SKIP (img exists): {destino_jpg}"
        append_log_txt(log_txt, msg); append_log_json(log_json, {"status":"skip","type":"img","path":destino_jpg})
        return msg

    # Try mp4 via HEAD -> download
    try:
        r_head = requests.head(url_mp4, headers=headers, timeout=8, allow_redirects=True)
        if r_head.status_code == 200:
            ok, detail = download_with_resume(url_mp4, destino_mp4, headers, reintentos=reintentos)
            if ok:
                msg = f"VIDEO OK: {destino_mp4}"
                append_log_txt(log_txt, msg); append_log_json(log_json, {"status":"ok","type":"video","path":destino_mp4})
                time.sleep(random.uniform(pausa_min, pausa_max))
                return msg
            else:
                if detail.startswith("BLOCK"):
                    msg = f"BLOCKED {detail}: {url_mp4}"
                    append_log_txt(log_txt, msg); append_log_json(log_json, {"status":"blocked","detail":detail,"url":url_mp4})
                    return msg
    except Exception:
        # try direct
        try:
            ok, detail = download_with_resume(url_mp4, destino_mp4, headers, reintentos=reintentos)
            if ok:
                msg = f"VIDEO OK: {destino_mp4}"
                append_log_txt(log_txt, msg); append_log_json(log_json, {"status":"ok","type":"video","path":destino_mp4})
                time.sleep(random.uniform(pausa_min, pausa_max))
                return msg
        except Exception:
            pass

    # try jpg
    try:
        r_head_j = requests.head(url_jpg, headers=headers, timeout=8, allow_redirects=True)
        if r_head_j.status_code == 200:
            ok, detail = download_with_resume(url_jpg, destino_jpg, headers, reintentos=reintentos)
            if ok:
                msg = f"IMG OK: {destino_jpg}"
                append_log_txt(log_txt, msg); append_log_json(log_json, {"status":"ok","type":"img","path":destino_jpg})
                time.sleep(random.uniform(pausa_min, pausa_max))
                return msg
            else:
                if detail.startswith("BLOCK"):
                    msg = f"BLOCKED {detail}: {url_jpg}"
                    append_log_txt(log_txt, msg); append_log_json(log_json, {"status":"blocked","detail":detail,"url":url_jpg})
                    return msg
    except Exception:
        try:
            ok, detail = download_with_resume(url_jpg, destino_jpg, headers, reintentos=reintentos)
            if ok:
                msg = f"IMG OK: {destino_jpg}"
                append_log_txt(log_txt, msg); append_log_json(log_json, {"status":"ok","type":"img","path":destino_jpg})
                time.sleep(random.uniform(pausa_min, pausa_max))
                return msg
        except Exception:
            pass

    msg = f"NOTFOUND: {base_no_ext}"
    append_log_txt(log_txt, msg); append_log_json(log_json, {"status":"notfound","base":base_no_ext})
    return msg

# -----------------------------
# GUI: helper widgets & styles
# -----------------------------
def style_setup(root):
    """Apply extra styling using ttkbootstrap when available, otherwise basic ttk tweaks."""
    if THEME_AVAILABLE:
        try:
            # set theme (superhero is dark blue; 'darkly' or 'cyborg' could be alternatives)
            root._style = tb.Style(theme="superhero")   # ✔ corregido: no usar root.style =
        except Exception:
            root._style = tb.Style()                   # ✔ de nuevo sin usar root.style =
    else:
        style = ttk.Style()
        # basic dark tweaks
        try:
            style.theme_use("clam")
        except:
            pass
        style.configure("TFrame", background="#0f1115")
        style.configure("TLabel", background="#0f1115", foreground="#eaf2ff", font=("Segoe UI", 10))
        style.configure("TButton", padding=6)


# Small wrapper to create icon if available
def get_icon(name, size=18):
    if THEME_AVAILABLE:
        try:
            return Icon.get(name)
        except Exception:
            return None
    return None

# Small animated button hover (simulate glow)
class HoverButton(ttk.Button):
    def __init__(self, master=None, **kw):
        super().__init__(master, **kw)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", lambda e: play_ui("click"))

    def _on_enter(self, e):
        try:
            self.configure(style="Accent.TButton")
        except:
            pass
        play_ui("hover")

    def _on_leave(self, e):
        try:
            self.configure(style="TButton")
        except:
            pass

# -----------------------------
# Main App class
# -----------------------------
class App(tb.Window if THEME_AVAILABLE else object):
    def __init__(self, root=None):
        # if using tb.Window, root param is not used; otherwise create tk root
        if THEME_AVAILABLE:
            # when using tb.Window as base, __init__ requires no args
            super().__init__(title=f"{APP_NAME}", themename="superhero")
            self.root = self
            style_setup(self)
        else:
            self.root = root or tk.Tk()
            style_setup(self.root)

        # load config
        self.cfg = load_config()
        # state & queue
        self.queue = queue.Queue()
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.threadpool = None

        # variables
        self.url_base = tk.StringVar(value=self.cfg.get("url_base",""))
        self.carpeta = tk.StringVar(value=self.cfg.get("carpeta", os.path.join(os.getcwd(),"descargas")))
        self.relleno = tk.IntVar(value=self.cfg.get("relleno", DEFAULT_RELLENO))
        self.hilos = tk.IntVar(value=self.cfg.get("hilos", DEFAULT_HILOS))
        self.reintentos = tk.IntVar(value=self.cfg.get("reintentos", DEFAULT_REINTENTOS))
        self.fin_detectado = tk.IntVar(value=0)
        self.hilos_det_var = tk.IntVar(value=self.cfg.get("hilos_det", DEFAULT_HILOS_DET))
        self.pausa_min_var = tk.DoubleVar(value=self.cfg.get("pausa_min", PAUSA_MIN))
        self.pausa_max_var = tk.DoubleVar(value=self.cfg.get("pausa_max", PAUSA_MAX))
        self.lim_err_var = tk.IntVar(value=self.cfg.get("lim_err", LIMITE_ERRORES))

        # manual range
        self.inicio_var = tk.StringVar(value=str(self.cfg.get("inicio",1)))
        self.fin_manual_var = tk.StringVar(value=str(self.cfg.get("fin_manual","")))

        # build UI
        self._build_ui()
        # start queue processor
        self.root.after(120, self._process_queue)

    def _build_ui(self):
        # top bar
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=8, pady=6)
        lbl = ttk.Label(top, text=f"🔵 {APP_NAME} — Dark Premium", font=("Segoe UI", 13, "bold"))
        lbl.pack(side="left")
        # save button
        btn_save = HoverButton(top, text="Guardar configuración", command=self._save_config)
        btn_save.pack(side="right", padx=6)

        # notebook (tabs)
        if THEME_AVAILABLE:
            nb = tb.Notebook(self.root)
        else:
            nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=10, pady=8)
        self.notebook = nb

        # build tabs
        tab_dl = ttk.Frame(nb)
        tab_cfg = ttk.Frame(nb)
        tab_logs = ttk.Frame(nb)
        tab_sound = ttk.Frame(nb)
        nb.add(tab_dl, text="Descarga", image=get_icon("download"), compound="left")
        nb.add(tab_cfg, text="Configuración", image=get_icon("gear"), compound="left")
        nb.add(tab_logs, text="Logs", image=get_icon("file-text"), compound="left")
        nb.add(tab_sound, text="Sonido", image=get_icon("volume-up"), compound="left")

        # ---- Descarga tab ----
        left = ttk.Frame(tab_dl)
        left.pack(side="left", fill="y", padx=8, pady=6)
        right = ttk.Frame(tab_dl)
        right.pack(side="right", fill="both", expand=True, padx=8, pady=6)

        # inputs: url, carpeta
        ttk.Label(left, text="URL base (sin número ni extensión):").pack(anchor="w", pady=(2,0))
        ent_url = ttk.Entry(left, textvariable=self.url_base, width=46)
        ent_url.pack(anchor="w", pady=4)
        # start/finish manual
        ttk.Label(left, text="Inicio (número):").pack(anchor="w", pady=(6,0))
        ttk.Entry(left, textvariable=self.inicio_var, width=8).pack(anchor="w", pady=2)
        ttk.Label(left, text="Cantidad (fin manual, opcional):").pack(anchor="w", pady=(6,0))
        ttk.Entry(left, textvariable=self.fin_manual_var, width=12).pack(anchor="w", pady=2)
        ttk.Label(left, text="(Si vacío, se usa detección o se te pedirá fin)").pack(anchor="w", pady=(0,6))

        ttk.Label(left, text="Carpeta destino:").pack(anchor="w", pady=(6,0))
        ttk.Entry(left, textvariable=self.carpeta, width=36).pack(anchor="w", pady=4)
        ttk.Button(left, text="Examinar...", command=self._browse_folder).pack(anchor="w")

        ttk.Label(left, text="Relleno (ceros):").pack(anchor="w", pady=(8,0))
        ttk.Spinbox(left, from_=1, to=8, textvariable=self.relleno, width=6).pack(anchor="w", pady=2)

        ttk.Label(left, text="Hilos (descarga):").pack(anchor="w", pady=(8,0))
        ttk.Spinbox(left, from_=1, to=200, textvariable=self.hilos, width=6).pack(anchor="w", pady=2)

        ttk.Label(left, text="Reintentos por archivo:").pack(anchor="w", pady=(8,0))
        ttk.Spinbox(left, from_=1, to=20, textvariable=self.reintentos, width=6).pack(anchor="w", pady=2)

        # action buttons
        bf = ttk.Frame(left)
        bf.pack(anchor="w", pady=12)
        self.btn_detect = HoverButton(bf, text="Detectar (mixta)", command=self._thread_detect)
        self.btn_detect.grid(row=0, column=0, padx=4)
        self.btn_start = HoverButton(bf, text="Iniciar descarga", command=self._thread_start)
        self.btn_start.grid(row=0, column=1, padx=4)
        self.btn_pause = HoverButton(bf, text="Pausar", command=self._pause, state="disabled")
        self.btn_pause.grid(row=0, column=2, padx=4)
        self.btn_stop = HoverButton(bf, text="Detener", command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=3, padx=4)

        ttk.Label(left, text="Separación: videos/  imágenes/").pack(anchor="w", pady=(8,0))
        ttk.Label(left, text="Logs: download.log.txt / download.log.jsonl").pack(anchor="w", pady=(2,0))

        # right: progress + log
        ttk.Label(right, text="Progreso total:").pack(anchor="w")
        self.pb_total = ttk.Progressbar(right, mode="determinate")
        self.pb_total.pack(fill="x", pady=6)
        ttk.Label(right, text="Progreso archivo:").pack(anchor="w")
        self.pb_file = ttk.Progressbar(right, mode="determinate")
        self.pb_file.pack(fill="x", pady=6)
        ttk.Label(right, text="Registro / Estado:").pack(anchor="w", pady=(8,0))
        self.txt_log = tk.Text(right, height=18, wrap="none", bg="#0f1115", fg="#eaf2ff")
        self.txt_log.pack(fill="both", expand=True, pady=4)

        # ---- Config tab ----
        ttk.Label(tab_cfg, text="Ajustes avanzados", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=8, pady=6)
        cfgf = ttk.Frame(tab_cfg)
        cfgf.pack(fill="both", expand=True, padx=8, pady=6)
        ttk.Label(cfgf, text="Max hilos detección (suave):").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Spinbox(cfgf, from_=1, to=20, textvariable=self.hilos_det_var, width=6).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(cfgf, text="Pausa mínima (s):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(cfgf, textvariable=self.pausa_min_var, width=8).grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(cfgf, text="Pausa máxima (s):").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(cfgf, textvariable=self.pausa_max_var, width=8).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(cfgf, text="Límite errores consecutivos:").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(cfgf, textvariable=self.lim_err_var, width=8).grid(row=3, column=1, sticky="w", padx=6)

        # ---- Logs tab ----
        ttk.Label(tab_logs, text="Logs", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=8, pady=6)
        lb = ttk.Frame(tab_logs)
        lb.pack(anchor="w", padx=8)
        ttk.Button(lb, text="Abrir carpeta destino", command=self._open_dest_folder).grid(row=0, column=0, padx=4)
        ttk.Button(lb, text="Abrir log.txt", command=self._open_log_txt).grid(row=0, column=1, padx=4)
        ttk.Button(lb, text="Abrir log.jsonl", command=self._open_log_json).grid(row=0, column=2, padx=4)
        ttk.Button(lb, text="Limpiar logs", command=self._clear_logs).grid(row=0, column=3, padx=4)
        self.log_preview = tk.Text(tab_logs, height=22, wrap="none", bg="#071019", fg="#9cf0ff")
        self.log_preview.pack(fill="both", expand=True, padx=8, pady=6)

        # ---- Sound tab ----
        ttk.Label(tab_sound, text="Sonidos UI", font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=8, pady=6)
        sf = ttk.Frame(tab_sound)
        sf.pack(fill="both", expand=True, padx=8, pady=6)
        ttk.Label(sf, text="Prueba los sonidos de la interfaz:").pack(anchor="w")
        ttk.Button(sf, text="Probar hover", command=lambda: play_ui("hover")).pack(anchor="w", pady=4)
        ttk.Button(sf, text="Probar click", command=lambda: play_ui("click")).pack(anchor="w", pady=4)
        ttk.Button(sf, text="Probar error", command=lambda: play_ui("err")).pack(anchor="w", pady=4)
        ttk.Button(sf, text="Probar done", command=lambda: play_ui("done")).pack(anchor="w", pady=4)
        ttk.Label(sf, text="(Los sonidos son generados localmente y suaves)").pack(anchor="w", pady=6)

    # -----------------------------
    # UI helpers
    # -----------------------------
    def _browse_folder(self):
        p = filedialog.askdirectory(initialdir=self.carpeta.get() or ".")
        if p:
            self.carpeta.set(p)

    def _open_dest_folder(self):
        path = self.carpeta.get()
        if os.path.exists(path):
            if sys.platform.startswith("win"):
                os.startfile(path)
            elif sys.platform == "darwin":
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        else:
            messagebox.showinfo("Info", "La carpeta aún no existe.")

    def _open_log_txt(self):
        p = os.path.join(self.carpeta.get(), "download.log.txt")
        if os.path.exists(p):
            if sys.platform.startswith("win"):
                os.startfile(p)
            else:
                os.system(f'xdg-open "{p}"')
        else:
            messagebox.showinfo("Info", "No existe download.log.txt todavía.")

    def _open_log_json(self):
        p = os.path.join(self.carpeta.get(), "download.log.jsonl")
        if os.path.exists(p):
            if sys.platform.startswith("win"):
                os.startfile(p)
            else:
                os.system(f'xdg-open "{p}"')
        else:
            messagebox.showinfo("Info", "No existe download.log.jsonl todavía.")

    def _clear_logs(self):
        base = self.carpeta.get()
        for p in (os.path.join(base, "download.log.txt"), os.path.join(base, "download.log.jsonl")):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except:
                pass
        messagebox.showinfo("Logs", "Logs eliminados.")
        self.log_preview.delete("1.0", "end")

    # -----------------------------
    # Queue processing (UI updates)
    # -----------------------------
    def _process_queue(self):
        while not self.queue.empty():
            item = self.queue.get_nowait()
            t = item.get("type")
            if t == "status":
                self._append_log(item.get("text"))
            elif t == "progress":
                try:
                    self.pb_total["maximum"] = item.get("max", int(self.pb_total["maximum"]) if self.pb_total["maximum"] else 1)
                except Exception:
                    self.pb_total["maximum"] = item.get("max", 1)
                self.pb_total["value"] = item.get("value", 0)
            elif t == "fileprogress":
                self.pb_file["value"] = item.get("value", 0)
            elif t == "detect":
                self.fin_detectado.set(item.get("value", 0))
                self._append_log(f"Detección estimada: {item.get('value',0)} archivos.")
        self.root.after(150, self._process_queue)

    def _append_log(self, text):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {text}"
        try:
            self.txt_log.insert("end", line + "\n")
            self.txt_log.see("end")
        except Exception:
            pass
        try:
            self.log_preview.insert("end", line + "\n")
            self.log_preview.see("end")
        except Exception:
            pass

    # -----------------------------
    # Actions: detect / start / pause / stop
    # -----------------------------
    def _thread_detect(self):
        url = self.url_base.get().strip()
        if not url:
            messagebox.showwarning("Falta URL", "Escribe la URL base primero.")
            return
        self.btn_detect["state"] = "disabled"
        self._append_log("Iniciando detección mixta...")
        threading.Thread(target=self._detect_background, daemon=True).start()

    def _detect_background(self):
        try:
            rell = int(self.relleno.get())
        except:
            rell = DEFAULT_RELLENO
        try:
            fin = detect_range_mixto(self.url_base.get().strip(), relleno=rell, max_busqueda=MAX_DETECT, quiet=False, hilos_det=self.hilos_det_var.get())
            self.queue.put({"type":"detect","value":fin})
            self.queue.put({"type":"status","text":f"Detección finalizada: {fin} archivos detectados (estimado)."})
        except Exception as e:
            self.queue.put({"type":"status","text":f"Error en detección: {e}"})
        finally:
            self.btn_detect["state"] = "normal"
            play_ui("click")

    def _thread_start(self):
        url = self.url_base.get().strip()
        carpeta = self.carpeta.get().strip() or "descargas"
        if not url:
            messagebox.showwarning("Falta URL", "Escribe la URL base primero.")
            return

        # parse inicio manual
        try:
            inicio = int(self.inicio_var.get())
            if inicio < 1:
                raise ValueError()
        except Exception:
            inicio = 1

        try:
            rell = int(self.relleno.get())
        except:
            rell = DEFAULT_RELLENO
        try:
            hilos = int(self.hilos.get())
        except:
            hilos = DEFAULT_HILOS
        try:
            reint = int(self.reintentos.get())
        except:
            reint = DEFAULT_REINTENTOS

        # decide fin:
        fin_manual_raw = self.fin_manual_var.get().strip()
        fin_final = None
        if fin_manual_raw:
            try:
                fin_candidate = int(fin_manual_raw)
                if fin_candidate >= inicio:
                    fin_final = fin_candidate
                    self._append_log(f"Usando FIN manual: {fin_final}")
                else:
                    messagebox.showwarning("Valor inválido", "El FIN manual debe ser mayor o igual que Inicio.")
                    play_ui("err")
                    return
            except Exception:
                messagebox.showwarning("Valor inválido", "FIN manual no es un número válido.")
                play_ui("err")
                return
        else:
            detected = self.fin_detectado.get()
            if detected > 0:
                fin_final = detected
                self._append_log(f"Usando FIN detectado: {fin_final}")
            else:
                fin_candidate = simpledialog.askinteger("Rango", "¿Hasta qué número quieres descargar?", initialvalue=100, minvalue=inicio)
                if not fin_candidate:
                    return
                fin_final = fin_candidate

        total = fin_final - inicio + 1
        if total < 1:
            messagebox.showwarning("Rango inválido", "El rango calculado es inválido (total < 1).")
            play_ui("err")
            return

        # ui prepare
        self.queue.put({"type":"progress","value":0,"max":total})
        self.btn_start["state"] = "disabled"
        self.btn_stop["state"] = "normal"
        self.btn_pause["state"] = "normal"
        self._append_log(f"Iniciando descargas {inicio}..{fin_final} en '{carpeta}' con {hilos} hilos...")

        # save config quick
        self.cfg.update({
            "url_base": self.url_base.get().strip(),
            "carpeta": carpeta,
            "relleno": rell,
            "hilos": hilos,
            "reintentos": reint
        })
        save_config(self.cfg)

        # start runner
        self.stop_event.clear()
        self.pause_event.clear()
        threading.Thread(target=self._run_downloads, args=(url, carpeta, inicio, fin_final, rell, hilos, reint), daemon=True).start()
        play_ui("click")

    def _pause(self):
        if not self.pause_event.is_set():
            self.pause_event.set()
            self.btn_pause["text"] = "Reanudar"
            self._append_log("Pausado por el usuario.")
            play_ui("click")
        else:
            self.pause_event.clear()
            self.btn_pause["text"] = "Pausar"
            self._append_log("Reanudar solicitud enviada.")
            play_ui("click")

    def _stop(self):
        self._append_log("Solicitud de detención enviada. Esperando tareas activas...")
        self.stop_event.set()
        self.btn_stop["state"] = "disabled"
        play_ui("click")

    def _run_downloads(self, url_base, carpeta, inicio, fin, relleno, hilos, reintentos):
        os.makedirs(carpeta, exist_ok=True)
        log_txt = os.path.join(carpeta, "download.log.txt")
        log_json = os.path.join(carpeta, "download.log.jsonl")
        bases = [f"{url_base}{str(i).zfill(relleno)}" for i in range(inicio, fin+1)]

        total = len(bases)
        completed = 0
        errores_seguidos = 0

        with ThreadPoolExecutor(max_workers=hilos) as ex:
            futures = {ex.submit(worker_job, b, carpeta, reintentos, self.pausa_min_var.get(), self.pausa_max_var.get()): b for b in bases}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception as e:
                    res = f"ERROR EXCEPCION: {e}\n{traceback.format_exc()}"

                completed += 1
                self.queue.put({"type":"progress","value":completed,"max":total})
                self.queue.put({"type":"status","text":res})

                if res.startswith("NOTFOUND") or "BLOCKED" in res or res.startswith("HTTP_"):
                    errores_seguidos += 1
                else:
                    errores_seguidos = 0

                if errores_seguidos >= self.lim_err_var.get():
                    self.queue.put({"type": "status", "text": f"⚠ Muchos errores seguidos ({errores_seguidos}) — pausa de emergencia {PAUSA_EMERGENCIA}s"})
                    time.sleep(PAUSA_EMERGENCIA)
                    errores_seguidos = 0

                while self.pause_event.is_set():
                    time.sleep(0.5)
                    if self.stop_event.is_set():
                        break

                if self.stop_event.is_set():
                    self._append_log("Detención solicitada: esperando a que terminen tareas activas.")
                    # continue looping until futures finish
                    pass

        append_log_txt(log_txt, "==== FIN DE SESIÓN ====")
        append_log_json(log_json, {"event":"finish","timestamp":time.time()})
        self.btn_start["state"] = "normal"
        self.btn_stop["state"] = "disabled"
        self.btn_pause["state"] = "disabled"
        self.queue.put({"type":"status","text":"Todas las tareas finalizadas."})
        play_ui("done")

    # -----------------------------
    # Save config
    # -----------------------------
    def _save_config(self):
        self.cfg.update({
            "url_base": self.url_base.get().strip(),
            "carpeta": self.carpeta.get().strip(),
            "relleno": int(self.relleno.get()),
            "hilos": int(self.hilos.get()),
            "reintentos": int(self.reintentos.get()),
            "hilos_det": int(self.hilos_det_var.get()),
            "pausa_min": float(self.pausa_min_var.get()),
            "pausa_max": float(self.pausa_max_var.get()),
            "lim_err": int(self.lim_err_var.get()),
            "inicio": int(self.inicio_var.get()) if self.inicio_var.get().strip().isdigit() else 1,
            "fin_manual": self.fin_manual_var.get().strip()
        })
        save_config(self.cfg)
        messagebox.showinfo("Configuración", "Configuración guardada.")
        play_ui("click")

# -----------------------------
# Entrypoint
# -----------------------------
def main():
    if THEME_AVAILABLE:
        app = App()
        # If using tb.Window, the mainloop is app.mainloop()
        app.mainloop()
    else:
        root = tk.Tk()
        app = App(root)
        root.mainloop()

if __name__ == "__main__":
    main()
