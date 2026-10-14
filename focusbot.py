"""FocusBot - a floating robot desktop companion that keeps you focused.

Features
  * Animated hover-drone robot (eyes follow your cursor, thruster particles, moods)
  * Pomodoro timer with progress ring
  * Distraction watcher: blacklist apps / sites (matched against the active
    window title and process name) and the bot tells you to get back to work
  * Four harshness levels: Chill, Firm, Drill Sergeant, Enforcer
"""
import ctypes
import datetime
import json
import math
import os
import random
import sys
import time

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QAction, QActionGroup, QBrush, QColor, QCursor, QFont,
                           QFontMetrics, QGuiApplication, QIcon, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFormLayout, QLabel, QMenu,
                               QMessageBox, QPlainTextEdit, QSpinBox,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

APP_NAME = "FocusBot"
W, H = 260, 320          # pet window size
BODY_CY = 190            # robot centre (y) inside the window

# --------------------------------------------------------------------------
# Config / stats
# --------------------------------------------------------------------------
CONFIG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

DEFAULT_BLACKLIST = [
    "youtube", "netflix", "reddit", "instagram", "tiktok", "facebook",
    "twitch", "discord", "steam.exe", "roblox", "9gag", "hulu", "disney+",
]

DEFAULT_CFG = {
    "harshness": 1,
    "work_min": 25,
    "break_min": 5,
    "long_break_min": 15,
    "watch": "always",          # "always" | "focus"
    "auto_continue": True,
    "sound": True,
    "blacklist": DEFAULT_BLACKLIST,
    "pos": None,
    "stats": {},
}


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Active window detection (Windows only; no-op elsewhere)
# --------------------------------------------------------------------------
IS_WIN = sys.platform == "win32"
if IS_WIN:
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def get_active_window():
    """Return (hwnd, title, exe_name, pid) for the foreground window, or None."""
    if not IS_WIN:
        return None
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if handle:
        size = wintypes.DWORD(1024)
        pbuf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, pbuf, ctypes.byref(size)):
            exe = os.path.basename(pbuf.value)
        kernel32.CloseHandle(handle)
    return hwnd, buf.value, exe, pid.value


def minimize_window(hwnd):
    if IS_WIN and hwnd:
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE


RUN_KEY = r"Software\\Microsoft\\Windows\\CurrentVersion\\Run"


def _startup_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else sys.executable}" "{os.path.abspath(__file__)}"'


def startup_enabled():
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(enabled):
    """Add/remove FocusBot from the current user's Windows startup programs."""
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


def match_blacklist(title, exe, blacklist):
    t, e = title.lower(), exe.lower()
    for kw in blacklist:
        k = kw.strip().lower()
        if k and (k in t or k in e):
            return kw.strip()
    return None


# --------------------------------------------------------------------------
# Harshness levels & dialogue
# --------------------------------------------------------------------------
HARSH = [
    dict(name="Chill", grace=20, renag=60, fly=False, minimize=False, lines=[
        "Hey... is {app} really what you meant to be doing?",
        "Psst. {app} can wait. Your goals can't.",
        "Little reminder: you were focusing. {app} will still be there later.",
    ]),
    dict(name="Firm", grace=8, renag=25, fly=False, minimize=False, lines=[
        "{app} detected. Close it and get back to work.",
        "Not now. {app} is off-limits while you focus.",
        "You said you'd focus. {app} isn't focus.",
    ]),
    dict(name="Drill Sergeant", grace=3, renag=8, fly=True, minimize=False, lines=[
        "{APP}?! DROP IT AND GET BACK TO WORK, RECRUIT!",
        "DID I SAY YOU COULD OPEN {APP}? MOVE IT!",
        "I DON'T SEE WORK, I SEE {APP}! FOCUS, NOW!",
        "EYES ON THE MISSION, SOLDIER! CLOSE {APP}!",
    ]),
    dict(name="Enforcer", grace=3, renag=6, fly=True, minimize=True, lines=[
        "{app}? Nope. Minimized. Back to work.",
        "Access denied: {app}. I put it away for you.",
        "{app} has been dealt with. Focus.",
    ]),
]
ESCALATE = "That's {n} times now. Seriously - close {app}."
WELCOME_BACK = ["Welcome back. Let's keep going.", "There you are. Back on track!", "Good. Focus mode restored."]
POKE = ["Beep boop!", "Hey, that tickles.", "I'm watching... in a supportive way.",
        "Double-click me to start focusing!", "Systems nominal.", "You've got this."]
