#!/usr/bin/env python3
"""QSH -> Home Assistant bridge.

Connects to QSH's live WebSocket feed (/ws/live), extracts the forward
heat-demand / solar forecast QSH already computes, and republishes it as
Home Assistant sensors in the same shape Predbat's load_forecast expects
from the predheat integration (a cumulative-kWh-since-now "external" list).

NOTE: the exact field names inside forecast_state_snapshot were reverse
engineered from QSH's public frontend source (ForecastStatePanel.tsx) and
have not been verified against a live instance. This script is written
defensively: it publishes whatever forecast_load_kwh_<N>h fields it finds
by pattern match, and always publishes the raw snapshot as an attribute so
field names can be confirmed/adjusted without a code change.
"""
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
import websockets

QSH_HOST = os.environ.get("QSH_HOST", "localhost")
QSH_PORT = os.environ.get("QSH_PORT", "9100")
QSH_SCHEME = os.environ.get("QSH_SCHEME", "ws")
ENTITY_PREFIX = os.environ.get("ENTITY_PREFIX", "sensor.qsh")
RECONNECT_DELAY = int(os.environ.get("RECONNECT_DELAY", "5"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API_BASE = "http://supervisor/core/api"

WS_URL = f"{QSH_SCHEME}://{QSH_HOST}:{QSH_PORT}/ws/live"

FORECAST_LOAD_RE = re.compile(r"^forecast_load_kwh_(\d+)h$")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("qshpredheat")


def ha_set_state(entity_id, state, attributes=None):
    if not SUPERVISOR_TOKEN:
        log.warning("No SUPERVISOR_TOKEN available; cannot publish %s", entity_id)
        return
    url = f"{HA_API_BASE}/states/{entity_id}"
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
    payload = {"state": state, "attributes": attributes or {}}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Failed to set %s: %s", entity_id, exc)


def build_external_series(anchors_hours_kwh, step_minutes=10):
    """Piecewise-linear interpolation of cumulative kWh between QSH's
    forecast checkpoints (e.g. 4h/12h/24h), in predheat's "external"
    attribute shape: a list of {last_updated, energy} dicts timestamped
    from now, with energy as cumulative kWh since now."""
    now = datetime.now(timezone.utc)
    points = sorted(anchors_hours_kwh, key=lambda p: p[0])
    if not points or points[0][0] != 0:
        points.insert(0, (0.0, 0.0))

    max_hour = points[-1][0]
    if max_hour <= 0:
        return []

    series = []
    minute = 0
    total_minutes = max_hour * 60
    while minute <= total_minutes:
        hour = minute / 60.0
        lo, hi = points[0], points[-1]
        for i in range(len(points) - 1):
            if points[i][0] <= hour <= points[i + 1][0]:
                lo, hi = points[i], points[i + 1]
                break
        if hi[0] == lo[0]:
            energy = lo[1]
        else:
            frac = (hour - lo[0]) / (hi[0] - lo[0])
            energy = lo[1] + frac * (hi[1] - lo[1])
        stamp = (now + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%S%z")
        series.append({"last_updated": stamp, "energy": round(energy, 3)})
        minute += step_minutes
    return series


def extract_forecast_loads(snapshot):
    loads = {}
    for key, value in snapshot.items():
        match = FORECAST_LOAD_RE.match(key)
        if match and isinstance(value, (int, float)):
            loads[int(match.group(1))] = float(value)
    return loads


def handle_snapshot(payload):
    if payload.get("type") == "keepalive":
        log.debug("keepalive")
        return

    snapshot = payload.get("forecast_state_snapshot")
    if not snapshot:
        log.debug("Message had no forecast_state_snapshot, keys=%s", list(payload.keys()))
        return

    loads = extract_forecast_loads(snapshot)
    if not loads:
        log.warning("No forecast_load_kwh_<N>h fields found; raw snapshot keys=%s", list(snapshot.keys()))

    anchors = [(0.0, 0.0)] + sorted(loads.items())
    external_series = build_external_series(anchors) if len(anchors) > 1 else []

    total_furthest = loads.get(max(loads)) if loads else None
    solar_12h = snapshot.get("solar_kwh_12h")

    ha_set_state(
        f"{ENTITY_PREFIX}_heat_energy",
        state=total_furthest if total_furthest is not None else "unknown",
        attributes={
            "friendly_name": "QSH Forecast Heat Energy",
            "unit_of_measurement": "kWh",
            "state_class": "measurement",
            "external": external_series,
            "forecast_load_kwh": loads,
            "solar_kwh_12h": solar_12h,
            "cold_snap_active": snapshot.get("cold_snap_active"),
            "wind_active": snapshot.get("wind_active"),
            "oat_rise_next_6h_c": snapshot.get("oat_rise_next_6h_c"),
            "hourly_temps_first_6": snapshot.get("hourly_temps_first_6"),
            "hourly_solar_first_6": snapshot.get("hourly_solar_first_6"),
            "raw_snapshot": snapshot,
        },
    )

    for hours, kwh in loads.items():
        ha_set_state(
            f"{ENTITY_PREFIX}_forecast_load_kwh_{hours}h",
            state=kwh,
            attributes={"friendly_name": f"QSH Forecast Load {hours}h", "unit_of_measurement": "kWh", "state_class": "measurement"},
        )

    if solar_12h is not None:
        ha_set_state(
            f"{ENTITY_PREFIX}_solar_kwh_12h",
            state=solar_12h,
            attributes={"friendly_name": "QSH Forecast Solar 12h", "unit_of_measurement": "kWh", "state_class": "measurement"},
        )

    log.info("Published QSH forecast: loads=%s solar_12h=%s", loads, solar_12h)


async def run():
    log.info("Connecting to %s", WS_URL)
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                log.info("Connected to QSH live feed")
                async for message in ws:
                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError:
                        log.debug("Non-JSON message ignored")
                        continue
                    handle_snapshot(payload)
        except (websockets.exceptions.ConnectionClosed, OSError) as exc:
            log.warning("Connection lost (%s); reconnecting in %ss", exc, RECONNECT_DELAY)
        except Exception:
            log.exception("Unexpected error; reconnecting in %ss", RECONNECT_DELAY)
        await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    asyncio.run(run())
