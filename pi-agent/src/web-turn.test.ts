import { afterEach, expect, it, vi } from "vitest";
import { createTools, type RequestState } from "./tools.js";
import type { SearchRouter } from "./search.js";

afterEach(() => vi.unstubAllGlobals());

it("carries the conversation turn to hardware and reports rejected stale turns", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(
    JSON.stringify({ ok: false, message: "原 Agent 轮次已结束" }), { status: 409 },
  ));
  vi.stubGlobal("fetch", fetchMock);
  const state: RequestState = { toolCalls: [], searchCount: 0, turnId: "web-turn-test" };
  const tool = createTools(state, {} as SearchRouter).find(t => t.name === "lelamp_play_motion")!;
  await expect(tool.execute("call", { name: "nod" }, undefined)).rejects.toThrow("原 Agent 轮次已结束");
  expect(fetchMock.mock.calls[0][1].headers["X-LeLamp-Turn"]).toBe("web-turn-test");
  expect(state.toolCalls[0].ok).toBe(false);
});
