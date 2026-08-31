FROM python:3.13-slim-trixie

# Chromium and its driver come from the same Debian source package, so apt
# keeps their major versions in lockstep. That matters more than it looks:
# a chromedriver even one major version off the browser refuses to start,
# and it is the usual reason Selenium-in-Docker setups rot.
#
# The Wayland libs are here for the headed authenticate.py run on the Pi's
# touchscreen; the server itself never needs them.
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium chromium-driver \
      fonts-liberation ca-certificates \
      libwayland-client0 libwayland-cursor0 libwayland-egl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY goodreads_mcp_server.py authenticate.py ./

EXPOSE 8000

CMD ["python", "goodreads_mcp_server.py"]
