"""Sinh docs/architecture.drawio — 4 trang sơ đồ, mở thẳng bằng draw.io.

Viết bằng script thay vì gõ tay XML để toạ độ nhất quán và sửa lại được.

Ba quy tắc bố cục, đặt ra sau khi bản đầu rối như tổ chim:

1. **Ô nằm trong khung.** Mỗi ô là con của swimlane chứa nó (`parent=`), toạ độ
   tính theo góc trái trên của swimlane. Kéo khung thì cả cụm đi theo, và
   draw.io biết đường vòng qua khung thay vì cắt ngang.
2. **Cạnh nào cũng có cổng.** Mọi cạnh đều ghi rõ `exitX/exitY` và
   `entryX/entryY`, cần thì thêm điểm gãy tuyệt đối. Không cạnh nào để router tự
   quyết, vì tự quyết là lúc nó vẽ chồng lên chữ.
3. **Đường dài thì gom lại.** Quan hệ giữa hai tầng cách xa nhau (API → Domain,
   Domain → Dữ liệu) vẽ *một* cạnh khung-với-khung, còn ai gọi ai thì viết bằng
   chữ trong ô. Mười cạnh cắt chéo trang giấy không nói được nhiều hơn một cạnh
   cộng một dòng chữ.
"""

from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent.parent / "docs" / "architecture.drawio"

# Bảng màu chuẩn của draw.io — quen mắt, in đen trắng vẫn phân biệt được.
CLIENT = "fillColor=#d5e8d4;strokeColor=#82b366;"
API = "fillColor=#dae8fc;strokeColor=#6c8ebf;"
DOMAIN = "fillColor=#ffe6cc;strokeColor=#d79b00;"
DATA = "fillColor=#e1d5e7;strokeColor=#9673a6;"
EXT = "fillColor=#f8cecc;strokeColor=#b85450;"
NOTE = "fillColor=#ffffff;strokeColor=#999999;dashed=1;"
GATE = "fillColor=#fff2cc;strokeColor=#d6b656;"

BOX = "rounded=0;whiteSpace=wrap;html=1;verticalAlign=middle;fontSize=12;"
SMALL = "fontSize=11;"
TINY = "fontSize=10;"
ROUND = "rounded=1;whiteSpace=wrap;html=1;verticalAlign=middle;fontSize=12;"
DB = "shape=cylinder3;boundedLbl=1;backgroundOutline=1;size=15;whiteSpace=wrap;html=1;fontSize=11;"
DECIDE = "rhombus;whiteSpace=wrap;html=1;fontSize=11;"
GROUP = "swimlane;html=1;startSize=28;fontSize=13;fontStyle=1;fillColor=#f5f5f5;strokeColor=#999999;"
SUBGROUP = "swimlane;html=1;startSize=28;fontSize=12;fontStyle=1;fillColor=#ffffff;strokeColor=#b3b3b3;"

# jumpStyle: chỗ nào bắt buộc phải cắt nhau thì nhảy cung, đọc ra ngay là hai
# đường không dính nhau. labelBackgroundColor: nhãn luôn có nền, không bao giờ
# lẫn vào đường kẻ bên dưới.
EDGE = (
    "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=10;endArrow=block;endFill=1;"
    "jumpStyle=arc;jumpSize=6;labelBackgroundColor=#FFFFFF;"
)
EDGE_DASH = EDGE + "dashed=1;"


def label_of(text: str) -> str:
    """Nhãn cho mxGraph.

    Xuống dòng phải là `<br>` chứ không phải ký tự newline: XML chuẩn hoá mọi
    newline nằm trong giá trị attribute thành dấu cách, nên nhãn nhiều dòng viết
    thẳng sẽ bị dồn thành một dòng. Mọi style ở đây đều có `html=1` nên `<br>`
    được render đúng.
    """
    return escape(text.replace("\n", "<br>"))


def ports(exit_: tuple[float, float] | None, entry: tuple[float, float] | None) -> str:
    """Cổng vào/ra cố định cho một cạnh.

    `(0,0)` là góc trái trên của ô, `(1,1)` là góc phải dưới. Ghi rõ cổng là cách
    duy nhất bắt draw.io vẽ đúng đoạn thẳng mình muốn: không ghi thì router chọn
    cạnh gần nhất, và "gần nhất" hay rơi vào chỗ có chữ.
    """
    style = ""
    if exit_ is not None:
        style += f"exitX={exit_[0]};exitY={exit_[1]};exitDx=0;exitDy=0;"
    if entry is not None:
        style += f"entryX={entry[0]};entryY={entry[1]};entryDx=0;entryDy=0;"
    return style


