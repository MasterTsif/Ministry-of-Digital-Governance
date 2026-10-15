FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY ministry_bot/ ministry_bot/
COPY settings.toml settings.example.toml ./

# Ensure data and logs directories exist
RUN mkdir -p /app/data /app/logs

# Run the bot
CMD ["python", "-m", "ministry_bot"]
