# Production image, used by Render (see render.yaml and docs/deploy.md).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements*.txt ./
RUN pip install -r requirements.txt

# The TagPro auto group bot needs Playwright and Chromium (about 400 MB more). Set the Render
# variable INSTALL_BOT=1 on the service that runs the bot; leave it unset everywhere else.
ARG INSTALL_BOT=0
RUN if [ "$INSTALL_BOT" = "1" ]; then \
        pip install -r requirements-bot.txt && playwright install --with-deps chromium; \
    fi

COPY app ./app
COPY static ./static

# One worker on purpose: the queue lock and the live-update WebSocket list are kept in memory.
# --proxy-headers makes the app see https:// behind Render's proxy.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 \
    --proxy-headers --forwarded-allow-ips="*"
