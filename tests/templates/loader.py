import os
import shutil
import tempfile
import time

from vibora.templates import TemplateEngine
from vibora.templates.compilers.python import PythonTemplateCompiler
from vibora.templates.loader import TemplateLoader
from vibora.tests import TestSuite


class CountingCompiler(PythonTemplateCompiler):
    """Counts compilations so tests can assert incremental behavior."""

    def __init__(self):
        super().__init__()
        self.compiled_hashes = []

    def compile(self, template, verbose=False):
        self.compiled_hashes.append(template.hash)
        return super().compile(template, verbose=verbose)


class IncrementalReloadSuite(TestSuite):

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.engine = TemplateEngine(compiler=CountingCompiler())
        self.loader = TemplateLoader([self.directory], self.engine)

    def write_template(self, name, content):
        path = os.path.join(self.directory, name)
        with open(path, 'w') as f:
            f.write(content)
        # Bumping mtime so the polling check always notices the change,
        # even on filesystems with coarse mtime resolution.
        future = time.time() + 5
        os.utime(path, (future, future))
        return path

    def load_all(self):
        self.loader.load()
        self.engine.compile_templates()
        self.engine.compiler.compiled_hashes.clear()

    async def test_only_changed_template_is_recompiled(self):
        self.write_template('a.html', 'A')
        self.write_template('b.html', 'B')
        self.write_template('c.html', 'C')
        self.load_all()

        self.write_template('b.html', 'B2')
        self.loader.check_for_modified_templates()

        self.assertEqual(1, len(self.engine.compiler.compiled_hashes))
        self.assertEqual('A', await self.engine.render('a.html'))
        self.assertEqual('B2', await self.engine.render('b.html'))
        self.assertEqual('C', await self.engine.render('c.html'))

    async def test_new_template_is_compiled_incrementally(self):
        self.write_template('a.html', 'A')
        self.load_all()

        self.write_template('new.html', 'NEW')
        self.loader.check_for_modified_templates()

        self.assertEqual(1, len(self.engine.compiler.compiled_hashes))
        self.assertEqual('NEW', await self.engine.render('new.html'))

    async def test_dependents_are_recompiled_when_parent_changes(self):
        self.write_template('parent.html', 'PARENT_V1')
        self.write_template('child.html', '{% include "parent.html" %}')
        self.load_all()
        self.assertEqual('PARENT_V1', await self.engine.render('child.html'))

        self.write_template('parent.html', 'PARENT_V2')
        self.loader.check_for_modified_templates()

        # The parent and its dependent child must be recompiled, nothing else.
        self.assertEqual(2, len(self.engine.compiler.compiled_hashes))
        self.assertEqual('PARENT_V2', await self.engine.render('child.html'))

    async def test_unrelated_templates_are_not_touched_on_reload(self):
        self.write_template('parent.html', 'P')
        self.write_template('child.html', '{% include "parent.html" %}')
        self.write_template('lonely.html', 'L')
        self.load_all()

        self.write_template('parent.html', 'P2')
        self.loader.check_for_modified_templates()

        lonely_hash = self.engine.templates['lonely.html'].hash
        self.assertNotIn(lonely_hash, self.engine.compiler.compiled_hashes)
        self.assertEqual('L', await self.engine.render('lonely.html'))
