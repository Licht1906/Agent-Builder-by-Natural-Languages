import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import Inspector from "./Inspector";

/* The MCP node has a panel of its own (it configures a *connected service*,
 * not a list of fields), so these read it through that panel. What they pin is
 * unchanged and is not about layout: the approval control offers exactly the
 * two values the backend accepts, defaults to `auto` when nothing was stored,
 * and reports the enum member rather than its sentence.
 *
 * `require_approval` is the case that broke the assumption. Pydantic writes an
 * enum-typed field as a `$ref` into `$defs`, so a panel reading only the
 * property sees no `type` and no `enum` and falls through to a free-text box —
 * a box whose every value but two the backend rejects. These tests pin the
 * reference-following, not the field. */

/** `NODE_REGISTRY[NodeType.MCP].config_schema`, as `GET /workflow/node-types`
 *  serves it. Copied rather than derived because a frontend test cannot import
 *  the Pydantic model — the same trade every fixture in this suite makes. */
const MCP_SCHEMA = {
  $defs: {
    McpApproval: {
      enum: ["auto", "always"],
      title: "McpApproval",
      type: "string",
    },
  },
  properties: {
    tool_id: { default: "", description: "Id của ToolDefinition", resource_ref: true, type: "string" },
    action: { default: "", description: "Tên action", resource_ref: true, type: "string" },
    require_approval: {
      $ref: "#/$defs/McpApproval",
      default: "auto",
      description: "'auto' để nền tảng tự quyết theo mức rủi ro; 'always' để luôn bắt buộc có người duyệt",
      optional: true,
    },
  },
  title: "McpConfig",
  type: "object",
};

const TOOLS = [
  {
    id: "reg:1",
    name: "CRM nội bộ",
    actions: [
      { name: "list_campaigns", risk_level: "low", read_only: true, requires_human_approval: false },
      { name: "send_campaign", risk_level: "high", read_only: false, requires_human_approval: true },
    ],
  },
];

function panel(config = {}, overrides = {}) {
  const props = {
    node: { id: "act", type: "mcp", config, position: { x: 0, y: 0 } },
    schema: MCP_SCHEMA,
    problems: [],
    tools: TOOLS,
    readOnly: false,
    isStart: false,
    metadata: { disclaimer: "" },
    onRename: vi.fn(),
    onConfig: vi.fn(),
    onDelete: vi.fn(),
    onMakeStart: vi.fn(),
    onMetadata: vi.fn(),
    ...overrides,
  };
  render(<Inspector {...props} />);
  return props;
}

/** The control under a given field label.
 *
 *  Not `getByLabelText`: the panel's `<label>` carries no `htmlFor`, so nothing
 *  associates it with its input and Testing Library cannot follow it. Worth
 *  knowing rather than working around silently — it means a screen reader
 *  cannot announce these fields either, which is a real defect and a wider fix
 *  than this change. Going through the label's own wrapper keeps the query
 *  honest about which field it means, where an index into every `<select>` on
 *  the panel would move the day a field is added above it. */
function fieldControl(labelText) {
  const label = screen.getByText(labelText, { selector: "label" });
  return label.parentElement.querySelector("select, input, textarea");
}

/** The panel shows one summary row; the controls live in the centred dialog it
 *  opens. Every assertion below is about those controls, so every test walks
 *  through the same door a person does. */
async function openActions() {
  await userEvent.click(screen.getByRole("button", { name: /Actions & approval/ }));
}

function approvalSelect() {
  return fieldControl("Approval");
}

describe("Inspector, enum config fields", () => {
  it("draws a closed set of values as a closed set of choices", async () => {
    panel({ tool_id: "reg:1", action: "list_campaigns" });
    await openActions();

    const select = approvalSelect();
    expect(select.tagName).toBe("SELECT");
    expect([...select.options].map((option) => option.value)).toEqual(["auto", "always"]);
  });

  it("shows the schema default when the stored config has never set the field", async () => {
    /* The regression this mirrors on the backend: a node drawn before the field
     * existed stores no value at all, and it means `auto` — not "unset". A
     * blank control here would invite the creator to fix something that is
     * already right. */
    panel({ tool_id: "reg:1", action: "list_campaigns" });
    await openActions();

    expect(approvalSelect().value).toBe("auto");
  });

  it("shows what was stored when the creator has chosen", async () => {
    panel({ tool_id: "reg:1", action: "list_campaigns", require_approval: "always" });
    await openActions();

    expect(approvalSelect().value).toBe("always");
  });

  it("offers no way to switch the approval gate off", async () => {
    /* `never` is a value Agent Builder's MCP node has and this platform refuses:
     * it would let the party being gated lift the gate. The form must not offer
     * a choice the backend rejects — that is a dead end with no message. */
    panel({ tool_id: "reg:1", action: "list_campaigns" });
    await openActions();

    expect([...approvalSelect().options].map((option) => option.value)).not.toContain("never");
  });

  it("reports the chosen value up as the enum member, not as its label", async () => {
    const props = panel({ tool_id: "reg:1", action: "list_campaigns" });
    await openActions();

    await userEvent.selectOptions(approvalSelect(), "always");

    expect(props.onConfig).toHaveBeenCalledWith("act", { require_approval: "always" });
  });

  it("labels the choices as sentences rather than as enum members", async () => {
    panel({ tool_id: "reg:1", action: "list_campaigns" });
    await openActions();

    expect(approvalSelect().textContent).toContain("Always require a person to approve");
  });

  it("leaves the fields that are not enums alone", async () => {
    /* `tool_id` and `action` keep their own rendering — they address a
     * registered connection, and the enum branch must not have swallowed them. */
    panel({ tool_id: "reg:1", action: "list_campaigns" });
    await openActions();

    expect([...fieldControl("Action").options].map((option) => option.value)).toEqual([
      "",
      "list_campaigns",
      "send_campaign",
    ]);
  });
});
