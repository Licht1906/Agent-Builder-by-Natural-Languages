import type { CSSProperties } from 'react';
import './AgentLabMascot.css';

export type MascotState =
  | 'idle'
  | 'welcome'
  | 'thinking'
  | 'processing'
  | 'exploring'
  | 'success'
  | 'alert'
  | 'builder'
  | 'rest';

export interface AgentLabMascotProps {
  /** A named animation that matches one product state. */
  state?: MascotState;
  /** CSS size for the square mascot canvas. */
  size?: number | string;
  /** Where the two PNG layers are served from (for example /agentlab-mascot). */
  assetBase?: string;
  className?: string;
  label?: string;
  reducedMotion?: boolean;
}

const labels: Record<MascotState, string> = {
  idle: 'AgentLab mascot is ready',
  welcome: 'AgentLab mascot welcomes the user',
  thinking: 'AgentLab mascot is thinking',
  processing: 'AgentLab mascot is processing a task',
  exploring: 'AgentLab mascot is exploring an agent flow',
  success: 'AgentLab mascot celebrates success',
  alert: 'AgentLab mascot signals attention is needed',
  builder: 'AgentLab mascot builds an agent from the AL blueprint',
  rest: 'AgentLab mascot is resting',
};

export function AgentLabMascot({
  state = 'idle',
  size = 220,
  assetBase = '/agentlab-mascot',
  className = '',
  label,
  reducedMotion = false,
}: AgentLabMascotProps) {
  const style = {
    '--mascot-size': typeof size === 'number' ? `${size}px` : size,
  } as CSSProperties;

  return (
    <div
      className={`al-mascot al-mascot--${state}${reducedMotion ? ' al-mascot--static' : ''} ${className}`.trim()}
      style={style}
      role="img"
      aria-label={label ?? labels[state]}
    >
      <span className="al-mascot__halo" aria-hidden="true" />
      <span className="al-mascot__orbit" aria-hidden="true" />
      <span className="al-mascot__wave al-mascot__wave--one" aria-hidden="true" />
      <span className="al-mascot__wave al-mascot__wave--two" aria-hidden="true" />

      <div className="al-mascot__blueprint" aria-hidden="true">
        <img src={`${assetBase}/agentlab-blueprint.png`} alt="" />
      </div>

      <div className="al-mascot__node-system" aria-hidden="true">
        <i />
        <i />
        <i />
        <b />
        <b />
      </div>

      <div className="al-mascot__robot" aria-hidden="true">
        <img src={`${assetBase}/agentlab-mascot.png`} alt="" />
        <span className="al-mascot__blink" />
      </div>

      <div className="al-mascot__sparkles" aria-hidden="true">
        <i />
        <i />
        <i />
      </div>
      <span className="al-mascot__alert-ring" aria-hidden="true" />
      <span className="al-mascot__sleep-z al-mascot__sleep-z--one" aria-hidden="true">z</span>
      <span className="al-mascot__sleep-z al-mascot__sleep-z--two" aria-hidden="true">z</span>
    </div>
  );
}
