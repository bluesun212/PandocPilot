# TODO
# elif key == 'CodeBlock':
# return RawBlock('html', f'<pre><code data-line-numbers>{value[1]}</code></pre>')


from typing import Optional, List, Dict, Tuple

from pandocpilot.pandocpilot import PluginManager, PluginCode, Plugin

from subprocess import Popen, PIPE

from astex.ast import *
from astex.utils import *

from pandocfilters import *


_INTERRUPT = "@PANDOCINTERRUPT"


class CorePlugin(PluginCode):
    def __init__(self, plugin: Plugin, manager: PluginManager):
        super().__init__(plugin, manager)
        self.custom_cmds = {}

        self.pandoc_processors.append((self._process_custom_envs, 0))

    def _process_custom_envs(self, key, value, _, __):
        if key in ('BulletList', 'Strong'):
            if stringify(value[0]) == _INTERRUPT:
                cmd_name = stringify(value[1])
                cmd = self.custom_cmds[cmd_name]

                # This fixes a slight problem where passing an empty argument will produce no span
                # So we append some data to the beginning of the argument to be removed later
                if key == 'Strong':
                    for v in value[2:]:
                        del v['c'][1][:2]

                return cmd['callback'](cmd_name, *value[2:])

    @staticmethod
    def _custom_command_body(cmd, args, inline):
        # Turn block elements into bullet lists so that it can pass through pandoc without being changed
        # For inline elements, use a bold span
        # In either case, wrap args in either paragraphs or spans
        # Note this is kind of a hack, so it may produce unexpected behavior.
        if inline:
            body = rf"\textbf\begingroup{{{_INTERRUPT}}}{{{cmd}}}"
        else:
            body = rf"\noexpand{{\begin{{itemize}}}}\item {_INTERRUPT} \item {cmd} "

        for i in range(args):
            if inline:
                body += rf"{{X #{i+1}}}"
            else:
                body += rf"\item #{i+1} "

        return body

    def get_custom_command(self, cmd, callback, inline=False, args=0, default=None):
        # Create a macro that can pass through pandoc without being modified
        end = r'\endgroup{}' if inline else r'\noexpand{\end{itemize}}'
        body = self._custom_command_body(cmd, args, inline) + end
        self.custom_cmds[cmd] = {'args': args, 'callback': callback}
        return {cmd: {'body': body, 'args': args, 'default': default}}

    def get_custom_env(self, env, callback, args=0, default=None, is_math=False):
        # Create a block environment that can pass through pandoc without being modified
        body = self._custom_command_body(env, args, False) + rf'\item'
        end_body = r"\noexpand{\end{itemize}}"
        if is_math:
            body += r'\['
            end_body = r'\]' + end_body

        self.custom_cmds[env] = {'args': args + 1, 'callback': callback}
        return {env: {'body': body, 'args': args, 'default': default},
                f"end{env}": {'body': end_body}}

    def get_macros(self):
        # Helper functions
        def _expand(n: Node, x: Node):
            cont = GroupNode()
            cont.take(x)
            cont, _ = self.manager.pilot.demacro.expand(cont, n.parent.data['macros'])
            return cont

        # Counter related custom commands
        def _counter(counter: str, num: int, new=False):
            node = GroupNode()
            node.add(CommandNode('newcommand' if new else 'renewcommand'))
            node.add(CommandNode(f'the{counter}'))
            num_node = BracketNode()
            num_node.add(TextNode(str(num)))
            node.add(num_node)
            return node

        def _newcounter(cmd: Node, counter: Node):
            counter = str(_expand(cmd, counter))
            return _counter(counter, 0, new=True)

        def _setcounter(cmd: Node, counter: Node, number: Node):
            counter = str(_expand(cmd, counter))
            number = str(_expand(cmd, number))
            return _counter(counter, int(number))

        def _addtocounter(cmd: Node, counter: Node, number: Node):
            counter = str(_expand(cmd, counter))
            num = int(str(number)) + int(str(_expand(cmd, CommandNode(f'the{counter}'))))
            return _counter(counter, num)

        def _stepcounter(cmd: Node, counter: Node):
            counter = _expand(cmd, counter)
            test1 = _expand(cmd, CommandNode(f'the{counter}'))
            num = int(str(test1)) + 1
            return _counter(counter, num)

        # Other custom commands
        def _ifempty(n, test, t, f):
            test = str(_expand(n, test))
            return f if test else t

        def _ifdefined(cmd: Node, test: Node, t: Node, f: Node):
            # Read command name
            if isinstance(test, GroupNode):
                # TODO: This is a workaround because read_next skips the current
                test.add(WhitespaceNode(' '), after=False)
                test = read_next(test.start)

            if isinstance(test, CommandNode):
                return t if test.data in cmd.parent.data['macros'] else f

            return f

        def _ifequal(cmd: Node, test1: Node, test2: Node, t: Node, f: Node):
            test1 = str(_expand(cmd, test1))
            test2 = str(_expand(cmd, test2))
            return t if test1 == test2 else f

        def _csname(n: Node, name: Node):
            return CommandNode(str(_expand(n, name)))

        def _ifcsname(cmd: Node, test: Node, t: Node, f: Node):
            test_data = str(_expand(cmd, test))
            return t if test_data in cmd.parent.data['macros'] else f

        def _noexpand(n, body):
            n.parent.take(body, n, after=False)

        def _let(n, cmd_new, cmd_old):
            macros = self.manager.pilot.demacro.check_macros(n)

            # Read command data
            if isinstance(cmd_new, GroupNode):
                cmd_new = read_next(cmd_new)

            if isinstance(cmd_old, GroupNode):
                cmd_old = read_next(cmd_old)

            if not isinstance(cmd_old, CommandNode) or cmd_old.data not in macros:
                raise ValueError("First argument of \\let must be an existing command")

            macros[cmd_new.data] = macros[cmd_old.data]

        def _edef(cmd: Node, d: Node, body: Node):
            macros = self.manager.pilot.demacro.check_macros(cmd)

            # Read command data
            if isinstance(d, GroupNode):
                # TODO
                d.add(WhitespaceNode(' '), after=False)
                d = read_next(d.start)

            if not isinstance(d, CommandNode):
                raise ValueError("First argument of edef must be command")

            macros[d.data] = {'args': 0, 'body': _expand(cmd, body)}

        def _breakpoint(_):
            print("breakpoint")

        def _html(cmd, attrs, html_id, classes, internal):
            attrs = stringify(attrs)
            html_id = stringify(html_id)
            classes = stringify(classes).split()
            if attrs:
                # TODO: Make a more robust implementation
                attrs = [p.split('=') for p in attrs.split(',')]
                for a in attrs:
                    a[1] = a[1].strip()[1:-1]  # Removes leading and trailing whitespace and quotes
            else:
                attrs = []

            if cmd.lower() == 'div':
                return Div([html_id, classes, attrs], internal)
            else:
                return Span([html_id, classes, attrs], internal['c'][1])

        def _raw_html(_, internal):
            return RawBlock('html', stringify(internal))

        # Construct return dictionary
        ret = {'newcounter': {'args': 1, 'body': _newcounter},
               'setcounter': {'args': 2, 'body': _setcounter},
               'addtocounter': {'args': 2, 'body': _addtocounter},
               'stepcounter': {'args': 1, 'body': _stepcounter},
               'ifempty': {'args': 3, 'body': _ifempty},
               'ifdefined': {'args': 3, 'body': _ifdefined},
               'ifequal': {'args': 4, 'body': _ifequal},
               'csname': {'args': 1, 'body': _csname},
               'ifcsname': {'args': 3, 'body': _ifcsname},
               'noexpand': {'args': 1, 'body': _noexpand},
               'let': {'args': 2, 'body': _let},
               'edef': {'args': 2, 'body': _edef},
               'breakpoint': {'args': 0, 'body': _breakpoint}}

        ret.update(self.get_custom_command('span', _html, args=4, default="", inline=True))
        ret.update(self.get_custom_command('div', _html, args=4, default=""))
        ret.update(self.get_custom_command('rawHTML', _raw_html, args=1))
        ret.update(self.get_custom_env('Div', _html, args=3, default=""))

        return ret


