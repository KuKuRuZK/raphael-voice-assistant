"""
Плаваюче вікно з анімацією стану та іконка трею.
"""
import logging
import math
import tkinter as tk

from PIL import Image, ImageDraw

from raphael import tts

log = logging.getLogger("Лін")

# Преміум-орб (окремий модуль): прозоре шарове вікно зі справжнім сяйвом.
# Якщо модуль/платформа недоступні — _PREMIUM_ORB=False і працює запасний tkinter-орб.
try:
    from raphael import orb as _orb
    _PREMIUM_ORB = bool(_orb.AVAILABLE)
except Exception:
    _orb = None
    _PREMIUM_ORB = False


# ============================================================
#  SYSTEM TRAY
# ============================================================

def _star_points(cx, cy, R, r):
    """Точки 4-кутної зірки — божественна іскра в емблемі Рафаеля."""
    pts = []
    for k in range(8):
        rad = R if k % 2 == 0 else r
        a = math.pi / 2 + k * math.pi / 4
        pts.append((cx + rad * math.cos(a), cy - rad * math.sin(a)))
    return pts


def _draw_emblem(draw, S):
    """Малює емблему-німб Рафаеля на полотні ImageDraw розміром S×S (для трею/іконки)."""
    c = S / 2.0
    u = S / 64.0   # масштаб відносно базових 64px
    draw.ellipse([3*u, 3*u, S-3*u, S-3*u], fill=(18, 16, 38, 255))       # небесний диск
    for i in range(12):                                                  # промені німба
        a = i * math.pi / 6
        draw.line([c + 22*u*math.cos(a), c + 22*u*math.sin(a),
                   c + 30*u*math.cos(a), c + 30*u*math.sin(a)],
                  fill=(201, 168, 78, 255), width=max(1, int(round(2*u))))
    draw.ellipse([11*u, 11*u, S-11*u, S-11*u], outline=(233, 207, 134, 255),
                 width=max(2, int(round(3*u))))                          # золотий німб
    draw.ellipse([23*u, 23*u, S-23*u, S-23*u], fill=(20, 18, 40, 255),
                 outline=(255, 240, 192, 255), width=max(1, int(round(2*u))))  # ядро
    draw.polygon(_star_points(c, c, 9.5*u, 3.4*u), fill=(255, 246, 224, 255))  # зірка


def create_tray_icon() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    _draw_emblem(ImageDraw.Draw(img), 64)
    return img


# ============================================================
#  GUI
# ============================================================

