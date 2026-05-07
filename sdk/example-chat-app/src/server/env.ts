import { existsSync } from "node:fs";
import { resolve } from "node:path";

export interface LoadExampleEnvInput {
  envPath?: string;
  loadEnvFile?: (path: string) => void;
}

export function loadExampleEnv({
  envPath = resolve(process.cwd(), ".env"),
  loadEnvFile = process.loadEnvFile?.bind(process),
}: LoadExampleEnvInput = {}) {
  if (!loadEnvFile || !existsSync(envPath)) return false;
  loadEnvFile(envPath);
  return true;
}
