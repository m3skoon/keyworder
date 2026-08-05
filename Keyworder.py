"""
Keyworder — Adobe Stock keyword generator
GUI · Anthropic Claude · 49 keywords · batch processing
"""
import warnings; warnings.filterwarnings("ignore")
import os, sys, json, csv, base64, subprocess, shutil, tempfile
import threading, time, io, re, multiprocessing, logging
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image
import anthropic

def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent

CONFIG_PATH = app_dir() / "settings.json"
LOG_PATH    = app_dir() / "keyworder.log"

logging.basicConfig(filename=str(LOG_PATH), filemode="a",
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)

IMG_EXT    = {".jpg",".jpeg",".png",".webp",".gif",".tiff",".bmp"}
VID_EXT    = {".mp4",".mov",".avi",".mkv",".mxf",".wmv"}
KW_COUNT   = 49
BATCH_SIZE = 8
VID_FRAMES = 3

# ── Стоп-слова — только бренды и имена ───────────────────────────────────────
STOP_WORDS = {
    # Tech brands
    "apple","iphone","ipad","macbook","android","samsung","microsoft",
    "windows","xbox","playstation","nintendo","adobe","photoshop",
    "instagram","facebook","twitter","youtube","tiktok","snapchat",
    "whatsapp","linkedin","amazon","netflix","spotify","google",
    # Fashion/lifestyle brands
    "gucci","prada","chanel","rolex","nike","adidas","zara","ikea",
    "starbucks","mcdonalds","coca-cola","pepsi","tesla","uber","airbnb",
    "lego","barbie","disney","pixar","marvel","pokemon","minecraft",
    # Financial benchmarks
    "nasdaq","brent","wti","ethereum","bitcoin",
}

def _clean(text, stopset):
    for sw in stopset:
        text = re.sub(r'(?<![a-z])'+re.escape(sw)+r'(?![a-z])', '', text, flags=re.IGNORECASE)
    return re.sub(r'\s{2,}', ' ', text).strip().strip(',').strip()

def clean_title(t): return _clean(t, STOP_WORDS)
def clean_keywords(lst):
    seen, out = set(), []
    for kw in lst:
        kw = _clean(kw, STOP_WORDS).strip().strip(',')
        if kw and kw.lower() not in seen and len(kw) > 1:
            seen.add(kw.lower()); out.append(kw)
    return out

