# Changelog

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
