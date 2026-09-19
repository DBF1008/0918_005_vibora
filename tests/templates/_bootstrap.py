"""
Test bootstrap for the templates sub-package.

The full vibora package cannot be imported in environments where its Cython
extensions are not compiled (vibora/__init__.py eagerly imports the server),
so we register a lightweight namespace stub for the parent package before
pulling the templates sub-package in.
"""
import asyncio
import os
import sys
import types
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

if 'vibora' not in sys.modules or not getattr(sys.modules['vibora'], '__path__', None):
    for module_name in list(sys.modules.keys()):
        if module_name == 'vibora' or module_name.startswith('vibora.'):
            sys.modules.pop(module_name)
    package = types.ModuleType('vibora')
    package.__path__ = [os.path.join(REPO_ROOT, 'vibora')]
    sys.modules['vibora'] = package


def run_async(coro):
    return asyncio.run(coro)


class AsyncTestCase(unittest.TestCase):
    """TestCase that transparently runs async test methods with asyncio.run."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if asyncio.iscoroutinefunction(getattr(self, self._testMethodName, None)):
            original = getattr(self, self._testMethodName)
            setattr(self, self._testMethodName, lambda *a, **kw: asyncio.run(original(*a, **kw)))