class Page:
    def __init__(self, name: str, width: int = 1600, height: int = 1100) -> None:
        self.name = name
        self.width = width
        self.height = height
        self.cells: list[str] = []
        self.counter = 0

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def group(self, label: str, x, y, w, h, *, style: str = GROUP, parent: str = "1") -> str:
        ident = self._id("g")
        self.cells.append(
            f'<mxCell id="{ident}" value="{label_of(label)}" style="{style}" vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
        )
        return ident

    def node(self, label: str, x, y, w, h, *, style: str = BOX + API, parent: str = "1") -> str:
        ident = self._id("n")
        self.cells.append(
            f'<mxCell id="{ident}" value="{label_of(label)}" style="{style}" vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
        )
        return ident

    def edge(
        self,
        source: str,
        target: str,
        label: str = "",
        *,
        style: str = EDGE,
        exit_: tuple[float, float] | None = None,
        entry: tuple[float, float] | None = None,
        points: list[tuple[int, int]] | None = None,
    ) -> str:
        """Một cạnh. `points` là điểm gãy, toạ độ tuyệt đối trên trang."""
        ident = self._id("e")
        waypoints = ""
        if points:
            inner = "".join(f'<mxPoint x="{x}" y="{y}"/>' for x, y in points)
            waypoints = f'<Array as="points">{inner}</Array>'
        self.cells.append(
            f'<mxCell id="{ident}" value="{label_of(label)}" style="{style}{ports(exit_, entry)}" '
            f'edge="1" parent="1" source="{source}" target="{target}">'
            f'<mxGeometry relative="1" as="geometry">{waypoints}</mxGeometry></mxCell>'
        )
        return ident

    def render(self) -> str:
        body = "".join(self.cells)
        return (
            f'<diagram id="{self.name.replace(" ", "_")}" name="{escape(self.name)}">'
            f'<mxGraphModel dx="1400" dy="800" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" '
            f'arrows="1" fold="1" page="1" pageScale="1" pageWidth="{self.width}" pageHeight="{self.height}" '
            f'math="0" shadow="0"><root><mxCell id="0"/><mxCell id="1" parent="0"/>{body}</root>'
            f"</mxGraphModel></diagram>"
        )


# ───────────────────────────── Trang 1: Kiến trúc ─────────────────────────────
#
# Cột trái (x 40..1050) là bốn tầng xếp dọc: Client → API → Domain → Dữ liệu.
# Cột phải (x 1170..1620) là chú giải, ghi chú, và dịch vụ ngoài — khối dịch vụ
# ngoài đặt đúng cao độ của các module gọi nó để cạnh nào cũng gần như nằm ngang.
p1 = Page("1. Kiến trúc hệ thống", 1680, 1290)

g_client = p1.group("Client", 40, 40, 1010, 125)
studio = p1.node(
    "Creator Studio — React 18 + Vite 5\nwizard mô tả · canvas kéo thả · dashboard version\n"
    "dev server proxy /api → FastAPI, nên không dính CORS",
    25, 42, 300, 64, style=BOX + SMALL + CLIENT, parent=g_client,
)
webchat = p1.node(
    "Web Chat người dùng cuối\nJWT + kiểm tra quyền của deployment",
    355, 42, 300, 64, style=BOX + SMALL + CLIENT, parent=g_client,
)
extsys = p1.node(
    "Hệ thống ngoài gọi vào\nX-API-Key của deployment",
    685, 42, 300, 64, style=BOX + SMALL + CLIENT, parent=g_client,
)

g_api = p1.group("API — FastAPI · src/api/ · 84 route · prefix /api/v1", 40, 205, 1010, 140)
r_auth = p1.node("auth_routes\n4 route\nJWT · 2 vai trò", 25, 42, 150, 76, style=BOX + SMALL + API, parent=g_api)
r_sol = p1.node("solution_routes\ninterview_routes\n12 route → bước 1", 187, 42, 150, 76, style=BOX + SMALL + API, parent=g_api)
r_wf = p1.node("workflow_routes\nagent_routes\n25 route → bước 2 · 3", 349, 42, 150, 76, style=BOX + SMALL + API, parent=g_api)
r_run = p1.node("workflow_run_routes\n5 route\n→ runtime", 511, 42, 150, 76, style=BOX + SMALL + API, parent=g_api)
r_doc = p1.node("document_routes\neval_routes\n13 route → bước 5", 673, 42, 150, 76, style=BOX + SMALL + API, parent=g_api)
r_adm = p1.node(
    "admin_routes\napproval_routes\ndeployment_routes\n25 route → bước 6 · 7 · 8",
    835, 42, 150, 76, style=BOX + SMALL + API, parent=g_api,
)

g_dom = p1.group("Domain — nghiệp vụ thuần Python (src/)", 40, 395, 1010, 644)

