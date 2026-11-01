"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Field, Input, Textarea } from "@/components/ui/field";
import { Panel } from "@/components/ui/panel";
import { Select } from "@/components/ui/select";
import { StatusPill } from "@/components/ui/status";
import { ApiError } from "@/lib/api";
import { formatRelative } from "@/lib/cn";
import type { Experience, LanguageEntry, Profile } from "@/lib/schemas";
import { useProfile, useSaveProfile, type ProfileInput } from "@/lib/vault-queries";

import { EMPTY_DETAILS, ProfileDetailsPanels, ProfilePiiPanels } from "./profile-v2-sections";

const DEGREES = [
  { value: "HIGH_SCHOOL", label: "High school" },
  { value: "BACHELOR", label: "Bachelor" },
  { value: "MASTER", label: "Master" },
  { value: "PHD", label: "PhD" },
  { value: "OTHER", label: "Other" },
];
const SCALES = ["4", "5", "10", "100"];

const EMPTY: ProfileInput = {
  full_name: "",
  pii: { home_address: null, phone: null, date_of_birth: null, national_id: null, passport_number: null },
  degree_level: null,
  major: null,
  institution: null,
  gpa: null,
  gpa_scale: 4,
  nationality: null,
  country_of_residence: null,
  graduation_year: null,
  skills: [],
  languages: [],
  experiences: [],
  details: EMPTY_DETAILS,
};

function fromProfile(p: Profile): ProfileInput {
  return {
    full_name: p.full_name,
    pii: { ...p.pii, referees: p.pii.referees ?? [] },
    degree_level: p.degree_level,
    major: p.major,
    institution: p.institution,
    gpa: p.gpa,
    gpa_scale: p.gpa_scale ?? 4,
    nationality: p.nationality,
    country_of_residence: p.country_of_residence,
    graduation_year: p.graduation_year,
    skills: p.skills,
    languages: p.languages,
    experiences: p.experiences,
    details: p.details ?? EMPTY_DETAILS,
  };
}

const text = (v: string | null | undefined) => v ?? "";
const orNull = (v: string) => (v.trim() ? v : null);

export function ProfileEditor() {
  const { data: profile, isLoading } = useProfile();
  if (isLoading) return <p className="font-mono text-xs text-faint">Loading</p>;
  // Remount the form when the stored profile changes (fresh initial state, no effect syncing).
  return <ProfileForm key={profile?.updated_at ?? "new"} profile={profile ?? null} />;
}

