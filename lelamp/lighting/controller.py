"""Small status-light controller used by the application."""
from __future__ import annotations

import math
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv

Color = tuple[int, int, int]
CONFIG_PATH = Path(__file__).resolve().parents[2] / "lighting.conf"


class LightingController:
    COLORS: dict[str, Color] = {
        "cyan": (40, 190, 210), "purple": (150, 55, 210),
        "warm_yellow": (255, 175, 55), "green": (90, 220, 110),
        "warm_orange": (255, 95, 25), "red": (220, 25, 25),
    }

    def __init__(self, rgb=None, led_brightness: int = 255):
        self._rgb = rgb
        self._unavailable = False
        self._unavailable_reported = False
        self._brightness = max(0, min(255, int(led_brightness)))
        self._current_color: Color = (255, 255, 255)
        self._effect_thread: Optional[threading.Thread] = None
        self._stop_effect = threading.Event()
        self._lock = threading.Lock()

    def _driver(self):
        if self._unavailable:
            return None
        if self._rgb is None:
            try:
                from .rgb import RGBService
                self._rgb = RGBService(led_brightness=self._brightness)
            except Exception as exc:
                self._unavailable = True
                if not self._unavailable_reported:
                    print(f"灯光不可用，语音功能继续运行: {exc}", flush=True)
                    self._unavailable_reported = True
                return None
        return self._rgb

    def _show(self, color: Color, brightness: float = 1.0) -> None:
        scaled = tuple(max(0, min(255, round(channel * brightness))) for channel in color)
        driver = self._driver()
        if driver is not None:
            driver._handle_solid(scaled)

    def _stop(self) -> None:
        self._stop_effect.set()
        thread = self._effect_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=0.8)
        self._effect_thread = None
        self._stop_effect.clear()

    def _start(self, effect: Callable[[threading.Event], None]) -> None:
        with self._lock:
            self._stop()
            self._effect_thread = threading.Thread(target=effect, args=(self._stop_effect,), daemon=True)
            self._effect_thread.start()

    @staticmethod
    def _breath(stop: threading.Event, show: Callable[[float], None], minimum: float,
                maximum: float, period: float) -> None:
        started = time.monotonic()
        while not stop.is_set():
            phase = ((time.monotonic() - started) % period) / period
            level = minimum + (maximum - minimum) * (0.5 - 0.5 * math.cos(phase * 2 * math.pi))
            show(level)
            stop.wait(0.05)

    def set_light(self, color: Color = (255, 255, 255), brightness: int = 255) -> None:
        if len(color) != 3 or any(not 0 <= int(value) <= 255 for value in color):
            raise ValueError("颜色必须是三个 0～255 的整数")
        if not 0 <= brightness <= 255:
            raise ValueError("亮度必须在 0～255")
        with self._lock:
            self._stop()
            self._brightness = brightness
            self._current_color = tuple(int(value) for value in color)
            self._show(self._current_color, brightness / 255)

    def fade_to(self, color: Color, brightness_percent: float, seconds: float = 0.8) -> None:
        """Fade a stable light level in; used by the persistent work mode."""
        if not 0 <= brightness_percent <= 100:
            raise ValueError("亮度必须在 0～100")
        if len(color) != 3 or any(not 0 <= int(value) <= 255 for value in color):
            raise ValueError("颜色必须是三个 0～255 的整数")
        with self._lock:
            self._stop()
            steps = max(1, round(max(0.0, seconds) * 30))
            for index in range(1, steps + 1):
                self._show(color, (brightness_percent / 100) * index / steps)
                if seconds > 0:
                    time.sleep(seconds / steps)
            self._brightness = round(255 * brightness_percent / 100)
            self._current_color = tuple(int(value) for value in color)

    def fade_off(self, seconds: float = 0.8) -> None:
        """Fade the current RGB output down to black."""
        with self._lock:
            self._stop()
            start = self._brightness / 255
            steps = max(1, round(max(0.0, seconds) * 30))
            for index in range(steps - 1, -1, -1):
                self._show(self._current_color, start * index / steps)
                if seconds > 0:
                    time.sleep(seconds / steps)
            self._brightness = 0

    def office_mode(self, brightness_percent: float | None = None) -> None:
        """Apply the configured desk-work light without changing RGB ratios."""
        load_dotenv(CONFIG_PATH, override=True)
        color = tuple(
            int(__import__("os").getenv(f"OFFICE_LIGHT_{channel}", str(default)))
            for channel, default in zip(("R", "G", "B"), (255, 220, 180))
        )
        percent = (float(os.getenv("OFFICE_LIGHT_BRIGHTNESS_PERCENT", "75"))
                   if brightness_percent is None else brightness_percent)
        if not 0 <= percent <= 100:
            raise ValueError("OFFICE_LIGHT_BRIGHTNESS_PERCENT 必须在 0～100 之间")
        self.set_light(color, round(255 * percent / 100))

    def work_light(self, tone: str, brightness_percent: float, seconds: float = 0.8) -> None:
        """Fade in the selected stable work-light tone."""
        load_dotenv(CONFIG_PATH, override=True)
        if tone == "white":
            color = tuple(int(os.getenv(f"OFFICE_LIGHT_{channel}", str(default)))
                          for channel, default in zip(("R", "G", "B"), (255, 220, 180)))
        elif tone == "warm":
            color = tuple(int(os.getenv(f"WARM_LIGHT_{channel}", str(default)))
                          for channel, default in zip(("R", "G", "B"), (255, 140, 40)))
        else:
            raise ValueError("办公色调必须是 white 或 warm")
        self.fade_to(color, brightness_percent, seconds)

    def warm_mode(self, brightness_percent: float | None = None) -> None:
        """Apply the configured warm-yellow light without changing RGB ratios."""
        load_dotenv(CONFIG_PATH, override=True)
        import os
        color = tuple(
            int(os.getenv(f"WARM_LIGHT_{channel}", str(default)))
            for channel, default in zip(("R", "G", "B"), (255, 160, 60))
        )
        percent = (float(os.getenv("WARM_LIGHT_BRIGHTNESS_PERCENT", "100"))
                   if brightness_percent is None else brightness_percent)
        if not 0 <= percent <= 100:
            raise ValueError("WARM_LIGHT_BRIGHTNESS_PERCENT 必须在 0～100 之间")
        self.set_light(color, round(255 * percent / 100))

    def wake_required(self) -> None:
        color = self.COLORS["warm_orange"]
        self._start(lambda stop: self._breath(stop, lambda level: self._show(color, level), 0.02, 0.10, 4.0))

    def wake_ack(self) -> None:
        color = self.COLORS["cyan"]
        def flash(stop):
            for _ in range(2):
                if stop.is_set(): return
                self._show(color, 0.75)
                if stop.wait(0.10): return
                self._show(color, 0.03)
                if stop.wait(0.10): return
            self._show(color, 0.22)
        self._start(flash)

    def listening(self) -> None:
        color = self.COLORS["cyan"]
        self._start(lambda stop: self._breath(stop, lambda level: self._show(color, level), 0.12, 0.28, 4.0))

    def thinking(self) -> None:
        color = self.COLORS["purple"]
        self._start(lambda stop: self._breath(stop, lambda level: self._show(color, level), 0.08, 0.32, 3.2))

    def speaking(self) -> None:
        color = self.COLORS["warm_yellow"]
        self._start(lambda stop: self._breath(stop, lambda level: self._show(color, level), 0.18, 0.38, 2.5))

    def turn_done(self) -> None:
        color = self.COLORS["green"]
        def flash(stop):
            self._show(color, 0.45)
            stop.wait(0.4)
        self._start(flash)

    def session_end(self) -> None:
        color = self.COLORS["warm_orange"]
        def fade(stop):
            for step in range(20, -1, -1):
                if stop.is_set(): return
                self._show(color, 0.04 * step)
                if stop.wait(0.05): return
            # The fade is the transition; waiting for the next voice-loop
            # iteration must not leave the lamp dark.
            self._breath(stop, lambda level: self._show(color, level), 0.02, 0.10, 4.0)
        self._start(fade)

    def error(self) -> None:
        color = self.COLORS["red"]
        self._start(lambda stop: self._breath(stop, lambda level: self._show(color, level), 0.25, 0.55, 1.4))

    def sleep(self) -> None:
        self.wake_required()

    def close(self) -> None:
        with self._lock:
            self._stop()
            if self._rgb is not None:
                self._rgb.clear()
