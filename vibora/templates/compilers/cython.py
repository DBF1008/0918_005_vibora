import hashlib
import importlib.util
import bisect
import json
import os
import tempfile
import time
import datetime
from setuptools import Extension, setup
from ..compilers.base import TemplateCompiler
from ..exceptions import TemplateRenderError
from ..utils import find_template_binary, CompilerFlavor, TemplateMeta, get_architecture_signature, CompilationResult


# TODO: Remove 'render' hardcoded name.

# Marker comments emitted into the generated Cython source. They are ignored
# by the Cython parser but allow us to translate runtime tracebacks back to
# the original template file/line once a compiled template is running.
SOURCE_MARKER_PREFIX = '# >>> vibora-source:'


class RenderTracebackTranslator:
    """
    Maps compiled Cython frames back to the original template location.
    """

    COMPILED_MODULE_PREFIX = 'compiled_templates'

    def __init__(self, source_map: dict, template_code: str, template_source: str,
                 template_name: str = None, compiled_filename: str = None):
        self.sorted_lines = sorted(int(key) for key in source_map.keys())
        self.source_map = {int(key): value for key, value in source_map.items()}
        self.template_code = template_code
        self.template_source = template_source
        self.template_name = template_name or template_source
        self.compiled_filename = compiled_filename

    def translate_line(self, cython_line: int) -> int:
        index = bisect.bisect_right(self.sorted_lines, cython_line) - 1
        if index < 0:
            return 1
        return self.source_map[self.sorted_lines[index]]

    @staticmethod
    def find_compiled_frame(error: BaseException, compiled_filename: str):
        traceback = error.__traceback__
        fallback = None
        while traceback is not None:
            frame_filename = traceback.tb_frame.f_code.co_filename
            basename = os.path.basename(frame_filename)
            if compiled_filename and basename == os.path.basename(compiled_filename):
                return traceback
            if fallback is None and (
                basename.startswith(RenderTracebackTranslator.COMPILED_MODULE_PREFIX)
                or 'compiled_templates' in frame_filename
            ):
                fallback = traceback
            traceback = traceback.tb_next
        return fallback

    def get_template_line_text(self, template_line: int) -> str:
        lines = self.template_code.splitlines()
        if 0 < template_line <= len(lines):
            return lines[template_line - 1].strip()
        return ''

    def render_exception(self, error: Exception):
        template_line = 1
        frame = self.find_compiled_frame(error, self.compiled_filename)
        if frame is not None:
            template_line = self.translate_line(frame.tb_lineno)
        template_line_text = self.get_template_line_text(template_line)
        return TemplateRenderError(
            template=None,
            template_line=template_line_text,
            exception=error,
            template_name=self.template_name,
            template_source=self.template_source,
            template_lineno=template_line
        )


def render_with_source_map(render_function, translator: RenderTracebackTranslator):
    def wrapper(*args, **kwargs):
        try:
            return render_function(*args, **kwargs)
        except TemplateRenderError:
            raise
        except Exception as error:
            raise translator.render_exception(error) from error
    wrapper.render_exception = translator.render_exception
    wrapper.source_map = translator.source_map
    wrapper.__name__ = getattr(render_function, '__name__', 'render')
    return wrapper


