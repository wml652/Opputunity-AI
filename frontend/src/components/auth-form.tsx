"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/field";
import { Panel } from "@/components/ui/panel";
import { ApiError } from "@/lib/api";
import { useLogin, useMe } from "@/lib/queries";

const schema = z.object({
  email: z.string().email("Enter a valid email"),
  password: z.string().min(10, "At least 10 characters"),
  display_name: z.string().max(120).optional(),
});

export function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const login = useLogin(mode);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  // Until React hydrates, a click would trigger a native form submit (page reload, and with GET
  // the credentials would land in the URL). Keep the button disabled until handlers are live.
  const [ready, setReady] = useState(false);
  useEffect(() => setReady(true), []);
  // Already authenticated (or DEV_AUTH_BYPASS is on): skip the form.
  const me = useMe();
  useEffect(() => {
    if (me.data) router.replace("/");
  }, [me.data, router]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const parsed = schema.safeParse({
      email: form.get("email"),
      password: form.get("password"),
      display_name: mode === "register" ? (form.get("display_name") ?? "") : undefined,
    });
    if (!parsed.success) {
      setErrors(
        Object.fromEntries(parsed.error.issues.map((i) => [String(i.path[0]), i.message])),
      );
      return;
    }
    setErrors({});
    setFormError(null);
    try {
      await login.mutateAsync(parsed.data);
      router.replace("/");
    } catch (error) {
      setFormError(error instanceof ApiError ? error.message : "Request failed");
    }
  }

  const isRegister = mode === "register";
  return (
    <Panel title={isRegister ? "Create account" : "Sign in"}>
      <form
        onSubmit={onSubmit}
        method="post"
        action="#"
        className="flex flex-col gap-3 p-3"
        noValidate
      >
        {isRegister ? (
          <Field id="display_name" label="Name">
            <Input id="display_name" name="display_name" autoComplete="name" />
          </Field>
        ) : null}
        <Field id="email" label="Email" error={errors.email}>
          <Input id="email" name="email" type="email" autoComplete="email" required />
        </Field>
        <Field id="password" label="Password" error={errors.password}>
          <Input
            id="password"
            name="password"
            type="password"
            autoComplete={isRegister ? "new-password" : "current-password"}
            required
          />
        </Field>
        {formError ? (
          <p className="rounded-sm border border-err/30 bg-err-soft px-2 py-1 text-xs text-err">
            {formError}
          </p>
        ) : null}
        <Button type="submit" variant="primary" disabled={!ready || login.isPending}>
          {!ready ? "Loading" : login.isPending ? "Working" : isRegister ? "Create account" : "Sign in"}
        </Button>
        <p className="text-xs text-muted">
          {isRegister ? "Have an account? " : "No account yet? "}
          <Link
            href={isRegister ? "/login" : "/register"}
            className="text-fg underline underline-offset-2"
          >
            {isRegister ? "Sign in" : "Create one"}
          </Link>
        </p>
      </form>
    </Panel>
  );
}
