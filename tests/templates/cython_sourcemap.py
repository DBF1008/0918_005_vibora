import importlib.util
import json
import os
import tempfile

from ._bootstrap import AsyncTestCase
from vibora.templates import Template, TemplateEngine
from vibora.templates.compilers.cython import (
    CythonTemplateCompiler, RenderTracebackTranslator, render_with_source_map,
    SOURCE_MARKER_PREFIX
)
from vibora.templates.exceptions import TemplateRenderError


def prepare_compiler(content, source='index.html'):
    engine = TemplateEngine()
    engine.add_template(Template(content, source=source), ['t'])
    engine.prepare_template(engine.templates['t'])
    compiler = CythonTemplateCompiler()
    compiler.consume(engine.templates['t'])
    return compiler, engine.templates['t']


class SourceMapEmissionSuite(AsyncTestCase):

    def test_markers_reference_original_source_and_line(self):
        content = 'line one\n{{ value }}\nline three'
        compiler, _ = prepare_compiler(content, source='/templates/index.html')
        self.assertIn(SOURCE_MARKER_PREFIX, compiler.content)
        self.assertIn('"/templates/index.html"', compiler.content)
        eval_markers = [line for line in compiler.content.splitlines()
                        if line.strip().startswith(SOURCE_MARKER_PREFIX) and '"line": 2' in line]
        self.assertTrue(eval_markers)

    def test_source_map_maps_cython_lines_to_template_lines(self):
        content = 'a\n{{ x }}\nb\n{{ y }}'
        compiler, _ = prepare_compiler(content, source='t.html')
        self.assertTrue(compiler.source_map)
        template_lines = set(compiler.source_map.values())
        self.assertIn(2, template_lines)
        self.assertIn(4, template_lines)

    def test_for_loop_body_is_mapped_to_template_line(self):
        content = '{% for x in [1, 2] %}\n{{ x }}\n{% endfor %}'
        compiler, _ = prepare_compiler(content, source='loop.html')
        self.assertIn(2, set(compiler.source_map.values()))

    def test_macros_shift_does_not_corrupt_map(self):
        content = '{% macro render(x) %}{{ x }}{% endmacro %}{{ render(1) }}'
        compiler, template = prepare_compiler(content, source='macro.html')
        helper_source = '\n\n'.join(compiler.functions) + '\n\n'
        shifted = compiler.build_source_map(helper_source.count('\n'))
        self.assertEqual(
            set(compiler.source_map.values()), set(shifted.values())
        )
        self.assertTrue(all(k >= helper_source.count('\n') for k in shifted))

    def test_generated_code_is_valid_cpython_without_async_syntax(self):
        # The sync Cython entry point must not contain await/async for loops.
        content = '{% for x in [1] %}{{ x }}{% endfor %}'
        compiler, _ = prepare_compiler(content)
        self.assertNotIn('await ', compiler.content)
        self.assertNotIn('async for', compiler.content)


class TranslatorSuite(AsyncTestCase):

    def test_translate_line_uses_nearest_previous_marker(self):
        translator = RenderTracebackTranslator(
            source_map={1: 1, 5: 3, 10: 7},
            template_code='',
            template_source='a.html'
        )
        self.assertEqual(1, translator.translate_line(1))
        self.assertEqual(1, translator.translate_line(4))
        self.assertEqual(3, translator.translate_line(5))
        self.assertEqual(3, translator.translate_line(9))
        self.assertEqual(7, translator.translate_line(100))

    def test_template_line_text_is_extracted(self):
        translator = RenderTracebackTranslator(
            source_map={1: 2},
            template_code='first\n{{ boom }}\nthird',
            template_source='a.html'
        )
        self.assertEqual('{{ boom }}', translator.get_template_line_text(2))

    def test_render_exception_carries_original_location(self):
        directory = tempfile.TemporaryDirectory()
        path = os.path.join(directory.name, 'compiled_templates_fake.py')
        with open(path, 'w') as f:
            f.write('\n' * 8 + 'def render(ctx):\n    raise ValueError("boom")\n')
        try:
            spec = importlib.util.spec_from_file_location('compiled_templates', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            content = 'header\n{{ x.missing }}\nfooter'
            translator = RenderTracebackTranslator(
                source_map={7: 1, 9: 2},
                template_code=content,
                template_source='/templates/a.html',
                template_name='a.html',
                compiled_filename='compiled_templates.so'
            )
            wrapped = render_with_source_map(module.render, translator)
            try:
                wrapped({})
                self.fail('expected TemplateRenderError')
            except TemplateRenderError as error:
                self.assertEqual('a.html', error.template_name)
                self.assertEqual('/templates/a.html', error.template_source)
                self.assertEqual(2, error.template_lineno)
                self.assertEqual('{{ x.missing }}', error.template_line)
                self.assertIsInstance(error.original_exception, ValueError)
                payload = json.loads(str(error))
                self.assertEqual('/templates/a.html', payload['template_source'])
                self.assertEqual(2, payload['template_lineno'])
        finally:
            directory.cleanup()

    def test_wrapper_passes_through_success_and_keeps_metadata(self):
        translator = RenderTracebackTranslator(
            source_map={1: 1}, template_code='', template_source='a.html'
        )
        wrapped = render_with_source_map(lambda ctx: 'ok', translator)
        self.assertEqual('ok', wrapped({}))
        self.assertEqual(translator.render_exception, wrapped.render_exception)
        self.assertEqual(translator.source_map, wrapped.source_map)

    def test_render_error_is_not_double_wrapped(self):
        translator = RenderTracebackTranslator(
            source_map={1: 1}, template_code='', template_source='a.html'
        )
        original = TemplateRenderError(None, '', KeyError('x'), 'a.html')

        def raising(ctx):
            raise original

        wrapped = render_with_source_map(raising, translator)
        try:
            wrapped({})
        except TemplateRenderError as error:
            self.assertIs(original, error)


class ParserLocationSuite(AsyncTestCase):

    def test_parser_stamps_node_line_and_source(self):
        parsed = TemplateEngine().template_parser.parse(
            Template('hello\n{% if x %}\n{{ x }}\n{% endif %}', source='page.html')
        )
        nodes = list(parsed.flat_view(parsed.ast))
        if_node = next(node for node in nodes if node.__class__.__name__ == 'IfNode')
        eval_node = next(node for node in nodes if node.__class__.__name__ == 'EvalNode')
        self.assertEqual(2, if_node.line)
        self.assertEqual(3, eval_node.line)
        self.assertEqual('page.html', if_node.source)
        self.assertEqual('page.html', eval_node.source)

    def test_default_template_source_is_placeholder(self):
        template = Template('{{ x }}')
        self.assertEqual('<template>', template.source)
