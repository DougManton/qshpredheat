# QSH Predheat Bridge

Connects to QSH's (Quantum Storm Heating, https://github.com/stuartj1-1981/QSH)
live WebSocket feed at `/ws/live` and republishes its forward heat-demand and
solar forecast as Home Assistant sensors, in the same attribute shape
Predbat's `load_forecast` config expects from the `predheat` integration
(a cumulative-kWh-since-now `external` list).

This add-on deliberately talks to QSH over **only** that one unauthenticated
WebSocket endpoint. QSH's own `IngressSecurityMiddleware` rejects any REST
request (`/api/...`) from outside the HA Supervisor network or localhost with
`403 Forbidden` once QSH's own ingress web UI has been opened even once —
which almost every real install will have done — and this bridge, running in
its own separate add-on container, sits outside that allow-list with no
bypass available (QSH doesn't expose its `QSH_ALLOW_DIRECT_ACCESS` escape
hatch as a configurable add-on option). Earlier versions of this add-on tried
polling QSH's `/api/sysid` (for a forecast-confidence summary) and `/api/scop`
(for a rolling COP) and hit this wall for both — those features were removed
rather than shipped half-working. Anything this add-on needs beyond
`/ws/live`'s own payload (COP, weather) is read from **Home Assistant's own
API** instead, via `cop_entity` / `weather_entity` below — not from QSH.

## Why

`predheat`'s thermal model has no solar-gain term — only a flat
`heat_gain_static` constant — so it overpredicts heating demand on mild or
sunny days. QSH already learns per-room thermal parameters from live data and
publishes a forecast that includes solar (`solar_kwh_12h`,
`hourly_solar_first_6`), so feeding that into Predbat instead should be more
accurate, provided QSH is already deployed and controlling your heat pump.

## Configuration

