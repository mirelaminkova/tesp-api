import sys
import types

try:
    import motor.motor_asyncio  # noqa: F401
except ImportError as error:
    if 'coroutine' not in str(error):
        raise
    # motor 2.5 imports asyncio.coroutine, removed in Python 3.11 (CI uses 3.11; the image uses 3.10).
    # Unit tests replace task_repository._tasks with a fake, so AsyncIOMotorClient is never called.
    # Remove once motor >= 3 or CI matches the image's Python.
    sys.modules['motor.motor_asyncio'] = types.SimpleNamespace(AsyncIOMotorClient=None)
