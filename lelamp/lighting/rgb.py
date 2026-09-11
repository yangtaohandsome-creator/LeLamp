from typing import Any, List, Union
import importlib
import os
import sys


def _load_ws281x():
    """Load the Pi5 RP1 build when configured, otherwise use the normal package."""
    driver_path = os.getenv("RPI_WS281X_DRIVER_PATH", "").strip()
    if not driver_path:
        # This is the location prepared by the Pi5 setup; on other machines
        # it simply does not exist and the regular package is used.
        driver_path = (
            "/home/lamppi/lelamp-drivers/rpi-ws281x-python/library/"
            "build/lib.linux-aarch64-cpython-312"
        )
    if driver_path and os.path.isdir(driver_path):
        # A previous failed import can leave the namespace-only PyPI package
        # cached, so remove it before preferring the Pi5 build.
        sys.path.insert(0, driver_path)
        sys.modules.pop("rpi_ws281x", None)
    module = importlib.import_module("rpi_ws281x")
    if not hasattr(module, "PixelStrip") or not hasattr(module, "Color"):
        raise ImportError(
            "rpi_ws281x 没有 PixelStrip；Pi5 请配置 RPI_WS281X_DRIVER_PATH"
        )
    return module.PixelStrip, module.Color


PixelStrip, Color = _load_ws281x()
from lelamp.service.base import ServiceBase


class RGBService(ServiceBase):
    def __init__(self, 
                 led_count: int = 64,
                 led_pin: int = 12,
                 led_freq_hz: int = 800000,
                 led_dma: int = 10,
                 led_brightness: int = 255,
                 led_invert: bool = False,
                 led_channel: int = 0):
        super().__init__("rgb")
        
        self.led_count = led_count
        self.strip = PixelStrip(
            led_count, led_pin, led_freq_hz, led_dma, 
            led_invert, led_brightness, led_channel
        )
        self.strip.begin()
        
    def handle_event(self, event_type: str, payload: Any):
        if event_type == "solid":
            self._handle_solid(payload)
        elif event_type == "paint":
            self._handle_paint(payload)
        else:
            self.logger.warning(f"Unknown event type: {event_type}")
    
    def _handle_solid(self, color_code: Union[int, tuple]):
        """Fill entire strip with single color"""
        if isinstance(color_code, tuple) and len(color_code) == 3:
            color = Color(color_code[0], color_code[1], color_code[2])
        elif isinstance(color_code, int):
            color = color_code
        else:
            self.logger.error(f"Invalid color format: {color_code}")
            return
            
        for i in range(self.led_count):
            self.strip.setPixelColor(i, color)
        self.strip.show()
        self.logger.debug(f"Applied solid color: {color_code}")
    
    def _handle_paint(self, colors: List[Union[int, tuple]]):
        """Set individual pixel colors from array"""
        if not isinstance(colors, list):
            self.logger.error(f"Paint payload must be a list, got: {type(colors)}")
            return
            
        max_pixels = min(len(colors), self.led_count)
        
        for i in range(max_pixels):
            color_code = colors[i]
            if isinstance(color_code, tuple) and len(color_code) == 3:
                color = Color(color_code[0], color_code[1], color_code[2])
            elif isinstance(color_code, int):
                color = color_code
            else:
                self.logger.warning(f"Invalid color at index {i}: {color_code}")
                continue
                
            self.strip.setPixelColor(i, color)
        
        self.strip.show()
        self.logger.debug(f"Applied paint pattern with {max_pixels} colors")
    
    def clear(self):
        """Turn off all LEDs"""
        for i in range(self.led_count):
            self.strip.setPixelColor(i, Color(0, 0, 0))
        self.strip.show()
    
    def stop(self, timeout: float = 5.0):
        """Override stop to clear LEDs before stopping"""
        self.clear()
        super().stop(timeout)
