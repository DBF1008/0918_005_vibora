from typing import Dict
from .ast import merge, raise_nodes, resolve_include_nodes
from .exceptions import TemplateNotFound, ConflictingNames
from .nodes import ExtendsNode, MacroNode
from .template import Template, TemplateParser, ParsedTemplate, CompiledTemplate
from .cache import InMemoryCache
from .compilers.python import PythonTemplateCompiler


class TemplateEngine:

    def __init__(self, cache_engine=None, compiler=None, extensions: list=None,
                 parser: TemplateParser=None):

        # Loaded templates list, caching purposes.
        # {Name: ParsedTemplate}
        self.templates: Dict[str, ParsedTemplate] = {}

        # This is a cache holding the functions responsible for actually rendering the template.
        # {TemplateHash: CompiledTemplate}
        self.compiled_templates: Dict[str, CompiledTemplate] = {}

        # Templates compiler, translate a template AST to a Python callable.
        self.compiler = compiler or PythonTemplateCompiler()

        # Extensions can modify template nodes before compilation.
        self.extensions = extensions if extensions else []

        # Template compilation is an expensive process so we try to cache as much as possible,
        # especially when using Cython compiler.
        self.cache = cache_engine or InMemoryCache()

        # Template parser.
        self.template_parser = parser or TemplateParser()

    def remove_template(self, template: ParsedTemplate):
        """

        :param template:
        :return:
        """
        keys_to_remove = []
        for name, t in self.templates.items():
            if t.hash == template.hash:
                keys_to_remove.append(name)
        for key in keys_to_remove:
            try:
                del self.templates[key]
            except KeyError:
                pass
            try:
                del self.compiled_templates[template.hash]
            except KeyError:
                pass
        self.cache.remove(template.hash)

    def add_template(self, template: Template, names: list) -> ParsedTemplate:
        """

        :param template:
        :param names:
        :return:
        """
        return self.add_templates([(template, names)])[0]

    def add_templates(self, templates: list) -> list:
        """
        Transactionally registers a batch of templates: either every template
        is registered or none of them are (all-or-nothing semantics).

        Each item must be a (Template, names) tuple. If any template fails to
        parse or has no unique name available, the whole batch is rolled back
        and the engine state is left untouched.

        :param templates: list of (Template, names) tuples.
        :return: list of ParsedTemplate, in the same order as the input.
        """
        parsed_templates = []
        staged = {}
        reserved_names = set(self.templates)
        for template, names in templates:
            # Parsing happens before any state mutation so a parse
            # failure cannot leave partial side effects behind.
            parsed = self.template_parser.parse(template)
            available_names = [name for name in names if name not in reserved_names]
            if not available_names:
                raise ConflictingNames('This template needs a unique name because imports are name based.')
            for name in available_names:
                staged[name] = parsed
            reserved_names.update(available_names)
            parsed_templates.append(parsed)

        # Commit point: the batch was fully validated upfront so the
        # registration below cannot fail halfway through.
        self.templates.update(staged)
        return parsed_templates

    async def render(self, name: str, streaming: bool=False, **template_vars):
        """

        :param streaming:
        :param name:
        :param template_vars:
        :return:
        """
        try:
            template = self.templates[name]
            try:
                compiled_template = self.compiled_templates[template.hash]
                template_generator = compiled_template.render({**template_vars})
                if streaming:
                    return template_generator
                try:
                    content = ''
                    async for chunk in template_generator:
                        content += chunk
                    return content
                except Exception as error:
                    raise compiled_template.render_exception(error, name=name)
            except KeyError:
                raise Exception('You need to compile your templates first.')
        except KeyError:
            raise TemplateNotFound(name)

    def get_template(self, name: str):
        """

        :param name:
        :return:
        """
        try:
            return self.templates[name]
        except KeyError:
            raise TemplateNotFound(name)

    def get_compiled_template(self, template: Template):
        """

        :param template:
        :return:
        """
        try:
            return self.compiled_templates[template.hash]
        except KeyError:
            raise TemplateNotFound(f'You need to compile this template first.')

    def prepare_template(self, template: ParsedTemplate):
        """

        :param template:
        :return:
        """

        # Checking if this template is already prepared.
        if template.prepared:
            return

        # Calling extensions so they have a chance to modify the template include/extends node.
        for extension in self.extensions:
            extension.before_prepare(self, template)

        # Resolving "include" nodes.
        relationships = resolve_include_nodes(self, template.ast.children)
        for t in relationships:
            template.dependencies.add(t.hash)

        # Resolving "extends" nodes.
        for index, node in enumerate(template.ast.children):
            if isinstance(node, ExtendsNode):
                parent = self.get_template(node.parent)
                template.dependencies.add(parent.hash)
                self.prepare_template(parent)
                template.ast = merge(parent, template)

        # Macro nodes needs to be compiled first.
        raise_nodes(lambda x: isinstance(x, MacroNode), template.ast)

        template.prepared = True

    def sync_cache(self):
        """

        :return:
        """
        updated_hashes = [t.hash for t in self.templates.values()]
        for template_hash, meta in self.cache.loaded_metas.items():
            if any([x for x in meta.dependencies if x not in updated_hashes]):
                self.cache.remove(template_hash)

    def compile_templates(self, verbose=False):
        """

        :param verbose:
        :return:
        """
        self.compile_many(self.templates.values(), verbose=verbose)

        # Cleaning old template cache files.
        self.cache.clean(set([t.hash for t in self.templates.values()]))

    def compile_many(self, templates, verbose=False):
        """
        Compiles only the given templates instead of the entire engine,
        allowing incremental recompilation of what actually changed.

        :param templates:
        :param verbose:
        :return:
        """
        for template in templates:
            self.compile_template(template, verbose=verbose)

    def compile_template(self, template: ParsedTemplate, verbose=False) -> CompiledTemplate:
        """

        :param template:
        :param verbose:
        :return:
        """
        # Trying to load the compiled version from cache,
        # if not possible then let's call the compiler to build this template.
        compiled_template = self.cache.get(template.hash)
        if compiled_template is None:

            # Optimizing/Replacing nodes so we compile it with the final AST.
            if not template.prepared:
                self.prepare_template(template)

            compiled_template = self.compiler.compile(template, verbose=verbose)
            self.cache.store(compiled_template)

        # Caching the render function for fast access.
        self.compiled_templates[template.hash] = compiled_template
        return compiled_template
