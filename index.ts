import { config } from "dotenv";
import { streamText } from "ai";

config({ path: ".env.local" });

async function main() {
  if (!process.env.AI_GATEWAY_API_KEY) {
    throw new Error(
      "AI_GATEWAY_API_KEY is missing. Add it to .env.local from https://vercel.com/[team]/~/ai-gateway/api-keys",
    );
  }

  const result = streamText({
    model: "openai/gpt-5.4",
    prompt: "Say hello in one short sentence.",
  });

  for await (const chunk of result.textStream) {
    process.stdout.write(chunk);
  }
  process.stdout.write("\n");

  const usage = await result.usage;
  console.log("Token usage:", {
    inputTokens: usage.inputTokens,
    outputTokens: usage.outputTokens,
    totalTokens: usage.totalTokens,
  });
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
