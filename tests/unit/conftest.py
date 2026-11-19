import sys
import types

try:
    import motor.motor_asyncio  # noqa: F401
except ImportError:
    # motor 2.5 imports asyncio.coroutine, which Python 3.11 (CI) removed; the image runs 3.10. Unit tests that import
    # the task repository replace its collection with a fake, so the client class is never used.
    sys.modules['motor.motor_asyncio'] = types.SimpleNamespace(AsyncIOMotorClient=None)
