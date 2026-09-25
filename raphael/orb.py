"""
Преміум-орб Рафаеля: офанім із рунами, справжнє сяйво (PIL gaussian blur),
прозоре шарове вікно Windows (per-pixel alpha через UpdateLayeredWindow).

Використання з raphael/ui.py (компактний режим):
    from raphael.orb import OrbWindow, AVAILABLE
    orb = OrbWindow(root, on_expand=callback)
    orb.show(x, y, state="idle"); orb.set_state("listening"); orb.hide()

Усе обгорнуто в try/except — якщо платформа не Windows або щось ламається,
AVAILABLE = False, і ui.py відкочується на простий tkinter-орб.
"""
import math
import threading
import tkinter as tk

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageChops
    import ctypes
    from ctypes import wintypes
    AVAILABLE = (__import__("os").name == "nt")
except Exception:
    AVAILABLE = False

# ── Параметри рендера ─────────────────────────────────────────────────────────
SIZE = 240                           # полотно з запасом: сяйво згасає в прозорих полях,
                                     # тож нема квадратної «рамки» (сам орб лишається малим)
CX = CY = SIZE // 2

# Розмір орба НА ЕКРАНІ. Рендер завжди йде в SIZE, а перед показом кадр
# зменшується — тому дрібний орб лишається гладким.
ORB_SIZE   = 130                     # було 240: орб затуляв частину екрана
ORB_MARGIN = 28                      # відступ від краю екрана
ORB_CORNER = "br"                    # br, bl, tr, tl — у якому куті висить
FRAMES = 48                          # більше кадрів → плавніша (особливо повільна) анімація
RUNES = "ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃᛇᛈᛉᛊᛏᛒᛖᛗᛚᛜᛟᛞ"
RUNE_N = 26                          # рун на кільце (більше деталізації)
BANDW = (13, 8, 2)                   # товщина смуги: темний край / золото / блік

FOCAL = 260                          # сила перспективи (3D-проєкція)

# Кожне кільце — коло радіуса R у 3D з базовою орієнтацією (Euler°) і обертанням
# по 3 ОСЯХ (обертів за цикл по X,Y,Z — цілі → рух незалежний, цикл безшовний).
# Усі кільця ОДНАКОВОГО діаметра (ідентичні), різні орієнтації → армілярна сфера.
# Ортогональні площини не збігаються → перетини чисті, без клешіння.
RINGS = (
    dict(R=60, base=(0, 0, 0),   turns=(1, 0, 1)),
    dict(R=60, base=(90, 0, 0),  turns=(0, 1, 1)),
    dict(R=60, base=(0, 90, 0),  turns=(2, 0, 1)),
    dict(R=60, base=(45, 35, 0), turns=(1, -1, 1)),
)

STATES = {
    "idle":      dict(rd=(122, 93, 34),  rg=(217, 182, 90),  rh=(255, 243, 207), core=(255, 216, 116), gl=(46, 35, 16),  cs=1.0,  gi=1.0,  delay=105),
    "listening": dict(rd=(44, 85, 102),  rg=(134, 201, 222), rh=(220, 245, 255), core=(159, 230, 255), gl=(16, 48, 61),  cs=1.05, gi=1.15, delay=72),
    "thinking":  dict(rd=(122, 77, 24),  rg=(240, 181, 78),  rh=(255, 226, 154), core=(255, 193, 90),  gl=(58, 34, 8),   cs=1.05, gi=1.2,  delay=46),
    "speaking":  dict(rd=(138, 107, 40), rg=(255, 227, 154), rh=(255, 255, 255), core=(255, 246, 219), gl=(58, 44, 16),  cs=1.3,  gi=1.45, delay=60),
}

_FONT_PATHS = (r"C:\Windows\Fonts\seguihis.ttf", r"C:\Windows\Fonts\seguisym.ttf",
               r"C:\Windows\Fonts\segoeui.ttf")
