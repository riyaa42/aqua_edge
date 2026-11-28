import * as React from "react";
import { cn } from "@/lib/utils";

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  tone?: "plain" | "dark" | "red" | "yellow" | "green";
}

const TONE_CLASSES: Record<NonNullable<BadgeProps["tone"]>, string> = {
  plain: "bg-[var(--panel-alt)] text-[var(--ink)]",
  dark: "bg-[#111111] text-white",
  red: "bg-[var(--status-red)] text-[#111111]",
  yellow: "bg-[var(--status-yellow)] text-[#111111]",
  green: "bg-[var(--status-green)] text-[#111111]",
};

export function Badge({ className, tone = "plain", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-block border border-black px-1.5 py-0.5 text-[11px] font-bold uppercase tracking-wide",
        TONE_CLASSES[tone],
        className
      )}
      {...props}
    />
  );
}
