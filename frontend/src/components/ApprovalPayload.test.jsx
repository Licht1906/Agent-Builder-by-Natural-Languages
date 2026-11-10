import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import ApprovalPayload from "./ApprovalPayload";
import { LanguageProvider } from "../i18n";

describe("an approval card's payload", () => {
  it("lays a database write out as the change it is, every value verbatim", () => {
    render(
      <LanguageProvider>
        <ApprovalPayload
          actions={[
            {
              node_id: "write",
              tool: "data_write",
              action: "update",
              args: {
                source_id: "4",
                table: "ar_notes",
                operation: "update",
                values: { note: "hẹn trả 20/11" },
                where: [{ column: "customer_id", value: "C1" }],
              },
            },
          ]}
        />
      </LanguageProvider>,
    );

    expect(screen.getByText("Edit rows")).toBeInTheDocument();
    expect(screen.getByText("ar_notes")).toBeInTheDocument();
    expect(screen.getByText('note = "hẹn trả 20/11"')).toBeInTheDocument();
    expect(screen.getByText('customer_id = "C1"')).toBeInTheDocument();
  });
});
