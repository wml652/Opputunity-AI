"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Field, Input, Textarea } from "@/components/ui/field";
import { Panel } from "@/components/ui/panel";
import { Select } from "@/components/ui/select";
import { ApiError } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useCreateCampaign } from "@/lib/discovery-queries";
import type { Scope } from "@/lib/discovery-schemas";

const SCOPES: { value: Scope; label: string }[] = [
  { value: "BOTH", label: "Local + International" },
  { value: "LOCAL", label: "Local only" },
  { value: "INTERNATIONAL", label: "International only" },
];

function lines(text: string): string[] {
  return text
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function Toggle({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <label
      className={cn(
        "flex cursor-pointer items-start gap-2 rounded-sm border px-2 py-1.5",
        checked ? "border-line-strong bg-surface-2" : "border-line",
      )}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="mt-0.5 accent-[var(--text)]"
      />
      <span className="flex flex-col">
        <span className="text-xs font-medium">{label}</span>
        <span className="text-2xs text-faint">{hint}</span>
      </span>
    </label>
  );
}

export function CampaignLauncher({ compact = false, onLaunched }: { compact?: boolean; onLaunched?: (id: string) => void }) {
  const create = useCreateCampaign();
  const [name, setName] = useState("");
  const [keywords, setKeywords] = useState("undergraduate scholarship\nresearch internship");
  const [urls, setUrls] = useState("");
  const [scope, setScope] = useState<Scope>("BOTH");
  const [country, setCountry] = useState("");
  const [useWebsites, setUseWebsites] = useState(true);
  const [useSearch, setUseSearch] = useState(true);
  const [useLinkedin, setUseLinkedin] = useState(false);
  const [maxPages, setMaxPages] = useState(15);
  const [depth, setDepth] = useState(2);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const campaign = await create.mutateAsync({
        name,
        scope,
        keywords: lines(keywords),
        target_urls: useWebsites ? lines(urls) : [],
        use_websites: useWebsites,
        use_search: useSearch,
        use_linkedin: useLinkedin,
        max_pages_per_site: maxPages,
        max_depth: depth,
        home_country: country.trim() || null,
      });
      onLaunched?.(campaign.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start campaign");
    }
  }

  return (
    <Panel title="Campaign launcher" meta="crawl -> extract -> evaluate -> queue">
      <form onSubmit={onSubmit} className={cn("grid gap-3 p-3", compact ? "grid-cols-4" : "grid-cols-6")}>
        <Field id="keywords" label="Keywords (one per line)" className={compact ? "col-span-1" : "col-span-2"}>
          <Textarea id="keywords" rows={3} value={keywords} onChange={(e) => setKeywords(e.target.value)} />
        </Field>
        <Field
          id="urls"
          label="Target websites (one per line)"
          className={compact ? "col-span-2" : "col-span-3"}
          hint="e.g. opportunity directories, university portals"
        >
          <Textarea
            id="urls"
            rows={3}
            value={urls}
            disabled={!useWebsites}
            onChange={(e) => setUrls(e.target.value)}
            placeholder="https://opportunitiesforyouth.org/"
            className="font-mono text-xs"
          />
        </Field>
        <div className="flex flex-col gap-2">
          <Field id="scope" label="Scope">
            <Select id="scope" value={scope} onValueChange={(v) => setScope(v as Scope)} options={SCOPES} className="w-full" />
          </Field>
          <Field id="country" label="Home country (local scope)">
            <Input id="country" value={country} onChange={(e) => setCountry(e.target.value)} placeholder="Pakistan" />
          </Field>
        </div>

        <div className={cn("grid gap-2", compact ? "col-span-3 grid-cols-3" : "col-span-4 grid-cols-3")}>
          <Toggle label="Websites" hint="Crawl the target sites (Playwright)" checked={useWebsites} onChange={setUseWebsites} />
          <Toggle label="Web search" hint="keywords x scope queries" checked={useSearch} onChange={setUseSearch} />
          <Toggle label="LinkedIn public" hint="Guest job listings, rate-limited" checked={useLinkedin} onChange={setUseLinkedin} />
        </div>
        <div className={cn("flex items-end gap-2", compact ? "col-span-1" : "col-span-2")}>
          {!compact ? (
            <>
              <Field id="name" label="Name" className="flex-1">
                <Input id="name" value={name} onChange={(e) => setName(e.target.value)} placeholder="optional" />
              </Field>
              <Field id="pages" label="Pages/site">
                <Input id="pages" type="number" min={1} max={100} value={maxPages} onChange={(e) => setMaxPages(Number(e.target.value) || 1)} className="w-16 font-mono text-xs" />
              </Field>
              <Field id="depth" label="Depth">
                <Input id="depth" type="number" min={0} max={4} value={depth} onChange={(e) => setDepth(Number(e.target.value) || 0)} className="w-14 font-mono text-xs" />
              </Field>
            </>
          ) : null}
          <Button type="submit" variant="primary" disabled={create.isPending} className="ml-auto">
            {create.isPending ? "Starting" : "Launch campaign"}
          </Button>
        </div>
        {error ? (
          <p className="col-span-full rounded-sm border border-err/30 bg-err-soft px-2 py-1 text-xs text-err">{error}</p>
        ) : null}
      </form>
    </Panel>
  );
}
