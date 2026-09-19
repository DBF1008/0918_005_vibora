import copy
from contextlib import contextmanager
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
        Registers a single parsed template under one or more names.

        Registration is atomic: if any of the requested names is already
        taken by another template no state is mutated and ConflictingNames
        is raised, so the caller never observes a partial registration.
        """
        parsed_template = self.template_parser.parse(template)
        conflicts = [name for name in names if name in self.templates]
        if conflicts:
            raise ConflictingNames(
                'This template needs a unique name because imports are name based. '
                f'Conflicting names: {conflicts}'
            )
        for name in names:
            self.templates[name] = parsed_template
        return parsed_template

    def add_templates(self, templates: list):
        """
        Transactionally registers a batch of (Template, names) pairs.

        Every template is parsed and validated up-front and the batch is
        committed only when no name conflicts exist; any failure rolls the
        engine back to its previous state (all-or-nothing semantics).
        """
        with self.transaction():
            parsed_templates = []
            claimed = {}
            for template, names in templates:
                parsed_template = self.template_parser.parse(template)
                for name in names:
                    if name in self.templates or name in claimed:
                        raise ConflictingNames(
                            'This template needs a unique name because imports are name based. '
                            f'Conflicting name: {name}'
                        )
                    claimed[name] = parsed_template
                parsed_templates.append((parsed_template, names))
            for parsed_template, names in parsed_templates:
                for name in names:
                    self.templates[name] = parsed_template
            return [parsed_template for parsed_template, _ in parsed_templates]

    def _snapshot(self) -> dict:
        return {
            'templates': {
                name: copy.deepcopy(template) for name, template in self.templates.items()
            },
            'compiled_templates': dict(self.compiled_templates),
            'cache_templates': dict(getattr(self.cache, 'loaded_templates', {})),
            'cache_metas': dict(getattr(self.cache, 'loaded_metas', {}))
        }

    def _restore(self, snapshot: dict):
        self.templates = copy.deepcopy(snapshot['templates'])
        self.compiled_templates = dict(snapshot['compiled_templates'])
        cache_templates = getattr(self.cache, 'loaded_templates', None)
        if cache_templates is not None:
            self.cache.loaded_templates = dict(snapshot['cache_templates'])
            self.cache.loaded_metas = dict(snapshot['cache_metas'])

    @contextmanager
    def transaction(self):
        """
        Context manager providing all-or-nothing semantics around batch
        operations. Any exception raised inside the block rolls back every
        side effect (registrations, AST rewrites, dependencies, cache).
        """
        snapshot = self._snapshot()
        try:
            yield self
        except Exception:
            self._restore(snapshot)
            raise

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
        for template_hash, meta in list(self.cache.loaded_metas.items()):
            if any([x for x in meta.dependencies if x not in updated_hashes]):
                self.cache.remove(template_hash)

    def get_dependents(self, template_hashes) -> set:
        """
        Returns the transitive closure of templates that depend (directly or
        indirectly) on any of the given template hashes.
        """
        affected = set(template_hashes)
        changed = True
        while changed:
            changed = False
            for template in self.templates.values():
                if template.hash in affected:
                    continue
                if template.dependencies & affected:
                    affected.add(template.hash)
                    changed = True
        return affected

    def compile_affected(self, changed_hashes: set, verbose: bool = False):
        """
        Recompiles only the changed templates and their dependents instead of
        recompiling the whole project. Still wrapped in a transaction so a
        failing compile never leaves the engine half-rebuilt.
        """
        affected_hashes = self.get_dependents(changed_hashes)
        with self.transaction():
            for template in list(self.templates.values()):
                if template.hash not in affected_hashes:
                    continue
                compiled_template = self.cache.get(template.hash)
                if compiled_template is None:
                    if not template.prepared:
                        self.prepare_template(template)
                    compiled_template = self.compiler.compile(template, verbose=verbose)
                    self.cache.store(compiled_template)
                self.compiled_templates[template.hash] = compiled_template
            self.cache.clean(set(t.hash for t in self.templates.values()))
        return affected_hashes

    def compile_templates(self, verbose=False):
        """

        :param verbose:
        :return:
        """
        # Checking if all dependencies are met
        for template in self.templates.values():

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

        # Cleaning old template cache files.
        self.cache.clean(set([t.hash for t in self.templates.values()]))