class LinUI:
    """Маленьке плаваюче вікно з анімацією стану."""

    # Палітра «Рафаель» — небесний мудрець: золото, тепле біле, блакить
    STATE_COLORS = {
        "idle":      ("#141228", "#caa84e", "✦",   "#e9cf86"),
        "listening": ("#10204a", "#7fb0ff", "🎤",  "#dcebff"),
        "thinking":  ("#1a1530", "#e9cf86", "✶",   "#fff0c0"),
        "speaking":  ("#1c1633", "#ffe9a8", "🔊",  "#fff6e0"),
    }

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Рафаель")
        self.root.geometry("310x218+40+40")
        self.root.configure(bg="#0a0913")
        self.root.resizable(False, False)
        self.root.overrideredirect(True)           # без рамки OS
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.9)        # напівпрозоре

        # Dragging (прив'язуємо до всіх зон вікна після їх створення)
        self._dx = self._dy = 0

        self.state  = "idle"
        self._frame = 0
        self._idle_drawn = False   # щоб не перемальовувати статичний idle-стан
        self._compact = False      # режим «лише орб» (плаваюча іконка)
        self._orb_win = None       # преміум-орб (orb.OrbWindow), лінива ініціалізація
        self._premium_active = False
        self._last_you = ""
        self._last_lin = ""

        # ── Повний інтерфейс (ховається в компактному режимі) ──
        self._full = tk.Frame(self.root, bg="#0a0913")
        self._full.pack(fill="both", expand=True)

        # ── Header ───────────────────────────────────────────
        hdr = tk.Frame(self._full, bg="#14111f", pady=5)
        hdr.pack(fill="x")
        hdr.bind("<Button-1>",  self._drag_start)
        hdr.bind("<B1-Motion>", self._drag_move)

        tk.Label(hdr, text="✦  Р А Ф А Е Л Ь", fg="#e9cf86", bg="#14111f",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=12)

        # Кнопка закрити (тільки ховає — повний вихід лише через трей)
        btn_x = tk.Label(hdr, text="✕", fg="#6a5f47", bg="#14111f",
                          font=("Segoe UI", 10), cursor="hand2", padx=8)
        btn_x.pack(side="right")
        btn_x.bind("<Button-1>", lambda e: self.hide())
        btn_x.bind("<Enter>",    lambda e: btn_x.configure(fg="#cc6688"))
        btn_x.bind("<Leave>",    lambda e: btn_x.configure(fg="#6a5f47"))

        # Кнопка «згорнути до орба» — лишає тільки іконку Рафа
        btn_min = tk.Label(hdr, text="◯", fg="#6a5f47", bg="#14111f",
                           font=("Segoe UI", 10), cursor="hand2", padx=6)
        btn_min.pack(side="right")
        btn_min.bind("<Button-1>", lambda e: self.set_compact(True))
        btn_min.bind("<Enter>",    lambda e: btn_min.configure(fg="#e9cf86"))
        btn_min.bind("<Leave>",    lambda e: btn_min.configure(fg="#6a5f47"))

        # Кнопка "стоп TTS"
        btn_stop = tk.Label(hdr, text="⏹", fg="#6a5f47", bg="#14111f",
                             font=("Segoe UI", 10), cursor="hand2", padx=6)
        btn_stop.pack(side="right")
        btn_stop.bind("<Button-1>", lambda e: self.stop_speaking())
        btn_stop.bind("<Enter>",    lambda e: btn_stop.configure(fg="#ffaa33"))
        btn_stop.bind("<Leave>",    lambda e: btn_stop.configure(fg="#6a5f47"))

        self._mode_lbl = tk.Label(hdr, text="● normal", fg="#5f5a47",
                                   bg="#14111f", font=("Segoe UI", 7))
        self._mode_lbl.pack(side="right", padx=4)

        # ── Body ──────────────────────────────────────────────
        body = tk.Frame(self._full, bg="#0a0913")
        body.pack(fill="x", padx=10, pady=(8, 4))

        self.canvas = tk.Canvas(body, width=84, height=84, bg="#0a0913",
                                 highlightthickness=0)
        self.canvas.pack(side="left")

        txt = tk.Frame(body, bg="#0a0913")
        txt.pack(side="left", fill="both", expand=True, padx=(10, 0))

        self._status = tk.StringVar(value="Чекаю на команду...")
        tk.Label(txt, textvariable=self._status, fg="#caa84e", bg="#0a0913",
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(fill="x")

        tk.Frame(txt, bg="#2a2414", height=1).pack(fill="x", pady=(3, 5))

        self._subtext = tk.StringVar(value="")
        tk.Label(txt, textvariable=self._subtext, fg="#cbb98a", bg="#0a0913",
                 font=("Segoe UI", 8), anchor="nw", wraplength=188,
                 justify="left").pack(fill="both", expand=True)

        # ── Chat history (остання репліка) ────────────────────
        tk.Frame(self._full, bg="#1a1712", height=1).pack(fill="x")
        hist = tk.Frame(self._full, bg="#07060d", pady=5)
        hist.pack(fill="x")

        self._you_var = tk.StringVar(value="")
        tk.Label(hist, textvariable=self._you_var,
                 fg="#7f93b8", bg="#07060d",
                 font=("Consolas", 7), anchor="w", padx=10,
                 wraplength=290).pack(fill="x")

        self._lin_var = tk.StringVar(value="")
        tk.Label(hist, textvariable=self._lin_var,
                 fg="#caa84e", bg="#07060d",
                 font=("Consolas", 7), anchor="w", padx=10,
                 wraplength=290).pack(fill="x")

        # ── Компактний орб (показується замість _full) ────────
        # bg = ключ прозорості (#010203): у компактному режимі робить кути вікна
        # повністю прозорими — лишається тільки круглий орб.
        self._orb = tk.Canvas(self.root, width=96, height=96, bg="#010203",
                              highlightthickness=0)
        self._orb.bind("<Button-1>",  self._drag_start)
        self._orb.bind("<B1-Motion>", self._drag_move)
        # Подвійний клік по орбу — розгорнути назад у вікно
        self._orb.bind("<Double-Button-1>", lambda e: self.set_compact(False))

        # Прив'язуємо drag до всіх ключових зон
        for w in (self.root, self._full, hdr, body, txt, hist, self.canvas):
            w.bind("<Button-1>",  self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        # Подвійний клік по аватару — миттєво зупинити озвучення («тихо»)
        self.canvas.bind("<Double-Button-1>", lambda e: self.stop_speaking())

        self._animate()

    # ── Drag ─────────────────────────────────────────────────
    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y()

    def _drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def hide(self):
        """Лише ховає плаваюче вікно. Повне закриття — ТІЛЬКИ через меню трею."""
        try:
            self.root.withdraw()
        except Exception:
            pass

    def set_compact(self, on: bool):
        """Перемикає повне вікно ↔ режим «лише орб» (плаваюча іконка Рафа)."""
        on = bool(on)
        if on == self._compact:
            return
        self._compact = on
        self._idle_drawn = False          # перемалювати аватар на активному полотні
        try:
            if on:
                gx, gy = self.root.winfo_x() + 100, self.root.winfo_y() + 60
                # ── Преміум-орб: окреме прозоре вікно зі сяйвом ──
                if _PREMIUM_ORB:
                    try:
                        if self._orb_win is None:
                            self._orb_win = _orb.OrbWindow(self.root, on_expand=lambda: self.set_compact(False))
                        self.root.withdraw()
                        self._orb_win.show(gx, gy, self.state)
                        self._premium_active = True
                        return
                    except Exception as e:
                        log.error(f"Преміум-орб не показався, fallback на tkinter: {e}")
                        self._premium_active = False
                        try:
                            self.root.deiconify()
                        except Exception:
                            pass
                # ── Запасний tkinter-орб (color-key прозорість) ──
                self._full.pack_forget()
                self._orb.pack(fill="both", expand=True)
                self.root.geometry("96x96")
                self.root.attributes("-transparentcolor", "#010203")
                self.root.attributes("-alpha", 1.0)
                self.root.deiconify()
                self.root.attributes("-topmost", True)
            else:
                # ── Назад у повне вікно ──
                if self._premium_active and self._orb_win is not None:
                    self._orb_win.hide()
                    self._premium_active = False
                self._orb.pack_forget()
                self._full.pack(fill="both", expand=True)
                self.root.geometry("310x218")
                self.root.attributes("-transparentcolor", "")
                self.root.attributes("-alpha", 0.9)
                self.root.deiconify()
                self.root.attributes("-topmost", True)
        except Exception as e:
            log.debug(f"set_compact: {e}")

    def safe_toggle_compact(self):
        """Перемкнути компактний режим з будь-якого потоку (для трею)."""
        try:
            self.root.after(0, lambda: self.set_compact(not self._compact))
        except Exception:
            pass

    # ── State ─────────────────────────────────────────────────
    def set_state(self, state: str, text: str = ""):
        self.state = state
        if self._premium_active and self._orb_win is not None:
            try:
                self._orb_win.set_state(state)
            except Exception:
                pass
        labels = {
            "idle":      "Чекаю на команду...",
            "listening": "🎤  Слухаю...",
            "thinking":  "⚙  Думаю...",
            "speaking":  "🔊  Відповідаю...",
        }
        self._status.set(labels.get(state, "..."))
        if text:
            short = text[:100] + ("…" if len(text) > 100 else "")
            self._subtext.set(short)
            # оновлюємо рядок історії
            trunc = text[:50] + ("…" if len(text) > 50 else "")
            if state == "thinking":
                self._you_var.set(f"▶  {trunc}")
            elif state == "speaking":
                self._lin_var.set(f"◆  {trunc}")
        elif state == "idle":
            self._subtext.set("")

    def safe_set_state(self, state: str, text: str = ""):
        """Thread-safe оновлення стану (з будь-якого потоку)."""
        try:
            self.root.after(0, lambda: self.set_state(state, text))
        except Exception:
            pass

    def set_mode(self, mode: str):
        labels = {"chat": "💬 chat", "dictation": "🎤 dictation", "normal": "● normal"}
        label = labels.get(mode, "● normal")
        try:
            self.root.after(0, lambda: self._mode_lbl.configure(text=label))
        except Exception:
            pass

    def show(self):
        try:
            self.root.after(0, lambda: (self.root.deiconify(), self.root.lift(),
                                        self.root.attributes("-topmost", True)))
        except Exception:
            pass

    def stop_speaking(self):
        """Зупиняє TTS достроково."""
        tts._tts_stop.set()
        log.info("TTS зупинено кнопкою")

    # ── Animation ─────────────────────────────────────────────
    def _animate(self):
        # Якщо активний преміум-орб (окреме вікно) — tkinter-аватар не малюємо
        if self._premium_active:
            self.root.after(200, self._animate)
            return
        # Вікно сховане (✕ або трей): малювати нікому, не палимо CPU
        try:
            hidden = self.root.state() == "withdrawn"
        except Exception:
            hidden = False
        if hidden:
            self._idle_drawn = False
            self.root.after(300, self._animate)
            return
        self._cv = self._orb if self._compact else self.canvas   # активне полотно
        cx = cy = 48 if self._compact else 42                    # центр під розмір полотна

        # Повне вікно + IDLE — статичний кадр (економія CPU). Орб завжди живий.
        if self.state == "idle" and not self._compact:
            if not self._idle_drawn:
                self._cv.delete("all")
                self._draw_idle(cx, cy)
                self._idle_drawn = True
            self.root.after(200, self._animate)
            return

        self._idle_drawn = False
        self._cv.delete("all")
        t = self._frame * 0.05
        self._frame += 1

        if self._compact:
            self._draw_orb_base(cx, cy, t)        # тіло орба + обертове «магічне кільце»

        if self.state == "idle":
            self._draw_idle(cx, cy)
        elif self.state == "listening":
            self._draw_listening(cx, cy, t)
        elif self.state == "thinking":
            self._draw_thinking(cx, cy, t)
        elif self.state == "speaking":
            self._draw_speaking(cx, cy, t)

        self.root.after(90 if (self._compact and self.state == "idle") else 50, self._animate)

    # ── Геометричні примітиви німба «Рафаеля» ────────────────
    def _star(self, cx, cy, R, r, fill, outline=""):
        """Чотирикутна зірка — божественна іскра в ядрі."""
        pts = []
        for k in range(8):
            rad = R if k % 2 == 0 else r
            a = math.pi / 2 + k * math.pi / 4
            pts += [cx + rad * math.cos(a), cy - rad * math.sin(a)]
        self._cv.create_polygon(pts, fill=fill, outline=outline, width=1)

    def _diamond(self, cx, cy, s, fill):
        self._cv.create_polygon(cx, cy - s, cx + s, cy, cx, cy + s, cx - s, cy,
                                   fill=fill, outline="")

    def _draw_orb_base(self, cx, cy, t):
        """Тіло компактного орба: темний диск + обертове «магічне кільце» + дуги-крила."""
        accent = self.STATE_COLORS.get(self.state, self.STATE_COLORS["idle"])[1]
        # Кругле тіло орба (на прозорому тлі лишається тільки воно)
        self._cv.create_oval(cx-45, cy-45, cx+45, cy+45, fill="#12101f", outline="#4a3c1d", width=1)
        self._cv.create_oval(cx-38, cy-38, cx+38, cy+38, outline="#241d10", width=1)
        # Обертове кільце з крапок (магічне коло)
        n = 18
        for i in range(n):
            a = (i / n) * 2 * math.pi + t * 0.5
            dx = cx + 42 * math.cos(a)
            dy = cy + 42 * math.sin(a)
            sz = 1.7 if i % 2 == 0 else 0.9
            self._cv.create_oval(dx-sz, dy-sz, dx+sz, dy+sz, fill=accent, outline="")
        # Дві дуги-«крила» обабіч — натяк на янгола
        self._cv.create_arc(cx-44, cy-44, cx+44, cy+44, start=55, extent=70,
                            style="arc", outline=accent, width=1)
        self._cv.create_arc(cx-44, cy-44, cx+44, cy+44, start=235, extent=70,
                            style="arc", outline=accent, width=1)

    def _draw_idle(self, cx, cy):
        # Подвійний золотий німб + ядро мудрості зі зіркою
        self._cv.create_oval(cx-32, cy-32, cx+32, cy+32, outline="#8a6f2e", width=1)
        self._cv.create_oval(cx-27, cy-27, cx+27, cy+27, outline="#caa84e", width=2)
        self._cv.create_oval(cx-13, cy-13, cx+13, cy+13,
                                 fill="#141228", outline="#e9cf86", width=2)
        self._star(cx, cy, 11, 4, "#fff0c0", "#caa84e")

    def _draw_listening(self, cx, cy, t):
        # Німб приймає сигнал — кільця пульсують (золото + блакить)
        pulse = abs(math.sin(t * 3)) * 12
        for i, (extra, col) in enumerate([(pulse, "#22407a"), (pulse*0.6, "#3a6abf"), (0, "#7fb0ff")]):
            r = 28 + extra
            self._cv.create_oval(cx-r, cy-r, cx+r, cy+r, outline=col,
                                     width=1 if i < 2 else 2)
        self._cv.create_oval(cx-13, cy-13, cx+13, cy+13,
                                 fill="#10204a", outline="#dcebff", width=2)
        self._star(cx, cy, 9, 3, "#dcebff")

    def _draw_thinking(self, cx, cy, t):
        # «Великий Мудрець аналізує» — геометричні вузли обертаються навколо ядра
        self._cv.create_oval(cx-30, cy-30, cx+30, cy+30, outline="#8a6f2e", width=1)
        shades = ["#6a5320", "#8a6f2e", "#b8902f", "#caa84e", "#e9cf86", "#fff0c0"]
        n = 6
        for i in range(n):
            a = (i / n) * 2 * math.pi + t * 3
            dx = cx + 22 * math.cos(a)
            dy = cy + 22 * math.sin(a)
            self._diamond(dx, dy, 4, shades[i % len(shades)])
        self._cv.create_oval(cx-11, cy-11, cx+11, cy+11,
                                 fill="#1a1530", outline="#e9cf86", width=2)
        self._star(cx, cy, 8, 3, "#fff0c0")

    def _draw_speaking(self, cx, cy, t):
        # Промениста пульсація — кільця розходяться від ядра
        cols = ["#fff6e0", "#ffe9a8", "#e9cf86", "#b8902f"]
        for i in range(3):
            phase = (t * 1.4 + i / 3.0) % 1.0
            r = 12 + phase * 22
            self._cv.create_oval(cx-r, cy-r, cx+r, cy+r,
                                     outline=cols[min(len(cols)-1, int(phase * len(cols)))],
                                     width=2)
        self._cv.create_oval(cx-12, cy-12, cx+12, cy+12,
                                 fill="#1c1633", outline="#ffe9a8", width=2)
        self._star(cx, cy, 10, 4, "#fff6e0", "#ffe9a8")