_font_cache = {}


def _font(px):
    if px in _font_cache:
        return _font_cache[px]
    f = None
    for p in _FONT_PATHS:
        try:
            f = ImageFont.truetype(p, px)
            break
        except Exception:
            continue
    if f is None:
        f = ImageFont.load_default()
    _font_cache[px] = f
    return f


def _star_pts(cx, cy, R, r):
    pts = []
    for k in range(8):
        rad = R if k % 2 == 0 else r
        a = math.pi / 2 + k * math.pi / 4
        pts.append((cx + rad * math.cos(a), cy - rad * math.sin(a)))
    return pts


def _rot(x, y, z, ax, ay, az):
    """Обертання точки по 3 осях (Euler, градуси)."""
    ax, ay, az = math.radians(ax), math.radians(ay), math.radians(az)
    ca, sa = math.cos(ax), math.sin(ax); y, z = y * ca - z * sa, y * sa + z * ca
    cb, sb = math.cos(ay), math.sin(ay); x, z = x * cb + z * sb, -x * sb + z * cb
    cg, sg = math.cos(az), math.sin(az); x, y = x * cg - y * sg, x * sg + y * cg
    return x, y, z


def _project(x, y, z):
    """Перспективна проєкція в екран → (sx, sy, z, scale)."""
    s = FOCAL / (FOCAL - z)
    return CX + x * s, CY + y * s, z, s


def _lerp(c1, c2, t):
    return (int(c1[0] + (c2[0] - c1[0]) * t),
            int(c1[1] + (c2[1] - c1[1]) * t),
            int(c1[2] + (c2[2] - c1[2]) * t))


# ── Обʼємні кільця: 3D-меш зі стінками + затінення за світлом ─────────────────
SS = 2                               # supersampling (гладкі краї)
HW = 3.5                             # тонка радіальна ширина → малі бічні грані (без рун)
HT = 8.0                             # висока ЗОВНІШНЯ грань (рунний обід) — ширша де руни
LIGHT = (0.28, -0.47, 0.84)          # напрям світла (нормалізований)
VIEW = (24, -28, 0)                   # глобальний нахил (ізометрія) → кільця відкриті, видно ядро


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _shade(p0, p1, p3, pal, dark):
    """Колір грані за нормаллю до світла (ламберт + ambient)."""
    n = _cross(_sub(p1, p0), _sub(p3, p0))
    m = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) or 1.0
    lit = abs((n[0] * LIGHT[0] + n[1] * LIGHT[1] + n[2] * LIGHT[2]) / m)
    sh = 0.25 + 0.80 * lit                       # ширший тональний діапазон → більше деталі
    col = _lerp(pal["rd"], pal["rg"], min(1.0, sh))
    if sh > 0.82:
        col = _lerp(col, pal["rh"], (sh - 0.82) / 0.18 * 0.85)
    if dark:
        col = _lerp(pal["rd"], col, 0.34)        # бічні грані помітно темніші → тінь на боках
    return col


def _ring_faces(R, euler, pal):
    """Грані обʼємного кільця: список (depth, [4 екранні точки×SS], колір)."""
    M = 48
    ro, ri = R + HW, R - HW
    Ot, Ob, It, Ib = [], [], [], []
    for k in range(M + 1):
        th = k / M * 2 * math.pi
        c, sn = math.cos(th), math.sin(th)
        Ot.append(_rot(*_rot(ro * c, ro * sn,  HT, *euler), *VIEW))
        Ob.append(_rot(*_rot(ro * c, ro * sn, -HT, *euler), *VIEW))
        It.append(_rot(*_rot(ri * c, ri * sn,  HT, *euler), *VIEW))
        Ib.append(_rot(*_rot(ri * c, ri * sn, -HT, *euler), *VIEW))

    def scr(p):
        sx, sy, _z, _s = _project(*p)
        return (sx * SS, sy * SS)

    faces = []
    for k in range(M):
        for quad, dark in (((Ot[k], Ot[k + 1], It[k + 1], It[k]), True),    # верхня грань (бічна — тінь)
                           ((Ob[k], Ob[k + 1], Ib[k + 1], Ib[k]), True),    # нижня грань (бічна — тінь)
                           ((Ot[k], Ot[k + 1], Ob[k + 1], Ob[k]), False)):  # зовнішня грань з рунами (світла)
            depth = (quad[0][2] + quad[1][2] + quad[2][2] + quad[3][2]) * 0.25
            col = _shade(quad[0], quad[1], quad[3], pal, dark)
            faces.append((depth, [scr(quad[0]), scr(quad[1]), scr(quad[2]), scr(quad[3])], col))
    return faces


