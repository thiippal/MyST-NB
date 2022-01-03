"""A parser for docutils."""
import json
from typing import Any, Dict, List, Optional, Tuple

import nbformat
from docutils import nodes
from docutils.core import default_description, publish_cmdline
from markdown_it.tree import SyntaxTreeNode
from myst_parser.docutils_ import DOCUTILS_EXCLUDED_ARGS as DOCUTILS_EXCLUDED_ARGS_MYST
from myst_parser.docutils_ import Parser as MystParser
from myst_parser.docutils_ import create_myst_config, create_myst_settings_spec
from myst_parser.docutils_renderer import DocutilsRenderer, token_line
from myst_parser.main import MdParserConfig, create_md_parser
from nbformat import NotebookNode

from myst_nb.configuration import NbParserConfig
from myst_nb.new.execute import update_notebook
from myst_nb.new.loggers import DocutilsLogger
from myst_nb.new.parse import notebook_to_tokens
from myst_nb.new.read import create_nb_reader
from myst_nb.new.render import NbElementRenderer, load_renderer, sanitize_script_content
from myst_nb.render_outputs import coalesce_streams

DOCUTILS_EXCLUDED_ARGS = {
    # docutils.conf can't represent dicts
    # TODO can we make this work?
    "custom_formats",
}


class Parser(MystParser):
    """Docutils parser for Jupyter Notebooks, containing MyST Markdown."""

    supported: Tuple[str, ...] = ("mystnb", "ipynb")
    """Aliases this parser supports."""

    settings_spec = (
        "MyST-NB options",
        None,
        create_myst_settings_spec(DOCUTILS_EXCLUDED_ARGS, NbParserConfig, "nb_"),
        *MystParser.settings_spec,
    )
    """Runtime settings specification."""

    config_section = "myst-nb parser"

    def parse(self, inputstring: str, document: nodes.document) -> None:
        """Parse source text.

        :param inputstring: The source string to parse
        :param document: The root docutils node to add AST elements to
        """
        document_source = document["source"]

        # get a logger for this document
        logger = DocutilsLogger(document)

        # get markdown parsing configuration
        try:
            md_config = create_myst_config(
                document.settings, DOCUTILS_EXCLUDED_ARGS_MYST
            )
        except (TypeError, ValueError) as error:
            logger.error(f"myst configuration invalid: {error.args[0]}")
            md_config = MdParserConfig()

        # get notebook rendering configuration
        try:
            nb_config = create_myst_config(
                document.settings, DOCUTILS_EXCLUDED_ARGS, NbParserConfig, "nb_"
            )
        except (TypeError, ValueError) as error:
            logger.error(f"myst-nb configuration invalid: {error.args[0]}")
            nb_config = NbParserConfig()

        # convert inputstring to notebook
        nb_reader, md_config = create_nb_reader(
            inputstring, document_source, md_config, nb_config
        )
        notebook = nb_reader(inputstring)

        # TODO update nb_config from notebook metadata

        # Setup the markdown parser
        mdit_parser = create_md_parser(md_config, DocutilsNbRenderer)
        mdit_parser.options["document"] = document
        mdit_parser.options["notebook"] = notebook
        mdit_parser.options["nb_config"] = nb_config.as_dict()
        mdit_env: Dict[str, Any] = {}

        # load notebook element renderer class from entry-point name
        # this is separate from DocutilsNbRenderer, so that users can override it
        renderer_name = nb_config.render_plugin
        nb_renderer: NbElementRenderer = load_renderer(renderer_name)(
            mdit_parser.renderer
        )
        mdit_parser.options["nb_renderer"] = nb_renderer

        # potentially execute notebook and/or populate outputs from cache
        notebook, exec_data = update_notebook(
            notebook, document_source, nb_config, logger
        )
        if exec_data:
            document["nb_exec_data"] = exec_data
        # TODO store/print error traceback?

        # TODO also write CSS to output folder if necessary or always?

        # parse to tokens
        mdit_tokens = notebook_to_tokens(notebook, mdit_parser, mdit_env, logger)
        # convert to docutils AST, which is added to the document
        mdit_parser.renderer.render(mdit_tokens, mdit_parser.options, mdit_env)


