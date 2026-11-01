#!/usr/bin/with-contenv bashio
set -e

export QSH_HOST=$(bashio::config 'qsh_host')
export QSH_PORT=$(bashio::config 'qsh_port')
export QSH_SCHEME=$(bashio::config 'qsh_scheme')
export ENTITY_PREFIX=$(bashio::config 'entity_prefix')
export RECONNECT_DELAY=$(bashio::config 'reconnect_delay')
export LOG_LEVEL=$(bashio::config 'log_level')
export HEAT_PUMP_COP=$(bashio::config 'heat_pump_cop')
export SYSID_POLL_INTERVAL=$(bashio::config 'sysid_poll_interval' '300')
export WEATHER_ENTITY=$(bashio::config 'weather_entity' '')
export WEATHER_POLL_INTERVAL=$(bashio::config 'weather_poll_interval' '1800')

bashio::log.info "Starting QSH Predheat Bridge, connecting to ${QSH_SCHEME}://${QSH_HOST}:${QSH_PORT}/ws/live"

exec python3 /main.py
