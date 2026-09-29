"""One-shot public-IP location lookup used when the app starts."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class Location:
    ip: str
    country: str
    region: str
    city: str
    timezone: str
    updated_at: str


class LocationError(RuntimeError):
    pass


def _cache_path(root: Path) -> Path:
    return root / os.getenv("LOCATION_CACHE_FILE", "runtime_state/location.json")


def load_cached_location(root: Path | None = None) -> Location:
    """Load and activate the last successful location lookup."""
    runtime_root = root or Path(__file__).resolve().parents[1]
    try:
        payload = json.loads(_cache_path(runtime_root).read_text(encoding="utf-8"))
        location = Location(**payload)
    except Exception as exc:
        raise LocationError(f"没有可用的历史定位配置: {exc}") from exc
    if not location.city or not location.timezone:
        raise LocationError("历史定位配置缺少城市或时区")
    os.environ["LELAMP_LOCATION_CITY"] = location.city
    os.environ["LELAMP_TIMEZONE"] = location.timezone
    return location


def refresh_location(root: Path | None = None) -> Location:
    """Resolve this Pi's public IP location once and cache the result."""
    runtime_root = root or Path(__file__).resolve().parents[1]
    url = os.getenv("LOCATION_LOOKUP_URL", "https://ipwho.is/")
    timeout = float(os.getenv("LOCATION_TIMEOUT_SECONDS", "5"))
    request = Request(url, headers={"User-Agent": "LeLamp/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as exc:
        raise LocationError(f"公网 IP 定位请求失败: {exc}") from exc

    if payload.get("success") is False:
        raise LocationError(f"公网 IP 定位失败: {payload.get('message', 'unknown error')}")
    city = str(payload.get("city", "")).strip()
    if not city:
        raise LocationError("公网 IP 定位结果没有城市")
    timezone_payload = payload.get("timezone")
    timezone_id = (
        str(timezone_payload.get("id", "")).strip()
        if isinstance(timezone_payload, dict) else ""
    )
    if not timezone_id:
        raise LocationError("公网 IP 定位结果没有时区")
    location = Location(
        ip=str(payload.get("ip", "")).strip(),
        country=str(payload.get("country", "")).strip(),
        region=str(payload.get("region", "")).strip(),
        city=city,
        timezone=timezone_id,
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    relative_cache = os.getenv("LOCATION_CACHE_FILE", "runtime_state/location.json")
    cache_path = runtime_root / relative_cache
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(asdict(location), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(cache_path)
    os.environ["LELAMP_LOCATION_CITY"] = city
    os.environ["LELAMP_TIMEZONE"] = timezone_id
    return location


def resolve_location(root: Path | None = None) -> tuple[Location, bool]:
    """Refresh the saved config, or reuse its last valid value on failure."""
    runtime_root = root or Path(__file__).resolve().parents[1]
    try:
        return refresh_location(runtime_root), True
    except LocationError:
        return load_cached_location(runtime_root), False
