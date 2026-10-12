
import type { SVGProps } from 'react';

export interface IconProps extends SVGProps<SVGSVGElement> {
  /**
   * Icon size (width and height)
   * @default 16
   */
  size?: number;

  /**
   * Additional CSS classes
   */
  className?: string;
}
