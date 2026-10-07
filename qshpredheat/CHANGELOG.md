# Changelog

## 0.1.1

- Fix default `qsh_port` to 9100 (verified against QSH's `Dockerfile`); the
  previous default of 8099 was an unverified guess and would never connect.

## 0.1.0

- Initial release: bridges QSH's `/ws/live` forecast snapshot into Home
  Assistant sensors shaped for Predbat's `load_forecast`.
