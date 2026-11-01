"use client";

import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/field";
import { Panel } from "@/components/ui/panel";
import { Select } from "@/components/ui/select";
import {
  englishTypes,
  workModes,
  type PII,
  type ProfileDetails,
  type Referee,
} from "@/lib/schemas";

export const EMPTY_DETAILS: ProfileDetails = {
  current_semester_or_year: null,
  cgpa_percentage: null,
  secondary_school_qualification: null,
  secondary_school_percentage: null,
  minor_or_specialization: null,
  thesis_or_fyp_title: null,
  academic_honors: [],
  english_proficiency_type: null,
  english_proficiency_score: null,
  standardized_tests: {},
  publications_count: null,
  research_interests: [],
  preferred_work_mode: null,
  earliest_start_date: null,
  requires_visa_sponsorship: null,
  has_valid_passport: null,
  financial_need_eligible: null,
  first_generation_student: null,
};

const EMPTY_REFEREE: Referee = {
  name: null,
  title: null,
  department: null,
  organization: null,
  email: null,
  phone: null,
  relationship: null,
};

const orNull = (v: string) => (v.trim() ? v : null);
const num = (v: string) => (v.trim() === "" || Number.isNaN(Number(v)) ? null : Number(v));
const list = (v: string) => v.split(",").map((s) => s.trim()).filter(Boolean);

const TRI = [
  { value: "unset", label: "Not stated" },
  { value: "yes", label: "Yes" },
  { value: "no", label: "No" },
];
const tri = (v: boolean | null) => (v === null ? "unset" : v ? "yes" : "no");
const fromTri = (v: string) => (v === "unset" ? null : v === "yes");

interface DetailsProps {
  value: ProfileDetails;
  onChange: (next: ProfileDetails) => void;
}

function TextField({
  id,
  label,
  value,
  onChange,
  mono,
  className,
  type,
  hint,
}: {
  id: string;
  label: string;
  value: string | number | null;
  onChange: (v: string) => void;
  mono?: boolean;
  className?: string;
  type?: string;
  hint?: string;
}) {
  return (
    <Field id={id} label={label} className={className} hint={hint}>
      <Input
        id={id}
        type={type}
        value={value === null ? "" : String(value)}
        onChange={(e) => onChange(e.target.value)}
        className={mono ? "font-mono text-xs" : undefined}
      />
    </Field>
  );
}

/** Academic depth, English / tests, research and work preferences (non-PII: eligibility, ranking
 * and the form filler use them; the user's model may see them). */
export function ProfileDetailsPanels({ value, onChange }: DetailsProps) {
  const set = <K extends keyof ProfileDetails>(key: K, v: ProfileDetails[K]) => onChange({ ...value, [key]: v });
  const tests = Object.entries(value.standardized_tests)
    .map(([k, v]) => `${k} ${v}`)
    .join(", ");
  return (
    <>
      <Panel title="Academic depth">
        <div className="grid grid-cols-6 gap-3 p-3">
          <TextField id="semester" label="Current semester / year" value={value.current_semester_or_year} onChange={(v) => set("current_semester_or_year", orNull(v))} className="col-span-2" />
          <TextField id="cgpa_pct" label="CGPA %" value={value.cgpa_percentage} onChange={(v) => set("cgpa_percentage", num(v))} mono />
          <TextField id="minor" label="Minor / specialization" value={value.minor_or_specialization} onChange={(v) => set("minor_or_specialization", orNull(v))} className="col-span-3" />
          <TextField id="secondary" label="Secondary school qualification" value={value.secondary_school_qualification} onChange={(v) => set("secondary_school_qualification", orNull(v))} className="col-span-3" hint="e.g. FSc Pre-Engineering / A-Levels" />
          <TextField id="secondary_pct" label="Secondary %" value={value.secondary_school_percentage} onChange={(v) => set("secondary_school_percentage", num(v))} mono />
          <TextField id="pubs" label="Publications" value={value.publications_count} onChange={(v) => set("publications_count", num(v))} mono />
          <TextField id="thesis" label="Thesis / FYP title" value={value.thesis_or_fyp_title} onChange={(v) => set("thesis_or_fyp_title", orNull(v))} className="col-span-4" />
          <TextField id="honors" label="Academic honors (comma separated)" value={value.academic_honors.join(", ")} onChange={(v) => set("academic_honors", list(v))} className="col-span-2" />
        </div>
      </Panel>
      <Panel title="English, tests and research">
        <div className="grid grid-cols-6 gap-3 p-3">
          <Field id="english_type" label="English proficiency" className="col-span-2">
            <Select
              id="english_type"
              value={value.english_proficiency_type ?? "unset"}
              onValueChange={(v) => set("english_proficiency_type", v === "unset" ? null : (v as ProfileDetails["english_proficiency_type"]))}
              options={[{ value: "unset", label: "Not stated" }, ...englishTypes.map((t) => ({ value: t, label: t.replace("_", " ") }))]}
            />
          </Field>
          <TextField id="english_score" label="Score" value={value.english_proficiency_score} onChange={(v) => set("english_proficiency_score", orNull(v))} mono />
          <Field id="tests" label="Standardized tests" className="col-span-3" hint='e.g. "GRE 320, SAT 1450"'>
            <Input
              id="tests"
              defaultValue={tests}
              onBlur={(e) =>
                set(
                  "standardized_tests",
                  Object.fromEntries(
                    list(e.target.value)
                      .map((t) => t.split(/\s+/))
                      .filter((parts) => parts.length >= 2)
                      .map((parts) => [parts[0]!.toUpperCase(), parts.slice(1).join(" ")]),
                  ),
                )
              }
              className="font-mono text-xs"
            />
          </Field>
          <TextField id="research" label="Research interests (comma separated)" value={value.research_interests.join(", ")} onChange={(v) => set("research_interests", list(v))} className="col-span-6" />
        </div>
      </Panel>
      <Panel title="Work preferences and status">
        <div className="grid grid-cols-6 gap-3 p-3">
          <Field id="work_mode" label="Preferred work mode">
            <Select
              id="work_mode"
              value={value.preferred_work_mode ?? "unset"}
              onValueChange={(v) => set("preferred_work_mode", v === "unset" ? null : (v as ProfileDetails["preferred_work_mode"]))}
              options={[{ value: "unset", label: "Not stated" }, ...workModes.map((m) => ({ value: m, label: m }))]}
            />
          </Field>
          <TextField id="start" label="Earliest start" type="date" value={value.earliest_start_date} onChange={(v) => set("earliest_start_date", orNull(v))} />
          {(
            [
              ["requires_visa_sponsorship", "Needs visa sponsorship"],
              ["has_valid_passport", "Valid passport"],
              ["financial_need_eligible", "Financial need"],
              ["first_generation_student", "First-generation student"],
            ] as const
          ).map(([key, label]) => (
            <Field key={key} id={key} label={label}>
              <Select id={key} value={tri(value[key])} onValueChange={(v) => set(key, fromTri(v))} options={TRI} />
            </Field>
          ))}
        </div>
      </Panel>
    </>
  );
}

