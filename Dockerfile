FROM python:3.12-slim

# Show script output in the logs immediately
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY newsletter.py .

CMD ["python", "newsletter.py"]
