FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        locales \
        xvfb \
        pulseaudio \
        pulseaudio-utils \
    && rm -rf /var/lib/apt/lists/* \
    && sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen \
    && locale-gen

ENV LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8

COPY bot/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && playwright install --with-deps chromium

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /run/pulse /audio \
    && chown -R bot:bot /run/pulse /audio

WORKDIR /app
COPY bot/__init__.py bot/join_meet.py bot/selectors.py bot/record_audio.py /app/bot/
COPY bot/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && chown -R bot:bot /app

USER bot
ENTRYPOINT ["/app/entrypoint.sh"]
