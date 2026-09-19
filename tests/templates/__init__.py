import os
import sys
import types

try:
    import vibora  # noqa
except ImportError:
    # The full vibora package requires compiled C extensions which may not be
    # built in this environment. The template subsystem is pure Python, so we
    # register a lightweight package placeholder allowing it to be imported
    # standalone (e.g. `import vibora.templates`).
    if 'vibora' not in sys.modules:
        package = types.ModuleType('vibora')
        package.__path__ = [
            os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'vibora'))
        ]
        sys.modules['vibora'] = package

# The async test wrapper in vibora.tests relies on asyncio.get_event_loop(),
# which raises RuntimeError on modern Python when no loop was explicitly set.
import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
