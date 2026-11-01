# Changelog

## 0.1.9

- Added demand-curve shaping: the `external` series published for Predbat
  no longer assumes a flat demand rate across each of QSH's checkpoint
  windows (4h/12h/24h). It now splits each window's energy across its hours
  proportional to a temperature-deficit weight (`T_target_mean - OAT_h`),
  using QSH's own `hourly_temps_first_6` for hours 0-5 and a new optional
  `weather_entity` option (polled via HA's `weather.get_forecasts` action)
  for hours 6+, so an overnight temperature dip gets correctly more of its
  window's energy instead of being averaged flat across the whole window.
  Each window's own total still matches QSH's checkpoint exactly — only the
  shape within it changes. With no `weather_entity` configured (default) or
  missing room-target data, this is mathematically identical to the
  previous flat-interpolation behaviour; new `weather_poll_interval` option
  (default 1800s) controls the poll cadence. New `demand_curve_shaped_hours`
  / `weather_entity` attributes on `<prefix>_heat_energy` show which hours
  got real shaping on a given cycle.

## 0.1.8

- Fixed `Failed to save: Missing option 'sysid_poll_interval' in root` when
  editing config in the HA UI on an install whose stored options predate
  0.1.6 (Supervisor doesn't always backfill a newly-added option into an
  existing install before the next save). `sysid_poll_interval`'s schema is
  now optional (`int(30,3600)?`) with `run.sh` supplying the `300` default
  via `bashio::config`'s fallback argument when the key is absent, instead
  of relying on it always being present.

## 0.1.7

- Fixed log spam and a stuck "unknown" state when QSH's `/api/sysid` is
  unreachable: QSH's own `IngressSecurityMiddleware` returns `403 Forbidden`
  to any REST caller outside the HA Supervisor network/localhost once QSH's
  ingress UI has been opened even once — expected behaviour for this bridge
  (a separate add-on container) in most real installs, not a transient
  fault, and not something this add-on can bypass (QSH exposes no
  configurable escape hatch). `poll_sysid_loop` now logs one `WARNING` per
  distinct failure kind instead of one every `sysid_poll_interval` forever,
  and `<prefix>_forecast_confidence` reports an explicit `unavailable`
  state (vs. `unknown` for "no poll has completed yet") so this is visible
  rather than silently stuck. Documented as "Forecast confidence blocked
  (403)" in DOCS.md.

## 0.1.6

- Added a confidence score alongside the forecast data: polls QSH's
  `/api/sysid` (verified against QSH's own source — `rooms.<room>.
  confidence` is a `low`/`medium`/`high` badge keyed on that room's
  `u_observations` count) on its own `sysid_poll_interval` cadence
  (default 300s, independent of the `/ws/live` forecast cycle). Reduces it
  to the worst confidence tier across all rooms (the bottleneck gates the
  whole aggregate load figure) and publishes it as a new
  `<prefix>_forecast_confidence` sensor (state = overall tier; attributes =
  `bottleneck_room`, `min_u_observations`, per-room breakdown, raw
  `/api/sysid` response) plus `forecast_confidence` /
  `forecast_confidence_detail` attributes on every `<prefix>_heat_energy`
  publish and in the per-cycle log line, so a load figure produced while
  sys-id is still immature is visibly flagged rather than looking as
  trustworthy as a mature one.

## 0.1.5

- Solar gain was never actually netted against QSH's thermal load anywhere
  in the pipeline — `solar_kwh_12h`/`hourly_solar_first_6` were only
  published as informational attributes, despite "QSH forecasts solar,
  predheat doesn't" being this add-on's whole reason to exist. Now nets
  `hourly_solar_first_6`/`solar_kwh_12h` off `forecast_load_kwh_<N>h` before
  the COP conversion, floored at 0; checkpoints beyond 12h (e.g. 24h) can
  only be netted against the 12h solar total (QSH's live feed has no solar
  forecast further out), flagged via `solar_netting_partial_checkpoints`.
- Replaced the static `heat_pump_cop` option as the primary COP source with
  QSH's own live-measured COP (`hp.cop` on every `/ws/live` message, which
  QSH itself nulls out rather than guessing when the heat pump is off or in
  sensor-loss fallback). Falls back to the last live COP seen, then to the
  `heat_pump_cop` option only if QSH has never reported one yet. New
  `heat_pump_cop_source` attribute (`live`/`last_known`/`default`) shows
  which applied. Renamed `forecast_load_kwh_thermal` →
  `forecast_load_kwh_thermal_gross` and added
  `forecast_load_kwh_thermal_net_of_solar` for the intermediate value.

## 0.1.4

- `forecast_load_kwh_<N>h` is gross fabric heat loss in **thermal** kWh, not
  the heat pump's electrical draw, so it can't feed Predbat's
  `load_forecast` (electrical kWh) directly. Added a `heat_pump_cop` option
  (default `3.0`) and divide QSH's thermal forecast by it before publishing
  `<prefix>_heat_energy` / `<prefix>_forecast_load_kwh_<N>h`. Raw thermal
  values are kept as `forecast_load_kwh_thermal` / `thermal_kwh` attributes.
  Documented QSH's `U_total`/`U_design` sys-id maturity check as a separate,
  unimplemented diagnostic for judging forecast trustworthiness.

## 0.1.3

- Document QSH's per-room thermal-model maturity gating (`/api/sysid`,
  `confidence` tiers) as the reason `forecast_load_kwh_<N>h` reads `null`
  on a fresh install, and confirm Predbat's `load_forecast` already treats
  an empty/missing `external` attribute as "skip this source," not zero
  load — safe to wire in before QSH has matured.

## 0.1.2

- `forecast_load_kwh_<N>h` keys are present in QSH's snapshot from the first
  cycle but their values may be `null` while QSH is still calibrating/has
  not reached its confidence "maturity" threshold. Diagnostics now log the
  actual values (not just the key names) so this is distinguishable from a
  genuine field-name mismatch.

## 0.1.1

- Fix default `qsh_port` to 9100 (verified against QSH's `Dockerfile`); the
  previous default of 8099 was an unverified guess and would never connect.

## 0.1.0

- Initial release: bridges QSH's `/ws/live` forecast snapshot into Home
  Assistant sensors shaped for Predbat's `load_forecast`.
