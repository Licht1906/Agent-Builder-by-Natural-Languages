"""Draw the LangGraph graph each workflow template compiles to.

    python scripts/show_graph.py                       # every template -> one HTML page
    python scripts/show_graph.py action_with_approval  # one template, Mermaid on stdout
    python scripts/show_graph.py action_with_approval --png

The graph drawn is the one `compile_workflow_graph` builds — the same
`StateGraph` a real run executes, asked for its own shape through
`get_graph()`, not a second drawing kept in step with it by hand.

A template is a skeleton: its nodes have no prompt, model or tool yet, and
`ensure_runnable` rightly refuses to *run* that. Drawing needs only the shape,
so the check is patched out here and nowhere else. Nothing is executed, no
model is called, and neither Postgres nor Qdrant has to be up.
"""

from __future__ import annotations

import argparse
import html
import sys
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.graph.state import CompiledStateGraph  # noqa: E402

from src.workflow import runtime  # noqa: E402
from src.workflow.templates import TEMPLATE_REGISTRY, TemplateSpec  # noqa: E402

OUTPUT_DIR = ROOT / "artifacts" / "graphs"

_PAGE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>LangGraph — workflow templates</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 960px; padding: 0 1rem; }}
  section {{ border: 1px solid #ddd; border-radius: 8px; padding: 1rem 1.5rem; margin-bottom: 2rem; }}
  h2 {{ margin-bottom: .25rem; }}
  code {{ background: #f3f3f3; padding: 0 .3rem; border-radius: 4px; }}
  .description {{ color: #555; }}
</style>
</head>
<body>
<h1>LangGraph — đồ thị của từng workflow template</h1>
<p>Nét liền là <code>add_edge</code>; nét đứt có nhãn là <code>add_conditional_edges</code> (cổng ra của
<code>IfElse</code>, <code>While</code>, <code>HumanApproval</code>).</p>
{sections}
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{ startOnLoad: true }});
</script>
</body>
</html>
"""

_SECTION = """<section>
<h2>{name}</h2>
<p><code>{id}</code> — {node_types}</p>
<p class="description">{description}</p>
<pre class="mermaid">
{mermaid}
</pre>
</section>"""


def compile_template(template: TemplateSpec) -> CompiledStateGraph:
    """`template`'s skeleton as a compiled graph, for drawing only.

    The run context is a stand-in carrying the one field the compiler reads
    (`scripted_approvals`); every other field is read inside a node, and no
    node runs here.
    """
    run = SimpleNamespace(scripted_approvals=None)
    with patch.object(runtime, "ensure_runnable", lambda workflow: None):
        return runtime.compile_workflow_graph(template.skeleton(), checkpointer=InMemorySaver(), run=run)  # type: ignore[arg-type]


def _section(template: TemplateSpec) -> str:
    node_types = ", ".join(f"{node.id}: {node.type.value}" for node in template.nodes)
    return _SECTION.format(
        name=html.escape(template.name),
        id=html.escape(template.id),
        node_types=html.escape(node_types),
        description=html.escape(template.description),
        mermaid=html.escape(compile_template(template).get_graph().draw_mermaid()),
    )


def write_page() -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    page = OUTPUT_DIR / "langgraph.html"
    sections = "\n".join(_section(template) for template in TEMPLATE_REGISTRY.values())
    page.write_text(_PAGE.format(sections=sections), encoding="utf-8")
    return page


def write_png(template: TemplateSpec) -> Path:
    """Render through mermaid.ink, which is what `draw_mermaid_png` calls — needs network."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    png = OUTPUT_DIR / f"{template.id}.png"
    png.write_bytes(compile_template(template).get_graph().draw_mermaid_png())
    return png


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("template", nargs="?", choices=sorted(TEMPLATE_REGISTRY), help="bỏ trống = vẽ tất cả ra HTML")
    parser.add_argument("--png", action="store_true", help="xuất thêm file PNG (cần mạng)")
    parser.add_argument("--no-open", action="store_true", help="không tự mở trình duyệt")
    args = parser.parse_args()

    if args.template is None:
        page = write_page()
        print(f"Đã vẽ {len(TEMPLATE_REGISTRY)} đồ thị: {page}")
        if not args.no_open:
            webbrowser.open(page.as_uri())
        return 0

    template = TEMPLATE_REGISTRY[args.template]
    print(compile_template(template).get_graph().draw_mermaid())
    if args.png:
        print(f"PNG: {write_png(template)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
