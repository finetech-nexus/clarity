FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libglib2.0-0 \
    libgomp1 \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-fra \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/clarity
COPY backend/requirements.txt /srv/clarity/backend/requirements.txt
RUN pip install --no-cache-dir -r /srv/clarity/backend/requirements.txt

# Weights only. Do not import Paddle here: it segfaults under QEMU when the
# image is built for the other architecture.
ENV CLARITY_PADDLE_DET=/opt/paddleocr/det \
    CLARITY_PADDLE_REC=/opt/paddleocr/rec \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    OMP_NUM_THREADS=1
RUN set -eux; \
    base="https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0"; \
    fetch() { \
      name="$1"; dest="$2"; \
      curl -fsSL "$base/${name}_infer.tar" -o "/tmp/${name}.tar"; \
      rm -rf "/tmp/${name}"; mkdir -p "/tmp/${name}"; \
      tar -xf "/tmp/${name}.tar" -C "/tmp/${name}"; \
      dir="$(find "/tmp/${name}" -name inference.pdiparams -printf '%h\n' | head -1)"; \
      test -n "$dir"; \
      rm -rf "$dest"; mkdir -p "$(dirname "$dest")"; \
      mv "$dir" "$dest"; \
      test -s "$dest/inference.pdiparams"; \
      rm -f "/tmp/${name}.tar"; \
    }; \
    fetch PP-OCRv5_mobile_det /opt/paddleocr/det; \
    fetch latin_PP-OCRv5_mobile_rec /opt/paddleocr/rec

COPY backend /srv/clarity/backend
COPY flowable /srv/clarity/flowable
COPY docs /srv/clarity/docs

WORKDIR /srv/clarity/backend
ENV PYTHONPATH=/srv/clarity/backend
EXPOSE 8095
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095"]
