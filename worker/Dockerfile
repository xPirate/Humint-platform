FROM python:3.11-slim

# tesseract-ocr: OCR for uploaded images/scanned documents.
# poppler-utils: pdftotext/pdftoppm, used for PDF text extraction and
# rasterizing scanned-PDF pages before OCR.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
