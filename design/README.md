# AgentLab animated mascot for React

This package contains a transparent robot layer, a separate `AL` blueprint layer, and a dependency-free React/CSS component. The mascot is animated in the browser, so the app can choose the right animation for each product state without loading GIFs.

## Install

Copy `assets/` into your project's `public/agentlab-mascot/` directory. Copy the three TypeScript/CSS files into your components folder.

```tsx
import { AgentLabMascot } from './AgentLabMascot';

export function AgentRunStatus({ status }: { status: 'idle' | 'running' | 'done' }) {
  const state = status === 'running' ? 'processing' : status === 'done' ? 'success' : 'idle';
  return <AgentLabMascot state={state} size={176} />;
}
```

## States

| State | Use in the product |
| --- | --- |
| `idle` | Empty state, waiting for input |
| `welcome` | Onboarding and greeting |
| `thinking` | Clarifying intent or preparing a plan |
| `processing` | Agent is running a task |
| `exploring` | Inspecting nodes, tools, or a workflow |
| `success` | Task has completed successfully |
| `alert` | Permission, error, or user action is required |
| `builder` | Creating or editing an agent; the AL blueprint appears behind the robot |
| `rest` | Paused, offline, or no active task |

## Props

```tsx
<AgentLabMascot
  state="builder"
  size={240}
  assetBase="/agentlab-mascot"
  reducedMotion={false}
/>
```

`assetBase` defaults to `/agentlab-mascot`. Set `reducedMotion` to `true` when your own app preference requires static artwork.
