import time
import os
import threading
from .engine import TemplateEngine
from .template import Template
from .utils import get_import_names


class TemplateLoader(threading.Thread):
    def __init__(self, directories: list, engine: TemplateEngine, supported_files: list=None, interval: int=0.5):
        super().__init__()
        self.directories = directories
        self.engine = engine
        self.supported_files = supported_files or ('.html', '.vib')
        self.cache = {}
        self.path_index = {}
        self.hash_index = {}
        self.interval = interval
        self.has_to_run = True

    def reload_templates(self, paths: list):
        changed_templates = {}
        for root, path in paths:
            if path in self.path_index:
                template = self.path_index[path]

                # Searching for templates who depends on this one.
                relationships = []
                for meta in self.engine.cache.loaded_metas.values():
                    if template.hash in meta.dependencies:
                        relationships.append(meta.template_hash)

                # In case we found dependencies we need to reload them too.
                for template_hash in relationships:
                    if template_hash in self.hash_index:
                        values = self.hash_index[template_hash]
                        self.engine.remove_template(values[2])
                        changed = self.add_to_engine(values[0], values[1])
                        changed_templates[changed.hash] = changed

                # Removing the actual template.
                self.engine.remove_template(template)

            changed = self.add_to_engine(root, path)
            changed_templates[changed.hash] = changed
        self.engine.sync_cache()
        # Incremental compilation: only recompile the templates that actually
        # changed (and their dependents) instead of the entire template set,
        # which keeps CPU usage flat on big projects.
        self.engine.compile_many(changed_templates.values())

    def check_for_modified_templates(self):
        to_be_notified = []
        for path in self.directories:
            for root, dirs, files in os.walk(path):
                for file in [f for f in files if f.endswith(self.supported_files)]:
                    path = os.path.join(root, file)
                    try:
                        last_modified = os.path.getmtime(path)
                        if path in self.cache:
                            if self.cache[path] != last_modified:
                                to_be_notified.append((root, path))
                        else:
                            to_be_notified.append((root, path))
                        self.cache[path] = last_modified
                    except FileNotFoundError:
                        continue
        if to_be_notified:
            self.reload_templates(to_be_notified)

    def add_to_engine(self, root: str, path: str):
        with open(path, 'r') as f:
            template = Template(f.read())
            template.filename = path
            names = get_import_names(root, path)
            template = self.engine.add_template(template, names=names)
            self.path_index[path] = template
            self.hash_index[template.hash] = (root, path, template)
            return template

    def load(self):
        for directory in self.directories:
            for root, dirs, files in os.walk(directory):
                for file in files:
                    if file.endswith(self.supported_files):
                        path = os.path.join(root, file)
                        self.add_to_engine(root, path)
                        try:
                            # Seeding the modification cache so the first polling
                            # cycle does not mistake every file for a new one.
                            self.cache[path] = os.path.getmtime(path)
                        except FileNotFoundError:
                            continue

    def run(self):
        while self.has_to_run:
            self.check_for_modified_templates()
            time.sleep(self.interval)
