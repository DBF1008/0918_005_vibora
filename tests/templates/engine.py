from vibora.templates import Template, TemplateEngine
from vibora.templates.exceptions import ConflictingNames, InvalidTag
from vibora.tests import TestSuite


class TransactionalLoadSuite(TestSuite):

    def setUp(self):
        self.engine = TemplateEngine()

    def test_add_template_conflict_does_not_mutate_state(self):
        self.engine.add_template(Template('hello'), names=['index'])
        with self.assertRaises(ConflictingNames):
            self.engine.add_template(Template('other'), names=['index'])
        self.assertEqual(['index'], list(self.engine.templates.keys()))
        self.assertEqual('hello', self.engine.templates['index'].content)

    def test_add_templates_batch_success(self):
        batch = [(Template(f'content {i}'), [f'tpl_{i}']) for i in range(50)]
        parsed = self.engine.add_templates(batch)
        self.assertEqual(50, len(parsed))
        self.assertEqual(50, len(self.engine.templates))
        for index in range(50):
            self.assertEqual(f'content {index}', self.engine.templates[f'tpl_{index}'].content)

    def test_add_templates_rollback_on_conflict(self):
        self.engine.add_template(Template('original'), names=['taken'])
        # Simulates loading 50 templates where the 49th one conflicts.
        batch = [(Template(f'content {i}'), [f'tpl_{i}']) for i in range(48)]
        batch.append((Template('conflicting'), ['taken']))
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates(batch)
        # All-or-nothing: none of the 48 valid templates may be registered.
        self.assertEqual(['taken'], list(self.engine.templates.keys()))
        self.assertEqual('original', self.engine.templates['taken'].content)

    def test_add_templates_rollback_on_parse_error(self):
        batch = [
            (Template('valid content'), ['valid']),
            (Template('{% invalid_tag %}'), ['broken']),
        ]
        with self.assertRaises(InvalidTag):
            self.engine.add_templates(batch)
        self.assertEqual({}, self.engine.templates)

    def test_add_templates_rollback_on_internal_conflict(self):
        batch = [
            (Template('a'), ['dup']),
            (Template('b'), ['dup']),
        ]
        with self.assertRaises(ConflictingNames):
            self.engine.add_templates(batch)
        self.assertEqual({}, self.engine.templates)

    def test_add_templates_returns_parsed_templates_in_order(self):
        batch = [(Template('one'), ['1']), (Template('two'), ['2'])]
        parsed = self.engine.add_templates(batch)
        self.assertEqual(['one', 'two'], [t.content for t in parsed])

    async def test_compile_template_single(self):
        self.engine.add_templates([
            (Template('hello {{ who }}'), ['one']),
            (Template('other'), ['two']),
        ])
        self.engine.compile_template(self.engine.templates['one'])
        self.assertEqual('hello world', await self.engine.render('one', who='world'))
        # The second template was not compiled and must not be renderable.
        with self.assertRaises(Exception):
            await self.engine.render('two')

    async def test_compile_many_compiles_only_given_templates(self):
        self.engine.add_templates([
            (Template('a'), ['a']),
            (Template('b'), ['b']),
            (Template('c'), ['c']),
        ])
        targets = [self.engine.templates['a'], self.engine.templates['c']]
        self.engine.compile_many(targets)
        self.assertEqual('a', await self.engine.render('a'))
        self.assertEqual('c', await self.engine.render('c'))
        with self.assertRaises(Exception):
            await self.engine.render('b')