class DocutilsNbRenderer(DocutilsRenderer):
    """ "A docutils-only renderer for Jupyter Notebooks."""

    def render(self, tokens, options, md_env) -> nodes.document:
        document = super().render(tokens, options, md_env)
        # write executed notebook to output folder
        # utf-8 is the de-facto standard encoding for notebooks.
        content = nbformat.writes(self.config["notebook"]).encode("utf-8")
        if self.sphinx_env:
            path = self.sphinx_env.docname.split("/")
            path[-1] += ".ipynb"
        else:
            # TODO maybe docutils should be optional on whether to do this?
            path = ["rendered.ipynb"]
        self.config["nb_renderer"].write_file(path, content, overwrite=True)
        return document

    # TODO maybe move more things to NbOutputRenderer?
    # and change name to e.g. NbElementRenderer

    def get_nb_config(self, key: str, cell_index: Optional[int]) -> Any:
        # TODO selection between config/notebook/cell level
        # (we can maybe update the nb_config with notebook level metadata in parser)
        # TODO handle KeyError better
        return self.config["nb_config"][key]

    def render_nb_spec_data(self, token: SyntaxTreeNode) -> None:
        """Add a notebook spec data to the document attributes."""
        self.document["nb_kernelspec"] = token.meta["kernelspec"]
        self.document["nb_language_info"] = token.meta["language_info"]

    def render_nb_cell_markdown(self, token: SyntaxTreeNode) -> None:
        """Render a notebook markdown cell."""
        # TODO this is currently just a "pass-through", but we could utilise the metadata
        # it would be nice to "wrap" this in a container that included the metadata,
        # but unfortunately this would break the heading structure of docutils/sphinx.
        # perhaps we add an "invisible" (non-rendered) marker node to the document tree,
        self.render_children(token)

    def render_nb_cell_raw(self, token: SyntaxTreeNode) -> None:
        """Render a notebook raw cell."""
        # TODO

    def render_nb_cell_code(self, token: SyntaxTreeNode) -> None:
        """Render a notebook code cell."""
        cell_index = token.meta["index"]
        exec_count = token.meta["execution_count"]
        tags = token.meta["metadata"].get("tags", [])
        # create a container for all the output
        classes = ["cell"]
        for tag in tags:
            classes.append(f"tag_{tag.replace(' ', '_')}")
        cell_container = nodes.container(
            nb_type="cell_code",  # TODO maybe nb_cell="code"/"markdown"/"raw"
            cell_index=cell_index,
            # TODO some way to use this to output cell indexes in HTML?
            exec_count=exec_count,
            cell_metadata=token.meta["metadata"],
            classes=classes,
        )
        self.add_line_and_source_path(cell_container, token)
        with self.current_node_context(cell_container, append=True):

            # TODO do we need this -/_ duplication of tag names, or can deprecate one?
            # TODO it would be nice if remove_input/remove_output were also config

            # render the code source code
            if (
                (not self.get_nb_config("remove_code_source", cell_index))
                and ("remove_input" not in tags)
                and ("remove-input" not in tags)
            ):
                cell_input = nodes.container(
                    nb_type="cell_code_source", classes=["cell_input"]
                )
                self.add_line_and_source_path(cell_input, token)
                with self.current_node_context(cell_input, append=True):
                    self.render_nb_cell_code_source(token)
            # render the execution output, if any
            has_outputs = self.config["notebook"]["cells"][cell_index].get(
                "outputs", []
            )
            if (
                has_outputs
                and (not self.get_nb_config("remove_code_outputs", cell_index))
                and ("remove_output" not in tags)
                and ("remove-output" not in tags)
            ):
                cell_output = nodes.container(
                    nb_type="cell_code_output", classes=["cell_output"]
                )
                self.add_line_and_source_path(cell_output, token)
                with self.current_node_context(cell_output, append=True):
                    self.render_nb_cell_code_outputs(token)

    def render_nb_cell_code_source(self, token: SyntaxTreeNode) -> None:
        """Render a notebook code cell's source."""
        cell_index = token.meta["index"]
        lexer = token.meta.get("lexer", None)
        node = self.create_highlighted_code_block(
            token.content,
            lexer,
            number_lines=self.get_nb_config("number_source_lines", cell_index),
            source=self.document["source"],
            line=token_line(token),
        )
        self.add_line_and_source_path(node, token)
        self.current_node.append(node)

    def render_nb_cell_code_outputs(self, token: SyntaxTreeNode) -> None:
        """Render a notebook code cell's outputs."""
        cell_index = token.meta["index"]
        line = token_line(token)
        outputs: List[NotebookNode] = self.config["notebook"]["cells"][cell_index].get(
            "outputs", []
        )
        if self.get_nb_config("merge_streams", cell_index):
            # TODO should this be moved to the parsing phase?
            outputs = coalesce_streams(outputs)

        renderer: NbElementRenderer = self.config["nb_renderer"]
        render_priority = self.get_nb_config("render_priority", cell_index)

        # render the outputs
        for output in outputs:
            if output.output_type == "stream":
                if output.name == "stdout":
                    _nodes = renderer.render_stdout(output, cell_index, line)
                    self.add_line_and_source_path_r(_nodes, token)
                    self.current_node.extend(_nodes)
                elif output.name == "stderr":
                    _nodes = renderer.render_stderr(output, cell_index, line)
                    self.add_line_and_source_path_r(_nodes, token)
                    self.current_node.extend(_nodes)
                else:
                    pass  # TODO warning
            elif output.output_type == "error":
                _nodes = renderer.render_error(output, cell_index, line)
                self.add_line_and_source_path_r(_nodes, token)
                self.current_node.extend(_nodes)
            elif output.output_type in ("display_data", "execute_result"):
                # TODO how to handle figures and other means of wrapping an output:
                # TODO unwrapped Markdown (so you can output headers)
                # maybe in a transform, we grab the containers and move them
                # "below" the code cell container?
                # if embed_markdown_outputs is True,
                # this should be top priority and we "mark" the container for the transform
                try:
                    mime_type = next(x for x in render_priority if x in output["data"])
                except StopIteration:
                    self.create_warning(
                        "No output mime type found from render_priority",
                        line=line,
                        append_to=self.current_node,
                        wtype="mystnb",
                        subtype="mime_type",
                    )
                else:
                    container = nodes.container(mime_type=mime_type)
                    with self.current_node_context(container, append=True):
                        _nodes = renderer.render_mime_type(
                            mime_type, output["data"][mime_type], cell_index, line
                        )
                        self.add_line_and_source_path_r(_nodes, token)
                        self.current_node.extend(_nodes)
            else:
                self.create_warning(
                    f"Unsupported output type: {output.output_type}",
                    line=line,
                    append_to=self.current_node,
                    wtype="mystnb",
                    subtype="output_type",
                )

    def render_nb_widget_state(self, token: SyntaxTreeNode) -> None:
        """Render the HTML defining the ipywidget state."""
        # The JSON inside the script tag is identified and parsed by:
        # https://github.com/jupyter-widgets/ipywidgets/blob/32f59acbc63c3ff0acf6afa86399cb563d3a9a86/packages/html-manager/src/libembed.ts#L36
        # TODO we also need to load JS URLs if widgets are present and HTML
        html = (
            f'<script type="{token.attrGet("type")}">\n'
            f"{sanitize_script_content(json.dumps(token.meta['state']))}\n"
            "</script>"
        )
        node = nodes.raw("", html, format="html", nb_type="widget_state")
        self.add_line_and_source_path(node, token)
        # always append to bottom of the document
        self.document.append(node)