def _core_img(W, pal):
    cs = pal["cs"]
    c = W // 2
    im = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    dc = ImageDraw.Draw(im)
    cr = int(27 * cs * SS)
    dc.ellipse([c - cr, c - cr, c + cr, c + cr], fill=(23, 15, 44, 255), outline=pal["rh"] + (255,), width=2 * SS)
    dc.polygon(_star_pts(c, c, int(22 * cs * SS), int(8 * cs * SS)), fill=(255, 255, 255, 255))
    dc.ellipse([c - 6 * SS, c - 6 * SS, c + 6 * SS, c + 6 * SS], fill=(255, 255, 255, 255))
    return im


def render_orb_frame(phase, state):
    """Кадр орба, phase∈[0,1). 4 ОБʼЄМНІ кільця тумблять у 3D + оклюзія ядра. Supersampled."""
    pal = STATES.get(state, STATES["idle"])
    W = SIZE * SS

    faces, runes = [], []
    for ring in RINGS:
        bx, by, bz = ring["base"]
        tx, ty, tz = ring["turns"]
        euler = (bx + tx * phase * 360.0, by + ty * phase * 360.0, bz + tz * phase * 360.0)
        R = ring["R"]
        faces.extend(_ring_faces(R, euler, pal))
        rr = R + HW + 1.0                                  # на ЗОВНІШНЬОМУ ободі (рим)
        for j in range(RUNE_N):
            th = j / RUNE_N * 2 * math.pi
            sx, sy, zz, s = _project(*_rot(*_rot(rr * math.cos(th), rr * math.sin(th), 0.0, *euler), *VIEW))
            if zz < 0:                                     # лише ближня дуга обода (без протікання)
                continue
            runes.append((zz, sx * SS, sy * SS, s, RUNES[j % len(RUNES)]))
    faces.sort(key=lambda f: f[0])
    runes.sort(key=lambda r: r[0])

    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rfont = {}

    def _rune(zz, sx, sy, s, ch):
        px = max(7, int(round(10 * s))) * SS
        f = rfont.get(px) or _font(px)
        rfont[px] = f
        t = max(0.0, min(1.0, (zz + 70) / 140))
        d.text((sx, sy), ch, font=f, fill=pal["gl"] + (int(120 + 130 * t),), anchor="mm")

    fi = next((i for i, f in enumerate(faces) if f[0] >= 0), len(faces))   # межа задні/передні
    ri = next((i for i, r in enumerate(runes) if r[0] >= 0), len(runes))
    for _dep, pts, col in faces[:fi]:                # задні грані
        d.polygon(pts, fill=col + (255,))
    for zz, sx, sy, s, ch in runes[:ri]:             # задні руни
        _rune(zz, sx, sy, s, ch)
    img.alpha_composite(_core_img(W, pal))           # ядро між задніми й передніми
    for _dep, pts, col in faces[fi:]:                # передні грані (над ядром)
        d.polygon(pts, fill=col + (255,))
    for zz, sx, sy, s, ch in runes[ri:]:             # передні руни
        _rune(zz, sx, sy, s, ch)

    img = img.resize((SIZE, SIZE), Image.LANCZOS)    # downsample → гладкі краї

    cs, gi = pal["cs"], pal["gi"]
    ambient = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(ambient).ellipse([CX - 60, CY - 60, CX + 60, CY + 60], fill=pal["core"] + (int(64 * gi),))
    ambient = ambient.filter(ImageFilter.GaussianBlur(46))
    glow_src = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow_src).ellipse([CX - int(34 * cs), CY - int(34 * cs), CX + int(34 * cs), CY + int(34 * cs)], fill=pal["core"] + (255,))
    core_glow = glow_src.filter(ImageFilter.GaussianBlur(int(28 * gi)))
    ring_glow = img.filter(ImageFilter.GaussianBlur(9))
    ring_glow.putalpha(ring_glow.getchannel("A").point(lambda a: int(a * 0.4 * gi)))

    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.alpha_composite(ambient)
    out.alpha_composite(core_glow)
    out.alpha_composite(ring_glow)
    out.alpha_composite(img)
    # ядро завжди трохи світиться крізь передні кільця → центр видно
    cap = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(cap).ellipse([CX - int(22 * cs), CY - int(22 * cs), CX + int(22 * cs), CY + int(22 * cs)], fill=pal["core"] + (255,))
    cap = cap.filter(ImageFilter.GaussianBlur(int(14 * gi)))
    cap.putalpha(cap.getchannel("A").point(lambda a: int(a * 0.5)))
    out.alpha_composite(cap)
    # Малюємо у повному розмірі, а на екран віддаємо зменшену копію: так
    # орб маленький, але лишається гладким (рендер великий → downsample).
    if ORB_SIZE != SIZE:
        out = out.resize((ORB_SIZE, ORB_SIZE), Image.LANCZOS)
    return out


