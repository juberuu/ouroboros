# Ouroboros — Docker image for web UI runtime
# Usage:
#   docker build -t ouroboros-web .
#   docker run --rm -p 8765:8765 ouroboros-web

FROM python:3.10-slim

# System dependencies (git + Playwright/Chromium native libs installed via playwright install-deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Working directory
ENV APP_HOME=/app
WORKDIR ${APP_HOME}

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install all Playwright native system dependencies for Chromium (authoritative list from Playwright)
RUN python3 -m playwright install-deps chromium

# Install Playwright Chromium browser binary so browser tools work out of the box
RUN PLAYWRIGHT_BROWSERS_PATH=0 python3 -m playwright install chromium

# Copy application
COPY . .

# Default environment
ENV OUROBOROS_SERVER_HOST=0.0.0.0 \
    OUROBOROS_SERVER_PORT=8765 \
    OUROBOROS_FILE_BROWSER_DEFAULT=${APP_HOME}

EXPOSE 8765

ENTRYPOINT ["python", "server.py"]