# Đường dựng agent đi hình rắn: hàng trên trái→phải, hàng dưới phải→trái. Nhờ vậy
# chỗ nối hai hàng chỉ là một đoạn thẳng đứng, không phải cú vòng ngang trang.
g_build = p1.group(
    "Đường dựng một agent — 8 bước, chi tiết ở trang 2", 20, 40, 970, 250, style=SUBGROUP, parent=g_dom
)
b1 = p1.node(
    "Bước 1 · AI Solution Designer\nsrc/interview/ · src/solution/\n7 slot → AgentSolutionSpecification",
    20, 40, 219, 80, style=BOX + SMALL + DOMAIN, parent=g_build,
)
b2 = p1.node(
    "Bước 2 · Workflow Generator\nsrc/workflow/ — templates · selection\nfill · drafts · 8 template có sẵn",
    257, 40, 219, 80, style=BOX + SMALL + DOMAIN, parent=g_build,
)
b3 = p1.node(
    "Bước 3 · Canvas & Validation\nsrc/workflow/ — validation\ncomposition · layout → problems[]",
    494, 40, 219, 80, style=BOX + SMALL + DOMAIN, parent=g_build,
)
b4 = p1.node(
    "Bước 4 · Sandbox\nsrc/workflow/sandbox.py\nchạy thật, tool có side effect dùng mock",
    731, 40, 219, 80, style=BOX + SMALL + DOMAIN, parent=g_build,
)
b5 = p1.node(
    "Bước 5 · Automatic Evaluation\nsrc/evals/\nsinh case · rule check · LLM judge",
    731, 150, 219, 80, style=BOX + SMALL + DOMAIN, parent=g_build,
)
b6 = p1.node(
    "Bước 6 · Versioning & Gate\nsrc/agents/versions.py · evals/gate.py\npass_rate ≥ ngưỡng mới đi tiếp",
    494, 150, 219, 80, style=BOX + SMALL + DOMAIN, parent=g_build,
)
b7 = p1.node(
    "Bước 7 · Admin Approval\nsrc/approvals/ · src/admin/\nhồ sơ duyệt: spec · workflow\neval · tool rủi ro",
    257, 150, 219, 80, style=BOX + SMALL + DOMAIN, parent=g_build,
)
b8 = p1.node(
    "Bước 8 · Deployment — chỉ chạy version đã duyệt\nsrc/deployments/\napi key · rate limit · quyền truy cập",
    20, 150, 219, 80, style=BOX + SMALL + DOMAIN, parent=g_build,
)

g_run = p1.group(
    "Khi agent chạy — runtime và các module nó gọi, xem trang 3", 20, 310, 970, 314, style=SUBGROUP, parent=g_dom
)
rt = p1.node(
    "Workflow Runtime\n(LangGraph)\nsrc/workflow/runtime.py\n\ncompile WorkflowJSON → StateGraph\n\n"
    "8 node type:\nagent_llm · knowledge_search\nweb_search · mcp · if_else\nwhile · human_approval · end\n\n"
    "state chung · router theo port\npause/resume · phát sự kiện",
    20, 40, 280, 254, style=BOX + SMALL + DOMAIN, parent=g_run,
)
sat_rag = p1.node(
    "Knowledge / Hybrid RAG — src/workflow/{chunking,embeddings,index,retrieval,knowledge}.py\n"
    "BM25 (unigram + bigram) + pgvector, hợp nhất bằng RRF · chi tiết ở trang 4",
    330, 40, 620, 46, style=BOX + SMALL + DOMAIN, parent=g_run,
)
sat_tool = p1.node(
    "Tool Registry & MCP — src/workflow/{tools,registrations,mcp,effects}.py\n"
    "risk level · credential của creator · trong sandbox thì thay bằng mock",
    330, 92, 620, 46, style=BOX + SMALL + DOMAIN, parent=g_run,
)
sat_llm = p1.node(
    "LLM & Tracing — src/services/{llm,tracing}.py\nmột chỗ duy nhất gọi model, một chỗ duy nhất bắn trace",
    330, 144, 620, 46, style=BOX + SMALL + DOMAIN, parent=g_run,
)
sat_appr = p1.node(
    "Human Approval — src/workflow/approvals.py · src/approvals/\n"
    "interrupt() → ghi run_approvals → Command(resume=…) · chi tiết ở trang 3",
    330, 196, 620, 46, style=BOX + SMALL + DOMAIN, parent=g_run,
)
sat_budget = p1.node(
    "Budget & Cost — src/agents/{budget,costs,spend}.py\n"
    "max_total_tokens · max_wall_seconds · ghi tiền thật vào run_costs",
    330, 248, 620, 46, style=BOX + SMALL + DOMAIN, parent=g_run,
)

