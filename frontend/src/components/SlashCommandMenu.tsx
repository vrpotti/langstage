import { useRef, useEffect } from "react";
import { Sparkles, Loader2 } from "lucide-react";

interface SlashCommandMenuProps {
  showSkillsMenu: boolean;
  filteredSkills: string[];
  isLoadingSkills: boolean;
  selectedIndex: number;
  onSelect: (index: number) => void;
  onHover: (index: number) => void;
}

export function SlashCommandMenu({
  showSkillsMenu,
  filteredSkills,
  isLoadingSkills,
  selectedIndex,
  onSelect,
  onHover,
}: SlashCommandMenuProps) {
  const selectedRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    selectedRef.current?.scrollIntoView({ block: "nearest" });
  }, [selectedIndex]);

  if (!showSkillsMenu) return null;

  return (
    <div className="absolute bottom-full left-0 right-0 mb-2 z-10">
      <div className="bg-[var(--color-card)] border border-[var(--color-border)] rounded-lg shadow-md overflow-hidden max-h-[200px] overflow-y-auto">
        <div className="py-1">
          <div className="px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-[var(--color-text-muted)]">
            Skills
          </div>
          {isLoadingSkills ? (
            <div className="flex items-center gap-2 px-3 py-2 text-xs text-[var(--color-text-muted)]">
              <Loader2 size={12} className="animate-spin" />
              Loading skills...
            </div>
          ) : filteredSkills.length === 0 ? (
            <div className="px-3 py-2 text-xs text-[var(--color-text-muted)] italic">
              No skills found.
            </div>
          ) : (
            filteredSkills.map((skill, i) => (
              <button
                key={skill}
                ref={i === selectedIndex ? selectedRef : undefined}
                onClick={() => onSelect(i)}
                onMouseEnter={() => onHover(i)}
                className={`w-full text-left px-3 py-1.5 flex items-center gap-2 transition-colors ${
                  i === selectedIndex
                    ? "bg-[var(--color-surface-3)]"
                    : "hover:bg-[var(--color-surface-3)]"
                }`}
              >
                <Sparkles
                  size={14}
                  className="text-[var(--color-text-secondary)] flex-shrink-0"
                />
                <span className="text-sm text-[var(--color-text)] truncate">
                  /{skill}
                </span>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
