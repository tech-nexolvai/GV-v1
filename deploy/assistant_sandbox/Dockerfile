# The review assistant's locked sandbox (app/review/assistant/sandbox.py).
# Build once on the machine that runs the API:
#   docker build -t gv-assistant-sandbox deploy/assistant_sandbox
# Each command then runs in a fresh container with no network, a read-only root and claude.ai's
# folders (see DockerSandbox.argv): the drawing set's PDFs read-only at /mnt/user-data/uploads, the
# home folder /home/claude (the side panel's scratch, or a chat's home kept for the whole chat),
# a chat's outputs at /mnt/user-data/outputs and the GV review skill read-only at
# /mnt/skills/gv-review.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "pypdf>=6,<7" "pdfplumber==0.11.*" "pypdfium2>=4,<6" "pillow>=11"

# For the files the agent hands over: reportlab draws the clouds and notes on a copy of a drawing
# (a marked-up PDF), matplotlib a chart, openpyxl a spreadsheet. Installed when the image is built;
# every command still runs with no network.
RUN pip install --no-cache-dir "reportlab>=5,<6" "matplotlib>=3.9,<4" "openpyxl>=3.1,<4"

# The mount points, so the read-only root needs nothing made at run time.
RUN mkdir -p /mnt/user-data/uploads /mnt/user-data/outputs /mnt/skills/gv-review /home/claude \
    && chown 65534:65534 /home/claude /mnt/user-data/outputs

USER 65534:65534
WORKDIR /home/claude
