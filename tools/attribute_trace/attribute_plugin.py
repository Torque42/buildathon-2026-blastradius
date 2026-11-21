"""Record dynamic reads in each ordinary pytest item at the pinned revision."""
import json
import os
from pathlib import Path

import pytest
import qualified


qualified.install(json.loads(Path(os.environ["BR_ATTRIBUTE_NAMES"]).read_bytes()))
ran = set()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    qualified.active = item.nodeid.split("[", 1)[0]
    qualified.seen.clear()
    ran.add(qualified.active)
    try:
        yield
    finally:
        qualified.active = None


def pytest_sessionfinish():
    output = {
        "ran": sorted(ran),
        "observed": {node_id: sorted(reads) for node_id, reads in qualified.observed.items()},
    }
    Path(os.environ["BR_ATTRIBUTE_TRACE"]).write_text(json.dumps(output))
