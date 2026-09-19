from vibora.templates import Template, TemplateParser
from vibora.templates.compilers.cython import CythonTemplateCompiler
from vibora.templates.utils import SourceMap
from vibora.tests import TestSuite


class CythonSourceMapSuite(TestSuite):

    def setUp(self):
        self.parser = TemplateParser()
        self.compiler = CythonTemplateCompiler()

    def parse(self, content, filename=None):
        template = Template(content)
        template.filename = filename
        return self.parser.parse(template)

    def test_generated_code_contains_template_header(self):
        parsed = self.parse('hello {{ name }}', filename='templates/index.html')
        self.compiler.consume(parsed)
        self.assertIn('# template: templates/index.html', self.compiler.content)
        self.assertIn(f'# template_hash: {parsed.hash}', self.compiler.content)

    def test_generated_code_contains_source_comments(self):
        parsed = self.parse('hello {{ name }}', filename='index.html')
        self.compiler.consume(parsed)
        self.assertIn('# {{ name }}', self.compiler.content)

    def test_generated_code_without_filename(self):
        parsed = self.parse('hello')
        self.compiler.consume(parsed)
        self.assertIn('# template: <unknown>', self.compiler.content)

    def test_source_map_records_template_line_numbers(self):
        parsed = self.parse('first\nsecond {{ name }}\nthird', filename='index.html')
        self.compiler.consume(parsed)
        entries = [e for e in self.compiler.source_map.entries if e[2] == '{{ name }}']
        self.assertEqual(1, len(entries))
        self.assertEqual(2, entries[0][1])
        self.assertEqual('index.html', self.compiler.source_map.filename)

    def test_source_map_generated_lines_are_valid(self):
        parsed = self.parse('{% for x in [1] %}{{ x }}{% endfor %}', filename='loop.html')
        self.compiler.consume(parsed)
        total_lines = len(self.compiler.content.splitlines())
        for generated_line, template_line, source in self.compiler.source_map.entries:
            self.assertTrue(1 <= generated_line <= total_lines + 1)
            self.assertTrue(template_line >= 1)
            self.assertTrue(source)

    def test_clean_resets_source_map(self):
        parsed = self.parse('{{ a }}')
        self.compiler.consume(parsed)
        self.assertTrue(self.compiler.source_map.entries)
        self.compiler.clean()
        self.assertEqual([], self.compiler.source_map.entries)
        self.assertIsNone(self.compiler.source_map.filename)


class SourceMapSuite(TestSuite):

    def test_lookup_returns_nearest_previous_entry(self):
        source_map = SourceMap(filename='tpl.html')
        source_map.add(5, 1, 'one')
        source_map.add(10, 2, 'two')
        self.assertEqual((1, 'one'), source_map.lookup(5))
        self.assertEqual((1, 'one'), source_map.lookup(9))
        self.assertEqual((2, 'two'), source_map.lookup(10))
        self.assertEqual((2, 'two'), source_map.lookup(99))
        self.assertEqual((None, None), source_map.lookup(4))

    def test_translate_frame_uses_template_location(self):
        source_map = SourceMap(filename='index.html')
        source_map.add(3, 7, '{{ x.missing }}')
        location, line_number, source = source_map.translate_frame('vt_abc.pyx', 4)
        self.assertEqual('index.html', location)
        self.assertEqual(7, line_number)
        self.assertEqual('{{ x.missing }}', source)

    def test_translate_frame_without_mapping_keeps_original(self):
        source_map = SourceMap(filename='index.html')
        location, line_number, source = source_map.translate_frame('other.py', 10)
        self.assertEqual('other.py', location)
        self.assertEqual(10, line_number)
        self.assertIsNone(source)

    def test_annotate_exception_maps_generated_lines_to_template(self):
        source_map = SourceMap(filename='index.html')
        source_map.add(2, 7, '{{ x.missing }}')
        # Simulates generated code failing at generated line 2.
        code = compile('def render():\n    raise ValueError("boom")\n', 'vt_abc.pyx', 'exec')
        namespace = {}
        exec(code, namespace)
        try:
            namespace['render']()
            self.fail('ValueError not raised')
        except ValueError as error:
            frames = source_map.annotate_exception(error)
        location, line_number, function_name, source = frames[-1]
        self.assertEqual('index.html', location)
        self.assertEqual(7, line_number)
        self.assertEqual('render', function_name)
        self.assertEqual('{{ x.missing }}', source)

    def test_format_exception_contains_template_location(self):
        source_map = SourceMap(filename='index.html')
        source_map.add(2, 7, '{{ x.missing }}')
        code = compile('def render():\n    raise ValueError("boom")\n', 'vt_abc.pyx', 'exec')
        namespace = {}
        exec(code, namespace)
        try:
            namespace['render']()
            self.fail('ValueError not raised')
        except ValueError as error:
            formatted = source_map.format_exception(error)
        self.assertIn('File "index.html", line 7', formatted)
        self.assertIn('{{ x.missing }}', formatted)
        self.assertIn('ValueError: boom', formatted)


class ParserLineNumberSuite(TestSuite):

    def test_parser_records_node_line_numbers(self):
        parser = TemplateParser()
        parsed = parser.parse(Template('one\ntwo {{ x }}\nthree {% for y in [1] %}{% endfor %}'))
        eval_node = parsed.ast.children[1]
        for_node = parsed.ast.children[3]
        self.assertEqual(2, eval_node.line_number)
        self.assertEqual(3, for_node.line_number)

    def test_parser_propagates_filename(self):
        parser = TemplateParser()
        template = Template('content')
        template.filename = 'page.html'
        parsed = parser.parse(template)
        self.assertEqual('page.html', parsed.filename)
