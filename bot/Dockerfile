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
        fontconfig \
        fonts-noto-core \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
    && rm -rf /var/lib/apt/lists/* \
    && sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen \
    && locale-gen \
    && printf '%s\n' '<?xml version="1.0"?>' '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">' '<fontconfig>' '  <alias><family>sans-serif</family><prefer><family>Noto Sans</family><family>DejaVu Sans</family></prefer></alias>' '  <alias><family>serif</family><prefer><family>Noto Serif</family><family>DejaVu Serif</family></prefer></alias>' '</fontconfig>' > /etc/fonts/local.conf \
    && fc-cache -f >/dev/null

ENV LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8

COPY bot/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && playwright install --with-deps chromium \
    && playwright install chrome --with-deps

RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /run/pulse /audio \
    && chown -R bot:bot /run/pulse /audio

WORKDIR /app
# Code identity stamp, placed here (not at the top) so SHA changes only
# invalidate this cheap tail - the apt/pip/Playwright layers stay cached.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}
COPY bot/ /app/bot/
COPY bot/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && chown -R bot:bot /app

USER bot
ENTRYPOINT ["/app/entrypoint.sh"]