# ── Шарове вікно з per-pixel alpha ────────────────────────────────────────────
def _premultiply_bgra(img):
    img = img.convert("RGBA")
    r, g, b, a = img.split()
    r = ImageChops.multiply(r, a)
    g = ImageChops.multiply(g, a)
    b = ImageChops.multiply(b, a)
    return Image.merge("RGBA", (b, g, r, a)).tobytes("raw", "RGBA")


class _BITMAPINFOHEADER(ctypes.Structure if AVAILABLE else object):
    if AVAILABLE:
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

if AVAILABLE:
    class _POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class _SIZE(ctypes.Structure):
        _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]

    class _BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                    ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


def _paint_layered(hwnd, img, x, y):
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    w, h = img.size
    bits = _premultiply_bgra(img)

    screen_dc = user32.GetDC(0)
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)

    bmi = _BITMAPINFOHEADER()
    bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.biWidth = w
    bmi.biHeight = -h          # top-down
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    bmi.biCompression = 0      # BI_RGB
    ppv = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(bmi), 0, ctypes.byref(ppv), None, 0)
    ctypes.memmove(ppv, bits, len(bits))
    old = gdi32.SelectObject(mem_dc, hbmp)

    size = _SIZE(w, h)
    src = _POINT(0, 0)
    dst = _POINT(int(x), int(y))
    blend = _BLENDFUNCTION(0, 0, 255, 1)   # AC_SRC_OVER, AC_SRC_ALPHA
    ULW_ALPHA = 2
    user32.UpdateLayeredWindow(hwnd, screen_dc, ctypes.byref(dst), ctypes.byref(size),
                               mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA)
    gdi32.SelectObject(mem_dc, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(0, screen_dc)


class OrbWindow:
    """Окреме прозоре вікно з анімованим орбом. on_expand() — коли розгорнути."""

    def __init__(self, parent, on_expand=None):
        self.parent = parent
        self.on_expand = on_expand or (lambda: None)
        self.state = "idle"
        self.x, self.y = self._corner_pos(parent)
        self._frames = {}          # state -> [PIL frames]
        self._idx = 0
        self._running = False
        self._after_id = None
        self._drag_off = (0, 0)
        self._layered = False

        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.geometry(f"{ORB_SIZE}x{ORB_SIZE}+{self.x}+{self.y}")
        self.win.attributes("-topmost", True)
        self.win.withdraw()
        self.win.bind("<Button-1>", self._drag_start)
        self.win.bind("<B1-Motion>", self._drag_move)
        self.win.bind("<Double-Button-1>", lambda e: self.on_expand())

    @staticmethod
    def _corner_pos(parent):
        """Кут екрана з відступом. Падає на (80, 80), якщо розмірів не дістати."""
        try:
            sw, sh = parent.winfo_screenwidth(), parent.winfo_screenheight()
            right  = sw - ORB_SIZE - ORB_MARGIN
            bottom = sh - ORB_SIZE - ORB_MARGIN - 48      # запас на панель задач
            return {
                "br": (right, bottom), "bl": (ORB_MARGIN, bottom),
                "tr": (right, ORB_MARGIN), "tl": (ORB_MARGIN, ORB_MARGIN),
            }.get(ORB_CORNER, (right, bottom))
        except Exception:
            return 80, 80

    # ── Win32 ──
    def _hwnd(self):
        return ctypes.windll.user32.GetParent(self.win.winfo_id())

    def _ensure_layered(self):
        if self._layered:
            return
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        u = ctypes.windll.user32
        h = self._hwnd()
        getf = getattr(u, "GetWindowLongPtrW", u.GetWindowLongW)
        setf = getattr(u, "SetWindowLongPtrW", u.SetWindowLongW)
        ex = getf(h, GWL_EXSTYLE)
        setf(h, GWL_EXSTYLE, ex | WS_EX_LAYERED)
        self._layered = True

    # ── Кадри (лінива генерація + кеш) ──
    def _ensure_frames(self, state):
        if state in self._frames:
            return
        # один кадр одразу (щоб не блимало), решта — у фоні
        self._frames[state] = [render_orb_frame(0.0, state)]

        def _build():
            fr = [render_orb_frame(i / FRAMES, state) for i in range(FRAMES)]
            self._frames[state] = fr
        threading.Thread(target=_build, daemon=True).start()

    # ── Публічне API ──
    def show(self, x=None, y=None, state=None):
        if x is not None:
            self.x = x
        if y is not None:
            self.y = y
        if state:
            self.state = state
        self._ensure_frames(self.state)
        self.win.deiconify()
        self.win.update_idletasks()
        try:
            self._ensure_layered()
        except Exception:
            pass
        self._running = True
        self._idx = 0
        self._tick()

    def hide(self):
        self._running = False
        if self._after_id:
            try:
                self.parent.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        self.win.withdraw()

    def set_state(self, state):
        if state == self.state:
            return
        self.state = state
        self._ensure_frames(state)
        self._idx = 0

    def destroy(self):
        self.hide()
        try:
            self.win.destroy()
        except Exception:
            pass

    # ── Анімація ──
    def _tick(self):
        if not self._running:
            return
        frames = self._frames.get(self.state) or self._frames.get("idle")
        if frames:
            frame = frames[self._idx % len(frames)]
            self._idx += 1
            try:
                _paint_layered(self._hwnd(), frame, self.x, self.y)
            except Exception:
                pass
        delay = STATES.get(self.state, STATES["idle"]).get("delay", 60)
        self._after_id = self.parent.after(delay, self._tick)

    # ── Перетягування ──
    def _drag_start(self, e):
        self._drag_off = (e.x_root - self.x, e.y_root - self.y)

    def _drag_move(self, e):
        self.x = e.x_root - self._drag_off[0]
        self.y = e.y_root - self._drag_off[1]
        frames = self._frames.get(self.state)
        if frames:
            try:
                _paint_layered(self._hwnd(), frames[self._idx % len(frames)], self.x, self.y)
            except Exception:
                pass
