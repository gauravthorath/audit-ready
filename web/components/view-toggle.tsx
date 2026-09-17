"use client";

/**
 * List ⇄ grid, remembered per surface (the key). Presentation only.
 */

import { LayoutGrid, Rows3 } from "lucide-react";
import { useLocalSetting } from "@/lib/use-local-setting";

export type ViewMode = "list" | "grid";
const MODES: readonly ViewMode[] = ["list", "grid"];

export function useViewMode(key: string, fallback: ViewMode = "list") {
  return useLocalSetting<ViewMode>(`view.${key}`, fallback, MODES);
}

export function ViewToggle({
  value,
  onChange,
  label = "View",
}: {
  value: ViewMode;
  onChange: (v: ViewMode) => void;
  label?: string;
}) {
  return (
    <div className="seg-ctl" role="group" aria-label={label}>
      <button type="button" aria-pressed={value === "list"} onClick={() => onChange("list")} title="List view">
        <Rows3 className="h-3.5 w-3.5" aria-hidden />
        <span className="hidden sm:inline">List</span>
      </button>
      <button type="button" aria-pressed={value === "grid"} onClick={() => onChange("grid")} title="Grid view">
        <LayoutGrid className="h-3.5 w-3.5" aria-hidden />
        <span className="hidden sm:inline">Grid</span>
      </button>
    </div>
  );
}
