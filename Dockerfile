FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home bot && mkdir /app/data && chown bot:bot /app/data
COPY bot ./bot
COPY messages ./messages
USER bot
CMD ["python", "-m", "bot"]
