"""
FastLifeBG — Live ECU Display  v12
TWO THEMES:
  Pages 1-5  = Theme 1 (icon style, neon accents)
  Pages 6-10 = Theme 2 (bold block style, inspired by BMW M57 mockup)

Button wiring:
  GPIO17 = NAVIGATE  (next page / move cursor up-down)
  GPIO27 = CONFIRM   (select / value up / arm timer)
  Hold GPIO27 1s     = value down / reset peak

Run:
  python3 edc15_display.py --sim
  python3 edc15_display.py --sim --page 6    (theme 2 engine)
  python3 edc15_display.py --sim --page 9    (theme 2 settings)
  python3 edc15_display.py --port /dev/ttyUSB0
"""
import time, math, os, sys, argparse, threading, logging, random, json, collections

ap = argparse.ArgumentParser()
ap.add_argument("--port",  default="/dev/ttyUSB0")
ap.add_argument("--debug", action="store_true")
ap.add_argument("--sim",   action="store_true")
ap.add_argument("--page",  type=int, default=1, choices=list(range(1,11)))
ARGS = ap.parse_args()

_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fastlifebg.log")
logging.basicConfig(
    level=logging.DEBUG if ARGS.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(_log_file, mode='a', encoding='utf-8'),
    ]
)
log = logging.getLogger("display")
log.info("=== FastLifeBG v12 started ===")

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("pip3 install Pillow --break-system-packages"); sys.exit(1)

# ── GPIO ───────────────────────────────────────────────────────────────────
GPIO_OK = False
_pwm_bl = None

