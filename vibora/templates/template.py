import hashlib
import json
import re
from typing import List, Callable
from .utils import TemplateMeta
from .exceptions import InvalidTag, TemplateRenderError
from .nodes import EvalNode, ForNode, ExtendsNode, BlockNode, IfNode, ElifNode, ElseNode, StaticNode, UrlNode, \
    MacroNode, IncludeNode, Node, TextNode


class Template:

    def __init__(self, content: str, source: str=None):
        self.content = content
        self.source = source or '<template>'
        self.hash = hashlib.md5(content.encode()).hexdigest()


class ParsedTemplate(Template):

    def __init__(self, content: str, ast: Node, dependencies: set = None, prepared: bool = False,
                 source: str = None):
        super().__init__(content=content, source=source)
        self.ast = ast
        self.dependencies = dependencies or set()
        self.prepared = prepared

    @staticmethod
    def flat_view(current_node: Node):
        for child in current_node.children:
            yield child
            for c in ParsedTemplate.flat_view(child):
                yield c


class CompiledTemplate(ParsedTemplate):
    def __init__(self, content: str, ast: Node, dependencies: set, code: str,
                 meta: TemplateMeta, render: Callable):
        super().__init__(content=content, ast=ast, dependencies=dependencies)
        self.code = code
        self.meta = meta
        self.render = render

    def render_exception(self, error: Exception, name: str):
        """

        :param error:
        :param name:
        :return:
        """
        line_number = error.__traceback__.tb_next.tb_lineno
        template_line = ''
        counter = 0
        start_counting = False
        for line in self.code.splitlines():
            line = line.strip()
            if self.meta.entry_point in line:
                start_counting = True
            if counter == line_number and start_counting:
                break
            if line.startswith('#'):
                template_line = line[2:]
            if start_counting:
                counter += 1
        raise TemplateRenderError(template=self, template_line=template_line, exception=error,
                                  template_name=name)


class TemplateParser:

    def __init__(self, nodes: List[Node] = None, tag_start: str = '{%', tag_end: str = '%}',
                 expression_start: str = '{{', expression_end: str = '}}'):
        self.nodes = nodes or [
            EvalNode, ForNode, ExtendsNode, BlockNode, IfNode, ElifNode, ElseNode, StaticNode, UrlNode,
            MacroNode, IncludeNode, TextNode
        ]
        self.tags_rgx = re.compile(tag_start + '(.*?)' + tag_end)
        self.expr_rgx = re.compile(expression_start + '(.*?)' + expression_end)

    @staticmethod
    def token_is_equal(a, b):
        """

        :param a:
        :param b:
        :return:
        """
        return a[2:-2].strip() == b[2:-2].strip()

    def parse_node(self, node: str, is_tag: bool):
        """

        :param node:
        :param is_tag:
        :return:
        """
        for node_element in self.nodes:
            values = node_element.check(node, is_tag)
            if values:
                return values
        return None, None

    def find_next_node(self, content: str):
        """

        :param content:
        :return:
        """
        next_tag = self.tags_rgx.search(content)
        next_expr = self.expr_rgx.search(content)
        if next_tag and next_expr:
            if next_tag.span()[0] < next_expr.span()[0]:
                return next_tag, True
            return next_expr, False
        elif next_tag:
            return next_tag, False
        elif next_expr:
            return next_expr, False
        return None

    def parse(self, template: Template) -> ParsedTemplate:
        """

        :param template:
        :return:
        """
        parsed_template = ParsedTemplate(content=template.content, ast=Node(),
                                         source=template.source)
        current_nodes, stop_tokens = [parsed_template.ast], []
        content = template.content
        consumed = 0
        while content:
            next_node = self.find_next_node(content)
            if not next_node:
                current_nodes[-1].children.append(TextNode(content))
                break
            else:
                node_start, node_end = next_node[0].span()
                previous_text = content[:node_start]
                if previous_text:
                    current_nodes[-1].children.append(TextNode(previous_text))
                raw_tag = content[node_start:node_end]
            if stop_tokens and self.token_is_equal(raw_tag.strip(), stop_tokens[-1]):
                current_nodes = current_nodes[:-1]
                stop_tokens = stop_tokens[:-1]
                consumed += node_end
                content = content[node_end:]
                continue
            if next_node:
                found_node, stop_tag = self.parse_node(next_node[0].group(), next_node[1])
                if not found_node:
                    raise InvalidTag(next_node)
                found_node.line = template.content.count('\n', 0, consumed + node_start) + 1
                found_node.source = template.source
                if stop_tag:
                    # Greed case
                    current_nodes[-1].children.append(found_node)
                    current_nodes.append(found_node)
                    stop_tokens.append(stop_tag)
                else:
                    current_nodes[-1].children.append(found_node)
                consumed += node_end
                content = content[node_end:]
        return parsed_template
