FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/clarity
COPY backend /srv/clarity/backend
COPY flowable /srv/clarity/flowable
COPY docs /srv/clarity/docs
RUN pip install --no-cache-dir -r /srv/clarity/backend/requirements.txt

WORKDIR /srv/clarity/backend
ENV PYTHONPATH=/srv/clarity/backend
EXPOSE 8095
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095"]
