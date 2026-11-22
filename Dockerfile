# Same base image the commits were labelled in. The selector only reads the
# committed maps and replays updated fixture rows using the pinned upstream test.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1
COPY *.py /app/
COPY maps/ /app/maps/
COPY tools/cache_maps.py tools/cache_replay.py tools/cache_import_impacts.py /app/tools/
RUN python -I /app/tools/cache_maps.py && python -I /app/tools/cache_replay.py && python -I /app/tools/cache_import_impacts.py && python -m compileall -q --invalidation-mode unchecked-hash /app/cache/pinned /app/*.py && python -I -S /app/collection_guard.py build
COPY fixture-requirements.txt /tmp/fixture-requirements.txt
RUN python -m pip install --disable-pip-version-check --only-binary=:all: --no-cache-dir -r /tmp/fixture-requirements.txt
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot-pin.tar.gz
RUN echo "93b2445f15f395b81c20556777ece19a6c7507df950f13fa9fac4a3be739af0d  /tmp/sqlglot-pin.tar.gz" | sha256sum -c - && tar -xzf /tmp/sqlglot-pin.tar.gz --strip-components=1 -C /app/cache/pinned sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa/tests/fixtures/optimizer/tpc-ds sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa/tests/fixtures/optimizer/tpc-h
CMD ["python", "-I", "/app/selector.py"]
