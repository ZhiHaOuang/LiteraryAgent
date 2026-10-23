
import type { FC } from 'react';
import './LayoutComponents.css';

/**
 * Props for ToolCallContainer
 */
export interface ToolCallContainerProps {
  /** Operation label (e.g., "Read", "Write", "Search") */
  label: string;
  /** Status for bullet color: 'success' | 'error' | 'warning' | 'loading' | 'default' */
  status?: 'success' | 'error' | 'warning' | 'loading' | 'default';
  /** Main content to display (optional - some tool calls only show title) */
  children?: React.ReactNode;
  /** Tool call ID for debugging */
  toolCallId?: string;
  /** Optional trailing content rendered next to label (e.g., clickable filename) */
  labelSuffix?: React.ReactNode;
  /** Optional custom class name */
  className?: string;
  /** Whether this is the first item in an AI response sequence (for timeline) */
  isFirst?: boolean;
  /** Whether this is the last item in an AI response sequence (for timeline) */
  isLast?: boolean;
}

/**
 * ToolCallContainer - Main container for tool call displays
 * Features timeline connector line and status bullet
 */
export const ToolCallContainer: FC<ToolCallContainerProps> = ({
  label,
  status = 'success',
  children,
  toolCallId: _toolCallId,
  labelSuffix,
  className: _className,
  isFirst = false,
  isLast = false,
}) => (
  <div
    className={`lg-message message-item ${_className || ''} relative pl-[30px] py-2 select-text toolcall-container toolcall-status-${status}`}
    data-first={isFirst}
    data-last={isLast}
  >
    <div className="toolcall-content-wrapper flex flex-col min-w-0 max-w-full">
      <div className="flex items-baseline gap-1.5 relative min-w-0">
        <span className="text-[14px] leading-none font-bold text-[var(--app-primary-foreground)]">
          {label}
        </span>
        <span className="text-[11px] text-[var(--app-secondary-foreground)]">
          {labelSuffix}
        </span>
      </div>
      {children && (
        <div className="text-[var(--app-secondary-foreground)]">{children}</div>
      )}
    </div>
  </div>
);

/**
 * Props for CodeBlock
 */
interface CodeBlockProps {
  children: string;
}

/**
 * CodeBlock - Code block for displaying formatted code or output
 */
export const CodeBlock: FC<CodeBlockProps> = ({ children }) => (
  <pre className="font-mono text-[var(--app-monospace-font-size)] bg-[var(--app-primary-background)] border border-[var(--app-input-border)] rounded-small p-medium overflow-x-auto mt-1 whitespace-pre-wrap break-words max-h-[300px] overflow-y-auto">
    {children}
  </pre>
);
