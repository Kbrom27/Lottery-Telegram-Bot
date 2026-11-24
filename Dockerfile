FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-amh \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY draw.py .
ENV PYTHONUNBUFFERED=1

# ONE process on purpose: it runs the bot (polling), the verification workers and the HTTP endpoints.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
