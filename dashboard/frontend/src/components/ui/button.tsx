import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const buttonVariants = cva(
  "inline-flex items-center justify-center border border-black text-xs font-semibold uppercase tracking-wide transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed",
  {
    variants: {
      variant: {
        default: "bg-[var(--panel-bg)] text-[var(--ink)] hover:bg-[var(--panel-alt)]",
        active: "bg-[#111111] text-white",
        outline: "bg-transparent text-[var(--ink)] hover:bg-[var(--panel-alt)]",
      },
      size: {
        default: "h-8 px-3 py-1",
        sm: "h-7 px-2.5 text-[11px]",
        icon: "h-9 w-9 p-0",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "default",
    },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button ref={ref} className={cn(buttonVariants({ variant, size, className }))} {...props} />
  )
);
Button.displayName = "Button";
