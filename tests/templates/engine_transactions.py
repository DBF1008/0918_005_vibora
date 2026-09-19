from ._bootstrap import AsyncTestCase, run_async
from vibora.templates import Template, TemplateEngine
from vibora.templates.exceptions import ConflictingNames, TemplateNotFound
from vibora.templates.compilers.python import PythonTemplateCompiler
from vibora.templates.template import ParsedTemplate


class AddTemplateAtomicSuite(AsyncTestCase):

    def setUp(self):
        self.engine = TemplateEngine()
        self.engine.add_template(Template('hello'), ['base'])

    def test_single_conflicting_name_raises_and_keeps_state(self):
        before = dict(self.engine.templates)
        with self.assertRaises(ConflictingNames):
            self.engine.add_template(Template('world'), ['base'])
        self.assertEqual(set(before), set(self.engine.templates))

    def test_one_conflict_among_many_names_is_fully_rolled_back(self):
        with self.assertRaises(ConflictingNames):
            self.engine.add_template(Template('world'), ['new_one', 'base', 'new_two'])
        self.assertNotIn('new_one', self.engine.templates)
        self.assertNotIn('new_two', self.engine.templates)

    def test_unique_names_are_registered(self):
        template = self.engine.add_template(Template('world'), ['a', 'b'])
        self.assertIs(self.engine.templates['a'], template)
        self.assertIs(self.engine.templates['b'], template)

    async def test_existing_template_still_renders_after_failed_add(self):
        self.engine.compile_templates()
        try:
            self.engine.add_template(Template('x'), ['base'])
        except ConflictingNames:
            pass
        self.assertEqual('hello', await self.engine.render('base'))


class BatchAddTemplatesSuite(AsyncTestCase):

    def setUp(self):
        self.engine = TemplateEngine()
        self.engine.add_template(Template('base'), ['base'])

    def test_batch_success_commits_everything(self):
        templates = self.engine.add_templates([
            (Template('one'), ['one']),
            (Template('two'), ['two', 'two_alias'])
        ])
        self.assertEqual(2, len(templates))
        self.assertIn('one', self.engine.templates)
        self.assertIn('two_alias', self.engine.templates)

    def test_batch_conflict_at_last_template_rolls_all_back(self):
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates([
                (Template('forty_eight'), [f't{i}']) for i in range(48)
            ] + [(Template('dupe'), ['base'])])
        for i in range(48):
            self.assertNotIn(f't{i}', self.engine.templates)
        self.assertIn('base', self.engine.templates)

    def test_batch_conflict_inside_batch_rolls_all_back(self):
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates([
                (Template('a'), ['dup']),
                (Template('b'), ['dup'])
            ])
        self.assertNotIn('dup', self.engine.templates)

    def test_batch_invalid_syntax_rolls_back_previous_entries(self):
        from vibora.templates.exceptions import InvalidTag
        with self.assertRaises(InvalidTag):
            self.engine.add_templates([
                (Template('good'), ['good']),
                (Template('{% broken syntax %}'), ['bad'])
            ])
        self.assertNotIn('good', self.engine.templates)
        self.assertNotIn('bad', self.engine.templates)


