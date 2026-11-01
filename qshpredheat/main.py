#!/usr/bin/env python3
"""QSH -> Home Assistant bridge.

Connects to QSH's live WebSocket feed (/ws/live), extracts the forward
heat-demand / solar forecast QSH already computes, and republishes it as
Home Assistant sensors in the same shape Predbat's load_forecast expects
from the predheat integration (a cumulative-kWh-since-now "external" list).

QSH's forecast_load_kwh_<N>h is gross fabric heat loss in thermal kWh: it
has not been netted against solar gain, and it is not the heat pump's
electrical draw. Before publishing it for Predbat this script:

  1. Nets off QSH's own solar forecast (hourly_solar_first_6 /
     solar_kwh_12h) from the thermal load, floored at zero.
  2. Divides by a COP to convert thermal kWh to electrical kWh, preferring
     QSH's own live-measured COP (the top-level "hp.cop" field already
     present on every /ws/live message) over the static `heat_pump_cop`
     add-on option, which is used only as a last-known-good fallback for
     cycles where QSH reports no live COP (heat pump off, or in
     sensor-loss fallback) and no prior live value has been seen yet.

The original (gross, thermal, pre-solar) figures are kept available as
attributes (forecast_load_kwh_thermal / thermal_kwh) for reference.

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
HEAT_PUMP_COP = float(os.environ.get("HEAT_PUMP_COP", "3.0"))

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API_BASE = "http://supervisor/core/api"

WS_URL = f"{QSH_SCHEME}://{QSH_HOST}:{QSH_PORT}/ws/live"

FORECAST_LOAD_RE = re.compile(r"^forecast_load_kwh_(\d+)h$")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("qshpredheat")

# Last live COP QSH reported (payload["hp"]["cop"]), kept across cycles
# where QSH reports none (heat pump off / sensor-loss fallback) so a brief
# gap doesn't fall all the way back to the static config default.
_last_known_cop = None


def resolve_cop(payload):
    """Return (cop, source) where source is "live", "last_known", or
    "default". QSH's own "hp.cop" is null-preserving (null when the heat
    pump is off or in sensor-loss fallback), never a guessed baseline, so
    that's the trustworthy signal to prefer over our own static default."""
    global _last_known_cop
    hp = payload.get("hp")
    live_cop = hp.get("cop") if isinstance(hp, dict) else None
    if isinstance(live_cop, (int, float)) and not isinstance(live_cop, bool):
        _last_known_cop = float(live_cop)
        return _last_known_cop, "live"
    if _last_known_cop is not None:
        return _last_known_cop, "last_known"
    return HEAT_PUMP_COP, "default"


def net_solar(loads_thermal, snapshot):
    """Net QSH's own solar forecast off its gross thermal load per
    checkpoint, floored at zero. hourly_solar_first_6 only covers the first
    6 hours and solar_kwh_12h only the first 12, so checkpoints beyond that
    (e.g. 24h) can only be netted against the 12h solar total we do have —
    an underestimate of the true offset for the hours past 12h. Returns
    (net_loads_thermal, solar_netted_kwh, partial_checkpoints)."""
    hourly_solar = snapshot.get("hourly_solar_first_6") or []
    solar_12h = snapshot.get("solar_kwh_12h")

    net_loads = {}
    solar_netted = {}
    partial = []
    for hours, thermal in loads_thermal.items():
        if hours <= len(hourly_solar):
            solar = sum(hourly_solar[:hours])
        elif solar_12h is not None:
            solar = solar_12h
            if hours > 12:
                partial.append(hours)
        else:
            solar = 0.0
        solar_netted[hours] = solar
        net_loads[hours] = max(0.0, thermal - solar)
    return net_loads, solar_netted, partial


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
        if not match:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            log.debug("%s present but not a usable number: %r (type=%s)", key, value, type(value).__name__)
            continue
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

    loads_thermal = extract_forecast_loads(snapshot)
    if not loads_thermal:
        diag = {k: snapshot.get(k) for k in snapshot if FORECAST_LOAD_RE.match(k)}
        log.warning(
            "forecast_load_kwh_<N>h fields present but not usable yet (likely still None while QSH calibrates/matures): %s",
            diag,
        )

    # QSH's forecast_load_kwh_<N>h is gross fabric heat loss (thermal kWh):
    # net off QSH's own solar forecast, then convert to electrical kWh using
    # QSH's live-measured COP (falling back to the configured default) —
    # Predbat's load_forecast expects electrical kWh.
    loads_net_thermal, solar_netted, partial_solar_checkpoints = net_solar(loads_thermal, snapshot)
    cop, cop_source = resolve_cop(payload)
    loads = {hours: kwh / cop for hours, kwh in loads_net_thermal.items()}

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
            "forecast_load_kwh_thermal_gross": loads_thermal,
            "forecast_load_kwh_thermal_net_of_solar": loads_net_thermal,
            "solar_netted_kwh": solar_netted,
            "solar_netting_partial_checkpoints": partial_solar_checkpoints,
            "heat_pump_cop": cop,
            "heat_pump_cop_source": cop_source,
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
            attributes={
                "friendly_name": f"QSH Forecast Load {hours}h",
                "unit_of_measurement": "kWh",
                "state_class": "measurement",
                "thermal_kwh_gross": loads_thermal.get(hours),
                "thermal_kwh_net_of_solar": loads_net_thermal.get(hours),
                "heat_pump_cop": cop,
                "heat_pump_cop_source": cop_source,
            },
        )

    if solar_12h is not None:
        ha_set_state(
            f"{ENTITY_PREFIX}_solar_kwh_12h",
            state=solar_12h,
            attributes={"friendly_name": "QSH Forecast Solar 12h", "unit_of_measurement": "kWh", "state_class": "measurement"},
        )

    log.info(
        "Published QSH forecast: loads_electrical=%s loads_thermal_net=%s loads_thermal_gross=%s "
        "cop=%s (%s) solar_netted=%s solar_12h=%s",
        loads, loads_net_thermal, loads_thermal, cop, cop_source, solar_netted, solar_12h,
    )


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