WORK_DONE = ["Session complete! Take a breather.", "Nice work! Break time.", "Focus block crushed. Rest up!"]
BREAK_DONE = ["Break's over. Back to it!", "Recharged? Let's go.", "Break done - new session starting."]

MOOD_COLORS = {
    "idle": QColor(80, 205, 255),
    "focus": QColor(140, 120, 255),
    "break": QColor(70, 230, 150),
    "happy": QColor(255, 205, 70),
    "warn": QColor(255, 165, 50),
    "angry": QColor(255, 55, 70),
    "sleepy": QColor(120, 140, 170),
}


# --------------------------------------------------------------------------
# Robot rendering (shared by pet window and the app icon)
# --------------------------------------------------------------------------
def _alpha(c, a):
    c = QColor(c)
    c.setAlpha(a)
    return c


def paint_robot(p, cx, cy, s, mood, t, look=(0.0, 0.0), blink=1.0):
    accent = MOOD_COLORS.get(mood, MOOD_COLORS["idle"])
    p.save()
    p.translate(cx, cy)
    p.scale(s, s)
    p.setPen(Qt.NoPen)

    # aura
    g = QRadialGradient(0, 0, 100)
    g.setColorAt(0, _alpha(accent, 75))
    g.setColorAt(1, _alpha(accent, 0))
    p.setBrush(g)
    p.drawEllipse(QPointF(0, 0), 100, 100)

    # antenna
    p.setPen(QPen(QColor(165, 175, 198), 4, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(0, -50), QPointF(math.sin(t * 2) * 3, -72))
    tip = QPointF(math.sin(t * 2) * 3, -76)
    pulse = 0.65 + 0.35 * math.sin(t * 4)
    g = QRadialGradient(tip, 18 * pulse)
    g.setColorAt(0, _alpha(accent, 220))
    g.setColorAt(1, _alpha(accent, 0))
    p.setPen(Qt.NoPen)
    p.setBrush(g)
    p.drawEllipse(tip, 18 * pulse, 18 * pulse)
    p.setBrush(accent.lighter(130))
    p.drawEllipse(tip, 5.5, 5.5)

    # side ears / thrusters
    for side in (-1, 1):
        r = QRectF(side * 64 - 7, -18, 14, 34)
        eg = QLinearGradient(r.topLeft(), r.bottomLeft())
        eg.setColorAt(0, QColor(215, 222, 238))
        eg.setColorAt(1, QColor(140, 150, 175))
        p.setBrush(eg)
        p.setPen(QPen(QColor(110, 120, 145), 1.5))
        p.drawRoundedRect(r, 6, 6)
        p.setPen(Qt.NoPen)
        p.setBrush(_alpha(accent, 200))
        p.drawRoundedRect(QRectF(side * 64 - 2.5, -10, 5, 18), 2.5, 2.5)

    # bottom nozzle
    p.setBrush(QColor(70, 78, 100))
    p.drawRoundedRect(QRectF(-17, 44, 34, 13), 5, 5)

    # body shell
    body = QPainterPath()
    body.addRoundedRect(QRectF(-60, -52, 120, 104), 36, 36)
    bg = QLinearGradient(0, -52, 0, 52)
    bg.setColorAt(0, QColor(252, 253, 255))
    bg.setColorAt(0.55, QColor(222, 228, 242))
    bg.setColorAt(1, QColor(165, 176, 202))
    p.setBrush(bg)
    p.setPen(QPen(QColor(115, 125, 152), 2))
    p.drawPath(body)
    if mood == "angry":
        p.setPen(Qt.NoPen)
        p.setBrush(_alpha(accent, 55))
        p.drawPath(body)
    # shell highlight
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 120))
    p.drawEllipse(QRectF(-44, -47, 46, 16))

    # visor
    visor = QRectF(-46, -37, 92, 58)
    vg = QLinearGradient(visor.topLeft(), visor.bottomLeft())
    vg.setColorAt(0, QColor(28, 32, 54))
    vg.setColorAt(1, QColor(8, 10, 22))
    p.setBrush(vg)
    p.setPen(QPen(_alpha(accent, 140), 1.6))
    p.drawRoundedRect(visor, 21, 21)
    # visor reflection
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 28))
    refl = QPainterPath()
    refl.moveTo(-36, -30)
    refl.lineTo(-16, -30)
    refl.lineTo(-34, 10)
    refl.lineTo(-40, 10)
    refl.closeSubpath()
    p.drawPath(refl)

    # eyes
    lx, ly = look
    eye_c = accent.lighter(135)
    for side in (-1, 1):
        ex, ey = side * 19 + lx, -9 + ly
        p.save()
        p.translate(ex, ey)
        path = QPainterPath()
        stroke = False
        if mood in ("break", "happy"):
            path.moveTo(-9, 4)
            path.quadTo(0, -12, 9, 4)
            stroke = True
        elif mood == "sleepy":
            path.moveTo(-9, 2)
            path.quadTo(0, 6, 9, 2)
            stroke = True
        elif mood == "angry":
            inner_drop = 7
            if side == -1:
                path.moveTo(-10, -9)
                path.lineTo(10, -9 + inner_drop)
            else:
                path.moveTo(-10, -9 + inner_drop)
                path.lineTo(10, -9)
            path.lineTo(10, 10)
            path.lineTo(-10, 10)
            path.closeSubpath()
        else:
            h = (18 if mood == "focus" else 26) * blink
            wdt = 22 if mood == "warn" else 18
            path.addRoundedRect(QRectF(-wdt / 2, -h / 2, wdt, max(h, 2)), 8, 8)
        if stroke:
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(_alpha(accent, 90), 10, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
            p.setPen(QPen(eye_c, 4.5, Qt.SolidLine, Qt.RoundCap))
            p.drawPath(path)
        else:
            p.setPen(QPen(_alpha(accent, 80), 8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(eye_c)
            p.drawPath(path)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 190))
            p.drawEllipse(QPointF(-3, -5 * blink), 2.6, 2.6 * max(blink, 0.3))
        p.restore()

    # mouth
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(eye_c, 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    mx, my = lx * 0.6, 10 + ly * 0.5
    m = QPainterPath()
    if mood in ("break", "happy"):
        m.moveTo(mx - 8, my - 1)
        m.quadTo(mx, my + 10, mx + 8, my - 1)
        m.closeSubpath()
        p.setBrush(_alpha(accent, 160))
    elif mood == "angry":
        m.moveTo(mx - 9, my + 3)
        for i, dx in enumerate(range(-6, 10, 3)):
            m.lineTo(mx + dx, my + (0 if i % 2 == 0 else 4))
    elif mood == "warn":
        m.addEllipse(QPointF(mx, my + 2), 3.5, 4.5)
    elif mood in ("sleepy", "focus"):
        m.moveTo(mx - 6, my + 2)
        m.lineTo(mx + 6, my + 2)
    else:
        m.moveTo(mx - 7, my)
        m.quadTo(mx, my + 6, mx + 7, my)
    p.drawPath(m)

    # chest light
    p.setPen(Qt.NoPen)
    g = QRadialGradient(QPointF(0, 36), 10)
    g.setColorAt(0, _alpha(accent, 255))
    g.setColorAt(1, _alpha(accent, 0))
    p.setBrush(g)
    p.drawEllipse(QPointF(0, 36), 10, 10)
    p.setBrush(accent.lighter(150))
    p.drawEllipse(QPointF(0, 36), 3, 3)

    if mood == "angry":
        # anger mark
        p.setPen(QPen(accent, 3.5, Qt.SolidLine, Qt.RoundCap))
        ax, ay = 52, -56
        for a in range(4):
            ang = a * math.pi / 2 + math.pi / 4
            p.drawLine(QPointF(ax + math.cos(ang) * 3, ay + math.sin(ang) * 3),
                       QPointF(ax + math.cos(ang) * 9, ay + math.sin(ang) * 9))
    elif mood == "sleepy":
        p.setPen(QColor(200, 210, 230))
        f = QFont("Segoe UI", 11)
        f.setBold(True)
        p.setFont(f)
        z = (t * 0.8) % 1.0
        p.drawText(QPointF(48 + z * 10, -50 - z * 18), "z")
    p.restore()


def make_icon_pixmap(size=256):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    paint_robot(p, size / 2, size / 2 + size * 0.06, size / 170, "idle", 0.4)
    p.end()
    return pm


# --------------------------------------------------------------------------
# Settings dialog
# --------------------------------------------------------------------------
class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} Settings")
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.harsh = QComboBox()
        for i, h in enumerate(HARSH):
            self.harsh.addItem(f"{i + 1}. {h['name']}")
        self.harsh.setCurrentIndex(cfg["harshness"])
        form.addRow("Harshness", self.harsh)
        self.work = QSpinBox(); self.work.setRange(1, 180); self.work.setValue(cfg["work_min"])
        self.brk = QSpinBox(); self.brk.setRange(1, 60); self.brk.setValue(cfg["break_min"])
        self.lbrk = QSpinBox(); self.lbrk.setRange(1, 90); self.lbrk.setValue(cfg["long_break_min"])
        form.addRow("Focus minutes", self.work)
        form.addRow("Break minutes", self.brk)
        form.addRow("Long break (every 4)", self.lbrk)
        self.watch = QComboBox()
        self.watch.addItems(["Always watch for distractions", "Only during focus sessions"])
        self.watch.setCurrentIndex(0 if cfg["watch"] == "always" else 1)
        form.addRow("Watching", self.watch)
        self.auto = QCheckBox("Start next focus session after a break")
        self.auto.setChecked(cfg["auto_continue"])
        form.addRow("", self.auto)
        self.sound = QCheckBox("Beep on alerts")
        self.sound.setChecked(cfg["sound"])
        form.addRow("", self.sound)
        lay.addLayout(form)
        lay.addWidget(QLabel("Blacklist (one per line). Matched against window titles\n"
                             "and program names, e.g.  youtube   or   steam.exe"))
        self.bl = QPlainTextEdit("\n".join(cfg["blacklist"]))
        lay.addWidget(self.bl)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def apply(self, cfg):
        cfg["harshness"] = self.harsh.currentIndex()
        cfg["work_min"] = self.work.value()
        cfg["break_min"] = self.brk.value()
        cfg["long_break_min"] = self.lbrk.value()
        cfg["watch"] = "always" if self.watch.currentIndex() == 0 else "focus"
        cfg["auto_continue"] = self.auto.isChecked()
        cfg["sound"] = self.sound.isChecked()
        cfg["blacklist"] = [l.strip() for l in self.bl.toPlainText().splitlines() if l.strip()]


# --------------------------------------------------------------------------
# The pet window
# --------------------------------------------------------------------------
class FocusBot(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(W, H)
        self.setWindowTitle(APP_NAME)
        self.cfg = load_cfg()

        self.t = 0.0
        self.particles = []
        self.next_blink = time.time() + 3
        self.msg, self.msg_until = "", 0.0
        self.temp_mood, self.mood_until = None, 0.0
        self.shake_until = 0.0
        self.drag_off, self.press_pos, self.dragging = None, None, False

        # pomodoro
        self.phase = "idle"   # idle | work | break
        self.paused = False
        self.phase_end = 0.0
        self.phase_total = 1.0
        self.remaining = 0.0
        self.sessions_done = 0

        # distraction tracking
        self.distract_kw = None
        self.distract_since = 0.0
        self.last_nag = 0.0
        self.nag_count = 0
        self.last_other = None
        self.away_until = 0.0

        geo = QGuiApplication.primaryScreen().availableGeometry()
        pos = self.cfg.get("pos")
        if pos and any(s.availableGeometry().contains(QPoint(*pos)) for s in QGuiApplication.screens()):
            self.home = QPoint(*pos)
        else:
            self.home = QPoint(geo.right() - W - 20, geo.bottom() - H - 10)
        self.move(self.home)
        self.fly_target = None

        self.tray = QSystemTrayIcon(QIcon(make_icon_pixmap(64)), self)
        self.tray.setToolTip(APP_NAME)
        self.tray_menu = QMenu()
        self.tray_menu.aboutToShow.connect(lambda: self.build_menu(self.tray_menu))
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

        self.anim = QTimer(self, timeout=self.on_frame)
        self.anim.start(16)
        self.ticker = QTimer(self, timeout=self.on_tick)
        self.ticker.start(1000)
        self._last_frame = time.time()
        self.say("FocusBot online! Double-click me to start a focus session.", 6)

    # ---- helpers ---------------------------------------------------------
    @property
    def level(self):
        return HARSH[max(0, min(3, self.cfg["harshness"]))]

    def say(self, text, secs=5.0):
        self.msg, self.msg_until = text, time.time() + secs
        self.raise_()

    def set_mood(self, mood, secs):
        self.temp_mood, self.mood_until = mood, time.time() + secs

    def beep(self):
        if self.cfg["sound"]:
            QApplication.beep()

    def mood(self):
        now = time.time()
        if now < self.mood_until:
            return self.temp_mood
        if self.distract_kw and self.nag_count:
            return "angry" if self.cfg["harshness"] >= 2 else "warn"
        if self.phase == "work":
            return "sleepy" if self.paused else "focus"
        if self.phase == "break":
            return "sleepy" if self.paused else "break"
        return "idle"

    def stats_today(self):
        key = datetime.date.today().isoformat()
        return self.cfg["stats"].setdefault(key, {"sessions": 0, "focus_min": 0, "distractions": 0})

    # ---- pomodoro --------------------------------------------------------
    def start_phase(self, phase, minutes):
        self.phase, self.paused = phase, False
        self.phase_total = minutes * 60.0
        self.phase_end = time.time() + self.phase_total

    def start_focus(self):
        self.start_phase("work", self.cfg["work_min"])
        self.set_mood("happy", 1.5)
        self.say(f"Focus mode: {self.cfg['work_min']} minutes. Let's go!", 4)

    def stop_timer(self):
        self.phase, self.paused = "idle", False
        self.say("Timer stopped.", 3)

    def toggle_pause(self):
        if self.phase == "idle":
            return
        if self.paused:
            self.phase_end = time.time() + self.remaining
            self.paused = False
            self.say("Resumed.", 2)
        else:
            self.remaining = max(0.0, self.phase_end - time.time())
            self.paused = True
            self.say("Paused.", 2)

    def time_left(self):
        if self.phase == "idle":
            return 0.0
        return self.remaining if self.paused else max(0.0, self.phase_end - time.time())

    def phase_done(self):
        self.beep()
        if self.phase == "work":
            self.sessions_done += 1
            st = self.stats_today()
            st["sessions"] += 1
            st["focus_min"] += self.cfg["work_min"]
            save_cfg(self.cfg)
            long_b = self.sessions_done % 4 == 0
            mins = self.cfg["long_break_min"] if long_b else self.cfg["break_min"]
            self.start_phase("break", mins)
            self.set_mood("happy", 4)
            self.say(random.choice(WORK_DONE) + f" ({mins} min{' long' if long_b else ''} break)", 7)
            self.tray.showMessage(APP_NAME, "Focus session complete! Break time.")
        else:
            if self.cfg["auto_continue"]:
                self.start_phase("work", self.cfg["work_min"])
                self.say(random.choice(BREAK_DONE), 6)
            else:
                self.phase = "idle"
                self.say("Break's over! Double-click me when you're ready.", 8)
            self.tray.showMessage(APP_NAME, "Break's over - back to focus!")

    # ---- distraction watcher ---------------------------------------------
    def check_distraction(self):
        info = get_active_window()
        if not info:
            return
        hwnd, title, exe, pid = info
        if pid == os.getpid():
            return
        self.last_other = (title, exe)
        watching = self.cfg["watch"] == "always" or (self.phase == "work" and not self.paused)
        hit = match_blacklist(title, exe, self.cfg["blacklist"]) if watching else None
        now = time.time()
        if hit:
            if hit != self.distract_kw:
                self.distract_kw, self.distract_since = hit, now
                self.nag_count, self.last_nag = 0, 0.0
            lvl = self.level
            due = lvl["renag"] if self.nag_count else 0
            if now - self.distract_since >= lvl["grace"] and now - self.last_nag >= due:
                self.nag(hit, hwnd)
        elif self.distract_kw:
            if self.nag_count:
                self.set_mood("happy", 2.5)
                self.say(random.choice(WELCOME_BACK), 3)
            self.distract_kw, self.nag_count = None, 0

    def nag(self, app, hwnd):
        lvl = self.level
        self.nag_count += 1
        self.last_nag = time.time()
        if self.nag_count == 1:
            self.stats_today()["distractions"] += 1
            save_cfg(self.cfg)
        if self.nag_count >= 3 and not lvl["fly"]:
            line = ESCALATE
        else:
            line = random.choice(lvl["lines"])
        self.say(line.format(app=app, APP=app.upper(), n=self.nag_count), 6)
        self.shake_until = time.time() + (1.2 if lvl["fly"] else 0.5)
        self.beep()
        if lvl["fly"]:
            scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            c = scr.availableGeometry().center()
            self.fly_target = QPoint(c.x() - W // 2, c.y() - H // 2)
            self.away_until = time.time() + 5
        if lvl["minimize"]:
            minimize_window(hwnd)

    def blacklist_last(self):
        if not self.last_other:
            self.say("Switch to the app first, then use this from my menu.", 4)
            return
        title, exe = self.last_other
        kw = exe or title
        if kw and kw.lower() not in [b.lower() for b in self.cfg["blacklist"]]:
            self.cfg["blacklist"].append(kw)
            save_cfg(self.cfg)
            self.say(f"Added '{kw}' to the blacklist.", 4)

    # ---- timers ----------------------------------------------------------
    def on_tick(self):
        if self.phase != "idle" and not self.paused and time.time() >= self.phase_end:
            self.phase_done()
        self.check_distraction()
        tl = self.time_left()
        self.tray.setToolTip(f"{APP_NAME} - {self.phase}" + (f" {int(tl)//60:02d}:{int(tl)%60:02d}" if tl else ""))

    def on_frame(self):
        now = time.time()
        dt = min(0.05, now - self._last_frame)
        self._last_frame = now
        self.t += dt
        # thruster particles
        intensity = 3 if self.mood() == "angry" else 2
        for _ in range(intensity):
            self.particles.append([W / 2 + random.uniform(-8, 8), BODY_CY + 58,
                                   random.uniform(-0.5, 0.5), random.uniform(1.6, 3.2), 1.0])
        for pt in self.particles:
            pt[0] += pt[2]
            pt[1] += pt[3]
            pt[4] -= dt * 2.2
        self.particles = [pt for pt in self.particles if pt[4] > 0]
        # flying
        if self.fly_target is None and now > self.away_until and self.pos() != self.home and not self.dragging:
            if self.away_until:
                self.fly_target = QPoint(self.home)
                self.away_until = 0.0
        if self.fly_target is not None and not self.dragging:
            cur = self.pos()
            d = self.fly_target - cur
            if abs(d.x()) + abs(d.y()) <= 2:
                self.move(self.fly_target)
                self.fly_target = None
            else:
                self.move(cur + QPoint(round(d.x() * 0.12) or (1 if d.x() > 0 else -1 if d.x() else 0),
                                       round(d.y() * 0.12) or (1 if d.y() > 0 else -1 if d.y() else 0)))
        self.update()

    # ---- painting --------------------------------------------------------
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        now = time.time()
        mood = self.mood()
        accent = MOOD_COLORS[mood]
        sx = random.uniform(-4, 4) if now < self.shake_until else 0
        cx = W / 2 + sx
        cy = BODY_CY + math.sin(self.t * 2.2) * 6

        # particles / flame
        p.setPen(Qt.NoPen)
        for x, y, _, _, life in self.particles:
            p.setBrush(_alpha(accent.lighter(130), int(200 * life)))
            p.drawEllipse(QPointF(x + sx, y + (cy - BODY_CY)), 4.5 * life, 4.5 * life)
        flen = 24 + math.sin(self.t * 31) * 5
        fl = QPainterPath()
        fl.moveTo(cx - 12, cy + 56)
        fl.quadTo(cx, cy + 56 + flen * 1.6, cx + 12, cy + 56)
        fl.closeSubpath()
        fg = QLinearGradient(0, cy + 56, 0, cy + 56 + flen)
        fg.setColorAt(0, QColor(255, 255, 255, 230))
        fg.setColorAt(0.35, _alpha(accent, 200))
        fg.setColorAt(1, _alpha(accent, 0))
        p.setBrush(fg)
        p.drawPath(fl)

        # pomodoro ring
        if self.phase != "idle":
            frac = self.time_left() / self.phase_total if self.phase_total else 0
            r = QRectF(cx - 86, cy - 86, 172, 172)
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(_alpha(accent, 45), 5))
            p.drawEllipse(r)
            p.setPen(QPen(accent, 5, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(r, 90 * 16, int(frac * 360 * 16))

        # robot
        blink = 1.0
        if now > self.next_blink:
            k = (now - self.next_blink) / 0.14
            if k >= 1:
                self.next_blink = now + random.uniform(2.5, 6)
            else:
                blink = max(0.08, abs(1 - 2 * k))
        gc = self.mapToGlobal(QPoint(int(cx), int(cy)))
        cp = QCursor.pos()
        dx, dy = cp.x() - gc.x(), cp.y() - gc.y()
        dist = math.hypot(dx, dy) or 1
        k = min(1.0, dist / 300)
        look = (dx / dist * 6 * k, dy / dist * 5 * k)
        paint_robot(p, cx, cy, 1.0, mood, self.t, look, blink)

        # timer text
        if self.phase != "idle":
            tl = int(self.time_left())
            txt = f"{tl // 60:02d}:{tl % 60:02d}" + ("  II" if self.paused else "")
            f = QFont("Segoe UI", 14)
            f.setBold(True)
            path = QPainterPath()
            fm = QFontMetrics(f)
            path.addText(cx - fm.horizontalAdvance(txt) / 2, cy + 108, f, txt)
            p.setPen(QPen(QColor(10, 12, 25, 220), 4))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255))
            p.drawPath(path)

        # speech bubble
        if self.msg and now < self.msg_until:
            fade = min(1.0, (self.msg_until - now) / 0.4)
            f = QFont("Segoe UI", 10)
            f.setBold(mood == "angry")
            fm = QFontMetrics(f)
            br = fm.boundingRect(QRect(0, 0, 216, 400), Qt.TextWordWrap, self.msg)
            bh = min(br.height() + 18, 92)
            bw = max(br.width() + 24, 70)
            box = QRectF(W / 2 - bw / 2, 96 - bh - 10, bw, bh)
            bub = QPainterPath()
            bub.addRoundedRect(box, 14, 14)
            tail = QPainterPath()
            tail.moveTo(W / 2 - 8, box.bottom() - 1)
            tail.lineTo(W / 2 + 4, box.bottom() + 10)
            tail.lineTo(W / 2 + 10, box.bottom() - 1)
            bub = bub.united(tail)
            p.setOpacity(fade)
            p.setPen(QPen(accent, 2))
            p.setBrush(QColor(18, 20, 36, 235))
            p.drawPath(bub)
            p.setPen(QColor(240, 244, 255))
            p.setFont(f)
            p.drawText(box.adjusted(12, 9, -12, -9), Qt.TextWordWrap | Qt.AlignCenter, self.msg)
            p.setOpacity(1.0)
        p.end()

    # ---- input -----------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.press_pos = e.globalPosition().toPoint()
            self.drag_off = self.press_pos - self.pos()
            self.dragging = False

    def mouseMoveEvent(self, e):
        if self.drag_off is not None and e.buttons() & Qt.LeftButton:
            gp = e.globalPosition().toPoint()
            if (gp - self.press_pos).manhattanLength() > 4:
                self.dragging = True
                self.fly_target = None
            if self.dragging:
                self.move(gp - self.drag_off)

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.dragging:
            self.home = self.pos()
            self.away_until = 0.0
            self.cfg["pos"] = [self.home.x(), self.home.y()]
            save_cfg(self.cfg)
        else:
            self.set_mood("happy", 1.2)
            self.say(random.choice(POKE), 2.5)
        self.dragging = False
        self.drag_off = None

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self.phase == "idle":
                self.start_focus()
            else:
                self.toggle_pause()

    def contextMenuEvent(self, e):
        m = QMenu(self)
        self.build_menu(m)
        m.exec(e.globalPos())

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.setVisible(not self.isVisible())

    def build_menu(self, m):
        m.clear()
        if self.phase == "idle":
            m.addAction("Start focus session", self.start_focus)
        else:
            m.addAction("Resume" if self.paused else "Pause", self.toggle_pause)
            m.addAction("Skip to next phase", self.phase_done)
            m.addAction("Stop timer", self.stop_timer)
        m.addSeparator()
        hm = m.addMenu("Harshness")
        grp = QActionGroup(hm)
        for i, h in enumerate(HARSH):
            a = QAction(f"{i + 1}. {h['name']}", hm, checkable=True)
            a.setChecked(i == self.cfg["harshness"])
            a.triggered.connect(lambda _=False, i=i: self.set_harshness(i))
            grp.addAction(a)
            hm.addAction(a)
        if self.last_other:
            name = self.last_other[1] or self.last_other[0][:30]
            m.addAction(f"Blacklist '{name}'", self.blacklist_last)
        if IS_WIN:
            a = QAction("Start with Windows", m, checkable=True)
            a.setChecked(startup_enabled())
            a.triggered.connect(self.toggle_startup)
            m.addAction(a)
        m.addAction("Settings...", self.open_settings)
        m.addAction("Today's stats", self.show_stats)
        m.addSeparator()
        m.addAction("Hide" if self.isVisible() else "Show", lambda: self.setVisible(not self.isVisible()))
        m.addAction("Quit", QApplication.quit)

    def toggle_startup(self, enabled):
        try:
            set_startup(enabled)
            self.say("I'll launch when Windows starts." if enabled else "Removed from startup.", 3)
        except OSError as e:
            self.say(f"Couldn't change startup: {e}", 4)

    def set_harshness(self, i):
        self.cfg["harshness"] = i
        save_cfg(self.cfg)
        self.set_mood("angry" if i >= 2 else "happy", 1.5)
        self.say(f"Harshness set to {HARSH[i]['name']}.", 3)

    def open_settings(self):
        dlg = SettingsDialog(self.cfg)
        if dlg.exec():
            dlg.apply(self.cfg)
            save_cfg(self.cfg)
            self.say("Settings saved.", 2.5)

    def show_stats(self):
        st = self.stats_today()
        QMessageBox.information(None, f"{APP_NAME} - Today",
                                f"Focus sessions: {st['sessions']}\n"
                                f"Focus minutes: {st['focus_min']}\n"
                                f"Distractions caught: {st['distractions']}")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(QIcon(make_icon_pixmap(256)))
    bot = FocusBot()
    bot.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