g_data = p1.group("Dữ liệu", 40, 1079, 1010, 155)
d_pg = p1.node(
    "PostgreSQL 16 + pgvector\nsrc/db/models.py — 18 bảng\nagents · agent_versions · deployments\n"
    "eval_* · run_approvals · run_costs",
    25, 42, 228, 93, style=DB + DATA, parent=g_data,
)
d_chunks = p1.node(
    "document_chunks\nembedding vector(1024)\ncontent_hash · chunker_version\nembedding_model",
    269, 42, 228, 93, style=DB + DATA, parent=g_data,
)
d_ckpt = p1.node(
    "Checkpoint của LangGraph\nbảng checkpoint* — state từng thread\nnền của pause/resume\nvà trí nhớ hội thoại",
    513, 42, 228, 93, style=DB + DATA, parent=g_data,
)
d_files = p1.node(
    "Thư viện tài liệu (trên đĩa)\ndata/workspace/creators/<owner_id>/\nPDF · DOCX · MD · TXT",
    757, 42, 228, 93, style=BOX + SMALL + DATA, parent=g_data,
)

g_legend = p1.group("Chú giải", 1170, 40, 450, 380)
for i, (text, colour) in enumerate(
    [
        ("Client — chạy trong trình duyệt", CLIENT),
        ("API — src/api/, FastAPI, REST + SSE", API),
        ("Domain — nghiệp vụ, không biết gì về HTTP", DOMAIN),
        ("Dữ liệu — Postgres và đĩa", DATA),
        ("Dịch vụ ngoài — gọi qua mạng", EXT),
    ]
):
    p1.node(text, 25, 40 + i * 36, 400, 28, style=BOX + SMALL + colour, parent=g_legend)
p1.node(
    "Nét liền: gọi trong tiến trình.  Nét đứt: gọi ra ngoài qua mạng.\n"
    "Bước 1 … 8 là thứ tự dựng một agent — trang 2 kể kỹ từng bước.\n\n"
    "Số đếm thẳng từ code, không phải ước lượng:\n"
    "84 route (src/api/) · 8 node type (NODE_REGISTRY)\n"
    "8 template (TEMPLATE_REGISTRY) · 18 bảng (src/db/models.py)",
    25, 226, 400, 130, style=BOX + SMALL + NOTE, parent=g_legend,
)

p1.node(
    "Ranh giới của một creator\n\n"
    "Mọi ref tài liệu đi qua đúng một hàm — knowledge.resolved(). Hàm đó vừa chặn "
    "path traversal vừa là ranh giới giữa các creator: hai việc nhưng cùng một quy tắc, "
    "nên không thể sửa chỗ này mà quên chỗ kia.",
    1170, 460, 450, 170, style=BOX + SMALL + NOTE,
)

g_ext = p1.group("Dịch vụ ngoài", 1170, 705, 450, 282)
x_openai = p1.node(
    "OpenAI — chat gpt-4.1 · 4.1-mini · 4o-mini\nembedding text-embedding-3-small (dự phòng, 1024 chiều)",
    25, 40, 400, 46, style=BOX + SMALL + EXT, parent=g_ext,
)
x_voyage = p1.node(
    "Voyage AI\nembedding chính — voyage-4 (1024 chiều)", 25, 92, 400, 46, style=BOX + SMALL + EXT, parent=g_ext
)
x_mcp = p1.node(
    "MCP server: Gmail · Drive · Outlook · Teams\nsandbox dùng mock; prod cần tool đã được duyệt",
    25, 144, 400, 46, style=BOX + SMALL + EXT, parent=g_ext,
)
x_lf = p1.node(
    "Langfuse\ntrace — tuỳ chọn, thiếu key vẫn chạy", 25, 196, 400, 46, style=BOX + SMALL + EXT, parent=g_ext
)

# Client → API: ba cạnh ngắn, khoảng zig nằm gọn trong khe 40px giữa hai tầng.
p1.edge(studio, r_sol, "REST + SSE", exit_=(0.5, 1), entry=(0.5, 0))
p1.edge(webchat, r_run, "SSE", exit_=(0.5, 1), entry=(0.5, 0))
p1.edge(extsys, r_adm, "X-API-Key", exit_=(0.5, 1), entry=(0.5, 0))

# API → Domain: một cạnh khung-với-khung. Route nào gọi module nào thì đã ghi
# ngay trong ô route (→ ①, → ⑤ …), khỏi cần sáu đường cắt chéo.
p1.edge(
    g_api, g_dom, "route gọi domain — chiều ngược lại thì không",
    exit_=(0.5, 1), entry=(0.5, 0),
)

# Đường rắn của tầng dựng agent.
p1.edge(b1, b2, exit_=(1, 0.5), entry=(0, 0.5))
p1.edge(b2, b3, exit_=(1, 0.5), entry=(0, 0.5))
p1.edge(b3, b4, exit_=(1, 0.5), entry=(0, 0.5))
p1.edge(b4, b5, exit_=(0.5, 1), entry=(0.5, 0))
p1.edge(b5, b6, exit_=(0, 0.5), entry=(1, 0.5))
p1.edge(b6, b7, exit_=(0, 0.5), entry=(1, 0.5))
p1.edge(b7, b8, exit_=(0, 0.5), entry=(1, 0.5))
p1.edge(b8, rt, exit_=(0.5, 1), entry=(0.391, 0))