try:
    import RPi.GPIO as GPIO
    GPIO.setwarnings(False); GPIO.setmode(GPIO.BCM)
    GPIO.setup(23, GPIO.OUT)
    _pwm_bl = GPIO.PWM(23, 1000); _pwm_bl.start(100)
    GPIO.setup(18, GPIO.OUT); GPIO.output(18, GPIO.LOW)
    GPIO.setup(17, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(27, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO_OK = True; log.info("GPIO ready")
except Exception as e:
    log.warning(f"GPIO not available: {e}")

# ── Display ────────────────────────────────────────────────────────────────
try:
    from luma.core.interface.serial import spi
    from luma.lcd.device import ili9341
    _s     = spi(port=0, device=1, gpio_DC=24, gpio_RST=25, bus_speed_hz=32000000)
    device = ili9341(_s, width=320, height=240, rotate=1, bgr=True)
    W, H   = 240, 320
    log.info("Display ready")
except Exception as e:
    log.warning(f"Display: {e} — headless"); device = None; W, H = 240, 320

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from edc15_driver import KW1281Driver, ATM_MBAR
    DRIVER_OK = True
except ImportError:
    log.warning("edc15_driver not found — sim"); DRIVER_OK = False
    ARGS.sim = True; ATM_MBAR = 1013.25
ATM = ATM_MBAR

# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

SETTINGS_PATH = os.path.expanduser("~/.fastlifebg/settings.json")

# 10 colour presets — stored as BGR (ILI9341 bgr=True)
COLOUR_PRESETS = [
    ("GREEN",  (0,   230, 120)),
    ("CYAN",   (200, 220,  20)),
    ("BLUE",   (220,  80,  10)),
    ("PURPLE", (200,  20, 160)),
    ("PINK",   (160,   0, 220)),
    ("RED",    (0,    20, 210)),
    ("ORANGE", (0,   120, 240)),
    ("AMBER",  (0,   160, 210)),
    ("YELLOW", (0,   220, 230)),
    ("WHITE",  (210, 210, 220)),
]

DEFAULT_SETTINGS = {"colour": 0, "depth": 7, "brightness": 8, "theme": 1}

def load_settings():
    try:
        with open(SETTINGS_PATH) as f: s = json.load(f)
        s["colour"]     = max(0,  min(9,  int(s.get("colour",     0))))
        s["depth"]      = max(1,  min(10, int(s.get("depth",      7))))
        s["brightness"] = max(1,  min(10, int(s.get("brightness", 8))))
        s["theme"]      = max(1,  min(2,  int(s.get("theme",      1))))
        return s
    except: return dict(DEFAULT_SETTINGS)

def save_settings(s):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as f: json.dump(s, f)
    except Exception as e: log.warning(f"Settings save: {e}")

def apply_brightness(s):
    if _pwm_bl: _pwm_bl.ChangeDutyCycle(s["brightness"] * 10)

def get_accent(s=None, flash=False, dim=False):
    if s is None: s = SETTINGS
    idx = s["colour"]; depth = s["depth"]
    base = COLOUR_PRESETS[idx][1]
    scale = (depth / 10.0) * (0.5 if dim else 1.0) * (1.15 if flash else 1.0)
    return tuple(min(255, int(c * scale)) for c in base)

def get_crit(flash=False):
    idx = SETTINGS["colour"]
    if idx == 5:   base = (0, 220, 220)   # RED theme → yellow crit
    elif idx == 7: base = (0,  30, 220)   # AMBER theme → red crit
    else:          base = (35,  35, 200)  # default red
    if flash: base = tuple(min(255, int(c*1.25)) for c in base)
    return base

def get_warn():
    idx = SETTINGS["colour"]
    if idx == 5:  return (0, 120, 240)   # RED → orange warn
    if idx == 7:  return (0, 220, 230)   # AMBER → yellow warn
    return (20, 160, 220)                # default amber

SETTINGS = load_settings()
apply_brightness(SETTINGS)

# ── Constants ──────────────────────────────────────────────────────────────
BG    = (11,   8,   6)
BG2   = (18,  14,  11)   # slightly lighter bg for cards
DIM   = (78,  68,  55)
BDR   = (48,  38,  30)
MID   = (160, 148, 130)
WHITE = (235, 230, 220)
BLACK = (0,     0,   0)

# Logo colours — fixed for all themes
LOGO_FAST = (235, 230, 220)   # white
LOGO_LIFE = (0,   230, 120)   # green (BGR)
LOGO_BG   = (0,    20, 210)   # red (BGR)

# ── Fonts ──────────────────────────────────────────────────────────────────
def _lf(sz, b=True):
    bp = ["/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
          "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"]
    rp = ["/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
          "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"]
    p = next((x for x in (bp if b else rp) if os.path.exists(x)), None)
    try:    return ImageFont.truetype(p, sz) if p else ImageFont.load_default()
    except: return ImageFont.load_default()

F = {"huge": _lf(34), "xlarge": _lf(26), "large": _lf(18),
     "med": _lf(15), "sm": _lf(12), "xs": _lf(9, False), "xs2": _lf(8, False)}

def show(img):
    if device: device.display(img)

def txt(d, text, x, y, font, color, anchor="lt"):
    d.text((x, y), str(text), font=font, fill=color, anchor=anchor)

def fmt(v, spec=".1f", fb="--"):
    if v is None: return fb
    try:    return format(v, spec)
    except: return fb

def tlen(d_or_font, text, font=None):
    f = font if font else d_or_font
    try:    return int(f.getlength(str(text)))
    except: return len(str(text)) * 8

def arc_pts(cx, cy, r, a1, a2, steps=60):
    pts = []
    for i in range(steps+1):
        a = math.radians(a1 + (a2-a1)*i/steps)
        pts.append((cx + r*math.cos(a), cy + r*math.sin(a)))
    return pts

# ── Boost history for graph ────────────────────────────────────────────────
BOOST_HISTORY = collections.deque(maxlen=80)

# ══════════════════════════════════════════════════════════════════════════════
# PROGRESS BAR helper  (used by Theme 2 and Settings)
# ══════════════════════════════════════════════════════════════════════════════

def draw_bar(d, x, y, w, h, value, vmin, vmax, col, bg=(20,18,15), radius=2):
    """Draw a sleek rounded progress bar."""
    d.rounded_rectangle([x, y, x+w, y+h], radius=radius, fill=bg)
    if vmax > vmin and value is not None:
        pct   = max(0.0, min(1.0, (value - vmin) / (vmax - vmin)))
        fw    = max(0, int(w * pct))
        if fw >= radius * 2:
            d.rounded_rectangle([x, y, x+fw, y+h], radius=radius, fill=col)
        elif fw > 0:
            d.rectangle([x, y, x+fw, y+h], fill=col)

# ══════════════════════════════════════════════════════════════════════════════
# THEME 1 ICONS
# ══════════════════════════════════════════════════════════════════════════════

def make_icon(fn, size, col):
    img = Image.new("RGB", (size, size), BG)
    d   = ImageDraw.Draw(img)
    fn(d, size//2, size//2, size, col)
    return img

def _thermo(d, cx, cy, s, col, letter=None):
    tw=max(2,s//7); bul=max(4,s//3)
    tt=cy-s//2; tb=cy+s//2-bul; th=tb-tt
    d.rounded_rectangle([cx-tw,tt,cx+tw,tb+tw],radius=tw,outline=col,width=1)
    ft=tt+int(th*0.55)
    if ft<tb: d.rectangle([cx-tw+1,ft,cx+tw-1,tb+1],fill=col)
    for i in range(3):
        ty=tt+int(th*0.15)+i*int(th*0.30); tl=5 if i%2==0 else 3
        d.line([(cx+tw,ty),(cx+tw+tl,ty)],fill=col,width=1)
    by=cy+s//2-bul
    d.ellipse([cx-bul,by,cx+bul,by+bul*2],outline=col,width=1,fill=col)
    if letter:
        try: ff=ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",max(6,s//4))
        except: ff=ImageFont.load_default()
        d.text((cx,by+bul),letter,font=ff,fill=BG,anchor="mm")

def _icon_coolant(d,cx,cy,s,col):
    ts=int(s*0.68); tc=cy-int(s*0.13); _thermo(d,cx,tc,ts,col)
    wt=tc+ts//2+3; seg=int(s*0.35)
    for row in range(2):
        wy=wt+row*5
        if wy+3>cy+s//2-1: break
        x0=cx-seg
        for h in range(2):
            wx0=x0+h*seg; wx1=wx0+seg; wxm=(wx0+wx1)//2
            d.arc([wx0,wy-3,wxm,wy+3],180,0,fill=col,width=2)
            d.arc([wxm,wy-3,wx1,wy+3],0,180,fill=col,width=2)

def _icon_fuel(d,cx,cy,s,col): _thermo(d,cx,cy,23,col,letter="F")

def _icon_intercooler(d,cx,cy,s,col):
    cw=int(s*0.78); ch=int(s*0.58)
    x0=cx-cw//2; y0=cy-ch//2; x1=x0+cw; y1=y0+ch
    tp=max(2,s//14)
    body=[(x0+tp,y0),(x1-tp,y0),(x1,y0+tp),(x1,y1-tp),(x1-tp,y1),(x0+tp,y1),(x0,y1-tp),(x0,y0+tp)]
    d.polygon(body,fill=BG)
    n=7; usable=ch-6; gap=max(2,usable//(n+1))
    for i in range(1,n+1):
        fy=y0+3+i*gap
        if fy<y1-3: d.line([(x0+4,fy),(x1-4,fy)],fill=col,width=1)
    d.polygon(body,outline=col,fill=None)
    pw=max(3,s//8); pl=max(4,s//6); py=y1-pw-2
    if py>y0+4:
        d.rectangle([x0-pl,py,x0+1,py+pw],fill=col)
        d.rectangle([x0-pl-3,py-2,x0-pl,py+pw+2],fill=col)
        d.rectangle([x0-pl-5,py-1,x0-pl-3,py+pw+1],fill=col)
        d.rectangle([x1-1,py,x1+pl,py+pw],fill=col)
        d.rectangle([x1+pl,py-2,x1+pl+3,py+pw+2],fill=col)
        d.rectangle([x1+pl+3,py-1,x1+pl+5,py+pw+1],fill=col)

def _icon_turbo(d,cx,cy,s,col):
    r=int(s*0.38); hcx=cx-s//14; hcy=cy+s//14
    pw=max(4,int(s*0.20)); pl=int(s*0.28)
    ay=hcy-r; acy=ay+pw//2
    d.rectangle([hcx,ay,hcx+r+pl,ay+pw],fill=col)
    fh=pw+5
    d.rectangle([hcx+r+pl,acy-fh//2,hcx+r+pl+3,acy+fh//2],fill=col)
    d.ellipse([hcx-r,hcy-r,hcx+r,hcy+r],fill=col)
    rg=int(r*0.76); d.ellipse([hcx-rg,hcy-rg,hcx+rg,hcy+rg],fill=BG)
    rw=int(r*0.70); rh=max(2,s//14)
    d.ellipse([hcx-rw,hcy-rw,hcx+rw,hcy+rw],outline=col,width=2)
    for i in range(12):
        ba=math.radians(i*360/12); pts=[]
        for st in range(8):
            t=st/7; rr=rh*2.0+(rw*0.88-rh*2.0)*t
            a=ba+math.radians(28*t); pts.append((hcx+rr*math.cos(a),hcy+rr*math.sin(a)))
        if len(pts)>1: d.line(pts,fill=col,width=1)
    d.ellipse([hcx-rh,hcy-rh,hcx+rh,hcy+rh],fill=col)
    av=hcx+r-2
    d.rectangle([av,ay,hcx+r+pl,ay+pw],fill=col)
    d.rectangle([hcx+r+pl,acy-fh//2,hcx+r+pl+3,acy+fh//2],fill=col)

def _icon_injector(d,cx,cy,s,col):
    bw=max(2,s//6)
    d.polygon([(cx-bw+1,cy-s//2+3),(cx-bw-2,cy-s//2+8),(cx-bw-2,cy-s//2+12),(cx-bw+1,cy-s//2+12)],outline=col,fill=BG)
    bh=int(s*0.46)
    d.rectangle([cx-bw,cy-s//2+3,cx+bw,cy-s//2+3+bh],outline=col,width=1)
    n1=max(1,bw-1); d.rectangle([cx-n1,cy-s//2+3+bh,cx+n1,cy-s//2+3+bh+s//8],outline=col,width=1)
    n2=max(1,bw-2); ty=cy-s//2+3+bh+s//8
    if ty+s//9<cy+s//2:
        d.rectangle([cx-n2,ty,cx+n2,ty+s//9],fill=col)
        sy=ty+s//9+1
        for dx,ax in [(-s//5,-22),(0,0),(s//5,22)]:
            ex=int((s//6)*math.sin(math.radians(ax)))
            if sy+s//7<=cy+s//2: d.line([(cx+dx,sy),(cx+dx+ex,sy+s//7)],fill=col,width=1)

def _icon_timer(d,cx,cy,s,col):
    r=int(s*0.38)
    d.ellipse([cx-r,cy-r+2,cx+r,cy+r+2],outline=col,width=2)
    d.rectangle([cx-3,cy-r-3+2,cx+3,cy-r+2],fill=col)
    d.rectangle([cx-6,cy-r-5+2,cx-1,cy-r-1+2],fill=col)
    d.rectangle([cx+1,cy-r-5+2,cx+6,cy-r-1+2],fill=col)
    d.line([(cx,cy+2),(cx,cy-r+6+2)],fill=col,width=2)
    d.line([(cx,cy+2),(cx+r-4,cy+4)],fill=col,width=1)
    d.ellipse([cx-2,cy,cx+2,cy+4],fill=col)

# ══════════════════════════════════════════════════════════════════════════════
# SMOOTHERS
# ══════════════════════════════════════════════════════════════════════════════

class Smoother:
    def __init__(self,au=0.35,ad=0.18,init=None):
        self.au=au; self.ad=ad; self.v=init
    def update(self,new):
        if new is None: return self.v
        if self.v is None: self.v=new; return self.v
        a=self.au if new>self.v else self.ad
        self.v=a*new+(1-a)*self.v; return self.v

S_rpm=Smoother(0.70,0.50); S_boost=Smoother(0.90,0.40)
S_btgt=Smoother(0.40,0.40); S_load=Smoother(0.85,0.25)
S_cool=Smoother(0.08,0.08); S_iat=Smoother(0.08,0.08)
S_inj=Smoother(0.65,0.40);  S_fuel=Smoother(0.05,0.05)
S_vnt=Smoother(0.30,0.30)

# ══════════════════════════════════════════════════════════════════════════════
# BOOST DEVIATION
# ══════════════════════════════════════════════════════════════════════════════

_over_since=None; _under_since=None
OVER_THRESH=0.10; UNDER_THRESH=0.15; OVER_TIME=2.0; UNDER_TIME=5.0

def boost_dev_col(boost_bar, btgt_bar, flash):
    global _over_since,_under_since
    now=time.time()
    if boost_bar is None or btgt_bar is None:
        _over_since=None; _under_since=None; return WHITE,False
    dev=boost_bar-btgt_bar; beep_f=False
    if btgt_bar>0.10:
        if dev>OVER_THRESH:
            if _over_since is None: _over_since=now
            _under_since=None
            if now-_over_since>=OVER_TIME: beep_f=True; return get_crit(flash),beep_f
        elif dev<-UNDER_THRESH:
            if _under_since is None: _under_since=now
            _over_since=None
            if now-_under_since>=UNDER_TIME: return get_warn(),False
        else: _over_since=None; _under_since=None
    else: _over_since=None; _under_since=None
    return WHITE,beep_f

# ══════════════════════════════════════════════════════════════════════════════
# BUZZER
# ══════════════════════════════════════════════════════════════════════════════

BUZZER_PIN=18; _buzzer_ok=GPIO_OK
def beep(pattern="warning"):
    if not _buzzer_ok: return
    def _b():
        if pattern=="critical":
            for _ in range(5): GPIO.output(BUZZER_PIN,GPIO.HIGH);time.sleep(0.05);GPIO.output(BUZZER_PIN,GPIO.LOW);time.sleep(0.05)
        elif pattern=="warning":
            for _ in range(2): GPIO.output(BUZZER_PIN,GPIO.HIGH);time.sleep(0.10);GPIO.output(BUZZER_PIN,GPIO.LOW);time.sleep(0.10)
        elif pattern=="single": GPIO.output(BUZZER_PIN,GPIO.HIGH);time.sleep(0.40);GPIO.output(BUZZER_PIN,GPIO.LOW)
    threading.Thread(target=_b,daemon=True).start()
_last_beep=0.0
def beep_if_due(pattern,interval=8.0):
    global _last_beep
    now=time.time()
    if now-_last_beep>=interval: _last_beep=now; beep(pattern)

# ══════════════════════════════════════════════════════════════════════════════
# BUTTONS
# ══════════════════════════════════════════════════════════════════════════════

_btn17_event=False; _btn27_event=False; _btn27_held=False
_btn27_down_at=None; BTN_HOLD_MS=1000

def _poll_buttons():
    global _btn17_event,_btn27_event,_btn27_held,_btn27_down_at
    if not GPIO_OK: return
    b17=GPIO.input(17)==GPIO.LOW; b27=GPIO.input(27)==GPIO.LOW; now=time.time()
    if not hasattr(_poll_buttons,'_b17'): _poll_buttons._b17=False; _poll_buttons._b27=False
    if b17 and not _poll_buttons._b17: _btn17_event=True
    if b27 and not _poll_buttons._b27: _btn27_down_at=now
    if not b27 and _poll_buttons._b27 and _btn27_down_at is not None:
        held_ms=(now-_btn27_down_at)*1000
        if held_ms>=BTN_HOLD_MS: _btn27_held=True
        else: _btn27_event=True
        _btn27_down_at=None
    _poll_buttons._b17=b17; _poll_buttons._b27=b27

def consume_nav():
    global _btn17_event
    if _btn17_event: _btn17_event=False; return True
    return False
def consume_confirm():
    global _btn27_event
    if _btn27_event: _btn27_event=False; return True
    return False
def consume_hold():
    global _btn27_held
    if _btn27_held: _btn27_held=False; return True
    return False

# ══════════════════════════════════════════════════════════════════════════════
# SHARED HEADER / FOOTER
# ══════════════════════════════════════════════════════════════════════════════

HEADER=22; FOOTER=20

def _draw_logo(d, x=10, y=4):
    """FAST(white) LIFE(green) BG(red) — fixed for all themes."""
    txt(d,"FAST",x,y,F["sm"],LOGO_FAST)
    try:    fw=int(F["sm"].getlength("FAST"))
    except: fw=28
    txt(d,"LIFE",x+fw,y,F["sm"],LOGO_LIFE)
    try:    fw2=int(F["sm"].getlength("LIFE"))
    except: fw2=28
    txt(d,"BG",x+fw+fw2+2,y+3,F["xs2"],LOGO_BG)

def _draw_header(d, img, pnum, connected, theme=1):
    bg_col = BLACK if theme==1 else (8,8,8)
    d.rectangle([0,0,W,HEADER],fill=bg_col)
    _draw_logo(d)
    pstr = f"T{theme} P{pnum}" if pnum else ""
    txt(d,pstr,W-38,5,F["xs2"],DIM)
    ac = get_accent()
    dot = ac if connected else get_crit(False)
    d.ellipse([W-12,8,W-6,14],fill=dot)
    d.line([(0,HEADER),(W,HEADER)],fill=BDR,width=1)

def _draw_footer(d,text,theme=1):
    d.line([(0,H-FOOTER),(W,H-FOOTER)],fill=BDR,width=1)
    txt(d,text,W//2,H-9,F["xs2"],(28,36,44),anchor="mt")

# ══════════════════════════════════════════════════════════════════════════════
# BOOT
# ══════════════════════════════════════════════════════════════════════════════

BOOT_CHECKS=[{"label":"SPI DISPLAY","at":0.20},{"label":"KKL SERIAL PORT","at":0.40},
             {"label":"K-LINE 9600 BAUD","at":0.60},{"label":"ECU ADDRESS 0x01","at":0.80}]

class Boot:
    def __init__(self): self.t0=time.time(); self.done=False; self._fade=None; self.tacho=225.0; self.checks=[]
    def update(self):
        el=time.time()-self.t0; prg=min(1.0,el/3.5)
        tgt=225+(el/2.5)*205 if el<2.5 else 430-(el-2.5)*60
        self.tacho+=(tgt-self.tacho)*0.10
        for i,c in enumerate(BOOT_CHECKS):
            if prg>=c["at"] and i not in self.checks: self.checks.append(i)
        if el>3.5 and self._fade is None: self._fade=time.time()
        if self._fade and time.time()-self._fade>0.6: self.done=True
    def draw(self):
        ac=get_accent(); img=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(img)
        el=time.time()-self.t0; prg=min(1.0,el/3.5); CX=W//2
        TCX,TCY,TR=CX,75,44
        pts_bg=arc_pts(TCX,TCY,TR,225,430)
        if len(pts_bg)>1: d.line(pts_bg,fill=(25,32,40),width=5)
        pct=max(0,min(1,(self.tacho-225)/205))
        if pct>0.01:
            pts_ac=arc_pts(TCX,TCY,TR,225,225+pct*205)
            ac2=get_crit(False) if pct>0.85 else get_warn() if pct>0.60 else ac
            if len(pts_ac)>1: d.line(pts_ac,fill=ac2,width=5)
        na=math.radians(self.tacho)
        d.line([(TCX,TCY),(TCX+34*math.cos(na),TCY+34*math.sin(na))],fill=(200,50,60),width=2)
        d.ellipse([TCX-5,TCY-5,TCX+5,TCY+5],fill=(22,28,36))
        d.ellipse([TCX-2,TCY-2,TCX+2,TCY+2],fill=ac)
        LY=TCY+74
        _draw_logo(d,CX-34,LY)
        txt(d,"ECU LIVE GAUGE SYSTEM",CX,LY+18,F["xs2"],DIM,anchor="mt")
        txt(d,"BOSCH EDC15P+  1.9 TDI",CX,LY+29,F["xs2"],(38,50,58),anchor="mt")
        CKY=LY+44
        for idx in self.checks:
            yy=CKY+idx*13
            txt(d,BOOT_CHECKS[idx]["label"],18,yy,F["xs2"],(100,120,115))
            txt(d,"OK",W-18,yy,F["xs2"],(0,150,75),anchor="rt")
        bx,by_,bw,bh=CX-55,H-38,110,3
        d.rectangle([bx,by_,bx+bw,by_+bh],fill=(18,24,30))
        fw2=max(0,int(bw*prg))
        if fw2>0: d.rectangle([bx,by_,bx+fw2,by_+bh],fill=ac if prg>=0.99 else (0,140,160))
        txt(d,f"{int(prg*100)}%",CX,by_+5,F["xs2"],DIM,anchor="mt")
        if self._fade:
            alpha=int(min(1.0,(time.time()-self._fade)/0.6)*255)
            ov=Image.new("RGBA",(W,H),(*BG,alpha)); img=img.convert("RGBA")
            img.alpha_composite(ov); img=img.convert("RGB")
        return img

# ══════════════════════════════════════════════════════════════════════════════
# ████████  THEME 1  — ICON STYLE  (Pages 1-5)
# ══════════════════════════════════════════════════════════════════════════════

ROW_H_T1=(H-HEADER-FOOTER)//6
ICON_SZ=42; ICON_CX=4; DATA_X=54

def t1_page1(rpm,load_pct,boost_mbar,btgt_mbar,vnt_pct,
             coolant,iat,fuel_temp,inj,faults,conn,flash,g7_age=0):
    ac=get_accent(); ac2=get_accent(dim=True)
    img=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(img)
    boost_bar=(boost_mbar-ATM)/1000.0 if boost_mbar is not None else None
    btgt_bar=(btgt_mbar-ATM)/1000.0   if btgt_mbar  is not None else None
    def tcol(v,w,c): return get_crit(flash) if v and v>c else get_warn() if v and v>w else WHITE
    def icol(v,w,c): return get_crit(flash) if v and v>c else get_warn() if v and v>w else ac
    def paste_icon(fn,row,col):
        ico=make_icon(fn,ICON_SZ,col); ry=HEADER+row*ROW_H_T1; iy=ry+(ROW_H_T1-ICON_SZ)//2
        img.paste(ico,(ICON_CX,iy))
    def row_data(row,label,val_s,unit,vcol):
        ry=HEADER+row*ROW_H_T1
        txt(d,label,DATA_X,ry+4,F["xs"],ac2)
        txt(d,val_s,DATA_X,ry+15,F["large"],vcol)
        try: vw=int(F["large"].getlength(val_s))
        except: vw=len(val_s)*10
        txt(d,unit,DATA_X+vw+3,ry+18,F["xs"],ac2)
    _draw_header(d,img,1,conn,theme=1)
    # ROW 0 BOOST
    ry=HEADER; paste_icon(_icon_turbo,0,ac)
    txt(d,"BOOST",DATA_X,ry+3,F["xs"],ac2)
    bc,_=boost_dev_col(boost_bar,btgt_bar,flash)
    bv=f"{boost_bar:+.2f}" if boost_bar is not None else "--"
    txt(d,bv,DATA_X,ry+13,F["huge"],bc)
    try: bvw=int(F["huge"].getlength(bv))
    except: bvw=55
    txt(d,"bar",DATA_X+bvw+3,ry+26,F["xs"],ac2)
    if btgt_bar is not None: txt(d,f"tgt {btgt_bar:+.2f}b",DATA_X,ry+ROW_H_T1-11,F["xs"],ac2)
    # ROW 1-4
    for row,(fn,lbl,val,unit,w,c) in enumerate([
        (_icon_coolant,"COOLANT",coolant,  "°C",90,95),
        (_icon_intercooler,"IAT" if not(0<g7_age<30) else f"IAT~{int(g7_age)}s",iat,"°C",45,60),
        (_icon_fuel,"FUEL TEMP",fuel_temp,"°C",85,95),
        (_icon_injector,"INJ QTY",inj,   "mg",None,None),
    ],1):
        ry=HEADER+row*ROW_H_T1; d.line([(8,ry),(W-8,ry)],fill=BDR,width=1)
        col=icol(val,w,c) if w else ac
        paste_icon(fn,row,col)
        vc=tcol(val,w,c) if w else WHITE
        row_data(row,lbl,fmt(val,".1f"),unit,vc)
    # ROW 5 LOAD+VNT
    ry=HEADER+5*ROW_H_T1; d.line([(8,ry),(W-8,ry)],fill=BDR,width=1)
    mid=W//2+4; d.line([(mid,ry+6),(mid,ry+ROW_H_T1-6)],fill=BDR,width=1)
    lv=fmt(load_pct,".0f"); txt(d,"LOAD",DATA_X,ry+4,F["xs"],ac2)
    txt(d,lv,DATA_X,ry+15,F["large"],MID)
    try: lw=int(F["large"].getlength(lv))
    except: lw=24
    txt(d,"%",DATA_X+lw+3,ry+18,F["xs"],ac2)
    vv=fmt(vnt_pct,".0f"); txt(d,"VNT",mid+8,ry+4,F["xs"],ac2)
    txt(d,vv,mid+8,ry+15,F["large"],ac2)
    try: vw=int(F["large"].getlength(vv))
    except: vw=24
    txt(d,"%",mid+8+vw+3,ry+18,F["xs"],ac2)
    # FOOTER
    d.line([(0,H-FOOTER),(W,H-FOOTER)],fill=BDR,width=1)
    if faults: txt(d,f"⚠ {len(faults)} FAULT(S) — P.4",W//2,H-9,F["xs"],get_crit(flash),anchor="mt")
    else: txt(d,"EDC15P+  038906019DQ  NO FAULTS",W//2,H-9,F["xs"],(28,36,44),anchor="mt")
    return img

def t1_page2(cyl,bat_v,fuel_lh,spd,conn,flash):
    ac=get_accent(); img=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(img)
    _draw_header(d,img,2,conn,theme=1)
    Y=HEADER+6
    txt(d,"INJECTOR BALANCE",8,Y,F["xs"],ac)
    txt(d,"mg/stroke",W-8,Y,F["xs"],DIM,anchor="rt"); Y+=13
    ICO2=34; RH2=42
    for i in range(4):
        val=cyl[i] if (cyl and len(cyl)>i) else None
        col=get_crit(flash) if val and abs(val)>2.8 else get_warn() if val and abs(val)>2.0 else ac if val is not None else DIM
        if val is not None and abs(val)>2.8:
            hl=Image.new("RGB",(W,RH2-1),(20,5,5)); img.paste(hl,(0,Y))
        ico=make_icon(_icon_injector,ICO2,col); img.paste(ico,(4,Y+(RH2-ICO2)//2))
        txt(d,f"CYLINDER {i+1}",ICO2+10,Y+5,F["xs"],DIM)
        if val is not None:
            st,sc=("FAULT",get_crit(flash)) if abs(val)>2.8 else ("WARN",get_warn()) if abs(val)>2.0 else ("OK",get_accent(dim=True))
            txt(d,st,ICO2+10,Y+17,F["xs"],sc)
        vstr=f"{val:+.2f}" if val is not None else "--"
        txt(d,vstr,W-52,Y+10,F["large"],col); txt(d,"mg",W-8,Y+14,F["xs"],ac,anchor="rt")
        d.line([(4,Y+RH2-1),(W-4,Y+RH2-1)],fill=BDR,width=1); Y+=RH2
    if cyl and all(v is not None for v in cyl[:4]):
        total=sum(cyl[:4]); sc=get_crit(flash) if abs(total)>0.5 else get_accent(dim=True)
        txt(d,f"Sum={total:+.3f}mg  {'OK' if abs(total)<0.1 else 'OFF'}",W//2,Y+2,F["xs"],sc,anchor="mt")
    Y+=14; d.line([(4,Y),(W-4,Y)],fill=BDR,width=1); Y+=6
    bv=f"{bat_v:.1f}" if bat_v is not None else "--"
    bvc=get_crit(False) if bat_v and bat_v<11.5 else get_warn() if bat_v and bat_v<12.5 else WHITE
    txt(d,"BATTERY",8,Y,F["xs"],ac); txt(d,bv,8,Y+12,F["large"],bvc)
    try: bvw=int(F["large"].getlength(bv))
    except: bvw=30
    txt(d,"V",8+bvw+2,Y+15,F["xs"],ac)
    txt(d,"FUEL CONS",W//2+4,Y,F["xs"],ac)
    if fuel_lh is not None and spd is not None and spd>5.0:
        l100=fuel_lh/spd*100.0; l100s=f"{l100:.1f}"
        txt(d,l100s,W//2+4,Y+12,F["large"],WHITE)
        try: lw=int(F["large"].getlength(l100s))
        except: lw=30
        txt(d,"L/100",W//2+4+lw+2,Y+15,F["xs"],ac)
    else: txt(d,"-- L/100",W//2+4,Y+12,F["med"],DIM)
    _draw_footer(d,"EDC15P+  INJECTORS + SYSTEMS",theme=1)
    return img

_timer_state={
    "t0100":None,"t0200":None,"t100200":None,
    "r0100":None,"r0200":None,"r100200":None,
    "launched":False,"rolling":False,"peak_boost":None,"speed_prev":0.0,
    "cursor":0,"armed":set(),
}
CORR=1.05

def update_timers(spd_kmh,boost_bar,rpm):
    ts=_timer_state; now=time.time()
    spd=spd_kmh if spd_kmh else 0.0; sr=spd/CORR
    if boost_bar is not None:
        if ts["peak_boost"] is None or boost_bar>ts["peak_boost"]: ts["peak_boost"]=boost_bar
    if not ts["launched"] and sr>5.0 and (0 in ts["armed"] or 2 in ts["armed"]):
        ts["launched"]=True
        if 0 in ts["armed"]: ts["t0100"]=now; ts["r0100"]=None
        if 2 in ts["armed"]: ts["t0200"]=now; ts["r0200"]=None
    if ts["launched"] and ts["r0100"] is None and 0 in ts["armed"] and spd>105.0:
        ts["r0100"]=now-ts["t0100"]; ts["armed"].discard(0)
    if ts["launched"] and ts["r0200"] is None and 2 in ts["armed"] and spd>205.0:
        ts["r0200"]=now-ts["t0200"]; ts["armed"].discard(2)
        if not ts["armed"]: ts["launched"]=False
    prev=ts["speed_prev"]
    if prev<=105.0 and spd>105.0 and 1 in ts["armed"] and not ts["rolling"]:
        ts["rolling"]=True; ts["t100200"]=now; ts["r100200"]=None
    if ts["rolling"] and ts["r100200"] is None and spd>205.0:
        ts["r100200"]=now-ts["t100200"]; ts["rolling"]=False; ts["armed"].discard(1)
    if spd<3.0:
        if ts["launched"]: ts["launched"]=False
        if ts["rolling"]:  ts["rolling"]=False
    ts["speed_prev"]=spd

def arm_timer(idx):
    ts=_timer_state
    if idx in ts["armed"]: ts["armed"].discard(idx)
    else:
        ts["armed"].add(idx)
        if idx==0: ts["r0100"]=None
        elif idx==1: ts["r100200"]=None
        elif idx==2: ts["r0200"]=None

def t1_page3(spd,boost_bar,rpm,conn,flash):
    ac=get_accent(); img=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(img)
    ts=_timer_state; now=time.time(); ICO=28
    _draw_header(d,img,3,conn,theme=1)
    Y=HEADER+4; TRH=50
    TIMERS=[("0 → 100 km/h",ts["r0100"],ts["t0100"] if ts["launched"] else None,0),
            ("100 → 200 km/h",ts["r100200"],ts["t100200"] if ts["rolling"] else None,1),
            ("0 → 200 km/h",ts["r0200"],ts["t0200"] if ts["launched"] else None,2)]
    for ri,(label,result,running,idx) in enumerate(TIMERS):
        ry=Y+ri*TRH; is_sel=(ts["cursor"]==ri); is_armed=idx in ts["armed"]
        if is_sel:
            hl=Image.new("RGB",(W,TRH-2),(18,22,18)); img.paste(hl,(0,ry))
            d.rectangle([0,ry,3,ry+TRH-2],fill=ac)
        ico_col=get_warn() if running else ac if result else get_warn() if is_armed else DIM
        ico=make_icon(_icon_timer,ICO,ico_col); img.paste(ico,(6,ry+(TRH-ICO)//2))
        txt(d,label,ICO+12,ry+5,F["xs"],ac if is_sel else DIM)
        # VALUE — vertically centred in the row
        val_y=ry+(TRH-18)//2
        if result is not None:
            rs=f"{result:.2f}s"; txt(d,rs,ICO+12,val_y,F["large"],WHITE)
            try: rw=int(F["large"].getlength(rs))
            except: rw=50
            txt(d,"✓",ICO+12+rw+4,val_y+3,F["xs"],ac)
        elif running:
            txt(d,f"{now-running:.2f}s",ICO+12,val_y,F["large"],get_warn())
        elif is_armed:
            txt(d,"READY",ICO+12,val_y,F["large"],get_warn())
        else:
            txt(d,"--:--.--",ICO+12,val_y,F["large"],DIM)
        if is_sel:
            hint="RESET" if (result or is_armed or running) else "ARM"
            txt(d,f"[{hint}]",W-6,ry+TRH//2-4,F["xs"],ac,anchor="rm")
        d.line([(4,ry+TRH-1),(W-4,ry+TRH-1)],fill=BDR,width=1)
    Y+=3*TRH+4
    pb=ts["peak_boost"]; pb_col=WHITE if pb is not None else DIM
    ico_t=make_icon(_icon_turbo,ICO,pb_col); img.paste(ico_t,(6,Y+2))
    txt(d,"PEAK BOOST",ICO+12,Y+2,F["xs"],ac)
    txt(d,f"{pb:+.2f} bar" if pb else "-- bar",ICO+12,Y+13,F["large"],pb_col)
    if pb: txt(d,"[hold B2 reset]",W-6,Y+18,F["xs"],(35,45,55),anchor="rt")
    Y+=38
    spd2=spd if spd else 0.0
    if ts["launched"] or ts["rolling"]: sc=get_warn(); st=f"Running  {spd2:.0f}km/h"
    elif ts["armed"]: sc=get_warn(); st="Armed — waiting"
    else: sc=DIM; st="B1=select  B2=arm"
    txt(d,st,W//2,Y,F["xs"],sc,anchor="mt")
    txt(d,"5% correction applied",W//2,Y+11,F["xs"],(30,40,50),anchor="mt")
    _draw_footer(d,"EDC15P+  PERFORMANCE",theme=1)
    return img

def vag_to_pcode(raw):
    if raw is None: return "-----"
    val=raw-16384
    bits=raw&0xFFFF; prefix=['P','C','B','U'][(bits>>14)&0x3]
    if prefix=='P':
        if 0<=val<=999:    return f"P0{val:03d}"
        elif val<=1999:    return f"P1{val-1000:03d}*"
        elif val<=2999:    return f"P2{val-2000:03d}*"
        elif val<=3999:    return f"P3{val-3000:03d}*"
        else:              return "P?????"
    return f"{prefix}{val:04d}"

def decode_status(sb):
    s=(sb&0x01); m=(sb&0x10)
    if s and m: return "STA+MIL",True
    elif s:     return "STATIC",True
    else:       return "SPORADIC",False

def t1_page4(faults,conn,flash):
    ac=get_accent(); img=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(img)
    _draw_header(d,img,4,conn,theme=1)
    fc=len(faults) if faults else 0
    Y=HEADER+6
    txt(d,"ENGINE FAULT CODES",8,Y,F["xs"],ac)
    txt(d,f"{fc} stored",W-8,Y,F["xs"],get_crit(flash) if fc else get_accent(dim=True),anchor="rt")
    Y+=12; d.line([(4,Y),(W-4,Y)],fill=BDR,width=1); Y+=4
    if not fc:
        Y+=20; txt(d,"NO FAULTS",W//2,Y,F["huge"],ac,anchor="mt"); Y+=44
        txt(d,"Engine ECU clean",W//2,Y,F["sm"],get_accent(dim=True),anchor="mt"); Y+=18
        txt(d,"(HVAC/ABS separate modules)",W//2,Y,F["xs"],(35,45,55),anchor="mt")
    else:
        txt(d,"VAG#",6,Y,F["xs"],DIM); txt(d,"CODE",70,Y,F["xs"],DIM)
        txt(d,"STATUS",W-6,Y,F["xs"],DIM,anchor="rt"); Y+=10
        d.line([(4,Y),(W-4,Y)],fill=BDR,width=1); Y+=2
        RH4=22; mr=(H-FOOTER-Y)//RH4-1; shown=0
        for code,sb,_ in (faults or []):
            if shown>=mr: break
            pc=vag_to_pcode(code); sl,ist=decode_status(sb)
            fc_col=get_crit(flash) if ist else get_warn()
            ry=Y+shown*RH4
            if ist:
                hl=Image.new("RGB",(W,RH4-1),(22,5,5)); img.paste(hl,(0,ry))
            txt(d,f"{code:05d}",6,ry+5,F["sm"],fc_col)
            txt(d,pc,70,ry+5,F["sm"],fc_col)
            txt(d,sl,W-6,ry+5,F["xs"],get_crit(flash) if ist else DIM,anchor="rt")
            d.line([(4,ry+RH4-1),(W-4,ry+RH4-1)],fill=BDR,width=1); shown+=1
        ov=fc-shown
        if ov>0: txt(d,f"+{ov} more",W//2,Y+shown*RH4+2,F["xs"],DIM,anchor="mt")
        txt(d,'Search VAG# for full info',W//2,H-FOOTER-13,F["xs2"],(35,48,58),anchor="mt")
    _draw_footer(d,"ENGINE ECU ONLY  |  60s refresh",theme=1)
    return img

# ── Theme 1 Settings (page 5) ──────────────────────────────────────────────
_set_cur=0; _SET_ROWS=4   # colour, depth, brightness, theme

def t1_page5(conn,flash):
    ac=get_accent(); img=Image.new("RGB",(W,H),BG); d=ImageDraw.Draw(img)
    _draw_header(d,img,5,conn,theme=1)
    Y=HEADER+8
    txt(d,"DISPLAY SETTINGS",W//2,Y,F["sm"],ac,anchor="mt"); Y+=18
    d.line([(8,Y),(W-8,Y)],fill=BDR,width=1); Y+=6
    RH5=52
    cidx=SETTINGS["colour"]; cdepth=SETTINGS["depth"]
    cname=COLOUR_PRESETS[cidx][0]
    cbase=COLOUR_PRESETS[cidx][1]
    cscaled=tuple(min(255,int(c*cdepth/10)) for c in cbase)
    ROWS=[
        ("COLOUR",  cname,                     None,None),
        ("DEPTH",   str(SETTINGS["depth"]),    1,   10),
        ("BRIGHT",  str(SETTINGS["brightness"]),1,  10),
        ("THEME",   f"T{SETTINGS['theme']}",   1,    2),
    ]
    for ri,(label,val_s,vmin,vmax) in enumerate(ROWS):
        ry=Y+ri*RH5; is_sel=(_set_cur==ri)
        if is_sel:
            hl=Image.new("RGB",(W,RH5-2),(14,20,14)); img.paste(hl,(0,ry))
            d.rectangle([0,ry,3,ry+RH5-2],fill=ac)
        rc=ac if is_sel else MID
        txt(d,label,W//2,ry+4,F["xs"],rc,anchor="mt")
        # left/right arrows
        txt(d,"<",10,ry+RH5//2-9,F["large"],rc)
        txt(d,">",W-10,ry+RH5//2-9,F["large"],rc,anchor="rt")
        if ri==0:
            # colour swatch
            sw=50; sx=(W-sw)//2
            d.rounded_rectangle([sx,ry+16,sx+sw,ry+38],radius=3,fill=cscaled,outline=ac if is_sel else BDR)
            txt(d,cname,W//2,ry+40,F["xs2"],cscaled if sum(cscaled)>40 else MID,anchor="mt")
        elif vmin is not None:
            bx=30; bw2=W-60; bh=10; by=ry+20
            # track
            d.rounded_rectangle([bx,by,bx+bw2,by+bh],radius=5,fill=(22,20,18))
            # segments
            steps=int(vmax)-int(vmin)
            val_i=int(val_s) if ri!=3 else SETTINGS["theme"]
            for seg in range(steps):
                seg_x=bx+seg*(bw2//steps); seg_w=bw2//steps-2
                filled=(seg<val_i-vmin)
                seg_col=ac if (filled and is_sel) else get_accent(dim=True) if filled else (28,25,22)
                d.rounded_rectangle([seg_x+1,by+1,seg_x+seg_w,by+bh-1],radius=3,fill=seg_col)
            # value label
            txt(d,val_s,W//2,ry+34,F["xs"],WHITE if is_sel else MID,anchor="mt")
        d.line([(4,ry+RH5-1),(W-4,ry+RH5-1)],fill=BDR,width=1)
    hint_y=Y+_SET_ROWS*RH5+4
    txt(d,"B1:move  B2:+  HoldB2:-",W//2,hint_y,F["xs2"],(40,55,45),anchor="mt")
    _draw_footer(d,"SETTINGS  |  auto-saved",theme=1)
    return img

def settings_navigate():
    global _set_cur; _set_cur=(_set_cur+1)%_SET_ROWS

def settings_value_up():
    if _set_cur==0: SETTINGS["colour"]=(SETTINGS["colour"]+1)%10
    elif _set_cur==1: SETTINGS["depth"]=min(10,SETTINGS["depth"]+1)
    elif _set_cur==2: SETTINGS["brightness"]=min(10,SETTINGS["brightness"]+1); apply_brightness(SETTINGS)
    elif _set_cur==3: SETTINGS["theme"]=2 if SETTINGS["theme"]==1 else 1
    save_settings(SETTINGS)

def settings_value_down():
    if _set_cur==0: SETTINGS["colour"]=(SETTINGS["colour"]-1)%10
    elif _set_cur==1: SETTINGS["depth"]=max(1,SETTINGS["depth"]-1)
    elif _set_cur==2: SETTINGS["brightness"]=max(1,SETTINGS["brightness"]-1); apply_brightness(SETTINGS)
    elif _set_cur==3: SETTINGS["theme"]=2 if SETTINGS["theme"]==1 else 1
    save_settings(SETTINGS)

# ══════════════════════════════════════════════════════════════════════════════
# ████████  THEME 2  — BOLD BLOCK STYLE  (Pages 6-10)
# ══════════════════════════════════════════════════════════════════════════════
# Layout inspired by BMW M57 mockup:
#  - No icons, small-caps section labels
#  - Large bold values in coloured highlight boxes
#  - Thin accent progress bars under primary metrics
#  - 2×2 grid boxes for secondary metrics
#  - Boost bar graph on engine page
#  - Clean divider lines, tight grid

T2_BG    = (8,  8,  8)    # near black
T2_CARD  = (18, 16, 14)   # dark card bg
T2_BDR   = (40, 36, 32)   # card border

def _t2_label(d, text, x, y):
    """Small-caps style label in dim colour."""
    txt(d, text.upper(), x, y, F["xs2"], (90, 85, 80))

def _t2_value_box(d, img, x, y, w, h, value_str, unit_str, col, bg=None):
    """Coloured value block — the signature element of Theme 2."""
    bg2 = bg or tuple(max(0, int(c * 0.18)) for c in col)
    d.rounded_rectangle([x, y, x+w, y+h], radius=3, fill=bg2)
    # left accent stripe
    d.rectangle([x, y, x+3, y+h], fill=col)
    # value text
    vw = tlen(None, value_str, F["xlarge"])
    txt(d, value_str, x+10, y+(h-26)//2, F["xlarge"], col)
    txt(d, unit_str,  x+10+vw+4, y+(h+2)//2, F["xs"], (140, 135, 128))

def _t2_small_box(d, label, val_str, unit_str, val_col, x, y, w, h):
    """Small metric box for grid layout."""
    d.rounded_rectangle([x, y, x+w, y+h], radius=3, fill=T2_CARD)
    d.rounded_rectangle([x, y, x+w, y+1], radius=0, fill=(40,36,32))
    _t2_label(d, label, x+6, y+5)
    txt(d, val_str, x+6, y+18, F["med"], val_col)
    try: vw=int(F["med"].getlength(val_str))
    except: vw=len(val_str)*9
    txt(d, unit_str, x+6+vw+2, y+21, F["xs2"], (90,85,80))

def _t2_header(d, img, pnum, connected):
    d.rectangle([0,0,W,HEADER],fill=(6,6,6))
    _draw_logo(d)
    pstr=f"T2·{pnum}"
    txt(d,pstr,W-38,5,F["xs2"],DIM)
    ac=get_accent()
    dot=ac if connected else get_crit(False)
    d.ellipse([W-12,8,W-6,14],fill=dot)
    d.line([(0,HEADER),(W,HEADER)],fill=T2_BDR,width=1)

def t2_page6(rpm,load_pct,boost_mbar,btgt_mbar,vnt_pct,
             coolant,iat,fuel_temp,inj,faults,conn,flash,g7_age=0):
    """Theme 2 — Engine page. Big boost block + bar + grid + boost graph."""
    ac=get_accent()
    img=Image.new("RGB",(W,H),T2_BG); d=ImageDraw.Draw(img)
    _t2_header(d,img,6,conn)

    boost_bar=(boost_mbar-ATM)/1000.0 if boost_mbar is not None else None
    btgt_bar=(btgt_mbar-ATM)/1000.0   if btgt_mbar  is not None else None
    bc,_=boost_dev_col(boost_bar,btgt_bar,flash)

    Y=HEADER+8
    # ── BOOST PRESSURE section ─────────────────────────────────────────────
    _t2_label(d,"BOOST PRESSURE",8,Y); Y+=12
    bv=f"{boost_bar:+.2f}" if boost_bar is not None else "--"
    bunit="BAR"
    _t2_value_box(d,img,8,Y,W-16,38,bv,bunit,bc)
    Y+=42
    # target line
    if btgt_bar is not None:
        txt(d,f"TARGET  {btgt_bar:+.2f} bar",8,Y,F["xs2"],(80,78,75))
        try: tw=int(F["xs2"].getlength(f"TARGET  {btgt_bar:+.2f} bar"))
        except: tw=100
    Y+=10
    # Boost bar  0→2.5 bar range
    draw_bar(d,8,Y,W-16,5,boost_bar,-0.5,2.5,bc); Y+=9

    # ── BOOST HISTORY GRAPH ────────────────────────────────────────────────
    GH=44; GW=W-16; GX=8; GY=Y
    d.rounded_rectangle([GX,GY,GX+GW,GY+GH],radius=2,fill=(12,11,10))
    _t2_label(d,"BOOST LOG",GX+4,GY+2)
    pts=list(BOOST_HISTORY)
    if len(pts)>=2:
        gmin=-0.2; gmax=2.5; pr=gmax-gmin
        graph_pts=[]
        for i,v in enumerate(pts):
            gx2=GX+4+int((i/(len(pts)-1))*(GW-8))
            gy2=GY+GH-4-int(max(0,min(1,(v-gmin)/pr))*(GH-12))
            graph_pts.append((gx2,gy2))
        # zero line
        zy=GY+GH-4-int(max(0,min(1,(0-gmin)/pr))*(GH-12))
        d.line([(GX+4,zy),(GX+GW-4,zy)],fill=(35,33,30),width=1)
        if len(graph_pts)>1:
            d.line(graph_pts,fill=ac,width=2)
            # glow effect — second slightly dimmer line offset
            glow=tuple(max(0,c//3) for c in ac)
            d.line([(x,y+1) for x,y in graph_pts],fill=glow,width=1)
    Y+=GH+6

    # ── 2×2 GRID for secondary metrics ────────────────────────────────────
    BW=(W-20)//2; BH=42; GAP=4
    cols_data=[
        ("COOLANT",  fmt(coolant,".1f"),   "°C",
         get_crit(flash) if coolant and coolant>95 else get_warn() if coolant and coolant>90 else WHITE),
        ("IAT" if g7_age<30 else f"IAT~{int(g7_age)}s",
         fmt(iat,".1f"), "°C",
         get_crit(flash) if iat and iat>60 else get_warn() if iat and iat>45 else WHITE),
        ("INJ QTY",  fmt(inj,".1f"),       "mg",  ac),
        ("VNT",      fmt(vnt_pct,".0f"),   "%",   ac),
    ]
    row1_y=Y; row2_y=Y+BH+GAP
    for i,(lbl,val,unit,vc) in enumerate(cols_data):
        gx2=8 if i%2==0 else 8+BW+GAP
        gy2=row1_y if i<2 else row2_y
        _t2_small_box(d,lbl,val,unit,vc,gx2,gy2,BW,BH)
    Y+=2*BH+GAP+4

    # fault strip
    if faults:
        d.rectangle([0,Y,W,Y+14],fill=(28,5,5))
        txt(d,f"⚠ {len(faults)} FAULT(S) — PAGE 9",W//2,Y+2,F["xs2"],get_crit(flash),anchor="mt")
    _draw_footer(d,"EDC15P+  T2  LIVE ENGINE",theme=2)
    return img

def t2_page7(cyl,bat_v,fuel_lh,spd,conn,flash):
    """Theme 2 — Injector health page."""
    ac=get_accent()
    img=Image.new("RGB",(W,H),T2_BG); d=ImageDraw.Draw(img)
    _t2_header(d,img,7,conn)
    Y=HEADER+8
    _t2_label(d,"INJECTOR BALANCE",8,Y); Y+=11
    d.line([(8,Y),(W-8,Y)],fill=T2_BDR,width=1); Y+=5
    BAR_W=W-90; BAR_H=8
    for i in range(4):
        val=cyl[i] if (cyl and len(cyl)>i) else None
        col=get_crit(flash) if val and abs(val)>2.8 else get_warn() if val and abs(val)>2.0 else ac if val else DIM
        RH=36
        if val is not None and abs(val)>2.8:
            hl=Image.new("RGB",(W,RH-1),(20,5,5)); img.paste(hl,(0,Y))
        # cyl label
        txt(d,f"CYL {i+1}",8,Y+4,F["xs"],DIM)
        st="FAULT" if (val and abs(val)>2.8) else "WARN" if (val and abs(val)>2.0) else "OK" if val else "--"
        st_col=get_crit(flash) if st=="FAULT" else get_warn() if st=="WARN" else ac
        txt(d,st,8,Y+17,F["xs2"],st_col)
        # value
        vs=f"{val:+.2f}" if val is not None else "--"
        txt(d,vs,W-8,Y+8,F["large"],col,anchor="rt")
        txt(d,"mg",W-8,Y+24,F["xs2"],(80,78,75),anchor="rt")
        # bar — centred on zero, -6 to +6 range
        bx=56; bw2=W-bx-50; mid_x=bx+bw2//2
        d.rounded_rectangle([bx,Y+14,bx+bw2,Y+14+BAR_H],radius=2,fill=(22,20,18))
        if val is not None:
            px=int(abs(val)/6.0*(bw2//2))
            px=min(px,bw2//2)
            if val>=0: d.rounded_rectangle([mid_x,Y+14,mid_x+px,Y+14+BAR_H],radius=2,fill=col)
            else:      d.rounded_rectangle([mid_x-px,Y+14,mid_x,Y+14+BAR_H],radius=2,fill=col)
        d.line([(mid_x,Y+12),(mid_x,Y+14+BAR_H+2)],fill=T2_BDR,width=1)
        d.line([(8,Y+RH-1),(W-8,Y+RH-1)],fill=T2_BDR,width=1); Y+=RH

    if cyl and all(v is not None for v in cyl[:4]):
        total=sum(cyl[:4]); sc=get_crit(flash) if abs(total)>0.5 else get_accent(dim=True)
        txt(d,f"ECU sum={total:+.3f}mg",W//2,Y+3,F["xs"],sc,anchor="mt"); Y+=14

    d.line([(8,Y),(W-8,Y)],fill=T2_BDR,width=1); Y+=6
    BW2=(W-20)//2; BH2=38
    bv=f"{bat_v:.1f}" if bat_v is not None else "--"
    bvc=get_crit(False) if bat_v and bat_v<11.5 else get_warn() if bat_v and bat_v<12.5 else WHITE
    _t2_small_box(d,"BATTERY",bv,"V",bvc,8,Y,BW2,BH2)
    if fuel_lh is not None and spd and spd>5:
        l100=fuel_lh/spd*100.0; ls=f"{l100:.1f}"
    else: ls="--"
    _t2_small_box(d,"FUEL L/100",ls,"L",ac,8+BW2+4,Y,BW2,BH2)
    _draw_footer(d,"EDC15P+  T2  INJECTORS",theme=2)
    return img

def t2_page8(spd,boost_bar,rpm,conn,flash):
    """Theme 2 — Performance timers. Bold block timer rows."""
    ac=get_accent()
    img=Image.new("RGB",(W,H),T2_BG); d=ImageDraw.Draw(img)
    ts=_timer_state; now=time.time()
    _t2_header(d,img,8,conn)
    Y=HEADER+8
    _t2_label(d,"PERFORMANCE TIMERS",8,Y); Y+=12
    TRH=56
    TIMERS=[("0 → 100 km/h",ts["r0100"],ts["t0100"] if ts["launched"] else None,0),
            ("100 → 200",    ts["r100200"],ts["t100200"] if ts["rolling"] else None,1),
            ("0 → 200 km/h", ts["r0200"],ts["t0200"] if ts["launched"] else None,2)]
    for ri,(label,result,running,idx) in enumerate(TIMERS):
        ry=Y+ri*TRH; is_sel=(ts["cursor"]==ri); is_armed=idx in ts["armed"]
        # card bg
        card_col=tuple(min(255,int(c*1.4)) for c in T2_CARD) if is_sel else T2_CARD
        d.rounded_rectangle([8,ry,W-8,ry+TRH-3],radius=3,fill=card_col)
        if is_sel: d.rectangle([8,ry,11,ry+TRH-3],fill=ac)
        # label
        _t2_label(d,label,16,ry+5)
        # status badge
        if result is not None:    badge,bcol="DONE",ac
        elif running:              badge,bcol=f"{now-running:.1f}s",get_warn()
        elif is_armed:             badge,bcol="READY",get_warn()
        else:                      badge,bcol="IDLE",DIM
        txt(d,badge,W-14,ry+5,F["xs"],bcol,anchor="rt")
        # main time value
        if result is not None:    vs=f"{result:.2f}"; vc=WHITE
        elif running:              vs=f"{now-running:.2f}"; vc=get_warn()
        elif is_armed:             vs="--:--"; vc=get_warn()
        else:                      vs="--:--"; vc=DIM
        txt(d,vs,16,ry+18,F["xlarge"],vc)
        try: vw=int(F["xlarge"].getlength(vs))
        except: vw=80
        txt(d,"s",16+vw+3,ry+26,F["xs"],(90,85,80))
        if is_sel:
            hint="RESET" if (result or is_armed or running) else "ARM"
            d.rounded_rectangle([W-40,ry+TRH-18,W-10,ry+TRH-5],radius=2,fill=ac if hint=="ARM" else T2_BDR)
            txt(d,hint,W-25,ry+TRH-12,F["xs2"],WHITE if hint=="ARM" else MID,anchor="mm")

    Y+=3*TRH+6
    # Peak boost block
    d.line([(8,Y),(W-8,Y)],fill=T2_BDR,width=1); Y+=6
    _t2_label(d,"PEAK BOOST",8,Y); Y+=12
    pb=ts["peak_boost"]
    pbv=f"{pb:+.2f}" if pb else "--"
    _t2_value_box(d,img,8,Y,W-16,32,pbv,"BAR",ac if pb else DIM)
    Y+=36
    spd2=spd if spd else 0.0
    if ts["launched"] or ts["rolling"]: sc=get_warn(); st=f"Running  {spd2:.0f} km/h"
    elif ts["armed"]: sc=get_warn(); st="Armed — waiting for speed"
    else: sc=DIM; st="B1 select   B2 arm/reset"
    txt(d,st,W//2,Y,F["xs2"],sc,anchor="mt")
    _draw_footer(d,"EDC15P+  T2  PERFORMANCE",theme=2)
    return img

def t2_page9(faults,conn,flash):
    """Theme 2 — Fault codes."""
    ac=get_accent()
    img=Image.new("RGB",(W,H),T2_BG); d=ImageDraw.Draw(img)
    _t2_header(d,img,9,conn)
    fc=len(faults) if faults else 0
    Y=HEADER+8
    _t2_label(d,"ENGINE FAULT CODES",8,Y)
    cnt_col=get_crit(flash) if fc else get_accent(dim=True)
    txt(d,f"{fc}",W-8,Y+1,F["sm"],cnt_col,anchor="rt"); Y+=14
    d.line([(8,Y),(W-8,Y)],fill=T2_BDR,width=1); Y+=5
    if not fc:
        Y+=30
        txt(d,"NO FAULTS",W//2,Y,F["xlarge"],ac,anchor="mt"); Y+=38
        txt(d,"Engine ECU clean",W//2,Y,F["sm"],get_accent(dim=True),anchor="mt"); Y+=16
        txt(d,"(HVAC/ABS not shown)",W//2,Y,F["xs2"],(50,48,45),anchor="mt")
    else:
        RH4=28; mr=(H-FOOTER-Y-20)//RH4; shown=0
        for code,sb,_ in (faults or []):
            if shown>=mr: break
            pc=vag_to_pcode(code); sl,ist=decode_status(sb)
            fc_col=get_crit(flash) if ist else get_warn()
            ry=Y+shown*RH4
            card_bg=(25,5,5) if ist else T2_CARD
            d.rounded_rectangle([8,ry,W-8,ry+RH4-2],radius=2,fill=card_bg)
            if ist: d.rectangle([8,ry,11,ry+RH4-2],fill=get_crit(flash))
            txt(d,f"{code:05d}",14,ry+5,F["sm"],fc_col)
            txt(d,pc,90,ry+5,F["sm"],fc_col)
            txt(d,sl,W-12,ry+8,F["xs2"],DIM,anchor="rt")
            shown+=1
        ov=fc-shown
        if ov>0: txt(d,f"+{ov} more",W//2,Y+shown*RH4+3,F["xs2"],DIM,anchor="mt")
        txt(d,"Search 5-digit VAG# online",W//2,H-FOOTER-13,F["xs2"],(50,48,45),anchor="mt")
    _draw_footer(d,"ENGINE ECU  |  60s refresh",theme=2)
    return img

def t2_page10(conn,flash):
    """Theme 2 — Settings. Full-width segment bars, colour swatch row."""
    ac=get_accent()
    img=Image.new("RGB",(W,H),T2_BG); d=ImageDraw.Draw(img)
    _t2_header(d,img,10,conn)
    Y=HEADER+8
    _t2_label(d,"DISPLAY SETTINGS",8,Y); Y+=12
    d.line([(8,Y),(W-8,Y)],fill=T2_BDR,width=1); Y+=8
    RH=54
    cidx=SETTINGS["colour"]; cdepth=SETTINGS["depth"]
    cbase=COLOUR_PRESETS[cidx][1]
    cscaled=tuple(min(255,int(c*cdepth/10)) for c in cbase)
    cname=COLOUR_PRESETS[cidx][0]
    ROWS=[
        ("COLOUR",    cname,  0, 9,  cidx),
        ("DEPTH",     str(SETTINGS["depth"]),    1, 10, SETTINGS["depth"]),
        ("BRIGHTNESS",str(SETTINGS["brightness"]),1,10, SETTINGS["brightness"]),
        ("THEME",     f"THEME {SETTINGS['theme']}",1,2, SETTINGS["theme"]),
    ]
    for ri,(label,val_s,vmin,vmax,vi) in enumerate(ROWS):
        ry=Y+ri*RH; is_sel=(_set_cur==ri)
        card=tuple(min(255,int(c*1.5)) for c in T2_CARD) if is_sel else T2_CARD
        d.rounded_rectangle([8,ry,W-8,ry+RH-3],radius=3,fill=card)
        if is_sel: d.rectangle([8,ry,11,ry+RH-3],fill=ac)
        _t2_label(d,label,16,ry+4)
        # left/right arrows
        arw=ac if is_sel else DIM
        txt(d,"‹",W-28,ry+RH//2-8,F["large"],arw)
        txt(d,"›",W-10,ry+RH//2-8,F["large"],arw,anchor="rt")
        if ri==0:
            # colour strip — show all 10 as small swatches
            sw=int((W-32)/10); sh=12; sy=ry+18
            for ci,(cn,cb) in enumerate(COLOUR_PRESETS):
                cs=tuple(min(255,int(c*cdepth/10)) for c in cb)
                sx2=16+ci*sw
                d.rounded_rectangle([sx2,sy,sx2+sw-1,sy+sh],radius=2,fill=cs)
                if ci==cidx:
                    d.rounded_rectangle([sx2-1,sy-2,sx2+sw,sy+sh+2],radius=2,outline=WHITE,fill=None)
            txt(d,cname,16,ry+34,F["xs2"],cscaled if sum(cscaled)>50 else MID)
        else:
            steps=int(vmax-vmin)
            bx=16; bw2=W-56; by2=ry+20; bh2=12
            # segmented bar
            seg_w=bw2//steps
            for si in range(steps):
                sx2=bx+si*seg_w+1; sw2=seg_w-2
                filled=(si<int(vi)-int(vmin))
                if filled: fc2=ac if is_sel else get_accent(dim=True)
                else:       fc2=(28,25,22)
                d.rounded_rectangle([sx2,by2,sx2+sw2,by2+bh2],radius=2,fill=fc2)
            txt(d,val_s,W-34,ry+20,F["sm"],WHITE if is_sel else MID)
    hint_y=Y+4*RH+4
    txt(d,"B1:next  B2:up  holdB2:down",W//2,hint_y,F["xs2"],(50,48,45),anchor="mt")
    _draw_footer(d,"SETTINGS  |  auto-saved",theme=2)
    return img

# ══════════════════════════════════════════════════════════════════════════════
# ECU STATE + THREADS
# ══════════════════════════════════════════════════════════════════════════════

_raw={"rpm":None,"load_pct":None,"boost_mbar":None,"boost_target_mbar":None,
      "vnt_pct":None,"coolant":None,"iat":None,"fuel_temp":None,"inj":None,
      "connected":False,"fault_codes":[],"g7_age":0.0,
      "cyl_balance":None,"battery_v":None,"fuel_lh":None,"speed_kmh":None}
_lock=threading.Lock()

def ecu_thread():
    drv=KW1281Driver(ARGS.port,debug=ARGS.debug); retry=2
    g15l=g16l=faultl=g6l=0.0
    while True:
        try:
            log.info("ECU connecting...")
            if not drv.connect():
                with _lock: _raw["connected"]=False
                time.sleep(retry); retry=min(retry*1.5,10); continue
            retry=2
            with _lock: _raw["connected"]=True
            while True:
                dd=drv.read_ecu_data()
                if dd is None or not dd.connected:
                    with _lock: _raw["connected"]=False; break
                with _lock:
                    _raw.update({"rpm":dd.rpm,"load_pct":dd.load_pct,
                        "boost_mbar":dd.boost_mbar,"boost_target_mbar":dd.boost_target_mbar,
                        "vnt_pct":dd.vnt_pct,"coolant":dd.coolant_temp,
                        "iat":dd.intake_air_temp,"fuel_temp":dd.fuel_temp,
                        "inj":dd.injection_qty,"connected":True,
                        "g7_age":dd.timestamp,"cyl_balance":dd.cyl_balance})
                now=time.time()
                if now-g15l>2.0:
                    g15=drv.read_groups([15])
                    if g15 and 15 in g15 and g15[15] and len(g15[15])>=3 and g15[15][2] is not None:
                        with _lock: _raw["fuel_lh"]=g15[15][2]
                    g15l=now
                if now-g6l>1.0:
                    g6=drv.read_groups([6])
                    if g6 and 6 in g6 and g6[6] and g6[6][0] is not None:
                        spd=g6[6][0]
                        if 0<=spd<300:
                            with _lock: _raw["speed_kmh"]=spd
                    g6l=now
                if now-g16l>5.0:
                    g16=drv.read_groups([16])
                    if g16 and 16 in g16 and g16[16] and len(g16[16])>=4 and g16[16][3] is not None:
                        v=g16[16][3]
                        if 8<v<18:
                            with _lock: _raw["battery_v"]=v
                    g16l=now
                if now-faultl>60.0:
                    faults=drv.read_fault_codes()
                    if faults is not None:
                        with _lock: _raw["fault_codes"]=faults
                    faultl=now
        except Exception as e:
            log.error(f"ECU: {e}")
            with _lock: _raw["connected"]=False
            try: drv.ser.close()
            except: pass
            drv=KW1281Driver(ARGS.port,debug=ARGS.debug); time.sleep(retry)

_sim_t=0.0
def update_sim():
    global _sim_t; _sim_t+=0.05
    rpm=900+2400*(0.5+0.5*math.sin(_sim_t*0.4))
    load=max(0,min(100,(rpm-1000)/3500*100+random.uniform(-2,2)))
    spd=max(0,load*2.2+random.uniform(-5,5))
    pg=ARGS.page
    with _lock:
        _raw.update({"rpm":rpm,"load_pct":load,
            "boost_mbar":ATM+load*14+random.uniform(-10,10),
            "boost_target_mbar":ATM+load*13,
            "vnt_pct":max(10,60-load*0.4+random.uniform(-2,2)),
            "coolant":87+math.sin(_sim_t*0.05)*3,
            "iat":32+load*0.10+random.uniform(-0.5,0.5),
            "fuel_temp":70+load*0.05+random.uniform(-0.2,0.2),
            "inj":max(2,load*0.85+random.uniform(-1,1)),
            "connected":True,
            "fault_codes":[(17978,0x01,"Static"),(16955,0x10,"Sporadic")] if pg in (4,9) else [],
            "g7_age":time.time(),
            "cyl_balance":[5.16,-2.91,-1.97,-0.28] if pg in (2,7) else [random.uniform(-1.5,1.5) for _ in range(4)],
            "battery_v":14.2+random.uniform(-0.1,0.1),
            "fuel_lh":max(0.5,load*0.12+random.uniform(-0.2,0.2)),
            "speed_kmh":spd})

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

print(f"FastLifeBG v12 — {'SIM' if ARGS.sim else ARGS.port} — Page {ARGS.page}")
print("Pages 1-5: Theme 1 (icon)    Pages 6-10: Theme 2 (block)")
print("Ctrl+C to exit\n")

if not ARGS.sim:
    threading.Thread(target=ecu_thread,daemon=True).start()

SPF=1/25; boot=Boot()
while not boot.done:
    t0=time.time(); boot.update(); show(boot.draw())
    if ARGS.sim: update_sim()
    time.sleep(max(0,SPF-(time.time()-t0)))

flash=False; flash_t=time.time()
current_page=ARGS.page
_auto_p2_since=None

# Page sets per theme
T1_PAGES=[1,2,3,4,5]; T2_PAGES=[6,7,8,9,10]

try:
    while True:
        t0=time.time()
        if time.time()-flash_t>=0.5: flash=not flash; flash_t=time.time()
        if ARGS.sim: update_sim()
        _poll_buttons()

        # ── button logic ───────────────────────────────────────────────────
        is_timer_page  = current_page in (3,8)
        is_settings_page = current_page in (5,10)

        if is_timer_page:
            if consume_nav():  _timer_state["cursor"]=(_timer_state["cursor"]+1)%3
            if consume_confirm(): arm_timer(_timer_state["cursor"])
            if consume_hold():    _timer_state["peak_boost"]=None
        elif is_settings_page:
            if consume_nav():    settings_navigate()
            if consume_confirm(): settings_value_up()
            if consume_hold():   settings_value_down()
        else:
            if consume_nav() or consume_confirm():
                # Cycle within current theme's page set
                if current_page in T1_PAGES:
                    idx=(T1_PAGES.index(current_page)+1)%len(T1_PAGES)
                    current_page=T1_PAGES[idx]
                else:
                    idx=(T2_PAGES.index(current_page)+1)%len(T2_PAGES)
                    current_page=T2_PAGES[idx]

        # ── read state ─────────────────────────────────────────────────────
        with _lock:
            rpm    =S_rpm.update(_raw["rpm"])
            load   =S_load.update(_raw["load_pct"])
            b_mbar =S_boost.update(_raw["boost_mbar"])
            bt_mbar=S_btgt.update(_raw["boost_target_mbar"])
            vnt    =S_vnt.update(_raw["vnt_pct"])
            coolant=S_cool.update(_raw["coolant"])
            iat    =S_iat.update(_raw["iat"])
            fuel   =S_fuel.update(_raw["fuel_temp"])
            inj    =S_inj.update(_raw["inj"])
            conn   =_raw["connected"]
            faults =_raw.get("fault_codes",[])
            g7_age =time.time()-_raw["g7_age"] if _raw["g7_age"] else 99
            cyl    =_raw["cyl_balance"]
            bat_v  =_raw["battery_v"]
            fuel_lh=_raw["fuel_lh"]
            spd_kmh=_raw["speed_kmh"]

        boost_bar=(b_mbar-ATM)/1000.0  if b_mbar  else None
        btgt_bar =(bt_mbar-ATM)/1000.0 if bt_mbar else None

        # Update boost history for graph
        if boost_bar is not None: BOOST_HISTORY.append(boost_bar)

        # Alarms
        bc,do_beep=boost_dev_col(boost_bar,btgt_bar,flash)
        if do_beep: beep_if_due("critical",interval=4.0)
        if coolant and coolant>95: beep_if_due("warning",interval=10.0)
        if fuel    and fuel>95:    beep_if_due("warning",interval=10.0)

        # Timers
        update_timers(spd_kmh,boost_bar,rpm)

        # Auto-switch p1→p2 / p6→p7 on critical injector
        cyl_crit=(cyl and any(len(cyl)>i and cyl[i] and abs(cyl[i])>2.8 for i in range(4)))
        now_t=time.time()
        if cyl_crit:
            if _auto_p2_since is None: _auto_p2_since=now_t
        else: _auto_p2_since=None
        auto_inj=(_auto_p2_since is not None and now_t-_auto_p2_since>5.0)

        ap=current_page
        if auto_inj and current_page==1: ap=2
        if auto_inj and current_page==6: ap=7

        # ── draw ───────────────────────────────────────────────────────────
        if   ap==1:  img=t1_page1(rpm,load,b_mbar,bt_mbar,vnt,coolant,iat,fuel,inj,faults,conn,flash,g7_age)
        elif ap==2:  img=t1_page2(cyl,bat_v,fuel_lh,spd_kmh,conn,flash)
        elif ap==3:  img=t1_page3(spd_kmh,boost_bar,rpm,conn,flash)
        elif ap==4:  img=t1_page4(faults,conn,flash)
        elif ap==5:  img=t1_page5(conn,flash)
        elif ap==6:  img=t2_page6(rpm,load,b_mbar,bt_mbar,vnt,coolant,iat,fuel,inj,faults,conn,flash,g7_age)
        elif ap==7:  img=t2_page7(cyl,bat_v,fuel_lh,spd_kmh,conn,flash)
        elif ap==8:  img=t2_page8(spd_kmh,boost_bar,rpm,conn,flash)
        elif ap==9:  img=t2_page9(faults,conn,flash)
        else:        img=t2_page10(conn,flash)

        show(img)
        time.sleep(max(0,SPF-(time.time()-t0)))

except KeyboardInterrupt:
    if GPIO_OK: GPIO.cleanup()
    print("\nDone")