| Option | Default | Description |
|---|---|---|
| `qsh_host` | `localhost` | Hostname/IP of the QSH add-on/container. If QSH is installed as a Home Assistant add-on, this is **not** `localhost` — find the real value on QSH's own Settings → Add-ons → Quantum Swarm Heating → Info tab, under "Hostname" (looks like `<install-id>-quantum-swarm-heating`). Supervisor add-on containers share an internal network and are reachable from other add-ons by this hostname, independent of whether QSH's web UI uses Ingress. |
| `qsh_port` | `9100` | Port QSH's API/WebSocket server listens on |
| `qsh_scheme` | `ws` | `ws` or `wss` |
| `entity_prefix` | `sensor.qsh` | Prefix for created entities |
| `reconnect_delay` | `5` | Seconds to wait before reconnecting after a dropped connection |
| `log_level` | `info` | `debug` / `info` / `warning` / `error` |
| `heat_pump_cop` | `3.0` | Fallback coefficient of performance, used only when `cop_entity` is unset, unreadable, or has never reported a usable number. See [Thermal vs electrical kWh](#thermal-vs-electrical-kwh) below. |
| `cop_entity` | `""` | A Home Assistant entity whose state is a **rolling-average** COP (e.g. a template sensor or utility-meter helper you maintain from your heat pump's own metered thermal/electrical energy, or whatever your heat pump's integration already exposes) — see [Thermal vs electrical kWh](#thermal-vs-electrical-kwh) below. Leave blank to always use `heat_pump_cop`. |
| `cop_poll_interval` | `3600` | Seconds between reads of `cop_entity`. A rolling average moves slowly, so this doesn't need to be frequent. |
| `weather_entity` | `""` | A Home Assistant weather entity (e.g. `weather.home`) used to shape the demand curve with real hour-by-hour temperature beyond QSH's own 6h forecast — see [Demand curve shaping](#demand-curve-shaping) below. Leave blank to disable (no cost; this option is a strict opt-in). |
| `weather_poll_interval` | `1800` | Seconds between polls of `weather_entity`'s hourly forecast. Weather forecasts update far less often than `/ws/live`'s forecast cadence, so this doesn't need to be frequent. |

## Entities created

- `<prefix>_heat_energy` — state is the furthest-out forecast total, in
  **electrical** kWh (solar-netted thermal kWh ÷ COP); carries an `external`
  attribute (list of `{last_updated, energy}` points, cumulative electrical
  kWh from now) built by interpolating between whatever `forecast_load_kwh_<N>h`
  checkpoints QSH reports (commonly 4h/12h/24h), shaped hour-by-hour within
  each checkpoint window by temperature — see
  [Demand curve shaping](#demand-curve-shaping) below. Point this at
  Predbat's `load_forecast:` config in place of
  `predheat.heat_energy$external`. Also carries, as attributes:
  `forecast_load_kwh_thermal_gross` (QSH's raw, unconverted thermal values),
  `forecast_load_kwh_thermal_net_of_solar` (after solar netting, still
  thermal), `solar_netted_kwh` and `solar_netting_partial_checkpoints` (see
  below), `heat_pump_cop` / `heat_pump_cop_source` (the divisor used and
  where it came from: `cop_entity`, `last_known`, or `default`), `cop_entity`
  (which entity, if any), and `demand_curve_shaped_hours` / `weather_entity`
  (which hours of the curve got real temperature shaping, and from where).
- `<prefix>_forecast_load_kwh_<N>h` — one sensor per checkpoint QSH reports,
  in electrical kWh; each carries the corresponding `thermal_kwh_gross` /
  `thermal_kwh_net_of_solar` / `heat_pump_cop` / `heat_pump_cop_source`
  values as attributes.
- `<prefix>_solar_kwh_12h` — QSH's 12h-ahead solar forecast, if present.

## Thermal vs electrical kWh

QSH's `forecast_load_kwh_<N>h` is **gross fabric heat loss**, in thermal
kWh: how much heat the building is calculated to lose over the next N
hours, before solar gain is netted off, and before converting to how much
electricity the heat pump will draw to replace it. Predbat's
`load_forecast` expects electrical kWh (it's forecasting what the
battery/grid will need to supply), so before publishing, this add-on:

1. **Nets off solar gain.** QSH reports fabric heat loss and solar gain as
   two separate figures (`forecast_load_kwh_<N>h` and
   `solar_kwh_12h`/`hourly_solar_first_6`) rather than a solar-adjusted
   load — so unless something nets them against each other, solar isn't
   actually accounted for anywhere, despite it being the whole reason to
   prefer QSH's forecast over `predheat`'s no-solar-gain model (see "Why"
   above). This add-on now does that netting itself: for the 4h checkpoint
   it subtracts the first 4 values of `hourly_solar_first_6`; for 12h it
   subtracts `solar_kwh_12h` directly; for any checkpoint beyond 12h (e.g.
   24h) it can only subtract the 12h solar total, since QSH's `/ws/live`
   feed carries no solar forecast beyond 12h — the result for a >12h
   checkpoint therefore under-nets the true solar offset for the hours past
   12h. Checkpoints netted this way are listed in
   `solar_netting_partial_checkpoints`. Each net figure is floored at 0 (a
   sunny-enough window can't produce negative heating demand in this
   approximation). The un-netted gross values stay available as
   `forecast_load_kwh_thermal_gross` for comparison.
2. **Converts thermal kWh to electrical kWh using a rolling-average COP,
   not a live/instantaneous one.** A live COP reading (QSH's `/ws/live`
   feed does carry one, in `hp.cop`) reflects this instant's flow/outdoor
   temperature — it says nothing about what COP will actually apply, on
   average, over the next 24 forecast hours, and it's frequently `null`
   anyway (QSH nulls it out whenever the heat pump is off, rather than
   guessing). QSH computes a proper rolling figure itself
   (`/api/scop?window=7d&mode=ch`), but that's a REST endpoint blocked by
   QSH's `IngressSecurityMiddleware` for this add-on (see the note at the
   top of this document) — so rather than build on data this add-on cannot
   reliably reach, point `cop_entity` at a Home Assistant entity that
   already tracks this for you instead (a template sensor computing
   `Σ thermal / Σ electrical` over a trailing window from your heat pump's
   own metered energy, a utility-meter helper, or whatever your heat pump's
   own integration exposes). This add-on reads that entity's state every
   `cop_poll_interval` seconds via HA's own API and divides the net thermal
   load by it. If `cop_entity` is unset, unreadable, or its state isn't a
   number (`unknown`/`unavailable`), it falls back to the last value it
   *has* read (`heat_pump_cop_source: "last_known"`), and only falls back
   to the static `heat_pump_cop` option (`"default"`) if no value has ever
   been read.

If you'd rather judge whether QSH's thermal forecast itself is trustworthy
(independent of COP), QSH's author gives this check:

```
U_total (kW/K) = Load_4h ÷ Σ (T_target − OAT_h), for h = the next 4 hours
```

1. Read `Load_4h` (`forecast_load_kwh_thermal_gross` → `4` — use the gross,
   pre-solar-netting figure here, since the formula's own `Σ (T_target −
   OAT_h)` denominator is a pure conduction term with no solar in it; or
   read it from the Forecast State panel in QSH's own UI). If unavailable, a
   zone has no temperature sensor or hasn't learned its U yet.
2. `T_target` = the plain mean of each room's current target temperature
   (unweighted — QSH doesn't weight by area).
3. `OAT_h` = the first 4 points of QSH's "Hourly OAT (next 6h)" forecast
   (`hourly_temps_first_6` on `<prefix>_heat_energy`), not your outdoor
   sensor.
4. Sum `(T_target − OAT_h)` over those 4 hours (units: K·h), then divide
   `Load_4h` by that sum to get `U_total`.
5. Compare against your design value,
   `U_design (kW/K) = design heat loss (kW) ÷ (design indoor temp − design outdoor temp)`,
   from your MCS survey. `U_total ÷ U_design ≈ 1` means the survey and
   QSH's sys-id agree; well off 1 means either the survey was wrong or QSH's
   sys-id hasn't matured yet (see "QSH still calibrating" below).

This add-on does not compute `U_total` for you today — the raw inputs
(`forecast_load_kwh_thermal_gross`, `hourly_temps_first_6`) are published so
you can do it in a Home Assistant template sensor if you want to track it.
Note this check itself needs QSH's `/api/sysid` (via its own web UI, or a
browser/client that *is* on the allowed network) to read `u_observations` —
that's a manual, occasional check you run yourself, not something this
add-on automates, for the same reachability reason described at the top of
this document.

The `<prefix>_heat_energy` sensor also carries a `raw_snapshot` attribute
with QSH's full `forecast_state_snapshot` payload, unmodified — use it in
Developer Tools to confirm the field names this add-on expects
(`forecast_load_kwh_<N>h`, `solar_kwh_12h`, `hourly_solar_first_6`,
`hourly_temps_first_6`, `cold_snap_active`, `wind_active`,
`oat_rise_next_6h_c`) still match your QSH version. These were taken from
QSH's public frontend source rather than a live instance, so if a sensor
comes up empty, check `raw_snapshot` first and adjust `main.py`'s regex /
field lookups to match.

## Demand curve shaping

QSH only gives a handful of checkpoints (commonly 4h/12h/24h), so turning
those into the minute-by-minute `external` series Predbat reads requires
interpolating between them somehow. Pure linear interpolation (this add-on's
original behaviour) assumes a flat demand rate across each whole checkpoint
window — so a window spanning, say, midnight to noon gets the same assumed
rate overnight (when it's coldest and demand is highest) as mid-morning,
even though the window's *total* energy (from QSH) is correct.

This add-on now shapes the curve within each checkpoint window by an
hour-by-hour temperature-deficit weight — `max(0, T_target_mean − OAT_h)`
for each hour `h` — so a colder hour gets a larger share of that window's
energy and a milder hour gets less. Each window's own total always still
matches QSH's checkpoint figure exactly; only the shape *within* it changes.
`T_target_mean` is the mean of each room's current target temperature, from
the same `/ws/live` message (no extra request needed). The hourly
temperature itself comes from two sources, by hour:

- **Hours 0-5**: QSH's own `hourly_temps_first_6`, since that's presumably
  the same OAT series QSH's forecast engine integrated against internally.
- **Hours 6 onward**: the `weather_entity` option, if configured — polled
  every `weather_poll_interval` seconds via Home Assistant's
  `weather.get_forecasts` action (`type: hourly`), the same mechanism HA's
  own automations use to read a weather integration's hourly forecast. This
  is HA's own API, not QSH's, so it isn't affected by the reachability note
  at the top of this document. Point it at whatever weather entity you'd
  normally use for this (e.g. `weather.home`); it must support an hourly
  forecast (not just daily) for this to have any effect beyond hour 6.

Any hour with no data from either source — `weather_entity` left blank, the
weather call failing, or room target temperatures unavailable — falls back
to an equal share for that hour, which is mathematically identical to the
old flat behaviour. There's no partial-failure risk: shaping is strictly
additive on top of the existing totals, never a replacement for them.
`<prefix>_heat_energy`'s `demand_curve_shaped_hours` attribute lists exactly
which hours (0-indexed, from now) got real temperature shaping on a given
cycle, so you can confirm it's working rather than guessing from the curve
shape alone.

## QSH still calibrating

On a fresh QSH install, `forecast_load_kwh_4h/12h/24h` will come through as
`null` (this add-on then publishes an empty `external: []`) until QSH's
per-room thermal model has enough passive observations. Check QSH's own
`/api/sysid` (via its web UI, or a client on the Supervisor network/
localhost — see the reachability note at the top of this document) for
per-room `u_observations` / `c_observations` counts and a `confidence` tier
(`low` under 10 observations — using a prior only, `medium` 10-99, `high`
100+); the load forecast is expected to populate once rooms clear the `low`
tier.

This is safe to leave wired into Predbat while QSH calibrates: Predbat's
`fetch_extra_load_forecast()` treats a missing/empty `external` attribute as
"skip this source" (logs a `Warn` and drops it from the sum), not as zero
load — it falls back to its other configured load-forecast sources in the
meantime.

## Wiring into Predbat

In `apps.yaml`, replace:

```yaml
load_forecast:
  - predheat.heat_energy$external
```

with:

```yaml
load_forecast:
  - sensor.qsh_heat_energy$external
```

(adjust the entity id if you changed `entity_prefix`).
