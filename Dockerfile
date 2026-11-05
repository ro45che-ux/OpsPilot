FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY opspilot ./opspilot
COPY testbed ./testbed
COPY scenarios ./scenarios
RUN pip install --no-cache-dir .
ENV OPSPILOT_STATE_DIR=/state PYTHONUNBUFFERED=1
