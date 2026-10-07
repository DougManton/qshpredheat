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
| `qsh_host` | `localhost` | Hostname/IP of the QSH add-on/container |
| `qsh_port` | `8099` | Port QSH's API/WebSocket server listens on |
| `qsh_scheme` | `ws` | `ws` or `wss` |
| `entity_prefix` | `sensor.qsh` | Prefix for created entities |
| `reconnect_delay` | `5` | Seconds to wait before reconnecting after a dropped connection |
| `log_level` | `info` | `debug` / `info` / `warning` / `error` |

## Entities created

- `<prefix>_heat_energy` — state is the furthest-out forecast total (kWh);
  carries an `external` attribute (list of `{last_updated, energy}` points,
  cumulative kWh from now) built by linearly interpolating between whatever
  `forecast_load_kwh_<N>h` checkpoints QSH reports (commonly 4h/12h/24h).
  Point this at Predbat's `load_forecast:` config in place of
  `predheat.heat_energy$external`.
- `<prefix>_forecast_load_kwh_<N>h` — one sensor per checkpoint QSH reports.
- `<prefix>_solar_kwh_12h` — QSH's 12h-ahead solar forecast, if present.

The `<prefix>_heat_energy` sensor also carries a `raw_snapshot` attribute
with QSH's full `forecast_state_snapshot` payload, unmodified — use it in
Developer Tools to confirm the field names this add-on expects
(`forecast_load_kwh_<N>h`, `solar_kwh_12h`, `hourly_solar_first_6`,
`hourly_temps_first_6`, `cold_snap_active`, `wind_active`,
`oat_rise_next_6h_c`) still match your QSH version. These were taken from
QSH's public frontend source rather than a live instance, so if a sensor
comes up empty, check `raw_snapshot` first and adjust `main.py`'s regex /
field lookups to match.

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
