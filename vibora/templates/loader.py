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
        self.stop_event = threading.Event()

    def _collect_reload_paths(self, changed_paths: list) -> list:
        """
        Expands the set of paths to reload with every transitive dependent of
        the changed templates, so includes/extends chains stay consistent.
        """
        reload_paths = list(changed_paths)
        changed_hashes = set()
        for _, path in changed_paths:
            template = self.path_index.get(path)
            if template is not None:
                changed_hashes.add(template.hash)
        if changed_hashes:
            affected_hashes = self.engine.get_dependents(changed_hashes)
            for template_hash in affected_hashes - changed_hashes:
                values = self.hash_index.get(template_hash)
                if values is not None:
                    reload_paths.append((values[0], values[1]))
        return reload_paths

    def reload_templates(self, paths: list):
        """
        Transactionally reloads only the given paths plus their dependents and
        triggers an incremental (not full-project) recompilation. If parsing
        or compilation fails the engine keeps serving the previous state.
        """
        reload_paths = self._collect_reload_paths(paths)
        changed_hashes = {
            self.path_index[path].hash for _, path in reload_paths if path in self.path_index
        }
        # Index changes are staged and only committed after the engine
        # transaction succeeds, keeping loader/engine state in sync on errors.
        saved_path_index = dict(self.path_index)
        saved_hash_index = dict(self.hash_index)
        try:
            with self.engine.transaction():
                for root, path in reload_paths:
                    self._purge_path(path)
                    self.add_to_engine(root, path)
                new_hashes = {
                    self.path_index[path].hash for _, path in reload_paths
                    if path in self.path_index
                }
                affected_hashes = changed_hashes | new_hashes
                self.engine.sync_cache()
                self.engine.compile_affected(affected_hashes)
        except Exception:
            self.path_index = saved_path_index
            self.hash_index = saved_hash_index
            raise

    def _purge_path(self, path: str):
        template = self.path_index.pop(path, None)
        if template is None:
            return
        values = self.hash_index.pop(template.hash, None)
        for registered_path in list(self.path_index.keys()):
            if self.path_index[registered_path].hash == template.hash:
                self.path_index.pop(registered_path, None)
        self.engine.remove_template(template)

    def remove_missing_files(self, existing_paths: set):
        missing_paths = [path for path in list(self.path_index.keys())
                         if path not in existing_paths]
        if not missing_paths:
            return
        removed_hashes = {
            self.path_index[path].hash for path in missing_paths if path in self.path_index
        }
        # Every dependent of a deleted template is invalid as well because
        # its include/extends chain can no longer be resolved.
        affected_paths = set(missing_paths)
        for template_hash in self.engine.get_dependents(removed_hashes) - removed_hashes:
            values = self.hash_index.get(template_hash)
            if values is not None:
                affected_paths.add(values[1])
        saved_path_index = dict(self.path_index)
        saved_hash_index = dict(self.hash_index)
        try:
            with self.engine.transaction():
                for path in affected_paths:
                    self._purge_path(path)
                    self.cache.pop(path, None)
                self.engine.sync_cache()
        except Exception:
            self.path_index = saved_path_index
            self.hash_index = saved_hash_index
            raise

    def check_for_modified_templates(self):
        to_be_notified = []
        existing_paths = set()
        for path in self.directories:
            for root, dirs, files in os.walk(path):
                for file in [f for f in files if f.endswith(self.supported_files)]:
                    path = os.path.join(root, file)
                    existing_paths.add(path)
                    try:
                        last_modified = os.path.getmtime(path)
                        if path in self.cache:
                            if self.cache[path] != last_modified:
                                to_be_notified.append((root, path))
                        else:
                            to_be_notified.append((root, path))
                    except FileNotFoundError:
                        continue

        self.remove_missing_files(existing_paths)

        # Some files may have disappeared after a failed reload retry; they
        # have already been purged above and must not be re-added.
        to_be_notified = [(root, path) for root, path in to_be_notified
                          if os.path.exists(path)]
        if to_be_notified:
            self.reload_templates(to_be_notified)
            # Only remember mtimes after the reload committed successfully,
            # otherwise the same change stays visible for the next retry.
            for root, path in to_be_notified:
                try:
                    self.cache[path] = os.path.getmtime(path)
                except FileNotFoundError:
                    self.cache.pop(path, None)
        return to_be_notified

    def add_to_engine(self, root: str, path: str):
        with open(path, 'r') as f:
            template = Template(f.read(), source=path)
            names = get_import_names(root, path)
            parsed_template = self.engine.add_template(template, names=names)
            self.path_index[path] = parsed_template
            self.hash_index[parsed_template.hash] = (root, path, parsed_template)

    def load(self):
        for directory in self.directories:
            for root, dirs, files in os.walk(directory):
                for file in files:
                    if file.endswith(self.supported_files):
                        path = os.path.join(root, file)
                        self.add_to_engine(root, path)

    def initial_load(self):
        """
        One-shot loading of every template directory followed by a single
        compilation, used by applications instead of starting the thread.
        """
        self.load()
        self.engine.sync_cache()
        self.engine.compile_templates()
        for path in list(self.path_index.keys()):
            try:
                self.cache[path] = os.path.getmtime(path)
            except FileNotFoundError:
                pass

    def run(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.check_for_modified_templates()
            except Exception:
                # A bad edit must never kill the watcher thread.
                continue

    def stop(self):
        self.has_to_run = False
        self.stop_event.set()
