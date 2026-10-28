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

# PP-OCRv5 weights in ONNX. The Paddle interpreter crashes on ARM, including
# this image's published arm64 build, so the models run through ONNX Runtime.
# CI downloads the pinned release. A local build falls back to ModelScope.
ARG PPOCR_RELEASE=""
ENV CLARITY_OCR_DET=/opt/ppocr/det.onnx \
    CLARITY_OCR_REC=/opt/ppocr/rec.onnx \
    CLARITY_OCR_KEYS=/opt/ppocr/latin_dict.txt \
    OMP_NUM_THREADS=1
RUN --mount=type=secret,id=ppocr_token,required=false \
    set -eux; \
    mkdir -p /opt/ppocr; \
    fetch() { \
      name="$1"; fallback="$2"; dest="$3"; \
      if [ -n "${PPOCR_RELEASE}" ] && [ -s /run/secrets/ppocr_token ]; then \
        curl --retry 5 --retry-all-errors --retry-delay 3 -fL \
          -H "Authorization: Bearer $(cat /run/secrets/ppocr_token)" \
          -H "Accept: application/octet-stream" \
          "${PPOCR_RELEASE}/${name}" -o "$dest"; \
      else \
        curl --retry 8 --retry-all-errors --retry-delay 5 -fL "$fallback" -o "$dest"; \
      fi; \
    }; \
    base="https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2"; \
    fetch det.onnx "$base/onnx/PP-OCRv5/det/ch_PP-OCRv5_det_server.onnx" /opt/ppocr/det.onnx; \
    fetch rec.onnx "$base/onnx/PP-OCRv5/rec/latin_PP-OCRv5_rec_mobile.onnx" /opt/ppocr/rec.onnx; \
    fetch latin_dict.txt "$base/paddle/PP-OCRv5/rec/latin_PP-OCRv5_rec_mobile/ppocrv5_latin_dict.txt" /opt/ppocr/latin_dict.txt; \
    echo "0f8846b1d4bba223a2a2f9d9b44022fbc22cc019051a602b41a7fda9667e4cad  /opt/ppocr/det.onnx" | sha256sum -c -; \
    echo "b20bd37c168a570f583afbc8cd7925603890efbcdc000a59e22c269d160b5f5a  /opt/ppocr/rec.onnx" | sha256sum -c -; \
    test -s /opt/ppocr/latin_dict.txt

COPY backend /srv/clarity/backend
COPY flowable /srv/clarity/flowable
COPY docs /srv/clarity/docs

WORKDIR /srv/clarity/backend
ENV PYTHONPATH=/srv/clarity/backend
EXPOSE 8095
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8095"]
