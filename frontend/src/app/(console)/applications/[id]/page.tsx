import { ApplicationInspector } from "@/components/applications/application-inspector";

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ApplicationInspector id={id} />;
}
