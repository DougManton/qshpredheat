# qshpredheat

A Home Assistant add-on repository containing the **QSH Predheat Bridge**
add-on: it connects to [QSH](https://github.com/stuartj1-1981/QSH)'s live
WebSocket feed and republishes its forward heat-demand/solar forecast as HA
sensors, shaped for [Predbat](https://github.com/springfall2008/batpred)'s
`load_forecast` config — as a replacement for `predheat`'s simpler
no-solar-gain thermal model.

## Install

1. In Home Assistant: Settings → Add-ons → Add-on Store → ⋮ → Repositories.
2. Add `https://github.com/DougManton/qshpredheat`.
3. Install "QSH Predheat Bridge" from the store, configure `qsh_host` /
   `qsh_port` for your QSH instance, and start it.

See [qshpredheat/DOCS.md](qshpredheat/DOCS.md) for configuration options,
the entities it creates, and how to wire it into Predbat's `apps.yaml`.

## Status

Field names used to parse QSH's forecast payload were taken from QSH's
public frontend source, not verified against a live instance. The add-on
always publishes the raw snapshot as an attribute so this can be checked
and adjusted without rebuilding the image — see DOCS.md.
