import { useState, useCallback, useRef } from "react";

interface SlashCommandsOptions {
  saveWorkflowPrompt?: string;
  runWorkflowPrompt?: string;
  createWorkflowPrompt?: string;
}

export function useSlashCommands(_options: SlashCommandsOptions = {}) {
  const [showCommandMenu, setShowCommandMenu] = useState(false);
  const [filteredSkills, setFilteredSkills] = useState<string[]>([]);
  const [isLoadingSkills, setIsLoadingSkills] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(0);

  const skillsCacheRef = useRef<{ skills: string[]; fetchedAt: number }>({
    skills: [],
    fetchedAt: 0,
  });

  const fetchSkills = useCallback(async (): Promise<string[]> => {
    const now = Date.now();
    if (
      now - skillsCacheRef.current.fetchedAt < 30_000 &&
      skillsCacheRef.current.skills.length > 0
    ) {
      return skillsCacheRef.current.skills;
    }

    setIsLoadingSkills(true);
    try {
      const res = await fetch("/api/skills");
      if (!res.ok) return [];
      const data = await res.json();
      const skills = Array.isArray(data?.skills)
        ? data.skills.filter((s: unknown): s is string => typeof s === "string")
        : [];
      skillsCacheRef.current = { skills, fetchedAt: now };
      return skills;
    } catch {
      return [];
    } finally {
      setIsLoadingSkills(false);
    }
  }, []);

  const handleInputChange = useCallback(
    (value: string) => {
      if (value.startsWith("/") && !value.includes(" ") && value.length < 80) {
        const prefix = value.slice(1).toLowerCase();
        fetchSkills().then((skills) => {
          const matches = prefix
            ? skills.filter((s) => s.toLowerCase().includes(prefix))
            : skills;
          setFilteredSkills(matches);
          setShowCommandMenu(matches.length > 0 || value === "/");
          setSelectedIndex(0);
        });
        return;
      }

      setShowCommandMenu(false);
      setSelectedIndex(0);
    },
    [fetchSkills],
  );

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent): boolean => {
      if (!showCommandMenu) return false;

      const count = filteredSkills.length;
      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          setSelectedIndex((prev) => (prev + 1) % Math.max(count, 1));
          return true;
        case "ArrowUp":
          e.preventDefault();
          setSelectedIndex((prev) => (prev - 1 + Math.max(count, 1)) % Math.max(count, 1));
          return true;
        case "Escape":
          e.preventDefault();
          setShowCommandMenu(false);
          return true;
        case "Tab":
        case "Enter":
          if (count > 0) {
            e.preventDefault();
            return true;
          }
          return false;
        default:
          return false;
      }
    },
    [showCommandMenu, filteredSkills],
  );

  const handleSelect = useCallback(
    (index: number): { expanded: string | null; newInput: string | null } => {
      const skill = filteredSkills[index];
      if (!skill) return { expanded: null, newInput: null };
      setShowCommandMenu(false);
      return { expanded: null, newInput: `/${skill} ` };
    },
    [filteredSkills],
  );

  const tryExecute = useCallback((_input: string): string | null => {
    // Skill invocations are passed through to backend as typed (/skill-name ...).
    return null;
  }, []);

  const reset = useCallback(() => {
    setShowCommandMenu(false);
    setFilteredSkills([]);
    setSelectedIndex(0);
  }, []);

  return {
    showSkillsMenu: showCommandMenu,
    filteredSkills,
    isLoadingSkills,
    selectedIndex,
    setSelectedIndex,
    handleInputChange,
    handleKeyDown,
    handleSelect,
    tryExecute,
    reset,
  };
}
