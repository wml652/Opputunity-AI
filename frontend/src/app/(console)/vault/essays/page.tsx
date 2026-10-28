import { EssayStudio } from "@/components/vault/essay-studio";
import { EssaySynthesizer } from "@/components/vault/essay-synthesizer";

export default function Page() {
  return (
    <div className="flex flex-col gap-3">
      <EssayStudio />
      <EssaySynthesizer />
    </div>
  );
}
