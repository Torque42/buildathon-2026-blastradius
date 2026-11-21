# Same base image the commits were labelled in. The selector only reads the
# committed maps (maps/), so the build needs no network beyond this image.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
COPY selector.py predicates.py predicate_select.py flag_readers.py replay_edit.py replay_state.py query_replay.py /app/
COPY maps/ /app/maps/
COPY tools/cache_maps.py /app/tools/cache_maps.py
RUN python -I /app/tools/cache_maps.py
CMD ["python", "-I", "/app/selector.py"]
