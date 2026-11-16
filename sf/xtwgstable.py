import os
import re
import sys
import math
import time
import queue
import shutil
import zipfile
import tempfile
import threading
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor

import tkinter as tk
from tkinter import messagebox, font as tkfont
from PIL import Image, ImageTk, ImageOps, ImageFilter, ImageChops, ImageDraw

# =========================
# XTWG CONFIG
# =========================

APP_NAME = "XTWG Game Launcher"
CATALOG_URL = "https://pulldata.duckdns.org/games/games.txt"

ROOT = Path.home() / "XTWG"
GAMES = ROOT / "Games"
CACHE = ROOT / "Cache"

for folder in [ROOT, GAMES, CACHE]:
    folder.mkdir(parents=True, exist_ok=True)

# Colors / fonts (PlayStation-like palette)
BG = "#0b0e15"
BG_RGB = (11, 14, 21)
FONT = "Segoe UI"
WHITE = "#ffffff"
TEXT_DIM = "#9fb0c8"
TEXT_SOFT = "#c3cfdf"
ACCENT = "#0070d1"       # PlayStation blue
GREEN = "#2ecc71"
ORANGE = "#ffb347"

# Make text crisp on Windows high-DPI screens
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


# =========================
# HELPERS
# =========================

def safe_name(name):
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip()


