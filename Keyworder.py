"""
Keyworder — Adobe Stock keyword generator
GUI · Anthropic Claude · 49 keywords · batch processing
Double safety: prompt rules + post-processing stop-word filter
"""
import warnings; warnings.filterwarnings("ignore")
import os, sys, json, csv, base64, subprocess, shutil, tempfile
import threading, time, io, re, multiprocessing, logging
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image
import anthropic

# ── Frozen-app path ───────────────────────────────────────────────────────────
def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent

CONFIG_PATH = app_dir() / "settings.json"
LOG_PATH    = app_dir() / "keyworder.log"
logging.basicConfig(filename=str(LOG_PATH), filemode="a",
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

# ── Constants ─────────────────────────────────────────────────────────────────
IMG_EXT    = {".jpg",".jpeg",".png",".webp",".gif",".tiff",".bmp"}
VID_EXT    = {".mp4",".mov",".avi",".mkv",".mxf",".wmv"}
KW_COUNT   = 49
BATCH_SIZE = 8
VID_FRAMES = 3

# ══════════════════════════════════════════════════════════════════════════════
#  STOP-WORDS  (двойная защита от copyright)
# ══════════════════════════════════════════════════════════════════════════════
STOP_WORDS_TITLE = {
    # tech brands
    "apple","iphone","ipad","macbook","mac","imac","airpods","android","google",
    "samsung","microsoft","windows","xbox","sony","playstation","intel","nvidia",
    "adobe","photoshop","instagram","facebook","twitter","youtube","tiktok",
    "snapchat","linkedin","whatsapp","telegram","amazon","netflix","spotify",
    # fashion / lifestyle brands
    "gucci","prada","chanel","louis vuitton","rolex","nike","adidas","zara",
    "h&m","ikea","starbucks","mcdonalds","coca cola","pepsi","tesla",
    # financial benchmarks
    "nasdaq","dow jones","s&p","ftse","brent","wti","bitcoin","ethereum",
    # cities / landmarks
    "new york","london","paris","tokyo","moscow","dubai","eiffel","colosseum",
    "times square","wall street","broadway","hollywood",
    # entertainment
    "disney","pixar","marvel","dc","lego","barbie","pokemon","minecraft",
    # artist styles / names — generic block via regex
}

STOP_WORDS_KEYWORDS = STOP_WORDS_TITLE | {
    # extra keyword-level blocks
    "iphone","macbook","photoshop","instagram","facebook","twitter","youtube",
    "tiktok","snapchat","whatsapp","amazon","netflix","spotify","tesla","uber",
    "airbnb","google","android","windows","xbox","playstation","nintendo",
    "coca-cola","pepsi","starbucks","mcdonalds","ikea","zara","h&m",
    "gucci","prada","chanel","nike","adidas","rolex","lego","disney","pixar",
    "marvel","dc comics","pokemon","minecraft","fortnite",
    "nasdaq","dow","s&p500","brent","wti","ethereum","bitcoin",
}

def _clean(text, stopset):
    """Remove stop-words from a string (case-insensitive whole-word match)."""
    for sw in stopset:
        # escape and match whole word / phrase
        pattern = r'(?<![a-z])' + re.escape(sw) + r'(?![a-z])'
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    # collapse extra spaces / commas
    text = re.sub(r'\s{2,}', ' ', text).strip().strip(',').strip()
    return text

def clean_title(title):
    return _clean(title, STOP_WORDS_TITLE)

def clean_keywords(kw_list):
    result = []
    seen = set()
    for kw in kw_list:
        kw = _clean(kw, STOP_WORDS_KEYWORDS).strip().strip(',')
        if kw and kw.lower() not in seen and len(kw) > 1:
            seen.add(kw.lower())
            result.append(kw)
    return result

# ══════════════════════════════════════════════════════════════════════════════
#  FFMPEG DISCOVERY
# ══════════════════════════════════════════════════════════════════════════════
FFMPEG_HINTS = [
    r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    r"C:\tools\ffmpeg\bin\ffmpeg.exe", r"C:\tools\ffmpeg.exe",
    r"C:\Windows\System32\ffmpeg.exe", r"C:\Windows\ffmpeg.exe",
    r"D:\ffmpeg\bin\ffmpeg.exe", r"D:\ffmpeg\ffmpeg.exe",
]

def find_ffmpeg(saved=""):
    bundled = app_dir() / "ffmpeg.exe"
    if bundled.exists(): return str(bundled)
    if saved and os.path.isfile(saved): return saved
    found = shutil.which("ffmpeg")
    if found: return found
    for p in FFMPEG_HINTS:
        if os.path.isfile(p): return p
    for drive in ["C:\\","D:\\"]:
        try:
            for root, dirs, files in os.walk(drive):
                if "ffmpeg.exe" in files: return os.path.join(root,"ffmpeg.exe")
                dirs[:] = [d for d in dirs
                           if d.lower() not in {"windows","system32","$recycle.bin",
                                                 "system volume information","programdata"}
                           and root.count(os.sep) < 6]
        except Exception:
            pass
    return None

# ══════════════════════════════════════════════════════════════════════════════
#  IMAGE / VIDEO HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def img_to_b64(path, max_px=1024):
    with Image.open(path) as img:
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img = img.convert("RGB")
        w, h = img.size
        if max(w,h) > max_px:
            r = max_px / max(w,h)
            img = img.resize((max(1,int(w*r)), max(1,int(h*r))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"

def extract_frames(video_path, ffmpeg_bin, n=VID_FRAMES):
    tmp, frames = tempfile.mkdtemp(), []
    try:
        ffprobe = ffmpeg_bin.replace("ffmpeg.exe","ffprobe.exe")
        if not os.path.isfile(ffprobe): ffprobe = "ffprobe"
        res = subprocess.run(
            [ffprobe,"-v","error","-show_entries","format=duration",
             "-of","default=noprint_wrappers=1:nokey=1",str(video_path)],
            capture_output=True, text=True, timeout=15)
        dur = float(res.stdout.strip() or "10")
    except Exception:
        dur = 10.0
    for i, t in enumerate([dur*j/(n+1) for j in range(1,n+1)]):
        out = os.path.join(tmp, f"f{i}.jpg")
        try:
            subprocess.run([ffmpeg_bin,"-ss",str(t),"-i",str(video_path),
                "-frames:v","1","-q:v","2",out,"-y","-loglevel","error"],
                capture_output=True, timeout=20)
            if os.path.exists(out): frames.append(out)
        except Exception:
            pass
    return frames, tmp

# ══════════════════════════════════════════════════════════════════════════════
#  PROMPT  (строгие правила copyright внутри промпта)
# ══════════════════════════════════════════════════════════════════════════════
SYSTEM_RULES = """You are a professional Adobe Stock keywording expert.

TITLE COPYRIGHT RULE: All metadata MUST be 100% safe for Commercial Stock Photography.
The title MUST NOT contain any brand names, trademarks, company names, copyrighted characters,
specific product models, industry benchmarks/standards, city names, or landmark names.
Use neutral, generic, descriptive wording only.

KEYWORD COPYRIGHT RULE: ABSOLUTELY FORBIDDEN in keywords:
brand names, trademarks, specific product models, identifiable proprietary names,
industry benchmarks (e.g. WTI, Brent, Nasdaq, S&P), specific city names, landmark names,
personal proper nouns, first names, last names, artist names, author styles,
and any word from this list:
iphone, macbook, android, google, samsung, microsoft, adobe, photoshop,
instagram, facebook, twitter, youtube, tiktok, amazon, netflix, spotify,
tesla, uber, airbnb, coca-cola, pepsi, starbucks, mcdonalds, ikea, zara,
gucci, prada, chanel, nike, adidas, rolex, lego, disney, pixar, marvel,
pokemon, minecraft, bitcoin, ethereum, nasdaq, dow, brent, wti.

Violation of these rules makes the file unsellable on Adobe Stock."""

def make_prompt(names, topic, prepend, append):
    lines = "\n".join(f"  [{i+1}] {n}" for i,n in enumerate(names))
    ctx  = f"\nCollection theme: {topic}" if topic.strip() else ""
    pre  = f"\nAdd these keywords right after position 5: {prepend}" if prepend.strip() else ""
    app_ = f"\nAdd these keywords at the very end: {append}"         if append.strip()  else ""
    return f"""Analyze the {len(names)} media file(s) below.
Generate Adobe Stock metadata in English.{ctx}{pre}{app_}

Files:
{lines}

RULES:
1. Exactly {KW_COUNT} keywords per file — all English.
2. FIRST 5 keywords: highest commercial buyer-intent search terms.
   Think like a real buyer on Adobe Stock.
   Example good top-5: "business team meeting", "happy family outdoors",
   "aerial city skyline sunset", "young woman working laptop", "fresh healthy food".
3. Place prepend keywords right after position 5 (if given).
4. Place append keywords at the very end (if given).
5. Fill remaining slots: subjects, colors, mood, composition, setting,
   emotions, actions, demographics, style, abstract concepts.
6. Single words or short 2-word phrases only. No duplicates. No punctuation.
7. Title: max 70 chars, natural English, start with main subject.
8. Follow ALL copyright rules from the system instructions — no brands, no cities, no trademarks.

Return ONLY a raw JSON array — no markdown, no preamble:
[
  {{"index":1,"title":"...","keywords":["kw1","kw2",...]}},
  ...
]"""

# ══════════════════════════════════════════════════════════════════════════════
#  API CALL
# ══════════════════════════════════════════════════════════════════════════════
def call_claude(client, batch, topic, prepend, append, log_fn):
    content, names = [], []
    for item in batch:
        for b64,mt in item["images"]:
            content.append({"type":"image","source":{"type":"base64","media_type":mt,"data":b64}})
        content.append({"type":"text","text":f"[File: {item['name']}]"})
        names.append(item["name"])
    content.append({"type":"text","text":make_prompt(names,topic,prepend,append)})

    for attempt in range(3):
        try:
            resp = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=min(8000, len(batch)*KW_COUNT*14+500),
                system=SYSTEM_RULES,
                messages=[{"role":"user","content":content}])
            raw = "".join(b.text for b in resp.content if hasattr(b,"text"))
            raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log_fn(f"  ⚠ JSON error (attempt {attempt+1}): {e}")
            if attempt==2: return []
            time.sleep(2)
        except anthropic.RateLimitError:
            w=20*(attempt+1); log_fn(f"  ⚠ Rate limit — ждём {w}с…"); time.sleep(w)
        except Exception as e:
            log_fn(f"  ⚠ API error (attempt {attempt+1}): {e}")
            if attempt==2: return []
            time.sleep(5)
    return []

# ══════════════════════════════════════════════════════════════════════════════
#  GUI
# ══════════════════════════════════════════════════════════════════════════════
class App(tk.Tk):
    BG    = "#F2F2F7"
    WHITE = "#FFFFFF"
    BDR   = "#D1D1D6"
    ACC   = "#5856D6"
    ACC2  = "#3634A3"
    FG    = "#1C1C1E"
    FG2   = "#6C6C70"
    FG3   = "#C7C7CC"
    GREEN = "#34C759"
    AMBER = "#FF9500"
    SEL   = "#EEEEFF"

    def __init__(self):
        super().__init__()
        self.title("Keyworder — Adobe Stock")
        self.configure(bg=self.BG)
        self.resizable(False, False)

        self.cfg = self._load_cfg()
        self.v_key     = tk.StringVar(value=self.cfg.get("api_key",""))
        self.v_folder  = tk.StringVar(value=self.cfg.get("folder",""))
        self.v_topic   = tk.StringVar(value=self.cfg.get("topic",""))
        self.v_prepend = tk.StringVar(value=self.cfg.get("prepend",""))
        self.v_append  = tk.StringVar(value=self.cfg.get("append",""))
        self.v_csvname = tk.StringVar(value=self.cfg.get("csvname","keywords"))
        self.v_mode    = tk.StringVar(value=self.cfg.get("mode","both"))
        self.v_status  = tk.StringVar(value="Готово к работе")
        self.ffmpeg    = None
        self.running   = False
        self._show_key = False

        self._build()
        self.after(200, self._init_ffmpeg)

    def _load_cfg(self):
        try:
            if CONFIG_PATH.exists():
                return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception: pass
        return {}

    def _save_cfg(self):
        CONFIG_PATH.write_text(
            json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── FFmpeg ────────────────────────────────────────────────────────────────
    def _init_ffmpeg(self):
        self.ffmpeg = find_ffmpeg(self.cfg.get("ffmpeg",""))
        if self.ffmpeg:
            self.cfg["ffmpeg"] = self.ffmpeg; self._save_cfg()
            self.ff_lbl.config(text="✓  FFmpeg найден", fg=self.GREEN, cursor="arrow")
        else:
            self.ff_lbl.config(
                text="⚠  FFmpeg не найден — нажмите чтобы указать",
                fg=self.AMBER, cursor="hand2")

    def _pick_ffmpeg(self, *_):
        if self.ff_lbl.cget("cursor") != "hand2": return
        p = filedialog.askopenfilename(title="Укажите ffmpeg.exe",
            filetypes=[("ffmpeg.exe","ffmpeg.exe"),("All","*")])
        if p and os.path.isfile(p):
            self.ffmpeg = p; self.cfg["ffmpeg"] = p; self._save_cfg()
            self.ff_lbl.config(text="✓  FFmpeg найден", fg=self.GREEN, cursor="arrow")

    # ── Widget helpers ────────────────────────────────────────────────────────
    def _card(self, parent, pady=(0,0)):
        f = tk.Frame(parent, bg=self.WHITE,
                     highlightthickness=1, highlightbackground=self.BDR)
        f.pack(fill="x", padx=16, pady=pady)
        inner = tk.Frame(f, bg=self.WHITE)
        inner.pack(fill="x", padx=14, pady=10)
        return inner

    def _entry(self, parent, var, show=None, readonly=False, width=None):
        kw = dict(textvariable=var, font=("Segoe UI",10),
                  bg="#FAFAFA", fg=self.FG, insertbackground=self.FG,
                  relief="flat", bd=0,
                  highlightthickness=1, highlightbackground=self.BDR,
                  highlightcolor=self.ACC)
        if show:     kw["show"] = show
        if readonly: kw.update(state="readonly", readonlybackground="#FAFAFA")
        if width:    kw["width"] = width
        return tk.Entry(parent, **kw)

    def _section_title(self, parent, text):
        tk.Label(parent, text=text, font=("Segoe UI",8,"bold"),
                 bg=self.BG, fg=self.FG3).pack(anchor="w", padx=18, pady=(12,2))

    def _divider(self, parent):
        tk.Frame(parent, bg=self.BDR, height=1).pack(fill="x", pady=(4,8))

    # ── Build ─────────────────────────────────────────────────────────────────
    def _build(self):
        self.geometry("500x840")

        # Header
        hdr = tk.Frame(self, bg=self.ACC)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Keyworder", font=("Segoe UI",14,"bold"),
                 bg=self.ACC, fg="#fff").pack(side="left", padx=16, pady=12)
        tk.Label(hdr, text="Adobe Stock · 49 keywords · ©safe",
                 font=("Segoe UI",9), bg=self.ACC, fg="#ccc8ff").pack(side="left")
        self.ff_lbl = tk.Label(hdr, text="Проверка FFmpeg…",
                               font=("Segoe UI",8), bg=self.ACC, fg="#fff")
        self.ff_lbl.pack(side="right", padx=12)
        self.ff_lbl.bind("<Button-1>", self._pick_ffmpeg)

        # Scrollable body
        outer = tk.Frame(self, bg=self.BG); outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=self.BG, highlightthickness=0, bd=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y"); canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=self.BG)
        cw = canvas.create_window((0,0), window=body, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(cw, width=e.width))
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1*(e.delta/120)),"units"))

        # ── API Key ──
        self._section_title(body, "API КЛЮЧ")
        c1 = self._card(body)
        tk.Label(c1, text="Anthropic API ключ", font=("Segoe UI",10,"bold"),
                 bg=self.WHITE, fg=self.FG).pack(anchor="w")
        tk.Label(c1, text="Хранится локально · передаётся только в Anthropic API",
                 font=("Segoe UI",9), bg=self.WHITE, fg=self.FG2).pack(anchor="w", pady=(1,6))
        r1 = tk.Frame(c1, bg=self.WHITE); r1.pack(fill="x")
        self.key_entry = self._entry(r1, self.v_key, show="•")
        self.key_entry.pack(side="left", fill="x", expand=True, ipady=7, ipadx=8)
        def tog_key():
            self._show_key = not self._show_key
            self.key_entry.config(show="" if self._show_key else "•")
            kb.config(text="Скрыть" if self._show_key else "Показать")
        kb = tk.Button(r1, text="Показать", font=("Segoe UI",9),
                       bg=self.BG, fg=self.FG2, relief="flat", bd=0, cursor="hand2",
                       highlightthickness=1, highlightbackground=self.BDR, command=tog_key)
        kb.pack(side="left", padx=(6,0), ipady=5, ipadx=10)
        tk.Button(c1, text="Где взять ключ →", font=("Segoe UI",9),
                  bg=self.WHITE, fg=self.ACC, relief="flat", bd=0,
                  cursor="hand2", command=self._api_help).pack(anchor="w", pady=(6,0))

        # ── Folder ──
        self._section_title(body, "ПАПКА С ФАЙЛАМИ")
        c2 = self._card(body)
        tk.Label(c2, text="Исходная папка", font=("Segoe UI",10,"bold"),
                 bg=self.WHITE, fg=self.FG).pack(anchor="w")
        tk.Label(c2, text="Все поддерживаемые файлы внутри будут обработаны",
                 font=("Segoe UI",9), bg=self.WHITE, fg=self.FG2).pack(anchor="w", pady=(1,6))
        r2 = tk.Frame(c2, bg=self.WHITE); r2.pack(fill="x")
        self._entry(r2, self.v_folder, readonly=True).pack(
            side="left", fill="x", expand=True, ipady=7, ipadx=8)
        tk.Button(r2, text="Выбрать…", font=("Segoe UI",9,"bold"),
                  bg=self.ACC, fg="#fff", activebackground=self.ACC2,
                  activeforeground="#fff", relief="flat", bd=0, cursor="hand2",
                  command=self._browse).pack(side="left", padx=(6,0), ipady=5, ipadx=12)

        # ── Mode ──
        self._section_title(body, "ТИП ФАЙЛОВ")
        c3 = self._card(body)
        tk.Label(c3, text="Что обрабатывать из папки",
                 font=("Segoe UI",10,"bold"), bg=self.WHITE, fg=self.FG).pack(anchor="w", pady=(0,6))
        r3 = tk.Frame(c3, bg=self.WHITE); r3.pack(anchor="w")
        self._mbts = {}
        for val, lbl in [("photo","🖼  Фото"),("video","🎬  Видео"),("both","📦  Фото + Видео")]:
            b = tk.Label(r3, text=lbl, font=("Segoe UI",9), bg=self.BG, fg=self.FG2,
                         cursor="hand2", padx=12, pady=5,
                         highlightthickness=1, highlightbackground=self.BDR)
            b.pack(side="left", padx=(0,6))
            b.bind("<Button-1>", lambda e,v=val: self._set_mode(v))
            self._mbts[val] = b
        self._set_mode(self.v_mode.get())

        # ── Settings (all in one card) ──
        self._section_title(body, "НАСТРОЙКИ")
        c4 = self._card(body)

        tk.Label(c4, text="Тематика коллекции", font=("Segoe UI",10,"bold"),
                 bg=self.WHITE, fg=self.FG).pack(anchor="w")
        tk.Label(c4, text="Повышает точность и релевантность ключевых слов",
                 font=("Segoe UI",9), bg=self.WHITE, fg=self.FG2).pack(anchor="w", pady=(1,4))
        self._entry(c4, self.v_topic).pack(fill="x", ipady=6, ipadx=8)
        tk.Label(c4, text="Например: альтернативная энергия, городской бизнес, природа…",
                 font=("Segoe UI",8), bg=self.WHITE, fg=self.FG3).pack(anchor="w", pady=(3,0))

        self._divider(c4)

        tk.Label(c4, text="Кастомные ключевые слова", font=("Segoe UI",10,"bold"),
                 bg=self.WHITE, fg=self.FG).pack(anchor="w")
        tk.Label(c4, text="В начало (после топ-5 коммерческих):",
                 font=("Segoe UI",9), bg=self.WHITE, fg=self.FG2).pack(anchor="w", pady=(4,2))
        self._entry(c4, self.v_prepend).pack(fill="x", ipady=6, ipadx=8)
        tk.Label(c4, text="Например: kitten, cute, adorable",
                 font=("Segoe UI",8), bg=self.WHITE, fg=self.FG3).pack(anchor="w", pady=(3,8))
        tk.Label(c4, text="В конец списка:",
                 font=("Segoe UI",9), bg=self.WHITE, fg=self.FG2).pack(anchor="w", pady=(0,2))
        self._entry(c4, self.v_append).pack(fill="x", ipady=6, ipadx=8)
        tk.Label(c4, text="Например: art, design, vector",
                 font=("Segoe UI",8), bg=self.WHITE, fg=self.FG3).pack(anchor="w", pady=(3,0))

        self._divider(c4)

        tk.Label(c4, text="Имя CSV файла", font=("Segoe UI",10,"bold"),
                 bg=self.WHITE, fg=self.FG).pack(anchor="w")
        tk.Label(c4, text="Сохранится в выбранной папке (Adobe Stock + StockSubmitter форматы)",
                 font=("Segoe UI",9), bg=self.WHITE, fg=self.FG2).pack(anchor="w", pady=(1,4))
        rcsv = tk.Frame(c4, bg=self.WHITE); rcsv.pack(anchor="w")
        self._entry(rcsv, self.v_csvname, width=22).pack(side="left", ipady=6, ipadx=8)
        tk.Label(rcsv, text=".csv", font=("Segoe UI",10),
                 bg=self.WHITE, fg=self.FG2).pack(side="left", padx=4)

        # ── Start button ──
        tk.Frame(body, bg=self.BG, height=8).pack()
        bf = tk.Frame(body, bg=self.BG); bf.pack(fill="x", padx=16)
        self.start_btn = tk.Button(
            bf, text="▶   Генерировать ключевые слова",
            font=("Segoe UI",11,"bold"),
            bg=self.ACC, fg="#fff", activebackground=self.ACC2,
            activeforeground="#fff", relief="flat", bd=0,
            cursor="hand2", command=self._start)
        self.start_btn.pack(fill="x", ipady=13)

        # ── Progress ──
        pf = tk.Frame(body, bg=self.BG); pf.pack(fill="x", padx=16, pady=(8,0))
        sty = ttk.Style(); sty.theme_use("default")
        sty.configure("K.Horizontal.TProgressbar",
                       troughcolor="#E5E5EA", background=self.ACC,
                       bordercolor="#E5E5EA", lightcolor=self.ACC, darkcolor=self.ACC)
        self.progress = ttk.Progressbar(pf, style="K.Horizontal.TProgressbar",
                                        mode="determinate")
        self.progress.pack(fill="x", ipady=2)
        sr = tk.Frame(body, bg=self.BG); sr.pack(fill="x", padx=16, pady=(3,0))
        self.status_lbl = tk.Label(sr, textvariable=self.v_status,
                                   font=("Segoe UI",9), bg=self.BG, fg=self.FG2, anchor="w")
        self.status_lbl.pack(side="left")
        self.count_lbl = tk.Label(sr, text="", font=("Segoe UI",9),
                                  bg=self.BG, fg=self.FG3, anchor="e")
        self.count_lbl.pack(side="right")

        # ── Log ──
        lf = tk.Frame(body, bg=self.BG); lf.pack(fill="x", padx=16, pady=(10,16))
        tk.Label(lf, text="ЛОГ", font=("Segoe UI",8,"bold"),
                 bg=self.BG, fg=self.FG3).pack(anchor="w", pady=(0,3))
        log_frame = tk.Frame(lf, bg=self.WHITE,
                             highlightthickness=1, highlightbackground=self.BDR)
        log_frame.pack(fill="x")
        self.log_box = tk.Text(log_frame, height=8, font=("Consolas",9),
                               bg=self.WHITE, fg="#333", relief="flat", bd=0,
                               state="disabled", wrap="word", padx=8, pady=6)
        lsb = tk.Scrollbar(log_frame, command=self.log_box.yview)
        self.log_box.config(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.log_box.pack(side="left", fill="x", expand=True)

    # ── Mode ──────────────────────────────────────────────────────────────────
    def _set_mode(self, mode):
        self.v_mode.set(mode)
        for val, btn in self._mbts.items():
            if val==mode: btn.config(bg=self.SEL, fg=self.ACC, highlightbackground=self.ACC)
            else:         btn.config(bg=self.BG,  fg=self.FG2, highlightbackground=self.BDR)

    def _browse(self):
        p = filedialog.askdirectory(title="Выберите папку с файлами")
        if p: self.v_folder.set(p)

    def _log(self, text):
        self.log_box.config(state="normal")
        self.log_box.insert("end", text+"\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        logging.info(text)

    def _api_help(self):
        w = tk.Toplevel(self); w.title("Где взять API ключ")
        w.configure(bg=self.WHITE); w.geometry("460x290"); w.resizable(False,False)
        txt = tk.Text(w, font=("Segoe UI",10), bg=self.WHITE, fg=self.FG,
                      relief="flat", bd=0, wrap="word", padx=16, pady=16)
        txt.pack(fill="both", expand=True)
        txt.insert("end",
"""Как получить Anthropic API ключ

1. Откройте  https://console.anthropic.com

2. Зарегистрируйтесь или войдите (бесплатно).

3. Левое меню → API Keys → Create Key.

4. Дайте любое название → скопируйте ключ.

5. Вставьте в поле приложения.
   Ключ хранится только на вашем ПК.

────────────────────────────────
Стоимость: ~$0.003 за фото · 100 фото ≈ $0.03
При регистрации есть бесплатные кредиты.""")
        txt.config(state="disabled")

    # ── Start ─────────────────────────────────────────────────────────────────
    def _start(self):
        if self.running: return
        key    = self.v_key.get().strip()
        folder = self.v_folder.get().strip()
        if not key:    messagebox.showerror("Нет ключа","Введите Anthropic API ключ."); return
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Нет папки","Выберите папку с файлами."); return

        self.cfg.update({"api_key":key,"folder":folder,
                         "topic":self.v_topic.get(),"prepend":self.v_prepend.get(),
                         "append":self.v_append.get(),"csvname":self.v_csvname.get(),
                         "mode":self.v_mode.get()})
        self._save_cfg()
        self.running = True
        self.start_btn.config(state="disabled", text="⏳  Обработка…")
        self.log_box.config(state="normal"); self.log_box.delete("1.0","end")
        self.log_box.config(state="disabled")
        self.progress["value"] = 0
        self.v_status.set("Запуск…")

        threading.Thread(
            target=self._run,
            args=(key, folder, self.v_mode.get(),
                  self.v_topic.get(), self.v_prepend.get(),
                  self.v_append.get(), self.v_csvname.get(), self.ffmpeg),
            daemon=True).start()

    def _run(self, *args):
        try:    self._process(*args)
        except Exception as e:
            self.after(0, self._log, f"❌ Критическая ошибка: {e}")
            logging.exception("Fatal")
        finally: self.after(0, self._finish)

    def _process(self, key, folder, mode, topic, prepend, append, csvname, ffmpeg_bin):
        client = anthropic.Anthropic(api_key=key)
        fp = Path(folder)

        all_f = sorted(fp.iterdir())
        if   mode=="photo": files=[f for f in all_f if f.suffix.lower() in IMG_EXT]
        elif mode=="video": files=[f for f in all_f if f.suffix.lower() in VID_EXT]
        else:               files=[f for f in all_f if f.suffix.lower() in IMG_EXT|VID_EXT]

        if not files:
            self.after(0,self._log,"❌ Нет поддерживаемых файлов в папке."); return

        imgs_n = sum(1 for f in files if f.suffix.lower() in IMG_EXT)
        vids_n = sum(1 for f in files if f.suffix.lower() in VID_EXT)
        self.after(0,self._log,f"Найдено: {imgs_n} фото, {vids_n} видео → {len(files)} файлов")
        if topic:   self.after(0,self._log,f"Тематика: {topic}")
        if prepend: self.after(0,self._log,f"В начало: {prepend}")
        if append:  self.after(0,self._log,f"В конец:  {append}")
        self.after(0,self._log,"")

        work, tmps = [], []
        self.after(0,self.v_status.set,"Загрузка файлов…")

        for i,f in enumerate(files):
            self.after(0,self.count_lbl.config,{"text":f"Загрузка {i+1}/{len(files)}"})
            is_vid = f.suffix.lower() in VID_EXT
            if is_vid:
                if not ffmpeg_bin:
                    self.after(0,self._log,f"  ⏭ Пропущено (нет FFmpeg): {f.name}"); continue
                self.after(0,self._log,f"  🎬 Кадры: {f.name}")
                frames, tmp = extract_frames(f, ffmpeg_bin)
                tmps.append(tmp)
                if not frames:
                    self.after(0,self._log,f"  ⚠ Не удалось извлечь кадры: {f.name}"); continue
                imgs=[]
                for fr in frames:
                    try: imgs.append(img_to_b64(fr))
                    except Exception as e: self.after(0,self._log,f"  ⚠ {e}")
                if imgs: work.append({"name":f.name,"images":imgs})
            else:
                try:
                    b64,mt = img_to_b64(f)
                    work.append({"name":f.name,"images":[(b64,mt)]})
                except Exception as e:
                    self.after(0,self._log,f"  ⚠ {f.name}: {e}")

        if not work:
            self.after(0,self._log,"❌ Нечего обрабатывать."); return

        total   = len(work)
        batches = [work[i:i+BATCH_SIZE] for i in range(0,total,BATCH_SIZE)]
        self.after(0,self._log,f"🚀 {total} файлов → {len(batches)} пакет(ов)\n")

        results, done = [], 0
        for bi,batch in enumerate(batches):
            self.after(0,self.v_status.set,f"Пакет {bi+1}/{len(batches)} — AI анализирует…")
            self.after(0,self.count_lbl.config,{"text":f"{done}/{total} готово"})
            self.after(0,self._log,
                f"━━ Пакет {bi+1}/{len(batches)}: {', '.join(x['name'] for x in batch)}")

            res = call_claude(client,batch,topic,prepend,append,
                              lambda m: self.after(0,self._log,m))

            for idx,item in enumerate(batch):
                matched = next((r for r in res if r.get("index",0)-1==idx),None)
                if matched is None and idx<len(res): matched=res[idx]

                raw_kws   = (matched.get("keywords",[]) if matched else [])
                raw_title = (matched.get("title","")    if matched else "")

                # ── Double safety: post-process stop-words ──
                title = clean_title(raw_title)
                kws   = clean_keywords(raw_kws)[:KW_COUNT]

                results.append({"filename":item["name"],"title":title,"keywords":kws})
                if matched:
                    self.after(0,self._log,
                        f"  ✓ {item['name']} — {len(kws)} kw | топ-5: {', '.join(kws[:5])}")
                else:
                    self.after(0,self._log,f"  ⚠ Нет ответа для {item['name']}")

            done+=len(batch)
            self.after(0,lambda p=int(done/total*100): self.progress.configure(value=p))
            if bi<len(batches)-1: time.sleep(1.0)

        for d in tmps:
            try: shutil.rmtree(d,ignore_errors=True)
            except: pass

        safe = "".join(c for c in (csvname or "keywords") if c.isalnum() or c in "_- ") or "keywords"

        # Adobe Stock CSV
        adobe = fp / f"{safe}.csv"
        with open(adobe,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f)
            w.writerow(["Filename","Title","Keywords","Category","Releases"])
            for r in results:
                w.writerow([r["filename"],r["title"],", ".join(r["keywords"]),"",""])

        # StockSubmitter CSV
        ss = fp / f"{safe}_stocksubmitter.csv"
        with open(ss,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f)
            w.writerow(["Filename","Title","Description","Keywords","Release name"])
            for r in results:
                w.writerow([r["filename"],r["title"],r["title"],", ".join(r["keywords"]),""])

        self.after(0,self._log,
            f"\n✅ Готово! {len(results)} файлов."
            f"\n📄 Adobe Stock:       {adobe}"
            f"\n📄 StockSubmitter:    {ss}")
        self.after(0,self.v_status.set,f"✅ Готово — {len(results)} файлов")
        self.after(0,self.count_lbl.config,{"text":f"{done}/{total}"})
        self.after(0,lambda: messagebox.showinfo("Готово!",
            f"Обработано: {len(results)} файлов\n\n"
            f"Adobe Stock CSV:\n{adobe}\n\n"
            f"StockSubmitter CSV:\n{ss}"))

    def _finish(self):
        self.running=False
        self.start_btn.config(state="normal",text="▶   Генерировать ключевые слова")

if __name__=="__main__":
    multiprocessing.freeze_support()
    App().mainloop()