# Runtime → các module: năm đoạn nằm ngang tuyệt đối, cổng ra tính đúng vào
# giữa từng hàng bên phải.
for satellite, exit_y in [
    (sat_rag, 0.091),
    (sat_tool, 0.295),
    (sat_llm, 0.5),
    (sat_appr, 0.705),
    (sat_budget, 0.909),
]:
    p1.edge(rt, satellite, exit_=(1, exit_y), entry=(0, 0.5))

p1.edge(g_dom, g_data, "SQLAlchemy 2.0 async — src/db/", exit_=(0.5, 1), entry=(0.5, 0))

# Domain → dịch vụ ngoài: năm đường đứt, mỗi đường một làn dọc riêng trong khe
# 1050..1170 nên không đường nào chồng đường nào.
p1.edge(sat_rag, x_openai, style=EDGE_DASH, exit_=(1, 0.35), entry=(0, 0.3), points=[(1060, 761)])
p1.edge(sat_rag, x_voyage, style=EDGE_DASH, exit_=(1, 0.75), entry=(0, 0.4), points=[(1082, 780)])
p1.edge(sat_tool, x_mcp, style=EDGE_DASH, exit_=(1, 0.5), entry=(0, 0.4), points=[(1104, 820)])
p1.edge(sat_llm, x_openai, style=EDGE_DASH, exit_=(1, 0.35), entry=(0, 0.75), points=[(1126, 865)])
p1.edge(sat_llm, x_lf, style=EDGE_DASH, exit_=(1, 0.7), entry=(0, 0.5), points=[(1148, 881)])

# ─────────────────────── Trang 2: Luồng dựng agent ───────────────────────
#
# 18 bước, bốn hàng, đổi chiều mỗi hàng. Chỗ xuống hàng là một đoạn thẳng đứng ở
# rìa trang thay vì một cú quét ngang. Chip nét đứt dưới mỗi ô là AgentVersion.status.
p2 = Page("2. Data flow — dựng agent", 1720, 940)
p2.node(
    "Dựng một agent: từ một câu mô tả tới một Deployment chạy thật.\n"
    "Chip nét đứt dưới mỗi bước là AgentVersion.status sau bước đó — chính nó chặn không cho nhảy cóc.",
    40, 30, 900, 60, style=BOX + NOTE,
)

COL = [40, 376, 712, 1048, 1384]
W = 296
ROW = [140, 350, 560, 770]


def step(page: Page, text: str, col: int, row: int, *, style: str, state: str = "") -> str:
    """Một bước của luồng, kèm chip trạng thái nếu bước đó đổi status."""
    ident = page.node(text, COL[col], ROW[row], W, 80, style=style)
    if state:
        page.node(state, COL[col], ROW[row] + 86, 200, 26, style=BOX + TINY + NOTE)
    return ident


s1 = step(p2, "Nhân viên mô tả bài toán\nbằng ngôn ngữ tự nhiên", 0, 0, style=ROUND + SMALL + CLIENT)
s2 = step(p2, "POST /solution-interviews\ntrích 7 slot, chỉ ra chỗ thiếu / mâu thuẫn", 1, 0, style=BOX + SMALL + API)
s3 = step(p2, "POST …/answers\nhỏi từng câu cho tới khi đủ 7 slot", 2, 0, style=BOX + SMALL + API)
s4 = step(
    p2,
    "POST …/spec → AgentSolutionSpecification\ngoal · users · data · actions · outcome ·\nsuccess_criteria · prohibitions",
    3, 0, style=BOX + SMALL + DOMAIN, state="spec_pending_confirmation",
)
s5 = step(p2, "Người tạo rà lại 7 trường\nPUT solution-spec — gắn ref tài liệu", 4, 0, style=BOX + SMALL + CLIENT)

s6 = step(p2, "POST …/solution-spec/confirmation", 4, 1, style=BOX + SMALL + API, state="spec_confirmed")
s7 = step(
    p2, "POST …/workflow\nchọn 1 trong 8 template rồi để AI\nđiền cấu hình từng node",
    3, 1, style=BOX + SMALL + DOMAIN, state="workflow_pending_confirmation",
)
s8 = step(
    p2, "Canvas: kéo thả hoặc sửa bằng lời\nPUT …/workflow · POST …/workflow/refine\nvalidation trả problems[]",
    2, 1, style=BOX + SMALL + CLIENT,
)
s9 = step(p2, "POST …/workflow/confirm", 1, 1, style=BOX + SMALL + API, state="workflow_confirmed")
s10 = step(
    p2, "POST …/workflow-sandbox-runs\nchạy thật, tool có side effect dùng mock,\nghi transcript từng node",
    0, 1, style=BOX + SMALL + DOMAIN, state="sandbox_passed",
)

