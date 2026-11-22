# Same base image the commits were labelled in. The selector only reads the
# committed maps (maps/), so the build needs no network beyond this image.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
COPY *.py /app/
COPY maps/ /app/maps/
COPY tools/cache_maps.py tools/cache_replay.py tools/cache_import_impacts.py tools/cache_shortlists.py /app/tools/
RUN python -I /app/tools/cache_maps.py && python -I /app/tools/cache_replay.py && python -I /app/tools/cache_import_impacts.py && python -I /app/tools/cache_shortlists.py && python -m compileall -q --invalidation-mode unchecked-hash /app/cache/pinned /app/*.py && python -I -S /app/collection_guard.py build
CMD ["python", "-I", "/app/selector.py"]
