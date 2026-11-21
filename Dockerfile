# Same base image the commits were labelled in. The selector only reads the
# committed maps (maps/), so the build needs no network beyond this image.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
COPY selector.py /app/selector.py
COPY maps/ /app/maps/
CMD ["python", "-I", "/app/selector.py"]
