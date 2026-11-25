/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect, useState } from "react";
import type { SuggestionItem } from "./types.ts";

interface SuggestionDropdownProps {
  open: boolean;
  items: SuggestionItem[];
  selectedIdx: number;
  onSelectIndex: (idx: number) => void;
  onAccept: (item: SuggestionItem) => void;
  positionClassName?: string;
}

export function SuggestionDropdown({
  open,
  items,
  selectedIdx,
  onSelectIndex,
  onAccept,
  positionClassName = "left-0 right-0 top-full mt-1",
}: SuggestionDropdownProps) {
  if (!open || items.length === 0) return null;

  return (
    <div
      role="listbox"
      aria-label="Autocomplete suggestions"
      className={`absolute z-50 ${positionClassName} max-h-60 overflow-y-auto rounded-lg border border-sky-500 bg-white p-1 shadow-xl font-mono text-xs`}
    >
      {items.slice(0, 12).map((item, idx) => {
        const isSelected = idx === selectedIdx;
        return (
          <button
            key={`${item.insert}-${item.meta}-${idx}`}
            type="button"
            role="option"
            aria-selected={isSelected}
            onMouseEnter={() => onSelectIndex(idx)}
            onMouseDown={(e) => {
              e.preventDefault();
              onAccept(item);
            }}
            className={`w-full flex items-center justify-between gap-2.5 rounded-md px-2.5 py-1.5 text-left cursor-pointer transition-colors ${
              isSelected
                ? "bg-sky-50 text-sky-800"
                : "text-slate-800 hover:bg-slate-50"
            }`}
          >
            <span className="font-semibold truncate">{item.label}</span>
            <span className="shrink-0 rounded-full border border-slate-200 bg-slate-100 px-2 py-0.5 text-[10px] text-slate-600">
              {item.meta}
            </span>
          </button>
        );
      })}
    </div>
  );
}

interface AutocompleteInputProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "onChange" | "value"> {
  value: string;
  onValueChange: (next: string) => void;
  getSuggestions: (value: string) => SuggestionItem[];
  submitOnEnter?: () => void;
  containerClassName?: string;
}

export function AutocompleteInput({
  value,
  onValueChange,
  getSuggestions,
  submitOnEnter,
  containerClassName = "relative",
  onKeyDown,
  ...rest
}: AutocompleteInputProps) {
  const [open, setOpen] = useState(false);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [userNavigated, setUserNavigated] = useState(false);

  const suggestions = open ? getSuggestions(value).slice(0, 12) : [];

  useEffect(() => {
    if (selectedIdx >= suggestions.length) {
      setSelectedIdx(Math.max(0, suggestions.length - 1));
    }
  }, [suggestions.length, selectedIdx]);

  const acceptItem = (item: SuggestionItem) => {
    onValueChange(item.insert);
    setOpen(false);
    setUserNavigated(false);
  };

  return (
    <div className={containerClassName}>
      <input
        {...rest}
        value={value}
        autoComplete="off"
        onFocus={() => {
          setSelectedIdx(0);
          setUserNavigated(false);
          setOpen(true);
        }}
        onClick={() => {
          if (!open) {
            setSelectedIdx(0);
            setUserNavigated(false);
            setOpen(true);
          }
        }}
        onChange={(e) => {
          onValueChange(e.target.value);
          setSelectedIdx(0);
          setUserNavigated(false);
          setOpen(true);
        }}
        onBlur={() => {
          setTimeout(() => {
            setOpen(false);
            setUserNavigated(false);
          }, 120);
        }}
        onKeyDown={(e) => {
          if (e.ctrlKey && e.code === "Space") {
            e.preventDefault();
            setSelectedIdx(0);
            setUserNavigated(true);
            setOpen(true);
            return;
          }

          if (open && suggestions.length > 0) {
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setSelectedIdx((prev) => (prev + 1) % suggestions.length);
              setUserNavigated(true);
              return;
            }
            if (e.key === "ArrowUp") {
              e.preventDefault();
              setSelectedIdx((prev) => (prev - 1 + suggestions.length) % suggestions.length);
              setUserNavigated(true);
              return;
            }
            if (e.key === "Tab") {
              e.preventDefault();
              acceptItem(suggestions[selectedIdx] ?? suggestions[0]);
              return;
            }
            if (e.key === "Escape") {
              e.preventDefault();
              setOpen(false);
              setUserNavigated(false);
              return;
            }
            if (e.key === "Enter" && !e.shiftKey) {
              if (!submitOnEnter || userNavigated) {
                e.preventDefault();
                acceptItem(suggestions[selectedIdx] ?? suggestions[0]);
                return;
              }
              setOpen(false);
              setUserNavigated(false);
            }
          }

          if (e.key === "Enter" && !e.shiftKey && submitOnEnter) {
            e.preventDefault();
            submitOnEnter();
            return;
          }

          onKeyDown?.(e);
        }}
      />
      <SuggestionDropdown
        open={open && suggestions.length > 0}
        items={suggestions}
        selectedIdx={selectedIdx}
        onSelectIndex={(idx) => {
          setSelectedIdx(idx);
          setUserNavigated(true);
        }}
        onAccept={acceptItem}
      />
    </div>
  );
}