def fetch(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "XTWG-Launcher/3.0"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def parse_games(data):
    games = []

    for block in re.split(r"(?m)^\s*\*\s*$", data):
        item = {}

        for line in block.strip().splitlines():
            line = line.strip()
            if ":" in line:
                key, value = line.split(":", 1)
                item[key.strip().lower()] = value.strip()

        if item.get("game"):
            games.append({
                "name": item.get("game", ""),
                "download": item.get("download url", ""),
                "cover": item.get("cover photo url", ""),
                "logo": item.get("logo url", ""),
                "description": item.get("explanation", ""),
                "version": item.get("version", ""),
                "age": item.get("game age rating", ""),
            })

    return games


def game_folder(game):
    return GAMES / safe_name(game["name"])


def version_file(game):
    return game_folder(game) / "Details" / "data" / "version.txt"


def installed_version(game):
    path = version_file(game)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _rm_error(func, path, _exc):
    """rmtree helper: clear read-only flag (Windows) and retry."""
    os.chmod(path, 0o700)
    func(path)


def version_key(version):
    parts = re.findall(r"\d+|[a-zA-Z]+", version)
    return tuple(
        (0, int(x)) if x.isdigit() else (1, x.lower())
        for x in parts
    )


# =========================
# MAIN APP
# =========================

class XTWG(tk.Tk):

    def __init__(self):
        super().__init__()

        self.title(APP_NAME)
        self.geometry("1280x760")
        self.minsize(1000, 640)
        self.configure(bg=BG)

        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        # state
        self.all_games = []            # full catalog (Store)
        self.games = []                # games shown in the current tab
        self.tab = "library"           # "library" or "store"
        self.first_load = True
        self.sel = 0
        self.pos = 0.0                 # animated selection position
        self.local = {}                # game name -> installed version
        self.imgs = {}                 # url -> PIL image
        self.logo_thumbs = {}
        self.tile_cache = {}
        self.bg_cache = {}
        self.bg_prev = None
        self.bg_cur = None
        self.bg_t = 1.0
        self.bg_photo = None
        self.logo_photo = None
        self.tile_rects = []
        self.hits_top = []
        self.hits_info = []
        self.hover = None
        self.status = "Ready"
        self.progress = 0.0
        self.busy = False
        self.busy_name = ""
        self.animating = False
        self._resize_job = None

        self.q = queue.Queue()
        self.pool = ThreadPoolExecutor(4)

        # input
        self.bind("<Left>", lambda e: self.select(self.sel - 1))
        self.bind("<Right>", lambda e: self.select(self.sel + 1))
        self.bind("<Return>", lambda e: self.do("primary"))
        self.bind("<KeyPress-u>", lambda e: self.do("updates"))
        self.bind("<F5>", lambda e: self.load_catalog())
        self.bind("<Tab>", lambda e: self.set_tab(
            "store" if self.tab == "library" else "library"))
        self.bind("<Delete>", lambda e: self.do("delete"))
        self.bind("<F11>", self.toggle_fullscreen)
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))
        self.canvas.bind("<Configure>", self.on_resize)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Button-4>", lambda e: self.select(self.sel - 1))
        self.canvas.bind("<Button-5>", lambda e: self.select(self.sel + 1))

        self.after(30, self._poll)
        self.after(50, self.redraw_all)
        self.after(100, self.load_catalog)
        self.after(20000, self._clock)

    # =====================
    # INFRA
    # =====================

    def post(self, fn, *args):
        """Thread-safe: run fn(*args) on the UI thread."""
        self.q.put((fn, args))

    def _poll(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                try:
                    fn(*args)
                except Exception as e:
                    print("UI error:", e, file=sys.stderr)
        except queue.Empty:
            pass
        self.after(30, self._poll)

    def _clock(self):
        self.draw_top()
        self.after(20000, self._clock)

    def toggle_fullscreen(self, _=None):
        self.attributes("-fullscreen", not self.attributes("-fullscreen"))

    def size(self):
        return (
            max(self.canvas.winfo_width(), 800),
            max(self.canvas.winfo_height(), 600),
        )

    def layout(self):
        W, H = self.size()
        S = min(230, int(H * 0.30))       # selected tile size
        N = int(S * 0.66)                 # normal tile size
        cy = 100 + S // 2                 # tile row center
        info_y = 100 + S + 50
        return S, N, cy, info_y

    def set_status(self, text):
        self.status = text
        self.draw_info()

    def set_progress(self, value):
        self.progress = value
        self.draw_info()

    # =====================
    # DRAWING PRIMITIVES
    # =====================

    def rrect(self, x1, y1, x2, y2, r, **kw):
        pts = [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def pill(self, x, y, w, h, text, action, kind="secondary", tags="info"):
        c = self.canvas
        hov = self.hover == action

        if kind == "primary":
            fill = "#dfe8ff" if hov else WHITE
            fg = BG
        elif kind == "disabled":
            fill, fg = "#3a4152", "#98a4b8"
        elif kind == "danger":
            fill = "#7a343d" if hov else "#4f262d"
            fg = "#ffb3bc"
        else:
            fill = "#3a4358" if hov else "#2b3242"
            fg = WHITE

        r = h / 2
        c.create_oval(x, y, x + h, y + h, fill=fill, outline="", tags=tags)
        c.create_oval(x + w - h, y, x + w, y + h, fill=fill, outline="", tags=tags)
        c.create_rectangle(x + r, y, x + w - r, y + h, fill=fill, outline="", tags=tags)
        c.create_text(
            x + w / 2, y + h / 2, text=text, fill=fg,
            font=(FONT, 13, "bold"), tags=tags
        )
        return (x, y, x + w, y + h, action)

    # =====================
    # BACKGROUND
    # =====================

    def cover_of(self, game):
        return self.imgs.get(game["cover"]) if game and game["cover"] else None

    def compose_bg(self, cover, w, h):
        base = Image.new("RGB", (w, h), BG_RGB)

        if cover is not None:
            small = ImageOps.fit(
                cover.convert("RGB"),
                (max(1, w // 3), max(1, h // 3)),
                Image.LANCZOS,
            ).filter(ImageFilter.GaussianBlur(3))
            art = small.resize((w, h), Image.BILINEAR)
        else:
            art = Image.new("RGB", (w, h), (22, 27, 39))

        grad = Image.linear_gradient("L")                    # top 0 -> bottom 255
        grad_v = grad.resize((w, h))
        grad_h = ImageOps.invert(grad.rotate(90)).resize((w, h))  # left 255 -> right 0

        mask_h = grad_h.point(lambda v: int(v * 0.93))
        mask_v = grad_v.point(lambda v: min(255, int(v * 1.25)))
        mask = ImageChops.lighter(mask_h, mask_v)
        mask = ImageChops.lighter(mask, Image.new("L", (w, h), 70))

        return Image.composite(Image.new("RGB", (w, h), BG_RGB), art, mask)

    def get_bg(self):
        W, H = self.size()
        game = self.games[self.sel] if self.games else None
        cover = self.cover_of(game)
        key = (game["name"] if game else "", W, H, cover is not None)

        if key not in self.bg_cache:
            if len(self.bg_cache) > 10:
                self.bg_cache.clear()
            self.bg_cache[key] = self.compose_bg(cover, W, H)

        return self.bg_cache[key]

    def set_background(self, instant=False):
        new = self.get_bg()
        self.bg_prev = self.bg_cur if self.bg_cur is not None else new
        self.bg_cur = new
        self.bg_t = 1.0 if instant else 0.0
        self.draw_bg()
        self.start_anim()

    def draw_bg(self):
        if self.bg_cur is None:
            return

        if (
            self.bg_t < 1.0
            and self.bg_prev is not None
            and self.bg_prev.size == self.bg_cur.size
        ):
            img = Image.blend(self.bg_prev, self.bg_cur, self.bg_t)
        else:
            img = self.bg_cur

        self.bg_photo = ImageTk.PhotoImage(img)
        self.canvas.delete("bg")
        self.canvas.create_image(0, 0, image=self.bg_photo, anchor="nw", tags="bg")
        self.canvas.tag_lower("bg")

    # =====================
    # TOP BAR
    # =====================

    def draw_top(self):
        c = self.canvas
        c.delete("top")
        self.hits_top = []
        W, H = self.size()

        c.create_text(80, 42, text="XTWG", anchor="w", fill=WHITE,
                      font=(FONT, 22, "bold"), tags="top")
        c.create_text(165, 46, text="GAME LAUNCHER", anchor="w", fill="#6aa5ff",
                      font=(FONT, 9, "bold"), tags="top")

        # nav tabs
        f = tkfont.Font(family=FONT, size=14, weight="bold")
        tx = 360
        for name, label in (("library", "Library"), ("store", "Store")):
            act = "tab_" + name
            w = f.measure(label)
            active = self.tab == name
            col = WHITE if (active or self.hover == act) else TEXT_DIM
            c.create_text(tx, 42, text=label, anchor="w", fill=col,
                          font=(FONT, 14, "bold"), tags="top")
            if active:
                c.create_line(tx, 62, tx + w, 62, fill=WHITE, width=3, tags="top")
            self.hits_top.append((tx - 12, 18, tx + w + 12, 68, act))
            tx += w + 44

        # refresh button
        hov = self.hover == "refresh"
        cx, cy = W - 80, 42
        c.create_oval(cx - 20, cy - 20, cx + 20, cy + 20,
                      fill="#3a4358" if hov else "#2b3242", outline="", tags="top")
        c.create_text(cx, cy - 1, text="↻", fill=WHITE,
                      font=(FONT, 16, "bold"), tags="top")
        self.hits_top.append((cx - 20, cy - 20, cx + 20, cy + 20, "refresh"))

        # clock
        c.create_text(W - 120, 42, text=time.strftime("%H:%M"), anchor="e",
                      fill=WHITE, font=(FONT, 15), tags="top")

        # hints
        c.create_text(
            80, H - 24,
            text="←  →  Navigate        Enter  Select        Tab  Library / Store"
                 "        Del  Delete        U  Updates        F5  Refresh"
                 "        F11  Fullscreen",
            anchor="w", fill="#566277", font=(FONT, 9), tags="top"
        )

    # =====================
    # TILE ROW
    # =====================

    def state(self, i):
        g = self.games[i]
        local = self.local.get(g["name"], "")
        if not local:
            return "none"
        if g["version"] and version_key(g["version"]) > version_key(local):
            return "update"
        return "installed"

    def tile_photo(self, i, size):
        size = max(48, int(round(size / 6.0)) * 6)
        g = self.games[i]
        key = (g["name"], size)

        if key in self.tile_cache:
            return self.tile_cache[key]

        if len(self.tile_cache) > 500:
            self.tile_cache.clear()

        cover = self.cover_of(g)
        has_cover = cover is not None

        if has_cover:
            img = ImageOps.fit(cover.convert("RGB"), (size, size), Image.BILINEAR)
        else:
            img = Image.new("RGB", (size, size), (35, 43, 59))

        scale = 3
        m = Image.new("L", (size * scale, size * scale), 0)
        ImageDraw.Draw(m).rounded_rectangle(
            (0, 0, size * scale - 1, size * scale - 1),
            radius=int(size * 0.09) * scale, fill=255
        )
        m = m.resize((size, size), Image.LANCZOS)

        img = img.convert("RGBA")
        img.putalpha(m)

        result = (ImageTk.PhotoImage(img), has_cover)
        self.tile_cache[key] = result
        return result

    def draw_tiles(self):
        c = self.canvas
        c.delete("tiles")
        self.tile_rects = []

        if not self.games:
            return

        W, H = self.size()
        S, N, cy, _ = self.layout()
        gap, x0 = 20, 80
        n = len(self.games)

        sizes = [N + (S - N) * max(0.0, 1 - abs(i - self.pos)) for i in range(n)]
        lefts, acc = [], 0.0
        for s in sizes:
            lefts.append(acc)
            acc += s + gap

        f = max(0, min(n - 1, int(math.floor(self.pos))))
        nx = min(n - 1, f + 1)
        frac = self.pos - f
        shift = lefts[f] + (lefts[nx] - lefts[f]) * frac

        focus = int(round(self.pos))

        for i in range(n):
            size = sizes[i]
            x = x0 + lefts[i] - shift
            if x + size < -20 or x > W + 20:
                continue

            y = cy - size / 2
            photo, has_cover = self.tile_photo(i, size)

            c.create_image(x, y, image=photo, anchor="nw", tags="tiles")

            if not has_cover:
                c.create_text(
                    x + size / 2, y + size / 2,
                    text=self.games[i]["name"][:1].upper(),
                    fill="#55627a", font=(FONT, int(size * 0.35), "bold"),
                    tags="tiles"
                )

            st = self.state(i)
            if st != "none":
                dot = GREEN if st == "installed" else ORANGE
                r = max(5, size * 0.04)
                bx, by = x + size - r * 2.6, y + size - r * 2.6
                c.create_oval(bx - r, by - r, bx + r, by + r,
                              fill=dot, outline=BG, width=2, tags="tiles")

            if i == focus:
                c.create_polygon(
                    self._ring_points(x - 6, y - 6, x + size + 6, y + size + 6, size * 0.11),
                    smooth=True, fill="", outline=WHITE, width=3, tags="tiles"
                )

            self.tile_rects.append((x, y, x + size, y + size, i))

    @staticmethod
    def _ring_points(x1, y1, x2, y2, r):
        return [
            x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
        ]

    # =====================
    # INFO PANEL
    # =====================

    def logo_of(self, game):
        url = game["logo"]
        img = self.imgs.get(url) if url else None
        if img is None:
            return None
        thumb = self.logo_thumbs.get(url)
        if thumb is None:
            thumb = img.copy()
            thumb.thumbnail((360, 90))
            self.logo_thumbs[url] = thumb
        return thumb

    def draw_info(self):
        c = self.canvas
        c.delete("info")
        self.hits_info = []
        W, H = self.size()
        S, N, cy, info_y = self.layout()
        x = 80

        if not self.games:
            ey = cy - 50
            if self.tab == "library" and self.all_games:
                c.create_text(x, ey, text="Your library is empty", anchor="nw",
                              fill=WHITE, font=(FONT, 34, "bold"), tags="info")
                c.create_text(x, ey + 62,
                              text="Games you download from the Store will appear here.",
                              anchor="nw", fill=TEXT_SOFT, font=(FONT, 13),
                              tags="info")
                self.hits_info.append(
                    self.pill(x, ey + 118, 200, 54, "Open Store", "tab_store", "primary"))
            else:
                c.create_text(x, ey, text=self.status, anchor="nw",
                              fill=TEXT_SOFT, font=(FONT, 16), tags="info")
            return

        g = self.games[self.sel]
        st = self.state(self.sel)
        local = self.local.get(g["name"], "")

        # title or logo
        logo = self.logo_of(g)
        if logo:
            self.logo_photo = ImageTk.PhotoImage(logo)
            c.create_image(x, info_y, image=self.logo_photo, anchor="nw", tags="info")
            meta_y = info_y + logo.height + 16
        else:
            c.create_text(x, info_y, text=g["name"], anchor="nw", fill=WHITE,
                          font=(FONT, 34, "bold"), tags="info")
            meta_y = info_y + 62

        # meta line
        parts = [f"Version {g['version'] or 'Unknown'}", f"Age {g['age'] or 'N/A'}"]
        if st == "none":
            parts.append("Not installed")
            meta_color = TEXT_DIM
        elif st == "installed":
            parts.append(f"Installed {local}")
            meta_color = TEXT_DIM
        else:
            parts.append(f"Update available  {local} → {g['version']}")
            meta_color = ORANGE

        c.create_text(x, meta_y, text="   •   ".join(parts), anchor="nw",
                      fill=meta_color, font=(FONT, 12), tags="info")

        # description
        desc = g["description"] or "No description available."
        if len(desc) > 240:
            desc = desc[:237].rstrip() + "…"
        c.create_text(x, meta_y + 34, text=desc, anchor="nw",
                      width=min(720, W - 160), fill=TEXT_SOFT,
                      font=(FONT, 13), tags="info")

        # buttons
        by = max(H - 170, meta_y + 118)

        if self.busy:
            self.pill(x, by, 200, 54, "Working…", "none", "disabled")
        else:
            if st == "none":
                btns = [("↓   Download", "primary", "primary", 200)]
            elif st == "installed":
                btns = [("▶   Play", "primary", "primary", 200),
                        ("Check updates", "updates", "secondary", 190)]
            else:
                btns = [("↓   Update", "primary", "primary", 200),
                        ("Play anyway", "play", "secondary", 160)]

            if st != "none":
                btns.append(("Delete", "delete", "danger", 130))

            bx = x
            for label, action, kind, w in btns:
                self.hits_info.append(self.pill(bx, by, w, 54, label, action, kind))
                bx += w + 14

        # progress + status
        show_bar = self.busy and self.busy_name == g["name"]
        if show_bar:
            bw = 400
            c.create_rectangle(x, by + 72, x + bw, by + 78,
                               fill="#2b3242", outline="", tags="info")
            c.create_rectangle(x, by + 72, x + bw * max(0.0, min(1.0, self.progress)),
                               by + 78, fill=ACCENT, outline="", tags="info")

        c.create_text(x, by + 88, text=self.status, anchor="nw", fill="#7f8fa6",
                      font=(FONT, 11), tags="info")

    def redraw_all(self):
        self.set_background(instant=True)
        self.draw_tiles()
        self.draw_info()
        self.draw_top()

    # =====================
    # ANIMATION
    # =====================

    def start_anim(self):
        if not self.animating:
            self.animating = True
            self._tick()

    def _tick(self):
        moving = False

        d = self.sel - self.pos
        if abs(d) > 0.002:
            self.pos += d * 0.22
            moving = True
        else:
            self.pos = float(self.sel)

        if self.bg_t < 1.0:
            self.bg_t = min(1.0, self.bg_t + 0.1)
            moving = True
            self.draw_bg()

        self.draw_tiles()

        if moving:
            self.after(16, self._tick)
        else:
            self.animating = False

    # =====================
    # INPUT
    # =====================

    def on_resize(self, _=None):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(90, self.redraw_all)

    def hit(self, x, y):
        for x1, y1, x2, y2, action in self.hits_top + self.hits_info:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return action
        return None

    def tile_at(self, x, y):
        for x1, y1, x2, y2, i in self.tile_rects:
            if x1 <= x <= x2 and y1 <= y <= y2:
                return i
        return None

    def on_motion(self, e):
        action = self.hit(e.x, e.y)
        tile = self.tile_at(e.x, e.y) if action is None else None

        self.canvas.configure(cursor="hand2" if (action or tile is not None) else "")

        if action != self.hover:
            self.hover = action
            self.draw_info()
            self.draw_top()

    def on_click(self, e):
        action = self.hit(e.x, e.y)
        if action:
            self.do(action)
            return

        i = self.tile_at(e.x, e.y)
        if i is not None:
            self.select(i)

    def on_wheel(self, e):
        self.select(self.sel - 1 if e.delta > 0 else self.sel + 1)

    def select(self, i):
        if not self.games:
            return

        i = max(0, min(len(self.games) - 1, i))
        if i == self.sel:
            return

        self.sel = i
        if not self.busy:
            self.progress = 0.0
            self.status = "Ready"

        self.set_background()
        self.draw_info()

    def do(self, action):
        if action == "refresh":
            self.load_catalog()
            return

        if action in ("tab_library", "tab_store"):
            self.set_tab(action[4:])
            return

        if not self.games:
            return

        if action == "primary":
            if self.busy:
                return
            if self.state(self.sel) == "installed":
                self.start_game()
            else:
                self.install_game()
        elif action == "play":
            self.start_game()
        elif action == "updates":
            self.check_updates()
        elif action == "delete":
            self.delete_game()

    # =====================
    # TABS / VIEW
    # =====================

    def rebuild_view(self, keep=None, fallback=0):
        """Recompute the games shown in the current tab."""
        if self.tab == "library":
            self.games = [g for g in self.all_games if self.local.get(g["name"])]
        else:
            self.games = list(self.all_games)

        found = False
        for i, g in enumerate(self.games):
            if g["name"] == keep:
                self.sel = i
                found = True
                break
        if not found:
            self.sel = max(0, min(fallback, len(self.games) - 1))

        self.pos = float(self.sel)
        self.redraw_all()

    def set_tab(self, tab):
        if tab == self.tab:
            return

        keep = self.games[self.sel]["name"] if self.games else None
        self.tab = tab

        if not self.busy:
            self.progress = 0.0

        self.status = "Ready"
        self.rebuild_view(keep)

        if tab == "library":
            self.status = f"{len(self.games)} installed"
        else:
            self.status = f"{len(self.games)} games in the store"
        self.draw_info()

    # =====================
    # CATALOG
    # =====================

    def load_catalog(self):
        self.set_status("Connecting to catalog...")

        def worker():
            try:
                data = fetch(CATALOG_URL).decode("utf-8-sig", errors="replace")
                games = parse_games(data)
                self.post(self.set_games, games)
            except Exception as e:
                self.post(self.set_status, f"Catalog error: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def refresh_local(self):
        self.local = {g["name"]: installed_version(g) for g in self.all_games}

    def set_games(self, games):
        prev = self.games[self.sel]["name"] if self.games else None

        self.all_games = games
        self.refresh_local()

        # first launch with an empty library -> open the Store directly
        if self.first_load:
            self.first_load = False
            if not any(self.local.values()):
                self.tab = "store"

        for g in games:
            for kind in ("cover", "logo"):
                url = g[kind]
                if url and url not in self.imgs:
                    self.pool.submit(self.fetch_image, url, kind)

        self.status = f"{len(games)} games loaded"
        self.rebuild_view(prev)

    def fetch_image(self, url, kind):
        try:
            img = Image.open(BytesIO(fetch(url))).convert("RGBA")
            img.thumbnail((1600, 1600) if kind == "cover" else (700, 250))
            self.post(self.on_image, url, img)
        except Exception:
            pass

    def on_image(self, url, img):
        self.imgs[url] = img
        self.tile_cache.clear()
        self.bg_cache.clear()
        self.logo_thumbs.pop(url, None)

        current = self.games[self.sel] if self.games else None
        if current and url == current["cover"]:
            self.set_background()

        self.draw_tiles()
        if current and url == current["logo"]:
            self.draw_info()

    # =====================
    # INSTALL
    # =====================

    def install_game(self):
        if self.busy or not self.games:
            return

        game = self.games[self.sel]
        if not game["download"]:
            self.set_status("This game has no download URL.")
            return

        self.busy = True
        self.busy_name = game["name"]
        self.progress = 0.0
        self.set_status("Preparing download...")

        def worker():
            temp = None
            try:
                url = game["download"]

                fd, temp_name = tempfile.mkstemp(dir=CACHE, suffix=".download")
                os.close(fd)
                temp = Path(temp_name)

                request = urllib.request.Request(
                    url, headers={"User-Agent": "XTWG/3.0"}
                )

                with urllib.request.urlopen(request, timeout=60) as response, \
                        open(temp, "wb") as file:

                    total = int(response.headers.get("Content-Length", 0))
                    done = 0
                    last = -1

                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break

                        file.write(chunk)
                        done += len(chunk)

                        if total:
                            pct = int(done * 100 / total)
                            if pct != last:
                                last = pct
                                self.post(self.set_progress, done / total)
                                self.post(self.status_only,
                                          f"Downloading... {pct}%")

                self.post(self.status_only, "Installing...")

                target = game_folder(game)
                target.mkdir(parents=True, exist_ok=True)

                if zipfile.is_zipfile(temp):
                    with zipfile.ZipFile(temp) as archive:
                        # Prevent ZIP path traversal.
                        root = target.resolve()
                        for member in archive.infolist():
                            destination = (root / member.filename).resolve()
                            if destination != root and root not in destination.parents:
                                raise ValueError("Unsafe ZIP path detected")
                        archive.extractall(target)
                else:
                    filename = Path(
                        urllib.parse.urlparse(url).path
                    ).name or game["name"] + ".download"
                    shutil.copy2(temp, target / safe_name(filename))

                vf = version_file(game)
                vf.parent.mkdir(parents=True, exist_ok=True)
                vf.write_text(game["version"], encoding="utf-8")

                self.post(self.install_done, "Installation completed!")

            except Exception as e:
                self.post(self.install_done, f"Installation failed: {e}")

            finally:
                if temp and temp.exists():
                    try:
                        temp.unlink()
                    except OSError:
                        pass

        threading.Thread(target=worker, daemon=True).start()

    def status_only(self, text):
        self.status = text
        self.draw_info()

    def install_done(self, message):
        keep = self.games[self.sel]["name"] if self.games else None
        self.busy = False
        self.busy_name = ""
        self.progress = 0.0
        self.refresh_local()
        self.status = message
        self.rebuild_view(keep, self.sel)

    # =====================
    # DELETE
    # =====================

    def delete_game(self):
        if self.busy or not self.games:
            return

        game = self.games[self.sel]

        if not self.local.get(game["name"]):
            return

        folder = game_folder(game)

        if not messagebox.askyesno(
            "Delete game",
            f"Delete {game['name']}?\n\n"
            f"This permanently removes the game folder:\n{folder}\n\n"
            "Any save files stored inside it will be lost too.",
            icon="warning",
        ):
            return

        # Safety: only ever delete inside the XTWG Games folder.
        try:
            target = folder.resolve()
            if GAMES.resolve() not in target.parents:
                raise ValueError("Folder is outside the Games directory.")
        except Exception as e:
            self.set_status(f"Delete failed: {e}")
            return

        idx = self.sel
        name = game["name"]

        self.busy = True
        self.busy_name = name
        self.progress = 0.0
        self.set_status("Deleting...")

        def worker():
            try:
                # Remove the version marker first so a partial delete
                # never leaves the game looking installed.
                vf = version_file(game)
                if vf.exists():
                    vf.unlink()

                if target.exists():
                    if sys.version_info >= (3, 12):
                        shutil.rmtree(target, onexc=_rm_error)
                    else:
                        shutil.rmtree(target, onerror=_rm_error)

                self.post(self.delete_done, f"{name} deleted.", name, idx)

            except Exception as e:
                self.post(self.delete_done, f"Delete failed: {e}", name, idx)

        threading.Thread(target=worker, daemon=True).start()

    def delete_done(self, message, name, idx):
        self.busy = False
        self.busy_name = ""
        self.progress = 0.0
        self.refresh_local()
        self.status = message
        self.rebuild_view(name, idx)

    # =====================
    # START GAME
    # =====================

    def start_game(self):
        if not self.games:
            return

        game = self.games[self.sel]
        folder = game_folder(game)

        if not folder.exists():
            return

        executables = sorted(folder.rglob("*.exe"))

        if not executables:
            messagebox.showwarning("XTWG", "No EXE file found in this game's folder.")
            return

        if len(executables) == 1:
            self.launch_exe(executables[0])
            return

        self.choose_exe(executables, folder)

    def choose_exe(self, executables, folder):
        dlg = tk.Toplevel(self)
        dlg.title("Select Game Executable")
        dlg.configure(bg="#10131c")
        dlg.geometry("540x440")
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="SELECT EXECUTABLE", bg="#10131c", fg=WHITE,
                 font=(FONT, 16, "bold")).pack(pady=18)

        for exe in executables[:12]:
            tk.Button(
                dlg,
                text=exe.relative_to(folder).as_posix(),
                anchor="w", relief="flat", bd=0,
                bg="#232b3b", fg=WHITE,
                activebackground=WHITE, activeforeground=BG,
                font=(FONT, 11), padx=14,
                command=lambda p=exe: (dlg.destroy(), self.launch_exe(p)),
            ).pack(fill="x", padx=18, pady=4, ipady=8)

    def launch_exe(self, exe):
        try:
            # The executable must remain inside the game folder.
            exe = exe.resolve()
            root = game_folder(self.games[self.sel]).resolve()

            if root not in exe.parents:
                raise ValueError("Executable is outside the game folder.")

            subprocess.Popen([str(exe)], cwd=str(exe.parent))
            self.set_status(f"Running: {exe.name}")

        except Exception as e:
            messagebox.showerror("Launch error", str(e))

    # =====================
    # UPDATES
    # =====================

    def check_updates(self):
        if not self.games or self.busy:
            return

        game = self.games[self.sel]
        local = installed_version(game)
        remote = game["version"]

        if not local:
            messagebox.showinfo("XTWG", "Game is not installed yet.")
            return

        if not remote:
            messagebox.showwarning("XTWG", "Server version is missing.")
            return

        if version_key(remote) > version_key(local):
            if messagebox.askyesno(
                "Update available",
                f"{game['name']}\n\n"
                f"Installed: {local}\n"
                f"Latest: {remote}\n\n"
                "Download update?"
            ):
                self.install_game()

        elif version_key(remote) == version_key(local):
            messagebox.showinfo("XTWG", "You have the latest version!")

        else:
            messagebox.showinfo(
                "XTWG", "Your installed version is newer than the catalog version."
            )


if __name__ == "__main__":
    app = XTWG()
    app.mainloop()