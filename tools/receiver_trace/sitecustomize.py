import os

if os.getenv("BR_RECEIVER_TEST"):
    import receiver_trace
    receiver_trace.install()