class LabelPlugin(PluginCode):
    def __init__(self, plugin: Plugin, manager: PluginManager):
        super().__init__(plugin, manager)
        self.pandoc_processors.append((self._process_refs, 0))

        self.span = to_ast(r"\span{#1}{}{#2}")

        self.labels: Dict[str, str] = {}
        self.label_stack: List[Optional[Tuple[Node, str]]] = [None]

    def _process_refs(self, key, value, _, __):
        if key == 'Link':
            # Fix references
            if len(value[0][2]) == 2:
                ref = value[0][2][1][1]
                value[1][0]['c'] = self.labels.get(ref, ref)
                return Link(*value)

    def reset(self):
        self.label_stack = [None]

    def get_macros(self) -> Dict:
        def _expand(n: Node, x: Node):
            cont = GroupNode()
            cont.take(x)
            cont, _ = self.manager.pilot.demacro.expand(cont, n.parent.data['macros'])
            return cont

        def _push_label_stack(_):
            self.label_stack.append(None)

        def _pop_label_stack(_):
            self.label_stack.pop()

        def _label_target(cmd: Node, n: Node, num: Node):
            ret = GroupNode()
            self.label_stack[-1] = (n, str(_expand(cmd, num)))
            ret.add(n)

            return ret

        def _wrap_target(cmd: Node, label: Node):
            label_str = str(_expand(cmd, label))

            # Get latest valid stack entry
            ind = len(self.label_stack)
            entry = None
            while ind > 0 and entry is None:
                ind -= 1
                entry = self.label_stack[ind]

            # A label targest was found, so wrap it in a span with label ID
            if entry is not None:
                node = entry[0]
                spanned = self.span.copy()
                replace_parameters(spanned, [TextNode(label_str), node], copy=False)
                spanned = _expand(cmd, spanned)

                node.replace(spanned)

                self.label_stack[ind] = (spanned, entry[1])
                return TextNode(entry[1])

            return TextNode('')

        def _save_label(cmd: Node, label: Node, num: Node):
            label_str = str(_expand(cmd, label))
            num_str = str(_expand(cmd, num))
            self.labels[label_str] = num_str

        return {'@pushlabelstack': {'body': _push_label_stack},
                '@poplabelstack': {'body': _pop_label_stack},
                '@labeltarget': {'body': _label_target},
                '@wraptarget': {'body': _wrap_target},
                '@savelabel': {'body': _save_label}}


