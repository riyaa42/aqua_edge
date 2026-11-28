import * as React from "react";
import { cn } from "@/lib/utils";

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: "plain" | "dark";
}

export function Badge({ className, tone = "plain", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-block border border-black px-1.5 py-0.5 text-[11px] font-bold uppercase tracking-wide",
        tone === "dark" ? "bg-[#111111] text-white" : "bg-[var(--panel-alt)] text-[var(--ink)]",
        className
      )}
      {...props}
    />
  );
}
