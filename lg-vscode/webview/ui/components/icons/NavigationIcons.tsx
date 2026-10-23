
import type { FC } from 'react';
import type { IconProps } from './types.js';

/**
 * Arrow up icon (20x20)
 * Used for send message button
 */
export const ArrowUpIcon: FC<IconProps> = ({
  size = 20,
  className,
  ...props
}) => (
  <svg
    xmlns="http://www.w3.org/2000/svg"
    viewBox="0 0 20 20"
    fill="currentColor"
    width={size}
    height={size}
    className={className}
    aria-hidden="true"
    {...props}
  >
    <path
      fillRule="evenodd"
      d="M10 17a.75.75 0 0 1-.75-.75V5.612L5.29 9.77a.75.75 0 0 1-1.08-1.04l5.25-5.5a.75.75 0 0 1 1.08 0l5.25 5.5a.75.75 0 1 1-1.08 1.04l-3.96-4.158V16.25A.75.75 0 0 1 10 17Z"
      clipRule="evenodd"
    />
  </svg>
);