class CitationPlugin(PluginCode):
    def __init__(self, plugin: Plugin, manager: PluginManager):
        super().__init__(plugin, manager)
        self.pandoc_processors.append((self._process_citations, -100))

        self.references = {}

    def reset(self):
        self.references = {}

    def _process_citations(self, key, value, _, __):
        if key == 'Div' and value[0][0] == "refs":
            print("Refs found")
            self.references = {ref['c'][0][0]: ref for ref in value[1]}
            return []

    def get_macros(self):
        def _insert_citation(_, citation):
            citation = stringify(citation)
            print(f"lf {citation}")

            if self.references:
                ref = self.references.get(f"ref-{citation}")
                return ref

        return self.manager.plugins['core'].code.get_custom_command('fullcite', _insert_citation, args=1)


class KaTeXJSONPlugin(PluginCode):
    def __init__(self, plugin: Plugin, manager: PluginManager):
        super().__init__(plugin, manager)
        self.pandoc_processors.append((self._process_katex, 0))

        self.proc = Popen("katex_json_cli", stdin=PIPE, stdout=PIPE)

    def _process_katex(self, key, value, _, __):
        # Use KaTeX to convert math to html
        if key == 'Math':
            is_block = value[0]['t'] != 'InlineMath'
            html = self.convert_latex(value[1], is_block)

            if is_block:
                return RawInline('html', html)
            else:
                return RawInline('html', html)

    def convert_latex(self, latex: str, disp=False):
        # Prepare options
        data = {"input": latex}
        opts = self.plugin.data.copy()

        if disp:
            opts['displayMode'] = True

        data['options'] = opts

        # Send over data
        self.proc.stdin.write(json.dumps(data).encode('utf-8'))
        self.proc.stdin.write(b'\n')
        self.proc.stdin.flush()

        # Read response
        out_data = json.loads(self.proc.stdout.readline())
        if 'output' in out_data:
            return out_data['output']
        else:
            print(out_data)
            raise ValueError("No data received")

    def finalize(self):
        self.proc.terminate()
        self.proc.wait()


class EnvironmentTrackerPlugin(PluginCode):
    def __init__(self, plugin: Plugin, manager: PluginManager):
        super().__init__(plugin, manager)
        self.env_stack = []

    def reset(self):
        self.env_stack = []

    def get_macros(self) -> Dict:
        def _push_envir(_, env: Node):
            node = GroupNode()
            node.take(env)
            self.env_stack.append(str(node))

        def _pop_envir(_):
            self.env_stack.pop()

        def _currenvir(_):
            return TextNode(self.env_stack[-1] if self.env_stack else '')

        return {'@pushenvir': {'args': 1, 'body': _push_envir},
                '@popenvir': {'args': 0, 'body': _pop_envir},
                '@currenvir': {'args': 0, 'body': _currenvir}}
