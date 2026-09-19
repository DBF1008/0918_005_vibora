import asyncio
import os
import tempfile
import time

from ._bootstrap import AsyncTestCase
from vibora.templates import TemplateEngine, Template
from vibora.templates.loader import TemplateLoader
from vibora.templates.compilers.python import PythonTemplateCompiler


class CountingCompiler(PythonTemplateCompiler):

    compile_calls = 0

    def compile(self, template, verbose=False):
        CountingCompiler.compile_calls += 1
        return super().compile(template, verbose=verbose)


class LoaderSuite(AsyncTestCase):

    def setUp(self):
        CountingCompiler.compile_calls = 0
        self.directory = tempfile.TemporaryDirectory()
        self.root = self.directory.name
        os.mkdir(os.path.join(self.root, 'sub'))
        self.write('base.html', 'base {% include "sub/part.html" %}')
        self.write('sub/part.html', 'PART')
        self.write('page.html', '{% include "base.html" %}PAGE')
        self.engine = TemplateEngine(compiler=CountingCompiler())
        self.loader = TemplateLoader([self.root], self.engine, interval=0.01)
        self.loader.initial_load()

    def tearDown(self):
        self.loader.stop()
        self.directory.cleanup()

    def write(self, relative_path, content, mtime=None):
        path = os.path.join(self.root, relative_path)
        with open(path, 'w') as f:
            f.write(content)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def render(self, name):
        return asyncio.run(self.engine.render(name))

    def bump_mtime(self, path):
        future = os.path.getmtime(path) + 1
        os.utime(path, (future, future))
        return future

    def test_initial_load_compiles_every_template_once(self):
        self.assertEqual(3, CountingCompiler.compile_calls)
        self.assertEqual('base PARTPAGE', self.render('page.html'))

    def test_no_changes_triggers_no_recompilation(self):
        CountingCompiler.compile_calls = 0
        changed = self.loader.check_for_modified_templates()
        self.assertEqual([], changed)
        self.assertEqual(0, CountingCompiler.compile_calls)

    def test_changed_leaf_only_recompiles_dependents_not_whole_project(self):
        self.write('unrelated.html', 'STANDALONE')
        self.loader.check_for_modified_templates()
        CountingCompiler.compile_calls = 0

        time.sleep(0.01)
        self.write('sub/part.html', 'PART2')
        changed = self.loader.check_for_modified_templates()
        self.assertEqual(1, len(changed))
        # part + base + page, but not unrelated.html
        self.assertEqual(3, CountingCompiler.compile_calls)
        self.assertEqual('base PART2PAGE', self.render('page.html'))
        self.assertEqual('STANDALONE', self.render('unrelated.html'))

    def test_new_file_is_picked_up_without_full_rebuild(self):
        CountingCompiler.compile_calls = 0
        self.write('fresh.html', 'FRESH')
        self.loader.check_for_modified_templates()
        self.assertEqual(1, CountingCompiler.compile_calls)
        self.assertEqual('FRESH', self.render('fresh.html'))

    def test_bad_edit_rolls_back_and_keeps_old_version_running(self):
        part_path = os.path.join(self.root, 'sub', 'part.html')
        old_hash = self.loader.path_index[part_path].hash
        time.sleep(0.01)
        self.write('sub/part.html', '{% totally invalid tag %}')
        with self.assertRaises(Exception):
            self.loader.check_for_modified_templates()
        self.assertEqual('base PARTPAGE', self.render('page.html'))
        part_paths = [p for p in self.loader.path_index if p.endswith('part.html')]
        self.assertEqual(1, len(part_paths))
        self.assertEqual(old_hash, self.loader.path_index[part_paths[0]].hash)

    def test_bad_edit_does_not_poison_mtime_cache(self):
        time.sleep(0.01)
        self.write('sub/part.html', '{% totally invalid tag %}')
        with self.assertRaises(Exception):
            self.loader.check_for_modified_templates()
        # Fixing the file must still be detected on the next poll.
        time.sleep(0.01)
        self.write('sub/part.html', 'PART_FIXED')
        changed = self.loader.check_for_modified_templates()
        self.assertEqual(1, len(changed))
        self.assertEqual('base PART_FIXEDPAGE', self.render('page.html'))

    def test_deleted_template_and_dependents_are_purged(self):
        os.remove(os.path.join(self.root, 'sub', 'part.html'))
        self.loader.check_for_modified_templates()
        remaining = '\n'.join(self.loader.path_index.keys())
        self.assertNotIn('part.html', remaining)
        self.assertNotIn('base.html', remaining)
        self.assertNotIn('page.html', remaining)

    def test_deleted_file_recreates_cleanly(self):
        part_path = os.path.join(self.root, 'sub', 'part.html')
        os.remove(part_path)
        self.loader.check_for_modified_templates()
        self.write('sub/part.html', 'PART3')
        self.write('base.html', 'base {% include "sub/part.html" %}')
        self.write('page.html', '{% include "base.html" %}PAGE')
        self.loader.check_for_modified_templates()
        self.assertEqual('base PART3PAGE', self.render('page.html'))

    def test_reload_returns_transitive_dependents(self):
        part_path = os.path.join(self.root, 'sub', 'part.html')
        template = self.loader.path_index[part_path]
        reload_paths = self.loader._collect_reload_paths([(self.root, part_path)])
        paths = '\n'.join(path for _, path in reload_paths)
        self.assertIn('base.html', paths)
        self.assertIn('page.html', paths)
        self.assertIsNotNone(template)


class WatcherThreadSuite(AsyncTestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engine = TemplateEngine()
        self.loader = TemplateLoader([self.directory.name], self.engine, interval=0.02)
        self.loader.daemon = True

    def tearDown(self):
        self.loader.stop()
        self.loader.join(timeout=2)
        self.directory.cleanup()

    def test_bad_edit_does_not_kill_watcher_thread(self):
        path = os.path.join(self.directory.name, 'a.html')
        with open(path, 'w') as f:
            f.write('A')
        self.loader.initial_load()
        self.loader.start()
        time.sleep(0.05)
        with open(path, 'w') as f:
            f.write('{% broken')
        time.sleep(0.1)
        self.assertTrue(self.loader.is_alive())

    def test_stop_terminates_thread(self):
        self.loader.start()
        time.sleep(0.05)
        self.loader.stop()
        self.loader.join(timeout=2)
        self.assertFalse(self.loader.is_alive())


class AddToEngineSuite(AsyncTestCase):

    def test_template_source_is_set_to_file_path(self):
        directory = tempfile.TemporaryDirectory()
        try:
            engine = TemplateEngine()
            loader = TemplateLoader([directory.name], engine)
            path = os.path.join(directory.name, 'x.html')
            with open(path, 'w') as f:
                f.write('X')
            loader.add_to_engine(directory.name, path)
            self.assertEqual(path, loader.path_index[path].source)
        finally:
            directory.cleanup()