class TransactionSuite(AsyncTestCase):

    def setUp(self):
        self.engine = TemplateEngine()

    def test_context_manager_rolls_back_on_exception(self):
        self.engine.add_template(Template('orig'), ['keep'])
        with self.assertRaises(RuntimeError):
            with self.engine.transaction():
                self.engine.add_template(Template('temp'), ['temp'])
                raise RuntimeError('boom')
        self.assertNotIn('temp', self.engine.templates)
        self.assertIn('keep', self.engine.templates)

    def test_context_manager_commits_on_success(self):
        with self.engine.transaction():
            self.engine.add_template(Template('ok'), ['ok'])
        self.assertIn('ok', self.engine.templates)

    async def test_failed_compile_after_prepare_restores_parent_ast(self):
        self.engine.add_template(
            Template('{% block content %}parent{% endblock %}'), ['parent']
        )
        self.engine.add_template(
            Template('{% extends "parent" %}{% block content %}child{% endblock %}'),
            ['child']
        )
        self.engine.compile_templates()
        parent = self.engine.templates['parent']
        original_children = list(parent.ast.children)
        with self.assertRaises(Exception):
            with self.engine.transaction():
                self.engine.add_template(Template('{% extends "missing" %}'), ['broken'])
                self.engine.prepare_template(self.engine.templates['broken'])
        self.assertEqual(len(original_children), len(parent.ast.children))
        self.assertNotIn('broken', self.engine.templates)

    async def test_render_still_works_after_rolled_back_transaction(self):
        self.engine.add_template(Template('{{ value }}'), ['t'])
        self.engine.compile_templates()
        with self.assertRaises(ConflictingNames):
            with self.engine.transaction():
                self.engine.add_template(Template('x'), ['t'])
        self.assertEqual('42', await self.engine.render('t', value=42))


class CompileAffectedSuite(AsyncTestCase):

    def setUp(self):
        self.engine = TemplateEngine()

    async def test_compile_affected_skips_unchanged_templates(self):
        class CountingCompiler(PythonTemplateCompiler):
            calls = 0
            def compile(self, template, verbose=False):
                CountingCompiler.calls += 1
                return super().compile(template, verbose=verbose)

        engine = TemplateEngine(compiler=CountingCompiler())
        engine.add_template(Template('one'), ['one'])
        engine.add_template(Template('two'), ['two'])
        engine.compile_templates()

        # No hash reported as changed: nothing should be recompiled.
        CountingCompiler.calls = 0
        engine.compile_affected(set())
        self.assertEqual(0, CountingCompiler.calls)
        self.assertEqual('one', await engine.render('one'))
        self.assertEqual('two', await engine.render('two'))

        # A freshly registered template is compiled when explicitly targeted,
        # while already compiled templates are left untouched.
        engine.add_template(Template('three'), ['three'])
        engine.compile_affected({engine.templates['three'].hash})
        self.assertEqual(1, CountingCompiler.calls)
        self.assertEqual('three', await engine.render('three'))

    def test_get_dependents_returns_transitive_closure(self):
        self.engine.add_template(Template('a'), ['a'])
        self.engine.add_template(Template('{% include "a" %}'), ['b'])
        self.engine.add_template(Template('{% include "b" %}'), ['c'])
        self.engine.compile_templates()
        a_hash = self.engine.templates['a'].hash
        dependents = self.engine.get_dependents({a_hash})
        self.assertEqual(
            {self.engine.templates[name].hash for name in ('a', 'b', 'c')},
            dependents
        )

    async def test_compile_affected_rolls_back_on_compile_error(self):
        class ExplodingCompiler(PythonTemplateCompiler):
            def compile(self, template, verbose=False):
                raise RuntimeError('compiler exploded')
        self.engine.add_template(Template('good'), ['good'])
        self.engine.compile_templates()
        snapshot = set(self.engine.compiled_templates)
        self.engine.compiler = ExplodingCompiler()
        self.engine.add_template(Template('new'), ['new'])
        with self.assertRaises(RuntimeError):
            self.engine.compile_affected({self.engine.templates['new'].hash})
        self.assertEqual(snapshot, set(self.engine.compiled_templates))
        self.assertEqual('good', await self.engine.render('good'))


class RemoveTemplateSuite(AsyncTestCase):

    async def test_remove_template_clears_names_compiled_and_cache(self):
        engine = TemplateEngine()
        engine.add_template(Template('bye'), ['bye', 'bye_alias'])
        engine.compile_templates()
        template = engine.templates['bye']
        engine.remove_template(template)
        self.assertNotIn('bye', engine.templates)
        self.assertNotIn('bye_alias', engine.templates)
        self.assertNotIn(template.hash, engine.compiled_templates)
        with self.assertRaises(TemplateNotFound):
            engine.get_template('bye')
