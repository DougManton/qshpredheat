#!/usr/bin/env python3
"""QSH -> Home Assistant bridge.

Connects to QSH's live WebSocket feed (/ws/live), extracts the forward
heat-demand / solar forecast QSH already computes, and republishes it as
Home Assistant sensors in the same shape Predbat's load_forecast expects
from the predheat integration (a cumulative-kWh-since-now "external" list).

QSH's forecast_load_kwh_<N>h is gross fabric heat loss in thermal kWh: it
has not been netted against solar gain, and it is not the heat pump's
electrical draw. Before publishing it for Predbat this script:

  1. Optionally nets off QSH's own solar forecast (hourly_solar_first_6 /
     solar_kwh_12h) from the thermal load, floored at zero — controlled by
     `enable_solar_netting` (default on). This is a real opt-out, not just
     a cosmetic one: QSH's own solar forecast has been observed producing
     physically implausible numbers (e.g. a flat, non-tapering hourly curve
     straight through sunset, or a large solar-gain total on an overcast
     day) traced to QSH's own forecast ingestion, not this add-on or the
     underlying weather data — confirmed by comparing against QSH's own
     Forecast State panel (same wrong numbers there) and the live weather
     entity/outdoor sensor underneath it (correct and varying). When QSH's
     solar forecast can't be trusted, set `enable_solar_netting: false` to
     fall back to the gross thermal load untouched, rather than silently
     under-stating demand by netting off solar that doesn't exist.
  2. Divides by a COP to convert thermal kWh to electrical kWh. A live,
     instantaneous COP is not representative of what will apply over a 24h
     *forecast* (it depends on this instant's flow/outdoor temp, not
     conditions over the forecast window), so a rolling-average COP is what
     this wants. QSH itself computes one (`/api/scop`), but that's a REST
     endpoint behind QSH's own IngressSecurityMiddleware, which blocks any
     caller outside the HA Supervisor network/localhost once QSH's ingress
     UI has been opened even once — this add-on, a separate container, is
     outside that allow-list and cannot reach it. So rather than build a
     feature on data this add-on structurally cannot fetch, this reads the
     rolling COP from a `cop_entity` option instead: a Home Assistant
     entity (e.g. a template/utility-meter sensor you maintain, or
     whatever your heat pump's own integration already exposes) whose state
     is a rolling-average COP. Polled on its own cadence (COP_POLL_INTERVAL)
     via HA Core's own API — not QSH's — so it isn't subject to the same
     restriction. The static `heat_pump_cop` option is the last-resort
     fallback when `cop_entity` is unset, unreadable, or has never reported
     a usable number.

The original (gross, thermal, pre-solar) figures are kept available as
attributes (forecast_load_kwh_thermal_gross / thermal_kwh_gross) for
reference.

The "external" cumulative series is also shaped within each QSH checkpoint
window by hour-of-day temperature, instead of a flat rate across the whole
window: QSH's own hourly_temps_first_6 covers hours 0-5, and the configured
`weather_entity` option (polled via HA's weather.get_forecasts action, also
HA Core's own API) covers hours beyond that, so an overnight temperature dip
inside a window gets correctly more of that window's energy rather than
being averaged flat across it. Each window's own total always matches QSH's
checkpoint exactly; only the shape within it changes. With no weather_entity
configured, or missing room-target data, this degrades to the old flat
behaviour exactly.

NOTE: this add-on deliberately makes no REST calls to QSH at all (only the
unauthenticated /ws/live WebSocket). QSH's IngressSecurityMiddleware blocks
any REST caller outside the HA Supervisor network/localhost once its own
ingress UI has been opened even once, which this add-on (a separate
container) cannot avoid or bypass — there is no QSH-side option to disable
it. An earlier version tried polling QSH's /api/sysid (for a forecast-
confidence summary) and /api/scop (for rolling COP) and hit this wall for
both; that approach was reverted rather than shipping a feature that cannot
work in a normal install. `cop_entity` and `weather_entity` read from Home
Assistant's own API instead, which is not affected by QSH's restriction.

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
ENABLE_SOLAR_NETTING = os.environ.get("ENABLE_SOLAR_NETTING", "true").strip().lower() not in ("false", "0", "no")
HEAT_PUMP_COP = float(os.environ.get("HEAT_PUMP_COP", "3.0"))
COP_ENTITY = os.environ.get("COP_ENTITY", "").strip()
COP_POLL_INTERVAL = int(os.environ.get("COP_POLL_INTERVAL", "3600"))
WEATHER_ENTITY = os.environ.get("WEATHER_ENTITY", "").strip()
WEATHER_POLL_INTERVAL = int(os.environ.get("WEATHER_POLL_INTERVAL", "1800"))

SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN")
HA_API_BASE = "http://supervisor/core/api"

WS_URL = f"{QSH_SCHEME}://{QSH_HOST}:{QSH_PORT}/ws/live"

FORECAST_LOAD_RE = re.compile(r"^forecast_load_kwh_(\d+)h$")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("qshpredheat")

# Most recent rolling COP read from the configured HA cop_entity (polled
# separately — see poll_cop_loop), kept across polls that fail so a brief
# gap doesn't fall all the way back to the static config default.
_last_known_cop = None
_last_cop_error = None


def fetch_cop_entity():
    """Reads COP_ENTITY's current state via HA Core's own REST API (the
    same one ha_set_state talks to) — never QSH's, so this isn't subject to
    QSH's IngressSecurityMiddleware. Returns (value, error); exactly one is
    None. `error` is "disabled" when cop_entity isn't configured,
    "unusable_state" when the entity exists but its state isn't a number
    (e.g. "unknown"/"unavailable"), "http_<status>" for an HTTP error, or
    "unreachable" for a connection-level failure."""
    if not COP_ENTITY:
        return None, "disabled"
    if not SUPERVISOR_TOKEN:
        return None, "no_token"
    url = f"{HA_API_BASE}/states/{COP_ENTITY}"
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        state = resp.json().get("state")
        try:
            return float(state), None
        except (TypeError, ValueError):
            return None, "unusable_state"
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        return None, f"http_{status}"
    except requests.RequestException:
        return None, "unreachable"


async def poll_cop_loop():
    """Refreshes _last_known_cop from cop_entity on its own cadence — a
    rolling-average COP moves slowly, so there's no need to read it every
    forecast cycle. A no-op (never polls) when cop_entity isn't configured,
    so this is a strict opt-in with no cost otherwise."""
    global _last_known_cop, _last_cop_error
    if not COP_ENTITY:
        log.info(
            "cop_entity not configured: COP conversion will use the static "
            "heat_pump_cop default (%.2f).", HEAT_PUMP_COP,
        )
        return
    while True:
        value, error = await asyncio.to_thread(fetch_cop_entity)
        if error is not None:
            if error != _last_cop_error:
                log.warning(
                    "Failed to read COP from %s: %s. Falling back to the last known "
                    "value, or heat_pump_cop if none has ever been seen.",
                    COP_ENTITY, error,
                )
                _last_cop_error = error
            else:
                log.debug("%s still unreadable (%s)", COP_ENTITY, error)
        else:
            _last_cop_error = None
            _last_known_cop = value
            log.debug("Rolling COP from %s: %.3f", COP_ENTITY, value)
        await asyncio.sleep(COP_POLL_INTERVAL)


def resolve_cop():
    """Return (cop, source) where source is "cop_entity", "last_known", or
    "default". Never reads a live/instantaneous COP from QSH — see module
    docstring for why that's unsuitable for a forecast, and why this reads
    from a configurable HA entity rather than QSH's own (blocked) /api/scop."""
    if _last_known_cop is not None:
        source = "cop_entity" if _last_cop_error is None else "last_known"
        return _last_known_cop, source
    return HEAT_PUMP_COP, "default"


