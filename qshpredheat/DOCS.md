# QSH Predheat Bridge

Connects to QSH's (Quantum Storm Heating, https://github.com/stuartj1-1981/QSH)
live WebSocket feed at `/ws/live` and republishes its forward heat-demand and
solar forecast as Home Assistant sensors, in the same attribute shape
Predbat's `load_forecast` config expects from the `predheat` integration
(a cumulative-kWh-since-now `external` list).

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
| `heat_pump_cop` | `3.0` | Fallback coefficient of performance, used only when QSH has never yet reported a live COP (fresh install, heat pump always off/sensor-loss so far). Normally this add-on reads COP from QSH itself — see [Thermal vs electrical kWh](#thermal-vs-electrical-kwh) below. |
| `sysid_poll_interval` | `300` | Seconds between polls of QSH's `/api/sysid` for the confidence summary — see [Forecast confidence](#forecast-confidence) below. Observation counts only grow over many control cycles, so this doesn't need to track the `/ws/live` forecast cadence. |

## Entities created

- `<prefix>_heat_energy` — state is the furthest-out forecast total, in
  **electrical** kWh (solar-netted thermal kWh ÷ COP); carries an `external`
  attribute (list of `{last_updated, energy}` points, cumulative electrical
  kWh from now) built by linearly interpolating between whatever
  `forecast_load_kwh_<N>h` checkpoints QSH reports (commonly 4h/12h/24h).
  Point this at Predbat's `load_forecast:` config in place of
  `predheat.heat_energy$external`. Also carries, as attributes:
  `forecast_load_kwh_thermal_gross` (QSH's raw, unconverted thermal values),
  `forecast_load_kwh_thermal_net_of_solar` (after solar netting, still
  thermal), `solar_netted_kwh` and `solar_netting_partial_checkpoints` (see
  below), and `heat_pump_cop` / `heat_pump_cop_source` (the divisor used and
  where it came from: `live`, `last_known`, or `default`).
- `<prefix>_forecast_load_kwh_<N>h` — one sensor per checkpoint QSH reports,
  in electrical kWh; each carries the corresponding `thermal_kwh_gross` /
  `thermal_kwh_net_of_solar` / `heat_pump_cop` / `heat_pump_cop_source`
  values as attributes.
- `<prefix>_solar_kwh_12h` — QSH's 12h-ahead solar forecast, if present.
- `<prefix>_forecast_confidence` — state is the worst per-room confidence
  tier (`low`/`medium`/`high`/`unknown`) QSH's `/api/sysid` reports across
  all rooms, polled every `sysid_poll_interval` seconds (see
  [Forecast confidence](#forecast-confidence) below). Carries
  `bottleneck_room`, `min_u_observations`, a full `rooms` breakdown, and the
  raw `/api/sysid` response as attributes. `<prefix>_heat_energy` also
  carries this same summary (`forecast_confidence` /
  `forecast_confidence_detail`) so it travels with the forecast it describes.

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
2. **Converts thermal kWh to electrical kWh using QSH's own COP.** QSH
   already tracks heat pump COP live — every `/ws/live` message carries a
   top-level `hp.cop` field, which QSH itself sets to `null` (rather than
   guessing) whenever the heat pump is off or in sensor-loss fallback. This
   add-on reads that value each cycle and divides the net thermal load by
   it. When `hp.cop` is `null`, it falls back to the last live value it
   *has* seen (`heat_pump_cop_source: "last_known"`), and only falls back
   to the static `heat_pump_cop` option (`"default"`) if QSH has never
   reported a live COP at all. A flat, config-supplied COP is no longer the
   normal path — it's purely the cold-start/always-off fallback.

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

The `<prefix>_heat_energy` sensor also carries a `raw_snapshot` attribute
with QSH's full `forecast_state_snapshot` payload, unmodified — use it in
Developer Tools to confirm the field names this add-on expects
(`forecast_load_kwh_<N>h`, `solar_kwh_12h`, `hourly_solar_first_6`,
`hourly_temps_first_6`, `cold_snap_active`, `wind_active`,
`oat_rise_next_6h_c`) still match your QSH version. These were taken from
QSH's public frontend source rather than a live instance, so if a sensor
comes up empty, check `raw_snapshot` first and adjust `main.py`'s regex /
field lookups to match. `<prefix>_forecast_confidence` carries the equivalent
`raw_sysid` attribute for `/api/sysid`'s response shape (`rooms.<room>.
confidence` / `u_observations`), verified against QSH's own source rather
than reverse-engineered, but still worth checking if it comes up `unknown`.

## Forecast confidence

A forecast can be numerically present (`forecast_load_kwh_<N>h` no longer
`null`) while still resting on an immature sys-id — QSH's own per-room
`confidence` badge (`low`/`medium`/`high`, from `/api/sysid`'s
`u_observations` count) is the signal for that, not whether a number has
appeared yet. This add-on polls `/api/sysid` every `sysid_poll_interval`
seconds and reduces it to one overall tier: the **worst** confidence across
all rooms, since `forecast_load_kwh_<N>h` sums fabric loss over every room,
so one poorly-observed room can make the whole aggregate figure unreliable
even if every other room is mature. That worst-case tier, the room
responsible for it (`bottleneck_room`), and its observation count
(`min_u_observations`) are attached to `<prefix>_forecast_confidence` and
to every `<prefix>_heat_energy` publish (`forecast_confidence` /
`forecast_confidence_detail`), so a load figure produced while confidence is
`low` is visibly flagged as such rather than looking as trustworthy as one
produced once rooms have matured. This is a count-of-evidence signal only —
it says nothing about whether the *learned* U value is numerically close to
reality, which is what the `U_total`/`U_design` cross-check above is for.

## QSH still calibrating

On a fresh QSH install, `forecast_load_kwh_4h/12h/24h` will come through as
`null` (this add-on then publishes an empty `external: []`) until QSH's
per-room thermal model has enough passive observations. Check
`http://<qsh_host>:<qsh_port>/api/sysid` for per-room `u_observations` /
`c_observations` counts and a `confidence` tier (`low` under 10
observations — using a prior only, `medium` 10-99, `high` 100+); the load
forecast is expected to populate once rooms clear the `low` tier. Once it
does, keep an eye on `<prefix>_forecast_confidence` for a while — clearing
`low` is gradual per room, so the aggregate load figure can stay
confidence-gated for some time after it first stops being `null`.

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