def _run_cli(writer_name: str, writer_description: str, argv: Optional[List[str]]):
    """Run the command line interface for a particular writer."""
    publish_cmdline(
        parser=Parser(),
        writer_name=writer_name,
        description=(
            f"Generates {writer_description} from standalone MyST Notebook sources.\n"
            f"{default_description}"
        ),
        argv=argv,
    )


def cli_html(argv: Optional[List[str]] = None) -> None:
    """Cmdline entrypoint for converting MyST to HTML."""
    _run_cli("html", "(X)HTML documents", argv)


def cli_html5(argv: Optional[List[str]] = None):
    """Cmdline entrypoint for converting MyST to HTML5."""
    _run_cli("html5", "HTML5 documents", argv)


def cli_latex(argv: Optional[List[str]] = None):
    """Cmdline entrypoint for converting MyST to LaTeX."""
    _run_cli("latex", "LaTeX documents", argv)


def cli_xml(argv: Optional[List[str]] = None):
    """Cmdline entrypoint for converting MyST to XML."""
    _run_cli("xml", "Docutils-native XML", argv)


def cli_pseudoxml(argv: Optional[List[str]] = None):
    """Cmdline entrypoint for converting MyST to pseudo-XML."""
    _run_cli("pseudoxml", "pseudo-XML", argv)