s11 = step(
    p2, "POST …/workflow-eval-cases/generate\nsinh case từ success_criteria + prohibitions", 0, 2,
    style=BOX + SMALL + DOMAIN,
)
s12 = step(
    p2, "POST …/workflow-eval-runs\nrule-based check + LLM judge\n→ pass_rate", 1, 2, style=BOX + SMALL + DOMAIN
)
s13 = step(p2, "POST …/approval-request\ngửi bản này đi duyệt", 2, 2, style=BOX + SMALL + API, state="pending_review")
s14 = step(
    p2, "Gate: pass_rate ≥ threshold?\nkhông đạt thì admin phải override", 3, 2, style=DECIDE + TINY + GATE
)
s15 = step(
    p2, "Admin đọc hồ sơ duyệt\nGET /admin/approvals/{version_id}\nspec · workflow · eval · tool rủi ro", 4, 2,
    style=BOX + SMALL + CLIENT,
)

s16 = step(
    p2, "POST /admin/approvals/{id}/approve\nhoặc /reject kèm lý do", 4, 3, style=BOX + SMALL + API, state="approved"
)
s17 = step(
    p2, "PUT /agents/{id}/deployed-version\ntạo Deployment gắn cứng một version", 3, 3, style=BOX + SMALL + DOMAIN
)
s18 = step(
    p2, "Deployment chạy thật\nweb chat (JWT) · api key · rate limit\naccess: private / selected_users", 2, 3,
    style=ROUND + SMALL + DATA,
)

for a, b in [(s1, s2), (s2, s3), (s3, s4), (s4, s5)]:
    p2.edge(a, b, exit_=(1, 0.5), entry=(0, 0.5))
p2.edge(s5, s6, exit_=(0.85, 1), entry=(0.85, 0))
for a, b in [(s6, s7), (s7, s8), (s8, s9), (s9, s10)]:
    p2.edge(a, b, exit_=(0, 0.5), entry=(1, 0.5))
p2.edge(s10, s11, exit_=(0.85, 1), entry=(0.85, 0))
for a, b in [(s11, s12), (s12, s13), (s13, s14)]:
    p2.edge(a, b, exit_=(1, 0.5), entry=(0, 0.5))
p2.edge(s14, s15, "đạt", exit_=(1, 0.5), entry=(0, 0.5))
p2.edge(s15, s16, exit_=(0.85, 1), entry=(0.85, 0))
for a, b in [(s16, s17), (s17, s18)]:
    p2.edge(a, b, exit_=(0, 0.5), entry=(1, 0.5))

# Hai vòng quay lại. Cả hai đi vòng qua dải trống giữa hai hàng, không cắt ô nào.
p2.edge(
    s8, s7, "chưa ưng — sinh lại workflow", style=EDGE_DASH,
    exit_=(0.75, 0), entry=(0.25, 0), points=[(934, 300), (1122, 300)],
)
p2.edge(
    s14, s11, "không đạt — sửa rồi eval lại", style=EDGE_DASH,
    exit_=(0.5, 1), entry=(0.5, 1), points=[(1196, 700), (188, 700)],
)

# ─────────────────────── Trang 3: Runtime ───────────────────────
#
# Ba dải: lượt chạy bình thường, nhánh dừng-chờ-duyệt, và những thứ ghi ra ngoài
# ở mọi bước. Cạnh giữa các dải đi vòng bên trái (x=30) nên không xuyên qua dải giữa.
p3 = Page("3. Data flow — runtime workflow", 1620, 780)
p3.node(
    "Runtime: mỗi node trong Workflow JSON ánh xạ tới một hàm, tất cả compile thành một đồ thị LangGraph.\n"
    "State đi qua mọi node; mỗi bước vừa phát sự kiện SSE ra client vừa ghi vào transcript.",
    60, 30, 1000, 55, style=BOX + NOTE,
)

g_a = p3.group("Một lượt chạy bình thường — state đi qua từng node", 60, 110, 1520, 200)
a1 = p3.node("Câu hỏi người dùng\n+ conversation_id", 20, 70, 180, 95, style=ROUND + SMALL + CLIENT, parent=g_a)
a2 = p3.node(
    "compile_workflow_graph()\nWorkflowJSON → StateGraph\nstate: messages · findings · searched",
    230, 70, 260, 95, style=BOX + SMALL + DOMAIN, parent=g_a,
)
a3 = p3.node(
    "knowledge_search\ntra tài liệu đã gắn\n→ findings[]", 520, 70, 180, 95, style=BOX + SMALL + API, parent=g_a
)
a4 = p3.node(
    "agent_llm\nprompt + evidence\nstream từng token", 730, 70, 180, 95, style=BOX + SMALL + API, parent=g_a
)
a5 = p3.node(
    "if_else · while\nđiều kiện viết bằng lời\nmỗi port một nhánh", 940, 70, 190, 95, style=DECIDE + TINY + GATE, parent=g_a
)
a6 = p3.node(
    "mcp · web_search\nmcp là node duy nhất\ncó side effect", 1160, 70, 180, 95, style=BOX + SMALL + API, parent=g_a
)
a7 = p3.node("end\nchốt câu trả lời\ncủa một nhánh", 1370, 70, 130, 95, style=ROUND + SMALL + CLIENT, parent=g_a)