class CythonTemplateCompiler(TemplateCompiler):

    NAME = 'cython'
    VERSION = '0.0.1'
    EXTENSION_NAME = 'compiled_templates'
    supports_async_values = False
    supports_async_iteration = False

    def __init__(self, flavor=CompilerFlavor.TEMPLATE, temporary_dir: str=None):
        super().__init__()
        self.content = ''
        self.current_scope = list()
        self.accumulated_text = ''
        self.content_var = '__content__'
        self.context_var = '__context__'
        self.functions = []
        self.flavor = flavor
        self.temporary_dir = temporary_dir or tempfile.gettempdir()
        self.verbose = False
        self.current_source = None
        self.source_map = {}
        self.template_source = '<template>'

    def clean(self):
        self._indentation = 0
        self.content = ''
        self.current_scope = list()
        self.accumulated_text = ''
        self.functions = []
        self.flavor = CompilerFlavor.TEMPLATE
        self.current_source = None
        self.source_map = {}
        self.template_source = '<template>'

    def add_text(self, content: str):
        content = content.replace("\n", "\\n")
        content = content.replace(r'"', r'\"')
        self.accumulated_text += content

    def flush_text(self):
        text = self.accumulated_text
        self.accumulated_text = ''
        stm = f'{self.content_var}.append("{text}")'
        self.add_statement(stm)

    def add_eval(self, statement: str):
        self.add_statement(f'{self.content_var}.append(str({statement}))')

    def add_statement(self, content: str):
        if self.accumulated_text:
            self.flush_text()
        self.stamp_source()
        new_content = (' ' * self._indentation) + content.strip() + '\n'
        self.content += new_content

    def add_comment(self, content: str):
        # Tag raw forms are already represented by source markers, comments
        # are kept out of the Cython output to preserve line alignment.
        pass

    def on_node(self, node):
        source = getattr(node, 'source', None)
        line = getattr(node, 'line', 0) or 0
        if source and line:
            self.current_source = (source, line)

    def stamp_source(self):
        if not self.current_source:
            return
        source, line = self.current_source
        cython_line = self.content.count('\n') + 1
        marker = SOURCE_MARKER_PREFIX + json.dumps({'source': source, 'line': line}) + '\n'
        self.content += marker
        self.source_map[cython_line] = line

    def consume(self, template):
        self.template_source = getattr(template, 'source', None) or '<template>'
        self.current_source = (self.template_source, 1)
        self.add_statement(f'cpdef str render(dict {self.context_var}):')
        self._indentation += 4
        self.add_statement(f"cdef list {self.content_var} = []")
        template.ast.compile(self)
        self.add_statement(f'return "".join({self.content_var})')
        self._indentation -= 4

    def build_source_map(self, prefix_lines: int) -> dict:
        return {line + prefix_lines: template_line
                for line, template_line in self.source_map.items()}

    def create_translator(self, template, source_map: dict, template_name: str=None):
        return RenderTracebackTranslator(
            source_map=source_map,
            template_code=template.content,
            template_source=getattr(template, 'source', None) or '<template>',
            template_name=template_name,
            compiled_filename=self.EXTENSION_NAME + '.so'
        )

    def create_new_macro(self, definition: str):
        new_compiler = self.__class__(flavor=CompilerFlavor.MACRO)
        new_compiler.add_statement('def ' + definition + ':')
        new_compiler.indent()
        new_compiler.add_statement(f"{self.content_var} = []")
        return new_compiler

    @classmethod
    def load_compiled_template(cls, meta: TemplateMeta, content: bytes):
        f = tempfile.NamedTemporaryFile(mode='wb', suffix='.so')
        f.file.write(content)
        f.file.flush()
        spec = importlib.util.spec_from_file_location(cls.EXTENSION_NAME, f.name)
        compiled_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compiled_module)
        render_function = getattr(compiled_module, meta.entry_point)
        source_map = getattr(meta, 'source_map', None)
        if source_map:
            source_map = {int(key): value for key, value in source_map.items()}
            translator = RenderTracebackTranslator(
                source_map=source_map,
                template_code='',
                template_source=getattr(meta, 'template_source', None) or '<template>',
                template_name=getattr(meta, 'template_source', None),
                compiled_filename=cls.EXTENSION_NAME + '.so'
            )
            return render_with_source_map(render_function, translator)
        return render_function

    def compile(self, template, verbose: bool=False) -> CompilationResult:
        """

        :param verbose:
        :param template:
        :return:
        """
        # Tracking compile times
        started_at = time.time()

        # Temporary directory for this compilation.
        working_dir = tempfile.TemporaryDirectory(dir=self.temporary_dir)

        # Generating .pyx files.
        self.consume(template)
        # print(template.hash)
        # print(self.content)
        template_hash = hashlib.md5(self.content.encode()).hexdigest()
        temp_path = os.path.join(working_dir.name, 'vt_' + template_hash + '.pyx')
        helper_source = ''
        for helper_function in self.functions:
            helper_source += helper_function + '\n\n'
        with open(temp_path, 'w') as f:
            f.write(helper_source)
            f.write(self.content)

        # Source map keys are line numbers in the final .pyx file, so the
        # helper functions/macros prefixed above must shift the recorded map.
        source_map = self.build_source_map(helper_source.count('\n'))

        # Building optimized binaries.
        ext = Extension(self.EXTENSION_NAME, [temp_path], extra_compile_args=['-O3'], include_dirs=['.'])
        build_path = os.path.join(working_dir.name, template_hash)
        trash_dir = os.path.join(working_dir.name, 'trash')
        args = ['build_ext', '-b', build_path, '-t', trash_dir]
        if not self.verbose:
            args = ['-q'] + args
        setup(ext_modules=[ext], script_args=args)

        # Loading modules.
        compiled_path = find_template_binary(build_path)
        spec = importlib.util.spec_from_file_location(self.EXTENSION_NAME, compiled_path)
        compiled_template = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compiled_template)

        # Wrapping the raw render function so any runtime exception gets
        # translated back to the original template file and line number.
        translator = self.create_translator(template, source_map)
        render_function = render_with_source_map(
            compiled_template.render, translator
        )

        # Generating meta data about this compilation so we can correctly
        # cache and load these templates later.
        meta = TemplateMeta(
            entry_point='render',
            version=self.VERSION,
            compiler=self.NAME,
            template_hash=template.hash,
            created_at=datetime.datetime.now().isoformat(),
            architecture=get_architecture_signature(),
            compilation_time=round(time.time() - started_at, 2),
            dependencies=template.dependencies
        )
        meta.source_map = source_map
        meta.template_source = getattr(template, 'source', None) or '<template>'

        # Compilation result contains the meta data and the render function loaded at runtime.
        compilation = CompilationResult(
            template=template,
            meta=meta, render_function=render_function, code=open(compiled_path, 'rb').read()
        )
        compilation.source_map = source_map
        compilation.pyx_source = helper_source + self.content

        # Clearing state
        self.clean()

        # Binding the render function
        return compilation

    @classmethod
    def generate_template_name(cls, hash_: str):
        return hash_ + '.so'
