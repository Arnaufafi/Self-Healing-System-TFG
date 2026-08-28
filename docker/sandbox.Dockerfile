# Hermetic test runner used by both qa_handoff_node and validation_node.
#
# Build (from repo root):
#   docker build -t self-healing-sandbox:latest -f docker/sandbox.Dockerfile .
#
# The sandbox adapter mounts the candidate workspace read-write at
# /workspace and runs `pytest <node_id> -x --tb=short`, so the image
# only needs the pytest runner on PATH. Project-specific dependencies
# are expected to be vendored in the workspace itself (e.g. an editable
# install or a requirements.txt processed by the upstream pipeline).
FROM python:3.12-slim

RUN pip install --no-cache-dir --disable-pip-version-check pytest==8.* \
    && pytest --version

WORKDIR /workspace