interface PiiProps {
  value: PII;
  onChange: (next: PII) => void;
}

/** Personal profile v2 data (encrypted with the rest of the PII; never shown to a model). */
export function ProfilePiiPanels({ value, onChange }: PiiProps) {
  const set = (key: keyof PII, v: string) => onChange({ ...value, [key]: orNull(v) });
  const emergency = value.emergency_contact ?? { name: null, phone: null, relationship: null };
  const referees = value.referees ?? [];
  const setReferee = (i: number, key: keyof Referee, v: string) =>
    onChange({ ...value, referees: referees.map((r, j) => (j === i ? { ...r, [key]: orNull(v) } : r)) });
  return (
    <>
      <Panel title="Location, links and contacts" meta="encrypted">
        <div className="grid grid-cols-6 gap-3 p-3">
          <TextField id="city" label="City" value={value.city ?? null} onChange={(v) => set("city", v)} />
          <TextField id="state" label="State / province" value={value.state_or_province ?? null} onChange={(v) => set("state_or_province", v)} />
          <TextField id="postal" label="Postal code" value={value.postal_code ?? null} onChange={(v) => set("postal_code", v)} mono />
          <TextField id="gender" label="Gender" value={value.gender ?? null} onChange={(v) => set("gender", v)} />
          <TextField id="marital" label="Marital status" value={value.marital_status ?? null} onChange={(v) => set("marital_status", v)} className="col-span-2" />
          <TextField id="linkedin" label="LinkedIn" value={value.linkedin_url ?? null} onChange={(v) => set("linkedin_url", v)} mono className="col-span-3" />
          <TextField id="github" label="GitHub" value={value.github_url ?? null} onChange={(v) => set("github_url", v)} mono className="col-span-3" />
          <TextField id="portfolio" label="Portfolio / website" value={value.portfolio_url ?? null} onChange={(v) => set("portfolio_url", v)} mono className="col-span-3" />
          <TextField id="orcid" label="ORCID / Google Scholar" value={value.orcid_or_scholar_url ?? null} onChange={(v) => set("orcid_or_scholar_url", v)} mono className="col-span-3" />
          {(["name", "phone", "relationship"] as const).map((k) => (
            <TextField
              key={k}
              id={`emergency_${k}`}
              label={`Emergency contact ${k}`}
              value={emergency[k]}
              onChange={(v) => onChange({ ...value, emergency_contact: { ...emergency, [k]: orNull(v) } })}
              className="col-span-2"
            />
          ))}
        </div>
      </Panel>
      <Panel
        title="Referees / recommenders"
        meta={`${referees.length} / 3 · encrypted`}
        actions={
          referees.length < 3 ? (
            <Button size="sm" onClick={() => onChange({ ...value, referees: [...referees, EMPTY_REFEREE] })}>
              Add referee
            </Button>
          ) : null
        }
      >
        {referees.length === 0 ? <p className="px-3 py-2 text-xs text-faint">No referees yet.</p> : null}
        {referees.map((r, i) => (
          <div key={i} className="grid grid-cols-7 gap-2 border-b border-line p-3 last:border-b-0">
            {(["name", "title", "department", "organization", "email", "phone", "relationship"] as const).map((k) => (
              <TextField key={k} id={`ref${i}_${k}`} label={`${i + 1}: ${k}`} value={r[k]} onChange={(v) => setReferee(i, k, v)} mono={k === "email" || k === "phone"} />
            ))}
            <div className="col-span-7 flex justify-end">
              <Button size="sm" variant="ghost" onClick={() => onChange({ ...value, referees: referees.filter((_, j) => j !== i) })}>
                Remove
              </Button>
            </div>
          </div>
        ))}
      </Panel>
    </>
  );
}