# Most recent hourly forecast from the configured HA weather_entity, as a
# list of (datetime, temperature_c) tuples — absolute times, so hour-offset-
# from-now is recomputed fresh at build time rather than going stale between
# polls. Empty/unset whenever WEATHER_ENTITY is "" or the fetch has failed.
_latest_weather_points = []
_last_weather_error = None


def fetch_weather_hourly():
    """Calls Home Assistant's weather.get_forecasts action for WEATHER_ENTITY
    (type=hourly) via HA Core's REST API, the same one ha_set_state already
    talks to. Returns (forecast_list, error); exactly one is None."""
    if not SUPERVISOR_TOKEN:
        return None, "no_token"
    url = f"{HA_API_BASE}/services/weather/get_forecasts?return_response"
    headers = {"Authorization": f"Bearer {SUPERVISOR_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, headers=headers, json={"entity_id": WEATHER_ENTITY, "type": "hourly"}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        forecast = (data.get("service_response") or {}).get(WEATHER_ENTITY, {}).get("forecast")
        if not isinstance(forecast, list):
            return None, "unexpected_response"
        return forecast, None
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        return None, f"http_{status}"
    except requests.RequestException:
        return None, "unreachable"


def parse_weather_forecast(forecast):
    """Extract (datetime, temperature_c) points from a weather.get_forecasts
    hourly response, skipping any entry missing/malformed datetime or
    temperature."""
    points = []
    for entry in forecast:
        if not isinstance(entry, dict):
            continue
        dt_str = entry.get("datetime")
        temp = entry.get("temperature")
        if not isinstance(dt_str, str) or not isinstance(temp, (int, float)) or isinstance(temp, bool):
            continue
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        points.append((dt, float(temp)))
    return points


async def poll_weather_loop():
    """Refreshes _latest_weather_points from WEATHER_ENTITY's hourly forecast
    on its own slower cadence — a no-op (never polls) when weather_entity
    isn't configured, so this is a strict opt-in with no cost otherwise."""
    global _latest_weather_points, _last_weather_error
    if not WEATHER_ENTITY:
        log.info(
            "weather_entity not configured: demand-curve shaping limited to "
            "QSH's own first-6h hourly forecast; hours beyond that use flat "
            "interpolation between QSH's checkpoints, as before."
        )
        return
    while True:
        forecast, error = await asyncio.to_thread(fetch_weather_hourly)
        if error is not None:
            if error != _last_weather_error:
                log.warning(
                    "Failed to fetch hourly forecast from weather.get_forecasts(%s): %s. "
                    "Demand-curve shaping beyond QSH's first 6h will fall back to flat "
                    "interpolation until this recovers.",
                    WEATHER_ENTITY, error,
                )
                _last_weather_error = error
            else:
                log.debug("weather.get_forecasts(%s) still failing (%s)", WEATHER_ENTITY, error)
        else:
            _last_weather_error = None
            _latest_weather_points = parse_weather_forecast(forecast)
            log.debug("Fetched %d hourly forecast points from %s", len(_latest_weather_points), WEATHER_ENTITY)
        await asyncio.sleep(WEATHER_POLL_INTERVAL)


def compute_hourly_weights(snapshot, payload, max_hour):
    """Approximate degree-hour demand weight per hour-of-forecast, so
    build_external_series can shape *within* QSH's checkpoints instead of
    just interpolating flatly between them. Weight for hour h is
    max(0, T_target_mean - OAT_h): a colder hour gets more of that window's
    energy. Hours 0-5 prefer QSH's own hourly_temps_first_6 (the OAT series
    QSH's own forecast engine presumably integrated against); hours beyond
    that fall back to the configured weather_entity's hourly forecast, if
    any. Hours covered by neither are left out of the dict entirely — the
    caller treats a missing hour as weight 1.0 (flat), not 0.

    Returns {} (meaning: shape nothing, fully flat) whenever room target
    temperatures aren't available, since the weighting is meaningless
    without them."""
    rooms = payload.get("rooms")
    if not isinstance(rooms, dict) or not rooms:
        return {}
    targets = [
        r.get("target") for r in rooms.values()
        if isinstance(r, dict) and isinstance(r.get("target"), (int, float)) and not isinstance(r.get("target"), bool)
    ]
    if not targets:
        return {}
    t_target_mean = sum(targets) / len(targets)

    weights = {}
    hourly_temps = snapshot.get("hourly_temps_first_6") or []
    for hour, temp in enumerate(hourly_temps):
        if hour >= max_hour:
            break
        if isinstance(temp, (int, float)) and not isinstance(temp, bool):
            weights[hour] = max(0.0, t_target_mean - temp)

    if _latest_weather_points:
        now = datetime.now(timezone.utc)
        for dt, temp in _latest_weather_points:
            hour = int((dt - now).total_seconds() // 3600)
            if hour in weights or hour < 0 or hour >= max_hour:
                continue
            weights[hour] = max(0.0, t_target_mean - temp)

    return weights


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


def build_external_series(anchors_hours_kwh, hourly_weights=None, step_minutes=10):
    """Piecewise-linear interpolation of cumulative kWh between QSH's
    forecast checkpoints (e.g. 4h/12h/24h), in predheat's "external"
    attribute shape: a list of {last_updated, energy} dicts timestamped
    from now, with energy as cumulative kWh since now.

    hourly_weights, from compute_hourly_weights, reshapes *within* each
    checkpoint window: each window's own total energy is preserved exactly
    (so this never disagrees with QSH's own checkpoint figures), but it's
    split across that window's hours proportional to hourly_weights (a
    missing hour defaults to weight 1.0, i.e. flat) instead of split evenly
    across the whole window. With hourly_weights=None/{} this is
    mathematically identical to the old pure-linear-between-checkpoints
    behaviour."""
    now = datetime.now(timezone.utc)
    points = sorted(anchors_hours_kwh, key=lambda p: p[0])
    if not points or points[0][0] != 0:
        points.insert(0, (0.0, 0.0))

    max_hour = points[-1][0]
    if max_hour <= 0:
        return []

    hourly_weights = hourly_weights or {}

    # Expand QSH's few checkpoints into hourly breakpoints, each window's
    # energy split per compute_hourly_weights instead of spread flat across
    # the whole window.
    hour_points = [(0.0, 0.0)]
    cumulative = 0.0
    for i in range(len(points) - 1):
        lo_hour, lo_kwh = points[i]
        hi_hour, hi_kwh = points[i + 1]
        span_kwh = hi_kwh - lo_kwh
        lo_i, hi_i = int(lo_hour), int(hi_hour)
        hours_in_span = list(range(lo_i, hi_i)) or [lo_i]
        span_weights = [max(hourly_weights.get(h, 1.0), 0.0) for h in hours_in_span]
        total_weight = sum(span_weights)
        if total_weight <= 0:
            span_weights = [1.0] * len(hours_in_span)
            total_weight = float(len(hours_in_span))
        for h, w in zip(hours_in_span, span_weights):
            cumulative += span_kwh * (w / total_weight)
            hour_points.append((float(h + 1), cumulative))
    # Correct float drift so the series still ends exactly on QSH's total.
    hour_points[-1] = (hour_points[-1][0], points[-1][1])

    series = []
    minute = 0
    total_minutes = max_hour * 60
    while minute <= total_minutes:
        hour = minute / 60.0
        lo, hi = hour_points[0], hour_points[-1]
        for i in range(len(hour_points) - 1):
            if hour_points[i][0] <= hour <= hour_points[i + 1][0]:
                lo, hi = hour_points[i], hour_points[i + 1]
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
    # net off QSH's own solar forecast (unless disabled — see module
    # docstring), then convert to electrical kWh using a rolling COP from
    # cop_entity (falling back to the configured default) — Predbat's
    # load_forecast expects electrical kWh.
    if ENABLE_SOLAR_NETTING:
        loads_net_thermal, solar_netted, partial_solar_checkpoints = net_solar(loads_thermal, snapshot)
    else:
        loads_net_thermal = dict(loads_thermal)
        solar_netted = {hours: 0.0 for hours in loads_thermal}
        partial_solar_checkpoints = []
    cop, cop_source = resolve_cop()
    loads = {hours: kwh / cop for hours, kwh in loads_net_thermal.items()}

    anchors = [(0.0, 0.0)] + sorted(loads.items())
    hourly_weights = compute_hourly_weights(snapshot, payload, int(anchors[-1][0])) if len(anchors) > 1 else {}
    external_series = build_external_series(anchors, hourly_weights) if len(anchors) > 1 else []

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
            "solar_netting_enabled": ENABLE_SOLAR_NETTING,
            "heat_pump_cop": cop,
            "heat_pump_cop_source": cop_source,
            "cop_entity": COP_ENTITY or None,
            "solar_kwh_12h": solar_12h,
            "cold_snap_active": snapshot.get("cold_snap_active"),
            "wind_active": snapshot.get("wind_active"),
            "oat_rise_next_6h_c": snapshot.get("oat_rise_next_6h_c"),
            "hourly_temps_first_6": snapshot.get("hourly_temps_first_6"),
            "hourly_solar_first_6": snapshot.get("hourly_solar_first_6"),
            "demand_curve_shaped_hours": sorted(hourly_weights.keys()),
            "weather_entity": WEATHER_ENTITY or None,
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


async def run_ws():
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


async def run():
    if not ENABLE_SOLAR_NETTING:
        log.info("enable_solar_netting is false: publishing QSH's gross thermal load unadjusted for solar.")
    await asyncio.gather(run_ws(), poll_weather_loop(), poll_cop_loop())


if __name__ == "__main__":
    asyncio.run(run())
