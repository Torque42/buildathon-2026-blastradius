# Same base image the commits were labelled in. The selector only reads the
# committed maps (maps/), so the build needs no network beyond this image.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
COPY isolated_imports.py selector.py identity_select.py compound.py conditions.py condition_select.py predicates.py predicate_select.py flag_readers.py replay_edit.py replay_state.py query_replay.py /app/
COPY maps/ /app/maps/
COPY tools/cache_maps.py tools/cache_replay.py /app/tools/
RUN python -I /app/tools/cache_maps.py && python -I /app/tools/cache_replay.py
CMD ["python", "-I", "/app/selector.py"]
