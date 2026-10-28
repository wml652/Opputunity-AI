"use client";

import { useRef, useState, type DragEvent } from "react";

import { Button } from "@/components/ui/button";
import { EmptyRow, Panel, td, th } from "@/components/ui/panel";
import { Select } from "@/components/ui/select";
import { StatusPill } from "@/components/ui/status";
import { ApiError } from "@/lib/api";
import { cn, formatRelative } from "@/lib/cn";
import type { DocKind } from "@/lib/schemas";
import {
  useDeleteDocument,
  useDocumentText,
  useDocuments,
  useUploadDocument,
} from "@/lib/vault-queries";

const KINDS: { value: DocKind; label: string }[] = [
  { value: "CV", label: "CV / Resume" },
  { value: "TRANSCRIPT", label: "Transcript" },
  { value: "ID", label: "ID document" },
  { value: "CERTIFICATE", label: "Certificate" },
  { value: "OTHER", label: "Other" },
];
const MAX_BYTES = 10 * 1024 * 1024;

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

export function DocumentVault() {
  const { data: documents, isLoading } = useDocuments();
  const upload = useUploadDocument();
  const remove = useDeleteDocument();
  const [kind, setKind] = useState<DocKind>("CV");
  const [dragging, setDragging] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  async function handleFiles(files: FileList | null) {
    const file = files?.[0];
    if (!file) return;
    if (file.size > MAX_BYTES) {
      setMessage({ tone: "err", text: `${file.name}: larger than 10 MB` });
      return;
    }
    setMessage(null);
    try {
      const doc = await upload.mutateAsync({ file, kind });
      setMessage({
        tone: "ok",
        text: `${doc.filename} stored encrypted · ${doc.page_count ?? "?"} pages · ${doc.text_chars.toLocaleString()} chars extracted`,
      });
      setSelected(doc.id);
    } catch (error) {
      setMessage({ tone: "err", text: error instanceof ApiError ? error.message : "Upload failed" });
    } finally {
      if (input.current) input.current.value = "";
    }
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    void handleFiles(event.dataTransfer.files);
  }

  return (
    <div className="flex flex-col gap-3">
      <Panel
        title="Upload document"
        actions={
          <Select
            aria-label="Document kind"
            value={kind}
            onValueChange={(v) => setKind(v as DocKind)}
            options={KINDS}
            className="w-40"
          />
        }
      >
        <div
          role="button"
          tabIndex={0}
          onClick={() => input.current?.click()}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && input.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cn(
            "m-3 flex h-28 cursor-pointer flex-col items-center justify-center gap-1 rounded-sm border border-dashed",
            dragging ? "border-muted bg-surface-2" : "border-line-strong hover:bg-surface-2",
          )}
        >
          <span className="text-sm text-fg">
            {upload.isPending ? "Encrypting and extracting text" : "Drop a PDF here or click to browse"}
          </span>
          <span className="text-2xs text-faint">
            PDF only (checked by file signature) · max 10 MB · stored AES-256-GCM encrypted
          </span>
          <input
            ref={input}
            type="file"
            accept="application/pdf,.pdf"
            className="hidden"
            onChange={(e) => void handleFiles(e.target.files)}
          />
        </div>
        {message ? (
          <p
            className={cn(
              "border-t border-line px-3 py-1.5 text-xs",
              message.tone === "ok" ? "text-ok" : "bg-err-soft text-err",
            )}
          >
            {message.text}
          </p>
        ) : null}
      </Panel>

      <div className="grid grid-cols-5 gap-3">
        <Panel
          title="Vault documents"
          meta={documents ? `${documents.length} files` : undefined}
          className="col-span-3"
        >
          <table className="w-full border-collapse">
            <thead>
              <tr>
                <th className={th}>Kind</th>
                <th className={th}>File</th>
                <th className={th}>Size</th>
                <th className={th}>Pages</th>
                <th className={th}>SHA-256</th>
                <th className={th}>Added</th>
                <th className={cn(th, "text-right")} />
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <EmptyRow colSpan={7}>Loading</EmptyRow>
              ) : !documents?.length ? (
                <EmptyRow colSpan={7}>No documents yet. Upload your CV and transcript.</EmptyRow>
              ) : (
                documents.map((doc) => (
                  <tr
                    key={doc.id}
                    onClick={() => setSelected(doc.id)}
                    className={cn("cursor-pointer hover:bg-surface-2", selected === doc.id && "bg-surface-2")}
                  >
                    <td className={cn(td, "font-mono text-2xs text-muted")}>{doc.kind}</td>
                    <td className={cn(td, "max-w-[220px] truncate text-xs")} title={doc.filename}>
                      {doc.filename}
                    </td>
                    <td className={cn(td, "font-mono text-2xs tabular")}>{formatBytes(doc.size_bytes)}</td>
                    <td className={cn(td, "font-mono text-2xs tabular")}>{doc.page_count ?? "—"}</td>
                    <td className={cn(td, "font-mono text-2xs text-faint")} title={doc.sha256}>
                      {doc.sha256.slice(0, 12)}
                    </td>
                    <td className={cn(td, "text-2xs text-faint")}>{formatRelative(doc.created_at)}</td>
                    <td className={cn(td, "text-right")}>
                      <div className="flex justify-end gap-1">
                        <Button size="sm" asChild>
                          <a
                            href={`/api/v1/documents/${doc.id}/download`}
                            onClick={(e) => e.stopPropagation()}
                          >
                            Download
                          </a>
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          disabled={remove.isPending}
                          onClick={(e) => {
                            e.stopPropagation();
                            if (window.confirm(`Delete ${doc.filename}? The encrypted file is destroyed.`)) {
                              remove.mutate(doc.id);
                              if (selected === doc.id) setSelected(null);
                            }
                          }}
                        >
                          Delete
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </Panel>
        <TextPreview id={selected} />
      </div>
    </div>
  );
}

function TextPreview({ id }: { id: string | null }) {
  const { data, isLoading } = useDocumentText(id);
  return (
    <Panel
      title="Extracted text"
      meta={data ? `${data.document.text_chars.toLocaleString()} chars` : undefined}
      actions={data ? <StatusPill tone="ok">decrypted for you</StatusPill> : null}
      className="col-span-2"
    >
      <pre className="h-[420px] overflow-auto whitespace-pre-wrap px-3 py-2 font-mono text-2xs text-muted">
        {!id
          ? "Select a document to preview the text the agent will use for form filling."
          : isLoading
            ? "Decrypting"
            : data?.text || "(no extractable text: scanned image or protected PDF)"}
        {data?.truncated ? "\n\n[preview truncated]" : ""}
      </pre>
    </Panel>
  );
}