g_b = p3.group("Khi chạm node human_approval — dừng hẳn, rồi chạy tiếp trong cùng một run", 60, 345, 1390, 150)
bb1 = p3.node("human_approval\ninterrupt() dừng đồ thị", 20, 45, 230, 85, style=BOX + SMALL + GATE, parent=g_b)
bb2 = p3.node(
    "ghi run_approvals + park budget ledger\nSSE awaiting_approval rồi đóng request",
    250, 45, 270, 85, style=BOX + SMALL + DATA, parent=g_b,
)
bb3 = p3.node(
    "Người duyệt quyết định\nPOST …/workflow-approvals/{id}", 540, 45, 270, 85, style=BOX + SMALL + CLIENT, parent=g_b
)
bb4 = p3.node(
    "Command(resume=…)\nchạy tiếp từ checkpoint,\nvẫn là run cũ chứ không phải run mới",
    830, 45, 270, 85, style=BOX + SMALL + DOMAIN, parent=g_b,
)
bb5 = p3.node(
    "approve → nhánh 'approved', hành động thật chạy\nreject → nhánh 'rejected'",
    1120, 45, 250, 85, style=BOX + SMALL + API, parent=g_b,
)

g_c = p3.group("Mỗi bước đều ghi ra ngoài", 60, 530, 1520, 165)
c1 = p3.node(
    "Sự kiện SSE ra client\nnode_started · token · tool · tool_result\nnode · usage · cost · budget_exceeded",
    20, 45, 420, 100, style=BOX + SMALL + DATA, parent=g_c,
)
c2 = p3.node(
    "checkpoints (Postgres)\nstate từng thread — nền của resume", 470, 45, 330, 100, style=DB + DATA, parent=g_c
)
c3 = p3.node("run_costs\ntoken và tiền thật của lần chạy", 830, 45, 330, 100, style=DB + DATA, parent=g_c)
c4 = p3.node(
    "Budget ledger\nmax_total_tokens · max_wall_seconds\nvượt trần → dừng và ghi rõ lý do",
    1190, 45, 330, 100, style=BOX + SMALL + DOMAIN, parent=g_c,
)

for a, b in [(a1, a2), (a2, a3), (a3, a4), (a4, a5), (a5, a6), (a6, a7)]:
    p3.edge(a, b, exit_=(1, 0.5), entry=(0, 0.5))
p3.edge(
    a5, a4, "while: quay lại thân vòng", style=EDGE_DASH,
    exit_=(0.5, 0), entry=(0.5, 0), points=[(1095, 162), (880, 162)],
)
p3.edge(g_a, g_b, "đồ thị có node human_approval", style=EDGE_DASH, exit_=(0.15, 1), entry=(0.08, 0))
p3.edge(bb5, a6, "chạy tiếp", exit_=(0.5, 0), entry=(0.9, 1))
p3.edge(g_a, g_c, "mỗi node, mỗi lần", exit_=(0.96, 1), entry=(0.96, 0))

# ─────────────────────── Trang 4: RAG ───────────────────────
#
# Hai nửa của knowledge_search, gặp nhau ở đúng một bảng đặt giữa trang.
p4 = Page("4. Data flow — Hybrid RAG (ADR-0045)", 1660, 820)
p4.node(
    "Hai nửa của một node knowledge_search: đưa tài liệu vào chỉ mục (chạy khi tài liệu đổi) và trả lời một câu hỏi\n"
    "(chạy mỗi lượt). Hai nửa gặp nhau ở đúng một bảng — document_chunks.",
    40, 30, 1000, 55, style=BOX + NOTE,
)

g_idx = p4.group("A · Đưa tài liệu vào chỉ mục — chạy lại khi tài liệu đổi", 40, 110, 520, 585)
i1 = p4.node("POST /api/v1/documents\nPDF · DOCX · MD · TXT", 20, 45, 480, 60, style=BOX + SMALL + API, parent=g_idx)
i2 = p4.node(
    "Bóc văn bản — pypdf (PDF) · python-docx (DOCX)\nDOCX gộp ô của một hàng bảng bằng ' · '",
    20, 135, 480, 70, style=BOX + SMALL + DOMAIN, parent=g_idx,
)
i3 = p4.node(
    "Cắt đoạn ngữ nghĩa — semantic_chunks_of()\ncắt theo tiêu đề Markdown và Điều / CHƯƠNG,\n"
    "rồi cắt tiếp ở chỗ ý nghĩa đổi (percentile 85)",
    20, 235, 480, 90, style=BOX + SMALL + DOMAIN, parent=g_idx,
)
i4 = p4.node(
    "Embed từng đoạn\ntext-embedding-3-small · 1024 chiều", 20, 355, 480, 70, style=BOX + SMALL + DOMAIN, parent=g_idx
)
p4.node(
    "Chỉ mục tự vô hiệu khi một trong ba thứ đổi:\nhash nội dung tệp · CHUNKER_VERSION · tên model embedding.\n\n"
    "Vector của hai model khác nhau không so được với nhau, kể cả gần đúng — nên đổi model là lập lại chỉ mục, "
    "không có đường tắt.",
    20, 455, 480, 110, style=BOX + SMALL + NOTE, parent=g_idx,
)

