import os
import json
import platform
import typing
from .exceptions import FailedToCompileTemplate


def find_template_binary(path: str):
    name = os.listdir(path)[-1]
    if not name:
        raise FailedToCompileTemplate(path)
    return os.path.join(path, name)


class CompilerFlavor:
    TEMPLATE = 1
    MACRO = 2


def get_scope_by_args(function_def: str):
    open_at = function_def.find('(') + 1
    close_at = function_def.rfind(')')
    args = function_def[open_at:close_at]
    scope = []
    for value in args.split(','):
        if value.find('='):
            scope.append(value.split('=')[0].strip())
        else:
            scope.append(value.strip())
    return scope


def get_function_name(definition: str):
    return definition[:definition.find('(')].strip()


class TemplateMeta:
    def __init__(self, entry_point: str, version: str, template_hash: str,
                 created_at: str, compiler: str, architecture: str, compilation_time: float,
                 dependencies: list=None):
        self.entry_point = entry_point
        self.version = version
        self.template_hash = template_hash
        self.created_at = created_at
        self.compiler = compiler
        self.architecture = architecture
        self.compilation_time = compilation_time
        self.dependencies = dependencies or []

    @classmethod
    def load_from_path(cls, path: str):
        with open(path) as f:
            return TemplateMeta(**json.loads(f.read()))

    def store(self, path: str):
        with open(path, 'w') as f:
            values = self.__dict__.copy()
            values['dependencies'] = list(self.dependencies)
            f.write(json.dumps(values))


class CompilationResult:
    def __init__(self, template, meta: TemplateMeta, render_function: typing.Callable,
                 code: bytes, source_map=None):
        self.template = template
        self.meta = meta
        self.render_function = render_function
        self.code = code
        self.source_map = source_map


class SourceMap:
    """
    Maps lines of compiler-generated source code back to the original
    template so runtime exceptions can be traced to their template origin
    (original file name and line number).
    """

    def __init__(self, template_name: str=None, filename: str=None):
        self.template_name = template_name
        self.filename = filename
        # [(generated_line, template_line, template_source)]
        self.entries = []

    def add(self, generated_line: int, template_line: int=None, source: str=''):
        self.entries.append((generated_line, template_line, source))

    def lookup(self, generated_line: int):
        """
        Returns the (template_line, template_source) of the nearest mapping
        at or before the given generated line, or (None, None).
        """
        match = None
        for entry_line, template_line, source in self.entries:
            if entry_line <= generated_line and (match is None or entry_line > match[0]):
                match = (entry_line, template_line, source)
        if match is None:
            return None, None
        return match[1], match[2]

    def translate_frame(self, filename: str, line_number: int):
        """
        Translates a generated-code frame back to its template origin.
        Returns (location, line_number, template_source).
        """
        template_line, source = self.lookup(line_number)
        if template_line is None:
            return filename, line_number, None
        return self.filename or self.template_name or filename, template_line, source

    def annotate_exception(self, error: Exception) -> list:
        """
        Walks the exception traceback mapping every frame back to the
        original template. Returns a list of
        (location, line_number, function_name, template_source) tuples.
        """
        frames = []
        traceback = error.__traceback__
        while traceback is not None:
            code = traceback.tb_frame.f_code
            location, line_number, source = self.translate_frame(code.co_filename, traceback.tb_lineno)
            frames.append((location, line_number, code.co_name, source))
            traceback = traceback.tb_next
        return frames

    def format_exception(self, error: Exception) -> str:
        """
        Renders an annotated traceback pointing to the original template
        locations instead of the generated code ones.
        """
        lines = ['Traceback (most recent call last, template source map):']
        for location, line_number, function_name, source in self.annotate_exception(error):
            lines.append(f'  File "{location}", line {line_number}, in {function_name}')
            if source:
                lines.append(f'    {source}')
        lines.append(f'{type(error).__name__}: {error}')
        return '\n'.join(lines)


def get_architecture_signature() -> str:
    return ''.join(platform.architecture())


def generate_entry_point(template) -> str:
    return 'render_' + template.hash


def get_import_names(root: str, template_path: str):
    names = {os.path.join(root, template_path), os.path.basename(template_path)}
    pieces = template_path.split('/')
    for index in range(1, len(pieces)):
        names.add(os.path.sep.join(pieces[index:]))
    return list(names)
