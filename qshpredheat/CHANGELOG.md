# Changelog

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
