import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { PI_TOOL_CONTRACTS } from "./tools.js";

describe("Agent tool contract", () => {
  it("has no duplicate Pi tool names", () => {
    expect(new Set(PI_TOOL_CONTRACTS).size).toBe(PI_TOOL_CONTRACTS.length);
  });

  it("matches the OpenClaw high-level tool set", () => {
    const source = readFileSync(
      resolve(process.cwd(), "../openclaw-plugin/src/index.ts"), "utf8",
    );
    const names = [...source.matchAll(/name:\s*"(lelamp_[a-z0-9_]+)"/g)]
      .map((match) => match[1]);
    expect([...new Set(names)].sort()).toEqual([...PI_TOOL_CONTRACTS].sort());
  });
});
