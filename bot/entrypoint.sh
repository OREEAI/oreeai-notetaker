#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
Xvfb :99 -screen 0 2400x1350x24 -nolisten tcp &
for _ in $(seq 1 25); do sleep 0.2; done

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/runtime-$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR" /run/pulse
chmod 700 "$XDG_RUNTIME_DIR"

pulseaudio --start --exit-idle-time=-1 --disable-shm
pactl load-module module-null-sink sink_name=virtual_speaker >/dev/null
pactl load-module module-null-sink sink_name=silent_sink sink_properties=device.description=Silent_Test_Sink >/dev/null
pactl load-module module-remap-source master=virtual_speaker.monitor source_name=virtual_mic source_properties=device.description=Virtual_Microphone >/dev/null
pactl load-module module-native-protocol-unix socket=/run/pulse/native >/dev/null
pactl set-default-sink virtual_speaker >/dev/null

export PULSE_SERVER=unix:/run/pulse/native
export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

# Interactive one-time sign-in for the persistent Chrome profile. VNC and
# noVNC bind loopback only; the Makefile publishes 7900 on 127.0.0.1.
if [ "${BOT_ENTRY_MODE:-run}" = "login" ]; then
  x11vnc -display :99 -localhost -nopw -forever -shared -bg >/dev/null 2>&1
  websockify --web=/usr/share/novnc/ 7900 localhost:5900 >/tmp/oreeai-websockify.log 2>&1 &
  cd /app
  exec python -m bot.login
fi

cd /app
exec python -m bot.join_meet
