"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/field";
import { Panel } from "@/components/ui/panel";
import { StatusPill } from "@/components/ui/status";
import { ApiError } from "@/lib/api";
import { useTestPrompt } from "@/lib/queries";

const DEFAULT_PROMPT = "In two sentences, what is a Statement of Purpose for a scholarship?";

export function PromptTest({ activeModel }: { activeModel: string | null }) {
  const test = useTestPrompt();
  const [prompt, setPrompt] = useState(DEFAULT_PROMPT);
  const [maxTokens, setMaxTokens] = useState(512);
  const [showReasoning, setShowReasoning] = useState(false);
  const result = test.data;

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!prompt.trim()) return;
    test.mutate({ prompt, max_tokens: maxTokens });
  }

  return (
    <Panel title="Prompt test" meta={activeModel ?? "no active provider"}>
      <form onSubmit={onSubmit} className="flex flex-col gap-2 p-3">
        <textarea
          aria-label="Prompt"
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          rows={3}
          maxLength={8000}
          className="w-full resize-y rounded-sm border border-line-strong bg-surface px-2 py-1.5 text-sm text-fg placeholder:text-faint focus:border-muted focus:outline-none"
        />
        <div className="flex items-center gap-2">
          <label htmlFor="max_tokens" className="text-xs text-muted">
            Max tokens
          </label>
          <Input
            id="max_tokens"
            type="number"
            min={1}
            max={4096}
            value={maxTokens}
            onChange={(e) => setMaxTokens(Math.max(1, Math.min(4096, Number(e.target.value) || 1)))}
            className="w-24 font-mono text-xs"
          />
          <span className="text-2xs text-faint">
            Reasoning models spend tokens thinking first; raise this if the answer comes back empty.
          </span>
          <Button
            type="submit"
            variant="primary"
            className="ml-auto"
            disabled={test.isPending || !activeModel}
          >
            {test.isPending ? "Generating" : "Run prompt"}
          </Button>
        </div>
      </form>

      {test.isError ? (
        <div className="border-t border-line bg-err-soft px-3 py-2 font-mono text-2xs text-err">
          {test.error instanceof ApiError
            ? `${test.error.code}: ${test.error.message}`
            : "Request failed"}
        </div>
      ) : null}

      {result ? (
        <div className="border-t border-line">
          <div className="flex flex-wrap items-center gap-3 px-3 py-1.5">
            <StatusPill tone={result.text ? "ok" : "warn"}>
              {result.text ? "Model responded" : "Empty answer"}
            </StatusPill>
            <Stat label="latency" value={`${result.latency_ms} ms`} />
            <Stat label="in" value={String(result.prompt_tokens ?? "?")} />
            <Stat label="out" value={String(result.completion_tokens ?? "?")} />
            <Stat label="finish" value={result.finish_reason ?? "?"} />
            <span className="ml-auto truncate font-mono text-2xs text-faint">{result.model}</span>
          </div>
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap border-t border-line px-3 py-2 font-sans text-sm text-fg">
            {result.text ||
              "(no final answer: the model used every token on reasoning; increase Max tokens)"}
          </pre>
          {result.reasoning ? (
            <div className="border-t border-line px-3 py-1.5">
              <button
                type="button"
                onClick={() => setShowReasoning((v) => !v)}
                className="text-xs text-muted hover:text-fg"
              >
                {showReasoning ? "Hide" : "Show"} reasoning ({result.reasoning.length} chars)
              </button>
              {showReasoning ? (
                <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap font-mono text-2xs text-muted">
                  {result.reasoning}
                </pre>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </Panel>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <span className="text-2xs text-faint">
      {label} <span className="font-mono text-fg tabular">{value}</span>
    </span>
  );
}