# ── FFmpeg ────────────────────────────────────────────────────────────────────
FFMPEG_HINTS = [
    r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\tools\ffmpeg\bin\ffmpeg.exe", r"C:\Windows\System32\ffmpeg.exe",
    r"D:\ffmpeg\bin\ffmpeg.exe",
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
        except: pass
    return None

def img_to_b64(path, max_px=1024):
    with Image.open(path) as img:
        try:
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)
        except: pass
        img = img.convert("RGB")
        w, h = img.size
        if max(w,h) > max_px:
            r = max_px/max(w,h)
            img = img.resize((max(1,int(w*r)), max(1,int(h*r))), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"

def extract_frames(video_path, ffmpeg_bin, n=VID_FRAMES):
    tmp, frames = tempfile.mkdtemp(), []
    try:
        ffprobe = ffmpeg_bin.replace("ffmpeg.exe","ffprobe.exe")
        if not os.path.isfile(ffprobe): ffprobe = "ffprobe"
        res = subprocess.run([ffprobe,"-v","error","-show_entries","format=duration",
            "-of","default=noprint_wrappers=1:nokey=1",str(video_path)],
            capture_output=True, text=True, timeout=15)
        dur = float(res.stdout.strip() or "10")
    except: dur = 10.0
    for i, t in enumerate([dur*j/(n+1) for j in range(1,n+1)]):
        out = os.path.join(tmp, f"f{i}.jpg")
        try:
            subprocess.run([ffmpeg_bin,"-ss",str(t),"-i",str(video_path),
                "-frames:v","1","-q:v","2",out,"-y","-loglevel","error"],
                capture_output=True, timeout=20)
            if os.path.exists(out): frames.append(out)
        except: pass
    return frames, tmp

# ── Промпт — обучен на реальных Adobe Stock CSV ───────────────────────────────
SYSTEM_PROMPT = """You are a professional Adobe Stock metadata specialist with years of experience in stock photography keywording.

You have analyzed thousands of successful Adobe Stock files and know exactly what sells.

TITLE RULES:
- Write exactly 3 sentences
- Sentence 1: Describe what is shown (subject + action + context)
- Sentence 2: Professional context — who would buy this and why
- Sentence 3: End with the main commercial concept keyword + "concept"
- Maximum 200 characters total
- Natural English, no brand names, no city names

KEYWORD RULES:
- Exactly 49 keywords, ALL single words or maximum 2-word phrases
- NO long phrases like "circuit breaker malfunction" — only "circuit" and "breaker" separately
- NO brand names, NO city names, NO personal names
- Positions 1-3: The most searched commercial terms for this image (what buyers type)
- Positions 4-15: Main subject, action, key details
- Positions 16-30: Colors, materials, setting, environment
- Positions 31-49: Concepts, emotions, synonyms, related themes
- Use professional terminology where appropriate
- Include both specific and general terms
- Think like a buyer: insurance company, news editor, blog writer, marketer

EXAMPLES of good keywords (single/2-word only):
corruption, bribe, crime, man, envelope, secret, deal, exchange, illegal,
table, restaurant, dark, shadow, hand, document, business, finance, fraud,
scandal, corporate, payment, cash, unethical, conspiracy, investigation

BAD keywords (too long, never use):
"circuit breaker malfunction", "emergency situation", "dangerous condition",
"safety concern", "appliance malfunction" """

def make_prompt(names, topic, prepend, append):
    lines = "\n".join(f"  [{i+1}] {n}" for i,n in enumerate(names))
    ctx  = f"\nCollection theme / context: {topic}" if topic.strip() else ""
    pre  = f"\nAdd these keywords after position 3: {prepend}" if prepend.strip() else ""
    app_ = f"\nAdd these keywords at the very end: {append}" if append.strip() else ""
    return f"""Analyze {len(names)} media file(s) and generate Adobe Stock metadata.{ctx}{pre}{app_}

Files:
{lines}

Generate for EACH file:
1. Title (3 sentences, max 200 chars, commercial and descriptive)
2. Exactly 49 keywords (single words or max 2-word phrases, no long phrases)

Return ONLY raw JSON array:
[
  {{"index": 1, "title": "...", "keywords": ["word1", "word2", ...]}},
  ...
]"""

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
                system=SYSTEM_PROMPT,
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
            log_fn(f"  ⚠ Error (attempt {attempt+1}): {e}")
            if attempt==2: return []
            time.sleep(5)
    return []

# ── GUI ───────────────────────────────────────────────────────────────────────
class App(tk.Tk):
    BG="#F2F2F7"; WHITE="#FFFFFF"; BDR="#D1D1D6"; ACC="#5856D6"; ACC2="#3634A3"
    FG="#1C1C1E"; FG2="#6C6C70"; FG3="#C7C7CC"; GREEN="#34C759"; AMBER="#FF9500"; SEL="#EEEEFF"

    def __init__(self):
        super().__init__()
        self.title("Keyworder — Adobe Stock")
        self.configure(bg=self.BG); self.resizable(False,False)
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
            if CONFIG_PATH.exists(): return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except: pass
        return {}

    def _save_cfg(self):
        CONFIG_PATH.write_text(json.dumps(self.cfg,ensure_ascii=False,indent=2),encoding="utf-8")

    def _init_ffmpeg(self):
        self.ffmpeg = find_ffmpeg(self.cfg.get("ffmpeg",""))
        if self.ffmpeg:
            self.cfg["ffmpeg"]=self.ffmpeg; self._save_cfg()
            self.ff_lbl.config(text="✓ FFmpeg",fg=self.GREEN,cursor="arrow")
        else:
            self.ff_lbl.config(text="⚠ FFmpeg — нажмите чтобы указать",fg=self.AMBER,cursor="hand2")

    def _pick_ffmpeg(self,*_):
        if self.ff_lbl.cget("cursor")!="hand2": return
        p=filedialog.askopenfilename(title="Укажите ffmpeg.exe",
            filetypes=[("ffmpeg.exe","ffmpeg.exe"),("All","*")])
        if p and os.path.isfile(p):
            self.ffmpeg=p; self.cfg["ffmpeg"]=p; self._save_cfg()
            self.ff_lbl.config(text="✓ FFmpeg",fg=self.GREEN,cursor="arrow")

    def _card(self,parent,pady=(0,0)):
        f=tk.Frame(parent,bg=self.WHITE,highlightthickness=1,highlightbackground=self.BDR)
        f.pack(fill="x",padx=16,pady=pady)
        inner=tk.Frame(f,bg=self.WHITE); inner.pack(fill="x",padx=14,pady=10)
        return inner

    def _entry(self,parent,var,show=None,readonly=False,width=None):
        kw=dict(textvariable=var,font=("Segoe UI",10),bg="#FAFAFA",fg=self.FG,
                insertbackground=self.FG,relief="flat",bd=0,
                highlightthickness=1,highlightbackground=self.BDR,highlightcolor=self.ACC)
        if show: kw["show"]=show
        if readonly: kw.update(state="readonly",readonlybackground="#FAFAFA")
        if width: kw["width"]=width
        e=tk.Entry(parent,**kw)
        def _paste(event):
            try:
                text=e.clipboard_get()
                try: e.delete(tk.SEL_FIRST,tk.SEL_LAST)
                except: pass
                e.insert(tk.INSERT,text)
            except: pass
            return "break"
        def _copy(event):
            try:
                text=e.selection_get()
                e.clipboard_clear()
                e.clipboard_append(text)
            except: pass
            return "break"
        def _cut(event):
            try:
                text=e.selection_get()
                e.clipboard_clear()
                e.clipboard_append(text)
                e.delete(tk.SEL_FIRST,tk.SEL_LAST)
            except: pass
            return "break"
        e.bind("<Control-v>",_paste)
        e.bind("<Control-V>",_paste)
        e.bind("<Control-c>",_copy)
        e.bind("<Control-C>",_copy)
        e.bind("<Control-x>",_cut)
        e.bind("<Control-X>",_cut)
        return e

    def _divider(self,p): tk.Frame(p,bg=self.BDR,height=1).pack(fill="x",pady=(4,8))

    def _build(self):
        self.geometry("500x800")

        hdr=tk.Frame(self,bg=self.ACC); hdr.pack(fill="x")
        tk.Label(hdr,text="Keyworder",font=("Segoe UI",14,"bold"),
                 bg=self.ACC,fg="#fff").pack(side="left",padx=16,pady=12)
        tk.Label(hdr,text="Adobe Stock · Claude AI · 49 keywords",
                 font=("Segoe UI",9),bg=self.ACC,fg="#ccc8ff").pack(side="left")
        self.ff_lbl=tk.Label(hdr,text="Проверка FFmpeg…",
                             font=("Segoe UI",8),bg=self.ACC,fg="#fff")
        self.ff_lbl.pack(side="right",padx=12)
        self.ff_lbl.bind("<Button-1>",self._pick_ffmpeg)

        outer=tk.Frame(self,bg=self.BG); outer.pack(fill="both",expand=True)
        canvas=tk.Canvas(outer,bg=self.BG,highlightthickness=0,bd=0)
        vsb=tk.Scrollbar(outer,orient="vertical",command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right",fill="y"); canvas.pack(side="left",fill="both",expand=True)
        body=tk.Frame(canvas,bg=self.BG)
        cw=canvas.create_window((0,0),window=body,anchor="nw")
        canvas.bind("<Configure>",lambda e:canvas.itemconfig(cw,width=e.width))
        body.bind("<Configure>",lambda e:canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind_all("<MouseWheel>",lambda e:canvas.yview_scroll(int(-1*(e.delta/120)),"units"))

        # API Key
        tk.Label(body,text="API КЛЮЧ",font=("Segoe UI",8,"bold"),
                 bg=self.BG,fg=self.FG3).pack(anchor="w",padx=18,pady=(14,2))
        c1=self._card(body)
        tk.Label(c1,text="Anthropic API ключ",font=("Segoe UI",10,"bold"),
                 bg=self.WHITE,fg=self.FG).pack(anchor="w")
        tk.Label(c1,text="Хранится локально · только для Anthropic API",
                 font=("Segoe UI",9),bg=self.WHITE,fg=self.FG2).pack(anchor="w",pady=(1,6))
        r1=tk.Frame(c1,bg=self.WHITE); r1.pack(fill="x")
        self.key_entry=self._entry(r1,self.v_key,show="•")
        self.key_entry.pack(side="left",fill="x",expand=True,ipady=7,ipadx=8)
        def tog():
            self._show_key=not self._show_key
            self.key_entry.config(show="" if self._show_key else "•")
            sb.config(text="Скрыть" if self._show_key else "Показать")
        sb=tk.Button(r1,text="Показать",font=("Segoe UI",9),bg=self.BG,fg=self.FG2,
                     relief="flat",bd=0,cursor="hand2",
                     highlightthickness=1,highlightbackground=self.BDR,command=tog)
        sb.pack(side="left",padx=(6,0),ipady=5,ipadx=10)
        tk.Button(c1,text="Где взять ключ →",font=("Segoe UI",9),
                  bg=self.WHITE,fg=self.ACC,relief="flat",bd=0,
                  cursor="hand2",command=self._api_help).pack(anchor="w",pady=(6,0))

        # Folder
        tk.Label(body,text="ПАПКА С ФАЙЛАМИ",font=("Segoe UI",8,"bold"),
                 bg=self.BG,fg=self.FG3).pack(anchor="w",padx=18,pady=(10,2))
        c2=self._card(body)
        tk.Label(c2,text="Исходная папка",font=("Segoe UI",10,"bold"),
                 bg=self.WHITE,fg=self.FG).pack(anchor="w")
        tk.Label(c2,text="Все поддерживаемые файлы внутри будут обработаны",
                 font=("Segoe UI",9),bg=self.WHITE,fg=self.FG2).pack(anchor="w",pady=(1,6))
        r2=tk.Frame(c2,bg=self.WHITE); r2.pack(fill="x")
        self._entry(r2,self.v_folder,readonly=True).pack(
            side="left",fill="x",expand=True,ipady=7,ipadx=8)
        tk.Button(r2,text="Выбрать…",font=("Segoe UI",9,"bold"),
                  bg=self.ACC,fg="#fff",activebackground=self.ACC2,activeforeground="#fff",
                  relief="flat",bd=0,cursor="hand2",command=self._browse).pack(
                  side="left",padx=(6,0),ipady=5,ipadx=12)

        # Mode
        tk.Label(body,text="ТИП ФАЙЛОВ",font=("Segoe UI",8,"bold"),
                 bg=self.BG,fg=self.FG3).pack(anchor="w",padx=18,pady=(10,2))
        c3=self._card(body)
        tk.Label(c3,text="Что обрабатывать из папки",font=("Segoe UI",10,"bold"),
                 bg=self.WHITE,fg=self.FG).pack(anchor="w",pady=(0,6))
        r3=tk.Frame(c3,bg=self.WHITE); r3.pack(anchor="w")
        self._mbts={}
        for val,lbl in [("photo","🖼  Фото"),("video","🎬  Видео"),("both","📦  Фото + Видео")]:
            b=tk.Label(r3,text=lbl,font=("Segoe UI",9),bg=self.BG,fg=self.FG2,
                       cursor="hand2",padx=12,pady=5,
                       highlightthickness=1,highlightbackground=self.BDR)
            b.pack(side="left",padx=(0,6))
            b.bind("<Button-1>",lambda e,v=val:self._set_mode(v))
            self._mbts[val]=b
        self._set_mode(self.v_mode.get())

        # Settings
        tk.Label(body,text="НАСТРОЙКИ",font=("Segoe UI",8,"bold"),
                 bg=self.BG,fg=self.FG3).pack(anchor="w",padx=18,pady=(10,2))
        c4=self._card(body)
        tk.Label(c4,text="Тематика коллекции",font=("Segoe UI",10,"bold"),
                 bg=self.WHITE,fg=self.FG).pack(anchor="w")
        tk.Label(c4,text="Повышает точность ключевых слов",
                 font=("Segoe UI",9),bg=self.WHITE,fg=self.FG2).pack(anchor="w",pady=(1,4))
        self._entry(c4,self.v_topic).pack(fill="x",ipady=6,ipadx=8)
        tk.Label(c4,text="Например: corruption concept, medical science, summer lifestyle",
                 font=("Segoe UI",8),bg=self.WHITE,fg=self.FG3).pack(anchor="w",pady=(2,0))

        self._divider(c4)

        tk.Label(c4,text="Кастомные ключевые слова",font=("Segoe UI",10,"bold"),
                 bg=self.WHITE,fg=self.FG).pack(anchor="w")
        tk.Label(c4,text="В начало (после топ-3):",
                 font=("Segoe UI",9),bg=self.WHITE,fg=self.FG2).pack(anchor="w",pady=(4,2))
        self._entry(c4,self.v_prepend).pack(fill="x",ipady=6,ipadx=8,pady=(0,8))
        tk.Label(c4,text="В конец:",
                 font=("Segoe UI",9),bg=self.WHITE,fg=self.FG2).pack(anchor="w",pady=(0,2))
        self._entry(c4,self.v_append).pack(fill="x",ipady=6,ipadx=8)

        self._divider(c4)

        tk.Label(c4,text="Имя CSV файла",font=("Segoe UI",10,"bold"),
                 bg=self.WHITE,fg=self.FG).pack(anchor="w")
        rcsv=tk.Frame(c4,bg=self.WHITE); rcsv.pack(anchor="w",pady=(4,0))
        self._entry(rcsv,self.v_csvname,width=22).pack(side="left",ipady=6,ipadx=8)
        tk.Label(rcsv,text=".csv",font=("Segoe UI",10),
                 bg=self.WHITE,fg=self.FG2).pack(side="left",padx=4)

        # Start
        tk.Frame(body,bg=self.BG,height=8).pack()
        bf=tk.Frame(body,bg=self.BG); bf.pack(fill="x",padx=16)
        self.start_btn=tk.Button(bf,text="▶   Генерировать ключевые слова",
            font=("Segoe UI",11,"bold"),bg=self.ACC,fg="#fff",
            activebackground=self.ACC2,activeforeground="#fff",
            relief="flat",bd=0,cursor="hand2",command=self._start)
        self.start_btn.pack(fill="x",ipady=13)

        # Progress
        pf=tk.Frame(body,bg=self.BG); pf.pack(fill="x",padx=16,pady=(8,0))
        sty=ttk.Style(); sty.theme_use("default")
        sty.configure("K.Horizontal.TProgressbar",troughcolor="#E5E5EA",background=self.ACC,
                       bordercolor="#E5E5EA",lightcolor=self.ACC,darkcolor=self.ACC)
        self.progress=ttk.Progressbar(pf,style="K.Horizontal.TProgressbar",mode="determinate")
        self.progress.pack(fill="x",ipady=2)
        sr=tk.Frame(body,bg=self.BG); sr.pack(fill="x",padx=16,pady=(3,0))
        self.status_lbl=tk.Label(sr,textvariable=self.v_status,font=("Segoe UI",9),
                                 bg=self.BG,fg=self.FG2,anchor="w")
        self.status_lbl.pack(side="left")
        self.count_lbl=tk.Label(sr,text="",font=("Segoe UI",9),bg=self.BG,fg=self.FG3,anchor="e")
        self.count_lbl.pack(side="right")

        # Log
        lf=tk.Frame(body,bg=self.BG); lf.pack(fill="x",padx=16,pady=(10,16))
        tk.Label(lf,text="ЛОГ",font=("Segoe UI",8,"bold"),
                 bg=self.BG,fg=self.FG3).pack(anchor="w",pady=(0,3))
        lframe=tk.Frame(lf,bg=self.WHITE,highlightthickness=1,highlightbackground=self.BDR)
        lframe.pack(fill="x")
        self.log_box=tk.Text(lframe,height=8,font=("Consolas",9),bg=self.WHITE,fg="#333",
                             relief="flat",bd=0,state="disabled",wrap="word",padx=8,pady=6)
        lsb=tk.Scrollbar(lframe,command=self.log_box.yview)
        self.log_box.config(yscrollcommand=lsb.set)
        lsb.pack(side="right",fill="y"); self.log_box.pack(side="left",fill="x",expand=True)

    def _set_mode(self,mode):
        self.v_mode.set(mode)
        for val,btn in self._mbts.items():
            if val==mode: btn.config(bg=self.SEL,fg=self.ACC,highlightbackground=self.ACC)
            else:         btn.config(bg=self.BG,fg=self.FG2,highlightbackground=self.BDR)

    def _browse(self):
        p=filedialog.askdirectory(title="Выберите папку с файлами")
        if p: self.v_folder.set(p)

    def _log(self,text):
        self.log_box.config(state="normal")
        self.log_box.insert("end",text+"\n"); self.log_box.see("end")
        self.log_box.config(state="disabled"); logging.info(text)

    def _api_help(self):
        w=tk.Toplevel(self); w.title("Где взять API ключ")
        w.configure(bg=self.WHITE); w.geometry("460x260"); w.resizable(False,False)
        txt=tk.Text(w,font=("Segoe UI",10),bg=self.WHITE,fg=self.FG,
                    relief="flat",bd=0,wrap="word",padx=16,pady=16)
        txt.pack(fill="both",expand=True)
        txt.insert("end",
"""Как получить Anthropic API ключ

1. Откройте  https://console.anthropic.com

2. Зарегистрируйтесь или войдите.

3. Левое меню → API Keys → Create Key.

4. Дайте любое название → скопируйте ключ.

5. Вставьте в поле приложения.

────────────────────────────────
Стоимость (claude-haiku-4-5):
~$0.003 за фото · 100 фото ≈ $0.30""")
        txt.config(state="disabled")

    def _start(self):
        if self.running: return
        key=self.v_key.get().strip()
        folder=self.v_folder.get().strip()
        if not key:
            messagebox.showerror("Нет ключа","Введите Anthropic API ключ."); return
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Нет папки","Выберите папку с файлами."); return

        self.cfg.update({"api_key":key,"folder":folder,"topic":self.v_topic.get(),
                         "prepend":self.v_prepend.get(),"append":self.v_append.get(),
                         "csvname":self.v_csvname.get(),"mode":self.v_mode.get()})
        self._save_cfg()
        self.running=True
        self.start_btn.config(state="disabled",text="⏳  Обработка…")
        self.log_box.config(state="normal"); self.log_box.delete("1.0","end")
        self.log_box.config(state="disabled")
        self.progress["value"]=0; self.v_status.set("Запуск…")

        threading.Thread(target=self._run,args=(
            key,folder,self.v_mode.get(),self.v_topic.get(),
            self.v_prepend.get(),self.v_append.get(),
            self.v_csvname.get(),self.ffmpeg),daemon=True).start()

    def _run(self,*args):
        try:    self._process(*args)
        except Exception as e:
            self.after(0,self._log,f"❌ Критическая ошибка: {e}"); logging.exception("Fatal")
        finally: self.after(0,self._finish)

    def _process(self,key,folder,mode,topic,prepend,append,csvname,ffmpeg_bin):
        client=anthropic.Anthropic(api_key=key)
        fp=Path(folder)
        all_f=sorted(fp.iterdir())
        if   mode=="photo": files=[f for f in all_f if f.suffix.lower() in IMG_EXT]
        elif mode=="video": files=[f for f in all_f if f.suffix.lower() in VID_EXT]
        else:               files=[f for f in all_f if f.suffix.lower() in IMG_EXT|VID_EXT]

        if not files: self.after(0,self._log,"❌ Нет поддерживаемых файлов."); return

        imgs_n=sum(1 for f in files if f.suffix.lower() in IMG_EXT)
        vids_n=sum(1 for f in files if f.suffix.lower() in VID_EXT)
        self.after(0,self._log,f"Найдено: {imgs_n} фото, {vids_n} видео → {len(files)} файлов")
        if topic:   self.after(0,self._log,f"Тематика: {topic}")
        if prepend: self.after(0,self._log,f"В начало: {prepend}")
        if append:  self.after(0,self._log,f"В конец:  {append}")
        self.after(0,self._log,"")

        work,tmps=[],[]
        self.after(0,self.v_status.set,"Загрузка файлов…")

        for i,f in enumerate(files):
            self.after(0,self.count_lbl.config,{"text":f"Загрузка {i+1}/{len(files)}"})
            is_vid=f.suffix.lower() in VID_EXT
            if is_vid:
                if not ffmpeg_bin:
                    self.after(0,self._log,f"  ⏭ Пропущено (нет FFmpeg): {f.name}"); continue
                self.after(0,self._log,f"  🎬 Кадры: {f.name}")
                frames,tmp=extract_frames(f,ffmpeg_bin); tmps.append(tmp)
                if not frames: self.after(0,self._log,f"  ⚠ Не удалось: {f.name}"); continue
                imgs=[]
                for fr in frames:
                    try: imgs.append(img_to_b64(fr))
                    except Exception as e: self.after(0,self._log,f"  ⚠ {e}")
                if imgs: work.append({"name":f.name,"images":imgs})
            else:
                try:
                    b64,mt=img_to_b64(f); work.append({"name":f.name,"images":[(b64,mt)]})
                except Exception as e: self.after(0,self._log,f"  ⚠ {f.name}: {e}")

        if not work: self.after(0,self._log,"❌ Нечего обрабатывать."); return

        total=len(work)
        batches=[work[i:i+BATCH_SIZE] for i in range(0,total,BATCH_SIZE)]
        self.after(0,self._log,f"🚀 {total} файлов → {len(batches)} пакет(ов)\n")

        results,done=[],0
        for bi,batch in enumerate(batches):
            self.after(0,self.v_status.set,f"Пакет {bi+1}/{len(batches)} — Claude анализирует…")
            self.after(0,self.count_lbl.config,{"text":f"{done}/{total} готово"})
            self.after(0,self._log,
                f"━━ Пакет {bi+1}/{len(batches)}: {', '.join(x['name'] for x in batch)}")

            res=call_claude(client,batch,topic,prepend,append,
                            lambda m:self.after(0,self._log,m))

            for idx,item in enumerate(batch):
                matched=next((r for r in res if r.get("index",0)-1==idx),None)
                if matched is None and idx<len(res): matched=res[idx]
                kws=clean_keywords((matched.get("keywords",[]) if matched else []))[:KW_COUNT]
                title=clean_title(matched.get("title","") if matched else "")
                results.append({"filename":item["name"],"title":title,"keywords":kws})
                if matched:
                    self.after(0,self._log,
                        f"  ✓ {item['name']} — {len(kws)} kw | топ-3: {', '.join(kws[:3])}")
                else: self.after(0,self._log,f"  ⚠ Нет ответа для {item['name']}")

            done+=len(batch)
            self.after(0,lambda p=int(done/total*100):self.progress.configure(value=p))
            if bi<len(batches)-1: time.sleep(1.0)

        for d in tmps:
            try: shutil.rmtree(d,ignore_errors=True)
            except: pass

        safe="".join(c for c in (csvname or "keywords") if c.isalnum() or c in "_- ") or "keywords"
        adobe=fp/f"{safe}.csv"
        with open(adobe,"w",newline="",encoding="utf-8-sig") as f:
            w=csv.writer(f)
            w.writerow(["Filename","Title","Keywords","Category","Releases"])
            for r in results:
                w.writerow([r["filename"],r["title"],", ".join(r["keywords"]),"",""])

        self.after(0,self._log,
            f"\n✅ Готово! {len(results)} файлов.\n📄 {adobe}")
        self.after(0,self.v_status.set,f"✅ Готово — {len(results)} файлов")
        self.after(0,self.count_lbl.config,{"text":f"{done}/{total}"})
        self.after(0,lambda:messagebox.showinfo("Готово!",
            f"Обработано: {len(results)} файлов\n\nCSV:\n{adobe}"))

    def _finish(self):
        self.running=False
        self.start_btn.config(state="normal",text="▶   Генерировать ключевые слова")

if __name__=="__main__":
    multiprocessing.freeze_support()
    App().mainloop()
