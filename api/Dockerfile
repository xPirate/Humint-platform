FROM python:3.11-slim

# fonts-dejavu-core: used by the PDF intel-package export (see
# api/report_pdf.py). ReportLab bundles Bitstream Vera and falls back to it if
# this isn't present, but Vera covers little beyond Latin-1 — a case file
# holding a Cyrillic or Greek name would render those as blank boxes in an
# exported report. DejaVu is a couple of MB, architecture-independent, and
# fixes that. No other system packages are needed: ReportLab is pure pip,
# which is what keeps this image buildable on a Raspberry Pi.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
