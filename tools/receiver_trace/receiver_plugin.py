import os

import pytest
import receiver_trace

if "BR_RECEIVER_TEST" not in os.environ:
    receiver_trace.install()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    previous = receiver_trace.active
    receiver_trace.active = item.nodeid.split("[", 1)[0]
    try:
        yield
    finally:
        receiver_trace.active = previous
