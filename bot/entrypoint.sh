#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
for _ in $(seq 1 25); do sleep 0.2; done

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR" /run/pulse
chmod 700 "$XDG_RUNTIME_DIR"

pulseaudio --start --exit-idle-time=-1 --disable-shm
pactl load-module module-null-sink sink_name=virtual_speaker >/dev/null
pactl load-module module-native-protocol-unix socket=/run/pulse/native >/dev/null
pactl set-default-sink virtual_speaker >/dev/null

export PULSE_SERVER=unix:/run/pulse/native
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

cd /app
exec python -m bot.join_meet