function ProfileForm({ profile }: { profile: Profile | null }) {
  const save = useSaveProfile();
  const [form, setForm] = useState<ProfileInput>(profile ? fromProfile(profile) : EMPTY);
  const [skillsText, setSkillsText] = useState(form.skills.join(", "));
  const [error, setError] = useState<string | null>(null);

  const set = <K extends keyof ProfileInput>(key: K, value: ProfileInput[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  const setPii = (key: "home_address" | "phone" | "date_of_birth" | "national_id" | "passport_number", value: string) =>
    setForm((f) => ({ ...f, pii: { ...f.pii, [key]: orNull(value) } }));

  const gpaNorm =
    form.gpa !== null && form.gpa_scale ? ((form.gpa / form.gpa_scale) * 4).toFixed(2) : null;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      await save.mutateAsync({
        ...form,
        skills: skillsText.split(",").map((s) => s.trim()).filter(Boolean),
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Save failed");
    }
  }

  return (
    <form onSubmit={onSubmit} className="flex flex-col gap-3" autoComplete="off">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <h1 className="text-sm font-semibold">Student profile</h1>
          {profile?.updated_at ? (
            <span className="text-2xs text-faint">saved {formatRelative(profile.updated_at)}</span>
          ) : (
            <span className="text-2xs text-faint">not saved yet</span>
          )}
        </div>
        <Button type="submit" variant="primary" disabled={save.isPending || !form.full_name.trim()}>
          {save.isPending ? "Saving" : "Save profile"}
        </Button>
      </div>
      {error ? (
        <p className="rounded-sm border border-err/30 bg-err-soft px-2 py-1 text-xs text-err">{error}</p>
      ) : null}

      <div className="grid grid-cols-2 gap-3">
        <Panel title="Identity">
          <div className="grid grid-cols-2 gap-3 p-3">
            <Field id="full_name" label="Full legal name" className="col-span-2">
              <Input
                id="full_name"
                value={form.full_name}
                onChange={(e) => set("full_name", e.target.value)}
                required
              />
            </Field>
            <Field id="nationality" label="Nationality">
              <Input id="nationality" value={text(form.nationality)} onChange={(e) => set("nationality", orNull(e.target.value))} />
            </Field>
            <Field id="residence" label="Country of residence">
              <Input id="residence" value={text(form.country_of_residence)} onChange={(e) => set("country_of_residence", orNull(e.target.value))} />
            </Field>
          </div>
        </Panel>

        <Panel
          title="Personal data"
          actions={
            <StatusPill tone={profile?.pii_encrypted ? "ok" : "idle"}>
              {profile?.pii_encrypted
                ? `AES-256-GCM · key v${profile.pii_key_version}`
                : "Encrypted on save"}
            </StatusPill>
          }
        >
          <div className="grid grid-cols-2 gap-3 p-3">
            <Field id="address" label="Home address" className="col-span-2">
              <Input id="address" value={text(form.pii.home_address)} onChange={(e) => setPii("home_address", e.target.value)} />
            </Field>
            <Field id="phone" label="Phone (with country code)">
              <Input id="phone" value={text(form.pii.phone)} onChange={(e) => setPii("phone", e.target.value)} className="font-mono text-xs" />
            </Field>
            <Field id="dob" label="Date of birth">
              <Input id="dob" type="date" value={text(form.pii.date_of_birth)} onChange={(e) => setPii("date_of_birth", e.target.value)} />
            </Field>
            <Field id="national_id" label="National ID">
              <Input id="national_id" value={text(form.pii.national_id)} onChange={(e) => setPii("national_id", e.target.value)} className="font-mono text-xs" />
            </Field>
            <Field id="passport" label="Passport number">
              <Input id="passport" value={text(form.pii.passport_number)} onChange={(e) => setPii("passport_number", e.target.value)} className="font-mono text-xs" />
            </Field>
          </div>
        </Panel>
      </div>

      <Panel title="Academic credentials" meta={gpaNorm ? `normalized ${gpaNorm} / 4.00` : undefined}>
        <div className="grid grid-cols-6 gap-3 p-3">
          <Field id="degree" label="Degree level" className="col-span-1">
            <Select
              id="degree"
              value={form.degree_level ?? undefined}
              onValueChange={(v) => set("degree_level", v)}
              placeholder="Select"
              options={DEGREES}
              className="w-full"
            />
          </Field>
          <Field id="major" label="Major / field" className="col-span-2">
            <Input id="major" value={text(form.major)} onChange={(e) => set("major", orNull(e.target.value))} />
          </Field>
          <Field id="institution" label="Institution" className="col-span-3">
            <Input id="institution" value={text(form.institution)} onChange={(e) => set("institution", orNull(e.target.value))} />
          </Field>
          <Field id="gpa" label="GPA">
            <Input
              id="gpa"
              type="number"
              step="0.01"
              min={0}
              value={form.gpa ?? ""}
              onChange={(e) => set("gpa", e.target.value === "" ? null : Number(e.target.value))}
              className="font-mono text-xs"
            />
          </Field>
          <Field id="gpa_scale" label="Scale">
            <Select
              id="gpa_scale"
              value={String(form.gpa_scale ?? 4)}
              onValueChange={(v) => set("gpa_scale", Number(v))}
              options={SCALES.map((s) => ({ value: s, label: `out of ${s}` }))}
              className="w-full"
            />
          </Field>
          <Field id="grad" label="Graduation year">
            <Input
              id="grad"
              type="number"
              min={1950}
              max={2100}
              value={form.graduation_year ?? ""}
              onChange={(e) =>
                set("graduation_year", e.target.value === "" ? null : Number(e.target.value))
              }
              className="font-mono text-xs"
            />
          </Field>
          <Field id="skills" label="Skills (comma separated)" className="col-span-3">
            <Input
              id="skills"
              value={skillsText}
              onChange={(e) => setSkillsText(e.target.value)}
              placeholder="Python, Machine Learning, Public speaking"
            />
          </Field>
        </div>
      </Panel>

      <ProfileDetailsPanels value={form.details} onChange={(v) => set("details", v)} />
      <ProfilePiiPanels value={form.pii} onChange={(v) => set("pii", v)} />

      <div className="grid grid-cols-3 gap-3">
        <LanguagesPanel value={form.languages} onChange={(v) => set("languages", v)} />
        <ExperiencesPanel value={form.experiences} onChange={(v) => set("experiences", v)} />
      </div>
    </form>
  );
}

function LanguagesPanel({
  value,
  onChange,
}: {
  value: LanguageEntry[];
  onChange: (v: LanguageEntry[]) => void;
}) {
  const update = (i: number, patch: Partial<LanguageEntry>) =>
    onChange(value.map((l, idx) => (idx === i ? { ...l, ...patch } : l)));
  return (
    <Panel
      title="Languages"
      actions={
        <Button size="sm" onClick={() => onChange([...value, { name: "", level: "" }])}>
          Add
        </Button>
      }
    >
      <div className="flex flex-col gap-2 p-3">
        {value.length === 0 ? <p className="text-xs text-faint">None added.</p> : null}
        {value.map((lang, i) => (
          <div key={i} className="flex gap-2">
            <Input aria-label="Language" placeholder="English" value={lang.name} onChange={(e) => update(i, { name: e.target.value })} />
            <Input aria-label="Level" placeholder="C1 / Native" value={lang.level} onChange={(e) => update(i, { level: e.target.value })} className="w-28" />
            <Button size="sm" variant="ghost" onClick={() => onChange(value.filter((_, idx) => idx !== i))}>
              Remove
            </Button>
          </div>
        ))}
      </div>
    </Panel>
  );
}

function ExperiencesPanel({
  value,
  onChange,
}: {
  value: Experience[];
  onChange: (v: Experience[]) => void;
}) {
  const update = (i: number, patch: Partial<Experience>) =>
    onChange(value.map((e, idx) => (idx === i ? { ...e, ...patch } : e)));
  return (
    <Panel
      title="Experience"
      className="col-span-2"
      actions={
        <Button
          size="sm"
          onClick={() =>
            onChange([...value, { title: "", organization: "", start: null, end: null, description: "" }])
          }
        >
          Add
        </Button>
      }
    >
      <div className="flex flex-col divide-y divide-line">
        {value.length === 0 ? <p className="p-3 text-xs text-faint">None added.</p> : null}
        {value.map((exp, i) => (
          <div key={i} className="grid grid-cols-6 gap-2 p-3">
            <Input aria-label="Title" placeholder="Research Intern" value={exp.title} onChange={(e) => update(i, { title: e.target.value })} className="col-span-2" />
            <Input aria-label="Organization" placeholder="Organization" value={exp.organization} onChange={(e) => update(i, { organization: e.target.value })} className="col-span-2" />
            <Input aria-label="Start" type="month" value={exp.start ?? ""} onChange={(e) => update(i, { start: orNull(e.target.value) })} />
            <Input aria-label="End" type="month" value={exp.end ?? ""} onChange={(e) => update(i, { end: orNull(e.target.value) })} />
            <Textarea aria-label="Description" rows={2} placeholder="What you did and the outcome" value={exp.description} onChange={(e) => update(i, { description: e.target.value })} className="col-span-5" />
            <Button size="sm" variant="ghost" onClick={() => onChange(value.filter((_, idx) => idx !== i))}>
              Remove
            </Button>
          </div>
        ))}
      </div>
    </Panel>
  );
}
