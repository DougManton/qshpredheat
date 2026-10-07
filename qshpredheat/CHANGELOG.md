# Changelog

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
