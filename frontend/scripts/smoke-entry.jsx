/* Render every screen once, server-side, and report anything that throws.
 *
 * `vite build` type-checks nothing and executes nothing, so a component that
 * references an identifier it never imported builds perfectly and throws the
 * moment it renders. That is not hypothetical: an `import { useLang }` line was
 * once inserted *inside* a block comment in `AuthShell`, the build stayed green,
 * and the sign-in screen went white.
 *
 * `renderToString` never runs effects, so nothing here talks to the API — this
 * checks the render path only, which is exactly the part the build cannot.
 */

import { renderToString } from "react-dom/server";
import { LanguageProvider } from "../src/i18n";

import LandingPage from "../src/pages/Landing";
import LoginPage from "../src/pages/Login";
import RegisterPage from "../src/pages/Register";
import Dashboard from "../src/pages/Dashboard";
import DesignerPage from "../src/pages/Designer";
import SpecPage from "../src/pages/Spec";
import BuilderPage from "../src/pages/Builder";
import ChatPage from "../src/pages/Chat";
import { AdminQueue, AdminDossier } from "../src/pages/AdminApprovals";
import AdminSettings from "../src/pages/AdminSettings";
import AppLayout from "../src/components/AppLayout";
import ConfirmDialog from "../src/components/ConfirmDialog";
import Inspector from "../src/pages/builder/Inspector";
import GraphChat from "../src/pages/builder/GraphChat";
import SandboxTab from "../src/pages/builder/SandboxTab";
import EvaluationTab from "../src/pages/builder/EvaluationTab";
import DeployTab from "../src/pages/builder/DeployTab";
import OperationsTab from "../src/pages/builder/OperationsTab";
import VersionCompare from "../src/pages/builder/VersionCompare";
import BuilderCanvas from "../src/pages/builder/Canvas";

const noop = () => {};
const session = { email: "creator@example.com", role: "creator" };
const agent = { id: 1, name: "Test agent", owner_email: "creator@example.com" };
const workflow = {
  start_node_id: "a",
  nodes: [{ id: "a", type: "agent_llm", config: { model: "gpt-4o", prompt: "hi" }, position: { x: 0, y: 0 } }],
  edges: [],
  metadata: {},
};

/* Every screen, with the props it is given in `App`. Two renders each — one per
   language — because a missing translation key and a broken `t` call are
   different bugs and only the second language finds the second one. */
const SCREENS = [
  ["Landing", <LandingPage goLogin={noop} goRegister={noop} />],
  ["Login", <LoginPage onSignedIn={noop} goLanding={noop} goRegister={noop} />],
  ["Register", <RegisterPage onSignedIn={noop} goLanding={noop} goLogin={noop} />],
  ["AppLayout", <AppLayout session={session} view="dashboard" onLogout={noop} onReload={noop}>x</AppLayout>],
  ["Dashboard", <Dashboard openAgent={noop} openChat={noop} startDesigner={noop} notify={noop} />],
  ["Designer", <DesignerPage onSpecReady={noop} />],
  ["Spec", <SpecPage agentId={1} versionId={1} agentName="A" openBuilder={noop} notify={noop} />],
  ["Builder", <BuilderPage agent={agent} versionId={1} setVersionId={noop} notify={noop} />],
  ["Chat", <ChatPage agent={agent} />],
  ["AdminQueue", <AdminQueue openVersion={noop} />],
  ["AdminDossier", <AdminDossier versionId={1} back={noop} notify={noop} />],
  ["AdminSettings", <AdminSettings notify={noop} />],
  ["ConfirmDialog", <ConfirmDialog open title="T" body="B" confirmLabel="C" onConfirm={noop} onCancel={noop} />],
  ["Inspector-empty", <Inspector node={null} problems={[]} tools={[]} onRename={noop} onConfig={noop} onDelete={noop} onMakeStart={noop} />],
  [
    "Inspector-node",
    <Inspector
      node={workflow.nodes[0]}
      schema={{ properties: { prompt: { type: "string", multiline: true }, model: { type: "string" } } }}
      problems={[]}
      tools={[]}
      onRename={noop}
      onConfig={noop}
      onDelete={noop}
      onMakeStart={noop}
    />,
  ],
  ["GraphChat", <GraphChat agentId={1} versionId={1} workflow={workflow} onApply={noop} notify={noop} />],
  ["SandboxTab", <SandboxTab agentId={1} versionId={1} onActiveNode={noop} onVersionChanged={noop} notify={noop} />],
  ["EvaluationTab", <EvaluationTab agentId={1} versionId={1} notify={noop} />],
  ["DeployTab", <DeployTab agentId={1} versionId={1} versionStatus="approved" onVersionsChanged={noop} notify={noop} />],
  ["OperationsTab", <OperationsTab agentId={1} notify={noop} />],
  ["VersionCompare", <VersionCompare agentId={1} versions={[{ id: 1, status: "draft" }, { id: 2, status: "draft" }]} />],
  [
    "Canvas",
    <BuilderCanvas
      workflow={workflow}
      nodeTypes={{ agent_llm: { type: "agent_llm", outputs: ["done"], config_schema: {} } }}
      selectedId={null}
      setSelectedId={noop}
      changedNodeIds={new Set()}
      problemNodeIds={new Set()}
      onMoveNode={noop}
      onConnect={noop}
      onDeleteEdge={noop}
    />,
  ],
];

export function run() {
  const failures = [];
  for (const lang of ["en", "vi"]) {
    for (const [name, element] of SCREENS) {
      try {
        renderToString(<LanguageProvider key={lang}>{element}</LanguageProvider>);
      } catch (error) {
        failures.push(`${name} [${lang}]: ${error.message}`);
      }
    }
  }
  return { count: SCREENS.length * 2, failures };
}
