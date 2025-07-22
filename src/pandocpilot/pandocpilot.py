from __future__ import annotations

from typing import Optional, Dict
from subprocess import Popen, PIPE

import json
import os.path
from pathlib import Path
import importlib.resources
import importlib.util

from pandocfilters import applyJSONFilters

from astex.demacro import Demacro
from astex.ast import to_ast
from astex.utils import clean


def _convert(cmd, data):
    proc = Popen(cmd, stdin=PIPE, stdout=PIPE)
    out = proc.communicate(data.encode('utf-8'))[0]
    proc.wait()

    return out.decode('utf-8')


class Plugin:
    def __init__(self, manager: PluginManager):
        self.manager = manager
        self.name: str = ""
        self.description: str = ""
        self.requires = []
        self.macro_filenames = []
        self.code_filename: Optional[str] = None
        self.auto_load = False
        self.data = None

        self.is_builtin = False
        self.code: Optional[PluginCode] = None
        self.macros = {}
        self.macro_files = []
        self.code_file = None

    @staticmethod
    def from_file(file, manager: PluginManager):
        with file:
            json_data = json.load(file)
            obj = Plugin(manager)

            obj.name = json_data.get('name', '')
            obj.description = json_data.get('description', '')
            obj.requires = json_data.get('requires', [])
            obj.macro_filenames = json_data.get('macro_files', [])
            obj.auto_load = json_data.get('auto_load', True)
            obj.data = json_data.get('data')
            obj.code_filename = json_data.get('code')

            return obj

    def load_internal(self, demacro: Demacro):
        old_macros = demacro.macros.copy()

        # Load the code object
        if self.code is None and self.code_filename is not None:
            module_name, obj_name = self.code_filename.rsplit('.', maxsplit=1)

            if self.code_file is not None:
                fname, _ = os.path.splitext(self.code_file.name)
                spec = importlib.util.spec_from_file_location(fname, self.code_file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            else:
                module = importlib.import_module(module_name)

            if module and hasattr(module, obj_name):
                obj = getattr(module, obj_name)
                if issubclass(obj, PluginCode):
                    self.code = obj(self, self.manager)
            else:
                raise ValueError(f"Could not import object {obj_name} from module {module_name}")

            # Insert any macros now
            demacro.add_macros(self.code.get_macros())

        # Now load all macro files
        for macro_file in self.macro_files:
            with open(macro_file) as file:
                demacro.demacro(to_ast(file=file))

        # Diff the macro dicts to get the modified items
        self.macros = {}

        for name, data in demacro.macros.items():
            if name not in old_macros or data is not old_macros[name]:
                self.macros[name] = data


class PluginManager:
    def __init__(self, pilot: PandocPilot):
        self.pilot = pilot

        self.plugin_dirs = []
        self.plugins = {}
        self._loading = []
        self.loaded = []

    def add_path(self, path: str):
        self.plugin_dirs.append((Path(path), False))

    def add_builtin_path(self, rsc):
        self.plugin_dirs.append((importlib.resources.files(rsc), True))

    def discover_plugins(self):
        self.plugins = {}

        for path, is_builtin in self.plugin_dirs:
            plugins = {}
            modules = {}
            macros = {}

            # Load module or plugin data
            for file in path.iterdir():
                if file.is_file():
                    fname, ext = os.path.splitext(file.name)
                    if ext.lower() == '.json':
                        plugin = Plugin.from_file(file.open(), self)
                        plugin.is_builtin = is_builtin
                        plugins[fname] = plugin
                    elif not is_builtin and ext.lower() == '.py':
                        modules[fname] = file
                    elif ext.lower() in ('.tex', '.sty'):
                        macros[file.name] = file

            for plugin in plugins.values():
                # Link module in the plugin
                if plugin.code_filename and '.' in plugin.code_filename:
                    module_name, _ = plugin.code_filename.rsplit('.', maxsplit=1)
                    if module_name in modules:
                        plugin.code_file = modules[module_name]

                # Link macro files listed
                for macro in plugin.macro_filenames:
                    if macro in macros:
                        plugin.macro_files.append(macros[macro])
                    else:
                        raise ValueError(f"Can't locate macro file {macro}")

            self.plugins.update(plugins)

    def load_plugin(self, name: str, demacro: Demacro):
        plugin = self.plugins[name]
        if plugin in self.loaded:
            return

        if plugin in self._loading:
            raise ValueError(f"Circular import dectected when importing {name}")

        self._loading.append(plugin)

        # Load all required plugins first
        for req in plugin.requires:
            req_plugin = self.plugins[req]
            if req_plugin not in self.loaded:
                self.load_plugin(req, demacro)

        # Now load this plugin
        plugin.load_internal(demacro)
        self._loading.remove(plugin)
        self.loaded.append(plugin)


class PluginCode:
    def __init__(self, plugin: Plugin, manager: PluginManager):
        self.plugin = plugin
        self.manager = manager
        self.pandoc_processors = []

    def get_macros(self) -> Dict:
        return {}

    def reset(self):
        pass

    def finalize(self):
        pass


class PandocPilot:
    def __init__(self):
        self.manager = PluginManager(self)
        self.demacro = Demacro()

        self.macro_files = []
        self.pandoc_html_opts = ['pandoc', '-f', 'json', '-t', 'html']
        self.pandoc_tex_opts = ['pandoc', '-f', 'latex', '-t', 'json']
        self.running = False

    def add_bibliography(self, bib_file):
        self.pandoc_tex_opts.extend(['--citeproc', '--bibliography', bib_file])

    def add_csl(self, csl_file):
        self.pandoc_tex_opts.extend(['--csl', csl_file])

    def start(self):
        # Load plugins and activate autoload ones
        self.manager.discover_plugins()

        for name, plugin in self.manager.plugins.items():
            if plugin.auto_load:
                self.manager.load_plugin(name, self.demacro)

        # Load additional macro files
        for file in self.macro_files:
            self.demacro.demacro(to_ast(file=file))

        self.running = True

    def stop(self):
        for plugin in self.manager.loaded:
            if plugin.code:
                plugin.code.finalize()

        self.running = False

    def process(self, input_file):
        if not self.running:
            self.start()

        # Load in file, convert to AST, prepare loaded plugins, then demacro and clean
        root = to_ast(file=input_file)
        prev_macros = self.demacro.macros

        for plugin in self.manager.loaded:
            if plugin.code:
                plugin.code.reset()

        root = self.demacro.demacro(root)
        self.demacro.macros = prev_macros
        latex = str(clean(root))

        # Convert latex data to pandoc json data, post-process, then convert to output format
        json_data = _convert(self.pandoc_tex_opts, latex)
        processors = []

        for plugin in self.manager.loaded:
            if plugin.code:
                processors.extend(plugin.code.pandoc_processors)

        processors = map(lambda p: p[0], sorted(processors, key=lambda p: p[1]))

        json_data = applyJSONFilters(processors, json_data)
        return _convert(self.pandoc_html_opts, json_data)
