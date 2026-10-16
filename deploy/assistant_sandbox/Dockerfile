# The review assistant's locked sandbox (app/review/assistant/sandbox.py).
# Build once on the machine that runs the API:
#   docker build -t gv-assistant-sandbox deploy/assistant_sandbox
# Each command then runs in a fresh container with no network, a read-only root, this package's
# PDFs read-only at /work and a scratch folder at /out (see DockerSandbox.argv).
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "pypdf>=6,<7" "pdfplumber==0.11.*" "pypdfium2>=4,<6" "pillow>=11"

USER 65534:65534
WORKDIR /out
