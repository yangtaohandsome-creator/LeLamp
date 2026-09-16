import { describe, expect, it, vi } from "vitest";
import { SearchRouter, type SearchSource } from "./search.js";

function source(name: string, implementation: SearchSource["search"]): SearchSource {
  return { name, search: implementation };
}

describe("SearchRouter", () => {
  it("uses the primary source without touching fallback", async () => {
    const fallback = vi.fn();
    const router = new SearchRouter(
      source("primary", async () => ({ content: [{ type: "text", text: "ok" }] })),
      source("fallback", fallback),
    );
    await expect(router.search("上海天气")).resolves.toMatchObject({ source: "primary" });
    expect(fallback).not.toHaveBeenCalled();
  });

  it("uses fallback only when primary fails", async () => {
    const fallback = vi.fn(async () => ({ content: [{ type: "text", text: "fallback" }] }));
    const router = new SearchRouter(
      source("primary", async () => { throw new Error("primary down"); }),
      source("fallback", fallback),
    );
    await expect(router.search("科技新闻")).resolves.toMatchObject({ source: "fallback" });
    expect(fallback).toHaveBeenCalledOnce();
  });

  it("reports both failures", async () => {
    const router = new SearchRouter(
      source("primary", async () => { throw new Error("p"); }),
      source("fallback", async () => { throw new Error("f"); }),
    );
    await expect(router.search("天气")).rejects.toThrow("主源 p；备用源 f");
  });
});