p4.node(
    "Vì sao phải lai hai kiểu tìm?\n\n"
    "BM25 bắt đúng từ khoá hiếm — mã số, tên điều, tên riêng. Embedding bắt ý khi người hỏi "
    "dùng chữ khác hẳn tài liệu. Thiếu nửa nào cũng hụt một loại câu hỏi.",
    600, 130, 300, 150, style=BOX + SMALL + NOTE,
)
store = p4.node(
    "document_chunks\ntext · heading_path · embedding vector(1024)\ncontent_hash · chunker_version · embedding_model",
    600, 320, 300, 140, style=DB + DATA,
)
p4.node(
    "Ngưỡng điểm > 0 cho phép trả về RỖNG.\n\n"
    "Đó là thứ giúp agent nói 'quy chế không quy định' thay vì trích đại một đoạn không liên quan. "
    "Im lặng đúng còn hơn trả lời sai.",
    600, 500, 300, 160, style=BOX + SMALL + NOTE,
)

g_q = p4.group(
    "B · Trả lời một câu hỏi — đọc chunk của đúng các ref đã gắn, chạy mỗi lượt", 940, 110, 680, 625
)
q1 = p4.node(
    "Câu hỏi của người dùng\n_retrieval_query()", 200, 45, 280, 56, style=ROUND + SMALL + CLIENT, parent=g_q
)
q2 = p4.node(
    "BM25 trong tiến trình\nunigram + bigram tiếng Việt\nIDF tính trên đúng tập tài liệu được gắn",
    20, 140, 290, 95, style=BOX + SMALL + DOMAIN, parent=g_q,
)
q3 = p4.node(
    "pgvector\nORDER BY embedding <=> :vector\ncosine, quét thẳng (chưa có ANN index)",
    370, 140, 290, 95, style=BOX + SMALL + DATA, parent=g_q,
)
q4 = p4.node(
    "Hợp nhất Reciprocal Rank Fusion\nfuse(lexical[:20], semantic[:20])\nchỉ đọc thứ hạng, khỏi cân trọng số",
    200, 275, 280, 90, style=BOX + SMALL + DOMAIN, parent=g_q,
)
q5 = p4.node(
    "Lọc lần cuối\nbỏ đoạn trùng (Jaccard ≥ 0.85)\ntối đa 2 đoạn mỗi mục · lấy 6 đoạn",
    200, 405, 280, 90, style=BOX + SMALL + DOMAIN, parent=g_q,
)
q6 = p4.node(
    "6 passage kèm [source_ref]\nghép vào prompt của node agent_llm",
    200, 535, 280, 70, style=ROUND + SMALL + CLIENT, parent=g_q,
)

for a, b in [(i1, i2), (i2, i3), (i3, i4)]:
    p4.edge(a, b, exit_=(0.5, 1), entry=(0.5, 0))
p4.edge(i4, store, "ghi 1 lần", exit_=(1, 0.5), entry=(0, 0.7))
p4.edge(store, g_q, exit_=(1, 0.5), entry=(0, 0.35))
p4.edge(q1, q2, "từ khoá", exit_=(0.25, 1), entry=(0.5, 0))
p4.edge(q1, q3, "vector câu hỏi", exit_=(0.75, 1), entry=(0.5, 0))
p4.edge(q2, q4, "top 20", exit_=(0.5, 1), entry=(0.25, 0))
p4.edge(q3, q4, "top 20", exit_=(0.5, 1), entry=(0.75, 0))
p4.edge(q4, q5, exit_=(0.5, 1), entry=(0.5, 0))
p4.edge(q5, q6, exit_=(0.5, 1), entry=(0.5, 0))

pages = "".join(page.render() for page in (p1, p2, p3, p4))
OUT.write_text(
    '<mxfile host="app.diagrams.net" agent="Agent-Builder" version="24.7.17" type="device">' + pages + "</mxfile>",
    encoding="utf-8",
)
# Console Windows hay là cp1258, không in nổi tiếng Việt có dấu — nên dòng báo
# kết quả cố tình viết không dấu, khỏi chết ở bước cuối vì một cái print.
print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(pages.split('<diagram')) - 1} pages)")
